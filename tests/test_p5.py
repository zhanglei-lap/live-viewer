import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from types import SimpleNamespace
from live_viewer.rtmp import Peer, amf, decode_amf, flv_tags, RtmpEndpoint
from live_viewer.resources import CacheLease
from live_viewer.potplayer import valid_local_url
from live_viewer.core import ViewerError


def flv_tag(kind, payload, stamp=123):
    return (bytes([kind]) + len(payload).to_bytes(3, "big") + (stamp & 0xffffff).to_bytes(3, "big") +
            bytes([stamp >> 24]) + b"\0\0\0" + payload + (len(payload) + 11).to_bytes(4, "big"))


class RtmpTests(unittest.TestCase):
    def test_amf_roundtrip_connect_values(self):
        values = ["connect", 1, {"app": "test", "objectEncoding": 0, "flag": True}, None]
        self.assertEqual(decode_amf(b"".join(amf(v) for v in values)), values)

    def test_malformed_and_deep_amf_is_rejected(self):
        for payload in (b"\x02\xff\xffx", b"\x03\x00\x02a", b"\xff"):
            with self.assertRaises((ValueError, IndexError, UnicodeError)):
                decode_amf(payload)
        nested = "end"
        for _ in range(8): nested = {"child": nested}
        with self.assertRaises(ValueError): decode_amf(amf(nested))

    def test_rtmp_extended_timestamp_and_chunk_continuations(self):
        a, b = socket.socketpair()
        a.settimeout(2);b.settimeout(2)
        try:
            sender, receiver = Peer(a), Peer(b)
            receiver.chunk = 4096
            payload = bytes(range(256)) * 20
            sender.send(9, payload, 0x12345678, 1, 6)
            kind, received, sid = receiver.receive()
            self.assertEqual((kind, received, sid), (9, payload, 1))
            self.assertEqual(receiver.states[6]["stamp"], 0x12345678)
        finally: a.close();b.close()

    def test_flv_parser_accepts_arbitrary_network_chunk_boundaries(self):
        payload = b"FLV\x01\x05\0\0\0\x09\0\0\0\0" + flv_tag(9, b"video", 0x12345678) + flv_tag(8, b"audio")
        chunks = [payload[i:i + 2] for i in range(0, len(payload), 2)]
        self.assertEqual(list(flv_tags(chunks)), [(9, b"video", 0x12345678), (8, b"audio", 123)])

    def test_invalid_tag_boundaries_never_get_forwarded(self):
        header = b"FLV\x01\x05\0\0\0\x09\0\0\0\0"
        wrong = flv_tag(9, b"payload")[:-4] + b"\0\0\0\0"
        with self.assertRaises(ValueError): list(flv_tags([header + wrong]))

    def test_endpoint_only_binds_loopback_and_closes_listener(self):
        endpoint = RtmpEndpoint()
        endpoint.prepare()
        address = endpoint.socket.getsockname()
        self.assertEqual(address[0], "127.0.0.1")
        self.assertTrue(valid_local_url(endpoint.url).startswith("rtmp://127.0.0.1:"))
        endpoint.close()
        self.assertTrue(endpoint.closed)

    def test_upstream_refresh_keeps_socket_media_and_continuous_timestamps(self):
        endpoint = RtmpEndpoint()
        header = b"FLV\x01\x05\0\0\0\x09\0\0\0\0"
        bodies = (b"\x17\x01first", b"\x17\x01second", b"\x17\x01third", b"\x17\x01fourth")
        inputs = iter((header + flv_tag(9, bodies[0], 100000) + flv_tag(9, bodies[1], 100040),
                       header + flv_tag(9, bodies[2], 0) + flv_tag(9, bodies[3], 40)))
        emitted = []
        relay = SimpleNamespace(stop_event=threading.Event(), active=threading.Lock(),
                                source_interrupted=threading.Event(), connection=None,
                                http=SimpleNamespace(_emit=lambda code, *a, **kw: emitted.append(code)))
        def source():
            data = next(inputs, None)
            if data is None:
                return iter(())
            return iter((data,))
        relay.source = source
        relay.stop = lambda: relay.stop_event.set()
        relay.disconnect = lambda: relay.stop_event.set()
        endpoint.attach(relay)
        client = socket.create_connection(endpoint.socket.getsockname(), timeout=3)
        try:
            peer = Peer(client)
            client.sendall(b"\x03" + b"\0" * 1536)
            server_hello = peer.read(3073)
            client.sendall(server_hello[1:1537])
            peer.command("connect", 1, {"app": endpoint.token})
            peer.command("createStream", 2, None)
            peer.command("play", 0, None, "live.flv", stream=1)
            received = []
            while len(received) < 4:
                kind, payload, sid = peer.receive()
                if kind == 9:
                    received.append((payload, peer.states[6]["stamp"], sid))
            self.assertEqual(received, [(bodies[i], i * 40, 1) for i in range(4)])
            self.assertIn("relay.cache_input_refreshed", emitted)
            self.assertFalse(relay.stop_event.is_set())
            connection = relay.connection
            duplicate = socket.create_connection(endpoint.socket.getsockname(), timeout=3)
            try:
                other = Peer(duplicate)
                duplicate.sendall(b"\x03" + b"\0" * 1536)
                hello = other.read(3073)
                duplicate.sendall(hello[1:1537])
                other.command("connect", 1, {"app": endpoint.token})
                other.command("play", 0, None, "live.flv", stream=1)
                with self.assertRaises(EOFError):
                    while True:
                        other.receive()
                self.assertIs(relay.connection, connection)
                self.assertTrue(relay.active.locked())
            finally:
                duplicate.close()
            self.assertTrue(relay.source_interrupted.wait(2))
        finally:
            endpoint.close()
            client.close()

    def test_rtmp_cannot_point_player_at_a_remote_or_credential_url(self):
        for url in ("rtmp://remote.example/live.flv", "rtmp://user:secret@127.0.0.1:123/live.flv",
                    "rtmp://127.0.0.1:123/live.flv?token=secret"):
            with self.assertRaises(ViewerError): valid_local_url(url)


class CacheTests(unittest.TestCase):
    def test_owned_temp_cache_is_under_data_and_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = CacheLease(temporary)
            self.assertEqual(cache.environment()["TEMP"], str(cache.path))
            (cache.path / "test-video.tmp").write_bytes(b"synthetic")
            cache.close()
            self.assertFalse(cache.path.exists())
            cache.close()

    def test_cleanup_requires_the_same_ownership_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = CacheLease(temporary)
            marker = cache.path / "owner.json"
            marker.write_text('{"identity":"other","native_temp_cache":1}')
            with self.assertRaises(ValueError): cache.close()
            self.assertTrue(cache.path.exists())
            marker.write_text(json.dumps({"identity": cache.identity, "native_temp_cache": 1}))
            cache.close()

    def test_cleanup_rejects_a_path_outside_its_exact_owned_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = CacheLease(temporary)
            original = cache.path
            other = Path(temporary) / "keep"
            other.mkdir();(other / "file.txt").write_text("keep")
            cache.path = other
            with self.assertRaises(ValueError): cache.close()
            self.assertTrue((other / "file.txt").exists())
            cache.path = original;cache.close()

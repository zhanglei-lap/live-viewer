import io
import socket
from dataclasses import replace
import threading
import unittest
from unittest.mock import patch
import requests
from live_viewer.vod_proxy import VodProxy, byte_range
from live_viewer.core import ViewerError
from vod_fixtures import source

PAYLOAD = b"\0\0\0\x18ftyp" + bytes(range(256)) * 30


class Response:
    def __init__(self, start, end, total, status=206):
        self.status_code = status
        self.headers = {"Content-Range": f"bytes {start}-{end}/{total}"}
        self.payload = PAYLOAD[start:end + 1]
        self.raw = io.BytesIO(self.payload)
    def __enter__(self):return self
    def __exit__(self, *args):self.close()
    def close(self):pass
    def iter_content(self, size):yield self.payload


class Session:
    def __init__(self, owner):self.owner = owner
    def __enter__(self):return self
    def __exit__(self, *args):pass
    def close(self):pass
    def get(self, url, *, headers, **kwargs):
        self.owner.calls.append((url, dict(headers)))
        interval = byte_range(headers.get("Range"), len(PAYLOAD))
        start, end = interval or (0, len(PAYLOAD) - 1)
        response = Response(start, end, len(PAYLOAD))
        if self.owner.corrupt:response.headers["Content-Range"] = "bytes 0-1/2"
        return response


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.calls = [];self.corrupt = False;self.events = []
        self.proxy = VodProxy(source(), on_event=lambda *args, **kw: self.events.append((args, kw)))
        self.patch = patch.object(self.proxy, "_session", side_effect=lambda: Session(self));self.patch.start()
        self.proxy.prepare();self.proxy.start()
        self.client = requests.Session();self.client.trust_env = False
    def tearDown(self):
        self.client.close();self.proxy.stop();self.patch.stop()

    def test_full_get_head_and_exact_partial_response(self):
        with self.client.head(self.proxy.url) as head:
            self.assertEqual(head.status_code, 200);self.assertEqual(int(head.headers["Content-Length"]), len(PAYLOAD))
        with self.client.get(self.proxy.url, headers={"Range": "bytes=10-29", "Cookie": "local-cookie=secret", "Authorization": "secret"}) as part:
            self.assertEqual(part.status_code, 206);self.assertEqual(part.content, PAYLOAD[10:30])
            self.assertEqual(part.headers["Content-Range"], f"bytes 10-29/{len(PAYLOAD)}")
        self.assertEqual(self.calls[-1][1], {"Range": "bytes=10-29"})
        with self.client.get(self.proxy.audio_url) as full:
            self.assertEqual(full.status_code, 200);self.assertEqual(full.content, PAYLOAD)

    def test_suffix_open_ended_and_clamped_range(self):
        for value, expected in (("bytes=-5", PAYLOAD[-5:]), ("bytes=7000-", PAYLOAD[7000:]), ("bytes=7000-99999", PAYLOAD[7000:])):
            with self.client.get(self.proxy.url, headers={"Range": value}) as response:
                self.assertEqual(response.status_code, 206);self.assertEqual(response.content, expected)

    def test_invalid_ranges_are_416_without_upstream(self):
        count = len(self.calls)
        for value in ("bytes=10-2", "bytes=99999-", "bytes=-0", "bytes=0-1,5-6", "bad"):
            with self.client.get(self.proxy.url, headers={"Range": value}) as response:self.assertEqual(response.status_code, 416)
        self.assertEqual(len(self.calls), count)

    def test_private_token_and_exact_routes_cannot_be_bypassed(self):
        count = len(self.calls)
        root = self.proxy.url.rsplit("/", 2)[0]
        for url in (root + "/wrong/vod-video.mp4", self.proxy.url + "?url=https://private", self.proxy.url.replace("vod-video.mp4", "unknown.mp4")):
            with self.client.get(url) as response:self.assertEqual(response.status_code, 404)
        self.assertEqual(len(self.calls), count)
        self.assertEqual(self.proxy.server.server_address[0], "127.0.0.1")

    def test_upstream_range_mismatch_is_error_not_corrupted_media(self):
        self.corrupt = True
        with self.client.get(self.proxy.url, headers={"Range": "bytes=10-20"}) as response:self.assertEqual(response.status_code, 502)
        self.assertEqual(self.events[-1][0][0], "vod.range_invalid")
        self.assertEqual(self.events[-1][1]["impact"], "interrupted")
        self.assertNotIn("private-test", str(self.events))

    def test_stop_closes_the_owned_listener(self):
        address = self.proxy.server.server_address
        self.proxy.stop()
        with self.assertRaises(OSError):socket.create_connection(address, timeout=.3)
        self.assertEqual(self.proxy.snapshot()["status"], "stopped")

    def test_redirects_cannot_escape_media_host_scope(self):
        class Redirect(Session):
            def get(self, *args, **kw):
                response = Response(0, 15, len(PAYLOAD), 302)
                response.headers["Location"] = "http://127.0.0.1/private"
                return response
        with self.assertRaises(ViewerError) as raised:self.proxy._request(Redirect(self), self.proxy.source_info.selected_video, {"Range": "bytes=0-15"})
        self.assertEqual(raised.exception.code, "vod.cdn_unsupported")

    def test_expired_range_refreshes_both_tracks_and_keeps_local_address(self):
        old = self.proxy.url
        fresh = source()
        fresh.selected_video = replace(fresh.selected_video, urls=("https://example.bilivideo.com/new-video.m4s",))
        self.proxy.refresher = lambda old: fresh
        original = Session.get
        def get(session, url, **kwargs):
            if "expired" in url:return Response(0, 15, len(PAYLOAD), 403)
            return original(session, url, **kwargs)
        self.proxy.tracks["vod-video.mp4"] = replace(self.proxy.tracks["vod-video.mp4"], urls=("https://example.bilivideo.com/expired.m4s",))
        with patch.object(Session, "get", get):
            with self.client.get(self.proxy.url, headers={"Range": "bytes=100-199"}) as response:
                self.assertEqual(response.status_code, 206);self.assertEqual(response.content, PAYLOAD[100:200])
        self.assertEqual(self.proxy.refreshes, 1);self.assertEqual(self.proxy.url, old)

    def test_parallel_expiry_refreshes_once_for_the_same_generation(self):
        calls = []
        self.proxy.refresher = lambda old: (calls.append(1), source())[1]
        threads = [threading.Thread(target=lambda: self.proxy.refresh(0)) for _ in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertEqual(len(calls), 1);self.assertEqual(self.proxy.refreshes, 1)

    def test_changed_media_fingerprint_stops_refresh_without_replacing_tracks(self):
        original = self.proxy.tracks
        self.proxy.refresher = lambda old: source()
        self.proxy.identities["vod-video.mp4"] = "different-file"
        with self.assertRaises(ViewerError) as raised:self.proxy.refresh(0)
        self.assertEqual(raised.exception.code, "vod.media_changed")
        self.assertIs(self.proxy.tracks, original)

    def test_stop_during_refresh_discards_the_late_source(self):
        self.proxy.refresher = lambda old: (self.proxy.stop_event.set(), source())[1]
        with self.assertRaises(ViewerError) as raised:self.proxy.refresh(0)
        self.assertEqual(raised.exception.code, "playback.cancelled")
        self.assertEqual(self.proxy.refreshes, 0)

    def test_mid_body_disconnect_resumes_exact_remaining_bytes(self):
        original = Session.get;calls = []
        def get(session, url, **kwargs):
            response = original(session, url, **kwargs)
            if kwargs["headers"].get("Range") == "bytes=100-199" and not calls:
                calls.append(1)
                def broken(size):
                    yield response.payload[:30]
                    raise requests.ConnectionError("synthetic disconnect")
                response.iter_content = broken
            return response
        with patch.object(Session, "get", get):
            with self.client.get(self.proxy.url, headers={"Range": "bytes=100-199"}) as response:
                self.assertEqual(response.content, PAYLOAD[100:200])
        self.assertEqual(self.calls[-1][1]["Range"], "bytes=130-199")

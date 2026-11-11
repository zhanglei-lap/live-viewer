import json
import threading
import time
import unittest
from unittest.mock import patch

import requests

from live_viewer.core import PlatformClient, Stream
from live_viewer.live_buffer import LiveBuffer, RelayBacklog
from live_viewer.relay import LocalRelay
from live_viewer.timing import FlvClock


def tag(timestamp, kind=9, payload=b"\x27\x01abc"):
    return (bytes([kind]) + len(payload).to_bytes(3, "big") + (timestamp & 0xFFFFFF).to_bytes(3, "big")
            + bytes([timestamp >> 24]) + b"\0\0\0" + payload + (len(payload) + 11).to_bytes(4, "big"))


HEADER = b"FLV\x01\x05\0\0\0\x09\0\0\0\0"


class ClockTests(unittest.TestCase):
    def test_fragmented_tags_ignore_sequence_headers_and_use_complete_media(self):
        clock = FlvClock()
        data = HEADER + tag(0, payload=b"\x17\x00config") + tag(200000) + tag(201000)
        for chunk in (data[:2], data[2:10], data[10:21], data[21:-2]):
            clock.feed(chunk, 10)
        self.assertEqual(clock.snapshot(11)["media_seconds"], 0)
        clock.feed(data[-2:], 11)
        self.assertEqual(clock.snapshot(11)["media_seconds"], 1)
        self.assertEqual(clock.snapshot(11)["clock_drift_seconds"], 0)

    def test_timestamp_wrap_and_minor_audio_reordering_do_not_create_false_drift(self):
        clock = FlvClock()
        clock.feed(HEADER + tag(2**32 - 500), 10)
        clock.feed(tag(500), 11)
        clock.feed(tag(480, kind=8, payload=b"\xaf\x01audio"), 11)
        self.assertEqual(clock.snapshot(11)["media_seconds"], 1)
        self.assertEqual(clock.snapshot(11)["timestamp_discontinuities"], 0)

    def test_major_timestamp_reset_reanchors_observation(self):
        clock = FlvClock()
        clock.feed(HEADER + tag(60000), 10)
        clock.feed(tag(1000), 11)
        self.assertEqual(clock.snapshot(11)["timestamp_discontinuities"], 1)
        self.assertEqual(clock.snapshot(11)["clock_drift_seconds"], 0)

    def test_clock_drift_is_relative_progress_and_does_not_claim_display_latency(self):
        clock = FlvClock()
        clock.feed(HEADER + tag(100000), 20)
        clock.feed(tag(105000), 30)
        self.assertEqual(clock.snapshot(30)["clock_drift_seconds"], 5)
        self.assertNotIn("player_latency", clock.snapshot(30))


class BufferTests(unittest.TestCase):
    def test_normal_fifo_is_byte_exact_and_drains_at_eof(self):
        fifo = LiveBuffer()
        fifo.put(b"first"); fifo.put(b"second"); fifo.finish()
        self.assertEqual(fifo.get(), b"first")
        self.assertEqual(fifo.get(), b"second")
        self.assertIsNone(fifo.get())

    def test_memory_overflow_aborts_instead_of_silently_dropping_bytes(self):
        fifo = LiveBuffer(max_bytes=8)
        fifo.put(b"12345678")
        with self.assertRaises(RelayBacklog): fifo.put(b"9")
        with self.assertRaises(RelayBacklog): fifo.get()
        self.assertEqual(fifo.snapshot()["peak_pending_bytes"], 8)

    def test_age_limit_aborts_stale_connection(self):
        now = [10]
        fifo = LiveBuffer(clock=lambda: now[0])
        fifo.put(b"frame")
        now[0] = 13.1
        with self.assertRaises(RelayBacklog): fifo.get()

    def test_cancellation_wakes_empty_consumer(self):
        fifo = LiveBuffer()
        finished = threading.Event()
        thread = threading.Thread(target=lambda: (fifo.get(), finished.set()), daemon=True)
        thread.start();fifo.stop()
        self.assertTrue(finished.wait(1))
        thread.join(1)


class GuardRelayTests(unittest.TestCase):
    def test_backlog_disconnect_preserves_header_and_does_not_splice_fallback(self):
        class Response:
            def close(self): pass
            def iter_content(self, _):
                yield HEADER + tag(10000)
                yield tag(11000, payload=b"\x27\x01" + b"a" * 8192)
        stream = Stream("douyu", "6979222", 0, 4, "blue", [], ["https://a.edgesrv.com/live.flv"], backup_cdns=["hw-h5"])
        with patch.object(PlatformClient, "resolve", return_value=stream), \
             patch.object(PlatformClient, "_douyu_play") as fallback, \
             patch("live_viewer.relay.open_media", return_value=Response()), \
             patch("live_viewer.relay.LiveBuffer", side_effect=lambda: LiveBuffer(max_bytes=4096)):
            relay = LocalRelay("douyu", "6657", None)
            relay.start()
            try:
                with requests.get(relay.url, timeout=5) as response:
                    self.assertEqual(response.content, HEADER + tag(10000))
                deadline = time.monotonic() + 1
                while not relay.snapshot()["reconnect_recommended"] and time.monotonic() < deadline:
                    time.sleep(0.01)
                stats = relay.snapshot()
                self.assertTrue(stats["reconnect_recommended"])
                self.assertEqual(stats["slow_reader_disconnects"], 1)
                self.assertFalse(stats["timing"]["player_display_latency_measured"])
                fallback.assert_not_called()
            finally:
                relay.stop()

    def test_compatibility_mode_preserves_stream_and_excludes_signed_addresses(self):
        payload = HEADER + tag(10000) + tag(11000)
        class Response:
            def close(self): pass
            def iter_content(self, _): yield payload
        stream = Stream("bilibili", "35", 10000, 250, "720p", [], ["https://a.bilivideo.com/SECRET"])
        with patch.object(PlatformClient, "resolve", return_value=stream), patch("live_viewer.relay.open_media", return_value=Response()):
            relay = LocalRelay("bilibili", "35", None, low_latency=False)
            relay.start()
            try:
                with requests.get(relay.url, timeout=5) as response:
                    self.assertEqual(response.content, payload)
                stats = relay.snapshot()
                self.assertEqual(stats["timing"]["forwarded_media_seconds"], 1)
                self.assertNotIn("SECRET", json.dumps(stats))
            finally:
                relay.stop()


if __name__ == "__main__":
    unittest.main()

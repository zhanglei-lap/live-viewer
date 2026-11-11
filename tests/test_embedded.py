import ctypes as C
import json
import unittest
from unittest.mock import Mock, patch

from live_viewer.core import ViewerError
from live_viewer.mpv_player import ClientMessage, Event, EndFile, EventProperty, MpvPlayer, property_value, validate_local_stream
from live_viewer.reconnect import ReconnectPolicy


def fake_player(events=()):
    player = object.__new__(MpvPlayer)
    player.closed = False;player.handle = 1;player.props = {};player.state = "playing"
    player.entry_id = 7;player.ignored_entries = set();player.load_serial = 1
    player.command_serial = 1000;player.load_request_id = None;player.last_error = None
    player.pending_stop_id = None;player.pending_url = None
    player.lib = Mock()
    player.lib.mpv_command_async.return_value = 0
    queue = list(events) + [Event(0, 0, 0, None)]
    player.lib.mpv_wait_event.side_effect = lambda *_: C.pointer(queue.pop(0))
    return player


class NativeBindingTests(unittest.TestCase):
    def test_native_window_shutdown_is_user_close_not_reconnect_failure(self):
        player = fake_player([Event(1, 0, 0, None)])
        self.assertEqual(player.poll(), [{"kind": "closed"}])
        self.assertEqual(player.state, "closed")

    def test_live_button_script_message_reaches_room_controller(self):
        args = (C.c_char_p * 1)(b'viewer-return-live')
        message = ClientMessage(1, args)
        player = fake_player([Event(16, 0, 0, C.addressof(message))])
        self.assertEqual(player.poll(), [{"kind": "reconnect"}])

    def test_load_after_user_stop_waits_for_stop_completion(self):
        player = fake_player()
        player.pending_stop_id = 777
        player.load("http://127.0.0.1:1234/opaque/live.flv")
        player.lib.mpv_command_async.assert_not_called()
        queue = [Event(5, 0, 777, None), Event(0, 0, 0, None)]
        player.lib.mpv_wait_event.side_effect = lambda *_: C.pointer(queue.pop(0))
        player.poll()
        self.assertIsNone(player.pending_stop_id)
        player.lib.mpv_command_async.assert_called_once()

    def test_media_replacement_uses_single_playlist_and_local_url_only(self):
        player = fake_player()
        player.load("http://127.0.0.1:1234/opaque/live.flv")
        args = player.lib.mpv_command_async.call_args.args[2]
        self.assertEqual([args[i] for i in range(5)], [b"loadfile", b"http://127.0.0.1:1234/opaque/live.flv", b"replace", b"-1", b"pause=no"])
        self.assertIn(7, player.ignored_entries)
        self.assertIsNone(player.entry_id)
        self.assertEqual(player.load_serial, 2)
        for url in ["https://cdn.example/live.flv?SECRET", "file:///SECRET", "http://127.0.0.1:1234/opaque/live.flv?cookie=SECRET"]:
            with self.assertRaises(ViewerError): validate_local_stream(url)

    def test_late_end_from_replaced_entry_does_not_fail_current_playback(self):
        old = EndFile(4, -13, 7, 0, 0)
        player = fake_player([Event(7, 0, 0, C.addressof(old))])
        player.entry_id = 8;player.ignored_entries.add(7)
        self.assertEqual(player.poll(), [])
        self.assertEqual(player.state, "playing")

    def test_current_end_triggers_reconnect_event_without_exposing_url(self):
        current = EndFile(4, -13, 7, 0, 0)
        player = fake_player([Event(7, 0, 0, C.addressof(current))])
        self.assertEqual(player.poll()[0]["kind"], "ended")
        self.assertEqual(player.state, "error")
        player.props["path"] = "SECRET_URL"
        self.assertNotIn("SECRET_URL", json.dumps(player.snapshot()))

    def test_old_generation_property_reply_is_ignored(self):
        value = C.c_double(999)
        prop = EventProperty(b"time-pos", 5, C.addressof(value))
        player = fake_player([Event(3, 0, 10101, C.addressof(prop))]);player.load_serial = 2
        player.poll()
        self.assertNotIn("time-pos", player.props)

    def test_c_property_formats_match_string_pointer_and_integer_width(self):
        value = C.c_char_p("中文解码器".encode("utf-8"))
        self.assertEqual(property_value(EventProperty(b"codec", 1, C.addressof(value))), "中文解码器")
        number = C.c_int64(2**40)
        self.assertEqual(property_value(EventProperty(b"counter", 4, C.addressof(number))), 2**40)
        self.assertIsNone(property_value(EventProperty(b"unknown", 0, None)))


class ReconnectTests(unittest.TestCase):
    def test_backoff_is_bounded_and_repeated_failures_do_not_reset_pending_timer(self):
        now = [0.0];policy = ReconnectPolicy(clock=lambda: now[0]);policy.start()
        for delay in (1, 2, 5, 10, 20, 30, 30):
            policy.failure();due = policy.due;policy.failure()
            self.assertEqual(policy.due, due)
            self.assertEqual(due - now[0], delay)
            self.assertFalse(policy.consume());now[0] = due
            self.assertTrue(policy.consume())
        self.assertEqual(policy.total_attempts, 7)

    def test_user_stop_cancels_pending_retry(self):
        now = [0.0];policy = ReconnectPolicy(clock=lambda: now[0]);policy.start();policy.failure();policy.stop()
        now[0] = 100
        self.assertFalse(policy.consume());self.assertIsNone(policy.due)

    def test_user_pause_suppresses_reconnect_and_success_resets_backoff(self):
        policy = ReconnectPolicy();policy.start();policy.paused = True;policy.failure()
        self.assertIsNone(policy.due)
        policy.paused = False;policy.failure();policy.ready()
        self.assertEqual(policy.attempts, 0);self.assertIsNone(policy.due)


if __name__ == "__main__": unittest.main()

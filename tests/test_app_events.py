import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock
from types import SimpleNamespace

from live_viewer.app import ViewerApp
from live_viewer.core import Stream, ViewerError
from live_viewer.relay import LocalRelay


@unittest.skipUnless(os.name == "nt", "Windows desktop required")
class AppEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ViewerApp(self.root, Path(self.temp.name))
        self.app.backend.set("PotPlayer")
        executable = Path(self.temp.name) / "FakePot.exe"
        executable.touch()
        self.app.player.set(str(executable))
        self.app.platform.set("斗鱼")
        self.app.room.set("6657")
        self.release = [threading.Event(), threading.Event()]
        self.managed = Mock()
        self.managed.profile = SimpleNamespace(source=executable.resolve())
        self.managed.ever_launched = False
        self.managed.snapshot.return_value = {"state": "idle", "intent": "stopped"}
        self.managed.poll.return_value = []
        self.managed.open.return_value = 1
        self.app.potplayer = self.managed

    def tearDown(self):
        for signal in self.release:
            signal.set()
        if not self.app.closed:
            self.app.close()
        self.temp.cleanup()

    def pump(self, condition):
        deadline = time.monotonic() + 4
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(condition())

    def test_stopped_late_success_never_opens_potplayer_and_closes_prepared_resource(self):
        ready, finished = threading.Event(), threading.Event()
        relays = []
        def prepare(spec, jar, token, **_):
            relay = LocalRelay(spec["platform"], spec["room_input"], None)
            relays.append(relay)
            ready.set()
            self.release[0].wait(3)
            finished.set()
            return relay, Stream("douyu", spec["room_input"], 0, 0, "原画", [], [])
        with patch.object(self.app.playback_service, "prepare", side_effect=prepare):
            self.app.play()
            self.assertTrue(ready.wait(2))
            self.app.stop_playback()
            self.assertFalse(self.app.busy)
            self.release[0].set()
            self.pump(lambda: finished.is_set() and relays[0].stop_event.is_set())
            self.managed.open.assert_not_called()
            self.assertEqual(self.app.controller.state, "stopped")
            self.assertIsNone(self.app.activity.notice())

    def test_old_result_does_not_release_new_job_or_launch_its_player(self):
        ready = [threading.Event(), threading.Event()]
        relays, calls = [], []
        def prepare(spec, jar, token, **_):
            index = len(calls)
            calls.append(token)
            relay = LocalRelay(spec["platform"], spec["room_input"], None)
            relays.append(relay)
            ready[index].set()
            self.release[index].wait(3)
            return relay, Stream("douyu", spec["room_input"], 0, 0, "原画", [], [])
        with patch.object(self.app.playback_service, "prepare", side_effect=prepare):
            self.app.play()
            self.assertTrue(ready[0].wait(2))
            self.app.stop_playback()
            self.app.room.set("6658")
            self.app.play()
            self.assertTrue(ready[1].wait(2))
            self.release[0].set()
            self.pump(lambda: relays[0].stop_event.is_set())
            self.assertTrue(self.app.busy)
            self.managed.open.assert_not_called()
            self.release[1].set()
            self.pump(lambda: not self.app.busy)
            self.managed.open.assert_called_once()
            self.assertIs(self.app.relay, relays[1])
            self.assertEqual(self.app.controller.snapshot()["target"], "douyu:6658")

    def test_stale_error_is_only_debug_and_cannot_replace_new_offline_notice(self):
        ready = threading.Event()
        count = []
        def prepare(spec, jar, token, **_):
            count.append(token)
            if len(count) == 1:
                ready.set()
                self.release[0].wait(3)
                raise ViewerError("旧房间的迟到故障", code="network.tls_failed")
            raise ViewerError("新房间未开播", code="room.offline", level="WARN")
        with patch.object(self.app.playback_service, "prepare", side_effect=prepare):
            self.app.play()
            self.assertTrue(ready.wait(2))
            self.app.stop_playback()
            self.app.room.set("6658")
            self.app.play()
            self.pump(lambda: not self.app.busy)
            self.release[0].set()
            self.pump(lambda: any(r["code"] == "operation.stale_result" for r in self.app.activity.store.read()))
            self.assertEqual(self.app.activity.notice().message, "新房间未开播")
            self.assertEqual(self.app.controller.state, "unavailable")
            self.assertNotIn("旧房间的迟到故障", self.app.activity.store.path.read_text(encoding="utf-8"))

    def test_long_error_reminder_fits_content_and_recovery_closes_it(self):
        request = {"platform": "douyu", "room_input": "6657"}
        token = self.app.controller.begin(request)
        self.app.controller.fail(token, ViewerError("证书连接失败（" + "long-host-" * 24 + ".invalid）。"))
        self.app._drain_activity()
        dialog = self.app.alert_window
        self.root.deiconify()
        dialog.deiconify()
        self.pump(lambda: dialog.winfo_height() > 1)
        frame = dialog.winfo_children()[0]
        requested_height = dialog.winfo_height()
        self.assertGreaterEqual(requested_height, frame.winfo_reqheight())
        self.app.controller.recovered(token)
        self.app._drain_activity()
        self.assertFalse(dialog.winfo_exists())

    def active_pot(self):
        request = {"platform": "douyu", "room_input": "6657", "requested": None, "room_name": "正在观看",
                   "session": "匿名", "backend": "potplayer", "low_latency": True, "network_mode": "direct"}
        self.app.active_spec = request
        self.app.pot_token = self.app.play_token = self.app.controller.begin(request)
        self.app.pot_serial = 1
        self.app.pot_had_graph = True
        self.app.pot_expected_absence = False
        self.app.retry.start()
        self.managed.ever_launched = True
        data = {"state": "playing", "raw_state": 2, "load_serial": 1, "owned_source": True, "source_present": True,
                "position_seconds": 3, "width": 1920, "height": 1080, "intent": "playing"}
        self.managed.snapshot.return_value = data
        return request, data

    def test_pot_pause_and_resume_use_active_room_and_block_queued_recovery(self):
        spec, data = self.active_pot()
        self.app.retry.failure()
        data["state"] = "paused"
        self.app._poll_potplayer()
        self.assertTrue(self.app.paused_intent)
        self.assertTrue(self.app.retry.paused)
        self.assertIsNone(self.app.retry.due)
        self.app.room.set("123456")
        data["state"] = "playing"
        with patch.object(self.app, "play") as play:
            self.app._poll_potplayer()
            play.assert_called_once_with(spec)

    def test_pot_close_cancels_a_pending_preparation_before_result_dispatch(self):
        _, data = self.active_pot()
        self.app.pending_play_token = self.app.play_token
        self.app.operation_token = self.app.operations.begin("playback.prepare")
        self.app.busy = True
        self.app.retry.failure()
        data["state"] = "closed"
        self.app._poll_potplayer()
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.active_spec)
        self.assertFalse(self.app.retry.active)
        self.managed.open.assert_not_called()

    def test_pot_close_playback_is_user_stop_and_never_an_automatic_retry(self):
        _, data = self.active_pot()
        data["state"] = "closed_media"
        with patch.object(self.app, "play") as play:
            self.app._poll_potplayer()
            play.assert_not_called()
        self.assertEqual(self.app.controller.state, "stopped")
        self.assertFalse(self.app.retry.active)
        self.assertIsNone(self.app.activity.notice())

    def test_old_pot_control_errors_do_not_overwrite_new_room_state(self):
        self.active_pot()
        self.app.controller.begin({"platform": "bilibili", "room_input": "35"})
        self.managed.poll.return_value = [{"kind": "error", "serial": 1,
                                         "error": ViewerError("旧播放器的错误", code="player.control_timeout")}]
        self.app._poll_potplayer()
        self.assertEqual(self.app.controller.snapshot()["target"], "bilibili:35")
        self.assertEqual(self.app.controller.state, "resolving")
        self.assertIsNone(self.app.activity.notice())

    def test_opening_another_file_in_owned_player_cancels_live_control(self):
        _, data = self.active_pot()
        data["owned_source"] = False
        self.app._poll_potplayer()
        self.assertEqual(self.app.controller.state, "stopped")
        self.assertFalse(self.app.retry.active)
        self.managed.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()

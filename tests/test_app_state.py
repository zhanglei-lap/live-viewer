import json
import os
import tempfile
import time
import types
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from live_viewer.app import ViewerApp
from live_viewer.core import cookies_from_text


@unittest.skipUnless(os.name == "nt", "Windows desktop required")
class AppStateTests(unittest.TestCase):
    def test_native_osc_pause_blocks_retry_and_resume_reconnects_current_room(self):
        self.app.retry.start()
        spec = {"backend": "mpv", "room_name": "playing"}
        self.app.active_spec = spec
        player = Mock();player.poll.return_value=[];player.props={}
        player.snapshot.return_value={"state":"playing", "paused":True, "position_seconds":3.0, "video_output_ready":False}
        self.app.embedded_window=types.SimpleNamespace(closed=False,player=player,update_status=Mock(),close=Mock())
        self.app._poll_embedded()
        self.assertTrue(self.app.paused_intent);self.assertTrue(self.app.retry.paused)
        player.snapshot.return_value["paused"] = False
        with patch.object(self.app,"play") as play:
            self.app._poll_embedded()
            play.assert_called_once_with(spec)

    def test_native_window_close_cancels_pending_reconnect(self):
        self.app.retry.start();self.app.retry.failure()
        player=Mock();player.poll.return_value=[{"kind":"closed"}]
        window=types.SimpleNamespace(closed=False,player=player,close=Mock())
        self.app.embedded_window=window
        self.app._poll_embedded()
        window.close.assert_called_once()

    def test_stop_keeps_last_decoder_evidence_for_export(self):
        self.app.active_spec = {"backend": "mpv"}
        player = Mock();player.snapshot.return_value = {"video_output_ready": True, "position_seconds": 2.0}
        self.app.embedded_window = types.SimpleNamespace(closed=False, player=player, close=Mock())
        relay = Mock();relay.snapshot.return_value = {"bytes_forwarded": 100, "decoded_verified": False}
        self.app.relay = relay
        self.app.stop_playback()
        self.assertTrue(self.app.playback_report()["decoded_verified"])
        self.assertEqual(self.app.playback_report()["status"], "已停止")

    def test_native_auto_retry_uses_active_room_not_edited_input(self):
        now = [0.0]
        self.app.retry.clock = lambda: now[0]
        self.app.retry.start()
        spec = {"platform": "douyu", "room_input": "6657", "requested": 0, "room_name": "watching",
                "session": "匿名", "backend": "mpv", "low_latency": True}
        self.app.active_spec = spec
        self.app.room.set("123456")
        player = Mock()
        player.poll.return_value = [];player.props = {}
        player.snapshot.return_value = {"state": "error", "position_seconds": None, "video_output_ready": False}
        self.app.embedded_window = types.SimpleNamespace(closed=False, player=player, update_status=Mock(), close=Mock())
        with patch.object(self.app, "play") as play:
            self.app._poll_embedded();now[0] = 1;self.app._poll_embedded()
            play.assert_called_once_with(spec, automatic=True)

    def test_native_retry_can_be_disabled_without_stopping_current_session(self):
        self.app.retry.start();self.app.retry.failure();self.app.auto_reconnect.set(False)
        self.app.active_spec = {"backend": "mpv", "room_name": "watching"}
        player = Mock();player.snapshot.return_value = {"state": "error", "position_seconds": None, "video_output_ready": False}
        player.poll.return_value = [];player.props = {}
        self.app.embedded_window = types.SimpleNamespace(closed=False, player=player, update_status=Mock(), close=Mock())
        with patch.object(self.app, "play") as play:
            self.app._poll_embedded();play.assert_not_called()
        self.assertTrue(self.app.retry.active);self.assertIsNone(self.app.retry.due)

    def test_inline_name_updates_existing_room_and_survives_restart(self):
        self.app.platform.set("斗鱼")
        self.app.room.set("06657")
        self.app.history = [{"platform": "douyu", "room": "6657", "name": "old", "played_at": "2026-10-07T00:00:00+08:00"}]
        count = len(self.app.favorites)
        self.app.room_name.set("玩机器")
        self.app.save_favorite()
        self.assertEqual(len(self.app.favorites), count)
        self.assertEqual(self.app._selection()[:2], ("douyu", "6657"))
        self.assertEqual(self.app.history[0]["name"], "玩机器")
        self.app.close()
        self.root = tk.Tk(); self.root.withdraw()
        self.app = ViewerApp(self.root, self.directory)
        self.assertEqual(self.app.room_name.get(), "玩机器")

    def test_same_name_different_room_ids_and_platforms_stay_distinct(self):
        for platform, room in (("斗鱼", "6657"), ("斗鱼", "123456"), ("B 站", "123456")):
            self.app.platform.set(platform)
            self.app.room.set(room)
            self.app.room_name.set("相同名字")
            self.app.save_favorite()
        named = [r for r in self.app.favorites if r["name"] == "相同名字"]
        self.assertEqual(len(named), 3)
        self.assertEqual(len({(r["platform"], r["room"]) for r in named}), 3)

    def test_switching_room_restores_correct_name_without_changing_identity(self):
        self.app.room_name.set("我的 B 站")
        self.app.save_favorite()
        self.app.platform.set("斗鱼");self.app.room.set("6657")
        self.assertNotEqual(self.app.room_name.get(), "我的 B 站")
        self.app.platform.set("B 站");self.app.room.set("35")
        self.assertEqual(self.app.room_name.get(), "我的 B 站")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name).resolve()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ViewerApp(self.root, self.directory)

    def tearDown(self):
        if not self.app.closed:
            self.app.close()
        self.temporary.cleanup()

    def import_cookie(self, platform, jar, verified):
        self.app.platform.set("斗鱼" if platform == "douyu" else "B 站")
        with patch("live_viewer.app.PlatformClient.login_status", return_value={"verified": verified, "message": "test login"}):
            self.app._import_cookie_jar(platform, jar)
            deadline = time.monotonic() + 3
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
        self.assertFalse(self.app.busy)

    def test_cookie_import_is_memory_only_by_default(self):
        jar = cookies_from_text("login_test=SYNTHETIC_SECRET", "douyu")
        self.import_cookie("douyu", jar, None)
        self.assertIn("douyu", self.app.cookies)
        self.assertFalse((self.directory / "credentials" / "douyu.dpapi").exists())
        self.assertNotIn("SYNTHETIC_SECRET", self.app.store.path.read_text(encoding="utf-8"))

    def test_opt_in_survives_restart_and_opt_out_removes_disk_login(self):
        jar = cookies_from_text("login_test=SYNTHETIC_SECRET", "douyu")
        self.import_cookie("douyu", jar, None)
        self.app.remember_login.set(True)
        self.app.toggle_remember_login()
        self.app.close()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = ViewerApp(self.root, self.directory)
        self.assertEqual(self.app.cookies["douyu"].get("login_test"), "SYNTHETIC_SECRET")
        self.assertEqual(self.app.mode.get(), "导入 Cookie")
        self.app.remember_login.set(False)
        self.app.toggle_remember_login()
        self.assertFalse((self.directory / "credentials" / "douyu.dpapi").exists())
        self.assertIn("douyu", self.app.cookies)
        self.app.clear_cookies()
        self.assertNotIn("douyu", self.app.cookies)
        self.assertFalse(json.loads(self.app.store.path.read_text(encoding="utf-8"))["remember_login"]["douyu"])

    def test_invalid_login_clears_previously_remembered_credentials(self):
        jar = cookies_from_text("SESSDATA=SYNTHETIC_SECRET", "bilibili")
        self.app.config["remember_login"]["bilibili"] = True
        self.app.credential_store.save("bilibili", jar)
        self.import_cookie("bilibili", jar, False)
        self.assertNotIn("bilibili", self.app.cookies)
        self.assertFalse((self.directory / "credentials" / "bilibili.dpapi").exists())
        self.assertFalse(self.app.remember_login.get())


if __name__ == "__main__":
    unittest.main()

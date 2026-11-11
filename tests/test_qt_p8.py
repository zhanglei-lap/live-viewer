import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bangumi_fixtures import inspection, season, source
from live_viewer.qt.app import ViewerWindow
from live_viewer.settings import StateStore
from PySide6.QtWidgets import QApplication


class BangumiQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.foreground = patch("live_viewer.qt.notifications.foreground", return_value=True);self.foreground.start()
        self.window = ViewerWindow(self.temp.name);self.window.show();self.window.go_page(6)
        self.page = self.window.bangumi_page

    def tearDown(self):
        self.page.cancel_lookup();self.window.close()
        end = time.monotonic() + 5
        while not self.window.closed and time.monotonic() < end:self.app.processEvents();time.sleep(.01)
        self.foreground.stop();self.temp.cleanup()

    def pump(self, test):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            self.window.tick();self.app.processEvents()
            if test():return
            time.sleep(.01)
        self.fail("condition timeout")

    def load(self):
        self.page.input.setText("ss43164")
        with patch("live_viewer.qt.bangumi_page.BangumiClient") as client:
            client.return_value.metadata.return_value = season()
            client.return_value.inspect.return_value = inspection()
            self.page.lookup();self.pump(lambda: self.page.source is not None)

    def test_ss_lists_versions_episodes_and_actual_tracks(self):
        self.load()
        self.assertEqual(self.window.stack.count(), 7)
        self.assertEqual(self.page.seasons.count(), 2)
        self.assertEqual(self.page.episodes.count(), 3)
        self.assertEqual(self.page.videos.count(), 2)
        self.assertIn("整集", self.page.rights.text())

    def test_saved_season_rename_retains_one_id_and_preserves_other_favorites(self):
        self.load()
        original = list(self.window.config["favorites"])
        self.page.name.setText("我的乐队");self.page.save()
        self.page.name.setText("改名乐队");self.page.save()
        restored = StateStore(self.temp.name).load()
        self.assertEqual(restored["bangumi_favorites"][0]["name"], "改名乐队")
        self.assertEqual(len(restored["bangumi_favorites"]), 1)
        self.assertEqual(restored["favorites"], original)

    def test_play_request_uses_ep_and_content_id_not_bv_page(self):
        self.load()
        self.window.config["video_progress"]["pgc:693247:875212290"] = {"position": 77, "duration": 1415, "page": 1}
        with patch.object(self.window.runtime, "play", return_value=False) as play:
            self.page.play()
        req = play.call_args.args[0]
        self.assertEqual(req["content_type"], "pgc")
        self.assertEqual(req["ep_id"], 693247);self.assertEqual(req["cid"], 875212290)
        self.assertEqual(req["resume_position"], 77);self.assertNotIn("video_input", req)

    def test_preview_keeps_episode_picker_and_shows_fixed_warning_without_tracks(self):
        self.page.input.setText("ss43164")
        with patch("live_viewer.qt.bangumi_page.BangumiClient") as client:
            client.return_value.metadata.return_value = season();client.return_value.inspect.return_value = inspection(preview=True)
            self.page.lookup();self.pump(lambda: self.window.activity.notice_for("pgc.tracks") is not None)
        self.assertEqual(self.page.episodes.count(), 3)
        self.assertIsNone(self.page.source);self.assertEqual(self.page.videos.count(), 0)
        self.assertTrue(self.window.notice.isVisible())
        self.assertEqual(self.window.notice_event.code, "pgc.preview_only")

    def test_changing_episode_rejects_late_tracks_and_clears_subtitles(self):
        self.load()
        blocked = threading.Event()
        with patch("live_viewer.qt.bangumi_page.BangumiClient") as client:
            def inspect(value, **kwargs):
                if value == "ep706287":blocked.wait(2);return inspection(706287)
                return inspection()
            client.return_value.inspect.side_effect = inspect
            self.page.episodes.setCurrentRow(1)
            self.page.episodes.setCurrentRow(0)
            blocked.set();self.pump(lambda: self.page.source is not None)
            self.assertEqual(self.page.source.episode.ep_id, 693247)
            self.assertEqual(self.page.subtitle_box.count(), 1)

    def test_stop_cancels_late_favorite_auto_play(self):
        gate = threading.Event()
        self.page.input.setText("ss43164")
        with patch("live_viewer.qt.bangumi_page.BangumiClient") as client, patch.object(self.window.runtime, "play") as play:
            client.return_value.metadata.side_effect = lambda *a: (gate.wait(2), season())[1]
            self.page.lookup(auto_play=True);self.window.stop();gate.set()
            time.sleep(.05);self.window.tick()
            play.assert_not_called()
            self.assertFalse(self.page.auto_play_pending)

    def test_saved_episode_cid_change_does_not_auto_play(self):
        self.page.select_record({"season_id": 43164, "ep_id": 693247, "cid": 999, "name": "旧条目"})
        with patch("live_viewer.qt.bangumi_page.BangumiClient") as client:
            client.return_value.metadata.return_value = season()
            def inspect(value, **kwargs):
                from live_viewer.core import ViewerError
                if kwargs.get("expected_cid") == 999:raise ViewerError("内容已改变", code="pgc.identity_mismatch")
                return inspection()
            client.return_value.inspect.side_effect = inspect
            self.page.lookup(auto_play=True);self.pump(lambda: self.window.activity.notice_for("pgc.tracks") is not None)
        self.assertIsNone(self.page.source)

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from live_viewer.qt.app import ViewerWindow
from live_viewer.settings import StateStore
from live_viewer.core import ViewerError
from vod_fixtures import source


class VideoQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app = QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.foreground = patch("live_viewer.qt.notifications.foreground", return_value=True);self.foreground.start()
        self.window = ViewerWindow(self.temp.name)
        self.window.show();self.window.go_page(5)
        self.page = self.window.vod_page
    def tearDown(self):
        self.window.close()
        limit = time.monotonic() + 5
        while not self.window.closed and time.monotonic() < limit:self.app.processEvents();time.sleep(.01)
        self.foreground.stop();self.temp.cleanup()
    def pump(self, condition):
        limit = time.monotonic() + 3
        while time.monotonic() < limit:
            self.window.tick();self.app.processEvents()
            if condition():return
            time.sleep(.01)
        self.fail("condition timeout")

    def test_favorite_rename_deduplicates_by_bv_and_cid_and_restores(self):
        self.page.input.setText("BV17x411w7KC");self.page.set_source(source())
        self.page.name.setText("我的普通视频");self.page.save()
        self.page.name.setText("新的名称");self.page.save()
        state = StateStore(self.temp.name).load()
        self.assertEqual(len(state["video_favorites"]), 1)
        self.assertEqual(state["video_favorites"][0]["name"], "新的名称")
        self.page.search.setText("新的名称");self.assertEqual(self.page.saved.count(), 1)

    def test_play_request_has_selected_part_and_both_track_keys(self):
        self.page.input.setText("BV17x411w7KC");self.page.set_source(source(2))
        self.page.videos.setCurrentIndex(1)
        with patch.object(self.window.runtime, "play", return_value=False) as play:
            self.page.play_button.click()
        request = play.call_args.args[0]
        self.assertEqual(request["kind"], "vod");self.assertEqual(request["page"], 2)
        self.assertEqual(request["video_key"], source(2).videos[1].key)
        self.assertEqual(request["requested"], 16)
        self.assertEqual(request["audio_key"], source(2).selected_audio.key)
        self.assertFalse(request["replay_mode"])

    def test_input_change_rejects_a_late_lookup(self):
        gate = threading.Event()
        with patch("live_viewer.qt.vod_page.VodClient") as client:
            client.return_value.resolve.side_effect = lambda *a: (gate.wait(2), source())[1]
            self.page.input.setText("BV17x411w7KC");self.page.lookup()
            self.page.input.setText("av170001");gate.set()
            self.pump(lambda: not self.window.runtime.jobs.pending)
        self.assertIsNone(self.page.source)

    def test_stop_cancels_pending_auto_play_from_a_saved_video(self):
        gate = threading.Event()
        with patch("live_viewer.qt.vod_page.VodClient") as client, patch.object(self.window.runtime, "play") as play:
            client.return_value.resolve.side_effect = lambda *a: (gate.wait(2), source())[1]
            self.page.input.setText("BV17x411w7KC");self.page.lookup(auto_play=True)
            self.window.stop();gate.set();self.pump(lambda: not self.window.runtime.jobs.pending)
            time.sleep(.03);self.window.tick()
            play.assert_not_called()
        self.assertFalse(self.page.auto_play_pending)

    def test_video_error_has_fixed_notice_and_retry_on_all_pages(self):
        self.window.runtime.error(ViewerError("视频接口暂不可用", code="vod.api_rejected"), scope="vod.lookup")
        self.window.tick();self.window.go_page(3)
        self.assertTrue(self.window.notice.isVisible())
        self.assertTrue(self.window.notice_retry.isVisible())
        self.assertEqual(self.window.notice_event.source, "vod")
        self.assertIn("视频接口", self.window.notice_body.text())

    def test_saved_part_is_found_by_cid_after_part_order_changes(self):
        self.page.select_record({"bvid": "BV17x411w7KC", "cid": 279787, "page": 1, "name": "我的第二集"})
        with patch("live_viewer.qt.vod_page.VodClient") as client:
            client.return_value.resolve.side_effect = [source(1), source(2)]
            self.page.lookup();self.pump(lambda: self.page.source is not None)
            self.assertEqual(client.return_value.resolve.call_count, 2)
        self.assertEqual(self.page.source.page["cid"], 279787)
        self.assertEqual(self.page.parts.currentData(), 2)
        self.assertEqual(self.page.name.text(), "我的第二集")

    def test_removed_saved_cid_cannot_autoplay_a_different_part(self):
        self.page.select_record({"bvid": "BV17x411w7KC", "cid": 999999, "page": 1, "name": "已删除的一集"})
        with patch("live_viewer.qt.vod_page.VodClient") as client, patch.object(self.window.runtime, "play") as play:
            client.return_value.resolve.return_value = source(1)
            self.page.lookup(auto_play=True)
            self.pump(lambda: not self.window.runtime.jobs.pending)
            play.assert_not_called()
        self.assertIsNone(self.page.source)
        self.assertEqual(self.window.activity.notice().code, "vod.saved_part_changed")

    def test_subtitle_catalog_and_saved_language_use_the_current_cid(self):
        from live_viewer.subtitles import Subtitle
        self.page.input.setText("BV17x411w7KC");self.page.set_source(source(2))
        track = Subtitle("123:zh-Hans", "zh-Hans", "简体中文", "https://i0.hdslb.com/bfs/subtitle/test.json")
        with patch("live_viewer.qt.vod_page.VodClient") as client:
            client.return_value.subtitle_catalog.return_value = ((track,), False)
            self.page.load_subtitles();self.pump(lambda: "vod.subtitles" not in self.window.runtime.jobs.pending)
            client.return_value.subtitle_catalog.assert_called_once_with("BV17x411w7KC", 279787)
        self.assertEqual(self.page.subtitle_box.count(), 2)
        self.assertIn("下次播放", self.page.subtitle_status.text())

    def test_resume_checkbox_uses_saved_cid_position(self):
        self.page.input.setText("BV17x411w7KC");self.page.set_source(source(2))
        self.window.config["video_progress"]["BV17x411w7KC:279787"] = {"position": 44, "duration": 120, "page": 2}
        self.page.resume_box.setChecked(True)
        with patch.object(self.window.runtime, "play", return_value=False) as play:self.page.play()
        self.assertEqual(play.call_args.args[0]["resume_position"], 44)
        self.page.resume_box.setChecked(False)
        with patch.object(self.window.runtime, "play", return_value=False) as play:self.page.play()
        self.assertEqual(play.call_args.args[0]["resume_position"], 0)

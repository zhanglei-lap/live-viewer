import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from bangumi_fixtures import source, request
from live_viewer.bangumi_state import record, records, remember
from live_viewer.core import ViewerError
from live_viewer.events import EventCenter
from live_viewer.playback import PlaybackController, PlaybackService
from live_viewer.reports import clean, REPORT
from live_viewer.runtime import LiveRuntime
from live_viewer.settings import sanitize_settings
from live_viewer.vod_state import bookmark, progress_records, progress_key


class PgcStateTests(unittest.TestCase):
    def test_upgrading_retains_live_ugc_and_pgc_bookmarks_without_collisions(self):
        old = sanitize_settings({"video_favorites": [{"bvid": "BV1yW4y1j7Ft", "cid": 875212290, "page": 1, "name": "投稿"}]})
        self.assertTrue(bookmark(old, {"bvid": "BV1yW4y1j7Ft", "cid": 875212290, "page": 1}, 20, 100))
        self.assertTrue(bookmark(old, request(), 40, 1415))
        restored = sanitize_settings(old)
        self.assertEqual(len(restored["favorites"]), 3)
        self.assertEqual(restored["video_progress"]["BV1yW4y1j7Ft:875212290"]["position"], 20)
        self.assertEqual(restored["video_progress"]["pgc:693247:875212290"]["position"], 40)
        self.assertEqual(len(restored["video_favorites"]), 1)

    def test_series_and_episode_indices_and_export_whitelist(self):
        values = [{"season_id": 43164, "name": "我的乐队", "last_ep_id": 693247, "url": "private-url", "cookie": "private-cookie"}] * 2
        self.assertEqual(len(records(values)), 1)
        history = remember([], {**source().episode.summary(), "name": "乐队"})
        history = remember(history, {**source(706287).episode.summary(), "name": "乐队"})
        self.assertEqual(len(history), 2)
        text = json.dumps(records(values))
        self.assertNotIn("private", text)
        self.assertIsNone(record({"season_id": True}))
        self.assertEqual(progress_records({"pgc:0:10": {"page": 1, "position": 0, "duration": 100}}), {})

    def test_renamed_season_updates_restored_history_and_last_episode(self):
        ep = {**source().episode.summary(), "name": "旧名"}
        config = sanitize_settings({"bangumi_favorites": [{"season_id": 43164, "name": "新名"}], "bangumi_history": [ep], "last_bangumi": ep})
        self.assertEqual(config["bangumi_history"][0]["name"], "新名")
        self.assertEqual(config["last_bangumi"]["name"], "新名")

    def test_report_preserves_ep_identity_but_drops_cookies_and_media_urls(self):
        s = source().summary();s["account"]["cookie"] = "fixture-cookie";s["video"]["urls"] = ["private-url"]
        d = clean({"playback": {"content_type": "pgc", "ep_id": 693247, "season_id": 43164, "transport": {"last_stream": s}}}, REPORT)
        self.assertEqual(d["playback"]["transport"]["last_stream"]["episode"]["cid"], 875212290)
        self.assertNotIn("private", json.dumps(d));self.assertNotIn("fixture-cookie", json.dumps(d))


class PgcServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = EventCenter(Path(self.temp.name))
        self.controller = PlaybackController(self.events)
        self.service = PlaybackService(self.controller)

    def tearDown(self):
        self.events.close();self.temp.cleanup()

    def test_resolve_and_refresh_use_pgc_ep_cid_and_fresh_account(self):
        req = request();req["session"] = "导入 Cookie"
        token = self.controller.begin(req)
        with patch("live_viewer.bangumi.BangumiClient") as client, patch("live_viewer.bangumi.BangumiProxy") as proxy:
            client.return_value.resolve.return_value = source()
            proxy.return_value.prepare.return_value = proxy.return_value
            relay, s = self.service.prepare_bangumi(req, "fixture-initial", token, cookie_provider=lambda: "fixture-fresh")
            refreshed = proxy.call_args.kwargs["refresher"](s)
            self.assertEqual(client.call_args.args[0], "fixture-fresh")
            self.assertEqual(client.return_value.resolve.call_args.args[0], "ep693247")
            self.assertEqual(client.return_value.resolve.call_args.kwargs["expected_cid"], 875212290)
            self.assertEqual(client.return_value.resolve.call_args.kwargs["video_key"], s.selected_video.key)
            self.assertEqual(refreshed.episode.ep_id, 693247)
            self.assertEqual(self.events.store.read()[-1]["source"], "pgc")

    def test_quarter_change_is_rejected_before_transport(self):
        req = request();req["season_id"] = 123
        token = self.controller.begin(req)
        with patch("live_viewer.bangumi.BangumiClient") as client, patch("live_viewer.bangumi.BangumiProxy") as proxy:
            client.return_value.resolve.return_value = source()
            with self.assertRaises(ViewerError):self.service.prepare_bangumi(req, None, token)
            proxy.assert_not_called()

    def test_cancel_during_resolution_never_creates_transport(self):
        req = request();token = self.controller.begin(req)
        def resolve(*a, **k):
            self.controller.stop();return source()
        with patch("live_viewer.bangumi.BangumiClient") as client, patch("live_viewer.bangumi.BangumiProxy") as proxy:
            client.return_value.resolve.side_effect = resolve
            with self.assertRaises(ViewerError) as exc:self.service.prepare_bangumi(req, None, token)
            self.assertEqual(exc.exception.code, "playback.cancelled");proxy.assert_not_called()

    def test_cleared_login_cannot_renew_anonymously(self):
        req = request();req["session"] = "导入 Cookie";token = self.controller.begin(req)
        with patch("live_viewer.bangumi.BangumiClient") as client, patch("live_viewer.bangumi.BangumiProxy") as proxy:
            client.return_value.resolve.return_value = source()
            self.service.prepare_bangumi(req, "fixture-jar", token, cookie_provider=lambda: None)
            with self.assertRaises(ViewerError) as exc:proxy.call_args.kwargs["refresher"](source())
            self.assertEqual(exc.exception.code, "account.missing")

    def test_pgc_permission_error_stops_retry_but_network_error_can_retry(self):
        req = request();token = self.controller.begin(req)
        self.controller.retry.active = True
        self.controller.relay_event(token, "pgc.preview_only", "仅试看", impact="interrupted", level="WARN")
        self.assertFalse(self.controller.retry.active)


class PgcRuntimeTests(unittest.TestCase):
    def test_native_eof_zero_position_preserves_completed_pgc_bookmark(self):
        with tempfile.TemporaryDirectory() as directory:
            events = EventCenter(Path(directory));runtime = LiveRuntime(events, directory, sanitize_settings({}), {})
            try:
                req = request();token = runtime.controller.begin(req)
                runtime.active = req;runtime.token = runtime.player_token = token;runtime.serial = 1
                runtime.position = 1414;runtime.progress_at = __import__('time').monotonic()
                runtime.restore_target = None
                runtime.player = Mock()
                runtime.player.poll.return_value = []
                runtime.player.snapshot.return_value = {"state": "ended", "position_seconds": 0, "duration_seconds": 1415,
                    "owned_source": True, "source_present": True, "width": 1920, "height": 1080, "load_serial": 1}
                runtime._poll_player()
                self.assertEqual(runtime.controller.state, "ended")
                self.assertTrue(runtime.config["video_progress"][progress_key(req)]["completed"])
            finally:runtime.active = None;runtime.player = None;runtime.close();events.close()

    def test_progress_and_recovery_are_pinned_by_ep_even_without_bv(self):
        with tempfile.TemporaryDirectory() as directory:
            events = EventCenter(Path(directory));runtime = LiveRuntime(events, directory, sanitize_settings({}), {})
            try:
                runtime.active = request()
                data = {"state": "playing", "owned_source": True, "width": 1920, "duration_seconds": 1415, "position_seconds": 77}
                runtime.save_video_progress(data)
                self.assertEqual(runtime.video_recovery_request(request())["resume_position"], 77)
                self.assertNotIn("resume_position", runtime.video_recovery_request(request(706287)))
                self.assertEqual(runtime.config["video_progress"][progress_key(request())]["position"], 77)
            finally:runtime.active = None;runtime.close();events.close()

import copy
import json
import threading
import unittest
from unittest.mock import Mock, patch

from live_viewer.bangumi import BangumiClient, BangumiProxy, bangumi_input, play_payload
from live_viewer.bangumi_validation import evaluate_gate, inspect_matrix, network_evidence, run_native
from live_viewer.core import ViewerError


def records():
    season = {"code": 0, "result": {"season_id": 43164, "title": "孤独摇滚！", "episodes": [
        {"id": 693247, "cid": 875212290, "aid": 944573356, "bvid": "BV1yW4y1j7Ft", "title": "1", "long_title": "孤独的转机", "duration": 1415000},
        {"id": 706287, "cid": 932704646, "aid": 476092951, "bvid": "BV1hK41167rG", "title": "7", "long_title": "去你家", "duration": 1425000, "status": 13}],
        "section": [{"title": "预告", "episodes": [{"id": 800001, "cid": 900001, "duration": 30000, "title": "PV"}]}]}}
    nav = {"code": -101, "data": {"isLogin": False, "vip": {"status": 0}, "mid": "private-user"}}
    v = {"id": 32, "baseUrl": "https://example.bilivideo.com/video?secret=signed", "backupUrl": ["https://unknown.evil/backup"],
         "codecs": "avc1.64001f", "bandwidth": 350000, "width": 852, "height": 480, "frameRate": "23.975"}
    a = {"id": 30280, "base_url": "http://example.bilivideo.com/audio?secret=signed", "codecs": "mp4a.40.2", "bandwidth": 192000}
    play = {"code": 0, "result": {"play_check": {"play_detail": "PLAY_WHOLE"},
            "play_view_business_info": {"episode_info": {"ep_id": 693247, "cid": 875212290}, "season_info": {"season_id": 43164}},
            "video_info": {"code": 0, "error_code": 0, "is_preview": 0, "is_drm": False, "timelength": 1415000, "quality": 32,
            "support_formats": [{"quality": 112, "new_description": "1080P 高码率", "need_vip": True}, {"quality": 32, "new_description": "480P"}],
            "dash": {"duration": 1415, "video": [v], "audio": [a]}}}}
    return [season, nav, play]


class BangumiTests(unittest.TestCase):
    def client(self, values=None):
        client = BangumiClient()
        self.addCleanup(client.close)
        client.json = Mock(side_effect=values or records())
        return client

    def test_ep_ss_and_tracking_link_inputs(self):
        for text in ("693247", "ep693247", "EP693247", "https://www.bilibili.com/bangumi/play/ep693247?tracking=ignored"):
            self.assertEqual(bangumi_input(text), {"ep_id": 693247})
        self.assertEqual(bangumi_input("ss43164"), {"season_id": 43164})
        for text in ("BV1yW4y1j7Ft", "av123", "ep0", "https://bilibili.com.evil/bangumi/play/ep123",
                     "https://x@www.bilibili.com/bangumi/play/ep123", "https://www.bilibili.com:900/bangumi/play/ep123",
                     "https://www.bilibili.com/video/ep123", "https://www.bilibili.com/bangumi/play/ep123/other"):
            with self.assertRaises(ViewerError):
                bangumi_input(text)

    def test_ep_identity_sections_and_duration_units(self):
        season = self.client().metadata("ep693247")
        self.assertEqual(season.episode(706287).cid, 932704646)
        self.assertEqual(season.episode(693247).duration, 1415)
        self.assertEqual(season.episode(800001).section, "预告")
        self.assertNotEqual(season.episode(693247).cid, season.episode(706287).cid)

    def test_no_wrong_episode_fallback(self):
        with self.assertRaises(ViewerError) as raised:
            self.client().metadata("ep123")
        self.assertEqual(raised.exception.code, "pgc.episode_missing")
        with self.assertRaises(ViewerError):
            self.client().metadata("ss99999")

    def test_current_v2_envelope_variants(self):
        result = records()[2]["result"]
        for raw in ({"code": 0, "result": result}, {"code": 0, "data": result}, {"code": 0, "data": {"code": 0, "result": result}}):
            container, video = play_payload(raw)
            self.assertEqual(video["dash"]["duration"], 1415)
        for raw in ({"code": -10404}, {"code": 0, "data": {"code": -10403, "result": result}}, {"code": 0, "data": {}}):
            with self.assertRaises(ViewerError):
                play_payload(raw)

    def test_uses_pgc_not_ugc_and_only_returned_tracks(self):
        client = self.client()
        result = client.inspect("ep693247")
        self.assertEqual(result.status, "pgc.whole")
        self.assertEqual([t.quality for t in result.source.videos], [32])
        self.assertEqual(client.json.call_args[0][0], "/pgc/player/web/v2/playurl")
        self.assertEqual(client.json.call_args[0][1]["ep_id"], 693247)
        self.assertEqual(result.source.referer, "https://www.bilibili.com/bangumi/play/ep693247")
        summary = json.dumps(result.summary())
        self.assertNotIn("secret", summary)
        self.assertNotIn("private-user", summary)
        self.assertNotIn("https://", summary)

    def test_preview_with_full_timelength_is_never_whole(self):
        values = records()
        info = values[-1]["result"]["video_info"]
        values[-1]["result"]["play_check"]["play_detail"] = "PLAY_PREVIEW"
        info.update(is_preview=1, error_code=-10403, durl=[{"length": 180180}])
        del info["dash"]
        inspected = self.client(values).inspect("ep693247")
        self.assertEqual(inspected.status, "pgc.preview_only")
        self.assertIsNone(inspected.source)
        self.assertEqual(inspected.evidence["api_total_seconds"], 1415)
        self.assertEqual(inspected.evidence["media_seconds"], 180.18)

    def test_unmarked_short_media_does_not_pass(self):
        values = records()
        values[-1]["result"]["video_info"]["dash"]["duration"] = 180
        self.assertEqual(self.client(values).inspect("ep693247").status, "pgc.duration_mismatch")

    def test_vip_does_not_override_episode_permissions(self):
        values = records()
        values[1] = {"code": 0, "data": {"isLogin": True, "vip": {"status": 1}}}
        values[-1]["result"]["video_info"]["error_code"] = -10403
        inspected = self.client(values).inspect("ep693247")
        self.assertEqual(inspected.account["vip"], "active")
        self.assertEqual(inspected.status, "pgc.entitlement_required")

    def test_expired_login_cannot_fall_back_to_anonymous(self):
        client = self.client()
        client.login_requested = True
        with self.assertRaises(ViewerError) as raised:
            client.inspect("ep693247")
        self.assertEqual(raised.exception.code, "account.expired")

    def test_returned_business_identity_and_pinned_cid(self):
        for key, wrong in (("ep_id", 706287), ("cid", 932704646)):
            values = records()
            values[-1]["result"]["play_view_business_info"]["episode_info"][key] = wrong
            with self.assertRaises(ViewerError) as raised:
                self.client(values).inspect("ep693247")
            self.assertEqual(raised.exception.code, "pgc.identity_mismatch")
        with self.assertRaises(ViewerError):
            self.client().inspect("ep693247", expected_cid=932704646)

    def test_drm_and_region_are_separate_results(self):
        values = records()
        values[-1]["result"]["video_info"]["is_drm"] = True
        self.assertEqual(self.client(values).inspect("ep693247").status, "pgc.protected_media")
        values = records()
        values[-1]["result"]["view_info"] = {"plugins": {"AreaLimitPanel": {"config": {"is_block": True}}}}
        self.assertEqual(self.client(values).inspect("ep693247").status, "pgc.region_restricted")

    def test_ambiguous_10403_requires_region_signal_and_never_exports_raw_message(self):
        with self.assertRaises(ViewerError) as region:
            play_payload({"code": -10403, "message": "抱歉您所在地区不可观看！ private-provider-text"})
        self.assertEqual(region.exception.code, "pgc.region_restricted")
        self.assertNotIn("private-provider-text", str(region.exception))
        with self.assertRaises(ViewerError) as permission:
            play_payload({"code": -10403, "message": "Success"})
        self.assertEqual(permission.exception.code, "pgc.entitlement_required")

    def test_explicit_hd_track_must_exist(self):
        with self.assertRaises(ViewerError) as raised:
            self.client().resolve("ep693247", video_key="video:112:locked")
        self.assertEqual(raised.exception.code, "pgc.track_missing")

    def test_member_high_bitrate_same_dimensions_is_distinct(self):
        values = records()
        values[1] = {"code": 0, "data": {"isLogin": True, "vip": {"status": 1}}}
        dash = values[-1]["result"]["video_info"]["dash"]
        for q, rate in ((80, 3000000), (112, 6000000)):
            track = copy.deepcopy(dash["video"][0])
            track.update(id=q, width=1920, height=1080, bandwidth=rate)
            dash["video"].append(track)
        source = self.client(values).resolve("ep693247")
        self.assertEqual(source.selected_video.quality, 112)
        self.assertEqual([t.bandwidth for t in source.videos[:2]], [6000000, 3000000])

    def test_qn_80_selects_80_even_when_pgc_returns_112_too(self):
        values = records()
        dash = values[-1]["result"]["video_info"]["dash"]
        for q, rate in ((80, 3000000), (112, 6000000)):
            track = copy.deepcopy(dash["video"][0])
            track.update(id=q, width=1920, height=1080, bandwidth=rate)
            dash["video"].append(track)
        selected = self.client(values).resolve("ep693247", requested=80).selected_video
        self.assertEqual(selected.quality, 80)

    def test_unpublished_episode_has_a_separate_outcome(self):
        values = records()
        values[0]["result"]["episodes"][0].update(cid=0, duration=0)
        with self.assertRaises(ViewerError) as raised:
            self.client(values).resolve("ep693247")
        self.assertEqual(raised.exception.code, "pgc.not_published")

    def test_cdn_session_has_ep_referer_and_no_auth_cookies(self):
        source = self.client().resolve("ep693247")
        proxy = BangumiProxy(source)
        self.addCleanup(proxy.stop)
        with proxy._session() as session:
            self.assertEqual(session.headers["Referer"], source.referer)
            self.assertFalse(session.cookies)
            self.assertFalse(session.trust_env)

    def test_refresh_rejects_ep_change_even_if_cid_and_bvid_unchanged(self):
        source = self.client().resolve("ep693247")
        changed = copy.deepcopy(source)
        from dataclasses import replace
        changed.episode = replace(source.episode, ep_id=999999)
        proxy = BangumiProxy(source, refresher=lambda old: changed)
        self.addCleanup(proxy.stop)
        with self.assertRaises(ViewerError) as raised:
            proxy.refresh()
        self.assertEqual(raised.exception.code, "pgc.identity_mismatch")

    def test_canceled_native_probe_never_resolves_or_launches(self):
        cancel = threading.Event()
        cancel.set()
        with patch("live_viewer.bangumi_validation.BangumiClient") as client, patch("live_viewer.bangumi_validation.PotPlayerAdapter") as player:
            result = run_native("ep693247", None, ".", "unused.exe", cancel=cancel)
            self.assertEqual(result["status"], "playback.cancelled")
            client.assert_not_called()
            player.assert_not_called()

    def test_direct_ignores_unreachable_process_proxy(self):
        with patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9"}):
            evidence = network_evidence()
            self.assertTrue(evidence["environment_proxy_present"])
            self.assertFalse(evidence["trust_env"])
            self.assertEqual(evidence["merged_proxy_count"], 0)
            self.assertEqual(evidence["d3_mainland_without_vpn"], "unverified")

    def test_anonymous_catalog_and_negative_cases_do_not_pass_member_gate(self):
        report = {"samples": [], "human_checks": {}, "d2_actual_api_and_cdn_passed": True}
        self.assertEqual(evaluate_gate(report)["gate"], "pending")
        self.assertGreater(len(report["gate_reasons"]), 6)

    def test_a_later_1080_comparison_does_not_erase_prior_hd_or_full_watch(self):
        from live_viewer.bangumi_validation import SAMPLES
        good = {"status": "passed", "continuous_seconds": 300, "media": {"actual": 112}, "checks": {}}
        rows = [{"episode": {"ep_id": ep}, "identity_matches_sample": True, "status": "pgc.whole",
                 "account": {"login": "authenticated", "vip": "active"}, "native": copy.deepcopy(good)}
                for _, _, ids in SAMPLES for ep in ids]
        earlier = copy.deepcopy(good)
        earlier["checks"].update(natural_episode_end=True, continuous_play=True,
                                decoded_dimensions_match_track=True, native_full_duration=True)
        earlier["full_episode_requested"] = True
        earlier["media"]["media_duration_seconds"] = 300
        rows[0]["native_runs"] = [earlier]
        rows[0]["native"]["media"]["actual"] = 80
        report = {"samples": rows, "human_checks": {"official_same_account_baseline": "confirmed", "picture_audio_sync": "confirmed"},
                  "d2_actual_api_and_cdn_passed": True, "network": {"d3_mainland_without_vpn": "user_confirmed"}}
        self.assertEqual(evaluate_gate(report)["gate"], "passed_d3_user_confirmed")
        earlier["status"] = "validation.native_failed"  # Later post-EOF control failed; complete watch remains recorded.
        self.assertEqual(evaluate_gate(report)["gate"], "passed_d3_user_confirmed")
        earlier["full_episode_requested"] = False
        self.assertEqual(evaluate_gate(report)["gate"], "pending")
        earlier["full_episode_requested"] = True
        report["d2_actual_api_and_cdn_passed"] = False
        self.assertEqual(evaluate_gate(report)["gate"], "pending")


if __name__ == "__main__":
    unittest.main()

import copy
import json
import unittest
from unittest.mock import Mock
from live_viewer.vod import VodClient, video_input, wbi_params, media_url
from live_viewer.core import ViewerError
from live_viewer.settings import sanitize_settings, remember_video
from live_viewer.reports import clean, REPORT
from vod_fixtures import source, api_records


class VideoTests(unittest.TestCase):
    def test_bv_av_and_link_page(self):
        self.assertEqual(video_input("BV17x411w7KC"), ({"bvid": "BV17x411w7KC"}, 1))
        self.assertEqual(video_input("av170001"), ({"aid": 170001}, 1))
        self.assertEqual(video_input("https://www.bilibili.com/video/BV17x411w7KC?p=2&tracking=ignored"), ({"bvid": "BV17x411w7KC"}, 2))

    def test_input_rejects_foreign_urls_and_invalid_pages(self):
        for text in ("https://www.bilibili.com.evil/video/BV17x411w7KC", "https://x@www.bilibili.com/video/BV17x411w7KC",
                     "https://www.bilibili.com/bangumi/play/ep123", "BVbad", "av0",
                     "https://www.bilibili.com/video/BV17x411w7KC?p=0", "https://www.bilibili.com/video/BV17x411w7KC?p=bad"):
            with self.assertRaises(ViewerError):video_input(text)

    def test_media_url_scope_and_tls(self):
        self.assertEqual(media_url("http://example.bilivideo.com/a?key=private"), "https://example.bilivideo.com/a?key=private")
        for url in ("https://bilivideo.com.evil/a", "https://localhost/a", "https://user:secret@x.bilivideo.com/a",
                    "https://x.bilivideo.com:999/a", "file:///test", "https://x.bilivideo.com/a#frag"):
            with self.assertRaises(ViewerError):media_url(url)

    def test_wbi_sign_is_order_independent_and_filters_special_characters(self):
        key = "a" * 32 + "b" * 32
        a = wbi_params({"b": "x!'()*", "a": 1}, key, now=100)
        b = wbi_params({"a": 1, "b": "x"}, key, now=100)
        self.assertEqual(a, b)
        self.assertEqual(a["wts"], "100")
        self.assertEqual(len(a["w_rid"]), 32)
        with self.assertRaises(ViewerError):wbi_params({}, "short")

    def client(self, records=None):
        client = VodClient()
        self.addCleanup(client.close)
        client.json = Mock(side_effect=records or api_records())
        return client

    def test_available_tracks_do_not_include_locked_advertised_quality(self):
        result = self.client().resolve("BV17x411w7KC", 2)
        self.assertEqual(result.page["cid"], 279787)
        self.assertEqual([t.quality for t in result.videos], [16])
        self.assertEqual(result.selected_audio.codec, "mp4a.40.2")
        self.assertNotIn("https://", json.dumps(result.summary()))

    def test_explicit_missing_track_is_not_silently_downgraded(self):
        with self.assertRaises(ViewerError) as raised:self.client().resolve("av170001", video_key="video:120:locked")
        self.assertEqual(raised.exception.code, "vod.track_missing")

    def test_missing_page_cannot_select_another_part(self):
        with self.assertRaises(ViewerError) as raised:self.client().resolve("BV17x411w7KC", 20)
        self.assertEqual(raised.exception.code, "vod.page_missing")

    def test_expired_imported_login_does_not_masquerade_as_authenticated(self):
        client = self.client();client.login_requested = True
        with self.assertRaises(ViewerError) as raised:client.resolve("BV17x411w7KC")
        self.assertEqual(raised.exception.code, "account.expired")

    def test_authenticated_session_exposes_only_additional_returned_tracks(self):
        records = api_records();records[1]["isLogin"] = True
        higher = copy.deepcopy(records[2]["dash"]["video"][0])
        higher.update(id=64, width=1280, height=720, bandwidth=2000000)
        records[2]["dash"]["video"].append(higher)
        client = self.client(records);client.login_requested = True
        result = client.resolve("BV17x411w7KC")
        self.assertEqual([t.quality for t in result.videos], [64, 16])
        self.assertEqual(result.selected_video.width, 1280)
        self.assertNotIn(120, [t.quality for t in result.videos])

    def test_preview_and_unsupported_codec_are_explicit(self):
        records = api_records();records[-1]["is_preview"] = True
        with self.assertRaises(ViewerError) as raised:self.client(records).resolve("BV17x411w7KC")
        self.assertEqual(raised.exception.code, "vod.preview_only")
        records = api_records();records[-1]["dash"]["video"][0]["codecs"] = "vvc1.0.08M.08"
        with self.assertRaises(ViewerError) as raised:self.client(records).resolve("BV17x411w7KC")
        self.assertEqual(raised.exception.code, "vod.tracks_unavailable")

    def test_saved_video_ids_and_names_migrate_without_signed_urls(self):
        record = {"bvid": "BV17x411w7KC", "cid": 279786, "page": 1, "name": "我的视频", "url": "private-url", "cookie": "secret"}
        state = sanitize_settings({"video_favorites": [record, record], "video_history": remember_video([], record)})
        self.assertEqual(len(state["video_favorites"]), 1)
        self.assertNotIn("private-url", json.dumps(state));self.assertNotIn("secret", json.dumps(state))
        self.assertEqual(sanitize_settings({})["video_favorites"], [])
        self.assertEqual(len(remember_video(state["video_history"], record)), 1)

    def test_video_report_keeps_actual_track_but_drops_urls(self):
        value = source().summary();value["video"]["urls"] = ["https://private/signature"]
        report = clean({"playback": {"kind": "vod", "transport": {"last_stream": value}}}, REPORT)
        self.assertEqual(report["playback"]["transport"]["last_stream"]["video"]["codec"], "avc1.64001E")
        self.assertNotIn("signature", json.dumps(report))

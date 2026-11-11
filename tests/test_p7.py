from dataclasses import replace
import copy
import json
import math
import threading
import unittest
from unittest.mock import Mock
from live_viewer.core import ViewerError
from live_viewer.vod import VodClient
from live_viewer.vod_state import bookmark, progress_records, resume_position
from live_viewer.subtitles import catalog, subtitle_url, to_srt
from vod_fixtures import source, api_records


class ProgressTests(unittest.TestCase):
    def test_bookmark_is_content_scoped_and_credential_free(self):
        config = {};request = {"bvid": "BV17x411w7KC", "cid": 279786, "page": 1,
            "video_key": "video:16:avc1:1000", "audio_key": "audio:30216:mp4a:50000", "url": "secret"}
        self.assertTrue(bookmark(config, request, 54.2, 199))
        self.assertEqual(resume_position(config, request["bvid"], request["cid"]), 54.2)
        self.assertEqual(resume_position(config, request["bvid"], 279787), 0)
        self.assertNotIn("secret", json.dumps(config))
        bookmark(config, request, 199, 199, completed=True)
        self.assertEqual(resume_position(config, request["bvid"], request["cid"]), 0)

    def test_invalid_progress_cannot_poison_other_saved_items(self):
        good = {"position": 20, "duration": 199, "page": 1}
        for value in (dict(good, position=float("nan")), dict(good, position=200), dict(good, duration=-1), dict(good, page=0)):
            self.assertEqual(progress_records({"BV17x411w7KC:279786": value}), {})
        result = progress_records({"BV17x411w7KC:279786": dict(good, cookie="private", video_key="https://secret")})
        self.assertNotIn("private", json.dumps(result));self.assertIsNone(next(iter(result.values()))["video_key"])

    def test_progress_records_are_bounded(self):
        records = {f"BV17x411w7KC:{i+1}": {"position": 10, "duration": 20, "page": 1} for i in range(600)}
        self.assertEqual(len(progress_records(records)), 500)


class SubtitleTests(unittest.TestCase):
    def test_catalog_has_no_unsigned_or_foreign_resource_urls_in_summary(self):
        data = {"need_login_subtitle": False, "subtitle": {"subtitles": [{"id_str": "123", "lan": "zh-Hans", "lan_doc": "简体中文",
            "subtitle_url": "//aisubtitle.hdslb.com/bfs/subtitle/test.json?auth_key=secret"}]}}
        entries, needed = catalog(data)
        self.assertFalse(needed);self.assertEqual(entries[0].key, "123:zh-Hans")
        self.assertNotIn("secret", json.dumps(entries[0].summary()))
        for url in ("https://hdslb.com.evil/bfs/subtitle/a.json", "http://127.0.0.1/bfs/subtitle/a.json", "https://i0.hdslb.com/bfs/a.png"):
            with self.assertRaises(ViewerError):subtitle_url(url)

    def test_srt_preserves_chinese_multiline_and_milliseconds(self):
        result = to_srt({"body": [{"from": 1.125, "to": 3.75, "content": "中文\n第二行"}]})
        self.assertIn("00:00:01,125 --> 00:00:03,750", result)
        self.assertIn("中文\n第二行", result)

    def test_invalid_subtitle_times_and_unbounded_content_are_rejected(self):
        for line in ({"from": math.nan, "to": 2, "content": "x"}, {"from": 2, "to": 1, "content": "x"},
                     {"from": 0, "to": 1, "content": "x" * 2001}):
            with self.assertRaises(ViewerError):to_srt({"body": [line]})
        with self.assertRaises(ViewerError):to_srt({"body": []})

    def test_login_required_is_an_explicit_empty_catalog(self):
        entries, needed = catalog({"need_login_subtitle": True, "subtitle": {"subtitles": []}})
        self.assertEqual(entries, ());self.assertTrue(needed)


class CodecTests(unittest.TestCase):
    def test_more_sdr_codecs_are_returned_and_avc_remains_default(self):
        records = api_records()
        base = records[-1]["dash"]["video"][0]
        for codec in ("hev1.1.6.L120", "av01.0.00M.08"):
            extra = copy.deepcopy(base);extra["codecs"] = codec;records[-1]["dash"]["video"].append(extra)
        client = VodClient();self.addCleanup(client.close);client.json = Mock(side_effect=records)
        result = client.resolve("BV17x411w7KC")
        self.assertEqual(len(result.videos), 3)
        self.assertTrue(result.selected_video.codec.startswith("avc1"))
        self.assertTrue(any("HEVC" in t.label for t in result.videos))
        self.assertTrue(any("AV1" in t.label for t in result.videos))

    def test_cid_is_resolved_after_part_order_changes(self):
        records = api_records();records[0]["pages"].reverse()
        client = VodClient();self.addCleanup(client.close);client.json = Mock(side_effect=records)
        self.assertEqual(client.resolve("BV17x411w7KC", page=1, cid=279787).page["cid"], 279787)

    def test_missing_cid_cannot_resume_a_different_part(self):
        client = VodClient();self.addCleanup(client.close);client.json = Mock(side_effect=api_records())
        with self.assertRaises(ViewerError) as raised:client.resolve("BV17x411w7KC", cid=999999)
        self.assertEqual(raised.exception.code, "vod.part_changed")

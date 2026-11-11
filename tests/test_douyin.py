"""抖音取流测试：全部离线，用固定响应结构验证解析与错误分支。

真实结构依据 P9-A 实测（docs/p9a-acceptance.md）：
  stream_url 同时返回 live_core_sdk_data 新结构与 flv_pull_url 旧结构；
  sdk_params 字段名是 VCodec / vbitrate（大小写敏感）；ao 是纯音频档位。
"""
import json
import unittest

from live_viewer.core import (
    DOUYIN_LEGACY_TIERS, DOUYIN_TIERS, PlatformClient, ViewerError,
    douyin_streams, room_number,
)


def sdk(codec="h264", bitrate=2_000_000, resolution="1280x720", fps=30):
    return json.dumps({"VCodec": codec, "vbitrate": bitrate, "resolution": resolution,
                       "gop": 2, "drType": "sdr", "fps": fps})


def stream_url(tiers=None, legacy=None, backup=None):
    """按实测结构构造 stream_url。tiers 的键即 origin/uhd/hd/sd/ld/md/ao。"""
    data = {}
    for key, params in (tiers or {}).items():
        main = {"flv": f"http://pull-flv-l1.douyincdn.com/third/stream-1_{key}.flv?t_id=abc",
                "sdk_params": params}
        if key != "ao":
            main["hls"] = f"http://pull-hls-l1.douyincdn.com/third/stream-1_{key}.m3u8"
        entry = {"main": main}
        if backup and key in backup:
            entry["backup"] = {"flv": f"http://pull-flv-l2.douyincdn.com/third/stream-1_{key}.flv?t_id=def"}
        data[key] = entry
    return {"live_core_sdk_data": {"pull_data": {"stream_data": json.dumps({"common": {}, "data": data})}},
            "flv_pull_url": legacy or {}}


FULL = {
    "origin": sdk("h264", 15_611_000, "1920x1080", 59),
    "uhd": sdk("h264", 6_000_000, "1920x1080", 45),
    "hd": sdk("h264", 4_000_000, "1280x720", 30),
    "sd": sdk("h264", 2_000_000, "1280x720", 30),
    "ld": sdk("h264", 1_000_000, "960x540", 25),
    "md": sdk("h264", 800_000, "640x360", 15),
    "ao": sdk("h264", 0, "", 0),
}


def payload(status=2, stack=None):
    return {"status_code": 0,
            "data": {"data": [{"status": status, "stream_url": stack or {}}]}}


class DouyinStreamParsingTests(unittest.TestCase):
    def test_new_layout_excludes_audio_only_and_sorts_by_quality(self):
        tiers = douyin_streams(stream_url(FULL))
        self.assertEqual([t["value"] for t in tiers], [6, 5, 4, 3, 2, 1])
        self.assertNotIn("ao", [t["name"] for t in tiers])
        self.assertEqual(tiers[0]["name"], "原画")
        self.assertEqual(tiers[0]["kbps"], 15611)
        self.assertEqual(tiers[-1]["name"], "极速")

    def test_api_http_urls_are_upgraded_to_https(self):
        for tier in douyin_streams(stream_url(FULL)):
            for url in tier["urls"]:
                self.assertTrue(url.startswith("https://"), url)

    def test_legacy_layout_is_used_only_when_new_layout_is_absent(self):
        legacy = {"FULL_HD1": "http://pull-flv-l1.douyincdn.com/a.flv",
                  "HD1": "http://pull-flv-l1.douyincdn.com/b.flv",
                  "SD1": "http://pull-flv-l1.douyincdn.com/c.flv",
                  "SD2": "http://pull-flv-l1.douyincdn.com/d.flv"}
        tiers = douyin_streams({"flv_pull_url": legacy})
        self.assertEqual([t["value"] for t in tiers], sorted(
            [value for value, _ in DOUYIN_LEGACY_TIERS.values()], reverse=True))
        self.assertTrue(all(t["layout"] == "flv_pull_url" for t in tiers))
        # 新结构存在时不得回退到旧结构
        mixed = stream_url(FULL, legacy)
        self.assertEqual(len(douyin_streams(mixed)), len(FULL) - 1)

    def test_h264_is_preferred_but_hevc_only_rooms_still_work(self):
        hevc = {"origin": sdk("h265", 8_000_000, "1920x1080", 60),
                "hd": sdk("h265", 3_000_000, "1280x720", 30),
                "sd": sdk("h264", 1_500_000, "1280x720", 30)}
        tiers = douyin_streams(stream_url(hevc))
        self.assertEqual(tiers[0]["name"], "标清", "存在 h264 时应只保留 h264 档位")
        self.assertTrue(all(t["codec"] == "h264" for t in tiers))
        tiers = douyin_streams(stream_url({"origin": sdk("h265", 8_000_000, "1920x1080", 60)}))
        self.assertEqual(len(tiers), 1, "整个房间只有 h265 时不应丢弃所有档位")

    def test_backup_host_is_offered_as_a_second_candidate(self):
        tiers = douyin_streams(stream_url(FULL, backup={"origin"}))
        origin = next(t for t in tiers if t["value"] == 6)
        self.assertEqual(len(origin["urls"]), 2)
        self.assertIn("pull-flv-l2", origin["urls"][1])

    def test_malformed_stream_data_does_not_raise(self):
        self.assertEqual(douyin_streams({"live_core_sdk_data": {"pull_data": {"stream_data": "{not json"}}}), [])
        self.assertEqual(douyin_streams({}), [])
        self.assertEqual(douyin_streams({"live_core_sdk_data": None}), [])


class DouyinResolveTests(unittest.TestCase):
    def client(self, response):
        client = PlatformClient("douyin")
        client._douyin_session = lambda: None
        client._douyin_enter = lambda room: response
        self.addCleanup(client.close)
        return client

    def test_default_request_selects_origin_quality(self):
        stream = self.client(payload(2, stream_url(FULL))).resolve("67876457927")
        self.assertEqual((stream.actual, stream.actual_name), (6, "原画"))
        self.assertEqual(stream.codec, "AVC")
        self.assertEqual(len(stream.urls), 1)
        self.assertTrue(stream.urls[0].startswith("https://"))
        self.assertEqual([q.value for q in stream.qualities], [6, 5, 4, 3, 2, 1])

    def test_lower_request_downgrades_and_never_exceeds_the_request(self):
        client = self.client(payload(2, stream_url(FULL)))
        self.assertEqual(client.resolve("1", 4).actual_name, "高清")
        self.assertEqual(client.resolve("1", 3).actual_name, "标清")
        # 请求值低于最低档时退回最低档，而不是报错
        self.assertEqual(client.resolve("1", 0).actual_name, "极速")

    def test_missing_tier_falls_back_to_the_next_one_down(self):
        tiers = {"origin": sdk("h264", 15_000_000), "sd": sdk("h264", 2_000_000)}
        stream = self.client(payload(2, stream_url(tiers))).resolve("1", 4)
        self.assertEqual(stream.actual_name, "标清")

    def test_offline_and_ended_rooms_are_typed_warnings(self):
        for status in (4, 0, None):
            with self.assertRaises(ViewerError) as error:
                self.client(payload(status, stream_url(FULL))).resolve("1")
            self.assertEqual(error.exception.code, "room.offline")
            self.assertEqual(error.exception.level, "WARN")

    def test_room_without_any_data_is_offline(self):
        with self.assertRaises(ViewerError) as error:
            self.client({"status_code": 0, "data": {"data": []}}).resolve("1")
        self.assertEqual(error.exception.code, "room.offline")

    def test_live_room_without_flv_stream_is_rejected_not_silently_empty(self):
        with self.assertRaises(ViewerError) as error:
            self.client(payload(2, {"flv_pull_url": {}})).resolve("1")
        self.assertEqual(error.exception.code, "platform.stream_rejected")

    def test_hevc_only_room_reports_hevc_codec(self):
        stream = self.client(payload(2, stream_url({"origin": sdk("h265", 8_000_000)}))).resolve("1")
        self.assertEqual(stream.codec, "HEVC")

    def test_enter_status_codes_map_to_actionable_errors(self):
        cases = {4001038: ("platform.stream_rejected", "WARN"), 10011: ("platform.busy", "WARN")}
        for code, (expected_code, level) in cases.items():
            client = PlatformClient("douyin")
            self.addCleanup(client.close)
            client._douyin_session = lambda: None
            # 错误映射在 _douyin_enter 内部，因此要替换 _api 而不是 _douyin_enter。
            client._api = lambda url, params=None, data=None, c=code: (
                {"status_code": c, "data": {"data": []}}, {})
            with self.assertRaises(ViewerError) as error:
                client.resolve("1")
            self.assertEqual(error.exception.code, expected_code)
            self.assertEqual(error.exception.level, level)

    def test_missing_ttwid_is_reported_explicitly(self):
        class Response:
            status_code = 200

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        client = PlatformClient("douyin")
        self.addCleanup(client.close)
        client.session.get = lambda *args, **kwargs: Response()  # 不发网络请求，且不下发 ttwid
        with self.assertRaises(ViewerError) as error:
            client._douyin_session()
        self.assertEqual(error.exception.code, "platform.session_rejected")


class DouyinRegistrationTests(unittest.TestCase):
    def test_room_number_accepts_the_live_page_and_rejects_other_platforms(self):
        self.assertEqual(room_number("douyin", "https://live.douyin.com/67876457927"), "67876457927")
        self.assertEqual(room_number("douyin", "67876457927"), "67876457927")
        with self.assertRaises(ViewerError):
            room_number("douyin", "https://www.douyu.com/6657")
        with self.assertRaises(ViewerError):
            room_number("douyin", "https://www.douyin.com/video/123")

    def test_qr_login_is_refused_for_douyin(self):
        from live_viewer.qr_login import QrLogin
        with self.assertRaises(ViewerError) as error:
            QrLogin("douyin")
        self.assertEqual(error.exception.code, "account.no_qr")

    def test_douyin_has_no_verifiable_login_status(self):
        client = PlatformClient("douyin")
        self.addCleanup(client.close)
        status = client.login_status()
        self.assertIsNone(status["verified"])
        self.assertIn("抖音", status["message"])

    def test_quality_values_do_not_collide_with_audio_only_or_unknown_tiers(self):
        self.assertNotIn("ao", DOUYIN_TIERS)
        self.assertEqual(sorted(DOUYIN_TIERS.values(), reverse=True),
                         [(6, "原画"), (5, "超清"), (4, "高清"), (3, "标清"), (2, "流畅"), (1, "极速")])

    def test_observed_cdn_hosts_pass_the_media_whitelist(self):
        """两个后缀都是实测见过的真实主机；只放其中一个会误拒备用线路。"""
        from live_viewer.core import validate_media_url
        for url in ("https://pull-flv-l13.douyincdn.com/third/stream-1_or4.flv?sig=1",
                    "https://pull-flv-q13-admin.douyincdn.com/third/stream-1_hd.flv?sig=1",
                    "https://pull-flv-f11.douyinliving.com/third/stream-1_or4.flv?sig=1",
                    "https://pull-flv-f11-admin.douyinliving.com/third/stream-1_hd.flv?sig=1"):
            validate_media_url(url, "douyin")

    def test_unrelated_hosts_are_still_rejected(self):
        from live_viewer.core import validate_media_url
        for url in ("https://evil.example.com/stream.flv",
                    "https://notdouyincdn.com/stream.flv",
                    "https://douyincdn.com.evil.example.com/stream.flv"):
            with self.assertRaises(ViewerError):
                validate_media_url(url, "douyin")


if __name__ == "__main__":
    unittest.main()

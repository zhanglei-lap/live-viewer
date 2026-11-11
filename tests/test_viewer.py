import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from live_viewer.core import (DouyuRoom, PlatformClient, Quality, Stream, ViewerError, comparison,
                              load_cookies, read_flv_sample, room_number, save_report, validate_media_url)
from live_viewer.relay import LocalRelay


def synthetic_flv():
    def tag(kind, timestamp, payload):
        return bytes([kind]) + len(payload).to_bytes(3, "big") + (timestamp & 0xFFFFFF).to_bytes(3, "big") + bytes([timestamp >> 24]) + b"\x00\x00\x00" + payload + (len(payload) + 11).to_bytes(4, "big")
    metadata = b"".join(len(k).to_bytes(2, "big") + k + b"\x00" + struct.pack(">d", v)
                         for k, v in [(b"width", 1280), (b"height", 720), (b"framerate", 30)])
    return (b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00" + tag(18, 0, metadata)
            + tag(9, 0, b"\x17\x00" + b"a" * 30)
            + tag(9, 200000, b"\x27\x01" + b"a" * 100)
            + tag(8, 200000, b"\xaf\x01" + b"a" * 30)
            + tag(9, 201000, b"\x27\x01" + b"a" * 100))


class Response:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *_): self.close()
    def close(self): pass
    def iter_content(self, _): yield self.data


class CoreTests(unittest.TestCase):
    def test_douyu_healthy_primary_does_not_request_backups(self):
        client = PlatformClient("douyu")
        stream = Stream("douyu", "6979222", 0, 4, "blue", [],
                        ["https://a.douyucdn2.cn/live.flv"], backup_cdns=["hw-h5"])
        try:
            with patch("live_viewer.core.open_media", return_value=Response(synthetic_flv())), \
                 patch.object(client, "_douyu_play") as play:
                sample = read_flv_sample(client, stream)
                self.assertEqual(sample["video_codec"], "H.264")
                play.assert_not_called()
        finally:
            client.close()

    def test_douyu_logged_in_backup_keeps_original_quality_and_cookies(self):
        jar = requests.cookies.RequestsCookieJar()
        jar.set("login_test", "TEST_SECRET", domain=".douyu.com", secure=True)
        client = PlatformClient("douyu", jar)
        stream = Stream("douyu", "6979222", 0, 0, "source", [],
                        ["https://a.edgesrv.com/live.flv"], backup_cdns=["hw-h5"])
        backup = {"rate": 0, "rtmp_url": "https://hw1a.douyucdn2.cn", "rtmp_live": "source.flv?token=SECRET"}
        try:
            with patch("live_viewer.core.open_media", side_effect=[ViewerError("TLS failed"), Response(synthetic_flv())]), \
                 patch.object(client, "_douyu_play", return_value=backup) as play:
                sample = read_flv_sample(client, stream)
                self.assertEqual(sample["video_codec"], "H.264")
                play.assert_called_once_with("6979222", 0, "hw-h5")
            api = client.session.prepare_request(requests.Request("POST", "https://www.douyu.com/lapi/live/getH5PlayV1/6979222"))
            media = client.session.prepare_request(requests.Request("GET", "https://hw1a.douyucdn2.cn/source.flv"))
            self.assertIn("login_test=TEST_SECRET", api.headers["Cookie"])
            self.assertNotIn("Cookie", media.headers)
            self.assertNotIn("SECRET", json.dumps(stream.summary()))
        finally:
            client.close()

    def test_douyu_backups_skip_downgraded_invalid_and_failed_responses(self):
        client = PlatformClient("douyu")
        primary = "https://a.edgesrv.com/source.flv"
        stream = Stream("douyu", "6979222", 0, 0, "source", [], [primary],
                        backup_cdns=["low", "invalid", "failed"])
        try:
            with patch.object(client, "_douyu_play", side_effect=[
                {"rate": 4, "rtmp_url": "https://a.douyucdn2.cn", "rtmp_live": "low.flv"},
                {"rtmp_url": "https://a.douyucdn2.cn", "rtmp_live": "invalid.flv"},
                ViewerError("API unavailable"),
            ]):
                self.assertEqual(list(client.media_urls(stream)), [primary])
        finally:
            client.close()

    def test_douyu_api_backup_list_is_deduplicated_bounded_and_prefers_hw(self):
        client = PlatformClient("douyu")
        play = {"rate": 4, "rtmp_url": "https://a.edgesrv.com", "rtmp_live": "live.flv",
                "rtmp_cdn": "primary", "cdnsWithName": [{"cdn": value} for value in
                ["primary", "second", "second", "https://evil.example", "third", "fourth", "hw-h5"]]}
        try:
            with patch.object(client, "_douyu_room", return_value=DouyuRoom("6979222", 1)), \
                 patch.object(client, "_douyu_play", return_value=play):
                stream = client.resolve("6657")
            self.assertEqual(stream.backup_cdns, ["hw-h5", "second", "third"])
        finally:
            client.close()

    def test_tls_failure_keeps_certificate_verification_and_hides_signed_url(self):
        from unittest.mock import Mock
        from live_viewer.core import open_media
        session = Mock()
        session.get.side_effect = requests.exceptions.SSLError("https://a.edgesrv.com/SECRET?token=SECRET")
        with self.assertRaises(ViewerError) as caught:
            open_media(session, "https://a.edgesrv.com/SECRET?token=SECRET", "douyu")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertNotEqual(session.get.call_args.kwargs.get("verify", True), False)

    def test_douyu_cdn_redirect_support_preserves_destination_validation(self):
        from unittest.mock import Mock
        from live_viewer.core import open_media
        session = Mock()
        redirect = Mock(status_code=302, headers={"Location": "https://hash.livehwc4.com/live.flv?token=SECRET"})
        media = Mock(status_code=200)
        session.get.side_effect = [redirect, media]
        self.assertIs(open_media(session, "https://hw1a.douyucdn2.cn/live.flv", "douyu"), media)
        self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
        redirect.headers["Location"] = "https://livehwc4.com.evil.example/live.flv"
        session.get.side_effect = [redirect]
        with self.assertRaises(ViewerError): open_media(session, "https://hw1a.douyucdn2.cn/live.flv", "douyu")

    def test_cdn_validation_errors_remain_visible_without_url_credentials(self):
        client = PlatformClient("douyu")
        stream = Stream("douyu", "6979222", 0, 4, "blue", [], ["https://other.example/live.flv?token=SECRET"])
        with self.assertRaises(ViewerError) as caught: read_flv_sample(client, stream)
        self.assertIn("CDN", str(caught.exception))
        self.assertIn("other.example", str(caught.exception))
        self.assertNotIn("SECRET", str(caught.exception))
        client.close()

    def test_douyu_short_id_resolves_main_room_not_recommendations(self):
        from live_viewer.core import parse_douyu_room
        other = {"roomInfo": {"room": {"room_id": 12345, "vipId": 123, "show_status": 1}}}
        main = {"roomInfo": {"room": {"room_id": 6979222, "vipId": 6657, "show_status": 1}}}
        payload = "1:" + json.dumps(other) + "\n2:" + json.dumps(main)
        split = payload.index("6979222") + 3
        page = "".join("<script>self.__next_f.push(" + json.dumps([1, chunk]) + ")</script>"
                       for chunk in (payload[:split], payload[split:]))
        self.assertEqual(parse_douyu_room(page, "6657"), DouyuRoom("6979222", 1))
        self.assertEqual(parse_douyu_room(page, "6979222"), DouyuRoom("6979222", 1))

    def test_douyu_legacy_room_assignment_and_unrecognized_page(self):
        from live_viewer.core import parse_douyu_room
        page = '<a href="/12345">recommendation</a><script>window.room_id = 6979222;</script>'
        self.assertEqual(parse_douyu_room(page, "6657").room, "6979222")
        with self.assertRaises(ViewerError): parse_douyu_room('<a href="/12345">recommendation</a>', "6657")

    def test_douyu_short_id_is_normalized_before_signing_and_posting(self):
        client = PlatformClient("douyu")
        enc = {"error": 0, "data": {"enc_time": 1, "rand_str": "r", "key": "k", "is_special": 0, "enc_data": "x"}}
        headers = {"date": "Mon, 05 Oct 2026 14:44:04 GMT"}
        play = {"error": 0, "data": {"rate": 4, "rtmp_url": "https://a.douyucdn2.cn", "rtmp_live": "live.flv",
                                     "multirates": [{"rate": 4, "name": "blue", "bit": 4000}]}}
        with patch.object(client, "_douyu_room", return_value=DouyuRoom("6979222", 1)), \
             patch.object(client, "_api", side_effect=[(enc, headers), (play, {})]) as api:
            stream = client.resolve("6657")
        self.assertEqual(stream.room, "6979222")
        self.assertTrue(api.call_args_list[1].args[0].endswith("/6979222"))
        import hashlib
        # Server timestamp, DID and real room must all be used in the signed request.
        from email.utils import parsedate_to_datetime
        stamp = int(parsedate_to_datetime(headers["date"]).timestamp())
        digest = hashlib.md5(b"rk").hexdigest()
        expected = hashlib.md5((digest + "k" + f"6979222{stamp}").encode()).hexdigest()
        self.assertEqual(api.call_args_list[1].kwargs["data"]["auth"], expected)
        client.close()

    def test_douyu_offline_main_room_skips_play_api(self):
        client = PlatformClient("douyu")
        with patch.object(client, "_douyu_room", return_value=DouyuRoom("6979222", 2)), patch.object(client, "_api") as api:
            with self.assertRaises(ViewerError): client.resolve("6657")
            api.assert_not_called()
        client.close()

    def test_extensionless_export_is_supported_but_sqlite_is_explained(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Cookies"
            path.write_text("# Netscape HTTP Cookie File\n.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tTEST_SECRET\n", encoding="utf-8")
            self.assertEqual(len(load_cookies(path, "bilibili")), 1)
            path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 128)
            with self.assertRaises(ViewerError) as caught:
                load_cookies(path, "bilibili")
            self.assertIn("Edge/Chrome", str(caught.exception))
            self.assertNotIn("TEST_SECRET", str(caught.exception))

    def test_pasted_cookies_are_scoped_and_preserve_equals_in_values(self):
        from live_viewer.core import cookies_from_text
        jar = cookies_from_text("Cookie: SESSDATA=TEST_SECRET==; bili_jct=abc", "bilibili")
        self.assertEqual(jar.get("SESSDATA"), "TEST_SECRET==")
        self.assertTrue(all(c.domain == ".bilibili.com" and c.secure for c in jar))
        prepared = requests.Request("GET", "https://other.example/", cookies=jar).prepare()
        self.assertNotIn("Cookie", prepared.headers)
        bare = cookies_from_text("TEST_SECRET", "bilibili")
        self.assertEqual(bare.get("SESSDATA"), "TEST_SECRET")

    def test_paste_errors_never_echo_secrets(self):
        from live_viewer.core import cookies_from_text
        for text in ["SESSDATA=TEST_SECRET\r\nInjected: yes", "bili_jct=TEST_SECRET", "SESSDATA="]:
            with self.assertRaises(ViewerError) as caught:
                cookies_from_text(text, "bilibili")
            self.assertNotIn("TEST_SECRET", str(caught.exception))

    def test_room_links_strip_tracking_and_reject_other_hosts(self):
        self.assertEqual(room_number("bilibili", "https://live.bilibili.com/35?session_id=secret"), "35")
        self.assertEqual(room_number("douyu", "https://www.douyu.com/topic/abc?rid=6657"), "6657")
        for value in ["https://evil.com/35", "https://live.bilibili.com.evil.com/35", "file:///35"]:
            with self.assertRaises(ViewerError): room_number("bilibili", value)

    def test_cookie_filter_keeps_only_selected_platform_and_honors_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cookies.txt"
            path.write_text("# Netscape HTTP Cookie File\n"
                            ".bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tTEST_SECRET\n"
                            ".bilibili.com\tTRUE\t/\tTRUE\t1\texpired\tNO\n"
                            ".douyu.com\tTRUE\t/\tTRUE\t0\tother\tNO\n"
                            ".evilbilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tNO\n", encoding="utf-8")
            jar = load_cookies(path, "bilibili")
            self.assertEqual([(c.name, c.value) for c in jar], [("SESSDATA", "TEST_SECRET")])

    def test_actual_bilibili_quality_is_not_advertised_quality(self):
        client = PlatformClient("bilibili")
        responses = [({"code": 0, "data": {"live_status": 1, "room_id": 21622811}}, {}),
                     ({"code": 0, "data": {"playurl_info": {"playurl": {
                         "g_qn_desc": [{"qn": 10000, "desc": "source"}, {"qn": 250, "desc": "720p"}],
                         "stream": [{"protocol_name": "http_stream", "format": [{"format_name": "flv", "codec": [{
                             "codec_name": "avc", "current_qn": 250, "accept_qn": [10000, 250],
                             "base_url": "/live.flv", "url_info": [{"host": "https://a.bilivideo.com", "extra": "?token=SECRET"}],
                         }]}]}]}}}}, {})]
        with patch.object(client, "_api", side_effect=responses): result = client.resolve("35", 10000)
        self.assertEqual(result.actual, 250)
        self.assertTrue(result.summary()["different_from_request"])
        self.assertNotIn("SECRET", repr(result))
        self.assertNotIn("SECRET", json.dumps(result.summary()))
        client.close()

    def test_douyu_actual_rate_required_not_guessed(self):
        client = PlatformClient("douyu")
        enc = {"error": 0, "data": {"enc_time": 1, "rand_str": "r", "key": "k", "is_special": 0, "enc_data": "x"}}
        headers = {"date": "Mon, 05 Oct 2026 14:44:04 GMT"}
        play = {"error": 0, "data": {"rtmp_url": "https://a.douyucdn2.cn", "rtmp_live": "live.flv?token=SECRET",
                                     "multirates": [{"rate": 0, "name": "source", "bit": 10000}, {"rate": 4, "name": "blue", "bit": 4000}]}}
        with patch.object(client, "_douyu_room", return_value=DouyuRoom("36252", 1)), patch.object(client, "_api", side_effect=[(enc, headers), (play, {})]):
            with self.assertRaises(ViewerError): client.resolve("36252")
        play["data"]["rate"] = 4
        with patch.object(client, "_douyu_room", return_value=DouyuRoom("36252", 1)), patch.object(client, "_api", side_effect=[(enc, headers), (play, {})]): stream = client.resolve("36252")
        self.assertEqual(stream.actual, 4)
        self.assertEqual(stream.nominal_kbps, 4000)
        client.close()

    def test_sample_excludes_sequence_header_timestamp_and_does_not_claim_decoding(self):
        client = PlatformClient("bilibili")
        stream = Stream("bilibili", "35", 10000, 250, "720p", [], ["https://a.bilivideo.com/live.flv"])
        with patch("live_viewer.core.open_media", return_value=Response(synthetic_flv())): result = read_flv_sample(client, stream)
        self.assertEqual(result["media_seconds"], 1.0)
        self.assertEqual(result["metadata"]["width"], 1280)
        self.assertEqual(result["video_codec"], "H.264")
        self.assertFalse(result["decoded"])
        client.close()

    def test_media_addresses_must_remain_on_https_platform_cdns(self):
        validate_media_url("https://a.bilivideo.com/live.flv", "bilibili")
        validate_media_url("https://a.edgesrv.com/live.flv", "douyu")
        with self.assertRaises(ViewerError):
            validate_media_url("https://edgesrv.com.evil.example/live.flv", "douyu")
        for url in ["http://a.bilivideo.com/live.flv", "https://127.0.0.1/live.flv",
                    "https://a.bilivideo.com.evil.com/live.flv", "https://a.bilivideo.com:123/live.flv"]:
            with self.assertRaises(ViewerError): validate_media_url(url, "bilibili")

    def test_invalid_login_does_not_produce_a_logged_in_stream_result(self):
        jar = requests.cookies.RequestsCookieJar()
        jar.set("SESSDATA", "SECRET", domain=".bilibili.com")
        stream = Stream("bilibili", "35", 10000, 250, "720p", [Quality(250, "720p")], ["https://a.bilivideo.com/live.flv"])
        with patch.object(PlatformClient, "resolve", return_value=stream), \
             patch.object(PlatformClient, "login_status", return_value={"verified": False, "message": "invalid login"}), \
             patch("live_viewer.core.read_flv_sample", return_value={"decoded": False}):
            rows, _ = comparison("bilibili", "35", None, jar)
        self.assertIn("actual", rows[0])
        self.assertNotIn("actual", rows[1])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            save_report(path, rows, {})
            self.assertNotIn("SECRET", path.read_text(encoding="utf-8"))


class RelayTests(unittest.TestCase):
    def test_source_preflight_reuses_checked_flv_header_and_cleans_unclaimed_response(self):
        stream = Stream("bilibili", "35", 10000, 250, "720p", [], ["https://a.bilivideo.com/live.flv"])
        client = PlatformClient("bilibili")
        response = Response(synthetic_flv())
        with patch("live_viewer.relay.open_media", return_value=response) as media:
            relay = LocalRelay("bilibili", "35", None, initial_client=client, initial_stream=stream)
            relay.prepare_first_source();relay.start()
            try:
                self.assertEqual(requests.get(relay.url, timeout=5).content, synthetic_flv())
                media.assert_called_once()
            finally: relay.stop()
        client = PlatformClient("bilibili")
        with patch("live_viewer.relay.open_media", return_value=response), patch.object(response,"close") as close:
            relay = LocalRelay("bilibili", "35", None, initial_client=client, initial_stream=stream)
            relay.prepare_first_source();relay.stop()
            close.assert_called_once()

    def test_prepared_first_connection_skips_duplicate_lookup_then_resolves_fresh(self):
        stream = Stream("bilibili", "35", 10000, 250, "720p", [], ["https://a.bilivideo.com/live.flv?SECRET"])
        client = PlatformClient("bilibili")
        with patch.object(client, "resolve") as first_resolve, patch.object(client, "close", wraps=client.close) as close, \
             patch.object(PlatformClient, "resolve", return_value=stream) as next_resolve, \
             patch("live_viewer.relay.open_media", side_effect=lambda *_: Response(synthetic_flv())):
            relay = LocalRelay("bilibili", "35", None, initial_client=client, initial_stream=stream)
            relay.start()
            try:
                self.assertEqual(requests.get(relay.url, timeout=5).status_code, 200)
                first_resolve.assert_not_called();next_resolve.assert_not_called()
                self.assertEqual(requests.get(relay.url, timeout=5).status_code, 200)
                next_resolve.assert_called_once_with("35", None)
                close.assert_called_once()
            finally: relay.stop()

    def test_stopping_before_player_connects_closes_prepared_client(self):
        client = PlatformClient("bilibili")
        with patch.object(client, "close", wraps=client.close) as close:
            relay = LocalRelay("bilibili", "35", None, initial_client=client, initial_stream=object())
            relay.stop()
            close.assert_called_once()

    def test_douyu_relay_retries_before_sending_and_retains_source_quality(self):
        jar = requests.cookies.RequestsCookieJar()
        jar.set("login_test", "TEST_SECRET", domain=".douyu.com")
        stream = Stream("douyu", "6979222", 0, 0, "source", [],
                        ["https://new.example/SECRET?token=SECRET"], backup_cdns=["hw-h5"])
        backup = {"rate": 0, "rtmp_url": "https://a.douyucdn2.cn", "rtmp_live": "source.flv?token=SECRET"}
        payload = synthetic_flv()
        with patch.object(PlatformClient, "resolve", return_value=stream), \
             patch.object(PlatformClient, "_douyu_play", return_value=backup) as play, \
             patch("live_viewer.relay.open_media", side_effect=[ViewerError("unknown CDN"), Response(payload)]):
            relay = LocalRelay("douyu", "6657", None, jar)
            relay.start()
            try:
                with requests.get(relay.url, timeout=5) as response:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, payload)
                    self.assertNotIn("Set-Cookie", response.headers)
                play.assert_called_once_with("6979222", 0, "hw-h5")
                stats = relay.snapshot()
                self.assertEqual(stats["last_stream"]["actual"], 0)
                self.assertNotIn("SECRET", json.dumps(stats))
            finally:
                relay.stop()

    def test_douyu_relay_does_not_splice_backup_after_sending_flv(self):
        class InterruptedResponse(Response):
            def iter_content(self, _):
                yield self.data
                raise requests.ConnectionError("SECRET")
        payload = synthetic_flv()
        stream = Stream("douyu", "6979222", 0, 4, "blue", [],
                        ["https://a.douyucdn2.cn/live.flv"], backup_cdns=["hw-h5"])
        with patch.object(PlatformClient, "resolve", return_value=stream), \
             patch.object(PlatformClient, "_douyu_play") as play, \
             patch("live_viewer.relay.open_media", return_value=InterruptedResponse(payload)):
            relay = LocalRelay("douyu", "6657", None)
            relay.start()
            try:
                with requests.get(relay.url, timeout=5) as response:
                    self.assertEqual(response.content, payload)
                play.assert_not_called()
                self.assertNotIn("SECRET", json.dumps(relay.snapshot()))
            finally:
                relay.stop()

    def test_local_relay_streams_bytes_without_forwarding_cookies_or_upstream_urls(self):
        stream = Stream("bilibili", "35", 10000, 250, "720p", [], ["https://a.bilivideo.com/live.flv?SECRET"])
        payload = synthetic_flv()
        with patch.object(PlatformClient, "resolve", return_value=stream), \
             patch("live_viewer.relay.open_media", side_effect=lambda *_: Response(payload)):
            relay = LocalRelay("bilibili", "35", None)
            relay.start()
            try:
                self.assertEqual(relay.server.server_address[0], "127.0.0.1")
                response = requests.get(relay.url, timeout=5)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, payload)
                self.assertNotIn("Set-Cookie", response.headers)
                stats = relay.snapshot()
                self.assertNotIn("SECRET", json.dumps(stats))
                self.assertEqual(stats["last_stream"]["actual"], 250)
                self.assertEqual(requests.get(relay.url + "wrong", timeout=5).status_code, 404)
                requests.get(relay.url, timeout=5)
                self.assertEqual(relay.snapshot()["connections"], 2)
            finally:
                relay.stop()


if __name__ == "__main__": unittest.main()

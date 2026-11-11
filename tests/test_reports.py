import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from live_viewer.core import Stream, Quality, ViewerError, read_flv_sample
from live_viewer.reports import connection_check, compare_quality, save_diagnostic_report


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.request = {"platform": "douyu", "room_input": "6657", "requested": None, "network_mode": "direct"}
        self.stream = Stream("douyu", "6979222", 0, 0, "原画", [Quality(0, "原画")], ["https://private.example/secret"])
        self.client = Mock()
        self.client.resolve.return_value = self.stream
        self.client.login_status.return_value = {"verified": None, "message": "未独立验证"}
        self.sample = {"verification": "container_only", "decoded": False, "metadata": {"width": 1920, "height": 1080},
                       "sample_total_kbps": 4050, "bytes_read": 1234}

    def test_connection_success_reports_container_only_and_no_media_addresses(self):
        with patch("live_viewer.reports.PlatformClient", return_value=self.client), \
             patch("live_viewer.reports.read_flv_sample", return_value=self.sample):
            result = connection_check(self.request)
        self.assertEqual(result["stage"], "完成")
        self.assertFalse(result["sample"]["decoded"])
        self.assertNotIn("private.example", json.dumps(result))
        self.client.close.assert_called_once()

    def test_offline_connection_is_a_warning_and_not_a_decode_claim(self):
        self.client.resolve.side_effect = ViewerError("未开播", code="room.offline", level="WARN")
        with patch("live_viewer.reports.PlatformClient", return_value=self.client):
            result = connection_check(self.request)
        self.assertEqual(result["error_code"], "room.offline")
        self.assertEqual(result["level"], "WARN")
        self.assertNotIn("sample", result)

    def test_compare_reports_actual_quality_and_skips_absent_account(self):
        with patch("live_viewer.reports.PlatformClient", return_value=self.client), \
             patch("live_viewer.reports.read_flv_sample", return_value=self.sample):
            rows, qualities = compare_quality(self.request)
        self.assertEqual(rows[0]["actual"], 0)
        self.assertEqual(rows[0]["sample"]["sample_total_kbps"], 4050)
        self.assertTrue(rows[1]["skipped"])
        self.assertEqual(qualities[0].value, 0)

    def test_cancel_between_sessions_prevents_authenticated_request(self):
        cancel = threading.Event()
        def sample(*_, **kw):
            cancel.set()
            return self.sample
        with patch("live_viewer.reports.PlatformClient", return_value=self.client) as factory, \
             patch("live_viewer.reports.read_flv_sample", side_effect=sample):
            with self.assertRaises(ViewerError) as error:
                compare_quality(self.request, {"synthetic": "jar"}, cancel=cancel)
        self.assertEqual(error.exception.code, "operation.cancelled")
        self.assertEqual(factory.call_count, 1)

    def test_unknown_exception_does_not_export_its_raw_payload(self):
        self.client.resolve.side_effect = RuntimeError("Cookie: synthetic-secret")
        with patch("live_viewer.reports.PlatformClient", return_value=self.client):
            result = connection_check(self.request)
        self.assertEqual(result["error_code"], "operation.unexpected")
        self.assertNotIn("synthetic-secret", json.dumps(result))

    def test_report_schema_filters_nested_secrets_urls_and_nonfinite_numbers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            report = {"generated_at": "test", "headers": {"Cookie": "SECRET"}, "connection": {"status": "Cookie: SECRET",
                       "sample": {"metadata": {"width": 1920, "height": float("nan"), "secret": "SECRET"},
                                  "cookies": {"name": "SECRET"}}},
                      "playback": {"room_name": "https://private.example/secret", "potplayer": {"url": "SECRET", "width": 1920},
                                   "user_note": "SESSDATA=SECRET"}}
            save_diagnostic_report(path, report)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("SECRET", text)
            self.assertNotIn("private.example", text)
            self.assertNotIn("NaN", text)
            self.assertEqual(json.loads(text)["playback"]["potplayer"]["width"], 1920)

    def test_cancel_before_sampling_does_not_even_ask_for_cdn_addresses(self):
        cancel = threading.Event();cancel.set()
        with self.assertRaises(ViewerError) as error:
            read_flv_sample(self.client, self.stream, cancelled=cancel)
        self.assertEqual(error.exception.code, "operation.cancelled")
        self.client.media_urls.assert_not_called()

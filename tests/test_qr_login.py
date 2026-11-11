import subprocess
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from live_viewer.core import ViewerError
from live_viewer.qr_login import LoginHttp, QrLogin, login_url, qr_png


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.session = requests.Session()
        self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        return response() if callable(response) else response
    def close(self): self.session.close()


class QrFlowTests(unittest.TestCase):
    def test_generate_png_and_bili_wait_scan_expiry_states(self):
        http = FakeHttp([{"code": 0, "data": {"url": "https://passport.bilibili.com/h5/scan?key=TEST", "qrcode_key": "SYNTHETIC_KEY"}},
                         {"code": 0, "data": {"code": 86101}}, {"code": 0, "data": {"code": 86090}},
                         {"code": 0, "data": {"code": 86038}}])
        flow = QrLogin("bilibili", http)
        challenge = flow.create()
        self.assertTrue(challenge.png.startswith(b"\x89PNG"))
        self.assertNotIn("SYNTHETIC_KEY", repr(challenge))
        self.assertEqual([flow.poll().state for _ in range(3)], ["waiting", "scanned", "expired"])

    def test_success_returns_only_platform_cookies_and_requires_login_cookie(self):
        http = FakeHttp([{"code": 0, "data": {"code": 0}}])
        http.session.cookies.set("SESSDATA", "SYNTHETIC_SECRET", domain=".bilibili.com", secure=True)
        http.session.cookies.set("other", "OTHER_SECRET", domain=".example.com")
        flow = QrLogin("bilibili", http);flow.code = "SYNTHETIC_KEY"
        result = flow.poll()
        self.assertEqual(result.state, "success")
        self.assertEqual(len(result.cookies), 1)
        self.assertNotIn("SYNTHETIC_SECRET", repr(result))
        result.cookies.clear()
        self.assertTrue(http.session.cookies.get("SESSDATA"))
        empty = QrLogin("bilibili", FakeHttp([{"code": 0, "data": {"code": 0}}]))
        empty.code = "TEST"
        with self.assertRaises(ViewerError): empty.poll()

    def test_cancel_during_confirmation_never_imports_late_login(self):
        flow = QrLogin("bilibili", FakeHttp([]));flow.code = "TEST"
        def confirm():
            flow.http.session.cookies.set("SESSDATA", "SYNTHETIC_SECRET", domain=".bilibili.com")
            flow.cancel()
            return {"code": 0, "data": {"code": 0}}
        flow.http.responses.append(confirm)
        result = flow.poll()
        self.assertEqual(result.state, "cancelled")
        self.assertIsNone(result.cookies)

    def test_douyu_wait_scan_expire_and_jsonp_completion(self):
        http = FakeHttp([{"error": 0, "data": {"url": "https://m.douyu.com/scan?code=TEST", "code": "TEST", "expire": 120}},
                         {"error": -2}, {"error": 1}, {"error": -1},
                         {"error": 0, "data": {"url": "https://www.douyu.com/member/login/scan?token=SECRET"}},
                         {"error": 0}])
        flow = QrLogin("douyu", http)
        self.assertEqual(flow.create().expires_seconds, 120)
        self.assertEqual([flow.poll().state for _ in range(3)], ["waiting", "scanned", "expired"])
        http.session.cookies.set("acf_auth", "SYNTHETIC_SECRET", domain=".douyu.com")
        self.assertEqual(flow.poll().state, "success")
        self.assertIn("callback=appClient_json_callback", http.calls[-1][1])

    def test_unknown_state_does_not_echo_platform_messages(self):
        flow = QrLogin("bilibili", FakeHttp([{"code": 0, "data": {"code": 123, "message": "SECRET"}}]))
        flow.code = "TEST"
        with self.assertRaises(ViewerError) as caught: flow.poll()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_douyu_cancel_releases_old_qr_on_platform(self):
        http = FakeHttp([{"error": 0}]);flow = QrLogin("douyu", http);flow.code = "TEST"
        flow.cancel();flow.close()
        self.assertTrue(http.calls[0][1].endswith("/scan/deleteCode"))
        self.assertIsNone(flow.code)


class QrTransportTests(unittest.TestCase):
    def test_windows_tls_fallback_keeps_secrets_on_stdin_and_collects_cookie(self):
        client = LoginHttp("bilibili")
        client.session.cookies.set("prior", "OLD_SECRET", domain=".bilibili.com")
        response = b"HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 200\r\nSet-Cookie: SESSDATA=SYNTHETIC_SECRET; Domain=.bilibili.com; Path=/; Secure; HttpOnly\r\n\r\n{\"code\":0}"
        with patch.object(client.session, "request", side_effect=requests.exceptions.SSLError("SECRET")), \
             patch.object(Path, "is_file", return_value=True), \
             patch("live_viewer.qr_login.subprocess.run", return_value=types.SimpleNamespace(returncode=0, stdout=response)) as run:
            result = client.request("GET", "https://passport.bilibili.com/poll?key=QR_SECRET")
            self.assertEqual(result["code"], 0)
            command = run.call_args.args[0]
            self.assertFalse(any("SECRET" in arg for arg in command))
            self.assertNotIn("--insecure", command)
            self.assertIn(b"OLD_SECRET", run.call_args.kwargs["input"])
            self.assertEqual(client.session.cookies.get("SESSDATA"), "SYNTHETIC_SECRET")

    def test_redirects_and_qr_urls_cannot_leave_platform_https(self):
        login_url("https://m.douyu.com/qr", "douyu")
        for url in ["http://passport.bilibili.com/qr", "https://passport.bilibili.com.evil.example/qr",
                    "https://user:SECRET@passport.bilibili.com/qr", "https://passport.bilibili.com:123/qr"]:
            with self.assertRaises(ViewerError): login_url(url, "bilibili")
        client = LoginHttp("bilibili")
        client.windows_tls = True
        with patch.object(client, "_curl", return_value=(302, {"Location": "https://evil.example/SECRET"}, b"")) as curl:
            with self.assertRaises(ViewerError) as caught: client.request("GET", "https://passport.bilibili.com/qr")
            self.assertEqual(curl.call_count, 1)
            self.assertNotIn("SECRET", str(caught.exception))

    def test_jsonp_is_parsed_without_executing_scripts(self):
        client = LoginHttp("douyu");client.windows_tls = True
        with patch.object(client, "_curl", return_value=(200, {}, b'appClient_json_callback({"error":0});')):
            self.assertEqual(client.request("GET", "https://www.douyu.com/callback")["error"], 0)
        with patch.object(client, "_curl", return_value=(200, {}, b'appClient_json_callback({"error":0});alert("SECRET");')):
            with self.assertRaises(ViewerError) as caught: client.request("GET", "https://www.douyu.com/callback")
            self.assertNotIn("SECRET", str(caught.exception))


if __name__ == "__main__": unittest.main()

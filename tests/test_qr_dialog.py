import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from live_viewer.app import ViewerApp
from live_viewer.qr_dialog import QrLoginDialog
from live_viewer.qr_login import Challenge, LoginState, qr_png


class FakeFlow:
    def __init__(self, platform, *, network_mode="direct"):
        self.cancelled = threading.Event()
    def create(self): return Challenge(qr_png("https://example.invalid/demo"), 180)
    def poll(self): return LoginState("waiting")
    def cancel(self): self.cancelled.set()
    def close(self): pass


@unittest.skipUnless(os.name == "nt", "Windows desktop required")
class DialogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = tk.Tk();self.root.withdraw()
        self.app = ViewerApp(self.root, Path(self.temporary.name))
        self.dialog = None

    def tearDown(self):
        if self.dialog is not None: self.dialog.close()
        if not self.app.closed: self.app.close()
        self.temporary.cleanup()

    def pump(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            self.root.update();time.sleep(0.01)
        self.assertTrue(condition())

    def test_refresh_ignores_success_from_old_generation(self):
        success = Mock()
        with patch("live_viewer.qr_dialog.QrLogin", FakeFlow):
            self.dialog = QrLoginDialog(self.root, "bilibili", success, lambda: None)
            old_generation, old_flow = self.dialog.generation, self.dialog.flow
            self.dialog.refresh()
            self.dialog.events.put((old_generation, "success", requests.cookies.RequestsCookieJar()))
            self.pump(lambda: self.dialog.photo is not None)
            self.assertTrue(old_flow.cancelled.is_set())
            success.assert_not_called()

    def test_closing_dialog_cancels_flow_without_replacing_existing_cookie(self):
        jar = requests.cookies.RequestsCookieJar();jar.set("SESSDATA", "SYNTHETIC_SECRET", domain=".bilibili.com")
        self.app.cookies["bilibili"] = jar
        with patch("live_viewer.qr_dialog.QrLogin", FakeFlow):
            self.app.qr_login();self.dialog = self.app.qr_dialog
            flow = self.dialog.flow
            self.dialog.close()
            self.assertTrue(flow.cancelled.is_set())
            self.assertIsNone(self.app.qr_dialog)
            self.assertIs(self.app.cookies["bilibili"], jar)

    def test_success_is_delivered_once_and_closes_the_popup(self):
        jar = requests.cookies.RequestsCookieJar();jar.set("SESSDATA", "SYNTHETIC_SECRET", domain=".bilibili.com")
        class SuccessfulFlow(FakeFlow):
            def poll(self): return LoginState("success", jar)
        success = Mock()
        with patch("live_viewer.qr_dialog.QrLogin", SuccessfulFlow):
            self.dialog = QrLoginDialog(self.root, "bilibili", success, lambda: None)
            self.pump(lambda: self.dialog.closed)
            success.assert_called_once_with(jar)

    def test_app_receives_qr_cookie_and_validates_login_without_persisting_by_default(self):
        jar = requests.cookies.RequestsCookieJar();jar.set("SESSDATA", "SYNTHETIC_SECRET", domain=".bilibili.com")
        class SuccessfulFlow(FakeFlow):
            def poll(self): return LoginState("success", jar)
        with patch("live_viewer.qr_dialog.QrLogin", SuccessfulFlow), \
             patch("live_viewer.app.PlatformClient.login_status", return_value={"verified": True, "message": "valid"}):
            self.app.qr_login();self.dialog = self.app.qr_dialog
            self.pump(lambda: "bilibili" in self.app.cookies and not self.app.busy)
            self.assertEqual(self.app.mode.get(), "导入 Cookie")
            self.assertIsNone(self.app.qr_dialog)
            self.assertFalse((Path(self.temporary.name) / "credentials" / "bilibili.dpapi").exists())


if __name__ == "__main__": unittest.main()

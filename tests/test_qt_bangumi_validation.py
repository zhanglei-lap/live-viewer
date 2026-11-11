import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
import requests
from live_viewer.qt.bangumi_validation import ValidationWindow


class ValidationQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.window = ValidationWindow(SimpleNamespace(player="unused.exe", data_dir=root, report=root / "report.json",
            full_episode=False, proxy_configured=False, show_login=False, auto_member=False))

    def tearDown(self):
        self.window.stop()
        if self.window.worker:
            self.window.worker.join(3)
        self.window.drain()
        self.window.close()
        self.temp.cleanup()

    def test_account_selection_is_explicit_and_has_no_login_fallback(self):
        self.window.session_choice.setCurrentIndex(1)
        with patch("live_viewer.qt.bangumi_validation.run_native") as run:
            self.window.play_current()
            run.assert_not_called()
        self.assertIn("扫码", self.window.status.text())

    def test_reports_never_include_in_memory_cookie_and_human_checks_are_not_inferred(self):
        jar = requests.cookies.RequestsCookieJar()
        jar.set("SESSDATA", "fixture-private-cookie", domain=".bilibili.com")
        self.window.jar = jar
        self.window.save()
        text = self.window.args.report.read_text(encoding="utf-8")
        self.assertNotIn("fixture-private-cookie", text)
        report = json.loads(text)
        self.assertEqual(report["human_checks"]["picture_audio_sync"], "unverified")
        self.assertEqual(report["network"]["d3_mainland_without_vpn"], "unverified")
        self.assertEqual(report["gate"], "pending")

    def test_close_cancels_and_waits_for_cleanup_without_starting_another_worker(self):
        started, completed = threading.Event(), threading.Event()
        def task(cancel):
            started.set()
            cancel.wait(2)
            completed.set()
        self.window.work(task)
        self.assertTrue(started.wait(1))
        self.window.close()
        self.window.worker.join(2)
        self.window.drain()
        self.assertTrue(completed.is_set())
        self.assertTrue(self.window.cancel.is_set())
        self.assertFalse(self.window.timer.isActive())

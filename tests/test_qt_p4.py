import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from live_viewer.qt.app import ViewerWindow
    from live_viewer.qt.qr_dialog import QrLoginDialog
    from live_viewer.core import cookies_from_text, ViewerError, Quality
    from live_viewer.qr_login import Challenge, LoginState, qr_png
except ImportError:
    QApplication = None


class WaitingQr:
    def __init__(self, *_, **__):
        self.cancelled = threading.Event()
    def create(self):
        return Challenge(qr_png("https://passport.bilibili.com/synthetic-test"), 180)
    def poll(self):
        self.cancelled.wait(2)
        return LoginState("cancelled")
    def cancel(self):
        self.cancelled.set()
    def close(self):
        pass


@unittest.skipIf(QApplication is None, "Qt dependencies required")
class QtP4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.window = ViewerWindow(self.directory)
        self.dialogs = []
        self.other_windows = []
        self.jar = cookies_from_text("SESSDATA=SYNTHETIC-P4-SECRET", "bilibili")

    def pump(self, condition, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.application.processEvents()
            self.window.tick()
            if condition():
                return
            time.sleep(.01)
        self.fail("condition timeout")

    def tearDown(self):
        for dialog in self.dialogs:
            if not dialog.closed:
                dialog.close()
        for window in self.other_windows + [self.window]:
            window.close()
            self.pump(lambda: window.closed)
        self.temporary.cleanup()

    def qr(self, on_success):
        events = []
        dialog = QrLoginDialog(self.window, "bilibili", on_success, lambda: None, flow_factory=WaitingQr,
                              on_event=lambda code, message, **kw: events.append((code, message)))
        dialog.show()
        self.dialogs.append(dialog)
        return dialog, events

    def test_refresh_ignores_queued_old_confirmation(self):
        accepted = []
        dialog, events = self.qr(accepted.append)
        generation = dialog.generation
        dialog.queue.put((generation, "success", self.jar))
        dialog.refresh()
        dialog.poll()
        self.assertEqual(accepted, [])
        self.assertTrue(any(code == "account.qr_stale" for code, _ in events))
        self.assertFalse(dialog.closed)

    def test_closed_qr_never_imports_late_confirmation(self):
        accepted = []
        dialog, _ = self.qr(accepted.append)
        dialog.queue.put((dialog.generation, "success", self.jar))
        dialog.close()
        dialog.poll()
        self.assertEqual(accepted, [])
        self.assertTrue(dialog.queue.empty())

    def test_current_qr_success_goes_through_account_verification(self):
        window = self.window
        with patch("live_viewer.qt.app.verify_login", return_value={"verified": True, "message": "登录有效"}):
            dialog, _ = self.qr(lambda jar: window._import_login("bilibili", lambda: jar))
            dialog.queue.put((dialog.generation, "success", self.jar))
            dialog.poll()
            self.pump(lambda: "bilibili" in window.cookies)
        self.assertTrue(dialog.closed)
        self.assertEqual(window.cookies["bilibili"].get("SESSDATA"), "SYNTHETIC-P4-SECRET")

    def test_clear_during_verification_rejects_late_account(self):
        window = self.window
        entered, release = threading.Event(), threading.Event()
        def verify(*_, **__):
            entered.set()
            release.wait(2)
            return {"verified": True, "message": "有效"}
        with patch("live_viewer.qt.app.verify_login", side_effect=verify):
            window._import_login("bilibili", lambda: self.jar)
            self.assertTrue(entered.wait(1))
            window.clear_login("bilibili")
            release.set()
            self.pump(lambda: not window.runtime.jobs.pending)
        self.assertNotIn("bilibili", window.cookies)
        self.assertFalse(window.config["remember_login"]["bilibili"])

    def test_invalid_new_import_does_not_replace_existing_account(self):
        window = self.window
        window.cookies["bilibili"] = self.jar
        with patch("live_viewer.qt.app.verify_login", side_effect=ViewerError("已过期", code="account.expired", level="WARN")):
            window._import_login("bilibili", lambda: cookies_from_text("SESSDATA=invalid-new", "bilibili"))
            self.pump(lambda: "account.bilibili" not in window.runtime.jobs.pending)
        self.assertIs(window.cookies["bilibili"], self.jar)
        self.assertEqual(window.activity.notice_for("account.bilibili").code, "account.expired")

    def test_remembered_import_is_encrypted_and_restore_uses_the_new_jar(self):
        window = self.window
        window.remember_boxes["bilibili"].setChecked(True)
        with patch("live_viewer.qt.app.verify_login", return_value={"verified": True, "message": "有效"}):
            window._import_login("bilibili", lambda: self.jar)
            self.pump(lambda: not window.runtime.jobs.pending)
        path = window.credentials.directory / "bilibili.dpapi"
        self.assertTrue(path.exists())
        self.assertNotIn(b"SYNTHETIC-P4-SECRET", path.read_bytes())
        self.assertNotIn("SYNTHETIC-P4-SECRET", window.store.path.read_text(encoding="utf-8"))
        other = ViewerWindow(self.directory)
        self.other_windows.append(other)
        self.pump(lambda: (other.tick(), "bilibili" in other.cookies)[1])
        self.assertEqual(other.cookies["bilibili"].get("SESSDATA"), "SYNTHETIC-P4-SECRET")

    def test_disable_remember_deletes_disk_login_but_keeps_current_session(self):
        window = self.window
        window.cookies["bilibili"] = self.jar
        window.remember_boxes["bilibili"].setChecked(True)
        self.pump(lambda: not window.runtime.jobs.pending)
        window.remember_boxes["bilibili"].setChecked(False)
        self.pump(lambda: not window.runtime.jobs.pending)
        self.assertIn("bilibili", window.cookies)
        self.assertFalse((window.credentials.directory / "bilibili.dpapi").exists())

    def test_remember_reenabled_during_restore_keeps_the_restored_login_on_disk(self):
        window = self.window
        entered, release = threading.Event(), threading.Event()
        def load(_):
            entered.set();release.wait(2)
            return self.jar
        window.remember_boxes["bilibili"].setChecked(True)
        with patch.object(window.credentials, "load", side_effect=load):
            window.restore_login("bilibili")
            self.assertTrue(entered.wait(1))
            window.remember_boxes["bilibili"].setChecked(False)
            window.remember_boxes["bilibili"].setChecked(True)
            release.set()
            self.pump(lambda: not window.runtime.jobs.pending and "bilibili" in window.cookies)
        self.assertTrue(window.config["remember_login"]["bilibili"])
        self.assertEqual(window.credentials.load("bilibili").get("SESSDATA"), "SYNTHETIC-P4-SECRET")

    def test_memory_session_remains_usable_when_encrypted_save_fails(self):
        window = self.window
        window.cookies["bilibili"] = self.jar
        with patch.object(window.credentials, "save", side_effect=ViewerError("保存失败")):
            window.remember_boxes["bilibili"].setChecked(True)
            self.pump(lambda: not window.runtime.jobs.pending)
        self.assertIn("bilibili", window.cookies)
        self.assertFalse(window.remember_boxes["bilibili"].isChecked())
        self.assertIsNotNone(window.activity.notice_for("account.persist.bilibili"))

    def test_cancelled_diagnostic_result_does_not_update_ui(self):
        window = self.window
        entered, release = threading.Event(), threading.Event()
        def check(*_, **__):
            entered.set();release.wait(2)
            return {"platform": "bilibili", "room_input": "35", "stage": "完成", "status": "旧结果"}
        with patch("live_viewer.qt.app.connection_check", side_effect=check):
            window.check_connection()
            self.assertTrue(entered.wait(1))
            window.cancel_diagnostics()
            release.set()
            self.application.processEvents()
            window.tick()
        self.assertIsNone(window.connection_result)
        self.assertIn("已取消", window.diagnostic_status.text())

    def test_comparison_results_cannot_change_different_edited_room_quality(self):
        window = self.window
        window.choose_room({"platform": "douyu", "room": "6657", "name": "first"})
        entered, release = threading.Event(), threading.Event()
        def compare(*_, **__):
            entered.set();release.wait(2)
            return ([{"session": "匿名", "actual_name": "原画", "status": "成功"}], [Quality(0, "原画"), Quality(1, "高清")])
        with patch("live_viewer.qt.app.compare_quality", side_effect=compare):
            window.compare()
            self.assertTrue(entered.wait(1))
            window.choose_room({"platform": "bilibili", "room": "35", "name": "second"})
            release.set()
            self.pump(lambda: not window.runtime.jobs.pending)
        self.assertEqual(window.quality_box.count(), 1)
        self.assertEqual(window.comparison_request["room_input"], "6657")
        window.choose_room({"platform": "douyu", "room": "6657", "name": "first"})
        self.assertGreaterEqual(window.quality_box.findData(1), 0)

    def test_diagnostic_success_does_not_clear_playback_error(self):
        window = self.window
        token = window.runtime.controller.begin({"platform": "douyu", "room_input": "6657"})
        window.runtime.controller.fail(token, ViewerError("播放失败", code="network.tls_failed"))
        with patch("live_viewer.qt.app.connection_check", return_value={"platform": "bilibili", "room_input": "35",
                   "stage": "完成", "status": "检查成功"}):
            window.check_connection()
            self.pump(lambda: not window.runtime.jobs.pending)
        self.assertEqual(window.activity.notice_for("playback").code, "network.tls_failed")

    def test_qt_export_report_is_safe_and_finishes_in_background(self):
        window = self.window
        window.note.setText("Cookie: SYNTHETIC-P4-SECRET")
        window.connection_result = {"platform": "bilibili", "room_input": "35", "status": "Cookie: SYNTHETIC-P4-SECRET"}
        path = self.directory / "report.json"
        with patch("live_viewer.qt.app.QFileDialog.getSaveFileName", return_value=(str(path), "JSON")):
            window.export_report()
        self.pump(lambda: window.report_button.isEnabled())
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("SYNTHETIC-P4-SECRET", text)
        from live_viewer.qt.app import VERSION
        self.assertEqual(json.loads(text)["app_version"], VERSION)

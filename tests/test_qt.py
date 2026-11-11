"""Qt boundary tests use disposable data and never change the user's clipboard."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from live_viewer.qt.app import ViewerWindow
    from live_viewer.qt.logs import LogModel
    from live_viewer.core import ViewerError, cookies_from_text
    from live_viewer.events import Event
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "Qt tests require requirements-qt.txt")
class QtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        cls.application.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.window = ViewerWindow(self.directory)
        self.foreground_patch = patch("live_viewer.qt.notifications.foreground", return_value=True)
        self.foreground_patch.start()

    def pump(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.application.processEvents()
            self.window.tick()
            if condition():
                return
            time.sleep(.01)
        self.fail("condition timeout")

    def tearDown(self):
        self.window.close()
        self.pump(lambda: self.window.closed)
        self.temporary.cleanup()
        self.foreground_patch.stop()

    def fail_playback(self):
        token = self.window.runtime.controller.begin({"platform": "douyu", "room_input": "6657"})
        self.window.runtime.controller.fail(token, ViewerError("连接失败", code="network.tls_failed"))
        self.window.tick()
        return token

    def test_async_ui_export_uses_snapshot_of_filtered_events(self):
        window = self.window
        window.activity.emit("diagnostic.test", "Cookie: private-cookie", source="diagnostic", level="ERROR",
                             target="douyu:6657", details={"host": "example.com", "cookie": "private-cookie"})
        window.tick()
        window.log_search.setText("diagnostic.test")
        path = self.directory / "export.json"
        with patch("live_viewer.qt.app.QFileDialog.getSaveFileName", return_value=(str(path), "JSON")):
            window.export_logs()
        self.pump(lambda: window.export_button.isEnabled())
        result = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["code"], "diagnostic.test")
        self.assertNotIn("private-cookie", path.read_text(encoding="utf-8"))

    def test_export_failure_is_visible_and_does_not_change_playback_fault(self):
        window = self.window
        self.fail_playback()
        with patch("live_viewer.qt.app.QFileDialog.getSaveFileName", return_value=(str(self.directory), "JSON")):
            window.export_logs()
        self.pump(lambda: window.export_button.isEnabled())
        self.assertEqual(window.activity.notice_for("playback").code, "network.tls_failed")
        self.assertTrue(any(r["code"] == "log.export_failed" for r in window.log_model.rows))

    def test_copy_selected_details_is_redacted(self):
        window = self.window
        window.activity.emit("diagnostic.copy", "SESSDATA=private-secret", source="diagnostic")
        window.tick()
        window.log_search.setText("diagnostic.copy")
        window.log_table.selectRow(0)
        clipboard = Mock()
        with patch("live_viewer.qt.app.QApplication.clipboard", return_value=clipboard):
            window.copy_log()
        copied = clipboard.setText.call_args.args[0]
        self.assertNotIn("private-secret", copied)
        self.assertIn("diagnostic.copy", copied)

    def test_error_reminder_stays_until_confirmed_or_resolved(self):
        window = self.window
        token = self.fail_playback()
        dialog = window.strong_dialog
        window.runtime.controller.fail(token, ViewerError("再次连接失败", code="network.timeout"))
        window.tick()
        self.assertIs(window.strong_dialog, dialog)
        self.assertEqual(window.notification_count, 1)
        window.close_strong_notice()
        self.assertIsNotNone(window.activity.notice_for("playback"))
        window.runtime.controller.recovered(token)
        window.tick()
        self.assertIsNone(window.notice_event)

    def test_room_name_is_plain_text_and_unique_key_survives_rename(self):
        window = self.window
        window.choose_room({"platform": "douyu", "room": "6657", "name": "old"})
        window.room_name.setText("<b>新的名称</b>")
        window.save_room()
        window.room_name.setText("another name")
        window.save_room()
        matching = [r for r in window.config["favorites"] if r["platform"] == "douyu" and r["room"] == "6657"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["name"], "another name")

    def test_import_scoped_cookie_does_not_persist_cleartext(self):
        window = self.window
        with patch("live_viewer.qt.app.verify_login", return_value={"verified": True, "message": "已验证测试登录"}):
            window._import_login("bilibili", lambda: cookies_from_text("SESSDATA=test-secret", "bilibili"))
            self.pump(lambda: "bilibili" in window.cookies)
        window.save_settings()
        self.assertNotIn("test-secret", window.store.path.read_text(encoding="utf-8"))
        self.assertFalse(window.credentials.directory.exists())
        window.tick()
        self.assertNotIn("test-secret", json.dumps(window.log_model.rows))

    def test_virtual_log_model_is_bounded_and_ignores_duplicate_and_bad_records(self):
        records = [Event("diagnostic.row", "entry").record() for _ in range(2003)]
        model = LogModel(records)
        self.assertEqual(model.rowCount(), 2000)
        model.append([records[-1], {"bad": "record"}])
        self.assertEqual(model.rowCount(), 2000)
        self.assertEqual(model.rows[-1]["event_id"], records[-1]["event_id"])

    def test_source_change_notice_is_cleared_on_next_manual_session(self):
        window = self.window
        window.activity.emit("player.source_changed", "已打开其他内容。", source="player",
                             level="WARN", presentation="banner", scope="player")
        window.tick()
        self.assertIsNotNone(window.notice_event)
        window.activity.clear("player")
        window.tick()
        self.assertIsNone(window.notice_event)

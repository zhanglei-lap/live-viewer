import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from live_viewer.events import Event
    from live_viewer.qt.app import ViewerWindow
except ImportError:
    QApplication = None


@unittest.skipIf(QApplication is None, "Qt required")
class NoticeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.window = ViewerWindow(Path(self.temporary.name))

    def tearDown(self):
        self.window.close()
        deadline = time.monotonic() + 5
        while not self.window.closed and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.01)
        self.temporary.cleanup()
    def test_background_fault_has_unread_record_without_a_modal_window(self):
        window = self.window
        with patch("live_viewer.qt.notifications.foreground", return_value=False):
            event = Event("test.background_error", "后台故障", level="ERROR", presentation="strong")
            self.assertFalse(window.notices.accept(event, background_enabled=False))
            self.assertEqual(len(window.notices.unread), 1)
            self.assertIsNone(window.strong_dialog)
            window.notices.accept(event, background_enabled=False)
            self.assertEqual(len(window.notices.unread), 1)
        window.notices.acknowledge()
        self.assertEqual(window.notices.unread, [])

    def test_os_display_callback_is_recorded_separately_from_request(self):
        self.window.notices.shown()
        self.assertEqual(self.window.notices.os_shown, 1)
        self.assertTrue(any(e.code == "notice.os_display_reported" for e in self.window.activity.history))

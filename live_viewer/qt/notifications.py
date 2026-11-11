"""Background notices retain an unread record even when Windows suppresses toast."""
import ctypes
from ctypes import wintypes as W
import os
from PySide6.QtCore import QAbstractNativeEventFilter, QTimer
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from .widgets import line_icon


def foreground(window):
    return window.isActiveWindow() and not window.isMinimized()


def notification_state():
    if os.name != "nt": return None
    value = ctypes.c_int()
    shell = ctypes.WinDLL("shell32")
    return value.value if shell.SHQueryUserNotificationState(ctypes.byref(value)) == 0 else None


class WindowsNoticeObserver(QAbstractNativeEventFilter):
    def __init__(self, callback):
        super().__init__()
        self.callback = callback
        self.hwnd = self.old_proc = self.proc = None

    @staticmethod
    def windows():
        if os.name != "nt": return set()
        user = ctypes.WinDLL("user32")
        enum = ctypes.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        user.EnumWindows.argtypes = [enum, W.LPARAM]
        user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
        user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, ctypes.c_int]
        found = set()
        @enum
        def visit(hwnd, _):
            pid = W.DWORD();user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid():
                title = ctypes.create_unicode_buffer(256);user.GetWindowTextW(hwnd, title, 256)
                if title.value == "QTrayIconMessageWindow": found.add(hwnd)
            return True
        user.EnumWindows(visit, 0)
        return found

    def attach(self, hwnd):
        if os.name != "nt": return
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        self.user.SetWindowLongPtrW.argtypes = [W.HWND, ctypes.c_int, ctypes.c_void_p]
        self.user.SetWindowLongPtrW.restype = ctypes.c_void_p
        self.user.CallWindowProcW.argtypes = [ctypes.c_void_p, W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        self.user.CallWindowProcW.restype = ctypes.c_ssize_t
        self.hwnd = hwnd
        @proc_type
        def receive(window, message, wp, lp):
            if message == 0x8000 + 101 and lp & 0xffff == 0x402:
                QTimer.singleShot(0, self.callback)
            return self.user.CallWindowProcW(self.old_proc, window, message, wp, lp)
        self.proc = receive
        self.old_proc = self.user.SetWindowLongPtrW(hwnd, -4, ctypes.cast(receive, ctypes.c_void_p))

    def detach(self):
        if self.hwnd is not None and self.old_proc:
            self.user.IsWindow.argtypes = [W.HWND]
            if self.user.IsWindow(self.hwnd):
                self.user.SetWindowLongPtrW(self.hwnd, -4, self.old_proc)
            self.hwnd = None

    def nativeEventFilter(self, event_type, message):
        if os.name == "nt" and event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            value = ctypes.cast(int(message), ctypes.POINTER(W.MSG)).contents
            # Qt 6.12's tray callback: WM_APP+101. Only our own HWND is observed.
            if value.message == 0x8000 + 101 and (value.lParam & 0xffff) == 0x402:
                user = ctypes.WinDLL("user32")
                user.GetWindowThreadProcessId.argtypes = [W.HWND, ctypes.POINTER(W.DWORD)]
                pid = W.DWORD();user.GetWindowThreadProcessId(value.hwnd, ctypes.byref(pid))
                if pid.value == os.getpid():
                    QTimer.singleShot(0, self.callback)
        return False


class NoticeManager:
    def __init__(self, window, events):
        self.window, self.events = window, events
        self.unread = []
        self.requests = self.os_shown = 0
        self.tray = None
        self.observer = WindowsNoticeObserver(self.shown)
        QApplication.instance().installNativeEventFilter(self.observer)
        if QSystemTrayIcon.isSystemTrayAvailable():
            existing_trays = WindowsNoticeObserver.windows()
            self.tray = QSystemTrayIcon(line_icon("play", "#386cf4"), window)
            self.tray.setToolTip("本地直播")
            menu = QMenu(window)
            menu.addAction("打开本地直播", self.open)
            menu.addAction("查看活动日志", lambda: (self.open(), window.go_page(3)))
            menu.addSeparator()
            menu.addAction("退出", window.close)
            self.tray.setContextMenu(menu)
            self.tray.messageClicked.connect(self.open)
            self.tray.activated.connect(lambda reason: self.open() if reason in (
                QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
            self.tray.show()
            created = WindowsNoticeObserver.windows() - existing_trays
            if created: self.observer.attach(next(iter(created)))

    def accept(self, event, *, background_enabled=True):
        if event.event_id not in {e.event_id for e in self.unread}:
            self.unread.append(event)
            self.unread = self.unread[-50:]
        if foreground(self.window): return True
        if background_enabled and self.tray is not None:
            self.requests += 1
            self.tray.showMessage("本地直播 · 需要处理", event.message,
                                  QSystemTrayIcon.MessageIcon.Critical, 10000)
            state = notification_state()
            self.events.emit("notice.background_requested", "已请求后台提醒；未读告示会保留。",
                             source="notice", level="DEBUG", target=event.target,
                             details={"reason": "windows_state_" + str(state)})
        return False

    def shown(self):
        self.os_shown += 1
        self.events.emit("notice.os_display_reported", "Windows 已报告提醒显示。",
                         source="notice", level="DEBUG")

    def acknowledge(self):
        self.unread.clear()
        self.events.emit("notice.read", "未读提醒已标为已读；仍存在的故障告示继续保留。", source="notice")

    def open(self):
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        self.acknowledge()
        if self.window.notice_event is not None and self.window.config["ui"]["strong_notifications"]:
            self.window.show_strong_notice(self.window.notice_event)

    def close(self):
        self.observer.detach()
        if self.tray is not None: self.tray.hide()
        QApplication.instance().removeNativeEventFilter(self.observer)

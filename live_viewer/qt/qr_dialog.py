"""Nonmodal Qt QR dialog. Challenges and cookies remain in memory."""
from __future__ import annotations

import queue
import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QVBoxLayout

from ..playback import safe_error
from ..qr_login import QrLogin
from ..settings import PLATFORMS
from .widgets import button, row, text


class QrLoginDialog(QDialog):
    def __init__(self, parent, platform, on_success, on_close, *, network_mode="direct",
                 on_event=lambda *a, **k: None, flow_factory=QrLogin):
        super().__init__(parent)
        self.platform, self.on_success, self.on_close = platform, on_success, on_close
        self.on_event, self.flow_factory = on_event, flow_factory
        self.network_mode = network_mode
        self.closed = False
        self.generation = 0
        self.flow = None
        self.queue = queue.Queue()
        self.deadline = self.last_state = None
        self.reason = "cancelled"
        caption = next(k for k, v in PLATFORMS.items() if v == platform)
        self.setWindowTitle(caption + " · 扫码登录")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(440)
        self.resize(480, min(590, self.screen().availableGeometry().height() - 40))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(text("使用" + caption + "手机 App 扫一扫", "SectionTitle", True))
        layout.addWidget(text("扫描后在手机上确认登录，账号会自动载入本程序。", "Muted", True))
        self.image_label = text("正在生成二维码…", "StatusContext", True)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(260)
        self.image_label.setObjectName("QrImage")
        layout.addWidget(self.image_label, 1)
        self.status = text("正在连接平台…", "StatusContext", True)
        self.countdown = text("", "Muted")
        layout.addWidget(self.status)
        layout.addWidget(self.countdown)
        self.refresh_button = button("刷新二维码", self.refresh, "Primary")
        layout.addLayout(row(self.refresh_button, button("取消", self.close)))
        layout.addWidget(text("二维码不写入磁盘；是否保存登录由账号页的登录记忆选项决定。", "Caption", True))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(150)
        self.refresh()

    def emit(self, code, message, **fields):
        self.on_event(code, message, **fields)

    def refresh(self):
        if self.closed:
            return
        if self.flow is not None:
            self.flow.cancel()
        self.generation += 1
        generation = self.generation
        self.deadline = self.last_state = None
        self.image_label.clear()
        self.image_label.setText("正在生成二维码…")
        self.status.setText("正在连接平台…")
        self.countdown.clear()
        self.emit("account.qr_refreshed", "正在生成新的登录二维码。")
        try:
            flow = self.flow_factory(self.platform, network_mode=self.network_mode)
            self.flow = flow
        except Exception as exc:
            self.handle("error", safe_error(exc))
            return

        def emit(kind, payload=None):
            if not self.closed and not flow.cancelled.is_set():
                self.queue.put((generation, kind, payload))

        def work():
            try:
                challenge = flow.create()
                emit("image", challenge)
                deadline = time.monotonic() + challenge.expires_seconds
                while not flow.cancelled.is_set() and time.monotonic() < deadline:
                    result = flow.poll()
                    emit(result.state, result.cookies)
                    if result.state in ("success", "expired", "cancelled"):
                        return
                    if flow.cancelled.wait(2):
                        return
                if not flow.cancelled.is_set():
                    emit("expired")
            except Exception as exc:
                emit("error", safe_error(exc))
            finally:
                flow.close()
        threading.Thread(target=work, name="qr-" + self.platform, daemon=True).start()

    def poll(self):
        if self.closed:
            return
        while True:
            try:
                generation, kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                self.emit("account.qr_stale", "已忽略刷新前的扫码结果。", level="DEBUG")
                continue
            self.handle(kind, payload)
            if self.closed:
                return
        if self.deadline is not None:
            remaining = max(0, int(self.deadline - time.monotonic()))
            self.countdown.setText(f"约 {remaining} 秒后过期")

    def handle(self, kind, payload=None):
        labels = {"image": "登录二维码已生成。", "waiting": "等待扫码。", "scanned": "已扫码，等待手机确认。",
                  "success": "扫码已确认，正在验证登录。", "expired": "二维码已过期，请刷新。",
                  "error": "扫码登录失败，请刷新或导入 Cookie。"}
        if kind != self.last_state and kind in labels:
            self.last_state = kind
            self.emit("account.qr_" + ("ready" if kind == "image" else kind), labels[kind],
                      level="WARN" if kind in ("expired", "error") else "INFO",
                      presentation="banner" if kind in ("error", "expired") else "log")
        if kind == "image":
            pixmap = QPixmap()
            if not pixmap.loadFromData(payload.png, "PNG"):
                self.handle("error", safe_error(None))
                return
            ratio = self.devicePixelRatioF()
            pixmap = pixmap.scaled(round(260 * ratio), round(260 * ratio),
                                    Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.FastTransformation)
            pixmap.setDevicePixelRatio(ratio)
            self.image_label.setPixmap(pixmap)
            self.deadline = time.monotonic() + payload.expires_seconds
        elif kind in ("waiting", "scanned"):
            self.status.setText("等待扫码，请在手机 App 打开扫一扫。" if kind == "waiting" else "已扫码，请在手机上确认登录。")
        elif kind == "success":
            self.reason = "completed"
            self.close()
            self.on_success(payload)
        elif kind in ("expired", "error"):
            self.deadline = None
            self.countdown.clear()
            self.image_label.clear()
            self.image_label.setText("二维码已失效" if kind == "expired" else "无法完成扫码登录")
            self.status.setText(labels[kind] if kind == "expired" or getattr(payload, "code", "") == "operation.unexpected" else str(payload))

    def closeEvent(self, event):
        if not self.closed:
            self.closed = True
            self.generation += 1
            self.timer.stop()
            if self.flow:
                self.flow.cancel()
            self.image_label.clear()
            self.deadline = None
            while not self.queue.empty():
                self.queue.get_nowait()
            self.emit("account.qr_closed", "扫码窗口已关闭。", details={"reason": self.reason})
            self.on_close()
        event.accept()

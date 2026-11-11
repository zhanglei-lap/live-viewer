from __future__ import annotations

import base64
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from .core import ViewerError
from .qr_login import QrLogin
from .settings import PLATFORMS


class QrLoginDialog:
    def __init__(self, parent, platform, on_success, on_close, *, network_mode="direct", on_event=None):
        self.platform, self.on_success, self.on_close = platform, on_success, on_close
        self.network_mode = network_mode
        self.on_event = on_event
        self.last_event_state = None
        self.closed = False
        self.generation = 0
        self.flow = None
        self.events = queue.Queue()
        self.photo = None
        self.deadline = None
        self.poll_after = None
        self.window = tk.Toplevel(parent)
        title = next(k for k, v in PLATFORMS.items() if v == platform)
        self.window.title(f"{title} · 扫码登录")
        self.window.geometry("440x570")
        self.window.resizable(False, False)
        self.window.transient(parent)
        frame = ttk.Frame(self.window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"使用{title}手机 App 扫一扫", font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(4, 10))
        ttk.Label(frame, text="扫描后在手机上确认登录，程序会自动接收登录态。", wraplength=395).pack()
        self.image_label = ttk.Label(frame, text="正在生成二维码…", anchor="center")
        self.image_label.pack(fill="both", expand=True, pady=12)
        self.status = tk.StringVar(value="正在连接平台…")
        ttk.Label(frame, textvariable=self.status, wraplength=395, anchor="center").pack(fill="x")
        self.countdown = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.countdown, foreground="#56616f").pack(pady=6)
        buttons = ttk.Frame(frame)
        buttons.pack(pady=8)
        self.refresh_button = ttk.Button(buttons, text="刷新二维码", command=self.refresh)
        self.refresh_button.pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭", command=self.close).pack(side="left", padx=8)
        ttk.Label(frame, text="二维码不写入磁盘；是否记住登录由账号页面的勾选项决定。", foreground="#56616f", wraplength=395).pack(pady=(4, 0))
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.grab_set()
        self.refresh()
        self.poll_after = self.window.after(150, self._poll)

    def refresh(self):
        if self.closed:
            return
        if self.flow is not None:
            self.flow.cancel()
        self.generation += 1
        generation = self.generation
        flow = QrLogin(self.platform, network_mode=self.network_mode)
        self.flow = flow
        self.deadline = None
        self.photo = None
        self.image_label.configure(image="", text="正在生成二维码…")
        self.status.set("正在连接平台…")
        self.countdown.set("")
        self.last_event_state = None
        self._emit("account.qr_refreshed", "正在生成新的登录二维码。")

        def emit(kind, payload=None):
            if not self.closed and not flow.cancelled.is_set():
                self.events.put((generation, kind, payload))

        def worker():
            try:
                challenge = flow.create()
                emit("image", challenge)
                deadline = time.monotonic() + challenge.expires_seconds
                while not flow.cancelled.is_set() and time.monotonic() < deadline:
                    result = flow.poll()
                    emit(result.state, result.cookies)
                    if result.state in ("success", "expired", "cancelled"):
                        return
                    if flow.cancelled.wait(2.0):
                        return
                if not flow.cancelled.is_set():
                    emit("expired")
            except ViewerError as exc:
                emit("error", str(exc))
            except Exception:
                emit("error", "扫码登录失败，请刷新二维码或改用 Cookie 导入。")
            finally:
                flow.close()
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        self.poll_after = None
        try:
            while True:
                generation, kind, payload = self.events.get_nowait()
                if generation != self.generation:
                    self._emit("account.qr_stale", "已忽略过期二维码的返回结果。", level="DEBUG")
                    continue
                if kind != self.last_event_state:
                    self.last_event_state = kind
                    labels = {"image": "登录二维码已生成。", "waiting": "等待扫码。",
                              "scanned": "已扫码，等待手机确认。", "success": "扫码已确认，正在验证登录。",
                              "expired": "二维码已过期，请刷新。", "error": "扫码登录失败，请刷新或导入 Cookie。"}
                    if kind in labels:
                        self._emit("account.qr_" + ("ready" if kind == "image" else kind), labels[kind],
                                   level="WARN" if kind in ("expired", "error") else "INFO",
                                   presentation="banner" if kind == "error" else "log")
                if kind == "image":
                    image = tk.PhotoImage(master=self.window, data=base64.b64encode(payload.png), format="png")
                    scale = max(1, (image.width() + 359) // 360)
                    self.photo = image.subsample(scale) if scale > 1 else image
                    self.image_label.configure(image=self.photo, text="")
                    self.deadline = time.monotonic() + payload.expires_seconds
                elif kind == "waiting":
                    self.status.set("等待扫码，请在手机 App 中打开扫一扫。")
                elif kind == "scanned":
                    self.status.set("已扫码，请在手机上确认登录。")
                elif kind == "success":
                    self.close(reason="completed")
                    self.on_success(payload)
                    return
                elif kind in ("expired", "error"):
                    self.deadline = None
                    self.photo = None
                    self.image_label.configure(image="", text="二维码已失效" if kind == "expired" else "无法生成或完成二维码登录")
                    self.status.set("二维码已过期，请点击刷新。" if kind == "expired" else payload)
                    self.countdown.set("")
        except queue.Empty:
            pass
        if self.deadline is not None:
            remaining = max(0, int(self.deadline - time.monotonic()))
            self.countdown.set(f"约 {remaining} 秒后过期")
        if not self.closed:
            self.poll_after = self.window.after(150, self._poll)

    def _emit(self, code, message, **fields):
        if self.on_event is not None:
            self.on_event(code, message, **fields)

    def close(self, *, reason="cancelled"):
        if self.closed:
            return
        self.closed = True
        self._emit("account.qr_closed", "扫码窗口已关闭。", details={"reason": reason})
        self.generation += 1
        if self.flow is not None:
            self.flow.cancel()
        if self.poll_after is not None:
            self.window.after_cancel(self.poll_after)
            self.poll_after = None
        self.photo = None
        self.window.grab_release()
        self.window.destroy()
        self.on_close()

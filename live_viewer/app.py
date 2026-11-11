from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, simpledialog, ttk

from .core import PlatformClient, Quality, ViewerError, comparison, cookies_from_text, load_cookies, read_flv_sample, room_number, save_report
from .credentials import CredentialStore
from .relay import LocalRelay, RelayEndpoint
from .qr_dialog import QrLoginDialog
from .settings import PLATFORMS, StateStore, remember_play
from .mpv_player import library_path
from .player_window import PlayerWindow
from .network import NETWORK_MODES
from .events import EventCenter
from .playback import PlaybackController, PlaybackService, OperationController, safe_error
from .potplayer import PotPlayerAdapter

VERSION = "0.5.2-p2"
BACKENDS = {"内置播放器": "mpv", "PotPlayer": "potplayer"}


def detect_potplayer() -> str:
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for name in ("PotPlayerMini64.exe", "PotPlayerMini.exe"):
                try:
                    with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + name) as key:
                        value = winreg.QueryValue(key, None).strip('"')
                        if Path(value).is_file():
                            return value
                except OSError:
                    pass
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")):
        for tail in ("DAUM/PotPlayer/PotPlayerMini64.exe", "PotPlayer/PotPlayerMini64.exe", "DAUM/PotPlayer/PotPlayerMini.exe"):
            candidate = Path(base) / tail
            if candidate.is_file():
                return str(candidate)
    return ""


class ViewerApp:
    def __init__(self, root: tk.Tk, data_dir: Path | None = None):
        self.root = root
        root.title(f"本地直播 · {VERSION}")
        root.geometry("1060x760")
        root.minsize(940, 620)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(10, 5))
        style.configure("Treeview", rowheight=34)
        self.store = StateStore(data_dir)
        config = self.store.load()
        if config["playback_backend"] == "mpv":
            try:
                library_path()
            except ViewerError:
                config["playback_backend"] = "potplayer"
        self.config = config
        self.activity = EventCenter(self.store.directory / "logs")
        self.controller = PlaybackController(self.activity)
        self.playback_service = PlaybackService(self.controller)
        self.operations = OperationController()
        self.credential_store = CredentialStore(self.store.directory)
        self.favorites = config["favorites"]
        self.history = config["history"]
        self.cookies = {}
        self.qr_dialog = None
        restore_errors = []
        for platform, remember in config["remember_login"].items():
            if remember:
                try:
                    jar = self.credential_store.load(platform)
                    if jar is not None:
                        self.cookies[platform] = jar
                except ViewerError as exc:
                    restore_errors.append(str(exc))
        self.rows = []
        self.qualities = []
        self.relay = None
        self.embedded_window = None
        self.potplayer = None
        self.pot_endpoint = RelayEndpoint()
        self.pot_token = None
        self.pot_serial = None
        self.pot_position = None
        self.pot_progress_at = None
        self.pot_last_state = None
        self.pot_expected_absence = True
        self.pot_expected_closed_media = False
        self.pot_had_graph = False
        self.active_spec = None
        self.retry = self.controller.retry
        self.paused_intent = False
        self.native_load_started = None
        self.native_progress_at = None
        self.native_position = None
        self.last_playback = {}
        self.network_result = None
        self.events = queue.Queue()
        self.busy = False
        self.closed = False
        self.play_generation = 0
        self.play_token = None
        self.operation_token = None
        self.pending_play_token = None
        self.alert_window = None
        self.alert_event = None
        self.alert_message = tk.StringVar()
        self.last_stats_logged_at = 0.0
        self.history_recorded = True
        last_room = config["last_room"]
        self.platform = tk.StringVar(value=next(k for k, v in PLATFORMS.items() if v == last_room["platform"]))
        self.room = tk.StringVar(value=last_room["room"])
        self.room_name = tk.StringVar(value=last_room["name"])
        self.quality = tk.StringVar(value="原画优先")
        self.mode = tk.StringVar(value="匿名")
        self.player = tk.StringVar(value=config["player"] or detect_potplayer())
        self.cookie_status = tk.StringVar(value="未导入 Cookie · 可先做匿名测试")
        self.status = tk.StringVar(value="请选择房间和播放方式，然后开始播放。")
        self.save_warning = ""
        self.playback_status = tk.StringVar(value="尚未开始实播")
        self.confirmed = tk.BooleanVar(value=False)
        self.note = tk.StringVar(value="")
        self.remember_login = tk.BooleanVar(value=False)
        self.low_latency = tk.BooleanVar(value=config["low_latency"])
        self.backend = tk.StringVar(value=next(k for k, v in BACKENDS.items() if v == config["playback_backend"]))
        self.auto_reconnect = tk.BooleanVar(value=config["auto_reconnect"])
        self.network_mode = tk.StringVar(value=next(k for k, v in NETWORK_MODES.items() if v == config["network_mode"]))
        self._build()
        root.report_callback_exception = lambda *_: self._error(
            None, code="app.callback_failed", scope="app")
        self.activity.emit("app.started", "程序已启动。")
        self._update_favorites()
        self._update_history()
        self._platform_changed()
        self.platform.trace_add("write", lambda *_: self._platform_changed())
        self.room.trace_add("write", lambda *_: self._reset_quality())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_after = self.root.after(200, self._poll)
        for message in ([self.store.warning] if self.store.warning else []) + restore_errors:
            self._log(message, code="storage.restore_warning", level="WARN", presentation="banner", scope="storage")
        for platform in self.cookies:
            self._log("已恢复 Windows 加密保存的登录；使用时检查有效性。",
                      code="account.restored", target=platform)

    def _build(self):
        shell = ttk.Frame(self.root, padding=(18, 14))
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="本地直播", font=("Microsoft YaHei UI", 23, "bold")).pack(anchor="w")
        ttk.Label(shell, text="保存喜欢的直播间，在 PotPlayer 专用窗口观看。", foreground="#56616f").pack(anchor="w", pady=(3, 12))
        self.notebook = ttk.Notebook(shell)
        self.notebook.pack(fill="both", expand=True)
        self.page_canvases = []

        def page(title):
            host = ttk.Frame(self.notebook)
            self.notebook.add(host, text=title)
            canvas = tk.Canvas(host, highlightthickness=0)
            scroll = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
            scroll.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            canvas.configure(yscrollcommand=scroll.set)
            frame = ttk.Frame(canvas, padding=14)
            window = canvas.create_window(0, 0, window=frame, anchor="nw")
            frame.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
            canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
            self.page_canvases.append(canvas)
            return frame

        watch = page("观看")
        account = page("账号与播放器")
        diagnostics = page("画质对比与报告")
        self.canvas = self.page_canvases[0]

        def wheel(event):
            if not isinstance(event.widget, (tk.Text, ttk.Combobox, ttk.Treeview)):
                self.page_canvases[self.notebook.index("current")].yview_scroll(-int(event.delta / 120), "units")
        self.root.bind("<MouseWheel>", wheel, add="+")

        settings = ttk.LabelFrame(watch, text="直播间", padding=12)
        settings.pack(fill="x")
        top = ttk.Frame(settings)
        top.pack(fill="x")
        ttk.Label(top, text="平台").pack(side="left")
        self.platform_box = ttk.Combobox(top, textvariable=self.platform, values=list(PLATFORMS), width=8, state="readonly")
        self.platform_box.pack(side="left", padx=(8, 16))
        ttk.Label(top, text="房间号或链接").pack(side="left")
        self.room_entry = ttk.Entry(top, textvariable=self.room)
        self.room_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.room_entry.bind("<Return>", lambda _: self.play() if not self.busy else None)
        self.save_button = ttk.Button(top, text="保存房间", command=self.save_favorite)
        self.save_button.pack(side="left")
        naming = ttk.Frame(settings)
        naming.pack(fill="x", pady=(10, 0))
        ttk.Label(naming, text="自定义房间名").pack(side="left")
        self.name_entry = ttk.Entry(naming, textvariable=self.room_name)
        self.name_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.name_entry.bind("<Return>", lambda _: self.save_favorite() if not self.busy else None)
        ttk.Label(naming, text="房间号为索引，名称可随时改", foreground="#56616f").pack(side="left")
        favorites = ttk.Frame(settings)
        favorites.pack(fill="x", pady=(12, 0))
        ttk.Label(favorites, text="收藏房间").pack(side="left")
        self.favorite_box = ttk.Combobox(favorites, state="readonly", width=30)
        self.favorite_box.pack(side="left", fill="x", expand=True, padx=8)
        self.favorite_box.bind("<<ComboboxSelected>>", self.open_favorite)
        self.favorite_play_button = ttk.Button(favorites, text="播放收藏", command=lambda: self.open_favorite(play=True))
        self.favorite_play_button.pack(side="left")
        self.rename_button = ttk.Button(favorites, text="改名", command=self.rename_favorite)
        self.rename_button.pack(side="left", padx=6)
        self.delete_button = ttk.Button(favorites, text="删除", command=self.delete_favorite)
        self.delete_button.pack(side="left")
        recent = ttk.Frame(settings)
        recent.pack(fill="x", pady=(10, 0))
        ttk.Label(recent, text="最近观看").pack(side="left")
        self.history_box = ttk.Combobox(recent, state="readonly", width=30)
        self.history_box.pack(side="left", fill="x", expand=True, padx=8)
        self.history_box.bind("<<ComboboxSelected>>", self.open_history)
        self.history_play_button = ttk.Button(recent, text="播放最近", command=lambda: self.open_history(play=True))
        self.history_play_button.pack(side="left")
        self.clear_history_button = ttk.Button(recent, text="清空记录", command=self.clear_history)
        self.clear_history_button.pack(side="left", padx=(6, 0))

        playback = ttk.LabelFrame(watch, text="播放", padding=12)
        playback.pack(fill="x", pady=(14, 0))
        buttons = ttk.Frame(playback)
        buttons.pack(fill="x")
        ttk.Label(buttons, text="画质").pack(side="left")
        self.quality_box = ttk.Combobox(buttons, textvariable=self.quality, state="readonly", values=["原画优先"], width=21)
        self.quality_box.pack(side="left", padx=(8, 16))
        ttk.Label(buttons, text="账号").pack(side="left")
        self.mode_box = ttk.Combobox(buttons, textvariable=self.mode, state="readonly", values=["匿名", "导入 Cookie"], width=12)
        self.mode_box.pack(side="left", padx=8)
        ttk.Label(buttons, text="播放器").pack(side="left", padx=(12, 0))
        self.backend_box = ttk.Combobox(buttons, textvariable=self.backend, state="readonly", values=list(BACKENDS), width=13)
        self.backend_box.pack(side="left", padx=8)
        ttk.Label(playback, textvariable=self.cookie_status, foreground="#56616f").pack(anchor="w", pady=(8, 0))
        controls = ttk.Frame(playback)
        controls.pack(fill="x", pady=12)
        self.play_button = ttk.Button(controls, text="开始播放", command=self.play)
        self.play_button.pack(side="left")
        self.reconnect_button = ttk.Button(controls, text="回到直播", command=self.reconnect)
        self.reconnect_button.pack(side="left", padx=8)
        ttk.Button(controls, text="停止播放流", command=self.stop_playback).pack(side="left")
        self.low_latency_box = ttk.Checkbutton(playback, text="优先实时播放（减少转发积压，下次播放生效）",
                                              variable=self.low_latency, command=self._write_config)
        self.low_latency_box.pack(anchor="w", pady=(4, 0))
        self.auto_retry_box = ttk.Checkbutton(playback, text="直播断流后自动重连", variable=self.auto_reconnect, command=self._write_config)
        self.auto_retry_box.pack(anchor="w", pady=(4, 0))
        ttk.Label(playback, text="转发进度差不代表画面延迟；画面落后时点“回到直播”，暂停后也建议重新连接。",
                  foreground="#56616f", wraplength=890).pack(anchor="w", pady=(6, 0))
        ttk.Label(watch, text="内置播放器：空格暂停 / 继续，F 或双击全屏，Esc 退出全屏，L 回到直播。", foreground="#56616f", wraplength=900).pack(anchor="w", pady=(12, 0))

        login = ttk.LabelFrame(account, text="当前平台登录", padding=12)
        login.pack(fill="x")
        ttk.Label(login, textvariable=self.platform, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(login, textvariable=self.cookie_status, wraplength=880).pack(anchor="w", pady=8)
        login_buttons = ttk.Frame(login)
        login_buttons.pack(fill="x")
        self.qr_button = ttk.Button(login_buttons, text="扫码登录", command=self.qr_login)
        self.qr_button.pack(side="left", padx=(0, 8))
        self.import_button = ttk.Button(login_buttons, text="导入 Cookie 文件", command=self.import_cookies)
        self.import_button.pack(side="left")
        self.paste_button = ttk.Button(login_buttons, text="粘贴 Cookie", command=self.paste_cookies)
        self.paste_button.pack(side="left", padx=8)
        self.clear_button = ttk.Button(login_buttons, text="清除当前平台登录", command=self.clear_cookies)
        self.clear_button.pack(side="left")
        self.remember_box = ttk.Checkbutton(login, text="记住当前平台登录（Windows 用户加密）", variable=self.remember_login, command=self.toggle_remember_login)
        self.remember_box.pack(anchor="w", pady=(12, 0))
        ttk.Label(login, text="勾选后下次启动会恢复登录；取消勾选会删除已保存的登录，本次会话仍可使用。", foreground="#56616f", wraplength=890).pack(anchor="w", pady=(6, 0))
        player_settings = ttk.LabelFrame(account, text="播放器", padding=12)
        player_settings.pack(fill="x", pady=14)
        path = ttk.Frame(player_settings)
        path.pack(fill="x")
        self.player_entry = ttk.Entry(path, textvariable=self.player)
        self.player_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.browse_button = ttk.Button(path, text="选择 PotPlayer.exe", command=self.choose_player)
        self.browse_button.pack(side="left")
        ttk.Label(player_settings, text="PotPlayer 使用本工具专用窗口与独立配置。首次运行准备本地副本，不更改安装目录或普通播放窗口；皮肤与视频增强可在专用窗口中设置。", foreground="#56616f", wraplength=890).pack(anchor="w", pady=(8, 0))
        network = ttk.LabelFrame(account, text="网络连接", padding=12)
        network.pack(fill="x", pady=(0, 14))
        row = ttk.Frame(network)
        row.pack(fill="x")
        ttk.Label(row, text="平台接口、登录和 CDN").pack(side="left")
        self.network_box = ttk.Combobox(row, textvariable=self.network_mode, values=list(NETWORK_MODES), state="readonly", width=22)
        self.network_box.pack(side="left", padx=10)
        self.network_box.bind("<<ComboboxSelected>>", lambda _: self._write_config())
        self.network_button = ttk.Button(row, text="检查当前直播间连接", command=self.check_connection)
        self.network_button.pack(side="left")
        ttk.Label(network, text="直连使用本机网络，不继承系统 / 环境代理；更改后重新播放生效。VPN 的全局隧道仍由 Windows 决定。", foreground="#56616f", wraplength=890).pack(anchor="w", pady=(8, 0))
        ttk.Label(account, text=f"版本 {VERSION} · 数据目录：{self.store.directory}", foreground="#56616f", wraplength=890).pack(anchor="w", pady=(0, 10))

        actions = ttk.Frame(diagnostics)
        actions.pack(fill="x", pady=(0, 10))
        self.compare_button = ttk.Button(actions, text="对比当前房间的匿名 / 登录画质", command=self.compare)
        self.compare_button.pack(side="left")
        self.export_button = ttk.Button(actions, text="导出报告", command=self.export)
        self.export_button.pack(side="right")
        columns = ("session", "request", "actual", "picture", "bitrate", "status")
        self.table = ttk.Treeview(diagnostics, columns=columns, show="headings", height=5)
        labels = ("会话", "请求档位", "实际返回", "流元数据", "样本码率", "结果")
        widths = (100, 70, 125, 160, 100, 280)
        for col, label, width in zip(columns, labels, widths):
            self.table.heading(col, text=label)
            self.table.column(col, width=width, minwidth=60, stretch=col in ("picture", "status"))
        self.table.pack(fill="x")
        table_scroll = ttk.Scrollbar(diagnostics, orient="horizontal", command=self.table.xview)
        table_scroll.pack(fill="x")
        self.table.configure(xscrollcommand=table_scroll.set)
        ttk.Label(diagnostics, text="使用观看页面的房间和请求画质。平台可能返回较低档位，结果以实际返回为准。", foreground="#56616f", wraplength=900).pack(anchor="w", pady=(10, 14))
        ttk.Checkbutton(diagnostics, text="我已在播放器确认画面、声音和音画同步", variable=self.confirmed).pack(anchor="w")
        notes = ttk.Frame(diagnostics)
        notes.pack(fill="x", pady=10)
        ttk.Label(notes, text="播放备注").pack(side="left")
        ttk.Entry(notes, textvariable=self.note).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(diagnostics, text="报告不包含 Cookie、签名直播地址或本机播放地址。请勿在备注中填入登录凭据。", foreground="#56616f", wraplength=900).pack(anchor="w")

        self.notice_text = tk.StringVar()
        self.notice_frame = tk.Frame(shell, background="#fff0ed", padx=12, pady=10)
        tk.Label(self.notice_frame, textvariable=self.notice_text, background="#fff0ed", foreground="#9e271d",
                 font=("Microsoft YaHei UI", 12, "bold"), wraplength=970, justify="left", anchor="w").pack(fill="x")
        self.playback_label = ttk.Label(shell, textvariable=self.playback_status, wraplength=980,
                                       font=("Microsoft YaHei UI", 12, "bold"))
        self.playback_label.pack(anchor="w", pady=(10, 6))
        footer = ttk.Frame(shell)
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status, wraplength=830).pack(side="left", fill="x", expand=True)
        self.log_export_button = ttk.Button(footer, text="导出事件日志", command=self.export_events)
        self.log_export_button.pack(side="right")
        self.log = tk.Text(shell, height=3, wrap="word", state="disabled", font=("Microsoft YaHei UI", 9))
        self.log.pack(fill="x")
        self.disable_during_work = [self.save_button, self.delete_button, self.rename_button,
                                    self.favorite_play_button, self.history_play_button, self.clear_history_button,
                                    self.import_button, self.paste_button, self.clear_button, self.remember_box, self.qr_button,
                                    self.compare_button, self.play_button, self.reconnect_button,
                                    self.browse_button, self.export_button, self.low_latency_box, self.network_button]

    def _log(self, message, *, code="operation.message", level="INFO", presentation="log",
             scope="operation", target=None, **fields):
        self.activity.emit(code, message, level=level, presentation=presentation, scope=scope,
                           target=target, **fields)
        self._drain_activity()

    def _error(self, exc, *, code=None, scope="operation", target=None):
        error = safe_error(exc)
        self._log(str(error), code=code or error.code, level=error.level, presentation="banner",
                  scope=scope, target=target)

    def _drain_activity(self):
        if self.closed:
            return
        if self.relay is None and self.controller.state != "idle":
            self.playback_status.set(self.controller.message)
        if self.relay is None and self.controller.state != "idle":
            self.playback_status.set(self.controller.message)
        rows, alerts = self.activity.drain()
        self.log.configure(state="normal")
        for event in rows:
            if event.level != "DEBUG":
                self.status.set(event.message)
            line = f"{event.occurred_at[11:19]} [{event.level}] {event.target or event.source} · {event.message}\n"
            self.log.insert("end", line)
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 600:
            self.log.delete("1.0", f"{lines - 500}.0")
        self.log.see("end")
        self.log.configure(state="disabled")
        notice = self.activity.notice()
        if notice is not None:
            self.notice_text.set(f"{notice.target + ' · ' if notice.target else ''}{notice.message}")
            if not self.notice_frame.winfo_manager():
                self.notice_frame.pack(fill="x", before=self.playback_label, pady=(8, 0))
        else:
            self.notice_frame.pack_forget()
        if self.alert_window is not None and self.alert_window.winfo_exists():
            matching = next((e for e in self.activity.notice_events()
                             if e.session_id == self.alert_event.session_id
                             and e.dedupe_key == self.alert_event.dedupe_key and e.presentation == "strong"), None)
            if matching is None:
                self.alert_window.destroy()
            else:
                self.alert_message.set(matching.message)
        for event in alerts:
            if event in self.activity.notice_events():
                self._show_alert(event)

    def _show_alert(self, event):
        # Nonblocking, no grab or confirmation needed to continue retrying.
        if self.alert_window is not None and self.alert_window.winfo_exists():
            self.alert_window.destroy()
        dialog = self.alert_window = tk.Toplevel(self.root)
        self.alert_event = event
        self.alert_message.set(event.message)
        dialog.title("直播无法继续")
        dialog.geometry("560x260")
        dialog.transient(self.root)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="直播连接需要处理", font=("Microsoft YaHei UI", 15, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.alert_message, wraplength=515, font=("Microsoft YaHei UI", 12)).pack(anchor="w", pady=12)
        buttons = ttk.Frame(frame);buttons.pack(fill="x", pady=8)
        ttk.Button(buttons, text="回到直播", command=lambda: (dialog.destroy(), self.reconnect())).pack(side="left")
        ttk.Button(buttons, text="查看日志", command=lambda: (dialog.destroy(), self.export_events())).pack(side="left", padx=8)
        ttk.Button(buttons, text="关闭提醒", command=dialog.destroy).pack(side="right")
        dialog.update_idletasks()
        height = min(max(260, frame.winfo_reqheight() + 20), dialog.winfo_screenheight() - 80)
        dialog.geometry(f"560x{height}")
        if not self.root.winfo_viewable():
            dialog.withdraw()  # Headless/offline checks must not leave popup windows.

    def export_events(self):
        path = filedialog.asksaveasfilename(title="导出脱敏事件日志", defaultextension=".json",
                                          initialfile="live-events.json", filetypes=[("JSON 日志", "*.json")])
        if path:
            try:
                count = self.activity.export(path)
                self._log(f"已导出最近 {count} 条事件。", code="log.exported")
            except OSError:
                self._log("事件日志导出失败，请选择可写目录。", code="log.export_failed",
                          level="ERROR", presentation="banner", scope="log")

    def _platform_changed(self):
        self._reset_quality()
        key = PLATFORMS[self.platform.get()]
        self.remember_login.set(self.config["remember_login"].get(key, False))
        self.cookie_status.set("Cookie 已载入；播放时检查登录状态" if key in self.cookies else "未导入 Cookie · 可匿名播放")

    def _reset_quality(self):
        self.qualities = []
        self.quality.set("原画优先")
        self.quality_box.configure(values=["原画优先"])
        try:
            platform = PLATFORMS[self.platform.get()]
            room = room_number(platform, self.room.get())
        except ViewerError:
            self.room_name.set("")
            return
        self.room_name.set(self._saved_room_name(platform, room))
        preference = self.config["room_preferences"].get(f"{platform}:{room}", {})
        requested = preference.get("quality")
        if requested is not None:
            self.qualities = [Quality(requested, "上次选择")]
            label = f"上次选择（{requested}）"
            self.quality_box.configure(values=["原画优先", label])
            self.quality.set(label)
        self.mode.set("导入 Cookie" if preference.get("mode") == "导入 Cookie" and platform in self.cookies else "匿名")

    def _selection(self):
        platform = PLATFORMS[self.platform.get()]
        room = room_number(platform, self.room.get())
        index = self.quality_box.current()
        requested = self.qualities[index - 1].value if 0 < index <= len(self.qualities) else None
        return platform, room, requested

    def _write_config(self):
        try:
            option_keys = ("player", "low_latency", "playback_backend", "auto_reconnect", "network_mode")
            previous = {key: self.config[key] for key in option_keys}
            try:
                platform, room, requested = self._selection()
                self.config["last_room"] = {"platform": platform, "room": room,
                                            "name": self.room_name.get().strip()[:80] or self._saved_room_name(platform, room)}
                self.config["room_preferences"][f"{platform}:{room}"] = {"quality": requested, "mode": self.mode.get()}
            except ViewerError:
                pass
            self.config.update(favorites=self.favorites, history=self.history, player=self.player.get())
            self.config["low_latency"] = self.low_latency.get()
            self.config["playback_backend"] = BACKENDS[self.backend.get()]
            self.config["auto_reconnect"] = self.auto_reconnect.get()
            self.config["network_mode"] = NETWORK_MODES[self.network_mode.get()]
            self.store.save(self.config)
            changed = [key for key in option_keys if previous[key] != self.config[key]]
            self.activity.emit("settings.saved", "设置已保存。", source="settings", level="DEBUG")
            if changed:
                self.activity.emit("settings.updated", "播放 / 网络设置已更新。", source="settings",
                                   details={"reason": ", ".join(changed)})
            if self.save_warning:
                self.activity.clear("storage")
                self.activity.emit("settings.write_recovered", "设置保存已恢复。", source="settings")
            self.save_warning = ""
        except OSError:
            self.save_warning = "设置保存失败；本次仍可使用，请将程序放在可写目录。"
            self._log(self.save_warning, code="settings.write_failed", level="WARN", presentation="banner", scope="storage")

    def _update_favorites(self):
        self.favorite_box.configure(values=[f"{r['name']} · {next(k for k, v in PLATFORMS.items() if v == r['platform'])} {r['room']}" for r in self.favorites])
        self.favorite_box.set("")

    def _update_history(self):
        self.history_box.configure(values=[f"{r['name']} · {next(k for k, v in PLATFORMS.items() if v == r['platform'])} {r['room']} · {r.get('played_at', '')[:16].replace('T', ' ')}" for r in self.history])
        self.history_box.set("")

    def _saved_room_name(self, platform, room):
        for record in self.favorites + self.history + [self.config["last_room"]]:
            if record["platform"] == platform and record["room"] == room:
                return record["name"]
        return f"{next(k for k, v in PLATFORMS.items() if v == platform)} · {room}"

    def _set_room_name(self, platform, room, name):
        for record in self.favorites + self.history + [self.config["last_room"]]:
            if record["platform"] == platform and record["room"] == room:
                record["name"] = name
        if self.last_playback.get("platform") == platform and self.last_playback.get("room_input") == room:
            self.last_playback["room_name"] = name
        try:
            current_platform, current_room, _ = self._selection()
            if (current_platform, current_room) == (platform, room):
                self.room_name.set(name)
        except ViewerError:
            pass

    def _choose_room(self, room):
        self._write_config()
        self.platform.set(next(k for k, v in PLATFORMS.items() if v == room["platform"]))
        self.room.set(room["room"])
        self.notebook.select(0)
        self._log("已选择保存的直播间。", code="room.selected", target=f"{room['platform']}:{room['room']}")

    def open_history(self, _=None, *, play=False):
        index = self.history_box.current()
        if index < 0:
            if play:
                self._log("请先选择一条最近观看记录。")
            return
        self._choose_room(self.history[index])
        if play:
            self.play()

    def clear_history(self):
        self.history.clear()
        self._update_history()
        self._write_config()
        self._log("最近观看记录已清空。", code="history.cleared")

    def save_favorite(self):
        try:
            platform, room, _ = self._selection()
        except ViewerError as exc:
            self._error(exc, code="room.input_invalid"); return
        existing = next((i for i, r in enumerate(self.favorites) if r["platform"] == platform and r["room"] == room), None)
        name = self.room_name.get().strip()[:80] or self._saved_room_name(platform, room)
        if existing is None:
            self.favorites.append({"platform": platform, "room": room, "name": name})
            existing = len(self.favorites) - 1
        self._set_room_name(platform, room, name)
        self._update_favorites()
        self._update_history()
        self._write_config()
        self.favorite_box.current(existing)
        self._log(f"已保存“{name}”；索引 {self.platform.get()} {room}。", code="room.saved", target=f"{platform}:{room}")

    def open_favorite(self, _=None, *, play=False):
        index = self.favorite_box.current()
        if index >= 0:
            room = self.favorites[index]
            self._choose_room(room)
            if play:
                self.play()
        elif play:
            self._log("请先选择一个收藏房间。")

    def rename_favorite(self):
        index = self.favorite_box.current()
        if index < 0:
            self._log("请先选择需要改名的收藏房间。")
            return
        room = self.favorites[index]
        name = simpledialog.askstring("收藏改名", "输入新名称：", initialvalue=room["name"], parent=self.root)
        if name is None or not name.strip():
            return
        self._set_room_name(room["platform"], room["room"], name.strip()[:80])
        self._update_favorites()
        self.favorite_box.current(index)
        self._update_history()
        self._write_config()
        self._log("收藏名称已更新。", code="room.renamed", target=f"{room['platform']}:{room['room']}")

    def delete_favorite(self):
        index = self.favorite_box.current()
        if index >= 0:
            room = self.favorites.pop(index)
            self._update_favorites()
            self._write_config()
            self._log("收藏房间已删除。", code="room.deleted", target=f"{room['platform']}:{room['room']}")

    def _set_busy(self, busy):
        self.busy = busy
        for widget in self.disable_during_work:
            widget.configure(state="disabled" if busy else "normal")
        for widget in (self.platform_box, self.favorite_box, self.history_box, self.quality_box, self.mode_box, self.backend_box, self.network_box):
            widget.configure(state="disabled" if busy else "readonly")
        for widget in (self.room_entry, self.name_entry, self.player_entry):
            widget.configure(state="disabled" if busy else "normal")

    def _work(self, fn, done, failed=None, *, code="operation.task", target=None, play_token=None):
        if self.busy:
            return
        self._set_busy(True)
        operation = self.operations.begin(code, target)
        self.operation_token = operation
        self.pending_play_token = play_token
        self.activity.emit(code + ".started", "后台操作已开始。", source="operation",
                           target=target, level="DEBUG")

        def worker():
            try:
                result = fn()
                if self.closed:
                    if isinstance(result, tuple) and isinstance(result[0], LocalRelay):
                        result[0].stop()
                else:
                    self.events.put((done, result, None, failed, operation, play_token))
            except ViewerError as exc:
                self.events.put((done, None, exc, failed, operation, play_token))
            except Exception:
                self.events.put((done, None, safe_error(None), failed, operation, play_token))
        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        self.poll_after = None
        # User close/pause/stop must invalidate a pending prepared result before
        # its callback has a chance to submit another open command.
        self._poll_potplayer()
        try:
            while True:
                done, result, error, failed, operation, play_token = self.events.get_nowait()
                if (not self.operations.current(operation)
                        or play_token is not None and not self.controller.current(play_token)):
                    if isinstance(result, tuple) and isinstance(result[0], LocalRelay):
                        result[0].stop()
                    self.activity.emit("operation.stale_result", "已丢弃过期后台结果。", source="operation",
                                       level="DEBUG", target=operation.target, details={"reason": operation.channel})
                    continue
                if operation == self.operation_token:
                    self.operation_token = self.pending_play_token = None
                    self._set_busy(False)
                if error:
                    if failed:
                        failed(error)
                    else:
                        self._error(error, scope=operation.channel, target=operation.target)
                else:
                    try:
                        self.activity.clear(operation.channel)
                        done(result)
                        self.activity.emit(operation.channel + ".completed", "后台操作已完成。",
                                           source="operation", target=operation.target, level="DEBUG")
                    except Exception as exc:
                        if failed:
                            failed(safe_error(exc))
                        else:
                            self._error(exc, scope=operation.channel, target=operation.target)
        except queue.Empty:
            pass
        self._poll_embedded()
        self._drain_activity()
        if self.relay:
            stats = self.relay.snapshot()
            if not self.history_recorded and stats["bytes_forwarded"] > 0:
                platform, room = self.last_playback["platform"], self.last_playback["room_input"]
                record = next((r for r in self.favorites if r["platform"] == platform and r["room"] == room),
                              {"platform": platform, "room": room, "name": self.last_playback.get("room_name", "")})
                self.history = remember_play(self.history, record)
                self.history_recorded = True
                self._update_history()
                self._write_config()
                self.activity.emit("history.recorded", "已记录本次观看房间。", source="history", target=f"{platform}:{room}")
            stream = stats.get("last_stream") or {}
            name = stream.get("actual_name", "解析中")
            gap = stats.get("timing", {}).get("stream_progress_gap_seconds")
            progress = f" · 转发进度差 {gap:.2f} 秒" if gap is not None else ""
            room_name = self.last_playback.get("room_name", "直播")
            self.playback_status.set(f"{room_name} · {stats['status']} · 实际 {name} · 已转发 {stats['bytes_forwarded'] / 1048576:.1f} MB · {stats['elapsed_seconds']:.0f} 秒{progress}")
            if time.monotonic() - self.last_stats_logged_at >= 60:
                self.last_stats_logged_at = time.monotonic()
                self.controller.publish(self.play_token, "relay.statistics",
                                        f"转发累计 {stats['bytes_forwarded'] / 1048576:.1f} MB；"
                                        f"连接 {stats['connections']} 次，失败 {stats['failures']} 次。",
                                        level="DEBUG", details={"event_count": stats["failures"]})
        elif self.controller.state != "idle":
            self.playback_status.set(self.controller.message)
        if not self.closed:
            self.poll_after = self.root.after(200, self._poll)

    def import_cookies(self):
        path = filedialog.askopenfilename(title="选择该平台的 Netscape Cookie 文件", filetypes=[("所有文件（支持无后缀的导出文本）", "*"), ("Netscape Cookie 文本", "*.txt")])
        if not path:
            return
        platform = PLATFORMS[self.platform.get()]
        try:
            jar = load_cookies(path, platform)
        except ViewerError as exc:
            self._error(exc, code="account.import_invalid", scope=f"account:{platform}"); return

        self._import_cookie_jar(platform, jar)

    def qr_login(self):
        if self.busy or self.closed:
            return
        if self.qr_dialog is not None:
            self.qr_dialog.window.lift()
            return
        platform = PLATFORMS[self.platform.get()]
        self._log("已打开扫码登录。", code="account.qr_opened", target=platform)

        def success(jar):
            if not self.closed:
                self._import_cookie_jar(platform, jar)

        def closed():
            self.qr_dialog = None

        self.qr_dialog = QrLoginDialog(self.root, platform, success, closed, network_mode=NETWORK_MODES[self.network_mode.get()],
                                     on_event=lambda code, message, **fields: self._log(
                                         message, code=code, scope=f"account:{platform}", target=platform, **fields))

    def paste_cookies(self):
        platform = PLATFORMS[self.platform.get()]
        prompt = ("在本机 Edge 登录 B 站后，按 F12 → 应用程序 → Cookies，找到 SESSDATA。\n"
                  "粘贴 SESSDATA 的值或完整 Cookie 请求头；勾选记住登录后会加密保存。") if platform == "bilibili" else (
                  "在本机 Edge 登录斗鱼后，按 F12 → 网络，选择发往 douyu.com 的请求。\n"
                  "从请求标头复制单行 Cookie（name=value; ...）粘贴到此处。")
        text = simpledialog.askstring("粘贴本机登录 Cookie", prompt, show="*", parent=self.root)
        if text is None:
            return
        try:
            jar = cookies_from_text(text, platform)
        except ViewerError as exc:
            self._error(exc, code="account.import_invalid", scope=f"account:{platform}")
            return
        finally:
            text = None
        self._import_cookie_jar(platform, jar)

    def _import_cookie_jar(self, platform, jar):
        route = NETWORK_MODES[self.network_mode.get()]
        def verify():
            client = PlatformClient(platform, jar, network_mode=route)
            try:
                return client.login_status()
            finally:
                client.close()

        def done(result):
            extra = ""
            if result["verified"] is False:
                self.cookies.pop(platform, None)
                self.mode.set("匿名")
                self.config["remember_login"][platform] = False
                self.remember_login.set(False)
                try:
                    self.credential_store.delete(platform)
                except ViewerError as exc:
                    extra = str(exc)
            else:
                self.cookies[platform] = jar
                self.mode.set("导入 Cookie")
                if self.config["remember_login"].get(platform):
                    try:
                        self.credential_store.save(platform, jar)
                        extra = "登录已加密保存，下次启动可恢复。"
                    except ViewerError as exc:
                        extra = str(exc)
                        self.config["remember_login"][platform] = False
                        self.remember_login.set(False)
            self.cookie_status.set(result["message"])
            self._write_config()
            if result["verified"] is not False:
                self.activity.clear(f"account:{platform}")
            self._log(result["message"] + ("；" + extra if extra else ""),
                      code="account.invalid" if result["verified"] is False else "account.imported",
                      level="WARN" if result["verified"] is False else "INFO",
                      presentation="banner" if result["verified"] is False else "log",
                      scope=f"account:{platform}", target=platform)
            if extra and not self.config["remember_login"].get(platform) and result["verified"] is not False:
                self._log(extra, code="account.remember_failed", level="WARN",
                          presentation="banner", scope=f"account:{platform}", target=platform)
        self._log("正在验证本机导入的登录状态…", code="account.verifying", target=platform)
        self._work(verify, done, code="account.import", target=platform)

    def clear_cookies(self):
        self.stop_playback()
        platform = PLATFORMS[self.platform.get()]
        self.operations.cancel("account.import")
        if self.operation_token is not None and self.operation_token.channel == "account.import":
            self.operation_token = None
            self._set_busy(False)
        self.cookies.pop(platform, None)
        self.config["remember_login"][platform] = False
        self.remember_login.set(False)
        self.mode.set("匿名")
        self.cookie_status.set("已清除当前平台登录状态")
        self._write_config()
        try:
            self.credential_store.delete(platform)
        except ViewerError as exc:
            self._error(exc, code="account.clear_failed", scope=f"account:{platform}", target=platform)
            return
        self.activity.clear(f"account:{platform}")
        self._log("当前平台 Cookie 已从程序内存清除。", code="account.cleared", target=platform)

    def toggle_remember_login(self):
        platform = PLATFORMS[self.platform.get()]
        remember = self.remember_login.get()
        try:
            if remember and platform in self.cookies:
                self.credential_store.save(platform, self.cookies[platform])
            elif not remember:
                self.credential_store.delete(platform)
        except ViewerError as exc:
            self.remember_login.set(self.config["remember_login"].get(platform, False))
            self._error(exc, code="account.remember_failed", scope=f"account:{platform}", target=platform)
            return
        self.config["remember_login"][platform] = remember
        self._write_config()
        self._log("已开启登录记忆；导入的 Cookie 会由 Windows 加密保存。" if remember else "已关闭登录记忆并删除保存的登录；本次会话仍可使用。",
                  code="account.remember_enabled" if remember else "account.remember_disabled", target=platform)

    def compare(self):
        try:
            platform, room, requested = self._selection()
        except ViewerError as exc:
            self._error(exc, code="room.input_invalid"); return
        jar = self.cookies.get(platform)
        self._log("正在依次测试匿名与 Cookie 会话，短时采样可能需要几十秒…", code="diagnostic.compare_requested", target=f"{platform}:{room}")

        def done(result):
            self.rows, self.qualities = result
            values = ["原画优先"] + [f"{q.name}（{q.value}）" for q in self.qualities]
            self.quality_box.configure(values=values)
            self.quality.set("原画优先" if requested is None else next((values[i + 1] for i, q in enumerate(self.qualities) if q.value == requested), values[0]))
            self.table.delete(*self.table.get_children())
            for row in self.rows:
                self.activity.emit("diagnostic.sample_result", row["status"], source="diagnostic",
                                   target=f"{platform}:{room}", level=row.get("level", "INFO"))
                sample = row.get("sample", {})
                meta = sample.get("metadata", {})
                picture = f"{meta.get('width', 0):g}×{meta.get('height', 0):g} / {meta.get('framerate', 0):g} fps" if meta else "未取得"
                bitrate = sample.get("sample_total_kbps")
                actual = row.get("actual_name", "—") + ("（已调整）" if row.get("different_from_request") else "")
                self.table.insert("", "end", values=(row["session"], row.get("requested", "—"), actual,
                                                       picture, f"{bitrate} kbps" if bitrate else "—", row["status"]))
            self._log("画质对比完成。点击开始播放，检查画面与声音。", code="diagnostic.compare_completed", target=f"{platform}:{room}")
        route = NETWORK_MODES[self.network_mode.get()]
        self._work(lambda: comparison(platform, room, requested, jar, network_mode=route), done,
                   code="diagnostic.compare", target=f"{platform}:{room}")

    def check_connection(self):
        try:
            platform, room, requested = self._selection()
        except ViewerError as exc:
            self._error(exc, code="room.input_invalid"); return
        route = NETWORK_MODES[self.network_mode.get()]
        jar = self.cookies.get(platform) if self.mode.get() == "导入 Cookie" else None
        def check():
            result = {"platform": platform, "room_input": room, "network_mode": route,
                      "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"), "stage": "房间 / 播放接口"}
            client = PlatformClient(platform, jar, network_mode=route)
            try:
                stream = client.resolve(room, requested)
                result.update(stage="CDN 直播读取", stream=stream.summary())
                result["sample"] = read_flv_sample(client, stream, seconds=2)
                result.update(stage="完成", status="房间接口和 CDN 直播读取成功；本检查不验证播放器解码。")
            except ViewerError as exc:
                result["status"] = str(exc)
                result["error_code"], result["level"] = exc.code, exc.level
            finally:
                client.close()
            return result
        def done(result):
            self.network_result = result
            self._log(f"{result['platform']} {room} · {result['stage']} · {result['status']}（可导出报告）",
                      code="diagnostic.connection_failed" if result.get("error_code") else "diagnostic.connection_ready",
                      level=result.get("level", "INFO"), presentation="banner" if result.get("error_code") else "log",
                      scope="diagnostic.connection", target=f"{platform}:{room}")
        self._log("正在检查当前直播间的接口和 CDN 连接…", code="diagnostic.connection_requested", target=f"{platform}:{room}")
        self._work(check, done, code="diagnostic.connection", target=f"{platform}:{room}")

    def choose_player(self):
        path = filedialog.askopenfilename(title="选择 PotPlayer 播放器程序", filetypes=[("Windows 程序", "*.exe")])
        if path:
            self.player.set(path)
            self._write_config()
            self._log("PotPlayer 路径已更新。", code="settings.player_changed")

    def _ensure_embedded(self):
        if self.embedded_window is None or self.embedded_window.closed:
            library_path()
            self.embedded_window = PlayerWindow(self.root, on_close=self._embedded_closed,
                on_pause=self._toggle_pause, on_reconnect=self.reconnect,
                on_volume=self._set_volume, on_mute=self._set_mute,
                volume=self.config["volume"], mute=self.config["mute"])
        return self.embedded_window

    def _ensure_potplayer(self):
        executable = Path(self.player.get().strip().strip('"')).resolve()
        if self.potplayer is not None and self.potplayer.profile.source != executable:
            self.potplayer.close()
            self.potplayer = None
        if self.potplayer is None:
            self.potplayer = PotPlayerAdapter(self.store.directory, executable)
        return self.potplayer

    def _cancel_play_job(self):
        if self.pending_play_token is not None:
            self.operations.cancel("playback.prepare")
            self.operation_token = self.pending_play_token = None
            self._set_busy(False)

    def _poll_potplayer(self):
        player = self.potplayer
        if player is None or self.closed:
            return
        data = player.snapshot()
        for event in player.poll():
            if event["kind"] == "cleanup_deferred":
                self._log("专用播放器临时文件清理已延后。", code="player.cleanup_deferred", level="WARN")
                continue
            if event.get("serial") != self.pot_serial:
                continue
            if event["kind"] == "error" and self.active_spec and self.active_spec["backend"] == "potplayer":
                error = event["error"]
                if self.controller.current(self.pot_token):
                    self.controller.fail(self.pot_token, error)
                    self.retry.stop()  # Control failures need explicit user action.
                continue
            if event["kind"] in ("launched", "reopen_requested") and self.controller.current(self.pot_token):
                self.controller.publish(self.pot_token, "player." + event["kind"], "PotPlayer 专用播放请求已发送。")
        spec = self.active_spec
        if spec is None or spec["backend"] != "potplayer":
            return
        state = data.get("state")
        if state == "closed":
            if not self.busy or not self.pot_expected_absence:
                self.stop_playback(stop_engine=False)
                self._log("PotPlayer 专用窗口已关闭，已取消直播与自动恢复。", code="player.user_closed")
            return
        if data.get("load_serial") != self.pot_serial:
            return
        if isinstance(data.get("volume"), (int, float)):
            self.config["volume"] = min(100, max(0, data["volume"]))
        if type(data.get("mute")) is bool:
            self.config["mute"] = data["mute"]
        if data.get("source_present") and not data.get("owned_source"):
            self.stop_playback(stop_engine=False)
            self._log("专用播放器已打开其他内容，直播自动恢复已停止。", code="player.source_changed",
                      level="WARN", presentation="banner", scope="player")
            return
        if state in ("playing", "paused", "ended"):
            self.pot_had_graph = True
        if state == "closed_media" and self.pot_had_graph and not (self.busy and self.pot_expected_closed_media):
            self.stop_playback(stop_engine=False)
            self._log("已在 PotPlayer 关闭播放，自动恢复已取消。", code="player.user_stopped")
            return
        if state == "paused":
            if not self.paused_intent:
                self.paused_intent = True
                self.retry.paused = True
                self.retry.due = None
                self._cancel_play_job()
                self.controller.pause()
            self.pot_last_state = state
            return
        if self.paused_intent and state in ("playing", "ended"):
            self.paused_intent = self.retry.paused = False
            self._log("继续直播，正在重新连接最新位置。", code="player.user_resumed")
            self.play(dict(spec))
            return
        if self.busy or not self.controller.current(self.pot_token):
            return
        now = time.monotonic()
        position = data.get("position_seconds")
        if state == "playing" and position is not None and position != self.pot_position:
            previous = self.pot_position
            self.pot_position, self.pot_progress_at = position, now
            if previous is not None and position > previous and data.get("width", 0) > 0 and data.get("height", 0) > 0:
                self.retry.ready()
                self.controller.recovered(self.pot_token)
                if self.controller.state != "playing":
                    self.controller.publish(self.pot_token, "player.progress_verified",
                        "PotPlayer 报告画面尺寸与持续播放进度；请确认画面和声音。", state="playing")
        stalled = state == "playing" and self.pot_progress_at is not None and now - self.pot_progress_at > 15
        if (state == "ended" or stalled) and self.retry.active:
            if self.auto_reconnect.get():
                if self.controller.state not in ("reconnecting", "error", "unavailable"):
                    self.controller.fail(self.pot_token, ViewerError("直播连接结束，正在准备重新连接。",
                                                                     code="player.interrupted"), recovering=True)
                self._schedule_retry()
            else:
                self.retry.due = None
        if self.auto_reconnect.get() and self.retry.consume():
            self.play(dict(spec), automatic=True)
        self.pot_last_state = state

    def _embedded_closed(self):
        self.embedded_window = None
        if self.active_spec is None or self.active_spec["backend"] == "mpv":
            self.stop_playback()
        self._write_config()

    def _set_volume(self, value):
        self.config["volume"] = min(100, max(0, value))
        if self.embedded_window is not None:
            self.embedded_window.player.set_property("volume", self.config["volume"])

    def _set_mute(self, value):
        self.config["mute"] = bool(value)
        if self.embedded_window is not None:
            self.embedded_window.player.set_property("mute", bool(value))
        self._write_config()

    def _toggle_pause(self):
        if self.busy or not self.active_spec or self.active_spec["backend"] != "mpv":
            return
        if self.paused_intent:
            self.paused_intent = self.retry.paused = False
            self.play(dict(self.active_spec))
        else:
            self.paused_intent = self.retry.paused = True
            self.retry.due = None
            self.controller.pause()
            if self.embedded_window is not None:
                self.embedded_window.player.set_property("pause", True)

    def _poll_embedded(self):
        window = self.embedded_window
        if window is None or window.closed:
            return
        events = window.player.poll()
        if any(event.get("kind") == "closed" for event in events):
            window.close()
            return
        if any(event.get("kind") == "reconnect" for event in events) and not self.busy:
            self.reconnect()
        snapshot = window.player.snapshot()
        now = time.monotonic()
        spec = self.active_spec
        if not spec or spec["backend"] != "mpv" or not self.retry.active:
            window.update_status("已停止，可从主窗口选择直播间播放。")
            return
        if snapshot.get("state") == "playing" and not self.busy:
            paused = bool(snapshot.get("paused"))
            if paused and not self.paused_intent:
                self.paused_intent = self.retry.paused = True
                self.retry.due = None
                self.controller.pause()
            elif not paused and self.paused_intent:
                # Native OSC/keyboard resume must use a fresh live connection.
                self.paused_intent = self.retry.paused = False
                self.play(dict(spec))
                return
        for key, value in (("volume", window.player.props.get("volume")), ("mute", window.player.props.get("mute"))):
            if value is not None:
                self.config[key] = value
        position = snapshot.get("position_seconds")
        if snapshot["state"] == "playing" and position is not None and position != self.native_position:
            self.native_position, self.native_progress_at = position, now
            if snapshot["video_output_ready"]:
                self.retry.ready()
                if position > 0.1 and not self.paused_intent and not self.busy:
                    self.controller.decoded(self.play_token)
        if not self.auto_reconnect.get():
            self.retry.due = None
        elif not self.paused_intent and not self.busy:
            stalled = self.native_progress_at is not None and now - self.native_progress_at > 12
            loading_timeout = (snapshot["state"] == "loading" and self.native_load_started is not None
                               and now - self.native_load_started > 120)
            if snapshot["state"] in ("ended", "error") or stalled or loading_timeout:
                if self.play_token is not None and self.controller.state not in ("reconnecting", "error", "unavailable"):
                    self.controller.fail(self.play_token, ViewerError("播放器连接中断，正在准备重新连接。",
                                                                     code="player.interrupted"), recovering=True)
                self._schedule_retry()
            if self.retry.consume():
                self.play(dict(spec), automatic=True)
        if self.paused_intent:
            text = "暂停中；点击继续直播会回到最新位置。"
        elif self.busy:
            text = "正在解析直播并连接…"
        elif self.retry.due is not None:
            wait = max(0, int(self.retry.due - now + 1))
            text = f"连接中断，{wait} 秒后自动重连（第 {self.retry.attempts} 次）。"
        elif snapshot["state"] in ("ended", "error"):
            text = "直播连接已结束；可点击回到直播重新连接。"
        elif snapshot["video_output_ready"]:
            text = f"播放中 · {snapshot['width']}×{snapshot['height']} · {snapshot.get('fps') or 0:g} fps"
            cache = snapshot.get("cache_seconds")
            if cache is not None:
                text += f" · 缓冲约 {cache:.1f} 秒"
        else:
            text = "正在加载画面…"
        window.update_status(spec["room_name"] + " · " + text, self.paused_intent)

    def _schedule_retry(self):
        previous = self.retry.due
        self.retry.failure()
        if previous is None and self.retry.due is not None and self.play_token is not None:
            self.controller.publish(self.play_token, "playback.retry_scheduled",
                                    f"已安排第 {self.retry.attempts} 次重连。", level="WARN",
                                    details={"attempt": self.retry.attempts})

    def play(self, request=None, *, automatic=False):
        if self.busy or self.closed:
            return
        try:
            if request is None:
                platform, room, requested = self._selection()
                request = {"platform": platform, "room_input": room, "requested": requested,
                           "room_name": self.room_name.get().strip()[:80] or self._saved_room_name(platform, room),
                           "session": self.mode.get(), "backend": BACKENDS[self.backend.get()],
                           "low_latency": self.low_latency.get(), "network_mode": NETWORK_MODES[self.network_mode.get()]}
            else:
                request = dict(request)
            request.setdefault("network_mode", "direct")
            platform, room, requested = request["platform"], request["room_input"], request["requested"]
            backend = request["backend"]
            executable = Path(self.player.get().strip().strip('"'))
            if backend == "potplayer" and (not executable.is_file() or executable.suffix.lower() != ".exe"):
                raise ViewerError("请先选择已安装的 PotPlayer.exe。", code="player.path_invalid")
            jar = self.cookies.get(platform) if request["session"] == "导入 Cookie" else None
            if request["session"] == "导入 Cookie" and jar is None:
                self.retry.stop()
                raise ViewerError("当前平台登录已清除，请重新登录或选择匿名播放。", code="account.missing", level="WARN")
            if backend == "mpv":
                self._ensure_embedded()
            else:
                pot = self._ensure_potplayer()
                prior = pot.snapshot()
                self.pot_expected_absence = not automatic and (prior.get("state") in ("idle", "closed") or not pot.ever_launched)
                self.pot_expected_closed_media = not automatic and prior.get("state") == "closed_media"
        except ViewerError as exc:
            self._log(str(exc), code=exc.code, level=exc.level, presentation="strong",
                      impact="start_blocked", scope="playback.command")
            return
        self.stop_playback(stop_engine=False, keep_intent=True)
        if backend == "potplayer" and self.embedded_window is not None and not self.embedded_window.closed:
            self.embedded_window.player.stop()
        if backend == "mpv" and self.potplayer is not None:
            self.potplayer.stop()
        self.activity.clear("playback.command")
        token = self.play_token = self.controller.begin(request, automatic=automatic)
        self.play_generation = token.generation
        self.active_spec = request
        if not automatic:
            self.retry.start()
            self.retry.active = True
        self.paused_intent = self.retry.paused = False
        self.native_load_started = self.native_progress_at = None
        self.native_position = None
        self.confirmed.set(False)
        if not automatic:
            self.note.set("")
            self._write_config()
        self.last_playback = dict(request)

        def failed(error):
            if not self.controller.current(token):
                return
            self.last_playback["status"] = str(error)
            blocked = error.code in ("room.offline", "account.expired", "account.missing") or error.code.startswith("player.")
            recovering = self.auto_reconnect.get() and not blocked
            if blocked:
                self.retry.stop()
            self.controller.fail(token, error, recovering=recovering)
            if recovering:
                self._schedule_retry()

        def start():
            if backend == "potplayer":
                pot.prepare()
            return self.playback_service.prepare(request, jar, token,
                                                  endpoint=self.pot_endpoint if backend == "potplayer" else None,
                                                  start=False)

        def done(result):
            relay, stream = result
            if not self.controller.current(token):
                relay.stop()
                return
            self.relay = relay
            self.history_recorded = False
            self.last_playback["requested"] = stream.requested
            try:
                relay.start()
                if backend == "potplayer":
                    if pot.snapshot().get("state") == "closed" and not self.pot_expected_absence:
                        self.stop_playback(stop_engine=False)
                        relay.stop()
                        return
                    self.pot_token = token
                    self.pot_position = self.pot_progress_at = None
                    self.pot_had_graph = False
                    self.pot_serial = pot.open(relay.url, request["room_name"], volume=self.config["volume"],
                                               mute=self.config["mute"],
                                               guard=lambda: self.controller.current(token) and not self.paused_intent,
                                               allow_new_process=self.pot_expected_absence or not pot.ever_launched)
                else:
                    window = self._ensure_embedded()
                    window.player.load(relay.url)
                    self.native_load_started = time.monotonic()
                    if not automatic:
                        window.show(request["room_name"])
            except (ViewerError, OSError) as exc:
                relay.stop();self.relay = None
                failed(exc if isinstance(exc, ViewerError) else
                       ViewerError("无法启动播放器，请检查所选程序路径。", code="player.start_failed"))
                return
            self.controller.publish(token, "player.open_requested",
                                    f"已向播放器发送 {request['room_name']}；预检查返回 {stream.actual_name}。",
                                    state="connecting")
        self._drain_activity()
        self._work(start, done, failed, code="playback.prepare", target=token.target, play_token=token)

    def reconnect(self):
        if self.active_spec is not None:
            self.play(dict(self.active_spec))
        elif self.last_playback:
            previous = self.last_playback
            request = {k: previous[k] for k in ("platform", "room_input", "requested", "session", "room_name", "backend", "low_latency")}
            request["network_mode"] = previous.get("network_mode", "direct")
            self.play(request)
        else:
            self._log("请先播放一个房间，再使用回到直播。")

    def playback_report(self):
        result = dict(self.last_playback)
        if self.relay:
            result.update(self.relay.snapshot())
        if self.embedded_window is not None and (self.relay is not None or self.active_spec is not None):
            native = self.embedded_window.player.snapshot()
            result["embedded_player"] = native
            result["decoded_verified"] = bool(native["video_output_ready"] and (native.get("position_seconds") or 0) > 0.1)
            result["auto_reconnect_attempts"] = self.retry.total_attempts
        if self.potplayer is not None and result.get("backend") == "potplayer":
            result["potplayer"] = self.potplayer.snapshot()
            result["decoded_verified"] = False
            result["auto_reconnect_attempts"] = self.retry.total_attempts
        result["picture_and_audio_confirmed_by_user"] = bool(self.confirmed.get() and result.get("bytes_forwarded", 0) > 0)
        result["user_note"] = self.note.get()
        if self.network_result is not None:
            result["network_check"] = self.network_result
        return result

    def stop_playback(self, *, stop_engine=True, keep_intent=False):
        previous_report = self.playback_report() if self.relay is not None else None
        self.play_generation += 1
        if not keep_intent:
            self.controller.stop(closing=self.closed)
            self.play_token = None
            if self.pending_play_token is not None:
                self.operations.cancel("playback.prepare")
                self.operation_token = self.pending_play_token = None
                if not self.closed:
                    self._set_busy(False)
            self.active_spec = None
            self.paused_intent = False
        if stop_engine and self.embedded_window is not None and not self.embedded_window.closed:
            self.embedded_window.player.stop()
        if stop_engine and self.potplayer is not None:
            self.potplayer.stop()
        if self.relay:
            self.last_playback = previous_report
            self.relay.stop()
            self.relay = None
            self.last_playback["status"] = "已停止"
        if not keep_intent:
            self.playback_status.set(self.controller.message)
            self._drain_activity()

    def export(self):
        if not self.rows and not self.last_playback and self.relay is None and self.network_result is None:
            self._log("请先执行画质对比或启动实播。")
            return
        path = filedialog.asksaveasfilename(title="导出不含凭据的测试报告", defaultextension=".json", initialfile="live-test-report.json", filetypes=[("JSON 报告", "*.json")])
        if path:
            try:
                save_report(path, self.rows, self.playback_report())
                self._log("测试报告已导出。", code="diagnostic.report_exported")
            except OSError:
                self._log("报告保存失败，请选择可写目录。", code="diagnostic.export_failed",
                          level="ERROR", presentation="banner", scope="diagnostic.export")

    def close(self):
        self.closed = True
        self.operations.close()
        if self.qr_dialog is not None:
            self.qr_dialog.close()
        if self.poll_after is not None:
            self.root.after_cancel(self.poll_after)
            self.poll_after = None
        self.stop_playback()
        self.cookies.clear()
        try:
            while True:
                _, result, _, _, _, _ = self.events.get_nowait()
                if isinstance(result, tuple) and isinstance(result[0], LocalRelay):
                    result[0].stop()
        except queue.Empty:
            pass
        if self.embedded_window is not None:
            self.embedded_window.close(notify=False)
            self.embedded_window = None
        self.pot_endpoint.close()
        if self.potplayer is not None:
            self.potplayer.close(wait=True)
        self._write_config()
        self.activity.emit("app.closed", "程序已关闭。")
        self.activity.close()
        self.root.destroy()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="本地直播观看")
    parser.add_argument("--data-dir", type=Path, help="设置与加密登录的保存目录")
    parser.add_argument("--self-test", type=Path, help="运行离线打包检查，结果写入指定 JSON 文件")
    parser.add_argument("--live-test", action="store_true", help="自检时额外验证公开房间的短时取流和本机转发")
    parser.add_argument("--qr-test", action="store_true", help="自检时额外验证生成二维码和未扫码状态，不登录账号")
    parser.add_argument("--mpv-test", action="store_true", help="自检时静音实播内置播放器并模拟转发断流恢复")
    args = parser.parse_args()
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (OSError, AttributeError):
            pass
    if args.self_test:
        from .diagnostics import packaged_check
        packaged_check(args.self_test, live=args.live_test, qr=args.qr_test, mpv=args.mpv_test)
    else:
        root = tk.Tk()
        ViewerApp(root, args.data_dir)
        root.mainloop()

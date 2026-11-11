"""Qt frontend for live playback, accounts, diagnostics and activity logs."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QInputDialog, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QPlainTextEdit, QSizePolicy, QStackedWidget, QStyle, QTableView,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..core import ViewerError, cookies_from_text, load_cookies, room_number
from ..credentials import CredentialStore
from ..accounts import CredentialWriter, verify_login
from ..events import EventCenter, LEVELS, safe_text
from ..reports import connection_check, compare_quality, save_diagnostic_report, clean, REPORT
from ..network import NETWORK_MODES
from ..runtime import LiveRuntime
from ..settings import PLATFORMS, StateStore
from .logs import LogFilter, LogModel, SOURCES
from .widgets import button, card, line_icon, row, scrolling_page, text
from .qr_dialog import QrLoginDialog
from .notifications import NoticeManager
from .vod_page import VodPage
from .bangumi_page import BangumiPage

VERSION = "0.9.0"
STATES = {"idle": "准备观看", "resolving": "正在解析直播", "connecting": "正在连接播放器",
          "playing": "正在播放", "paused": "已暂停", "reconnecting": "正在恢复连接",
          "unavailable": "房间未开播", "error": "播放遇到问题", "stopped": "已停止", "ended": "播放完毕"}
CAPTIONS = ("选择收藏，或输入房间号开始观看。", "按平台管理登录，账号与播放状态分别呈现。",
            "播放器、网络、恢复与通知选项。", "筛选活动，追踪会话，查看和导出技术详情。",
            "检查接口与线路，对比匿名和登录后的实际画质。", "播放 B 站普通视频，选择分 P 与可用画质。",
            "选择季度与剧集，查看账号资格、收藏作品并继续观看。")


def detect_player():
    if os.name == "nt":
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\PotPlayerMini64.exe") as key:
                    candidate = winreg.QueryValue(key, None).strip('"')
                    if Path(candidate).is_file():
                        return candidate
            except OSError:
                pass
    for base in (os.environ.get("ProgramFiles", ""),):
        for suffix in ("DAUM/PotPlayer/PotPlayerMini64.exe", "PotPlayer/PotPlayerMini64.exe"):
            candidate = Path(base) / suffix
            if candidate.is_file():
                return str(candidate)
    return ""


class ViewerWindow(QMainWindow):
    PAGE_TITLES = ("观看", "账号", "设置", "活动日志", "诊断", "B 站视频", "B 站番剧")

    def __init__(self, data_dir=None, *, runtime_factory=LiveRuntime):
        super().__init__()
        self.store = StateStore(data_dir)
        self.config = self.store.load()
        self.config["player"] = self.config["player"] or detect_player()
        self.activity = EventCenter(self.store.directory / "logs")
        self.credentials = CredentialStore(self.store.directory)
        self.credential_writer = CredentialWriter(self.credentials)
        self.cookies = {}
        self.runtime = runtime_factory(self.activity, self.store.directory, self.config, self.cookies, persist=self.save_settings)
        self.closed = self.closing = False
        self.shutdown_done = threading.Event()
        self.strong_dialog = self.strong_event = self.notice_event = None
        self.notification_count = 0
        self.last_history_keys = self.last_quality_stream = None
        self.qr_dialog = None
        self.known_qualities = {}
        self.diagnostic_cancels = {}
        self.connection_result = None
        self.comparison_rows = []
        self.comparison_request = {}
        self.setWindowTitle("本地直播 · " + VERSION)
        self.setMinimumSize(920, 620)
        available = self.screen().availableGeometry()
        self.resize(min(1240, max(920, available.width() - 40)), min(850, max(620, available.height() - 50)))
        self._build()
        self.notices = NoticeManager(self, self.activity)
        self.choose_room(self.config["last_room"], log=False)
        self.refresh_rooms()
        self.activity.emit("app.started", "Qt 前端已启动。")
        if self.store.warning:
            self.activity.emit("storage.restore_warning", self.store.warning, source="storage", level="WARN",
                               presentation="banner", scope="storage")
        for platform, enabled in self.config["remember_login"].items():
            if enabled:
                self.restore_login(platform)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.tick)
        self.poll_timer.start(200)
        self.tick()

    def _build(self):
        root = QWidget()
        root.setObjectName("Root")
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.setCentralWidget(root)
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(190)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(18, 28, 18, 22)
        mark = text("∿", "BrandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(42, 42)
        nav.addLayout(row(mark, text("本地直播", "Brand")))
        nav.addWidget(text("桌面观看 · PotPlayer", "Caption"))
        nav.addSpacing(28)
        self.navigation = QButtonGroup(self)
        self.nav_buttons = []
        for i, title in enumerate(self.PAGE_TITLES):
            btn = button(title, lambda i=i: self.go_page(i), "Nav")
            btn.setCheckable(True)
            self.navigation.addButton(btn)
            self.nav_buttons.append(btn)
            nav.addWidget(btn)
        nav.addStretch()
        nav.addWidget(text(VERSION + " · Qt", "Muted"))
        nav.addWidget(text("收藏与设置保存在本机", "Caption", True))
        shell.addWidget(sidebar)
        main = QWidget()
        layout = QVBoxLayout(main)
        layout.setContentsMargins(26, 23, 22, 12)
        layout.setSpacing(16)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.page_title = text("观看", "Title")
        self.page_subtitle = text(CAPTIONS[0], "Subtitle", True)
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_subtitle)
        header.addLayout(heading, 1)
        self.global_status = text("准备观看", "Pill")
        header.addWidget(self.global_status)
        layout.addLayout(header)

        self.notice = QFrame()
        self.notice.setObjectName("Notice")
        box = QVBoxLayout(self.notice)
        box.setContentsMargins(18, 14, 18, 14)
        box.setSpacing(8)
        self.notice_level = text("", "Level")
        self.notice_title = text("", "NoticeTitle", True)
        self.notice_title.setMinimumWidth(0)
        self.notice_body = text("", "NoticeBody", True)
        self.notice_body.setTextFormat(Qt.TextFormat.PlainText)
        self.notice_details = text("", "NoticeDetails", True)
        self.notice_details.setTextFormat(Qt.TextFormat.PlainText)
        self.notice_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notice_details.hide()
        self.notice_retry = button("重试", self.reconnect, "Primary")
        self.notice_check = button("检查连接", self.notice_diagnose)
        box.addLayout(row(self.notice_level, self.notice_title, button("查看日志", self.open_notice_log),
                          self.notice_check, self.notice_retry))
        box.addWidget(self.notice_body)
        self.notice_detail_button = button("展开技术详情", self.toggle_notice_details)
        self.notice_detail_button.setMaximumWidth(140)
        box.addWidget(self.notice_detail_button)
        box.addWidget(self.notice_details)
        self.notice.hide()
        layout.addWidget(self.notice)
        self.stack = QStackedWidget()
        self.vod_page = VodPage(self)
        self.bangumi_page = BangumiPage(self)
        for page in (self._watch_page(), self._account_page(), self._settings_page(), self._logs_page(), self._diagnostics_page(), self.vod_page, self.bangumi_page):
            self.stack.addWidget(page)
        layout.addWidget(self.stack, 1)
        footer = QFrame()
        footer.setObjectName("Footer")
        bottom = QHBoxLayout(footer)
        bottom.setContentsMargins(0, 10, 0, 0)
        self.last_activity = text("活动已记录", "Caption")
        self.last_activity.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bottom.addWidget(self.last_activity, 1)
        self.unread_button = button("未读提醒 0", self.open_unread)
        bottom.addWidget(self.unread_button)
        bottom.addWidget(button("打开日志", lambda: self.go_page(3)))
        layout.addWidget(footer)
        shell.addWidget(main, 1)
        self.go_page(0, log=False)

    def _watch_page(self):
        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        saved, box = card("我的房间", "以平台与房间号为索引")
        saved.setMinimumWidth(190)
        saved.setMaximumWidth(250)
        self.room_search = QLineEdit()
        self.room_search.setPlaceholderText("搜索名称或房间号")
        self.room_search.textChanged.connect(self.refresh_rooms)
        box.addWidget(self.room_search)
        self.room_tab = QComboBox()
        self.room_tab.addItems(["收藏", "最近观看"])
        self.room_tab.currentIndexChanged.connect(self.refresh_rooms)
        box.addWidget(self.room_tab)
        self.room_list = QListWidget()
        self.room_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.room_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.room_list.setWordWrap(True)
        self.room_list.currentItemChanged.connect(self.select_room)
        self.room_list.itemDoubleClicked.connect(lambda *_: self.start())
        box.addWidget(self.room_list, 1)
        box.addWidget(button("保存当前房间", self.save_room))
        box.addWidget(button("删除所选", self.delete_room))
        self.clear_history_button = button("清空最近观看", self.clear_history)
        box.addWidget(self.clear_history_button)
        layout.addWidget(saved, 1)
        scroll, content = scrolling_page()
        self.status_card, box = card(name="StatusCard")
        self.status_room = text("尚未开始实播", "SectionTitle", True)
        self.status_room.setTextFormat(Qt.TextFormat.PlainText)
        self.status_title = text("准备观看", "StatusTitle", True)
        self.status_context = text("", "StatusContext", True)
        self.status_context.setTextFormat(Qt.TextFormat.PlainText)
        for label in (self.status_room, self.status_title, self.status_context):
            box.addWidget(label)
        self.stream_info = text("PotPlayer · 实际画质在取流后显示", "Muted", True)
        box.addWidget(self.stream_info)
        self.start_button = button("开始播放", self.start, "Primary", QStyle.StandardPixmap.SP_MediaPlay)
        self.pause_button = button("暂停", self.pause, icon=QStyle.StandardPixmap.SP_MediaPause)
        self.stop_button = button("停止", self.stop, "Danger", QStyle.StandardPixmap.SP_MediaStop)
        self.reconnect_button = button("回到直播", self.reconnect, icon=QStyle.StandardPixmap.SP_BrowserReload)
        box.addLayout(row(self.start_button, self.pause_button, self.stop_button, self.reconnect_button))
        self.confirmed = QCheckBox("我已确认当前直播有画面和声音")
        self.note = QLineEdit()
        self.note.setMaxLength(500)
        self.note.setPlaceholderText("可选：实播观察，随诊断报告导出")
        box.addWidget(self.confirmed)
        box.addWidget(self.note)
        content.addWidget(self.status_card)
        source, box = card("打开直播间")
        self.platform_box = QComboBox()
        for caption, platform in PLATFORMS.items():
            self.platform_box.addItem(caption, platform)
        self.platform_box.currentIndexChanged.connect(self.input_changed)
        self.room_input = QLineEdit()
        self.room_input.setPlaceholderText("输入房间号或直播间链接")
        self.room_input.returnPressed.connect(self.start)
        self.room_input.editingFinished.connect(self.input_changed)
        box.addLayout(row(self.platform_box, self.room_input))
        self.room_name = QLineEdit()
        self.room_name.setMaxLength(80)
        self.room_name.setPlaceholderText("自定义房间名")
        box.addLayout(row(text("房间名称", "Muted"), self.room_name, button("保存 / 改名", self.save_room)))
        content.addWidget(source)
        options, box = card("观看选项")
        self.quality_box = QComboBox()
        self.quality_box.addItem("原画优先", None)
        self.mode_box = QComboBox()
        self.mode_box.addItem("匿名观看", "匿名")
        self.mode_box.addItem("使用已登录账号", "导入 Cookie")
        box.addLayout(row(text("画质", "Muted"), self.quality_box, text("账号", "Muted"), self.mode_box))
        box.addWidget(text("开启回看时，暂停后继续保持原位置；“回到直播”跳到直播附近，保留回看缓存。",
                           "Muted", True))
        content.addWidget(options)
        content.addStretch()
        layout.addWidget(scroll, 3)
        return host

    def _account_page(self):
        scroll, layout = scrolling_page()
        self.account_status = {}
        self.remember_boxes = {}
        self.account_check_buttons = {}
        for caption, platform in PLATFORMS.items():
            frame, box = card(caption)
            label = text("未登录 · 可匿名观看", "StatusContext", True)
            self.account_status[platform] = label
            box.addWidget(label)
            box.addLayout(row(button("扫码登录", lambda p=platform: self.qr_login(p), "Primary"),
                              button("导入 Cookie", lambda p=platform: self.import_cookie(p)),
                              button("粘贴 Cookie", lambda p=platform: self.paste_cookie(p)),
                              button("清除登录", lambda p=platform: self.clear_login(p))))
            remembered = QCheckBox("记住此平台登录（Windows 当前用户加密）")
            remembered.setChecked(self.config["remember_login"][platform])
            remembered.toggled.connect(lambda enabled, p=platform: self.toggle_remember_login(p, enabled))
            self.remember_boxes[platform] = remembered
            box.addWidget(remembered)
            check = button("检查登录状态", lambda p=platform: self.check_login(p))
            self.account_check_buttons[platform] = check
            box.addWidget(check)
            box.addWidget(text("关闭记忆会删除已保存登录，本次会话仍可使用。B 站可验证登录；斗鱼有效性尚未独立验证。",
                               "Muted", True))
            layout.addWidget(frame)
        layout.addStretch()
        return scroll

    def _settings_page(self):
        scroll, layout = scrolling_page()
        frame, box = card("播放器", "使用本工具创建的 PotPlayer 专用会话；播放器拥有自己的全屏与进度条。")
        self.player_path = QLineEdit(self.config["player"])
        self.player_path.editingFinished.connect(self.update_options)
        box.addLayout(row(self.player_path, button("选择程序", self.choose_player)))
        box.addWidget(text("请选择 64 位 PotPlayerMini64.exe。原安装目录与原播放器的个人配置保留。",
                           "Muted", True))
        self.replay_box = QCheckBox("使用 PotPlayer 原生进度条回看直播")
        self.replay_box.setChecked(self.config["native_replay"])
        self.replay_minutes_box = QComboBox()
        for value in (10, 30, 60, 180, 300):
            self.replay_minutes_box.addItem(f"{value} 分钟", value)
        if self.replay_minutes_box.findData(self.config["replay_minutes"]) < 0:
            self.replay_minutes_box.addItem(f"{self.config['replay_minutes']} 分钟", self.config["replay_minutes"])
        self.replay_minutes_box.setCurrentIndex(self.replay_minutes_box.findData(self.config["replay_minutes"]))
        self.vsr_box = QCheckBox("NVIDIA 视频超分（内置 D3D11，支持的 RTX 显卡）")
        self.vsr_box.setChecked(self.config["vsr"])
        for widget in (self.replay_box, self.vsr_box):
            widget.toggled.connect(self.update_options)
        self.replay_minutes_box.currentIndexChanged.connect(self.update_options)
        box.addWidget(self.replay_box)
        box.addLayout(row(text("最长回看范围", "Muted"), self.replay_minutes_box))
        box.addWidget(self.vsr_box)
        box.addWidget(text("这些选项在下次播放生效。原生回看由播放器管理，可能占用较多内存；临时文件隔离在 data/cache/live，关闭后清理。超分需视频实际显示并放大。",
                           "Muted", True))
        layout.addWidget(frame)
        frame, box = card("网络连接")
        self.network_box = QComboBox()
        for caption, mode in NETWORK_MODES.items():
            self.network_box.addItem(caption, mode)
        self.network_box.setCurrentIndex(self.network_box.findData(self.config["network_mode"]))
        self.network_box.currentIndexChanged.connect(self.update_options)
        box.addLayout(row(text("平台连接方式", "Muted"), self.network_box))
        box.addWidget(text("直连优先 IPv4，保留 IPv6 回退与证书校验。网络与缓冲设置在下次连接生效。",
                           "Muted", True))
        layout.addWidget(frame)
        frame, box = card("恢复与通知")
        self.auto_reconnect = QCheckBox("断流后自动恢复")
        self.auto_reconnect.setChecked(self.config["auto_reconnect"])
        self.low_latency = QCheckBox("优先实时播放，减少转发积压")
        self.low_latency.setChecked(self.config["low_latency"])
        self.strong_notifications = QCheckBox("重要故障显示提醒窗口，同时保留固定告示")
        self.strong_notifications.setChecked(self.config["ui"]["strong_notifications"])
        self.background_notifications = QCheckBox("后台播放故障使用系统提醒，并保留未读告示")
        self.background_notifications.setChecked(self.config["ui"]["background_notifications"])
        for widget in (self.auto_reconnect, self.low_latency, self.strong_notifications, self.background_notifications):
            widget.toggled.connect(self.update_options)
            box.addWidget(widget)
        box.addWidget(text("主动暂停、停止或关闭播放器后不自动启动。恢复提醒不会逐次弹窗。", "Muted", True))
        layout.addWidget(frame)
        frame, box = card("本地数据与日志")
        directory = text(str(self.store.directory), "StatusContext", True)
        directory.setTextFormat(Qt.TextFormat.PlainText)
        directory.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        box.addWidget(directory)
        box.addWidget(text("设置自动保存。日志分级落盘、轮转，界面保留最近 2,000 条可筛选事件。", "Muted", True))
        box.addWidget(button("打开日志目录", self.open_log_directory))
        layout.addWidget(frame)
        layout.addStretch()
        return scroll

    def _logs_page(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.level_filter = QComboBox()
        self.level_filter.addItem("全部等级", "")
        for level in LEVELS:
            self.level_filter.addItem(level, level)
        self.source_filter = QComboBox()
        self.source_filter.addItem("全部来源", "")
        for source, caption in SOURCES.items():
            self.source_filter.addItem(caption, source)
        self.target_filter = QLineEdit()
        self.target_filter.setPlaceholderText("房间 / 目标")
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("搜索事件、内容或会话")
        layout.addLayout(row(self.level_filter, self.source_filter, self.target_filter, self.log_search))
        self.log_model = LogModel(self.activity.store.read(), self)
        self.log_filter = LogFilter(self)
        self.log_filter.setSourceModel(self.log_model)
        self.log_table = QTableView()
        self.log_table.setModel(self.log_filter)
        self.log_table.verticalHeader().hide()
        self.log_table.setShowGrid(False)
        self.log_table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.log_table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.log_table.verticalHeader().setDefaultSectionSize(42)
        for i, width in enumerate((128, 76, 76, 145)):
            self.log_table.setColumnWidth(i, width)
        self.log_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.log_table.selectionModel().selectionChanged.connect(self.show_log_detail)
        layout.addWidget(self.log_table, 3)
        self.log_detail = QPlainTextEdit()
        self.log_detail.setReadOnly(True)
        self.log_detail.setMaximumHeight(155)
        self.log_detail.setPlaceholderText("选择日志，查看完整时间、事件代码、播放会话和公开技术详情。")
        layout.addWidget(self.log_detail, 1)
        controls = QHBoxLayout()
        self.log_count = text("", "Caption")
        controls.addWidget(self.log_count, 1)
        controls.addWidget(button("清除筛选", self.reset_filters))
        self.copy_button = button("复制详情", self.copy_log)
        controls.addWidget(self.copy_button)
        self.export_button = button("导出当前筛选", self.export_logs)
        controls.addWidget(self.export_button)
        layout.addLayout(controls)
        for widget in (self.level_filter, self.source_filter):
            widget.currentIndexChanged.connect(self.filter_logs)
        for widget in (self.target_filter, self.log_search):
            widget.textChanged.connect(self.filter_logs)
        return host

    def _diagnostics_page(self):
        scroll, layout = scrolling_page()
        frame, box = card("连接与画质检查", "短时采样检查接口和 FLV 数据，不启动播放器。")
        self.diagnostic_target = QComboBox()
        self.diagnostic_target.addItems(["观看页输入的房间", "当前 / 上次播放会话"])
        box.addLayout(row(text("检查对象", "Muted"), self.diagnostic_target))
        self.connection_button = button("检查连接", self.check_connection, "Primary")
        self.compare_button = button("对比匿名 / 登录画质", self.compare)
        self.cancel_diagnostic_button = button("取消检查", self.cancel_diagnostics)
        box.addLayout(row(self.connection_button, self.compare_button, self.cancel_diagnostic_button))
        self.diagnostic_status = text("尚未检查。画质对比可能需要几十秒。", "StatusContext", True)
        self.connection_details = QPlainTextEdit()
        self.connection_details.setReadOnly(True)
        self.connection_details.setMaximumHeight(165)
        self.connection_details.setPlaceholderText("连接结果会列出检查阶段、公开域名与采样信息。")
        box.addWidget(self.diagnostic_status)
        box.addWidget(self.connection_details)
        layout.addWidget(frame)
        frame, box = card("画质对比")
        self.comparison_caption = text("尚未进行采样", "Muted", True)
        box.addWidget(self.comparison_caption)
        self.comparison_table = QTableWidget(0, 4)
        self.comparison_table.setHorizontalHeaderLabels(["会话", "实际画质 / 元数据", "采样码率", "结果"])
        self.comparison_table.verticalHeader().hide()
        self.comparison_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.comparison_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.comparison_table.setShowGrid(False)
        for i, width in enumerate((105, 165, 100)):
            self.comparison_table.setColumnWidth(i, width)
        self.comparison_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.comparison_table.setMinimumHeight(180)
        box.addWidget(self.comparison_table)
        box.addWidget(text("码率是短时媒体数据采样值，会随场景变化；元数据中的帧率不等于实测播放帧率。",
                           "Muted", True))
        layout.addWidget(frame)
        frame, box = card("当前播放会话")
        self.diagnostic_info = QPlainTextEdit()
        self.diagnostic_info.setReadOnly(True)
        self.diagnostic_info.setMinimumHeight(160)
        self.diagnostic_info.setMaximumHeight(180)
        box.addWidget(self.diagnostic_info)
        box.addWidget(text("尺寸与播放进度来自 PotPlayer；画面、声音和实际延迟仍需观看确认。", "Muted", True))
        layout.addWidget(frame)
        self.report_button = button("导出诊断报告", self.export_report, "Primary")
        box.addLayout(row(self.report_button, button("查看活动日志", lambda: self.go_page(3))))
        box.addWidget(text("报告仅含脱敏摘要、短时采样与实播观察。NVIDIA 视频超分将在 P5 验证。", "Muted", True))
        layout.addWidget(frame)
        layout.addStretch()
        return scroll

    def go_page(self, index, *, log=True):
        self.stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        for i, btn in enumerate(self.nav_buttons):
            btn.setIcon(line_icon(("play", "account", "settings", "logs", "diagnostics", "play", "play")[i],
                                  "#386cf4" if i == index else "#6b7890"))
        self.page_title.setText(self.PAGE_TITLES[index])
        self.page_subtitle.setText(CAPTIONS[index])
        if log:
            self.activity.emit("ui.page_opened", "已打开" + self.PAGE_TITLES[index] + "页。", level="DEBUG")

    def save_settings(self):
        try:
            self.store.save(self.config)
            self.activity.clear("storage")
            return True
        except (OSError, ValueError):
            self.activity.emit("storage.save_failed", "设置保存失败，请检查数据目录是否可写。", source="storage",
                               level="ERROR", presentation="strong", scope="storage")
            return False

    def update_options(self, *_):
        previous_auto = self.config["auto_reconnect"]
        previous_route = self.config["network_mode"]
        changed = (self.config["player"] != self.player_path.text().strip() or
                   self.config["network_mode"] != self.network_box.currentData() or
                   self.config["auto_reconnect"] != self.auto_reconnect.isChecked() or
                   self.config["low_latency"] != self.low_latency.isChecked() or
                   self.config["ui"]["strong_notifications"] != self.strong_notifications.isChecked())
        self.config.update(player=self.player_path.text().strip(), network_mode=self.network_box.currentData(),
                           auto_reconnect=self.auto_reconnect.isChecked(), low_latency=self.low_latency.isChecked())
        extra_changed = (self.config["native_replay"] != self.replay_box.isChecked()
                         or self.config["replay_minutes"] != self.replay_minutes_box.currentData()
                         or self.config["vsr"] != self.vsr_box.isChecked()
                         or self.config["ui"]["background_notifications"] != self.background_notifications.isChecked())
        self.config.update(native_replay=self.replay_box.isChecked(), replay_minutes=self.replay_minutes_box.currentData(),
                           vsr=self.vsr_box.isChecked())
        self.low_latency.setEnabled(not self.replay_box.isChecked())
        self.low_latency.setToolTip("原生回看需要持续接收直播；关闭回看时可选择实时积压限制。")
        self.config["ui"]["background_notifications"] = self.background_notifications.isChecked()
        self.config["ui"]["strong_notifications"] = self.strong_notifications.isChecked()
        if previous_auto != self.config["auto_reconnect"]:
            self.runtime.set_auto_reconnect(self.config["auto_reconnect"])
        if previous_route != self.config["network_mode"]:
            self.vod_page.invalidate_tracks()
            self.bangumi_page.invalidate_tracks()
        if self.qr_dialog is not None:
            self.qr_dialog.network_mode = self.config["network_mode"]
        if not self.strong_notifications.isChecked():
            self.close_strong_notice()
        if (changed or extra_changed) and self.save_settings():
            self.activity.emit("settings.saved", "设置已保存；网络与缓冲选项在下次连接生效。", source="settings")

    def choose_player(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 64 位 PotPlayer", self.player_path.text(), "程序 (*.exe)")
        if path:
            self.player_path.setText(path)
            self.update_options()

    def read_room(self):
        platform = self.platform_box.currentData()
        number = room_number(platform, self.room_input.text())
        favorite = next((r for r in self.config["favorites"] if (r["platform"], r["room"]) == (platform, number)), None)
        name = self.room_name.text().strip() or (favorite["name"] if favorite else self.platform_box.currentText() + " · " + number)
        return {"platform": platform, "room": number, "name": name[:80]}

    def input_changed(self, *_):
        if not hasattr(self, "quality_box"):
            return
        self.quality_box.clear()
        self.quality_box.addItem("原画优先", None)
        try:
            platform, number = self.platform_box.currentData(), room_number(self.platform_box.currentData(), self.room_input.text())
        except ViewerError:
            return
        preference = self.config["room_preferences"].get(platform + ":" + number, {})
        known = self.known_qualities.get((platform, number), [])
        for item in known:
            self.quality_box.addItem(safe_text(item.name), item.value)
        quality = preference.get("quality")
        if quality is not None and self.quality_box.findData(quality) < 0:
            self.quality_box.addItem(f"上次选择 ({quality})", quality)
        self.quality_box.setCurrentIndex(max(0, self.quality_box.findData(quality)))
        self.mode_box.setCurrentIndex(self.mode_box.findData(preference.get("mode", "匿名")))
        favorite = next((r for r in self.config["favorites"] if (r["platform"], r["room"]) == (platform, number)), None)
        self.room_name.setText(favorite["name"] if favorite else self.platform_box.currentText() + " · " + number)

    def choose_room(self, record, *, log=True):
        self.platform_box.blockSignals(True)
        self.platform_box.setCurrentIndex(self.platform_box.findData(record["platform"]))
        self.platform_box.blockSignals(False)
        self.room_input.setText(record["room"])
        self.input_changed()
        self.room_name.setText(record["name"])
        if log:
            self.activity.emit("room.selected", "已选择 " + record["name"] + "。", source="room",
                               target=record["platform"] + ":" + record["room"])

    def refresh_rooms(self, *_):
        if not hasattr(self, "room_tab"):
            return
        selected = self.room_list.currentItem()
        key = selected.data(Qt.ItemDataRole.UserRole) if selected else None
        query = self.room_search.text().strip().casefold()
        self.room_list.blockSignals(True)
        self.room_list.clear()
        records = self.config["history"] if self.room_tab.currentIndex() == 1 else self.config["favorites"]
        for record in records:
            caption = next(k for k, v in PLATFORMS.items() if v == record["platform"]) + " · " + record["room"]
            if query not in (record["name"] + " " + caption).casefold():
                continue
            item = QListWidgetItem(record["name"] + "\n" + caption)
            item.setData(Qt.ItemDataRole.UserRole, dict(record))
            item.setToolTip(record["name"] + "\n" + caption)
            self.room_list.addItem(item)
            if key and (key["platform"], key["room"]) == (record["platform"], record["room"]):
                self.room_list.setCurrentItem(item)
        self.room_list.blockSignals(False)
        self.clear_history_button.setVisible(self.room_tab.currentIndex() == 1)

    def select_room(self, item, *_):
        if item is not None:
            self.choose_room(item.data(Qt.ItemDataRole.UserRole))

    def save_room(self):
        try:
            record = self.read_room()
        except ViewerError as exc:
            self.runtime.error(exc, scope="room")
            return
        self.activity.clear("room")
        favorites = self.config["favorites"]
        same = lambda r: (r["platform"], r["room"]) == (record["platform"], record["room"])
        index = next((i for i, r in enumerate(favorites) if same(r)), None)
        if index is None and len(favorites) >= 500:
            self.runtime.error(ViewerError("最多保存 500 个收藏，请先删除部分房间。", code="room.limit", level="WARN"), scope="room")
            return
        if index is None:
            favorites.append(record)
        else:
            favorites[index] = record
        for r in self.config["history"]:
            if same(r):
                r["name"] = record["name"]
        self.config["last_room"] = record
        for request in (self.runtime.active, self.runtime.last_request):
            if request and (request["platform"], request["room_input"]) == (record["platform"], record["room"]):
                request["room_name"] = record["name"]
        if self.save_settings():
            self.activity.emit("room.saved", "已保存 " + record["name"] + "。", source="room",
                               target=record["platform"] + ":" + record["room"])
        self.refresh_rooms()

    def delete_room(self):
        item = self.room_list.currentItem()
        if item is None:
            return
        record = item.data(Qt.ItemDataRole.UserRole)
        field = "history" if self.room_tab.currentIndex() else "favorites"
        self.config[field] = [r for r in self.config[field] if (r["platform"], r["room"]) != (record["platform"], record["room"])]
        if self.save_settings():
            self.activity.emit("room.deleted", "已从列表移除 " + record["name"] + "。", source="room",
                               target=record["platform"] + ":" + record["room"])
        self.refresh_rooms()

    def clear_history(self):
        self.config["history"] = []
        if self.save_settings():
            self.activity.emit("history.cleared", "最近观看已清空。", source="history")
        self.refresh_rooms()

    def start(self):
        self.vod_page.cancel_lookup()
        self.bangumi_page.cancel_lookup()
        try:
            record = self.read_room()
        except ViewerError as exc:
            self.runtime.error(exc)
            self.tick()
            return
        self.update_options()
        request = {"platform": record["platform"], "room_input": record["room"], "room_name": record["name"],
                   "requested": self.quality_box.currentData(), "session": self.mode_box.currentData(),
                   "backend": "potplayer", "network_mode": self.config["network_mode"],
                   "low_latency": self.config["low_latency"], "replay_mode": self.config["native_replay"],
                   "replay_minutes": self.config["replay_minutes"], "vsr": self.config["vsr"]}
        if self.runtime.play(request):
            self.confirmed.setChecked(False)
            self.note.clear()
            self.config["last_room"] = record
            self.config["room_preferences"][record["platform"] + ":" + record["room"]] = {
                "quality": request["requested"], "mode": request["session"]}
            self.save_settings()
        self.tick()

    def reconnect(self):
        self.confirmed.setChecked(False)
        if self.notice_event and self.notice_event.source == "pgc" and self.notice_event.session_id is None:
            if self.notice_event.code.startswith("pgc.subtitle"):self.bangumi_page.load_subtitles()
            elif self.bangumi_page.episode:self.bangumi_page.load_tracks()
            else:self.bangumi_page.lookup()
            return
        if self.notice_event and self.notice_event.source == "vod" and self.notice_event.session_id is None:
            if self.notice_event.code.startswith("vod.subtitle"):self.vod_page.load_subtitles()
            else:self.vod_page.lookup()
            return
        self.runtime.reconnect()
        self.tick()

    def pause(self):
        self.runtime.pause()
        self.tick()

    def stop(self):
        self.vod_page.cancel_lookup()
        self.bangumi_page.cancel_lookup()
        self.runtime.stop()
        self.close_strong_notice()
        self.tick()

    def tick(self):
        if self.closing or self.closed:
            return
        self.runtime.tick()
        rows, strong = self.activity.drain()
        self.log_model.append([e.record() for e in rows])
        if rows:
            last = rows[-1]
            self.last_activity.setText(last.occurred_at[11:19] + " · " + last.level + " · " + last.message)
            self.last_activity.setToolTip(last.message)
        self.refresh_log_count()
        notice = self.activity.notice()
        if notice != self.notice_event:
            self.notice_event = notice
            self.notice_details.hide()
            self.notice_detail_button.setText("展开技术详情")
            if notice is None:
                self.notice.hide()
            else:
                self.notice.setProperty("level", notice.level)
                self.notice_level.setText(notice.level)
                self.notice_level.setProperty("level", notice.level)
                self.notice_title.setText("无法开始观看" if notice.impact == "start_blocked" else
                                          "播放连接中断" if notice.impact == "interrupted" else "需要处理")
                self.notice_body.setText(notice.message)
                self.notice_details.setText("事件：" + notice.code + "  ·  目标：" + (notice.target or "程序") +
                                            ("\n" + json.dumps(dict(notice.details), ensure_ascii=False) if notice.details else ""))
                self.notice_retry.setVisible(notice.source in ("playback", "player", "vod", "pgc") and
                                             (bool(self.runtime.last_request) or notice.source in ("vod", "pgc")))
                self.notice_check.setVisible(notice.source in ("playback", "player", "diagnostic"))
                self.notice.show()
                for widget in (self.notice, self.notice_level):
                    widget.style().unpolish(widget);widget.style().polish(widget)
        # Close a resolved dialog; a confirmation never clears the actual fault.
        if self.strong_dialog:
            previous = self.strong_event
            same_incident = lambda e: (e.session_id, e.source, e.target, e.dedupe_key or e.code) == (
                previous.session_id, previous.source, previous.target, previous.dedupe_key or previous.code)
            if not any(same_incident(e) for e in self.activity.notice_events()):
                self.close_strong_notice()
        if strong:
            in_front = self.notices.accept(strong[-1], background_enabled=self.config["ui"]["background_notifications"])
            if in_front and self.config["ui"]["strong_notifications"]:
                self.show_strong_notice(strong[-1])
        self.unread_button.setText(f"未读提醒 {len(self.notices.unread)}")
        self.unread_button.setVisible(bool(self.notices.unread))
        snapshot = self.runtime.controller.snapshot()
        state, message = snapshot["state"], snapshot["message"]
        self.status_title.setText(STATES[state])
        self.global_status.setText(STATES[state])
        self.status_title.setProperty("state", state)
        self.status_title.style().unpolish(self.status_title);self.status_title.style().polish(self.status_title)
        self.status_context.setText(message)
        active = self.runtime.active or self.runtime.last_request
        vod_active = active is not None and active.get("kind") == "vod"
        self.status_room.setText(active["room_name"] if active else "尚未开始实播")
        self.start_button.setEnabled(not self.runtime.busy)
        self.start_button.setText("正在连接…" if self.runtime.busy else "开始播放")
        self.pause_button.setText("继续" if self.runtime.paused else "暂停")
        self.pause_button.setEnabled(not vod_active and self.runtime.active is not None and not self.runtime.busy and state in ("playing", "paused"))
        self.stop_button.setEnabled(self.runtime.active is not None or self.runtime.busy)
        self.reconnect_button.setEnabled(not vod_active and self.runtime.last_request is not None and not self.runtime.busy)
        for platform, widget in self.account_check_buttons.items():
            widget.setEnabled("account." + platform not in self.runtime.jobs.pending)
        self.connection_button.setEnabled("diagnostic.connection" not in self.runtime.jobs.pending)
        self.compare_button.setEnabled("diagnostic.compare" not in self.runtime.jobs.pending)
        self.cancel_diagnostic_button.setEnabled(any("diagnostic." + name in self.runtime.jobs.pending for name in ("connection", "compare")))
        if self.runtime.stream:
            stream = self.runtime.stream
            stats = self.runtime.relay.snapshot() if self.runtime.relay else {}
            self.stream_info.setText("PotPlayer · 实际 " + stream.actual_name +
                f" · 已转发 {stats.get('bytes_forwarded', 0) / 1048576:.1f} MB")
            if self.runtime.player is not None and self.runtime.active.get("replay_mode"):
                native = self.runtime.player.snapshot()
                span, position = native.get("duration_seconds") or 0, native.get("position_seconds") or 0
                self.stream_info.setText(self.stream_info.text() + f" · 回看范围 {span / 60:.1f} 分钟 · 距接收末端 {max(0, span - position):.1f} 秒")
            if stream is not self.last_quality_stream and self.runtime.active and not vod_active:
                self.set_qualities((self.runtime.active["platform"], self.runtime.active["room_input"]), stream.qualities)
                self.last_quality_stream = stream
        elif state in ("idle", "stopped"):
            self.stream_info.setText("PotPlayer · 实际画质在取流后显示")
        history_keys = tuple((r["platform"], r["room"], r["name"], r.get("played_at")) for r in self.config["history"])
        if history_keys != self.last_history_keys:
            self.last_history_keys = history_keys
            self.refresh_rooms()
        self.vod_page.tick()
        self.bangumi_page.tick()
        if self.stack.currentIndex() == 4:
            player = self.runtime.player.snapshot() if self.runtime.player else {}
            self.diagnostic_info.setPlainText(
                f"播放目标：{snapshot['target'] or '无'}\n状态：{STATES[state]}\n"
                f"网络：{self.runtime.active.get('network_mode') if self.runtime.active else self.config['network_mode']}\n"
                f"PotPlayer：{player.get('version') or '尚未启动'}\n"
                f"画面尺寸：{player.get('width', 0)} × {player.get('height', 0)}\n"
                f"播放进度：{player.get('position_seconds') or 0:.1f} 秒\n"
                f"{'视频总时长' if vod_active else '回看时间范围'}：{(player.get('duration_seconds') or 0) / 60:.1f} 分钟\n"
                f"播放器内存：{player.get('private_memory_mb') or 0:.1f} MB（含解码与缓存）\n"
                f"视频超分：{'已请求，实际效果依赖播放条件' if player.get('vsr_requested') else '关闭'}\n"
                f"自动重连：{self.runtime.controller.retry.total_attempts} 次")

    def open_unread(self):
        self.notices.open()

    def toggle_notice_details(self):
        visible = not self.notice_details.isVisible()
        self.notice_details.setVisible(visible)
        self.notice_detail_button.setText("收起技术详情" if visible else "展开技术详情")

    def open_notice_log(self):
        self.reset_filters()
        if self.notice_event:
            self.log_search.setText(self.notice_event.code)
        self.go_page(3)

    def show_strong_notice(self, event):
        self.close_strong_notice()
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setProperty("event_id", event.event_id)
        dialog.setWindowTitle("观看遇到问题" if event.impact in ("start_blocked", "interrupted") else "操作需要处理")
        dialog.setMinimumWidth(400)
        dialog.resize(500, 250)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)
        layout.addWidget(text("播放需要处理" if event.impact in ("start_blocked", "interrupted") else "操作需要处理", "NoticeTitle"))
        detail = text(event.message, "StatusContext", True)
        detail.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(detail)
        layout.addWidget(text("确认只关闭提醒。故障告示会保留到问题解决。", "Muted", True))
        layout.addLayout(row(button("查看日志", lambda: (self.open_notice_log(), self.close_strong_notice())),
                             button("知道了", self.close_strong_notice, "Primary")))
        self.strong_dialog = dialog
        self.strong_event = event
        dialog.finished.connect(lambda *_: self._dialog_finished(dialog))
        self.notification_count += 1
        dialog.show()

    def _dialog_finished(self, dialog):
        if self.strong_dialog is dialog:
            self.strong_dialog = None
            self.strong_event = None

    def close_strong_notice(self):
        if self.strong_dialog:
            dialog, self.strong_dialog = self.strong_dialog, None
            self.strong_event = None
            dialog.close()

    def filter_logs(self, *_):
        self.log_filter.change(level=self.level_filter.currentData(), source=self.source_filter.currentData(),
                               target=self.target_filter.text(), query=self.log_search.text())
        self.log_detail.clear()
        self.refresh_log_count()

    def reset_filters(self):
        self.level_filter.setCurrentIndex(0)
        self.source_filter.setCurrentIndex(0)
        self.target_filter.clear()
        self.log_search.clear()

    def refresh_log_count(self):
        self.log_count.setText(f"显示 {self.log_filter.rowCount()} / {self.log_model.rowCount()} 条 · 已脱敏")
        self.copy_button.setEnabled(bool(self.log_detail.toPlainText()))

    def show_log_detail(self, *_):
        indexes = self.log_table.selectionModel().selectedRows()
        if not indexes:
            self.log_detail.clear()
        else:
            record = self.log_model.rows[self.log_filter.mapToSource(indexes[0]).row()]
            self.log_detail.setPlainText(json.dumps(record, ensure_ascii=False, indent=2))
        self.refresh_log_count()

    def copy_log(self):
        if self.log_detail.toPlainText():
            QApplication.clipboard().setText(self.log_detail.toPlainText())
            self.activity.emit("log.detail_copied", "已复制脱敏的事件详情。", source="log")

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出当前筛选的脱敏日志", "活动日志.json", "JSON (*.json)")
        if path:
            records = self.log_filter.records()
            self.export_button.setEnabled(False)
            def work():
                from ..settings import atomic_write
                atomic_write(Path(path), json.dumps({"schema_version": 1, "scope": "filtered", "events": records},
                                                    ensure_ascii=False, indent=2).encode("utf-8"))
                return len(records)
            def done(count):
                self.export_button.setEnabled(True)
                self.activity.emit("log.exported", f"已导出当前筛选的 {count} 条脱敏日志。", source="log")
                self.activity.clear("log.export")
            def failed(exc):
                self.export_button.setEnabled(True)
                self.runtime.error(ViewerError("日志导出失败，请选择可写位置。", code="log.export_failed"), scope="log.export")
            self.runtime.jobs.submit("log.export", work, done, failed)

    def open_log_directory(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        directory = self.store.directory / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
        self.activity.emit("log.directory_opened", "已打开日志目录。", source="log")

    def diagnostic_request(self):
        if self.diagnostic_target.currentIndex() == 1:
            request = self.runtime.active or self.runtime.last_request
            if request is None:
                raise ViewerError("还没有播放会话，请选择观看页输入的房间。", code="diagnostic.no_session", level="WARN")
            return dict(request)
        record = self.read_room()
        return {"platform": record["platform"], "room_input": record["room"], "room_name": record["name"],
                "requested": self.quality_box.currentData(), "session": self.mode_box.currentData(),
                "network_mode": self.config["network_mode"]}

    def notice_diagnose(self):
        self.go_page(4)
        self.diagnostic_target.setCurrentIndex(1 if self.runtime.active or self.runtime.last_request else 0)
        self.check_connection()

    def diagnostic_error(self, exc, scope):
        from ..playback import safe_error
        exc = safe_error(exc)
        self.diagnostic_status.setText(str(exc))
        self.activity.emit(exc.code, str(exc), source="diagnostic", level=exc.level, scope=scope,
                           presentation="banner" if exc.level == "WARN" else "strong", impact="degraded")

    def start_diagnostic(self, name, request, work, done):
        channel = "diagnostic." + name
        if name in self.diagnostic_cancels:
            self.diagnostic_cancels[name].set()
        cancel = threading.Event()
        self.diagnostic_cancels[name] = cancel
        target = request["platform"] + ":" + request["room_input"]
        self.diagnostic_status.setText(target + (" · 正在检查接口与 CDN…" if name == "connection" else " · 正在依次采样匿名与登录会话…"))
        self.activity.reset_dedupe(channel)
        self.activity.emit(channel + "_requested", "已开始连接检查。" if name == "connection" else "已开始匿名与登录画质对比。",
                           source="diagnostic", target=target)
        def completed(result):
            self.diagnostic_cancels.pop(name, None)
            done(result)
        def failed(exc):
            self.diagnostic_cancels.pop(name, None)
            self.diagnostic_error(exc, channel)
        self.runtime.jobs.submit(channel, lambda: work(cancel), completed, failed, target=target,
                                 valid=lambda: not cancel.is_set())
        self.tick()

    def check_connection(self):
        try:
            request = self.diagnostic_request()
            jar = self.cookies.get(request["platform"]) if request["session"] == "导入 Cookie" else None
            if request["session"] == "导入 Cookie" and jar is None:
                raise ViewerError("所选平台没有可用登录，请选择匿名检查或先登录。", code="account.missing", level="WARN")
        except ViewerError as exc:
            self.diagnostic_error(exc, "diagnostic.connection")
            return
        target = request["platform"] + ":" + request["room_input"]
        def done(result):
            self.connection_result = result
            self.connection_details.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
            self.diagnostic_status.setText(target + " · " + result["stage"] + " · " + result["status"])
            if not result.get("error_code"):
                self.activity.clear("diagnostic.connection")
            level = result.get("level", "INFO")
            self.activity.emit("diagnostic.connection_failed" if result.get("error_code") else "diagnostic.connection_ready",
                               result["status"], source="diagnostic", level=level, target=target,
                               presentation=("banner" if level == "WARN" else "strong") if result.get("error_code") else "log",
                               impact="degraded" if result.get("error_code") else "none", scope="diagnostic.connection",
                               details={"stage": result["stage"], "reason": result.get("error_code", "ready"),
                                        "network_mode": request["network_mode"]})
        self.start_diagnostic("connection", request, lambda cancel: connection_check(request, jar, cancel=cancel), done)

    def set_qualities(self, key, qualities):
        self.known_qualities[key] = list(qualities)
        try:
            record = self.read_room()
        except ViewerError:
            return
        if (record["platform"], record["room"]) != key:
            return
        selected = self.quality_box.currentData()
        self.quality_box.clear()
        self.quality_box.addItem("原画优先", None)
        for quality in qualities:
            self.quality_box.addItem(safe_text(quality.name), quality.value)
        if selected is not None and self.quality_box.findData(selected) < 0:
            self.quality_box.addItem(f"上次选择 ({selected})", selected)
        self.quality_box.setCurrentIndex(max(0, self.quality_box.findData(selected)))

    def compare(self):
        try:
            request = self.diagnostic_request()
        except ViewerError as exc:
            self.diagnostic_error(exc, "diagnostic.compare")
            return
        target = request["platform"] + ":" + request["room_input"]
        jar = self.cookies.get(request["platform"])
        def done(result):
            rows, qualities = result
            self.comparison_rows = rows
            self.comparison_request = request
            self.set_qualities((request["platform"], request["room_input"]), qualities)
            self.comparison_caption.setText(target + " · 请求 " + str(request["requested"] if request["requested"] is not None else "原画优先"))
            self.comparison_table.setRowCount(len(rows))
            for i, item in enumerate(rows):
                metadata = item.get("sample", {}).get("metadata", {})
                picture = f"\n{metadata.get('width', 0):g}×{metadata.get('height', 0):g} · {metadata.get('framerate', 0):g} fps" if metadata else ""
                actual = item.get("actual_name", "—") + ("（已调整）" if item.get("different_from_request") else "") + picture
                bitrate = item.get("sample", {}).get("sample_total_kbps")
                values = (item["session"], actual, f"{bitrate} kbps" if bitrate else "—", item["status"])
                for j, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setToolTip(value)
                    self.comparison_table.setItem(i, j, cell)
                self.comparison_table.setRowHeight(i, 60)
                self.activity.emit("diagnostic.sample_result", item["session"] + " · " + item["status"],
                                   source="diagnostic", target=target, level=item.get("level", "INFO"))
            failures = [r for r in rows if r.get("error_code")]
            tested = [r for r in rows if not r.get("skipped")]
            if failures:
                complete_failure = len(failures) == len(tested)
                level = "ERROR" if complete_failure and any(r.get("level") == "ERROR" for r in failures) else "WARN"
                message = "画质采样未成功，请查看结果和连接检查。" if complete_failure else "部分会话采样失败，请查看对比结果。"
                self.activity.emit("diagnostic.compare_failed", message, source="diagnostic", target=target, level=level,
                                   presentation="strong" if level == "ERROR" else "banner", impact="degraded",
                                   scope="diagnostic.compare")
            else:
                self.activity.clear("diagnostic.compare")
            self.diagnostic_status.setText(target + " · 画质对比完成；实播确认后可导出报告。")
            self.activity.emit("diagnostic.compare_completed", "画质对比已完成。", source="diagnostic", target=target)
        self.start_diagnostic("compare", request, lambda cancel: compare_quality(request, jar, cancel=cancel), done)

    def cancel_diagnostics(self, *, log=True):
        had_pending = bool(self.diagnostic_cancels)
        for name, cancel in self.diagnostic_cancels.items():
            cancel.set()
            self.runtime.jobs.cancel("diagnostic." + name)
        self.diagnostic_cancels.clear()
        if log and had_pending:
            self.diagnostic_status.setText("检查已取消，保留上次完成的结果。")
            self.activity.emit("diagnostic.cancelled", "检查已取消，旧结果不会更新当前操作。", source="diagnostic")

    def diagnostic_report(self):
        return clean({"schema_version": 1, "app_version": VERSION,
                      "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                      "playback": self.runtime.report(confirmed=self.confirmed.isChecked(), note=self.note.text()),
                      "connection": self.connection_result, "comparison": self.comparison_rows,
                      "comparison_request": self.comparison_request}, REPORT)

    def export_report(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出脱敏诊断报告", "直播诊断.json", "JSON (*.json)")
        if not path:
            return
        report = self.diagnostic_report()
        self.report_button.setEnabled(False)
        def done(_):
            self.report_button.setEnabled(True)
            self.activity.clear("diagnostic.export")
            self.activity.emit("diagnostic.exported", "脱敏诊断报告已导出。", source="diagnostic")
        def failed(exc):
            self.report_button.setEnabled(True)
            self.diagnostic_error(ViewerError("诊断报告导出失败，请选择可写位置。", code="diagnostic.export_failed"), "diagnostic.export")
        self.runtime.jobs.submit("diagnostic.export", lambda: save_diagnostic_report(Path(path), report), done, failed)

    def restore_login(self, platform):
        self.account_status[platform].setText("正在读取加密保存的登录…")
        previous_write = self.credential_writer.generations.get(platform, 0)
        def done(jar):
            if jar:
                self.cookies[platform] = jar
                self.account_status[platform].setText("已恢复加密保存的登录 · 使用时检查有效性")
                self.activity.emit("account.restored", "已恢复本机加密保存的登录。", source="account", target=platform)
                if (self.config["remember_login"][platform] and
                        self.credential_writer.generations.get(platform, 0) != previous_write):
                    self.persist_login(platform, jar)
            else:
                self.account_status[platform].setText("没有保存的登录 · 可匿名观看")
        self.runtime.jobs.submit("account." + platform, lambda: self.credentials.load(platform), done,
                                 lambda exc: self.account_error(platform, exc, code="account.restore_failed"), target=platform)

    def _import_login(self, platform, work):
        self.cancel_qr(platform)
        self.activity.reset_dedupe("account." + platform)
        self.account_status[platform].setText("正在验证导入的登录…")
        route = self.config["network_mode"]
        self.activity.emit("account.verifying", "正在验证导入的登录。", source="account", target=platform)
        def verify():
            jar = work()
            return jar, verify_login(platform, jar, network_mode=route)
        def done(result):
            jar, status = result
            self.cookies[platform] = jar
            self.account_status[platform].setText(status["message"])
            self.activity.clear("account." + platform)
            self.activity.emit("account.imported", status["message"],
                               source="account", target=platform)
            if self.platform_box.currentData() == platform:
                self.mode_box.setCurrentIndex(self.mode_box.findData("导入 Cookie"))
                try:
                    room = self.read_room()["room"]
                    preference = self.config["room_preferences"].setdefault(platform + ":" + room, {})
                    preference["mode"] = "导入 Cookie"
                except ViewerError:
                    pass
            if self.config["remember_login"][platform]:
                self.persist_login(platform, jar)
            self.save_settings()
        self.runtime.jobs.submit("account." + platform, verify, done,
                                 lambda exc: self.account_error(platform, exc), target=platform)

    def account_error(self, platform, error, *, code=None):
        from ..playback import safe_error
        error = safe_error(error)
        prefix = "原登录保留；" if platform in self.cookies else "可先匿名观看；"
        self.account_status[platform].setText(prefix + str(error))
        self.activity.emit(code or error.code, str(error), source="account", target=platform, level=error.level,
                           presentation="banner" if error.level == "WARN" else "strong",
                           impact="degraded", scope="account." + platform, details={"reason": error.code})

    def persist_login(self, platform, jar=None):
        request = self.credential_writer.request(platform, jar)
        def done(applied):
            if not applied:
                return
            self.activity.clear("account.persist." + platform)
            self.activity.emit("account.remember_saved" if jar is not None else "account.saved_login_deleted",
                               "登录已由 Windows 当前用户加密保存。" if jar is not None else "保存的登录已删除。",
                               source="account", target=platform)
        def failed(exc):
            if jar is not None:
                self.config["remember_login"][platform] = False
                self.set_remember_box(platform, False)
                self.save_settings()
            self.activity.emit("account.remember_failed",
                               "登录保存失败，本次会话仍可使用。" if jar is not None else
                               "已关闭登录记忆，但保存的登录删除失败，请检查数据目录权限并再次清除。",
                               source="account", target=platform, level="WARN", presentation="banner",
                               scope="account.persist." + platform, details={"reason": exc.code})
        self.runtime.jobs.submit("credential." + platform, lambda: self.credential_writer.apply(request),
                                 done, failed, target=platform)

    def set_remember_box(self, platform, enabled):
        widget = self.remember_boxes[platform]
        widget.blockSignals(True)
        widget.setChecked(enabled)
        widget.blockSignals(False)

    def toggle_remember_login(self, platform, enabled):
        self.config["remember_login"][platform] = enabled
        self.save_settings()
        if not enabled or platform in self.cookies:
            self.persist_login(platform, self.cookies.get(platform) if enabled else None)
        self.activity.emit("account.remember_enabled" if enabled else "account.remember_disabled",
                           "已开启登录记忆；当前或下次导入的登录会加密保存。" if enabled else
                           "已关闭登录记忆，本次会话仍可使用，正在删除保存的登录。",
                           source="account", target=platform)

    def check_login(self, platform):
        jar = self.cookies.get(platform)
        if jar is None:
            self.account_error(platform, ViewerError("没有可检查的登录，请扫码或导入 Cookie。",
                                                    code="account.missing", level="WARN"))
            return
        self.account_status[platform].setText("正在检查登录状态…")
        route = self.config["network_mode"]
        self.activity.emit("account.check_requested", "正在检查登录状态。", source="account", target=platform)
        def done(status):
            self.account_status[platform].setText(status["message"])
            self.activity.clear("account." + platform)
            self.activity.emit("account.checked", status["message"], source="account", target=platform)
        def failed(exc):
            if exc.code == "account.expired":
                self.cookies.pop(platform, None)
                self.config["remember_login"][platform] = False
                self.set_remember_box(platform, False)
                self.persist_login(platform)
                self.save_settings()
                if self.platform_box.currentData() == platform:
                    self.mode_box.setCurrentIndex(0)
                if self.runtime.active and self.runtime.active["platform"] == platform and self.runtime.active["session"] == "导入 Cookie":
                    self.runtime.stop()
            self.account_error(platform, exc)
        self.runtime.jobs.submit("account." + platform, lambda: verify_login(platform, jar, network_mode=route),
                                 done, failed, target=platform)

    def cancel_qr(self, platform=None):
        if self.qr_dialog is not None and (platform is None or self.qr_dialog.platform == platform):
            self.qr_dialog.close()

    def qr_login(self, platform):
        if self.qr_dialog is not None and self.qr_dialog.platform == platform:
            self.qr_dialog.show()
            self.qr_dialog.raise_()
            return
        self.cancel_qr()
        self.runtime.jobs.cancel("account." + platform)
        self.activity.clear("account." + platform)
        self.activity.emit("account.qr_opened", "已打开扫码登录。", source="account", target=platform)
        def closed():
            self.qr_dialog = None
        def event(code, message, **fields):
            if code == "account.qr_refreshed":
                self.activity.clear("account." + platform)
            self.activity.emit(code, message, source="account", target=platform, scope="account." + platform, **fields)
        self.qr_dialog = QrLoginDialog(self, platform, lambda jar: self._import_login(platform, lambda: jar),
                                       closed, network_mode=self.config["network_mode"], on_event=event)
        self.qr_dialog.show()

    def import_cookie(self, platform):
        path, _ = QFileDialog.getOpenFileName(self, "选择 Netscape Cookie 文件", "", "所有文件 (*)")
        if path:
            self._import_login(platform, lambda: load_cookies(path, platform))

    def paste_cookie(self, platform):
        from PySide6.QtWidgets import QLineEdit
        value, accepted = QInputDialog.getText(self, "粘贴 Cookie", "仅导入所选平台；不会写入日志。",
                                              QLineEdit.EchoMode.Password)
        if accepted and value:
            self._import_login(platform, lambda: cookies_from_text(value, platform))

    def clear_login(self, platform):
        self.cancel_qr(platform)
        self.runtime.jobs.cancel("account." + platform)
        self.cookies.pop(platform, None)
        self.account_status[platform].setText("未登录 · 可匿名观看")
        self.config["remember_login"][platform] = False
        self.set_remember_box(platform, False)
        if self.platform_box.currentData() == platform:
            self.mode_box.setCurrentIndex(0)
        if self.runtime.active and self.runtime.active["platform"] == platform and self.runtime.active["session"] == "导入 Cookie":
            self.runtime.stop()
        self.save_settings()
        self.persist_login(platform)
        self.activity.clear("account." + platform)
        self.activity.emit("account.cleared", "已清除内存登录，正在删除保存的登录。", source="account", target=platform)

    def closeEvent(self, event):
        if self.closed:
            event.accept()
            return
        event.ignore()
        if self.closing:
            return
        self.closing = True
        self.notices.close()
        self.cancel_qr()
        self.cancel_diagnostics(log=False)
        self.poll_timer.stop()
        self.close_strong_notice()
        self.runtime.controller.stop(closing=True)  # Invalidate launch guards immediately.
        self.centralWidget().setEnabled(False)
        self.global_status.setText("正在关闭播放器…")
        self.save_settings()
        def shutdown():
            try:
                self.runtime.close()
            finally:
                self.credential_writer.close()
                self.activity.close()
                self.shutdown_done.set()
        threading.Thread(target=shutdown, name="qt-shutdown", daemon=True).start()
        self.shutdown_timer = QTimer(self)
        self.shutdown_timer.timeout.connect(self._finish_shutdown)
        self.shutdown_timer.start(50)

    def _finish_shutdown(self):
        if self.shutdown_done.is_set():
            self.shutdown_timer.stop()
            self.closed = True
            self.close()


def main():
    parser = argparse.ArgumentParser(description="本地直播 Qt 前端")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--validate", type=Path)
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--probe-live", type=Path)
    parser.add_argument("--probe-features", type=Path)
    parser.add_argument("--probe-vod", type=Path)
    parser.add_argument("--probe-p7", type=Path)
    parser.add_argument("--probe-p8", type=Path)
    parser.add_argument("--probe-p8-member", action="store_true")
    parser.add_argument("--probe-p8-full", action="store_true")
    parser.add_argument("--probe-p7-minutes", type=float, default=0)
    parser.add_argument("--probe-video", default="BV17x411w7KC")
    parser.add_argument("--probe-vod-pause-seconds", type=int, default=60)
    parser.add_argument("--probe-player", type=Path, default=Path(r"D:\potplayer\PotPlayerMini64.exe"))
    parser.add_argument("--probe-platform", choices=("douyu", "bilibili"), default="douyu")
    parser.add_argument("--probe-room", default="6657")
    args = parser.parse_args()
    if (args.probe_p8_member or args.probe_p8_full) and not args.probe_p8:
        parser.error("番剧会员 / 完整观看验收须同时指定 --probe-p8 和独立 --data-dir")
    # Validation must never accidentally read or overwrite the user's normal data.
    if (args.validate or args.probe_live or args.probe_features or args.probe_vod or args.probe_p7 or args.probe_p8) and args.data_dir is None:
        parser.error("验证必须使用独立的 --data-dir")
    if sum(bool(v) for v in (args.validate, args.probe_live, args.probe_features, args.probe_vod, args.probe_p7, args.probe_p8)) > 1:
        parser.error("离线交互验证与匿名实播探针分别运行")
    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    app.setStyleSheet(Path(__file__).with_name("theme.qss").read_text(encoding="utf-8"))
    window = ViewerWindow(args.data_dir)
    window.show()
    def callback_exception(*_):
        window.activity.emit("ui.callback_failed", "界面操作失败，请查看日志或重新启动。", source="app",
                             level="ERROR", presentation="strong", scope="app")
    sys.excepthook = callback_exception
    if args.validate:
        from .validate import validate
        QTimer.singleShot(150, lambda: validate(app, window, args.validate, args.screenshots))
    elif args.probe_live:
        from .live_probe import probe
        QTimer.singleShot(150, lambda: probe(app, window, args))
    elif args.probe_features:
        from .feature_probe import probe
        QTimer.singleShot(150, lambda: probe(app, window, args))
    elif args.probe_vod:
        from .vod_probe import probe
        QTimer.singleShot(150, lambda: probe(app, window, args))
    elif args.probe_p7:
        from .p7_probe import probe
        QTimer.singleShot(150, lambda: probe(app, window, args))
    elif args.probe_p8:
        from .p8_probe import probe
        QTimer.singleShot(150, lambda: probe(app, window, args))
    return app.exec()

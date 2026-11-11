"""Native Qt P0 interaction prototype. All activity is simulated in memory."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget,
    QMainWindow, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy,
    QStackedWidget, QStyle, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from model import DemoModel, LEVELS, Room, STATE_LABELS


def text(value, role=None, wrap=False):
    widget = QLabel(value)
    if role:
        widget.setObjectName(role)
    widget.setWordWrap(wrap)
    if wrap:
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    return widget


def row(*widgets):
    layout = QHBoxLayout()
    layout.setSpacing(10)
    for widget in widgets:
        layout.addWidget(widget)
    return layout


def line_icon(kind, color="#6b7890"):
    pixmap = QPixmap(24, 24)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1.7, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if kind == "play":
        painter.setBrush(QColor(color))
        painter.drawPolygon(QPolygonF([QPointF(8, 5), QPointF(19, 12), QPointF(8, 19)]))
    elif kind == "pause":
        painter.drawLine(8, 5, 8, 19);painter.drawLine(16, 5, 16, 19)
    elif kind == "stop":
        painter.drawRoundedRect(6, 6, 12, 12, 1.5, 1.5)
    elif kind == "reload":
        painter.drawArc(5, 5, 14, 14, 30 * 16, 285 * 16)
        painter.drawLine(17, 5, 20, 5);painter.drawLine(20, 5, 20, 8)
    elif kind == "account":
        painter.drawEllipse(9, 4, 6, 6);painter.drawArc(5, 11, 14, 11, 0, 180 * 16)
        painter.drawLine(5, 17, 5, 20);painter.drawLine(19, 17, 19, 20)
    elif kind == "settings":
        painter.drawEllipse(5, 5, 14, 14);painter.drawEllipse(9, 9, 6, 6)
        for x0, y0, x1, y1 in ((12,2,12,5),(12,19,12,22),(2,12,5,12),(19,12,22,12)):
            painter.drawLine(x0,y0,x1,y1)
    elif kind == "logs":
        painter.drawRoundedRect(5, 3, 14, 18, 2, 2)
        for y in (8, 12, 16):painter.drawLine(8,y,16,y)
    else:
        painter.drawLine(4, 19, 4, 5);painter.drawLine(4, 19, 20, 19)
        painter.drawPolyline(QPolygonF([QPointF(6,15),QPointF(10,9),QPointF(14,12),QPointF(19,5)]))
    painter.end()
    return QIcon(pixmap)


ICON_NAMES = {QStyle.StandardPixmap.SP_MediaPlay: "play",
              QStyle.StandardPixmap.SP_MediaPause: "pause",
              QStyle.StandardPixmap.SP_MediaStop: "stop",
              QStyle.StandardPixmap.SP_BrowserReload: "reload",
              QStyle.StandardPixmap.SP_DialogApplyButton: "account",
              QStyle.StandardPixmap.SP_FileDialogDetailedView: "settings",
              QStyle.StandardPixmap.SP_FileIcon: "logs",
              QStyle.StandardPixmap.SP_MessageBoxInformation: "diagnostics"}


def button(title, callback, role=None, icon=None):
    widget = QPushButton(title)
    if role:
        widget.setObjectName(role)
    if icon is not None:
        widget.setIcon(line_icon(ICON_NAMES.get(icon, "diagnostics"),
                                 "#ffffff" if role == "Primary" else "#b32c3f" if role == "Danger" else "#6b7890"))
    widget.clicked.connect(lambda checked=False: callback())
    return widget


def card(title=None, description=None, name="Card"):
    widget = QFrame()
    widget.setObjectName(name)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(20, 18, 20, 18)
    layout.setSpacing(12)
    if title:
        layout.addWidget(text(title, "SectionTitle"))
    if description:
        layout.addWidget(text(description, "Muted", True))
    return widget, layout


def scrolling_page():
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 9, 0)
    layout.setSpacing(16)
    scroll.setWidget(body)
    return scroll, layout


class Prototype(QMainWindow):
    PAGE_TITLES = ("观看", "账号", "设置", "活动日志", "诊断")

    def __init__(self):
        super().__init__()
        self.model = DemoModel()
        self.generation = 0
        self.toast_generation = 0
        self.strong_dialog = None
        self.notification_count = 0
        self.account_dialog = None
        self.setWindowTitle("本地直播 · P0 交互原型")
        self.resize(1240, 850)
        self.setMinimumSize(920, 620)
        available = self.screen().availableGeometry()
        self.resize(min(1240, max(920, available.width() - 40)),
                    min(850, max(620, available.height() - 50)))
        self._build()
        self._refresh_rooms()
        self._refresh_state()
        self.emit("INFO", "prototype.opened", "界面", "已打开 P0 原型；所有状态均为离线演示。")

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
        nav.setSpacing(8)
        mark = text("∿", "BrandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(42, 42)
        brand = row(mark, text("本地直播", "Brand"))
        nav.addLayout(brand)
        nav.addWidget(text("桌面观看 · PotPlayer", "Caption"))
        nav.addSpacing(28)
        self.navigation = QButtonGroup(self)
        self.nav_buttons = []
        icons = (QStyle.StandardPixmap.SP_MediaPlay, QStyle.StandardPixmap.SP_DialogApplyButton,
                 QStyle.StandardPixmap.SP_FileDialogDetailedView, QStyle.StandardPixmap.SP_FileIcon,
                 QStyle.StandardPixmap.SP_MessageBoxInformation)
        for index, title in enumerate(self.PAGE_TITLES):
            widget = button(title, lambda i=index: self.go_page(i), "Nav", icons[index])
            widget.setCheckable(True)
            self.navigation.addButton(widget)
            self.nav_buttons.append(widget)
            nav.addWidget(widget)
        nav.addStretch(1)
        nav.addWidget(text("P0 · 界面与事件预览", "Muted"))
        nav.addWidget(text("不连接平台，不启动播放器\n账号、房间状态均为示例", "Caption", True))
        shell.addWidget(sidebar)

        main = QWidget()
        main_layout = QVBoxLayout(main)
        main_layout.setContentsMargins(26, 23, 22, 12)
        main_layout.setSpacing(16)
        header = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(4)
        self.page_title = text("观看", "Title")
        self.page_subtitle = text("选择收藏，或输入房间号开始观看。", "Subtitle", True)
        heading.addWidget(self.page_title)
        heading.addWidget(self.page_subtitle)
        header.addLayout(heading, 1)
        header.addWidget(text("P0 原型 · 演示数据", "PrototypeTag"))
        self.scene_picker = QComboBox()
        self.scene_picker.setObjectName("ScenarioPicker")
        self.scene_picker.addItems(["选择演示场景", "未开播", "TLS 连接失败", "重连后恢复",
                                    "重连最终失败", "重复同类故障", "登录成功不覆盖故障", "手动停止"])
        self.scene_picker.setMinimumWidth(165)
        header.addWidget(self.scene_picker)
        header.addWidget(button("演示", self.run_picked_scene))
        main_layout.addLayout(header)

        self.notice = QFrame()
        self.notice.setObjectName("Notice")
        notice_layout = QVBoxLayout(self.notice)
        notice_layout.setContentsMargins(18, 14, 18, 14)
        notice_layout.setSpacing(8)
        notice_top = QHBoxLayout()
        self.notice_level = text("", "Level")
        notice_top.addWidget(self.notice_level)
        self.notice_title = text("", "NoticeTitle", True)
        notice_top.addWidget(self.notice_title, 1)
        self.notice_log_button = button("查看日志", lambda: self.go_page(3))
        notice_top.addWidget(self.notice_log_button)
        self.notice_action_button = button("重试", self.notice_action, "Primary")
        notice_top.addWidget(self.notice_action_button)
        notice_layout.addLayout(notice_top)
        self.notice_body = text("", "NoticeBody", True)
        notice_layout.addWidget(self.notice_body)
        self.notice_details_button = button("展开技术详情", self.toggle_notice_details)
        self.notice_details_button.setMaximumWidth(140)
        notice_layout.addWidget(self.notice_details_button)
        self.notice_details = text("", "NoticeDetails", True)
        self.notice_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        notice_layout.addWidget(self.notice_details)
        self.notice_details.hide()
        self.notice.hide()
        main_layout.addWidget(self.notice)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._watch_page())
        self.stack.addWidget(self._account_page())
        self.stack.addWidget(self._settings_page())
        self.stack.addWidget(self._logs_page())
        self.stack.addWidget(self._diagnostics_page())
        main_layout.addWidget(self.stack, 1)

        footer = QFrame()
        footer.setObjectName("Footer")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 10, 0, 0)
        self.last_activity = text("活动已记录", "Caption")
        self.last_activity.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer_layout.addWidget(self.last_activity, 1)
        self.toast = text("", "Muted")
        footer_layout.addWidget(self.toast)
        footer_layout.addWidget(button("打开日志", lambda: self.go_page(3)))
        main_layout.addWidget(footer)
        shell.addWidget(main, 1)
        self.nav_buttons[0].setChecked(True)
        self.nav_buttons[0].setIcon(line_icon("play", "#386cf4"))

    def _watch_page(self):
        host = QWidget()
        layout = QHBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        favorites, favorites_layout = card("我的收藏", "按平台与房间号保存")
        favorites.setMinimumWidth(185)
        favorites.setMaximumWidth(250)
        self.room_search = QLineEdit()
        self.room_search.setPlaceholderText("搜索名称或房间号")
        self.room_search.textChanged.connect(self._refresh_rooms)
        favorites_layout.addWidget(self.room_search)
        self.room_list = QListWidget()
        self.room_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.room_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.room_list.setWordWrap(True)
        self.room_list.currentRowChanged.connect(self.select_room)
        favorites_layout.addWidget(self.room_list, 1)
        favorites_layout.addWidget(button("保存当前房间", self.save_room))
        favorites_layout.addWidget(text("收藏仅保存在本次演示中", "Caption", True))
        layout.addWidget(favorites, 1)

        scroll, content = scrolling_page()
        self.status_card, status_layout = card(name="StatusCard")
        self.status_room = text("玩机器", "SectionTitle")
        status_layout.addWidget(self.status_room)
        self.status_title = text("", "StatusTitle", True)
        status_layout.addWidget(self.status_title)
        self.status_context = text("", "StatusContext", True)
        status_layout.addWidget(self.status_context)
        pills = QHBoxLayout()
        for value in ("PotPlayer", "原画优先", "仅演示"):
            pills.addWidget(text(value, "Pill"))
        pills.addStretch(1)
        status_layout.addLayout(pills)
        actions = QHBoxLayout()
        self.start_button = button("开始播放", self.start_demo, "Primary", QStyle.StandardPixmap.SP_MediaPlay)
        self.pause_button = button("暂停", self.toggle_pause, icon=QStyle.StandardPixmap.SP_MediaPause)
        self.stop_button = button("停止", self.stop_demo, "Danger", QStyle.StandardPixmap.SP_MediaStop)
        self.reconnect_button = button("回到直播", lambda: self.start_demo(reconnect=True),
                                       icon=QStyle.StandardPixmap.SP_BrowserReload)
        for widget in (self.start_button, self.pause_button, self.stop_button, self.reconnect_button):
            actions.addWidget(widget)
        actions.addStretch(1)
        status_layout.addLayout(actions)
        content.addWidget(self.status_card)

        source, source_layout = card("打开直播间")
        self.platform_box = QComboBox()
        self.platform_box.addItems(["斗鱼", "B 站"])
        self.platform_box.setMinimumWidth(95)
        self.platform_box.currentTextChanged.connect(self.input_platform_changed)
        self.room_input = QLineEdit("6657")
        self.room_input.setPlaceholderText("输入房间号或平台链接")
        self.room_input.returnPressed.connect(self.start_demo)
        source_layout.addLayout(row(self.platform_box, self.room_input))
        self.room_name = QLineEdit("玩机器")
        self.room_name.setPlaceholderText("自定义房间名")
        source_layout.addLayout(row(text("房间名称", "Muted"), self.room_name,
                                    button("保存 / 改名", self.save_room)))
        content.addWidget(source)
        options, options_layout = card("观看选项")
        self.quality_box = QComboBox()
        self.quality_box.addItems(["原画优先", "超清", "高清"])
        self.mode_box = QComboBox()
        self.mode_box.addItems(["匿名观看", "使用已登录账号"])
        options_layout.addLayout(row(text("画质", "Muted"), self.quality_box,
                                     text("账号", "Muted"), self.mode_box))
        options_layout.addWidget(text("播放器在独立窗口打开，管理界面保留当前会话与恢复状态。", "Muted", True))
        content.addWidget(options)
        content.addStretch(1)
        layout.addWidget(scroll, 3)
        return host

    def _account_page(self):
        scroll, layout = scrolling_page()
        self.account_status = {}
        for platform in ("B 站", "斗鱼"):
            item, item_layout = card(platform, "登录可用于请求当前账号有权访问的画质。")
            status = text("未登录 · 演示状态", "StatusContext")
            self.account_status[platform] = status
            item_layout.addWidget(status)
            item_layout.addLayout(row(
                button("扫码登录", lambda p=platform: self.show_login_demo(p), "Primary"),
                button("导入 Cookie", self.show_import_placeholder),
                button("清除登录", lambda p=platform: self.clear_login_demo(p))))
            remembered = QCheckBox("记住登录（正式版使用 Windows 用户加密）")
            remembered.stateChanged.connect(lambda state, p=platform:
                self.emit("INFO", "account.remember_preview", "账号", f"{p}登录记忆选项已修改，仅作用于演示。"))
            item_layout.addWidget(remembered)
            layout.addWidget(item)
        note, note_layout = card("账号数据")
        note_layout.addWidget(text("本原型不接受真实 Cookie 或密码。扫码区用于检查布局与登录状态变化。", "Muted", True))
        layout.addWidget(note)
        layout.addStretch(1)
        return scroll

    def _settings_page(self):
        scroll, layout = scrolling_page()
        player, box = card("播放器", "PotPlayer 保持自己的皮肤、进度条、全屏与快捷键。")
        self.player_path = QLineEdit(r"D:\potplayer\PotPlayerMini64.exe")
        self.player_path.setReadOnly(True)
        box.addLayout(row(text("程序路径", "Muted"), self.player_path,
                          button("选择程序", lambda: self.emit("INFO", "settings.player_path_preview", "设置",
                                "P0 使用示例路径；程序选择将在 P2 接入。", presentation="toast"))))
        box.addWidget(text("接管方式：本工具关联的独立播放会话", "StatusContext"))
        box.addWidget(text("NVIDIA 视频超分：尚未验证 · P5 在 RTX 5060 + 1440p 上验收", "Muted", True))
        layout.addWidget(player)
        network, box = card("网络连接")
        network_mode = QComboBox()
        network_mode.addItems(["直连（推荐）", "系统 / 环境代理"])
        network_mode.currentTextChanged.connect(lambda value:
            self.emit("INFO", "settings.network_preview", "设置", f"网络选项已选择：{value}（演示）。", presentation="toast"))
        box.addLayout(row(text("平台连接方式", "Muted"), network_mode))
        box.addWidget(text("正式版直连优先 IPv4，保留 IPv6 回退。更改网络设置后重新连接生效。", "Muted", True))
        layout.addWidget(network)
        recovery, box = card("恢复与通知")
        self.recovery_options = {}
        for key, caption in (("auto", "断流后自动恢复"), ("strong", "重要故障使用强提醒"),
                              ("background", "后台或全屏时保留未读提醒（正式版验证）")):
            check = QCheckBox(caption);check.setChecked(True)
            self.recovery_options[key] = check
            check.toggled.connect(lambda enabled, k=key, c=caption: self.setting_changed(k, c, enabled))
            box.addWidget(check)
        box.addWidget(text("手动暂停、停止或关闭播放器后，不自动重新开始播放。", "Muted", True))
        layout.addWidget(recovery)
        logs, box = card("日志")
        box.addWidget(text("正式版：操作、状态变化与异常落盘；支持等级筛选、脱敏导出和日志轮转。", "Muted", True))
        box.addWidget(text("P0：仅记录当前原型的演示事件，关闭后清空。", "Caption"))
        layout.addWidget(logs)
        layout.addStretch(1)
        return scroll

    def _logs_page(self):
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        controls = QHBoxLayout()
        self.level_filter = QComboBox()
        self.level_filter.addItems(["全部等级", *LEVELS])
        self.level_filter.currentTextChanged.connect(self._refresh_logs)
        self.log_search = QLineEdit()
        self.log_search.setPlaceholderText("搜索事件、来源或房间号")
        self.log_search.textChanged.connect(self._refresh_logs)
        controls.addWidget(self.level_filter)
        controls.addWidget(self.log_search, 1)
        controls.addWidget(button("导出演示日志", self.export_logs))
        layout.addLayout(controls)
        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels(["时间", "等级", "来源", "目标", "活动"])
        self.log_table.verticalHeader().hide()
        self.log_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.log_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.log_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.log_table.setShowGrid(False)
        self.log_table.setAlternatingRowColors(False)
        for index, width in enumerate((100, 80, 70, 125)):
            self.log_table.setColumnWidth(index, width)
            self.log_table.horizontalHeader().setSectionResizeMode(index, QHeaderView.ResizeMode.Fixed)
        self.log_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.log_table.itemSelectionChanged.connect(self.show_log_detail)
        layout.addWidget(self.log_table, 3)
        self.log_detail = QPlainTextEdit()
        self.log_detail.setReadOnly(True)
        self.log_detail.setPlaceholderText("选择一条日志，查看事件代码、播放会话与公开技术详情。")
        self.log_detail.setMaximumHeight(200)
        layout.addWidget(self.log_detail, 1)
        self.log_count = text("0 条活动 · P0 仅存于内存", "Caption")
        layout.addWidget(self.log_count)
        return host

    def _diagnostics_page(self):
        scroll, layout = scrolling_page()
        current, box = card("当前会话连接检查", "把房间接口、CDN 与播放器状态分别记录，避免用转发成功代替解码成功。")
        box.addLayout(row(button("演示连接检查", self.run_diagnostic_demo, "Primary"),
                          button("查看活动日志", lambda: self.go_page(3))))
        self.diagnostic_result = text("尚未进行真实连接。P0 仅展示检查步骤与结果样式。", "StatusContext", True)
        box.addWidget(self.diagnostic_result)
        layout.addWidget(current)
        info, box = card("诊断信息")
        box.addWidget(text("网络模式：直连 · IPv4 优先\n播放器：PotPlayer 独立窗口\n增强：RTX VSR 尚未验证", "Muted", True))
        box.addWidget(text("日志与报告不包含 Cookie、扫码密钥或签名媒体地址。", "Muted", True))
        layout.addWidget(info)
        display, box = card("演示情景", "通过右上角的场景菜单预览提醒等级和故障恢复。")
        box.addLayout(row(button("未开播", self.offline_demo),
                          button("TLS 失败", self.tls_demo),
                          button("重连恢复", lambda: self.recovery_demo(True))))
        box.addLayout(row(button("恢复失败", lambda: self.recovery_demo(False)),
                          button("重复故障", self.repeat_tls_demo),
                          button("登录成功不覆盖故障", self.login_success_while_failed)))
        layout.addWidget(display)
        layout.addStretch(1)
        return scroll

    def go_page(self, index):
        self.stack.setCurrentIndex(index)
        self.nav_buttons[index].setChecked(True)
        for i, widget in enumerate(self.nav_buttons):
            widget.setIcon(line_icon(("play", "account", "settings", "logs", "diagnostics")[i],
                                     "#386cf4" if i == index else "#6b7890"))
        self.page_title.setText(self.PAGE_TITLES[index])
        captions = ("选择收藏，或输入房间号开始观看。",
                    "按平台管理账号，登录状态与播放状态分开呈现。",
                    "播放器、网络、恢复和通知选项。",
                    "所有操作与状态变化，都能在这里追踪。",
                    "定位房间、线路和播放器各阶段的问题。")
        self.page_subtitle.setText(captions[index])
        if index == 3:
            self._refresh_logs()

    def _refresh_rooms(self, *args):
        query = self.room_search.text().strip().casefold()
        self.visible_rooms = [r for r in self.model.rooms if query in f"{r.name} {r.caption}".casefold()]
        self.room_list.blockSignals(True)
        self.room_list.clear()
        for room in self.visible_rooms:
            self.room_list.addItem(f"{room.name}\n{room.caption}")
        if self.model.selected in self.visible_rooms:
            self.room_list.setCurrentRow(self.visible_rooms.index(self.model.selected))
        self.room_list.blockSignals(False)

    def select_room(self, index):
        if index < 0 or index >= len(self.visible_rooms):
            return
        self.model.selected = self.visible_rooms[index]
        room = self.model.selected
        self.sync_room_inputs()
        self.emit("INFO", "room.selected", "收藏", f"已选择“{room.name}”。")
        self._refresh_state()

    def sync_room_inputs(self):
        room = self.model.selected
        self.platform_box.setCurrentText(room.platform)
        self.room_input.setText(room.number)
        self.room_name.setText(room.name)
        self._refresh_rooms()

    def input_platform_changed(self, *args):
        # Input changes never replace an active playback session.
        pass

    def read_room(self):
        value = self.room_input.text().strip()
        if not value.isascii() or not value.isdigit() or not 0 < len(value) <= 15 or int(value) < 1:
            self.emit("WARN", "input.room_invalid", "界面", "原型请使用数字房间号；正式版支持平台链接。",
                      impact="start_blocked", presentation="banner",
                      dedupe_key="input.room_invalid")
            return None
        platform, number = self.platform_box.currentText(), str(int(value))
        existing = next((r for r in self.model.rooms if r.key == f"{platform}:{number}"), None)
        return existing or Room(platform, number, self.room_name.text().strip()[:80] or f"{platform} · {number}")

    def save_room(self):
        room = self.read_room()
        if room is None:
            return
        room.name = self.room_name.text().strip()[:80] or room.name
        if not any(r.key == room.key for r in self.model.rooms):
            self.model.rooms.append(room)
        self.model.selected = room
        self._refresh_rooms()
        self._refresh_state()
        self.emit("INFO", "room.saved", "收藏", f"已保存“{room.name}”，索引 {room.key}。", presentation="toast")

    def emit(self, level, code, source, message, **kwargs):
        event = self.model.emit(level, code, source, message, **kwargs)
        self.last_activity.setText(f"{event.occurred_at[11:19]} · {level} · {message[:42]}")
        self.last_activity.setToolTip(message)
        self._refresh_logs()
        if event.presentation == "toast":
            self.show_toast(message)
        elif event.presentation in ("banner", "strong"):
            self.render_notice()
            if event.presentation == "strong" and self.recovery_options["strong"].isChecked():
                self.show_strong_notice(event)
        return event

    def show_toast(self, message):
        self.toast_generation += 1
        generation = self.toast_generation
        self.toast.setText("✓ " + message[:32])
        self.toast.setToolTip(message)
        QTimer.singleShot(3500, lambda: self.toast.clear() if generation == self.toast_generation else None)

    def set_state(self, state, context=None, clear_notice=False):
        self.model.state = state
        self._refresh_state(context)
        if clear_notice:
            self.model.alert = None
            self.notice.hide()
            self.close_strong_notice()

    def _refresh_state(self, context=None):
        room = self.model.active or self.model.selected
        self.status_room.setText(f"{room.name}  ·  {room.caption}")
        self.status_title.setText(STATE_LABELS[self.model.state])
        self.status_title.setProperty("state", self.model.state)
        self.status_title.style().unpolish(self.status_title)
        self.status_title.style().polish(self.status_title)
        defaults = {
            "idle": "选择房间后，在 PotPlayer 独立窗口观看。",
            "resolving": "正在检查平台与画质（演示）。",
            "connecting": "正在发送播放请求（演示）。",
            "playing": "演示状态；未打开真实媒体，不表示已验证解码。",
            "paused": "由你暂停。继续演示会回到直播位置。",
            "reconnecting": "等待恢复；每次尝试写日志，不反复弹窗。",
            "stopped": "你主动停止了播放，自动恢复已取消。",
            "unavailable": "当前房间暂未开播，可以选择其他收藏。",
            "error": "没有可用播放线路。可重试或运行连接检查。",
        }
        self.status_context.setText(context or defaults[self.model.state])
        active = self.model.state in ("playing", "paused", "reconnecting", "connecting", "resolving")
        self.pause_button.setEnabled(self.model.state in ("playing", "paused"))
        self.pause_button.setText("继续直播" if self.model.state == "paused" else "暂停")
        self.stop_button.setEnabled(active or self.model.state in ("error", "unavailable"))
        self.start_button.setEnabled(self.model.state not in ("resolving", "connecting"))
        self.reconnect_button.setEnabled(self.model.active is not None)

    def render_notice(self):
        event = self.model.alert
        if event is None:
            self.notice.hide();return
        titles = {
            "room.offline": "房间暂未开播",
            "network.tls_failed": "无法建立直播连接",
            "playback.retrying": "直播连接中断，正在恢复",
            "playback.recovery_exhausted": "自动恢复未成功",
            "input.room_invalid": "请检查房间输入",
        }
        self.notice.setProperty("level", event.level)
        self.notice.style().unpolish(self.notice);self.notice.style().polish(self.notice)
        self.notice_level.setText(event.level)
        self.notice_level.setProperty("level", event.level)
        self.notice_level.style().unpolish(self.notice_level);self.notice_level.style().polish(self.notice_level)
        self.notice_title.setText(titles.get(event.code, event.message))
        self.notice_body.setText(event.message)
        details = "\n".join([f"事件：{event.code}", f"目标：{event.target or '应用'}",
                             *[f"{k}：{v}" for k, v in event.details.items()]])
        self.notice_details.setText(details)
        self.notice_details.hide()
        self.notice_details_button.setText("展开技术详情")
        self.notice_action_button.setText("选择其他房间" if event.code == "room.offline" else
                                          "检查连接" if event.level in ("ERROR", "CRITICAL") else "重新尝试")
        self.notice.show()

    def notice_action(self):
        event = self.model.alert
        if event and event.code == "room.offline":
            self.go_page(0)
            self.room_list.setFocus()
        elif event and event.level in ("ERROR", "CRITICAL"):
            self.close_strong_notice()
            self.go_page(4)
        else:
            self.start_demo()

    def toggle_notice_details(self):
        visible = not self.notice_details.isVisible()
        self.notice_details.setVisible(visible)
        self.notice_details_button.setText("收起技术详情" if visible else "展开技术详情")

    def show_strong_notice(self, event):
        key = f"{event.session_id}:{event.dedupe_key or event.code}"
        if key in self.model.acknowledged:
            return
        self.model.acknowledged.add(key)
        self.notification_count += 1
        if self.strong_dialog is not None:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("播放失败 · P0 演示")
        dialog.setMinimumWidth(470)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(26, 24, 26, 24)
        layout.setSpacing(16)
        layout.addWidget(text("当前无法播放", "Title"))
        layout.addWidget(text(event.message, "StatusContext", True))
        layout.addWidget(text("故障会继续显示在主界面。相同会话的同类故障不重复弹窗。", "Muted", True))
        actions = QHBoxLayout()
        actions.addWidget(button("查看日志", lambda: self.dialog_action(dialog, 3)))
        actions.addWidget(button("检查连接", lambda: self.dialog_action(dialog, 4), "Primary"))
        actions.addWidget(button("知道了", dialog.accept))
        layout.addLayout(actions)
        dialog.finished.connect(lambda _: self._dialog_closed(dialog))
        self.strong_dialog = dialog
        dialog.show()  # Non-blocking: timers and the UI continue to process events.

    def dialog_action(self, dialog, page):
        dialog.accept();self.go_page(page)

    def _dialog_closed(self, dialog):
        if self.strong_dialog is dialog:
            self.strong_dialog = None
        dialog.deleteLater()

    def close_strong_notice(self):
        if self.strong_dialog is not None:
            self.strong_dialog.accept()

    def invalidate_pending(self):
        self.generation += 1
        return self.generation

    def schedule(self, delay_ms, generation, callback):
        def run():
            if generation == self.generation:
                callback()
            else:
                self.emit("DEBUG", "task.stale_ignored", "会话", "已忽略旧演示任务的迟到结果。")
        QTimer.singleShot(delay_ms, run)

    def start_demo(self, reconnect=False):
        room = self.model.active if reconnect and self.model.active else self.read_room()
        if room is None:
            return
        generation = self.invalidate_pending()
        self.model.selected = room
        self.model.new_session()
        self.set_state("resolving", clear_notice=True)
        self.emit("INFO", "playback.resolve_requested", "播放", f"开始解析 {room.caption}（演示）。")
        def connecting():
            self.set_state("connecting")
            self.emit("INFO", "source.ready", "取流", "示例媒体已就绪，正在发送播放器请求。")
        def ready():
            self.set_state("playing", clear_notice=True)
            self.emit("INFO", "playback.demo_ready", "播放器", "演示播放状态已就绪；未启动真实播放器。", presentation="toast")
        self.schedule(350, generation, connecting)
        self.schedule(800, generation, ready)

    def stop_demo(self):
        self.invalidate_pending()
        self.set_state("stopped", clear_notice=True)
        self.emit("INFO", "playback.user_stopped", "播放", "你主动停止了播放，自动恢复已取消。", presentation="toast")

    def toggle_pause(self):
        if self.model.state == "paused":
            self.emit("INFO", "playback.user_resumed", "播放", "继续演示，将重新连接直播。")
            self.start_demo(reconnect=True)
        elif self.model.state == "playing":
            self.invalidate_pending()
            self.set_state("paused")
            self.emit("INFO", "playback.user_paused", "播放", "你暂停了播放，自动恢复暂不执行。", presentation="toast")

    def offline_demo(self):
        self.invalidate_pending()
        self.model.selected = self.model.rooms[1]
        self.model.new_session()
        self.sync_room_inputs()
        self.set_state("unavailable", clear_notice=True)
        self.emit("WARN", "room.offline", "平台", "B 站 · 35 暂未开播，本次播放没有启动。",
                  impact="start_blocked", presentation="banner", dedupe_key="room.offline")
        self.go_page(0)

    def tls_demo(self, new_session=True):
        self.invalidate_pending()
        if new_session or self.model.session_id is None:
            self.close_strong_notice()
            self.model.selected = self.model.rooms[0]
            self.model.new_session()
            self.sync_room_inputs()
        self.set_state("error")
        self.emit("ERROR", "network.tls_failed", "CDN", "直播连接的证书校验失败，目前没有可用线路。",
                  impact="start_blocked", presentation="strong", dedupe_key="network.tls_failed",
                  details={"host": "stream-beijing-edu-121-194-9-3.edgesrv.com", "stage": "CDN",
                           "reason": "TLS_CERTIFICATE_VERIFY_FAILED", "network_mode": "direct"})

    def repeat_tls_demo(self):
        if self.model.state != "error":
            self.tls_demo()
        self.tls_demo(new_session=False)

    def recovery_demo(self, success):
        generation = self.invalidate_pending()
        self.model.selected = self.model.rooms[0]
        self.model.new_session()
        self.sync_room_inputs()
        if not self.recovery_options["auto"].isChecked():
            self.set_state("error", clear_notice=True)
            self.emit("WARN", "playback.auto_disabled", "恢复", "自动恢复已关闭，连接中断后需要手动重新连接。",
                      impact="interrupted", presentation="banner", dedupe_key="playback.auto_disabled")
            return
        self.set_state("reconnecting", clear_notice=True)
        self.emit("WARN", "playback.retrying", "恢复", "直播连接已中断，正在尝试恢复。",
                  impact="interrupted", presentation="banner", dedupe_key="playback.retrying",
                  details={"attempt": 1})
        def attempt(number):
            self.emit("WARN", "playback.retry_attempt", "恢复", f"第 {number} 次恢复尝试（演示）。",
                      impact="interrupted", details={"attempt": number})
            self._refresh_state(f"第 {number} 次尝试。每次写日志，不逐次弹出确认框。")
        for number in (1, 2, 3):
            self.schedule(number * 550, generation, lambda n=number: attempt(n))
        def finish():
            if success:
                self.set_state("playing", clear_notice=True)
                self.emit("INFO", "playback.recovered", "恢复", "连接演示已恢复，当前故障告示已清除。", presentation="toast")
            else:
                self.set_state("error")
                self.emit("ERROR", "playback.recovery_exhausted", "恢复", "多次恢复仍未成功，播放已中断。",
                          impact="interrupted", presentation="strong", dedupe_key="playback.recovery_exhausted",
                          details={"attempt": 3})
        self.schedule(2200, generation, finish)

    def setting_changed(self, key, caption, enabled):
        self.emit("INFO", "settings.option_changed", "设置", f"{caption}：{'开启' if enabled else '关闭'}（演示）。",
                  presentation="toast")
        if key == "auto" and not enabled and self.model.state == "reconnecting":
            self.invalidate_pending()
            self.set_state("error")
            self.emit("WARN", "playback.auto_disabled", "恢复", "已取消自动恢复；连接仍然中断，可手动重新连接。",
                      impact="interrupted", presentation="banner", dedupe_key="playback.auto_disabled")

    def login_success_demo(self, platform="B 站"):
        self.model.logged_in[platform] = True
        self.account_status[platform].setText("已登录 · 仅演示，未导入真实账号")
        self.emit("INFO", "account.demo_login_succeeded", "账号", f"{platform}登录演示成功。", presentation="toast",
                  target=f"account:{platform}")

    def login_success_while_failed(self):
        self.tls_demo()
        self.close_strong_notice()
        self.login_success_demo()

    def clear_login_demo(self, platform):
        self.model.logged_in[platform] = False
        self.account_status[platform].setText("未登录 · 演示状态")
        self.emit("INFO", "account.demo_cleared", "账号", f"已清除{platform}演示登录。", presentation="toast",
                  target=f"account:{platform}")

    def show_import_placeholder(self):
        self.emit("INFO", "account.import_preview", "账号", "P0 不接受真实 Cookie；账号导入将在 P4 接入。", presentation="toast",
                  target="account")

    def show_login_demo(self, platform):
        if self.account_dialog is not None:
            self.account_dialog.raise_();return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{platform} · 扫码布局预览")
        dialog.setFixedWidth(430)
        box = QVBoxLayout(dialog);box.setContentsMargins(28, 26, 28, 26);box.setSpacing(18)
        box.addWidget(text("扫码登录", "Title"))
        box.addWidget(text(f"使用{platform}手机 App 扫描（正式版流程）。", "Muted", True))
        qr = text("二维码布局示意\n\nP0 尚未接入真实扫码", "StatusContext", True)
        qr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        qr.setMinimumHeight(250)
        qr.setStyleSheet("background:#fff;border:1px dashed #c5d0e2;border-radius:10px;")
        box.addWidget(qr)
        box.addWidget(text("在手机上确认后，账号状态更新；播放故障状态保持独立。", "Muted", True))
        box.addWidget(button("模拟登录成功", lambda: (self.login_success_demo(platform), dialog.accept()), "Primary"))
        box.addWidget(button("取消", dialog.reject))
        def closed(_):
            self.account_dialog = None
            self.emit("INFO", "account.login_preview_closed", "账号", "扫码布局预览已关闭。", target=f"account:{platform}")
            dialog.deleteLater()
        dialog.finished.connect(closed)
        self.account_dialog = dialog
        self.emit("INFO", "account.login_preview_opened", "账号", "已打开扫码布局预览。", target=f"account:{platform}")
        dialog.show()

    def run_diagnostic_demo(self):
        self.close_strong_notice()
        self.emit("INFO", "diagnostic.started", "诊断", "开始演示连接检查；没有真实网络请求。")
        if self.model.state == "error":
            self.diagnostic_result.setText("示例结果：房间接口成功 → CDN 证书失败 → 尚未交给播放器。\n可查看日志中的公开域名与错误代码。")
            self.emit("WARN", "diagnostic.demo_failed", "诊断", "连接检查示例定位到 CDN TLS 失败。",
                      details={"stage": "CDN", "reason": "TLS_CERTIFICATE_VERIFY_FAILED"})
        else:
            self.diagnostic_result.setText("示例结果：房间接口成功 → CDN 可读取 → 播放器解码待实际验证。")
            self.emit("INFO", "diagnostic.demo_completed", "诊断", "连接检查演示已完成；没有验证真实解码。", presentation="toast")
        self.go_page(4)

    def run_picked_scene(self):
        actions = {1: self.offline_demo, 2: self.tls_demo, 3: lambda: self.recovery_demo(True),
                   4: lambda: self.recovery_demo(False), 5: self.repeat_tls_demo,
                   6: self.login_success_while_failed, 7: self.stop_demo}
        action = actions.get(self.scene_picker.currentIndex())
        if action:
            action()

    def _refresh_logs(self, *args):
        if not hasattr(self, "log_table"):
            return
        level = self.level_filter.currentText()
        query = self.log_search.text().casefold().strip()
        events = [e for e in reversed(self.model.events)
                  if (level == "全部等级" or e.level == level)
                  and query in f"{e.message} {e.code} {e.source} {e.target}".casefold()]
        self.filtered_events = events
        self.log_table.blockSignals(True)
        self.log_table.setRowCount(len(events))
        colors = {"DEBUG": "#8993a6", "INFO": "#3867c4", "WARN": "#a87318", "ERROR": "#bf3045", "CRITICAL": "#982138"}
        for index, event in enumerate(events):
            values = (event.occurred_at[11:19], event.level, event.source, event.target or "—", event.message)
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                if col == 1:
                    item.setForeground(QColor(colors[event.level]))
                self.log_table.setItem(index, col, item)
            self.log_table.setRowHeight(index, 45)
        self.log_table.blockSignals(False)
        self.log_count.setText(f"{len(events)} / {len(self.model.events)} 条活动 · P0 仅存于内存 · 导出不含登录凭据")

    def show_log_detail(self):
        rows = self.log_table.selectionModel().selectedRows()
        if rows and rows[0].row() < len(self.filtered_events):
            self.log_detail.setPlainText(json.dumps(self.filtered_events[rows[0].row()].to_dict(), ensure_ascii=False, indent=2))

    def export_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出 P0 演示日志", "P0-demo-events.json", "JSON 文件 (*.json)")
        if path:
            try:
                Path(path).write_text(json.dumps(self.model.export(), ensure_ascii=False, indent=2), encoding="utf-8")
                self.emit("INFO", "log.demo_exported", "日志", "演示日志已导出。", presentation="toast")
            except OSError:
                self.emit("ERROR", "log.export_failed", "日志", "无法导出文件，请选择可写目录。",
                          presentation="strong", dedupe_key="log.export_failed")

    def closeEvent(self, event):
        self.invalidate_pending()
        self.close_strong_notice()
        if self.account_dialog is not None:
            self.account_dialog.reject()
        super().closeEvent(event)


def make_application():
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("本地直播 P0")
    application.setStyle("Fusion")
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(14)
    application.setFont(font)
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    application.setStyleSheet((base / "theme.qss").read_text(encoding="utf-8"))
    return application


def main():
    parser = argparse.ArgumentParser(description="本地直播 P0：Qt 交互原型，全部为演示数据")
    parser.add_argument("--self-test", type=Path)
    parser.add_argument("--screenshots", type=Path)
    args = parser.parse_args()
    application = make_application()
    window = Prototype()
    window.show()
    if args.self_test:
        from validate import validate_prototype
        QTimer.singleShot(150, lambda: validate_prototype(application, window, args.self_test, args.screenshots))
    raise SystemExit(application.exec())


if __name__ == "__main__":
    main()

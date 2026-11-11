"""Temporary P8-A test window. No production favorites or login store changes."""
import copy
import json
from pathlib import Path
import queue
import threading

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QLineEdit,
                              QPlainTextEdit, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..bangumi_validation import error_record, evaluate_gate, inspect_matrix, new_report, run_native
from ..settings import atomic_write
from .qr_dialog import QrLoginDialog
from .widgets import button, row, text


class ValidationWindow(QWidget):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.jar = None
        self.dialog = None
        self.closed = False
        self.cancel = threading.Event()
        self.worker = None
        self.queue = queue.Queue()
        self.report = new_report()
        self.setWindowTitle("P8-A · 番剧可行性验证")
        self.setMinimumSize(920, 650)
        self.resize(1080, 820)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(text("番剧验证 · P8-A", "PageTitle"))
        layout.addWidget(text("独立数据与本机 PotPlayer；登录仅在内存中保留。先查询匿名，再扫码查询会员。", "Muted", True))
        self.account = text("账号：匿名。会员是否有效及单集资格以平台返回为准。", "StatusContext", True)
        layout.addWidget(self.account)
        self.scan_button = button("B 站扫码登录", self.login, "Primary")
        self.anonymous_button = button("查询匿名六集", lambda: self.inspect(False))
        self.member_button = button("查询会员六集", lambda: self.inspect(True))
        self.forget_button = button("退出登录", self.forget)
        layout.addLayout(row(self.scan_button, self.anonymous_button, self.member_button, self.forget_button))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["作品 / 集数", "EP / cid", "账号 / 资格", "实际视频轨道", "原生验证"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 190)
        self.table.setColumnWidth(1, 170)
        self.table.setColumnWidth(2, 170)
        self.table.setColumnWidth(3, 240)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.itemSelectionChanged.connect(self.selection_changed)
        layout.addWidget(self.table, 2)
        self.input = QLineEdit("ep693247")
        self.input.setPlaceholderText("EP / SS 编号或链接；六集之外可单独验证")
        self.track = QComboBox()
        self.track.addItem("最高实际可用 SDR（重新查询）", None)
        self.requested = QComboBox()
        for title, value in (("请求最高 SDR", 127), ("请求 1080P 高码率", 112), ("请求普通 1080P", 80)):
            self.requested.addItem(title, value)
        self.session_choice = QComboBox()
        self.session_choice.addItem("匿名", False)
        self.session_choice.addItem("扫码账号", True)
        layout.addLayout(row(self.input, self.track, self.requested, self.session_choice))
        self.player_path = QLineEdit(args.player)
        layout.addLayout(row(text("PotPlayer"), self.player_path))
        self.full = QCheckBox("当前集完整观看（约 24 分钟，结束后自动检查控制与清理）")
        self.full.setChecked(args.full_episode)
        layout.addWidget(self.full)
        self.play_button = button("当前集五分钟实播", self.play_current, "Primary")
        self.matrix_button = button("会员六集顺序实播", self.play_matrix)
        self.stop_button = button("取消并关闭专用播放器", self.stop)
        layout.addLayout(row(self.play_button, self.matrix_button, self.stop_button))
        self.baseline = QCheckBox("已在同网络、同账号的官网核对：整集可看，实际高清档位一致")
        self.av = QCheckBox("已人工确认本工具播放内容正确，声音、画面与音画同步正常")
        self.mainland = QCheckBox("本轮在中国大陆网络完成，已关闭 VPN / 全局隧道")
        layout.addWidget(self.baseline)
        layout.addWidget(self.av)
        layout.addWidget(self.mainland)
        self.status = text("等待开始。查询和实播均使用 direct；五分钟实播会自动测试拖动、暂停、续期与退出清理。", "StatusContext", True)
        layout.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(1200)
        layout.addWidget(self.log, 1)
        layout.addLayout(row(button("保存脱敏报告", self.save), text(str(args.report), "Caption", True)))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.drain)
        self.timer.start(100)
        self.next_checkpoint = 0
        if args.show_login:
            QTimer.singleShot(300, self.login)

    def emit(self, message):
        self.log.appendPlainText(message)
        self.status.setText(message)

    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def work(self, function):
        if self.busy() or self.closed:
            self.emit("上一项验证仍在运行或清理，请等待完成。")
            return
        self.cancel = threading.Event()
        cancel = self.cancel
        self.set_busy(True)
        def run():
            try:
                function(cancel)
            except Exception as exc:
                self.queue.put(("message", error_record(exc)["message"]))
            finally:
                self.queue.put(("done", None))
        self.worker = threading.Thread(target=run, name="p8a-validation", daemon=False)
        self.worker.start()

    def set_busy(self, busy):
        for widget in (self.scan_button, self.anonymous_button, self.member_button, self.forget_button,
                       self.play_button, self.matrix_button, self.input, self.track, self.requested, self.player_path,
                       self.session_choice, self.full):
            widget.setEnabled(not busy)

    def login(self):
        if self.dialog:
            self.dialog.raise_()
            return
        def success(jar):
            self.jar = jar
            self.session_choice.setCurrentIndex(1)
            self.account.setText("扫码已确认。点击“查询会员六集”验证大会员状态和实际整集资格。")
            self.emit("[INFO] 登录会话已载入内存，不写入稳定版或磁盘。")
            if self.args.auto_member:
                self.play_matrix()
        self.dialog = QrLoginDialog(self, "bilibili", success, lambda: setattr(self, "dialog", None),
            on_event=lambda code, message, **kw: self.log.appendPlainText(f"[{kw.get('level','INFO')}] {message}"))
        self.dialog.show()

    def forget(self):
        self.jar = None
        self.session_choice.setCurrentIndex(0)
        self.account.setText("账号：匿名。内存登录会话已清除。")

    def inspect(self, member):
        if member and not self.jar:
            self.emit("请先扫码登录，再查询会员样本。")
            return
        jar = copy.deepcopy(self.jar) if member else None
        def run(cancel):
            inspected = inspect_matrix(jar, cancel=cancel, on_row=lambda r: self.queue.put(("row", r)))
            self.queue.put(("network", inspected["network"]))
        self.emit("[INFO] 正在查询六集" + ("会员" if member else "匿名") + "资格与实际轨道…")
        self.work(run)

    def add_row(self, value):
        self.report["samples"].append(value)
        ep, account = value["episode"], value.get("account") or {}
        r = self.table.rowCount()
        self.table.insertRow(r)
        values = (value["season_title"] + " · " + ep.get("number", ""),
                  f"{ep['ep_id']} / {ep.get('cid', '?')}",
                  account.get("login", "未验证") + " / " + value["status"].removeprefix("pgc."),
                  (value.get("media") or {}).get("actual_name", value.get("message", "")), "尚未实播")
        for c, title in enumerate(values):
            item = QTableWidgetItem(title)
            item.setToolTip(title)
            self.table.setItem(r, c, item)
        self.table.item(r, 0).setData(Qt.ItemDataRole.UserRole, value)
        if account:
            self.account.setText(f"最近查询：{account['login']}；大会员：{account['vip']}；当前集：{value['status']}。")
        self.log.appendPlainText(f"[{ 'INFO' if value['status']=='pgc.whole' else 'WARN'}] EP{ep['ep_id']}：{value.get('message','')}")

    def selection_changed(self):
        r = self.table.currentRow()
        if r < 0 or self.busy():
            return
        value = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        self.input.setText("ep" + str(value["episode"]["ep_id"]))
        self.track.clear()
        self.track.addItem("最高实际可用 SDR（重新查询）", None)
        for t in value.get("available_video_tracks", []):
            self.track.addItem(t["label"] + f" · {t['bandwidth']/1000000:.2f} Mbps", t["key"])
        # Anonymous and logged-in rows remain distinct test cases.
        self.session_choice.setCurrentIndex(1 if value.get("account", {}).get("login") == "authenticated" else 0)

    def play_current(self):
        value, key, requested = self.input.text(), self.track.currentData(), self.requested.currentData()
        member = self.session_choice.currentData()
        if member and not self.jar:
            self.emit("所选会员记录需重新扫码登录。")
            return
        jar = copy.deepcopy(self.jar) if member else None
        player = self.player_path.text()
        full = self.full.isChecked()
        def run(cancel):
            result = run_native(value, jar, self.args.data_dir, player, video_key=key, requested=requested,
                full_episode=full, cancel=cancel, on_progress=lambda msg: self.queue.put(("message", msg)))
            self.queue.put(("native", result))
        self.work(run)

    def play_matrix(self):
        if not self.jar:
            self.emit("会员整集验证需要先扫码登录。")
            return
        jar, player, full = copy.deepcopy(self.jar), self.player_path.text(), self.full.isChecked()
        def run(cancel):
            inspected = inspect_matrix(jar, cancel=cancel, on_row=lambda r: self.queue.put(("row", r)))
            first = True
            for value in inspected["samples"]:
                if cancel.is_set():
                    return
                if value["status"] != "pgc.whole" or not value.get("identity_matches_sample"):
                    continue
                result = run_native("ep" + str(value["episode"]["ep_id"]), jar, self.args.data_dir, player,
                    full_episode=full and first, cancel=cancel, on_progress=lambda msg: self.queue.put(("message", msg)))
                self.queue.put(("native", result))
                first = False
        self.work(run)

    def apply_native(self, result):
        media = result.get("media") or {}
        matches = [r for r in self.report["samples"] if r["episode"]["ep_id"] == media.get("episode", {}).get("ep_id")
                   and r.get("account", {}).get("login") == media.get("account", {}).get("login")]
        if matches:
            value = matches[-1]
            value["native"] = result
            value.setdefault("native_runs", []).append(result)
            for i in range(self.table.rowCount()):
                item = self.table.item(i, 0)
                if item.data(Qt.ItemDataRole.UserRole).get("episode") == value["episode"] and i == self.report["samples"].index(value):
                    self.table.item(i, 4).setText(f"{result['status']} · {result.get('continuous_seconds', 0)} 秒")
        else:
            self.report.setdefault("additional_native_runs", []).append(result)
        if self.args.proxy_configured and result["status"] == "passed":
            self.report["d2_actual_api_and_cdn_passed"] = True
        self.emit(f"[{ 'INFO' if result['status']=='passed' else 'ERROR'}] 实播结果：{result['status']}；{result.get('message','检查完成。')}")

    def stop(self):
        self.cancel.set()
        self.emit("[INFO] 已取消验证；等待在途请求结束并清理专用播放器。")

    def drain(self):
        while not self.queue.empty():
            kind, payload = self.queue.get_nowait()
            if kind == "message":
                self.emit(payload)
            elif kind == "row":
                self.add_row(payload)
            elif kind == "native":
                self.apply_native(payload)
            elif kind == "network":
                self.report["network"] = payload
            elif kind == "done":
                self.set_busy(False)
                self.save()
        if self.closed and not self.busy():
            self.save()
            self.timer.stop()
            self.close()
        elif self.busy():
            import time
            if time.monotonic() >= self.next_checkpoint:
                self.save()
                self.next_checkpoint = time.monotonic() + 10

    def save(self):
        self.report["human_checks"] = {"official_same_account_baseline": "confirmed" if self.baseline.isChecked() else "unverified",
                                      "picture_audio_sync": "confirmed" if self.av.isChecked() else "unverified"}
        self.report["network"]["d3_mainland_without_vpn"] = "user_confirmed" if self.mainland.isChecked() else "unverified"
        evaluate_gate(self.report)
        atomic_write(self.args.report, json.dumps(self.report, ensure_ascii=False, indent=2).encode("utf-8"))

    def closeEvent(self, event):
        if self.dialog:
            self.dialog.close()
        self.cancel.set()
        if self.busy():
            self.closed = True
            self.hide()
            event.ignore()
        else:
            self.save()
            self.jar = None
            self.timer.stop()
            event.accept()


def launch(args):
    app = QApplication([])
    app.setStyle("Fusion")
    app.setStyleSheet(Path(__file__).with_name("theme.qss").read_text(encoding="utf-8"))
    window = ValidationWindow(args)
    window.show()
    return app.exec()

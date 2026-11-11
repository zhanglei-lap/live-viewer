"""Bounded, virtual event table with independent filters and redacted exports."""
import json
from datetime import datetime

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor

from ..events import Event
from ..settings import atomic_write

COLORS = {"DEBUG": "#8993a6", "INFO": "#335fba", "WARN": "#97600e", "ERROR": "#b3293c", "CRITICAL": "#b3293c"}
SOURCES = {"app": "程序", "playback": "播放", "player": "播放器", "room": "房间",
           "history": "历史", "account": "账号", "settings": "设置", "log": "日志",
           "storage": "存储", "operation": "操作", "diagnostic": "诊断", "notice": "提醒", "vod": "视频", "pgc": "番剧"}


class LogModel(QAbstractTableModel):
    headers = ("时间", "等级", "来源", "目标", "活动")

    def __init__(self, records=(), parent=None):
        super().__init__(parent)
        self.rows = []
        self.ids = set()
        self.append(records)

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.headers)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.headers[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            try:
                stamp = datetime.fromisoformat(row["occurred_at"]).astimezone().strftime("%m-%d %H:%M:%S")
            except ValueError:
                stamp = row["occurred_at"]
            return (stamp, row["level"], SOURCES.get(row["source"], row["source"]), row["target"] or "—",
                    row["message"])[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row["message"] + "\n" + row["code"]
        if role == Qt.ItemDataRole.ForegroundRole and index.column() == 1:
            return QColor(COLORS[row["level"]])
        return None

    def append(self, records):
        new = []
        for record in records:
            try:
                record = Event(**record).record()  # Revalidate/sanitize disk and external callers.
            except (TypeError, ValueError):
                continue
            if record["event_id"] not in self.ids:
                self.ids.add(record["event_id"])
                new.append(record)
        if not new:
            return
        new.sort(key=lambda r: r["occurred_at"])
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows) + len(new) - 1)
        self.rows.extend(new)
        self.endInsertRows()
        excess = len(self.rows) - 2000
        if excess > 0:
            self.beginRemoveRows(QModelIndex(), 0, excess - 1)
            del self.rows[:excess]
            self.endRemoveRows()
            self.ids = {r["event_id"] for r in self.rows}


class LogFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.level = self.source = ""
        self.target = self.query = ""
        self.setDynamicSortFilter(True)
        self.sort(0, Qt.SortOrder.DescendingOrder)

    def change(self, *, level="", source="", target="", query=""):
        self.beginFilterChange()
        self.level, self.source = level, source
        self.target, self.query = target.strip().casefold(), query.strip().casefold()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, parent):
        value = self.sourceModel().rows[row]
        return (not self.level or value["level"] == self.level) and (not self.source or value["source"] == self.source) \
            and self.target in (value["target"] or "").casefold() \
            and self.query in " ".join(str(value[k] or "") for k in ("code", "message", "source", "target", "session_id")).casefold()

    def lessThan(self, left, right):
        return self.sourceModel().rows[left.row()]["occurred_at"] < self.sourceModel().rows[right.row()]["occurred_at"]

    def records(self):
        return [self.sourceModel().rows[self.mapToSource(self.index(i, 0)).row()] for i in range(self.rowCount())]

    def export(self, path):
        records = self.records()
        atomic_write(path, json.dumps({"schema_version": 1, "scope": "filtered", "events": records},
                                      ensure_ascii=False, indent=2).encode("utf-8"))
        return len(records)

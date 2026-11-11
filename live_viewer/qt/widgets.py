"""Shared widgets and vector icons from the accepted P0 design."""
from __future__ import annotations

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QStyle,
    QVBoxLayout, QWidget,
)



def text(value, role=None, wrap=False):
    widget = QLabel(value)
    widget.setTextFormat(Qt.TextFormat.PlainText)
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


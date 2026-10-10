"""Status Log group box: timestamped action/result lines + Clear Log."""

from __future__ import annotations

import html
from datetime import datetime

from PySide6.QtCore import Slot
from PySide6.QtWidgets import QGroupBox, QHBoxLayout, QPlainTextEdit, QPushButton, QVBoxLayout

from .theme import ThemeManager


class StatusLog(QGroupBox):
    def __init__(self, parent=None) -> None:
        super().__init__("Status Log", parent)
        self.view = QPlainTextEdit()
        self.view.setObjectName("statusLog")
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        clear = QPushButton("Clear Log")
        clear.clicked.connect(self.view.clear)
        lay = QHBoxLayout(self)
        lay.addWidget(self.view, 1)
        side = QVBoxLayout()
        side.addWidget(clear)
        side.addStretch(1)
        lay.addLayout(side)

    @Slot(str, str)
    def log(self, message: str, level: str = "info") -> None:
        """level: 'info' | 'ok' | 'warn' | 'error'."""
        c = ThemeManager.instance().colors
        color = {"ok": c.ok, "warn": c.warn, "error": c.bad}.get(level, c.text)
        tag = {"ok": "OK  ", "warn": "WARN", "error": "ERR "}.get(level, "    ")
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        self.view.appendHtml(
            f'<span style="color:{c.muted}">{ts}</span>&nbsp;&nbsp;'
            f'<span style="color:{color}"><b>{tag}</b></span>&nbsp;&nbsp;'
            f'<span style="color:{c.text}">{html.escape(message)}</span>'
        )

"""Registers tab — full register table plus a Bit Description panel."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import placeholder_regmap as rm
from ..theme import mono_font
from ..widgets import BitStrip, muted
from ._regtable import RegisterTable


class RegistersTab(QWidget):
    readAllRequested = Signal()
    registerReadRequested = Signal(int)  # addr
    registerWriteRequested = Signal(int, int)  # addr, value

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split)

        # ---- left: register table
        left = QGroupBox("Register Map")
        ll = QVBoxLayout(left)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name or address…")
        self.search.setClearButtonEnabled(True)
        self.read_one = QPushButton("Read")
        self.read_one.setToolTip("Read the selected register.")
        self.read_all = QPushButton("Read All")
        self.read_all.setToolTip("Read every register from the ADC.")
        bar.addWidget(self.search, 1)
        bar.addWidget(self.read_one)
        bar.addWidget(self.read_all)
        ll.addLayout(bar)
        self.table = RegisterTable(rm.REGISTERS)
        ll.addWidget(self.table)
        hint = muted(
            "PLACEHOLDER register list — replaced by regmap.yaml in Phase 2."
            if rm.PLACEHOLDER
            else ""
        )
        ll.addWidget(hint)
        split.addWidget(left)

        # ---- right: bit description
        right = QGroupBox("Bit Description")
        rl = QVBoxLayout(right)
        self.reg_title = QLabel("—")
        f = self.reg_title.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.reg_title.setFont(f)
        self.reg_meta = muted("")
        self.reg_desc = QLabel("")
        self.reg_desc.setWordWrap(True)
        self.fields = QTableWidget(0, 4)
        self.fields.setHorizontalHeaderLabels(["Bits", "Field", "Value", "Description"])
        self.fields.verticalHeader().setVisible(False)
        self.fields.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.fields.setSelectionMode(QAbstractItemView.NoSelection)
        self.fields.setWordWrap(True)
        h = self.fields.horizontalHeader()
        for c in (0, 1, 2):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(3, QHeaderView.Stretch)
        self.bits_strip = BitStrip()
        rl.addWidget(self.reg_title)
        rl.addWidget(self.reg_meta)
        rl.addWidget(self.bits_strip)
        rl.addWidget(self.reg_desc)
        rl.addWidget(self.fields, 1)
        split.addWidget(right)
        split.setSizes([520, 680])

        # ---- wiring
        self.search.textChanged.connect(self.table.set_filter)
        self.read_all.clicked.connect(self.readAllRequested)
        self.read_one.clicked.connect(self._read_selected)
        self.table.writeRequested.connect(self._on_write)
        self.table.currentRegisterChanged.connect(self._show_bits)
        self.table.selectRow(rm.REGISTERS.index(rm.register_by_addr(0x08)))

    # ---------------------------------------------------------------- internals
    def _read_selected(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.registerReadRequested.emit(self.table.register_at(r).addr)

    def _on_write(self, addr: int, value: int) -> None:
        self.registerWriteRequested.emit(addr, value)
        self._refresh_if_current(addr)

    def _refresh_if_current(self, addr: int) -> None:
        r = self.table.currentRow()
        if r >= 0 and self.table.register_at(r).addr == addr:
            self._show_bits(addr)

    def _show_bits(self, addr: int) -> None:
        reg = rm.register_by_addr(addr)
        if reg is None:
            return
        value = self.table.value_of(addr)
        self.reg_title.setText(f"{reg.name}")
        vtxt = f"0x{value:0{reg.hex_digits}X}" if value is not None else "not read"
        self.reg_meta.setText(f"Address 0x{addr:02X}  ·  {reg.width}-bit  ·  value {vtxt}")
        self.reg_desc.setText(reg.description)
        self.bits_strip.set_register(reg.width, reg.fields, value)
        flds = sorted(reg.fields, key=lambda f: -f.msb)
        if not flds:
            self.fields.setRowCount(1)
            self.fields.setItem(0, 0, QTableWidgetItem(f"{reg.width - 1}:0"))
            self.fields.setItem(0, 1, QTableWidgetItem(reg.name))
            self.fields.setItem(0, 2, QTableWidgetItem(vtxt))
            self.fields.setItem(0, 3, QTableWidgetItem(rm.PENDING))
        else:
            self.fields.setRowCount(len(flds))
            for i, fld in enumerate(flds):
                val = (
                    "—"
                    if value is None
                    else f"{fld.extract(value)}  (0b{fld.extract(value):0{fld.msb - fld.lsb + 1}b})"
                )
                for c, text in enumerate((fld.bits, fld.name, val, fld.description)):
                    it = QTableWidgetItem(text)
                    if c < 3:
                        it.setFont(mono_font())
                    self.fields.setItem(i, c, it)
        self.fields.resizeRowsToContents()

    # ---------------------------------------------------------------- public API
    @Slot(int, int)
    def set_register_value(self, addr: int, value: int) -> None:
        self.table.set_value(addr, value)
        self._refresh_if_current(addr)

    @Slot(dict)
    def set_register_values(self, values: dict) -> None:
        for a, v in values.items():
            self.table.set_value(a, v)
        r = self.table.currentRow()
        if r >= 0:
            self._show_bits(self.table.register_at(r).addr)

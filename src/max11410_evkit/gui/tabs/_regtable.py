"""Shared register table (Address | Name | Value (Hex)) for the Registers and Sequence tabs."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem

from ..placeholder_regmap import Register
from ..theme import mono_font
from ..widgets import HexDelegate

COL_ADDR, COL_NAME, COL_VALUE = 0, 1, 2


class RegisterTable(QTableWidget):
    writeRequested = Signal(int, int)  # addr, value — user edited a Value (Hex) cell
    currentRegisterChanged = Signal(int)  # addr

    def __init__(self, registers: list[Register], parent=None) -> None:
        super().__init__(len(registers), 3, parent)
        self._regs = registers
        self._row_of = {r.addr: i for i, r in enumerate(registers)}
        self._loading = False
        self.setHorizontalHeaderLabels(["Address", "Name", "Value (Hex)"])
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(26)
        h = self.horizontalHeader()
        h.setSectionResizeMode(COL_ADDR, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        h.setSectionResizeMode(COL_VALUE, QHeaderView.ResizeToContents)
        h.setMinimumSectionSize(90)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(
            QAbstractItemView.DoubleClicked
            | QAbstractItemView.EditKeyPressed
            | QAbstractItemView.AnyKeyPressed
        )
        self.setAlternatingRowColors(True)
        self.setItemDelegateForColumn(
            COL_VALUE, HexDelegate(lambda row: self._regs[row].hex_digits, self)
        )
        mono = mono_font()
        self._loading = True
        for i, reg in enumerate(registers):
            a = QTableWidgetItem(f"0x{reg.addr:02X}")
            a.setFont(mono)
            a.setFlags(a.flags() & ~Qt.ItemIsEditable)
            n = QTableWidgetItem(reg.name)
            n.setFlags(n.flags() & ~Qt.ItemIsEditable)
            n.setToolTip(f"{reg.name} — {reg.width}-bit")
            v = QTableWidgetItem("0x" + "-" * reg.hex_digits)
            v.setFont(mono)
            v.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            v.setToolTip("Double-click to edit; the value is written to the ADC on Enter.")
            self.setItem(i, COL_ADDR, a)
            self.setItem(i, COL_NAME, n)
            self.setItem(i, COL_VALUE, v)
        self._loading = False
        self.itemChanged.connect(self._on_item)
        self.currentCellChanged.connect(
            lambda r, *_: r >= 0 and self.currentRegisterChanged.emit(self._regs[r].addr)
        )

    def register_at(self, row: int) -> Register:
        return self._regs[row]

    def value_of(self, addr: int) -> int | None:
        row = self._row_of.get(addr)
        if row is None:
            return None
        try:
            return int(self.item(row, COL_VALUE).text(), 16)
        except ValueError:
            return None

    def set_value(self, addr: int, value: int) -> None:
        row = self._row_of.get(addr)
        if row is None:
            return
        self._loading = True
        self.item(row, COL_VALUE).setText(f"0x{value:0{self._regs[row].hex_digits}X}")
        self._loading = False

    def set_filter(self, text: str) -> None:
        t = text.strip().lower()
        for i, reg in enumerate(self._regs):
            self.setRowHidden(
                i, bool(t) and t not in reg.name.lower() and t not in f"0x{reg.addr:02x}"
            )

    def _on_item(self, it: QTableWidgetItem) -> None:
        if self._loading or it.column() != COL_VALUE:
            return
        reg = self._regs[it.row()]
        try:
            value = int(it.text(), 16) & ((1 << reg.width) - 1)
        except ValueError:
            return
        self.set_value(reg.addr, value)  # normalise formatting
        self.writeRequested.emit(reg.addr, value)

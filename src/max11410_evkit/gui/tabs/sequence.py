"""Sequence tab — sequencer microcode registers µC0..µC52 and sequencer control."""

from __future__ import annotations

from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .. import placeholder_regmap as rm
from ..theme import mono_font
from ..widgets import compact_form, hex_output, muted, primary_button
from ._regtable import RegisterTable


class HexSpinBox(QSpinBox):
    def __init__(self, lo: int, hi: int, parent=None) -> None:
        super().__init__(parent)
        self.setDisplayIntegerBase(16)
        self.setPrefix("0x")
        self.setRange(lo, hi)
        self.setFont(mono_font())

    def textFromValue(self, value: int) -> str:  # noqa: N802
        return f"{value:02X}"

    def valueFromText(self, text: str) -> int:  # noqa: N802
        return int(text.removeprefix("0x") or "0", 16)


class SequenceTab(QWidget):
    readAllRequested = Signal()
    microcodeWriteRequested = Signal(int, int)  # addr, value
    sequenceStartRequested = Signal(int)  # start address
    currentAddressRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        first, last = rm.MICROCODE[0].addr, rm.MICROCODE[-1].addr
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        left = QGroupBox(
            f"Microcode Registers  (µC0–µC{len(rm.MICROCODE) - 1}, 0x{first:02X}–0x{last:02X})"
        )
        ll = QVBoxLayout(left)
        self.table = RegisterTable(rm.MICROCODE)
        ll.addWidget(self.table)
        bar = QHBoxLayout()
        bar.addWidget(muted("Double-click a value to edit; Enter writes it to the ADC."))
        bar.addStretch(1)
        self.read_all = QPushButton("Read All")
        self.read_all.setToolTip("Read all microcode registers.")
        self.read_all.clicked.connect(self.readAllRequested)
        bar.addWidget(self.read_all)
        ll.addLayout(bar)
        root.addWidget(left, 3)

        right = QVBoxLayout()
        right.setSpacing(10)
        ctl = QGroupBox("Sequencer")
        f = compact_form()
        self.start_addr = HexSpinBox(first, last)
        self.start_addr.setToolTip("Microcode address where the sequencer starts (SEQ_START).")
        start = primary_button("Start Sequence", "Start the sequencer at the given address.")
        start.clicked.connect(lambda: self.sequenceStartRequested.emit(self.start_addr.value()))
        f.addRow("Start address", self.start_addr)
        f.addRow("", start)
        ctl.setLayout(f)
        right.addWidget(ctl)

        cur = QGroupBox("Current Address")
        cf = compact_form()
        self.cur_addr = hex_output(
            6, "Microcode address the sequencer is currently executing (GP_SEQ_ADDR)."
        )
        self.cur_name = muted("—")
        read_cur = QPushButton("Read Current Address")
        read_cur.clicked.connect(self.currentAddressRequested)
        cf.addRow("Address", self.cur_addr)
        cf.addRow("Register", self.cur_name)
        cf.addRow("", read_cur)
        cur.setLayout(cf)
        right.addWidget(cur)

        info = QGroupBox("About the Sequencer")
        il = QVBoxLayout(info)
        txt = QLabel(
            "The on-chip sequencer executes microcode stored in µC0–µC52, "
            "switching inputs, starting conversions and writing results to "
            "DATA0–DATA7 without host intervention."
        )
        txt.setWordWrap(True)
        txt.setProperty("role", "muted")
        il.addWidget(txt)
        right.addWidget(info)
        right.addStretch(1)
        root.addLayout(right, 2)

        self.table.writeRequested.connect(self.microcodeWriteRequested)

    # ---------------------------------------------------------------- public API
    @Slot(dict)
    def set_microcode_values(self, values: dict) -> None:
        for a, v in values.items():
            self.table.set_value(a, v)

    @Slot(int)
    def show_current_address(self, addr: int) -> None:
        self.cur_addr.setText(f"0x{addr:02X}")
        reg = rm.register_by_addr(addr)
        self.cur_name.setText(reg.name if reg else "—")

"""Other tab — channel thresholds, wait timer, clock source and GPIO modes."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import placeholder_regmap as rm
from ..theme import mono_font
from ..widgets import compact_form, muted, primary_button


def _centered(w: QWidget) -> QWidget:
    holder = QWidget()
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setAlignment(Qt.AlignCenter)
    lay.addWidget(w)
    return holder


class OtherTab(QWidget):
    # ---- user intents
    readAllRequested = Signal()
    thresholdUnitChanged = Signal(str)
    thresholdChanged = Signal(int, str, float, str)  # channel, "lower"|"upper", value, unit
    thresholdInterruptChanged = Signal(int, str, bool)  # channel, "tur"|"tor", enabled
    waitCalcRequested = Signal(int, int)  # wait, wait_ext
    waitStartRequested = Signal(int, int)
    clockSourceChanged = Signal(str)  # "internal" | "external"
    gpioModeChanged = Signal(int, str)  # gpio index (0/1), mode text

    COLS = ["Lower threshold", "Upper threshold", "TUR INT", "TOR INT"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addWidget(self._build_thresholds(), 3)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._build_wait())
        right.addWidget(self._build_clock())
        right.addWidget(self._build_gpio())
        right.addStretch(1)
        self.read_all_btn = QPushButton("Read All")
        self.read_all_btn.setToolTip(
            "Read thresholds, wait, clock and GPIO registers from the ADC."
        )
        self.read_all_btn.clicked.connect(self.readAllRequested)
        right.addWidget(self.read_all_btn, 0, Qt.AlignRight)
        root.addLayout(right, 2)

    # ---------------------------------------------------------------- builders
    def _build_thresholds(self) -> QGroupBox:
        g = QGroupBox("Channel Thresholds")
        lay = QVBoxLayout(g)
        top = QHBoxLayout()
        top.addWidget(muted("Out-of-range / under-range comparison per DATA channel."))
        top.addStretch(1)
        top.addWidget(QLabel("Threshold units"))
        self.units = QComboBox()
        self.units.addItems(rm.THRESHOLD_UNITS)
        self.units.setToolTip("Units used to enter and display thresholds (stored as LSB codes).")
        self.units.currentTextChanged.connect(self._on_units)
        top.addWidget(self.units)
        lay.addLayout(top)

        t = QTableWidget(8, len(self.COLS))
        t.setHorizontalHeaderLabels(self.COLS)
        t.setVerticalHeaderLabels([f"Channel {i}" for i in range(8)])
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        t.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (2, 3):
            t.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        t.verticalHeader().setSectionResizeMode(QHeaderView.Stretch)
        t.verticalHeader().setMinimumSectionSize(28)
        t.setSelectionBehavior(QAbstractItemView.SelectItems)
        t.setAlternatingRowColors(True)
        t.setFont(mono_font())
        t.horizontalHeaderItem(2).setToolTip(
            "Interrupt when the result is below the lower threshold."
        )
        t.horizontalHeaderItem(3).setToolTip(
            "Interrupt when the result is above the upper threshold."
        )
        self.int_boxes: list[tuple[QCheckBox, QCheckBox]] = []
        for r in range(8):
            for c in (0, 1):
                it = QTableWidgetItem("0")
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                t.setItem(r, c, it)
            tur, tor = QCheckBox(), QCheckBox()
            tur.toggled.connect(lambda on, ch=r: self.thresholdInterruptChanged.emit(ch, "tur", on))
            tor.toggled.connect(lambda on, ch=r: self.thresholdInterruptChanged.emit(ch, "tor", on))
            t.setCellWidget(r, 2, _centered(tur))
            t.setCellWidget(r, 3, _centered(tor))
            self.int_boxes.append((tur, tor))
        t.itemChanged.connect(self._on_item)
        self.table = t
        lay.addWidget(t)
        return g

    def _build_wait(self) -> QGroupBox:
        g = QGroupBox("Wait Timer")
        f = compact_form()
        self.wait = QSpinBox()
        self.wait.setRange(0, 255)
        self.wait.setToolTip("WAIT_START value (clock cycles, scaled by Wait Extension).")
        self.wait_ext = QSpinBox()
        self.wait_ext.setRange(0, 255)
        self.wait_ext.setToolTip("WAIT_EXT prescaler for longer delays.")
        calc = QPushButton("Calculate Wait Time")
        calc.setToolTip("Compute the delay from WAIT / WAIT_EXT at the 2.4576 MHz clock.")
        start = primary_button("Start Wait Time", "Write WAIT_START to begin the timer.")
        calc.clicked.connect(
            lambda: self.waitCalcRequested.emit(self.wait.value(), self.wait_ext.value())
        )
        start.clicked.connect(
            lambda: self.waitStartRequested.emit(self.wait.value(), self.wait_ext.value())
        )
        self.wait_result = QLabel("—")
        self.wait_result.setFont(mono_font())
        self.wait_result.setProperty("role", "value")
        f.addRow("Wait", self.wait)
        f.addRow("Wait extension", self.wait_ext)
        f.addRow("Wait time", self.wait_result)
        f.addRow(muted("Clock 2.4576 MHz"))
        btns = QHBoxLayout()
        btns.addWidget(calc)
        btns.addWidget(start)
        f.addRow(btns)
        g.setLayout(f)
        return g

    def _build_clock(self) -> QGroupBox:
        g = QGroupBox("Clock")
        f = compact_form()
        self.clock = QComboBox()
        self.clock.addItems(rm.CLOCK_SOURCES)
        self.clock.setToolTip("Modulator clock source (CTRL / GPIO clock input).")
        self.clock.currentIndexChanged.connect(
            lambda i: (
                (not self._loading)
                and self.clockSourceChanged.emit("internal" if i == 0 else "external")
            )
        )
        f.addRow("Source", self.clock)
        g.setLayout(f)
        return g

    def _build_gpio(self) -> QGroupBox:
        g = QGroupBox("GPIO")
        f = compact_form()
        self.gpio: list[QComboBox] = []
        for i in range(2):
            cb = QComboBox()
            cb.addItems(rm.GPIO_MODES)
            cb.setToolTip(f"Function of GPIO{i} (GP{i}_CTRL). Mode list is a placeholder.")
            cb.currentTextChanged.connect(
                lambda text, n=i: (not self._loading) and self.gpioModeChanged.emit(n, text)
            )
            f.addRow(f"GPIO{i} mode", cb)
            self.gpio.append(cb)
        g.setLayout(f)
        return g

    # ---------------------------------------------------------------- internals
    def _on_units(self, unit: str) -> None:
        if not self._loading:
            self.thresholdUnitChanged.emit(unit)

    def _on_item(self, it: QTableWidgetItem) -> None:
        if self._loading or it.column() > 1:
            return
        try:
            v = float(it.text())
        except ValueError:
            return
        self.thresholdChanged.emit(
            it.row(), "lower" if it.column() == 0 else "upper", v, self.units.currentText()
        )

    # ---------------------------------------------------------------- public API
    @Slot(list, str)
    def set_thresholds(self, rows: list, unit: str) -> None:
        """rows: 8 x (lower, upper) already expressed in ``unit``."""
        self._loading = True
        try:
            self.units.setCurrentText(unit)
            fmt = "{:.0f}" if unit == "LSB" else "{:.6g}"
            for r, (lo, hi) in enumerate(rows):
                self.table.item(r, 0).setText(fmt.format(lo))
                self.table.item(r, 1).setText(fmt.format(hi))
        finally:
            self._loading = False

    @Slot(list)
    def set_threshold_interrupts(self, flags: list) -> None:
        """flags: 8 x (tur, tor) booleans."""
        for (tur, tor), (a, b) in zip(self.int_boxes, flags, strict=False):
            for box, val in ((tur, a), (tor, b)):
                box.blockSignals(True)
                box.setChecked(bool(val))
                box.blockSignals(False)

    @Slot(float)
    def show_wait_time(self, seconds: float) -> None:
        self.wait_result.setText(f"{seconds * 1e3:.4f} ms")

    def set_wait(self, wait: int, wait_ext: int) -> None:
        self.wait.setValue(wait)
        self.wait_ext.setValue(wait_ext)

    def set_clock_source(self, source: str) -> None:
        self._loading = True
        self.clock.setCurrentIndex(0 if source == "internal" else 1)
        self._loading = False

    def set_gpio_mode(self, gpio: int, mode: str) -> None:
        self._loading = True
        self.gpio[gpio].setCurrentText(mode)
        self._loading = False

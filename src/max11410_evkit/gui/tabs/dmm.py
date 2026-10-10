"""DMM tab — large numeric readout with single / continuous measurement and a trend strip."""

from __future__ import annotations

from collections import deque

import numpy as np
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..placeholder_regmap import CAPTURE_RATES
from ..theme import ThemeManager, mono_font
from ..widgets import StatsPanel, ThemedPlot, compact_form, fmt_si, muted, primary_button, trace_pen

TREND_LEN = 300


class DmmTab(QWidget):
    measureRequested = Signal()
    continuousToggled = Signal(bool)
    rateChanged = Signal(float)
    averagingChanged = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hist: deque[float] = deque(maxlen=TREND_LEN)
        self._all: list[float] = []

        # ---- readout
        frame = QFrame()
        frame.setObjectName("readoutFrame")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(24, 16, 24, 16)
        top = QHBoxLayout()
        self.mode_label = muted("DC VOLTS")
        self.input_label = muted("AIN2 – GND")
        top.addWidget(self.mode_label)
        top.addStretch(1)
        top.addWidget(self.input_label)
        fl.addLayout(top)
        val_row = QHBoxLayout()
        val_row.addStretch(1)
        self.value = QLabel("—.——————")
        self.value.setProperty("role", "readout")
        f = mono_font(64)
        f.setBold(True)
        self.value.setFont(f)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.unit = QLabel("V")
        self.unit.setProperty("role", "unit")
        uf = mono_font(30)
        self.unit.setFont(uf)
        self.unit.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        val_row.addWidget(self.value)
        val_row.addSpacing(8)
        val_row.addWidget(self.unit, 0, Qt.AlignBottom)
        val_row.addStretch(1)
        fl.addLayout(val_row)
        bottom = QHBoxLayout()
        self.code_label = muted("code —")
        self.code_label.setFont(mono_font())
        self.rate_label = muted("")
        bottom.addWidget(self.code_label)
        bottom.addStretch(1)
        bottom.addWidget(self.rate_label)
        fl.addLayout(bottom)

        # ---- trend
        self.trend = ThemedPlot("Reading #", "", "Voltage", "V")
        self.trend.setMinimumHeight(170)
        self.trend.getAxis("bottom").enableAutoSIPrefix(False)
        self.trend_curve = self.trend.plot([], [], pen=trace_pen(1.6))
        ThemeManager.instance().changed.connect(lambda: self.trend_curve.setPen(trace_pen(1.6)))

        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(frame)
        trend_box = QGroupBox("Trend")
        tl = QVBoxLayout(trend_box)
        tl.addWidget(self.trend)
        left.addWidget(trend_box, 1)

        # ---- controls
        ctl = QGroupBox("Measurement")
        cf = compact_form()
        self.rate = QComboBox()
        for r in CAPTURE_RATES:
            self.rate.addItem(f"{r:g} sps", r)
        self.rate.setCurrentIndex(CAPTURE_RATES.index(59.8))
        self.rate.setToolTip("Output data rate used for DMM readings.")
        self.avg = QComboBox()
        for n in (1, 4, 16, 64):
            self.avg.addItem(f"{n}" if n > 1 else "Off", n)
        self.avg.setToolTip("Average N conversions per displayed reading.")
        self.measure = QPushButton("Capture")
        self.measure.setToolTip("Take a single reading.")
        self.run = primary_button(
            "Run continuous", "Start / stop continuous readings.", checkable=True
        )
        self.reset = QPushButton("Reset statistics")
        cf.addRow("Rate", self.rate)
        cf.addRow("Averaging", self.avg)
        btns = QHBoxLayout()
        btns.addWidget(self.measure)
        btns.addWidget(self.run)
        cf.addRow(btns)
        cf.addRow(self.reset)
        ctl.setLayout(cf)

        self.stats = StatsPanel(
            "Statistics",
            [
                ("mean", "Mean", lambda v: fmt_si(v, "V", 6)),
                ("std", "Std. deviation", lambda v: fmt_si(v, "V", 3)),
                ("max", "Maximum", lambda v: fmt_si(v, "V", 6)),
                ("min", "Minimum", lambda v: fmt_si(v, "V", 6)),
                ("n", "Readings", lambda v: f"{v:d}"),
            ],
        )
        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(ctl)
        self.stats.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        right.addWidget(self.stats, 1)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addLayout(left, 1)
        rw = QWidget()
        rw.setLayout(right)
        rw.setFixedWidth(280)
        right.setContentsMargins(0, 0, 0, 0)
        root.addWidget(rw)

        self.measure.clicked.connect(self.measureRequested)
        self.run.toggled.connect(self._on_run)
        self.rate.currentIndexChanged.connect(
            lambda _i: self.rateChanged.emit(self.rate.currentData())
        )
        self.avg.currentIndexChanged.connect(
            lambda _i: self.averagingChanged.emit(self.avg.currentData())
        )
        self.reset.clicked.connect(self.reset_statistics)
        self._update_rate_label()
        self.rate.currentIndexChanged.connect(lambda _i: self._update_rate_label())

    def _update_rate_label(self) -> None:
        self.rate_label.setText(f"{self.rate.currentData():g} sps · SINC4")

    def _on_run(self, on: bool) -> None:
        self.run.setText("Stop" if on else "Run continuous")
        self.measure.setEnabled(not on)
        self.continuousToggled.emit(on)

    # ---------------------------------------------------------------- public API
    @Slot(float, int)
    def show_reading(self, volts: float, code: int = 0) -> None:
        a = abs(volts)
        if a >= 1 or a == 0:
            text, unit = f"{volts:+.6f}", "V"
        elif a >= 1e-3:
            text, unit = f"{volts * 1e3:+.4f}", "mV"
        else:
            text, unit = f"{volts * 1e6:+.2f}", "µV"
        self.value.setText(text)
        self.unit.setText(unit)
        self.code_label.setText(f"code 0x{code & 0xFFFFFF:06X}")
        self._hist.append(volts)
        self._all.append(volts)
        y = np.fromiter(self._hist, float)
        self.trend_curve.setData(np.arange(len(self._all) - len(y), len(self._all)), y)
        arr = np.asarray(self._all)
        self.stats.set_values(
            {
                "mean": arr.mean(),
                "std": arr.std(ddof=1) if len(arr) > 1 else 0.0,
                "max": arr.max(),
                "min": arr.min(),
                "n": len(arr),
            }
        )

    def set_input_label(self, text: str) -> None:
        self.input_label.setText(text)

    def reset_statistics(self) -> None:
        self._hist.clear()
        self._all.clear()
        self.trend_curve.setData([], [])
        self.stats.clear()

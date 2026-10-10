"""Scope tab — time-domain capture plot with statistics."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QVBoxLayout, QWidget

from ..theme import ThemeManager
from ..widgets import CaptureBar, StatsPanel, ThemedPlot, fmt_si, muted, trace_pen


class ScopeTab(QWidget):
    captureRequested = Signal(float, int)  # rate_sps, n_samples
    displayUnitChanged = Signal(str)  # "LSB" | "Voltage"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._volts: np.ndarray | None = None
        self._rate = 1.0
        self._lsb = 1.0
        self._stats: dict = {}

        self.unit = QComboBox()
        self.unit.addItems(["Voltage", "LSB"])
        self.unit.setToolTip("Y-axis and statistics units.")
        self.bar = CaptureBar(leading=[muted("Display unit"), self.unit])
        self.bar.layout().insertSpacing(2, 6)
        self.plot = ThemedPlot("Time", "s", "Amplitude", "V")
        self.curve = self.plot.plot([], [], pen=trace_pen())
        ThemeManager.instance().changed.connect(lambda: self.curve.setPen(trace_pen()))

        self.stats = StatsPanel(
            "Statistics",
            [
                ("average", "Average", self._fmt),
                ("std_dev", "Std. deviation", self._fmt),
                ("max", "Maximum", self._fmt),
                ("min", "Minimum", self._fmt),
                ("p2p", "Peak-to-peak", self._fmt),
                ("fundamental_hz", "Fundamental", lambda v: fmt_si(v, "Hz", 3)),
                ("n", "Samples", lambda v: f"{v:d}"),
                ("rate", "Sample rate", lambda v: f"{v:g} sps"),
            ],
        )

        plot_box = QGroupBox("Time Domain")
        pl = QVBoxLayout(plot_box)
        pl.addWidget(self.bar)
        pl.addWidget(self.plot, 1)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addWidget(plot_box, 1)
        root.addWidget(self.stats)

        self.bar.captureClicked.connect(self.captureRequested)
        self.unit.currentTextChanged.connect(self._on_unit)

    def _fmt(self, v) -> str:
        if self.unit.currentText() == "LSB":
            return f"{v / self._lsb:,.1f} LSB"
        return fmt_si(v, "V", 4)

    def _on_unit(self, unit: str) -> None:
        self.displayUnitChanged.emit(unit)
        self._redraw()

    def _redraw(self) -> None:
        lsb_mode = self.unit.currentText() == "LSB"
        self.plot.set_axis_labels("Time", "s", "Amplitude", "LSB" if lsb_mode else "V")
        if lsb_mode:
            # pyqtgraph would otherwise SI-prefix "LSB" (kLSB); show raw codes
            self.plot.getAxis("left").enableAutoSIPrefix(False)
        else:
            self.plot.getAxis("left").enableAutoSIPrefix(True)
        if self._volts is None:
            return
        y = self._volts / self._lsb if lsb_mode else self._volts
        t = np.arange(len(y)) / self._rate
        self.curve.setData(t, y)
        self.stats.set_values(self._stats)

    # ---------------------------------------------------------------- public API
    @Slot(object, float, float, dict)
    def show_capture(
        self, volts: np.ndarray, rate_sps: float, lsb_volts: float, stats: dict
    ) -> None:
        """Show a capture.

        volts: samples in volts; stats keys (volts / Hz):
        average, std_dev, max, min, p2p, fundamental_hz.
        """
        self._volts = np.asarray(volts, dtype=float)
        self._rate, self._lsb = rate_sps, lsb_volts
        self._stats = dict(stats, n=len(self._volts), rate=rate_sps)
        self._redraw()
        self.plot.enableAutoRange()

    def set_busy(self, busy: bool) -> None:
        self.bar.set_busy(busy)

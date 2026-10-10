"""FFT tab — magnitude spectrum (dBFS vs Hz) with dynamic-performance metrics."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QVBoxLayout, QWidget

from ..theme import ThemeManager
from ..widgets import CaptureBar, StatsPanel, ThemedPlot, fmt_si, muted, trace_pen

WINDOWS = ["Blackman-Harris 4", "Hann", "Flat top", "Rectangular"]


def _db(v) -> str:
    return f"{v:.2f} dB"


class FftTab(QWidget):
    captureRequested = Signal(float, int)
    windowChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window = QComboBox()
        self.window.addItems(WINDOWS)
        self.window.setToolTip("Window function applied before the FFT.")
        self.bar = CaptureBar(leading=[muted("Window"), self.window])
        self.bar.layout().insertSpacing(2, 6)
        self.plot = ThemedPlot("Frequency", "Hz", "Magnitude", "dBFS")
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.curve = self.plot.plot([], [], pen=trace_pen(1.2))
        self.marker = pg.ScatterPlotItem(size=9, symbol="o")
        self.plot.addItem(self.marker)
        self.marker_label = pg.TextItem(anchor=(0, 1))
        self.plot.addItem(self.marker_label)
        ThemeManager.instance().changed.connect(self._recolor)
        self._recolor()

        self.stats = StatsPanel(
            "Dynamic Performance",
            [
                ("fundamental_hz", "Fundamental", lambda v: fmt_si(v, "Hz", 3)),
                ("fundamental_dbfs", "Fund. amplitude", lambda v: f"{v:.2f} dBFS"),
                ("thd_db", "THD", _db),
                ("snr_db", "SNR", _db),
                ("sinad_db", "SINAD", _db),
                ("sfdr_db", "SFDR", lambda v: f"{v:.2f} dBc"),
                ("enob", "ENOB", lambda v: f"{v:.2f} bits"),
                ("noise_floor_dbfs", "Noise floor", lambda v: f"{v:.1f} dBFS"),
            ],
        )

        box = QGroupBox("Spectrum")
        bl = QVBoxLayout(box)
        bl.addWidget(self.bar)
        bl.addWidget(self.plot, 1)
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addWidget(box, 1)
        root.addWidget(self.stats)

        self.bar.captureClicked.connect(self.captureRequested)
        self.window.currentTextChanged.connect(self.windowChanged)

    def _recolor(self) -> None:
        c = ThemeManager.instance().colors
        self.curve.setPen(trace_pen(1.2))
        self.marker.setBrush(pg.mkBrush(c.trace2))
        self.marker.setPen(pg.mkPen(c.trace2))
        self.marker_label.setColor(c.trace2)

    @Slot(object, object, dict)
    def show_spectrum(self, freqs_hz: np.ndarray, mag_dbfs: np.ndarray, metrics: dict) -> None:
        self.curve.setData(np.asarray(freqs_hz), np.asarray(mag_dbfs))
        self.stats.set_values(metrics)
        f0 = metrics.get("fundamental_hz")
        a0 = metrics.get("fundamental_dbfs")
        if f0 is not None and a0 is not None:
            self.marker.setData([f0], [a0])
            self.marker_label.setText(f" {f0:.2f} Hz, {a0:.1f} dBFS")
            self.marker_label.setPos(f0, a0)
        self.plot.setYRange(float(np.min(mag_dbfs)) - 5, 5)
        self.plot.setXRange(0, float(freqs_hz[-1]), padding=0.01)

    def set_busy(self, busy: bool) -> None:
        self.bar.set_busy(busy)

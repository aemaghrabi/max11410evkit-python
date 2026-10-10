"""FFT tab — magnitude spectrum (dBFS vs Hz) of one channel with dynamic-performance metrics."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QVBoxLayout, QWidget

from ..channels import ChannelStore, channel_label
from ..theme import ThemeManager
from ..widgets import (
    CaptureBar,
    ChannelChips,
    StatsPanel,
    ThemedPlot,
    channel_pen,
    fmt_si,
    muted,
)
from .scope import rate_summary

WINDOWS = ["Blackman-Harris 4", "Hann", "Flat top", "Rectangular"]


def _db(v) -> str:
    return f"{v:.2f} dB"


class FftTab(QWidget):
    captureRequested = Signal(int, int)  # channel, n_samples
    channelSelected = Signal(int)
    windowChanged = Signal(str)

    def __init__(self, store: ChannelStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store or ChannelStore(self)
        self._ch = 0
        self.chips = ChannelChips(self.store, multi=False)
        self.window = QComboBox()
        self.window.addItems(WINDOWS)
        self.window.setToolTip("Window function applied before the FFT.")
        self.bar = CaptureBar(leading=[muted("Window"), self.window])
        self.plot = ThemedPlot("Frequency", "Hz", "Magnitude", "dBFS")
        self.plot.getAxis("left").enableAutoSIPrefix(False)
        self.curve = self.plot.plot([], [], pen=channel_pen(0, 1.2))
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
        top = QHBoxLayout()
        top.addWidget(muted("Channel"))
        top.addWidget(self.chips)
        top.addStretch(1)
        bl.addLayout(top)
        bl.addWidget(self.bar)
        bl.addWidget(self.plot, 1)
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addWidget(box, 1)
        root.addWidget(self.stats)

        self.bar.captureClicked.connect(self._capture)
        self.chips.selectionChanged.connect(self._on_selection)
        self.store.changed.connect(lambda _ch: self._update_rate())
        self.window.currentTextChanged.connect(self.windowChanged)
        self._update_rate()

    def _capture(self, n: int) -> None:
        sel = self.chips.selected()
        if sel:
            self.captureRequested.emit(sel[0], n)

    def _on_selection(self, chans: list) -> None:
        self._update_rate()
        if chans:
            self.channelSelected.emit(chans[0])

    def _update_rate(self) -> None:
        self.bar.set_rate_text(rate_summary(self.store, self.chips.selected()))

    def _recolor(self) -> None:
        c = ThemeManager.instance().colors
        self.curve.setPen(channel_pen(self._ch, 1.2))
        self.marker.setBrush(pg.mkBrush(c.trace2))
        self.marker.setPen(pg.mkPen(c.trace2))
        self.marker_label.setColor(c.trace2)

    @Slot(int, object, object, dict)
    def show_spectrum(self, ch: int, freqs_hz: np.ndarray, mag_dbfs: np.ndarray, metrics: dict):
        self._ch = ch
        self._recolor()
        self.stats.setTitle(f"Dynamic Performance — {channel_label(ch, self.store.get(ch))}")
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

    def set_selected_channel(self, ch: int) -> None:
        self.chips.set_selected([ch])
        self._update_rate()

    def set_busy(self, busy: bool) -> None:
        self.bar.set_busy(busy)

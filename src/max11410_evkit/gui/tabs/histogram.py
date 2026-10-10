"""Histogram tab — code histogram of a capture with noise statistics."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QGroupBox, QHBoxLayout, QVBoxLayout, QWidget

from ..theme import ThemeManager
from ..widgets import CaptureBar, IntAxis, StatsPanel, ThemedPlot, fmt_si


def _bits(v) -> str:
    return f"{v:.2f} bits"


class HistogramTab(QWidget):
    captureRequested = Signal(float, int)
    histogramEnabledChanged = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.disable = QCheckBox("Disable Histogram")
        self.disable.setToolTip("Skip histogram plotting (statistics are still computed).")
        self.bar = CaptureBar(leading=[self.disable])
        self.bar.layout().insertSpacing(1, 12)
        self.plot = ThemedPlot(
            "Output code", "LSB", "Occurrences", "", axisItems={"bottom": IntAxis("bottom")}
        )
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.bars: pg.BarGraphItem | None = None

        self.stats = StatsPanel(
            "Noise Analysis",
            [
                ("average", "Average", lambda v: fmt_si(v, "V", 6)),
                ("std_dev", "Std. dev. (RMS noise)", lambda v: fmt_si(v, "V", 3)),
                ("max", "Maximum", lambda v: fmt_si(v, "V", 6)),
                ("min", "Minimum", lambda v: fmt_si(v, "V", 6)),
                ("p2p_noise", "Peak-to-peak noise", lambda v: fmt_si(v, "V", 3)),
                ("effective_resolution", "Effective resolution", _bits),
                ("noise_free_resolution", "Noise-free resolution", _bits),
                ("n", "Samples", lambda v: f"{v:d}"),
            ],
        )

        box = QGroupBox("Histogram")
        bl = QVBoxLayout(box)
        bl.addWidget(self.bar)
        bl.addWidget(self.plot, 1)
        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addWidget(box, 1)
        root.addWidget(self.stats)

        self.bar.captureClicked.connect(self.captureRequested)
        self.disable.toggled.connect(
            lambda on: (self.plot.setEnabled(not on), self.histogramEnabledChanged.emit(not on))
        )
        ThemeManager.instance().changed.connect(self._recolor)
        self._codes: np.ndarray | None = None

    def _recolor(self) -> None:
        if self.bars is not None:
            c = QColor(ThemeManager.instance().colors.trace)
            self.bars.setOpts(brush=c, pen=c.darker(140))

    @Slot(object, dict)
    def show_capture(self, codes: np.ndarray, stats: dict) -> None:
        """codes: integer ADC codes; stats keys listed in DESIGN.md (volts / bits)."""
        self._codes = np.asarray(codes)
        self.stats.set_values(dict(stats, n=len(self._codes)))
        if self.bars is not None:
            self.plot.removeItem(self.bars)
            self.bars = None
        if self.disable.isChecked():
            return
        lo, hi = int(self._codes.min()), int(self._codes.max())
        edges = np.arange(lo, hi + 2)
        counts, _ = np.histogram(self._codes, bins=edges)
        c = QColor(ThemeManager.instance().colors.trace)
        self.bars = pg.BarGraphItem(
            x=edges[:-1], height=counts, width=0.85, brush=c, pen=c.darker(140)
        )
        self.plot.addItem(self.bars)
        self.plot.enableAutoRange()

    def set_busy(self, busy: bool) -> None:
        self.bar.set_busy(busy)

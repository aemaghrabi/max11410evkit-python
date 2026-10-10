"""Histogram tab — code histogram of one channel's capture with noise statistics."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QGroupBox, QHBoxLayout, QVBoxLayout, QWidget

from ..channels import ChannelStore, channel_label
from ..theme import ThemeManager, channel_color
from ..widgets import CaptureBar, ChannelChips, IntAxis, StatsPanel, ThemedPlot, fmt_si, muted
from .scope import rate_summary


def _bits(v) -> str:
    return f"{v:.2f} bits"


class HistogramTab(QWidget):
    captureRequested = Signal(int, int)  # channel, n_samples
    channelSelected = Signal(int)
    histogramEnabledChanged = Signal(bool)

    def __init__(self, store: ChannelStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store or ChannelStore(self)
        self._ch: int | None = None
        self.chips = ChannelChips(self.store, multi=False)
        self.disable = QCheckBox("Disable Histogram")
        self.disable.setToolTip("Skip histogram plotting (statistics are still computed).")
        self.bar = CaptureBar(leading=[self.disable])
        self.plot = ThemedPlot(
            "Output code", "LSB", "Occurrences", "", axisItems={"bottom": IntAxis("bottom")}
        )
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
        self.stats_title = self.stats.title()

        box = QGroupBox("Histogram")
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
        self.disable.toggled.connect(
            lambda on: (self.plot.setEnabled(not on), self.histogramEnabledChanged.emit(not on))
        )
        ThemeManager.instance().changed.connect(self._recolor)
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
        if self.bars is not None and self._ch is not None:
            c = QColor(channel_color(self._ch))
            self.bars.setOpts(brush=c, pen=c.darker(140))

    @Slot(int, object, dict)
    def show_capture(self, ch: int, codes: np.ndarray, stats: dict) -> None:
        """codes: integer ADC codes of channel ``ch``; stats keys listed in DESIGN.md."""
        self._ch = ch
        codes = np.asarray(codes)
        self.stats.setTitle(f"Noise Analysis — {channel_label(ch, self.store.get(ch))}")
        self.stats.set_values(dict(stats, n=len(codes)))
        if self.bars is not None:
            self.plot.removeItem(self.bars)
            self.bars = None
        if self.disable.isChecked():
            return
        lo, hi = int(codes.min()), int(codes.max())
        edges = np.arange(lo, hi + 2)
        counts, _ = np.histogram(codes, bins=edges)
        c = QColor(channel_color(ch))
        self.bars = pg.BarGraphItem(
            x=edges[:-1], height=counts, width=0.85, brush=c, pen=c.darker(140)
        )
        self.plot.addItem(self.bars)
        self.plot.enableAutoRange()

    def set_selected_channel(self, ch: int) -> None:
        self.chips.set_selected([ch])
        self._update_rate()

    def set_busy(self, busy: bool) -> None:
        self.bar.set_busy(busy)

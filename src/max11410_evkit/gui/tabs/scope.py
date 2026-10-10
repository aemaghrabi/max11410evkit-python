"""Scope tab — time-domain capture of one or more channels, with statistics."""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import Signal, Slot
from PySide6.QtWidgets import QComboBox, QGroupBox, QHBoxLayout, QVBoxLayout, QWidget

from ..channels import ChannelStore, channel_label
from ..theme import ThemeManager
from ..widgets import CaptureBar, ChannelChips, StatsPanel, ThemedPlot, channel_pen, fmt_si, muted


def rate_summary(store: ChannelStore, chans: list[int]) -> str:
    rates = {store.get(ch)["rate"] for ch in chans}
    if not rates:
        return ""
    if len(rates) == 1:
        return f"{rates.pop():g} sps (from channel config)"
    return "mixed rates (from channel configs)"


class ScopeTab(QWidget):
    captureRequested = Signal(list, int)  # channels, n_samples (per channel)
    channelsSelected = Signal(list)
    displayUnitChanged = Signal(str)  # "Voltage" | "LSB"

    def __init__(self, store: ChannelStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store or ChannelStore(self)
        self._data: dict[int, np.ndarray] = {}
        self._rates: dict[int, float] = {}
        self._lsb: dict[int, float] = {}
        self._stats: dict[int, dict] = {}
        self._curves: dict = {}

        self.chips = ChannelChips(self.store, multi=True)
        self.unit = QComboBox()
        self.unit.addItems(["Voltage", "LSB"])
        self.unit.setToolTip("Y-axis and statistics units.")
        self.bar = CaptureBar(leading=[muted("Display unit"), self.unit])
        self.plot = ThemedPlot("Time", "s", "Amplitude", "V")
        self.legend = self.plot.addLegend(offset=(-10, 10))

        self.stats_channel = QComboBox()
        self.stats_channel.setToolTip("Channel whose statistics are shown.")
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
            header=self.stats_channel,
        )

        plot_box = QGroupBox("Time Domain")
        pl = QVBoxLayout(plot_box)
        top = QHBoxLayout()
        top.addWidget(muted("Channels"))
        top.addWidget(self.chips)
        top.addStretch(1)
        pl.addLayout(top)
        pl.addWidget(self.bar)
        pl.addWidget(self.plot, 1)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addWidget(plot_box, 1)
        root.addWidget(self.stats)

        self.bar.captureClicked.connect(
            lambda n: self.captureRequested.emit(self.chips.selected(), n)
        )
        self.chips.selectionChanged.connect(self._on_selection)
        self.unit.currentTextChanged.connect(self._on_unit)
        self.stats_channel.currentIndexChanged.connect(lambda _i: self._show_stats())
        self.store.changed.connect(lambda _ch: self._update_rate())
        ThemeManager.instance().changed.connect(self._redraw)
        self._update_rate()

    # ---------------------------------------------------------------- internals
    def _on_selection(self, chans: list) -> None:
        self._update_rate()
        self.channelsSelected.emit(chans)

    def _update_rate(self) -> None:
        self.bar.set_rate_text(rate_summary(self.store, self.chips.selected()))

    def _lsb_of(self) -> float:
        ch = self.stats_channel.currentData()
        return self._lsb.get(ch, 1.0)

    def _fmt(self, v) -> str:
        if self.unit.currentText() == "LSB":
            return f"{v / self._lsb_of():,.1f} LSB"
        return fmt_si(v, "V", 4)

    def _on_unit(self, unit: str) -> None:
        self.displayUnitChanged.emit(unit)
        self._redraw()

    def _redraw(self) -> None:
        lsb_mode = self.unit.currentText() == "LSB"
        self.plot.set_axis_labels("Time", "s", "Amplitude", "LSB" if lsb_mode else "V")
        self.plot.getAxis("left").enableAutoSIPrefix(not lsb_mode)
        self.plot.clear()
        self.legend.clear()
        self.legend.setLabelTextColor(ThemeManager.instance().colors.plot_fg)
        for ch, v in self._data.items():
            y = v / self._lsb[ch] if lsb_mode else v
            t = np.arange(len(y)) / self._rates[ch]
            self.plot.plot(t, y, pen=channel_pen(ch), name=channel_label(ch, self.store.get(ch)))
        self._show_stats()

    def _show_stats(self) -> None:
        ch = self.stats_channel.currentData()
        if ch in self._stats:
            self.stats.set_values(self._stats[ch])
        else:
            self.stats.clear()

    # ---------------------------------------------------------------- public API
    @Slot(dict, dict, dict, dict)
    def show_capture(self, data: dict, rates: dict, lsb_volts: dict, stats: dict) -> None:
        """All args keyed by channel.

        data[ch]: samples in volts; rates[ch]: sps; lsb_volts[ch]: V/LSB;
        stats[ch]: {average, std_dev, max, min, p2p, fundamental_hz} (volts / Hz).
        """
        self._data = {ch: np.asarray(v, dtype=float) for ch, v in data.items()}
        self._rates, self._lsb = dict(rates), dict(lsb_volts)
        self._stats = {
            ch: dict(s, n=len(self._data[ch]), rate=self._rates[ch]) for ch, s in stats.items()
        }
        prev = self.stats_channel.currentData()
        self.stats_channel.blockSignals(True)
        self.stats_channel.clear()
        for ch in self._data:
            self.stats_channel.addItem(channel_label(ch, self.store.get(ch)), ch)
        i = self.stats_channel.findData(prev)
        self.stats_channel.setCurrentIndex(max(i, 0))
        self.stats_channel.blockSignals(False)
        self._redraw()
        self.plot.enableAutoRange()

    def set_selected_channels(self, chans: list[int]) -> None:
        self.chips.set_selected(chans)
        self._update_rate()

    def set_busy(self, busy: bool) -> None:
        self.bar.set_busy(busy)

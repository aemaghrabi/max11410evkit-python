"""DMM tab — large readout of the primary channel, mini readouts for other selected
channels, single / continuous measurement and a multi-channel trend strip."""

from __future__ import annotations

from collections import deque

import numpy as np
from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..channels import ChannelStore, channel_label, filter_text, input_text
from ..theme import ThemeManager, channel_color, mono_font
from ..widgets import (
    ChannelChips,
    StatsPanel,
    ThemedPlot,
    channel_pen,
    compact_form,
    fmt_si,
    muted,
    primary_button,
)

TREND_LEN = 300


def split_units(volts: float) -> tuple[str, str]:
    a = abs(volts)
    if a >= 1 or a == 0:
        return f"{volts:+.6f}", "V"
    if a >= 1e-3:
        return f"{volts * 1e3:+.4f}", "mV"
    return f"{volts * 1e6:+.2f}", "µV"


class _MiniReadout(QFrame):
    """Compact tile: colour bar + channel label + value."""

    def __init__(self, ch: int, label: str, on_click=None) -> None:
        super().__init__()
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to make this the primary channel")
        self.setObjectName("readoutFrame")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 4, 10, 4)
        lay.setSpacing(8)
        self.bar = QFrame()
        self.bar.setFixedWidth(4)
        self.bar.setStyleSheet(f"background: {channel_color(ch)}; border-radius: 2px;")
        name = muted(label)
        self.value = QLabel("—")
        self.value.setFont(mono_font(12))
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self.bar)
        lay.addWidget(name, 1)
        lay.addWidget(self.value)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if self._on_click:
            self._on_click()
        super().mousePressEvent(ev)

    def set_value(self, volts: float) -> None:
        v, u = split_units(volts)
        self.value.setText(f"{v} {u}")


class DmmTab(QWidget):
    measureRequested = Signal(list)  # channels to read once
    continuousToggled = Signal(bool)  # controller repeatedly reads the selected channels
    channelsSelected = Signal(list)
    averagingChanged = Signal(int)

    def __init__(self, store: ChannelStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store or ChannelStore(self)
        self._hist: dict[int, deque[float]] = {}
        self._all: dict[int, list[float]] = {}
        self._count: dict[int, int] = {}
        self._tiles: dict[int, _MiniReadout] = {}
        self._curves: dict[int, object] = {}

        # ---- big readout (primary = first selected channel)
        frame = QFrame()
        frame.setObjectName("readoutFrame")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(24, 12, 24, 12)
        top = QHBoxLayout()
        self.mode_label = muted("DC VOLTS")
        self.primary_label = QLabel("—")
        f = self.primary_label.font()
        f.setBold(True)
        self.primary_label.setFont(f)
        self.input_label = muted("")
        top.addWidget(self.mode_label)
        top.addSpacing(12)
        top.addWidget(self.primary_label)
        top.addStretch(1)
        top.addWidget(self.input_label)
        fl.addLayout(top)
        val_row = QHBoxLayout()
        val_row.addStretch(1)
        self.value = QLabel("—.——————")
        self.value.setProperty("role", "readout")
        vf = mono_font(56)
        vf.setBold(True)
        self.value.setFont(vf)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.unit = QLabel("V")
        self.unit.setProperty("role", "unit")
        self.unit.setFont(mono_font(26))
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

        # ---- other selected channels
        self.tiles_box = QWidget()
        self.tiles_grid = QGridLayout(self.tiles_box)
        self.tiles_grid.setContentsMargins(0, 0, 0, 0)
        self.tiles_grid.setHorizontalSpacing(8)
        self.tiles_grid.setVerticalSpacing(6)

        # ---- trend
        self.trend = ThemedPlot("Reading #", "", "Voltage", "V")
        self.trend.setMinimumHeight(150)
        self.trend.getAxis("bottom").enableAutoSIPrefix(False)
        self.trend_legend = self.trend.addLegend(offset=(-10, 10))

        self.chips = ChannelChips(self.store, multi=True)
        chip_row = QHBoxLayout()
        chip_row.addWidget(muted("Channels"))
        chip_row.addWidget(self.chips)
        chip_row.addStretch(1)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addLayout(chip_row)
        left.addWidget(frame)
        left.addWidget(self.tiles_box)
        trend_box = QGroupBox("Trend")
        tl = QVBoxLayout(trend_box)
        trow = QHBoxLayout()
        trow.addWidget(muted("Show"))
        self.trend_mode = QComboBox()
        self.trend_mode.addItems(["Deviation from mean (all selected)", "Absolute (primary only)"])
        self.trend_mode.setToolTip(
            "Channels with very different ranges are easiest to compare as deviation from mean."
        )
        self.trend_mode.currentIndexChanged.connect(lambda _i: self._redraw_trend())
        trow.addWidget(self.trend_mode)
        trow.addStretch(1)
        tl.addLayout(trow)
        tl.addWidget(self.trend)
        left.addWidget(trend_box, 1)

        # ---- controls
        ctl = QGroupBox("Measurement")
        cf = compact_form()
        self.avg = QComboBox()
        for n in (1, 4, 16, 64):
            self.avg.addItem(f"{n}" if n > 1 else "Off", n)
        self.avg.setToolTip("Average N conversions per displayed reading.")
        self.measure = QPushButton("Capture")
        self.measure.setToolTip("Take one reading of each selected channel.")
        self.run = primary_button(
            "Run continuous", "Start / stop continuous readings.", checkable=True
        )
        self.reset = QPushButton("Reset statistics")
        cf.addRow("Averaging", self.avg)
        btns = QHBoxLayout()
        btns.addWidget(self.measure)
        btns.addWidget(self.run)
        cf.addRow(btns)
        cf.addRow(self.reset)
        cf.addRow(muted("Rate, range and input come from\neach channel's configuration."))
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
        self.stats.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(10)
        right.addWidget(ctl)
        right.addWidget(self.stats, 1)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)
        root.addLayout(left, 1)
        rw = QWidget()
        rw.setLayout(right)
        rw.setFixedWidth(290)
        root.addWidget(rw)

        self.measure.clicked.connect(lambda: self.measureRequested.emit(self.chips.selected()))
        self.run.toggled.connect(self._on_run)
        self.avg.currentIndexChanged.connect(
            lambda _i: self.averagingChanged.emit(self.avg.currentData())
        )
        self.reset.clicked.connect(self.reset_statistics)
        self.chips.selectionChanged.connect(self._on_selection)
        self.store.changed.connect(lambda _ch: self._on_selection(self.chips.selected(), False))
        ThemeManager.instance().changed.connect(lambda: self._on_selection(self.selected(), False))
        self._on_selection(self.chips.selected(), emit=False)

    # ---------------------------------------------------------------- internals
    def selected(self) -> list[int]:
        return self.chips.selected()

    @property
    def primary(self) -> int | None:
        sel = self.chips.selected()
        return sel[0] if sel else None

    def _on_selection(self, chans: list, emit: bool = True) -> None:
        p = self.primary
        if p is not None:
            cfg = self.store.get(p)
            self.primary_label.setText(channel_label(p, cfg))
            self.primary_label.setStyleSheet(f"color: {channel_color(p)};")
            self.input_label.setText(input_text(cfg))
            self.rate_label.setText(filter_text(cfg))
        # rebuild tiles for the non-primary channels
        while self.tiles_grid.count():
            it = self.tiles_grid.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        self._tiles.clear()
        for i, ch in enumerate(chans[1:]):
            tile = _MiniReadout(
                ch, channel_label(ch, self.store.get(ch)), lambda c=ch: self._make_primary(c)
            )
            if self._all.get(ch):
                tile.set_value(self._all[ch][-1])
            self.tiles_grid.addWidget(tile, i // 3, i % 3)
            self._tiles[ch] = tile
        self.tiles_box.setVisible(len(chans) > 1)
        self._redraw_trend()
        self._show_primary()
        if emit:
            self.channelsSelected.emit(list(chans))

    def _on_run(self, on: bool) -> None:
        self.run.setText("Stop" if on else "Run continuous")
        self.measure.setEnabled(not on)
        self.continuousToggled.emit(on)

    def _deviation_mode(self) -> bool:
        return self.trend_mode.currentIndex() == 0

    def _trend_xy(self, ch: int) -> tuple[np.ndarray, np.ndarray]:
        if ch not in self._hist:
            return np.array([]), np.array([])
        y = np.fromiter(self._hist[ch], float)
        x = np.arange(self._count[ch] - len(y), self._count[ch])
        if self._deviation_mode():
            y = y - np.mean(self._all[ch])
        return x, y

    def _redraw_trend(self) -> None:
        dev = self._deviation_mode()
        self.trend.set_axis_labels("Reading #", "", "Δ from mean" if dev else "Voltage", "V")
        self.trend.clear()
        self.trend_legend.clear()
        self.trend_legend.setLabelTextColor(ThemeManager.instance().colors.plot_fg)
        self._curves.clear()
        chans = self.chips.selected() if dev else self.chips.selected()[:1]
        for ch in chans:
            x, y = self._trend_xy(ch)
            self._curves[ch] = self.trend.plot(
                x, y, pen=channel_pen(ch, 1.6), name=channel_label(ch, self.store.get(ch))
            )

    def _make_primary(self, ch: int) -> None:
        sel = self.chips.selected()
        if ch in sel:
            sel.remove(ch)
            self.chips.set_selected([ch, *sel])
            self._on_selection(self.chips.selected())

    def _show_primary(self) -> None:
        p = self.primary
        vals = self._all.get(p) if p is not None else None
        if not vals:
            self.value.setText("—.——————")
            self.stats.clear()
            return
        arr = np.asarray(vals)
        self.stats.setTitle(f"Statistics — Ch {p}")
        self.stats.set_values(
            {
                "mean": arr.mean(),
                "std": arr.std(ddof=1) if len(arr) > 1 else 0.0,
                "max": arr.max(),
                "min": arr.min(),
                "n": len(arr),
            }
        )

    # ---------------------------------------------------------------- public API
    @Slot(int, float, int)
    def show_reading(self, ch: int, volts: float, code: int = 0) -> None:
        self._hist.setdefault(ch, deque(maxlen=TREND_LEN)).append(volts)
        self._all.setdefault(ch, []).append(volts)
        self._count[ch] = self._count.get(ch, 0) + 1
        if ch == self.primary:
            v, u = split_units(volts)
            self.value.setText(v)
            self.unit.setText(u)
            self.code_label.setText(f"code 0x{code & 0xFFFFFF:06X}")
            self._show_primary()
        if ch in self._tiles:
            self._tiles[ch].set_value(volts)
        if ch in self._curves:
            self._curves[ch].setData(*self._trend_xy(ch))

    def set_selected_channels(self, chans: list[int]) -> None:
        self.chips.set_selected(chans)
        self._on_selection(self.chips.selected(), emit=False)

    def reset_statistics(self) -> None:
        self._hist.clear()
        self._all.clear()
        self._count.clear()
        self._on_selection(self.chips.selected(), emit=False)

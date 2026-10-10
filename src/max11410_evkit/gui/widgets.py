"""Small reusable widgets shared by the tabs (no hardware knowledge in here)."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence

import pyqtgraph as pg
from PySide6.QtCore import QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QRegularExpressionValidator
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyledItemDelegate,
    QWidget,
)

from .placeholder_regmap import CAPTURE_RATES, SAMPLE_COUNTS
from .theme import ThemeManager, mono_font

# ------------------------------------------------------------------ formatting
_SI = [(1e0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n")]


def fmt_si(value: float | None, unit: str = "V", digits: int = 4) -> str:
    """Engineering formatting: 0.0012345 -> '1.2345 mV'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if value == 0:
        return f"{0:.{digits}f} {unit}"
    a = abs(value)
    for scale, prefix in _SI:
        if a >= scale:
            return f"{value / scale:.{digits}f} {prefix}{unit}"
    return f"{value / 1e-9:.{digits}f} n{unit}"


def fmt_hex(value: int | None, digits: int = 6) -> str:
    return "—" if value is None else f"0x{value:0{digits}X}"


# ------------------------------------------------------------------ buttons / labels
def primary_button(text: str, tooltip: str = "", checkable: bool = False) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("primary", True)
    b.setCheckable(checkable)
    b.setCursor(Qt.PointingHandCursor)
    if tooltip:
        b.setToolTip(tooltip)
    return b


def muted(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setProperty("role", "muted")
    return lbl


def hex_output(width_chars: int = 10, tooltip: str = "") -> QLineEdit:
    e = QLineEdit()
    e.setReadOnly(True)
    e.setProperty("role", "hexout")
    e.setFont(mono_font())
    e.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    e.setMinimumWidth(e.fontMetrics().horizontalAdvance("0" * width_chars) + 16)
    if tooltip:
        e.setToolTip(tooltip)
    return e


def compact_form() -> QFormLayout:
    f = QFormLayout()
    f.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    f.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
    f.setHorizontalSpacing(10)
    f.setVerticalSpacing(6)
    return f


class Led(QWidget):
    """Round status indicator: state in {'off', 'ok', 'warn', 'bad'}."""

    def __init__(
        self, state: str = "off", diameter: int = 12, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._state = state
        self._d = diameter
        self.setFixedSize(diameter + 2, diameter + 2)
        ThemeManager.instance().changed.connect(self.update)

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._d + 2, self._d + 2)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        c = ThemeManager.instance().colors
        color = {"ok": c.ok, "warn": c.warn, "bad": c.bad}.get(self._state, c.border)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QColor(color).darker(130))
        p.setBrush(QColor(color))
        p.drawEllipse(1, 1, self._d, self._d)


# ------------------------------------------------------------------ stats panel
class StatsPanel(QGroupBox):
    """Right-hand statistics panel: label / value rows with fixed-width numbers.

    ``rows`` is a sequence of (key, label, formatter) where formatter maps the raw
    value from the stats dict to display text.
    """

    def __init__(
        self,
        title: str,
        rows: Sequence[tuple[str, str, Callable[[object], str]]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self._fmt: dict[str, Callable[[object], str]] = {}
        self._labels: dict[str, QLabel] = {}
        grid = QGridLayout(self)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        for r, (key, label, fmt) in enumerate(rows):
            name = muted(label)
            val = QLabel("—")
            val.setProperty("role", "value")
            val.setFont(mono_font())
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            grid.addWidget(name, r, 0)
            grid.addWidget(val, r, 1)
            self._fmt[key] = fmt
            self._labels[key] = val
        grid.setRowStretch(len(rows), 1)
        grid.setColumnStretch(1, 1)
        self.setMinimumWidth(250)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)

    def set_values(self, stats: dict) -> None:
        for key, lbl in self._labels.items():
            lbl.setText(self._fmt[key](stats.get(key)) if key in stats else "—")

    def clear(self) -> None:
        for lbl in self._labels.values():
            lbl.setText("—")


# ------------------------------------------------------------------ capture bar
class CaptureBar(QWidget):
    """Sample-rate + sample-count + Capture button, used by Scope/Histogram/FFT."""

    captureClicked = Signal(float, int)

    def __init__(self, leading: Iterable[QWidget] = (), parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        for w in leading:
            lay.addWidget(w)
        self.rate = QComboBox()
        self.rate.setToolTip("Continuous-mode output data rate used for the capture (SINC4).")
        for r in CAPTURE_RATES:
            self.rate.addItem(f"{r:g} sps", r)
        self.rate.setCurrentIndex(CAPTURE_RATES.index(59.8))
        self.samples = QComboBox()
        self.samples.setToolTip(
            "Number of samples to capture into the board SRAM (max 8192 with 32 KB)."
        )
        for n in SAMPLE_COUNTS:
            self.samples.addItem(str(n), n)
        self.samples.setCurrentIndex(SAMPLE_COUNTS.index(1024))
        self.capture = primary_button("Capture", "Acquire a block of samples from the EV kit.")
        lay.addWidget(muted("Sample rate"))
        lay.addWidget(self.rate)
        lay.addSpacing(6)
        lay.addWidget(muted("Samples"))
        lay.addWidget(self.samples)
        lay.addStretch(1)
        lay.addWidget(self.capture)
        self.capture.clicked.connect(
            lambda: self.captureClicked.emit(
                float(self.rate.currentData()), int(self.samples.currentData())
            )
        )

    def set_busy(self, busy: bool) -> None:
        self.capture.setEnabled(not busy)
        self.capture.setText("Capturing…" if busy else "Capture")


# ------------------------------------------------------------------ themed plot
class IntAxis(pg.AxisItem):
    """Axis that prints plain integers (no SI prefix / scientific notation), e.g. ADC codes."""

    def __init__(self, orientation: str = "bottom", **kw) -> None:
        super().__init__(orientation, **kw)
        self.enableAutoSIPrefix(False)

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        return [f"{round(v * scale):d}" for v in values]


class ThemedPlot(pg.PlotWidget):
    """pyqtgraph PlotWidget that follows ThemeManager colours."""

    def __init__(
        self,
        x_label: str = "",
        x_units: str = "",
        y_label: str = "",
        y_units: str = "",
        parent: QWidget | None = None,
        axisItems: dict | None = None,  # noqa: N803
    ) -> None:
        super().__init__(parent=parent, axisItems=axisItems)
        self.setMinimumSize(320, 240)
        self.showGrid(x=True, y=True)
        self.plotItem.setMenuEnabled(True)
        self.plotItem.getViewBox().setDefaultPadding(0.02)
        self._labels = (x_label, x_units, y_label, y_units)
        self.set_axis_labels(*self._labels)
        ThemeManager.instance().changed.connect(self.apply_theme)
        self.apply_theme()

    def set_axis_labels(self, x_label: str, x_units: str, y_label: str, y_units: str) -> None:
        self._labels = (x_label, x_units, y_label, y_units)
        self.setLabel("bottom", x_label, units=x_units or None)
        self.setLabel("left", y_label, units=y_units or None)

    def apply_theme(self) -> None:
        c = ThemeManager.instance().colors
        self.setBackground(c.plot_bg)
        for ax in ("left", "bottom", "right", "top"):
            axis = self.plotItem.getAxis(ax)
            axis.setPen(pg.mkPen(c.plot_fg))
            axis.setTextPen(pg.mkPen(c.plot_fg))
        self.showGrid(x=True, y=True, alpha=c.grid_alpha)
        self.set_axis_labels(*self._labels)


def trace_pen(width: float = 1.4, which: int = 1) -> object:
    c = ThemeManager.instance().colors
    return pg.mkPen(c.trace if which == 1 else c.trace2, width=width)


# ------------------------------------------------------------------ hex delegate
class HexDelegate(QStyledItemDelegate):
    """Editor for 'Value (Hex)' cells: accepts up to ``digits`` hex characters."""

    def __init__(self, digits_for_row: Callable[[int], int], parent=None) -> None:
        super().__init__(parent)
        self._digits = digits_for_row

    def createEditor(self, parent, option, index):  # noqa: N802
        e = QLineEdit(parent)
        e.setFont(mono_font())
        n = self._digits(index.row())
        e.setValidator(
            QRegularExpressionValidator(QRegularExpression(f"(0[xX])?[0-9A-Fa-f]{{1,{n}}}"), e)
        )
        e.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return e

    def setEditorData(self, editor, index):  # noqa: N802
        editor.setText(str(index.data()).replace("0x", ""))
        editor.selectAll()


# ------------------------------------------------------------------ bit strip
class BitStrip(QWidget):
    """Painted MSB..LSB bit boxes with bit-field brackets underneath (Registers tab)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._width = 8
        self._fields: list = []
        self._value: int | None = None
        self.setMinimumHeight(66)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        ThemeManager.instance().changed.connect(self.update)

    def set_register(self, width: int, fields, value: int | None) -> None:
        self._width, self._fields, self._value = width, list(fields), value
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(400, 66)

    def paintEvent(self, _ev) -> None:  # noqa: N802
        from PySide6.QtCore import QRectF

        c = ThemeManager.instance().colors
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        n = self._width
        box = min(36.0, (self.width() - 2) / n)
        x0 = 1.0
        small = self.font()
        small.setPointSizeF(max(small.pointSizeF() * 0.75, 6.5))
        mono = mono_font()
        owner: dict[int, int] = {}
        for i, f in enumerate(self._fields):
            for b in range(f.lsb, f.msb + 1):
                owner[b] = i
        tint = [QColor(c.accent), QColor(c.trace2)]
        for col in range(n):
            bit = n - 1 - col
            r = QRectF(x0 + col * box, 14, box - 2, 24)
            fi = owner.get(bit)
            fill = QColor(tint[fi % 2]) if fi is not None else QColor(c.border)
            fill.setAlpha(55 if fi is not None else 70)
            p.setPen(QColor(c.border))
            p.setBrush(fill)
            p.drawRoundedRect(r, 3, 3)
            p.setFont(small)
            p.setPen(QColor(c.muted))
            if box >= 18 or bit % 4 == 0:
                p.drawText(QRectF(r.x(), 0, r.width(), 13), Qt.AlignCenter, str(bit))
            if self._value is not None:
                p.setFont(mono)
                p.setPen(QColor(c.text))
                p.drawText(r, Qt.AlignCenter, str((self._value >> bit) & 1))
        p.setFont(small)
        for i, f in enumerate(self._fields):
            left = x0 + (n - 1 - f.msb) * box
            right = x0 + (n - f.lsb) * box - 2
            col = tint[i % 2]
            p.setPen(col)
            p.drawLine(int(left), 42, int(right), 42)
            p.drawText(QRectF(left, 44, right - left, 16), Qt.AlignCenter, f.name)

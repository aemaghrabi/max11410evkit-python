"""Visual theme: Fusion style + light/dark palettes + a small application stylesheet.

The whole GUI uses Qt's *Fusion* style on every platform so that macOS, Windows and
Linux render the same instrument-like look. Colours come from a QPalette (so the
stylesheet can refer to ``palette(...)`` roles and follow light/dark switches), plus
a handful of named accent colours used by plots and status indicators.

``ThemeManager`` is a tiny singleton QObject: widgets that draw their own colours
(pyqtgraph plots, LEDs) connect to ``ThemeManager.instance().changed`` and re-read
``ThemeManager.instance().colors``.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

MODES = ("system", "light", "dark")


@dataclass(frozen=True)
class ThemeColors:
    name: str
    window: str
    base: str
    alt_base: str
    group: str
    text: str
    muted: str
    border: str
    button: str
    accent: str
    accent_text: str
    trace: str
    trace2: str
    plot_bg: str
    plot_fg: str
    grid_alpha: float
    ok: str
    warn: str
    bad: str


LIGHT = ThemeColors(
    name="light",
    window="#f3f4f6",
    base="#ffffff",
    alt_base="#f7f8fa",
    group="#f9fafb",
    text="#1d2330",
    muted="#667085",
    border="#d0d5dd",
    button="#fbfbfc",
    accent="#1f6feb",
    accent_text="#ffffff",
    trace="#1f6feb",
    trace2="#e8590c",
    plot_bg="#ffffff",
    plot_fg="#344054",
    grid_alpha=0.18,
    ok="#12b76a",
    warn="#f79009",
    bad="#d92d20",
)

DARK = ThemeColors(
    name="dark",
    window="#1b1e24",
    base="#2a2f39",
    alt_base="#262a33",
    group="#21252c",
    text="#e4e7ec",
    muted="#98a2b3",
    border="#434a57",
    button="#323843",
    accent="#4c8dff",
    accent_text="#ffffff",
    trace="#5ab0ff",
    trace2="#ff9f43",
    plot_bg="#111318",
    plot_fg="#c0c6d0",
    grid_alpha=0.22,
    ok="#32d583",
    warn="#fdb022",
    bad="#f97066",
)


def _palette(c: ThemeColors) -> QPalette:
    p = QPalette()
    col = QColor
    p.setColor(QPalette.Window, col(c.window))
    p.setColor(QPalette.WindowText, col(c.text))
    p.setColor(QPalette.Base, col(c.base))
    p.setColor(QPalette.AlternateBase, col(c.alt_base))
    p.setColor(QPalette.ToolTipBase, col(c.base))
    p.setColor(QPalette.ToolTipText, col(c.text))
    p.setColor(QPalette.PlaceholderText, col(c.muted))
    p.setColor(QPalette.Text, col(c.text))
    p.setColor(QPalette.Button, col(c.button))
    p.setColor(QPalette.ButtonText, col(c.text))
    p.setColor(QPalette.BrightText, col(c.bad))
    p.setColor(QPalette.Highlight, col(c.accent))
    p.setColor(QPalette.HighlightedText, col(c.accent_text))
    p.setColor(QPalette.Link, col(c.accent))
    p.setColor(QPalette.Mid, col(c.border))
    p.setColor(QPalette.Midlight, col(c.border))
    p.setColor(QPalette.Dark, col(c.border).darker(130))
    p.setColor(QPalette.Light, col(c.base))
    p.setColor(QPalette.Shadow, col("#000000"))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, col(c.muted))
    return p


def _stylesheet(c: ThemeColors) -> str:
    return f"""
    QGroupBox {{
        font-weight: 600;
        border: 1px solid {c.border};
        border-radius: 6px;
        margin-top: 12px;
        padding: 10px 8px 8px 8px;
        background: {c.group};
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        subcontrol-position: top left;
        left: 10px;
        padding: 0 4px;
        color: {c.text};
    }}
    QGroupBox QLabel, QGroupBox QCheckBox, QGroupBox QRadioButton {{ font-weight: normal; }}
    QGroupBox QPushButton, QGroupBox QComboBox, QGroupBox QLineEdit,
    QGroupBox QSpinBox, QGroupBox QDoubleSpinBox, QGroupBox QTableView {{ font-weight: normal; }}
    QTabWidget::pane {{ border: 1px solid {c.border}; border-radius: 6px; top: -1px;
        background: {c.window}; }}
    QTabBar::tab {{
        padding: 6px 16px; margin-right: 2px;
        border: 1px solid {c.border}; border-bottom: none;
        border-top-left-radius: 6px; border-top-right-radius: 6px;
        background: {c.button}; color: {c.muted};
    }}
    QTabBar::tab:selected {{ background: {c.window}; color: {c.text}; font-weight: 600;
        border-top: 2px solid {c.accent}; }}
    QTabBar::tab:hover:!selected {{ color: {c.text}; }}
    QPushButton {{ padding: 4px 12px; min-height: 20px; }}
    QPushButton[primary="true"] {{
        background: {c.accent}; color: {c.accent_text}; border: 1px solid {c.accent};
        border-radius: 4px; font-weight: 600; padding: 4px 16px;
    }}
    QPushButton[primary="true"]:hover {{ background: {QColor(c.accent).lighter(112).name()}; }}
    QPushButton[primary="true"]:pressed {{ background: {QColor(c.accent).darker(115).name()}; }}
    QPushButton[primary="true"]:checked {{ background: {c.trace2}; border-color: {c.trace2}; }}
    QLabel[role="muted"] {{ color: {c.muted}; }}
    QLabel[role="value"] {{ font-family: "{mono_family()}"; }}
    QLabel[role="readout"] {{ font-family: "{mono_family()}"; color: {c.text}; }}
    QLabel[role="unit"] {{ color: {c.muted}; }}
    QLineEdit[role="hexout"] {{ font-family: "{mono_family()}"; background: {c.window}; }}
    QTableView {{ gridline-color: {c.border}; selection-background-color: {c.accent}; }}
    QHeaderView::section {{ padding: 4px 6px; border: none; border-right: 1px solid {c.border};
        border-bottom: 1px solid {c.border}; background: {c.button}; font-weight: 600; }}
    QPlainTextEdit#statusLog {{ font-family: "{mono_family()}"; background: {c.base}; }}
    QStatusBar QLabel {{ padding: 0 6px; }}
    QFrame#readoutFrame {{ background: {c.base}; border: 1px solid {c.border};
        border-radius: 8px; }}
    """


def mono_family() -> str:
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    return f.family()


def mono_font(point_size: int | None = None) -> QFont:
    f = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    if point_size:
        f.setPointSize(point_size)
    return f


class ThemeManager(QObject):
    """Holds the active theme; emits ``changed`` after applying a new one."""

    changed = Signal()
    _instance: ThemeManager | None = None

    def __init__(self) -> None:
        super().__init__()
        self.mode = "system"
        self.colors: ThemeColors = LIGHT

    @classmethod
    def instance(cls) -> ThemeManager:
        if cls._instance is None:
            cls._instance = ThemeManager()
        return cls._instance

    @staticmethod
    def system_is_dark() -> bool:
        hints = QGuiApplication.styleHints()
        try:
            return hints.colorScheme() == Qt.ColorScheme.Dark
        except AttributeError:  # Qt < 6.5
            return QGuiApplication.palette().color(QPalette.Window).lightness() < 128

    def apply(self, mode: str = "system") -> None:
        if mode not in MODES:
            raise ValueError(mode)
        self.mode = mode
        dark = self.system_is_dark() if mode == "system" else mode == "dark"
        self.colors = DARK if dark else LIGHT
        app = QApplication.instance()
        app.setStyle("Fusion")
        app.setPalette(_palette(self.colors))
        app.setStyleSheet(_stylesheet(self.colors))
        self.changed.emit()

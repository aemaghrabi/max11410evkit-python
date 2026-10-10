"""Main window: tabs on top, Status Log below, connection status bottom-right."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QTabWidget,
    QWidget,
)

from .channels import ChannelStore
from .status_log import StatusLog
from .tabs import (
    AdcConfigTab,
    DmmTab,
    FftTab,
    HistogramTab,
    OtherTab,
    RegistersTab,
    ScopeTab,
    SequenceTab,
)
from .theme import ThemeManager
from .widgets import Led

APP_TITLE = "MAX11410 EV Kit"


class MainWindow(QMainWindow):
    # Window-level intents (file handling is GUI-side in the mockup; see DESIGN.md open questions)
    saveConfigRequested = Signal(str)  # path
    loadConfigRequested = Signal(str)  # path
    themeChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_TITLE)
        self._config_path: str | None = None

        # One shared host-side channel model (8 channel configs + active channel).
        self.channels = ChannelStore(self)
        self.adc_tab = AdcConfigTab(self.channels)
        self.other_tab = OtherTab(self.channels)
        self.sequence_tab = SequenceTab()
        self.registers_tab = RegistersTab()
        self.scope_tab = ScopeTab(self.channels)
        self.dmm_tab = DmmTab(self.channels)
        self.histogram_tab = HistogramTab(self.channels)
        self.fft_tab = FftTab(self.channels)

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(False)
        for name, tab in self.tab_items():
            self.tabs.addTab(tab, name)

        self.status_log = StatusLog()
        split = QSplitter(Qt.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(self.tabs)
        split.addWidget(self.status_log)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([640, 150])
        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(8, 8, 8, 4)
        lay.addWidget(split)
        self.setCentralWidget(central)

        # status bar (connection indicator bottom-right)
        self.conn_led = Led("off")
        self.conn_label = QLabel()
        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 6, 0)
        hl.setSpacing(6)
        hl.addWidget(self.conn_led)
        hl.addWidget(self.conn_label)
        self.statusBar().addPermanentWidget(holder)
        self.statusBar().setSizeGripEnabled(True)
        self.set_connected(False)

        self._build_menus()

    def tab_items(self) -> list[tuple[str, QWidget]]:
        return [
            ("ADC Config", self.adc_tab),
            ("Other", self.other_tab),
            ("Sequence", self.sequence_tab),
            ("Registers", self.registers_tab),
            ("Scope", self.scope_tab),
            ("DMM", self.dmm_tab),
            ("Histogram", self.histogram_tab),
            ("FFT", self.fft_tab),
        ]

    # ---------------------------------------------------------------- menus
    def _build_menus(self) -> None:
        mb = self.menuBar()
        f = mb.addMenu("&File")
        self._act(f, "Save ADC Config &As…", self._save_as, QKeySequence.SaveAs)
        self._act(f, "&Save ADC Config", self._save, QKeySequence.Save)
        self._act(f, "&Load ADC Config…", self._load, QKeySequence.Open)
        f.addSeparator()
        self._act(f, "E&xit", self.close, QKeySequence.Quit)

        o = mb.addMenu("&Options")
        sg = o.addMenu("Save &Graph")
        self._act(sg, "&Scope…", lambda: self._save_graph("scope", self.scope_tab.plot))
        self._act(sg, "&Histogram…", lambda: self._save_graph("histogram", self.histogram_tab.plot))
        self._act(sg, "&FFT…", lambda: self._save_graph("fft", self.fft_tab.plot))

        v = mb.addMenu("&View")
        tm = v.addMenu("&Theme")
        grp = QActionGroup(self)
        for mode, label in (("system", "Follow &System"), ("light", "&Light"), ("dark", "&Dark")):
            a = QAction(label, self, checkable=True)
            a.setChecked(mode == ThemeManager.instance().mode)
            a.triggered.connect(lambda _c=False, m=mode: self.set_theme(m))
            grp.addAction(a)
            tm.addAction(a)

        h = mb.addMenu("&Help")
        self._act(h, "&About", self._about)

    def _act(self, menu, text, slot, shortcut=None) -> QAction:
        a = QAction(text, self)
        if shortcut is not None:
            a.setShortcut(shortcut)
        a.triggered.connect(slot)
        menu.addAction(a)
        return a

    # ---------------------------------------------------------------- actions
    def set_theme(self, mode: str) -> None:
        ThemeManager.instance().apply(mode)
        self.themeChanged.emit(mode)
        self.log(f"Theme: {mode}")

    def _save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save ADC Config As", "max11410_config.json", "ADC config (*.json)"
        )
        if path:
            self._config_path = path
            self._write_config(path)

    def _save(self) -> None:
        if self._config_path:
            self._write_config(self._config_path)
        else:
            self._save_as()

    def _write_config(self, path: str) -> None:
        Path(path).write_text(json.dumps(self.adc_tab.config(), indent=2))
        self.saveConfigRequested.emit(path)
        self.log(f"ADC config saved to {path}", "ok")

    def _load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load ADC Config", "", "ADC config (*.json)")
        if not path:
            return
        try:
            cfg = json.loads(Path(path).read_text())
        except (OSError, ValueError) as e:
            self.log(f"Could not load {path}: {e}", "error")
            return
        self.adc_tab.set_config(cfg)
        self._config_path = path
        self.loadConfigRequested.emit(path)
        self.log(f"ADC config loaded from {path}", "ok")

    def _save_graph(self, name: str, plot) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, f"Save {name} graph", f"{name}.png", "PNG image (*.png)"
        )
        if path:
            plot.grab().save(path)
            self.log(f"{name.capitalize()} graph saved to {path}", "ok")

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_TITLE}",
            "<h3>MAX11410 EV Kit</h3>"
            "<p>Cross-platform control software for the MAX11410EVKIT "
            "(24-bit, 10-channel delta-sigma ADC).</p>"
            "<p>PySide6 · pyqtgraph · numpy</p>",
        )

    # ---------------------------------------------------------------- public API
    @Slot(bool)
    def set_connected(self, connected: bool, detail: str = "") -> None:
        self.conn_led.set_state("ok" if connected else "bad")
        text = "EV Kit Hardware Connected" if connected else "EV Kit Hardware Disconnected"
        self.conn_label.setText(f"{text}{'  ·  ' + detail if detail else ''}")

    @Slot(str, str)
    def log(self, message: str, level: str = "info") -> None:
        self.status_log.log(message, level)

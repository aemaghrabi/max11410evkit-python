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


#: Tooltip on tabs that are not wired to the board yet (Phase 5 greys them out).
UNWIRED_TIP = "Not wired to the board yet: {name} is wired in Phase {phase}."
TAB_PHASE = {
    "ADC Config": 6, "Other": 6, "Sequence": 6, "Registers": 6,
    "Scope": 7, "DMM": 7, "Histogram": 7, "FFT": 7,
}  # fmt: skip


class MainWindow(QMainWindow):
    # Window-level intents. With ``device_files`` (a board controller is attached) Save/Load
    # only emit these and the controller does the work; otherwise (--mock) the window writes
    # the GUI's own file format itself.
    saveConfigRequested = Signal(str)  # path
    loadConfigRequested = Signal(str)  # path
    themeChanged = Signal(str)
    connectRequested = Signal()  # File > Connect… (show the kit picker)
    disconnectRequested = Signal()  # File > Disconnect
    simulatedToggled = Signal(bool)  # File > Use Simulated Board
    closing = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(APP_TITLE)
        self._config_path: str | None = None
        self.device_files = False

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
        self.act_connect = self._act(f, "&Connect…", self.connectRequested.emit)
        self.act_disconnect = self._act(f, "&Disconnect", self.disconnectRequested.emit)
        self.act_simulated = QAction("Use &Simulated Board", self, checkable=True)
        self.act_simulated.setToolTip("Work offline on an emulated EV kit (no hardware).")
        self.act_simulated.toggled.connect(self._on_simulated_toggled)
        f.addAction(self.act_simulated)
        f.addSeparator()
        self.act_save_as = self._act(f, "Save ADC Config &As…", self._save_as, QKeySequence.SaveAs)
        self.act_save = self._act(f, "&Save ADC Config", self._save, QKeySequence.Save)
        self.act_load = self._act(f, "&Load ADC Config…", self._load, QKeySequence.Open)
        f.addSeparator()
        self._act(f, "E&xit", self.close, QKeySequence.Quit)
        self._sim_guard = False

        o = mb.addMenu("&Options")
        sg = o.addMenu("Save &Graph")
        self.save_graph_menu = sg
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
        if self.device_files:
            self.saveConfigRequested.emit(path)
            return
        Path(path).write_text(json.dumps(self.adc_tab.config(), indent=2))
        self.saveConfigRequested.emit(path)
        self.log(f"ADC config saved to {path}", "ok")

    def _load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Load ADC Config", "", "ADC config (*.json)")
        if not path:
            return
        if self.device_files:
            self._config_path = path
            self.loadConfigRequested.emit(path)
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

    def _on_simulated_toggled(self, on: bool) -> None:
        if not self._sim_guard:
            self.simulatedToggled.emit(on)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.closing.emit()
        super().closeEvent(event)

    # ---------------------------------------------------------------- public API
    def set_simulated_checked(self, on: bool) -> None:
        """Tick/untick File > Use Simulated Board without emitting ``simulatedToggled``."""
        self._sim_guard = True
        try:
            self.act_simulated.setChecked(on)
        finally:
            self._sim_guard = False

    def set_file_actions_enabled(self, enabled: bool) -> None:
        """Save/Load need a connected (real or simulated) board."""
        for a in (self.act_save_as, self.act_save, self.act_load):
            a.setEnabled(enabled)

    def set_board_actions_visible(self, visible: bool) -> None:
        """Connect/Disconnect/Simulated only make sense with a board controller (not --mock)."""
        for a in (self.act_connect, self.act_disconnect, self.act_simulated):
            a.setVisible(visible)

    def set_tabs_wired(self, wired: bool | dict[str, bool]) -> None:
        """Grey out the tabs that are not wired to the board yet.

        ``wired`` is one flag for every tab or ``{tab name: flag}``. A greyed tab still shows
        the values the controller puts into it (e.g. after Read All).
        """
        for i, (name, tab) in enumerate(self.tab_items()):
            on = wired.get(name, False) if isinstance(wired, dict) else wired
            tab.setEnabled(on)
            tip = "" if on else UNWIRED_TIP.format(name=name, phase=TAB_PHASE.get(name, 6))
            tab.setToolTip(tip)
            self.tabs.setTabToolTip(i, tip)
        all_on = all(wired.values()) if isinstance(wired, dict) else wired
        self.save_graph_menu.setEnabled(bool(all_on))

    @Slot(bool)
    def set_connected(self, connected: bool, detail: str = "") -> None:
        self.conn_led.set_state("ok" if connected else "bad")
        text = "EV Kit Hardware Connected" if connected else "EV Kit Hardware Disconnected"
        self.conn_label.setText(f"{text}{'  ·  ' + detail if detail else ''}")

    @Slot(str, str)
    def log(self, message: str, level: str = "info") -> None:
        self.status_log.log(message, level)

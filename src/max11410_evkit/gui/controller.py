"""DeviceController: connects the real board (through DeviceWorker) to the main window.

Runs in the main (GUI) thread. It owns the worker thread, turns window intents into queued
worker calls, asks the user when a choice or a confirmation is needed, and puts the worker's
results into the tabs. Phase 5 scope: connection, Read All display, File Save/Load. The tabs
stay greyed out until Phases 6/7 wire them (decisions in ``docs/plan/p5-gui-shell.md``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import QApplication, QCheckBox, QInputDialog, QMessageBox

from .adapters import gui_channels, gui_device
from .worker import BoardEntry, DeviceWorker

#: (parent, entries) -> chosen key or None
PickFn = Callable[[object, Sequence[BoardEntry]], "str | None"]
#: (parent, path) -> (go ahead?, with_cal)
ConfirmLoadFn = Callable[[object, str], "tuple[bool, bool]"]


def pick_board_dialog(parent, entries: Sequence[BoardEntry]) -> str | None:
    if not entries:
        QMessageBox.information(parent, "Connect", "No MAX11410 EV kit is attached.")
        return None
    labels = [e.label for e in entries]
    choice, ok = QInputDialog.getItem(
        parent, "Select EV kit", "Several MAX11410 EV kits are attached. Connect to:",
        labels, 0, False,
    )  # fmt: skip
    if not ok:
        return None
    return entries[labels.index(choice)].key


def confirm_load_dialog(parent, path: str) -> tuple[bool, bool]:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning)
    box.setWindowTitle("Load ADC Config")
    box.setText(f"Write the configuration in “{Path(path).name}” to the board?")
    box.setInformativeText(
        "This overwrites the register settings on the board (PD is written last, then every "
        "register is read back and checked)."
    )
    cal = QCheckBox("Also restore the calibration registers")
    cal.setToolTip("SYS_OFF_A/B, SYS_GAIN_A/B, SELF_OFF and SELF_GAIN_1..128 (CLI: --with-cal)")
    box.setCheckBox(cal)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel)
    go = box.exec() == QMessageBox.Yes
    return go, cal.isChecked()


class DeviceController(QObject):
    # ---- intents to the worker (queued: the worker lives in its own thread)
    _start = Signal()
    _connectTo = Signal(str)
    _listNow = Signal()
    _disconnect = Signal()
    _setSimulated = Signal(bool)
    _readAll = Signal()
    _save = Signal(str)
    _load = Signal(str, bool)

    def __init__(
        self,
        window,
        *,
        simulated: bool = False,
        pick: PickFn = pick_board_dialog,
        confirm_load: ConfirmLoadFn = confirm_load_dialog,
        **worker_kwargs,
    ) -> None:
        super().__init__(window)
        self.w = window
        self._pick = pick
        self._confirm_load = confirm_load
        self.connected_info: dict | None = None
        self.snapshot: dict | None = None
        self._running = False

        self.thread = QThread(self)
        self.thread.setObjectName("max11410-device")
        self.worker = DeviceWorker(simulated=simulated, **worker_kwargs)
        self.worker.moveToThread(self.thread)
        self.thread.finished.connect(self.worker.deleteLater)

        # controller -> worker
        self._start.connect(self.worker.start)
        self._connectTo.connect(self.worker.connect_to)
        self._listNow.connect(self.worker.list_now)
        self._disconnect.connect(self.worker.disconnect_board)
        self._setSimulated.connect(self.worker.set_simulated)
        self._readAll.connect(self.worker.read_all)
        self._save.connect(self.worker.save_config)
        self._load.connect(self.worker.load_config)
        # worker -> controller
        wk = self.worker
        wk.connected.connect(self._on_connected)
        wk.disconnected.connect(self._on_disconnected)
        wk.boardsChanged.connect(self._on_boards)
        wk.snapshotReady.connect(self._on_snapshot)
        wk.log.connect(self.w.log)
        wk.failed.connect(self._on_failed)
        wk.simulatedChanged.connect(self.w.set_simulated_checked)
        # window -> controller
        w = self.w
        w.device_files = True
        w.set_board_actions_visible(True)
        w.set_tabs_wired(False)
        w.set_simulated_checked(simulated)
        w.set_file_actions_enabled(False)
        w.set_connected(False)
        w.connectRequested.connect(self._listNow)
        w.disconnectRequested.connect(self._disconnect)
        w.simulatedToggled.connect(self._setSimulated)
        w.saveConfigRequested.connect(self._save)
        w.loadConfigRequested.connect(self._on_load_requested)
        w.closing.connect(self.shutdown)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

    # ================================================================ lifecycle
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.thread.start()
        self._start.emit()

    @Slot()
    def shutdown(self) -> None:
        """Close the board and stop the worker thread (idempotent)."""
        if not self._running:
            return
        self._running = False
        # Blocking: the board is closed before the thread's event loop stops.
        QMetaObject.invokeMethod(self.worker, "stop", Qt.BlockingQueuedConnection)
        self.thread.quit()
        self.thread.wait(5000)

    def read_all(self) -> None:
        self._readAll.emit()

    # ================================================================ worker results
    @Slot(dict)
    def _on_connected(self, info: dict) -> None:
        self.connected_info = info
        if info.get("simulated"):
            detail = f"Simulated board · fw {info['firmware']}"
            self.w.log("Simulated EV kit connected (no hardware)", "ok")
        else:
            detail = f"fw {info['firmware']} · {info['key']}"
            self.w.log(
                f"EV kit hardware connected: {info['label']}, firmware {info['firmware']}, "
                f"PART_ID 0x{info['part_id']:06X}",
                "ok",
            )
        self.w.set_connected(True, detail)
        self.w.set_file_actions_enabled(True)

    @Slot(str)
    def _on_disconnected(self, reason: str) -> None:
        self.connected_info = None
        self.w.set_connected(False)
        self.w.set_file_actions_enabled(False)
        self.w.log(f"EV kit hardware disconnected ({reason})", "info")

    @Slot(list)
    def _on_boards(self, entries: list) -> None:
        key = self._pick(self.w, entries)
        if key is not None:
            self._connectTo.emit(key)

    @Slot(dict)
    def _on_snapshot(self, snap: dict) -> None:
        self.snapshot = snap
        regs: dict[str, int] = snap["registers"]
        addrs: dict[str, int] = snap["addresses"]
        adc = self.w.adc_tab
        adc.set_device_config(gui_device(regs))
        adc.set_channel_configs(gui_channels(snap["channels"], ctrl=regs.get("CTRL")))
        adc.set_active_channel(0)
        by_addr = {addrs[n]: v for n, v in regs.items()}
        for addr, value in by_addr.items():
            self.w.registers_tab.table.set_value(addr, value)
        self.w.sequence_tab.set_microcode_values(
            {a: v for a, v in by_addr.items() if 0x3A <= a <= 0x6E}
        )

    @Slot(str, str)
    def _on_failed(self, action: str, message: str) -> None:
        self.w.log(f"{action} failed: {message}", "error")

    # ================================================================ window intents
    @Slot(str)
    def _on_load_requested(self, path: str) -> None:
        go, with_cal = self._confirm_load(self.w, path)
        if not go:
            self.w.log(f"Load of {Path(path).name} cancelled; the board was not changed", "info")
            return
        self._load.emit(path, with_cal)

"""DeviceWorker: the only GUI code that touches the board (Phase 5).

It lives in its own ``QThread``. The main thread talks to it only through queued signals
(see :class:`~max11410_evkit.gui.controller.DeviceController`), so every board command runs
on the worker thread and the window never blocks.

Behaviour (decisions in ``docs/plan/p5-gui-shell.md``):

* Connecting only **reads** the board: project name, firmware version, PART_ID, then Read All.
  Nothing is written (``MAX11410.init()`` is not called; it writes PD and CTRL.FORMAT).
* Channel 0 is decoded from MUX_CTRL0/CTRL/PGA/FILTER; channels 1-7 are default, disabled.
* Once a second the USB list is checked. No board chosen yet: 1 kit -> connect, 2+ -> ask
  (``boardsChanged``). The chosen kit is reconnected automatically after an unplug; if it is
  gone and 2+ other kits are present, the user is asked. The question is asked once per change
  in the set of kits.
* Simulated mode uses the fake board (``FakeEvKitTransport``) and skips the USB checks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from max11410_evkit.board.evkit import EvKitBoard, connect
from max11410_evkit.config import DeviceConfig, apply_config, default_channels, read_config
from max11410_evkit.device.max11410 import MAX11410, channel_from_registers
from max11410_evkit.transport.errors import EvKitError, NotConnected

log = logging.getLogger("max11410_evkit.gui.worker")

SIMULATED_KEY = "simulated"


@dataclass(frozen=True)
class BoardEntry:
    """One EV kit as the GUI sees it. ``key`` = serial number, or the USB path without one."""

    key: str
    label: str
    path: bytes | str | None = None
    simulated: bool = False


def entry_from_info(info: Any) -> BoardEntry:
    """``transport.hid.BoardInfo`` -> BoardEntry."""
    path_str = info.path_str
    key = info.serial or path_str
    serial = info.serial or "no serial"
    return BoardEntry(key=key, label=f"{info.product or 'MAXQ622HID'} ({serial}) — {path_str}",
                      path=info.path)  # fmt: skip


def usb_boards() -> list[BoardEntry]:
    from max11410_evkit.transport.hid import list_boards

    return [entry_from_info(i) for i in list_boards()]


def open_usb_board(entry: BoardEntry) -> EvKitBoard:
    return connect(entry.path)


def open_simulated_board(entry: BoardEntry) -> EvKitBoard:
    from max11410_evkit.transport.fake import FakeEvKitTransport

    return connect(transport=FakeEvKitTransport())


class DeviceWorker(QObject):
    # ---- results (worker -> main thread)
    connected = Signal(dict)  # {"key", "label", "firmware", "part_id", "simulated"}
    disconnected = Signal(str)  # reason
    boardsChanged = Signal(list)  # list[BoardEntry]: the user has to pick one
    snapshotReady = Signal(dict)  # see _read_all()
    configSaved = Signal(str)  # path
    configLoaded = Signal(str, dict)  # path, {"written", "skipped_cal", "mismatches"}
    busyChanged = Signal(bool)
    simulatedChanged = Signal(bool)
    log = Signal(str, str)  # message, level
    failed = Signal(str, str)  # action, message

    def __init__(
        self,
        *,
        list_boards: Callable[[], Sequence[BoardEntry]] = usb_boards,
        open_board: Callable[[BoardEntry], EvKitBoard] = open_usb_board,
        open_simulated: Callable[[BoardEntry], EvKitBoard] = open_simulated_board,
        poll_ms: int = 1000,
        simulated: bool = False,
    ) -> None:
        super().__init__()
        self._list_boards = list_boards
        self._open_board = open_board
        self._open_simulated = open_simulated
        self._poll_ms = poll_ms
        self._simulated = simulated
        self.board: EvKitBoard | None = None
        self.dev: MAX11410 | None = None
        self.entry: BoardEntry | None = None
        self.target: str | None = None  # key of the kit to (re)connect to
        self.auto = True  # automatic connect / reconnect
        self._offered: frozenset[str] | None = None  # set of kits last offered in the picker
        self._last_error: str | None = None
        self._last_list_error: str | None = None
        self._timer: QTimer | None = None

    # ================================================================ lifecycle
    @Slot()
    def start(self) -> None:
        """Called once the worker is in its thread: start the USB checks."""
        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_ms)
        self._timer.timeout.connect(self.tick)
        self._timer.start()
        if self.simulated:
            self._open(BoardEntry(SIMULATED_KEY, "Simulated board", simulated=True))
        else:
            self.tick()

    @Slot()
    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._close()

    @property
    def simulated(self) -> bool:
        return self._simulated

    @simulated.setter
    def simulated(self, on: bool) -> None:
        if on != self._simulated:
            self._simulated = on
            self.simulatedChanged.emit(on)

    @property
    def is_connected(self) -> bool:
        return self.dev is not None

    # ================================================================ USB checks
    @Slot()
    def tick(self) -> None:
        if self.simulated:
            return
        try:
            boards = list(self._list_boards())
        except Exception as exc:  # hidapi missing / enumeration error: report once
            msg = f"USB enumeration failed: {exc}"
            if msg != self._last_list_error:
                self._last_list_error = msg
                self.log.emit(msg, "error")
            return
        self._last_list_error = None
        by_key = {b.key: b for b in boards}

        if self.is_connected:
            if self.entry is not None and self.entry.key not in by_key:
                self._lost("board unplugged")
            else:
                return
        if not self.auto:
            return
        if self.target is not None and self.target in by_key:
            self._open(by_key[self.target])
            return
        if self.target is None and len(boards) == 1:
            self._open(boards[0])
            return
        if len(boards) >= 2:
            keys = frozenset(by_key)
            if keys != self._offered:
                self._offered = keys
                self.boardsChanged.emit(boards)
            return
        self._offered = None  # 0 kits (or 1 kit that is not ours): ask again next time

    # ================================================================ user actions
    @Slot(str)
    def connect_to(self, key: str) -> None:
        """Connect to the kit with this key (from the picker); re-enables auto-reconnect."""
        if self.simulated:
            self.set_simulated(False)
        self.auto = True
        self.target = key
        if self.is_connected and self.entry is not None and self.entry.key == key:
            return
        self._close()
        try:
            boards = {b.key: b for b in self._list_boards()}
        except Exception as exc:
            self.failed.emit("connect", f"USB enumeration failed: {exc}")
            return
        if key not in boards:
            self.failed.emit("connect", "that EV kit is no longer attached")
            return
        self._open(boards[key])

    @Slot()
    def list_now(self) -> None:
        """File > Connect…: always offer the current list (even with 0 or 1 kits)."""
        try:
            boards = list(self._list_boards())
        except Exception as exc:
            self.failed.emit("connect", f"USB enumeration failed: {exc}")
            return
        self._offered = frozenset(b.key for b in boards)
        self.boardsChanged.emit(boards)

    @Slot()
    def disconnect_board(self) -> None:
        """File > Disconnect: close and stop reconnecting until the user connects again."""
        self.auto = False
        if self.simulated:
            self.simulated = False
        if self.is_connected:
            self._close()
            self.disconnected.emit("disconnected by the user")

    @Slot(bool)
    def set_simulated(self, on: bool) -> None:
        if on == self.simulated:
            return
        self._close()
        self.simulated = on
        if on:
            self.disconnected.emit("switching to the simulated board")
            self._open(BoardEntry(SIMULATED_KEY, "Simulated board", simulated=True))
        else:
            self.disconnected.emit("simulated board closed")
            self.auto = True
            self._offered = None
            self.tick()

    @Slot()
    def read_all(self) -> None:
        self._guard("Read All", self._read_all)

    def _read_all_fresh(self) -> None:
        self._read_all(reset_channels=True)

    @Slot(str)
    def save_config(self, path: str) -> None:
        def run() -> None:
            assert self.dev is not None
            cfg = read_config(self.dev)
            cfg.save(path, self.dev.regs.spec)
            self.log.emit(
                f"ADC config saved to {path} ({len(cfg.registers)} registers, 8 channels)", "ok"
            )
            self.configSaved.emit(path)

        self._guard("Save ADC config", run)

    @Slot(str, bool)
    def load_config(self, path: str, with_cal: bool) -> None:
        def run() -> None:
            assert self.dev is not None
            cfg = DeviceConfig.load(path, self.dev.regs.spec)
            res = apply_config(self.dev, cfg, with_cal=with_cal, verify=True)
            spec = self.dev.regs.spec
            mism = [
                f"{n}: wrote {spec[n].fmt(w)}, read back {spec[n].fmt(b)}"
                for n, w, b in res.mismatches
            ]
            self.log.emit(
                f"ADC config loaded from {Path(path).name}: {len(res.written)} registers written"
                + (
                    f", {len(res.skipped_cal)} calibration registers skipped"
                    if res.skipped_cal
                    else ""
                )  # fmt: skip
                + ("" if mism else ", all read back correctly"),
                "warn" if mism else "ok",
            )
            for m in mism:
                self.log.emit(f"Readback mismatch — {m}", "warn")
            self.configLoaded.emit(
                path,
                {"written": len(res.written), "skipped_cal": len(res.skipped_cal),
                 "mismatches": mism},
            )  # fmt: skip
            self._read_all()

        self._guard("Load ADC config", run)

    # ================================================================ internals
    def _guard(self, action: str, fn: Callable[[], None]) -> None:
        if not self.is_connected:
            self.failed.emit(action, "no EV kit connected")
            return
        self.busyChanged.emit(True)
        try:
            fn()
        except NotConnected as exc:
            self._lost(str(exc))
            self.failed.emit(action, f"board connection lost ({exc})")
        except (EvKitError, OSError, ValueError) as exc:  # ConfigError is a ValueError
            self.failed.emit(action, str(exc))
        finally:
            self.busyChanged.emit(False)

    def _open(self, entry: BoardEntry) -> None:
        opener = self._open_simulated if entry.simulated else self._open_board
        board: EvKitBoard | None = None
        try:
            board = opener(entry)
            dev = MAX11410(board)
            firmware = str(board.version())
            part_id = dev.identify()
        except Exception as exc:  # report each distinct failure once, keep retrying
            if board is not None:
                try:
                    board.close()
                except Exception:
                    log.debug("close after failed open", exc_info=True)
            msg = f"Could not connect to {entry.label}: {exc}"
            if msg != self._last_error:
                self._last_error = msg
                self.log.emit(msg, "error")
            if entry.simulated:
                self.simulated = False
            return
        self._last_error = None
        self._offered = None
        self.board, self.dev, self.entry = board, dev, entry
        if not entry.simulated:
            self.target = entry.key
        self.connected.emit(
            {"key": entry.key, "label": entry.label, "firmware": firmware,
             "part_id": part_id, "simulated": entry.simulated}
        )  # fmt: skip
        self._guard("Read All", self._read_all_fresh)

    def _read_all(self, reset_channels: bool = False) -> None:
        """Read every readable register and decode channel 0 from the board.

        ``reset_channels`` (a fresh connect): start from the default channels, so channels 1-7
        are disabled defaults and every reference voltage is 2.5 V. Otherwise the channel configs
        the device holds (e.g. from a loaded config file, which is where the reference voltages
        come from) are kept. Channel 0's register-backed settings always come from the board.
        """
        assert self.dev is not None
        regs = self.dev.regs
        values = regs.read_all(include_sequencer=True)
        if reset_channels:
            for i, c in enumerate(default_channels()):
                self.dev.set_channel(i, c)
        ch0, notes = channel_from_registers(values, self.dev.channels[0])
        self.dev.set_channel(0, replace(ch0, enabled=True))
        for n in notes:
            self.log.emit(f"Channel 0: {n}", "warn")
        spec = regs.spec
        self.snapshotReady.emit(
            {
                "registers": dict(values),
                "addresses": {name: spec[name].addr for name in values},
                "channels": [replace(c) for c in self.dev.channels],
                "notes": notes,
            }
        )
        self.log.emit(f"Read All: {len(values)} registers read", "ok")

    def _lost(self, reason: str) -> None:
        self._close()
        self.disconnected.emit(reason)
        self.log.emit(f"EV kit disconnected: {reason}", "warn")

    def _close(self) -> None:
        board, self.board, self.dev, self.entry = self.board, None, None, None
        if board is not None:
            try:
                board.close()
            except Exception:
                log.debug("board close failed", exc_info=True)

"""Helpers for the Phase 5 GUI tests: offscreen QApplication, event-loop waits, fake USB."""

from __future__ import annotations

import os
import time
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from max11410_evkit.board.evkit import EvKitBoard, connect  # noqa: E402
from max11410_evkit.gui.worker import BoardEntry  # noqa: E402
from max11410_evkit.transport.errors import NotConnected  # noqa: E402
from max11410_evkit.transport.fake import FakeEvKitTransport  # noqa: E402


def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def wait_until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    """Process Qt events until ``cond()`` is true (AssertionError on timeout)."""
    app = qapp()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return
        time.sleep(0.005)
    app.processEvents()
    assert cond(), "condition not met within timeout"


class FakeUsb:
    """A USB bus of fake EV kits that tests can plug and unplug."""

    def __init__(self, *keys: str) -> None:
        self.present: dict[str, BoardEntry] = {}
        self.transports: dict[str, FakeEvKitTransport] = {}
        self.opened: list[str] = []
        for k in keys:
            self.plug(k)

    def entry(self, key: str) -> BoardEntry:
        return BoardEntry(key=key, label=f"MAXQ622HID ({key})", path=key.encode())

    def plug(self, key: str) -> FakeEvKitTransport:
        t = FakeEvKitTransport(drdy_timeout_s=0.05)
        self.transports[key] = t
        self.present[key] = self.entry(key)  # last: the worker thread may list it at once
        return t

    def unplug(self, key: str) -> None:
        self.present.pop(key, None)
        self.transports[key].disconnect()

    def list_boards(self) -> list[BoardEntry]:
        return list(self.present.values())

    def open_board(self, entry: BoardEntry) -> EvKitBoard:
        if entry.key not in self.present:
            raise NotConnected(f"{entry.key} is not attached")
        self.opened.append(entry.key)
        return connect(transport=self.transports[entry.key], timeout=0.5, drdy_timeout=1.0)

    def spi_writes(self, key: str) -> list[int]:
        """Register addresses written over SPI on that kit (first byte without the read bit)."""
        return [first for first, _mosi, _miso in self.transports[key].adc.spi_log
                if not first & 0x80]  # fmt: skip

"""A stand-in for the ``hid`` (hidapi) module, bridged to FakeEvKitTransport, plus a spy."""

from __future__ import annotations

from max11410_evkit.transport.base import Transport
from max11410_evkit.transport.fake import FakeEvKitTransport


class FakeHidDevice:
    def __init__(self, module: FakeHidModule) -> None:
        self.module = module
        self.board: FakeEvKitTransport | None = None
        self.nonblocking = 0
        self.writes: list[bytes] = []
        self.opened_with: tuple | None = None
        self.fail_io = False

    def open(self, vid, pid, serial=None):
        if (vid, pid) != (0x0B6A, 0x2B31) or not self.module.present:
            raise OSError("open failed")
        self.opened_with = ("vidpid", vid, pid, serial)
        self._attach()

    def open_path(self, path):
        if not self.module.present:
            raise OSError("open failed")
        self.opened_with = ("path", path)
        self._attach()

    def _attach(self):
        self.board = FakeEvKitTransport()
        self.board.open()
        self.module.devices.append(self)

    def set_nonblocking(self, v):
        self.nonblocking = v
        return 0

    def write(self, buf):
        buf = bytes(buf)
        self.writes.append(buf)
        if self.fail_io:
            return -1
        assert len(buf) == 65 and buf[0] == 0, "expected report ID 0 + 64 bytes"
        self.board.write_report(buf[1:])
        return len(buf)

    def read(self, max_length, timeout_ms=0):
        if self.fail_io:
            raise OSError("read error")
        if timeout_ms == 0 and not self.nonblocking:
            raise AssertionError("blocking read without timeout would hang on a real board")
        r = self.board.read_report(timeout_ms)
        return list(r[:max_length]) if r else []

    def close(self):
        if self.board:
            self.board.close()


class FakeHidModule:
    """Install with ``monkeypatch.setitem(sys.modules, "hid", FakeHidModule())``."""

    def __init__(self, entries: list[dict] | None = None, present: bool = True) -> None:
        self.present = present
        self.devices: list[FakeHidDevice] = []
        self.entries = (
            entries
            if entries is not None
            else [
                {
                    "path": b"/dev/hidraw7",
                    "vendor_id": 0x0B6A,
                    "product_id": 0x2B31,
                    "serial_number": "",
                    "release_number": 0x0100,
                    "manufacturer_string": "Maxim",
                    "product_string": "MAXQ622HID",
                    "usage_page": 0xFF00,
                    "usage": 1,
                    "interface_number": 0,
                }
            ]
        )

    def device(self) -> FakeHidDevice:
        return FakeHidDevice(self)

    def enumerate(self, vid=0, pid=0):
        return [
            e
            for e in self.entries
            if (not vid or e["vendor_id"] == vid) and (not pid or e["product_id"] == pid)
        ]


class SpyTransport(Transport):
    """Scripted transport: returns queued reports and records writes."""

    def __init__(self, reports: list[bytes | None] | None = None) -> None:
        self.reports = list(reports or [])
        self.writes: list[bytes] = []
        self.on_write = None
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def write_report(self, data: bytes) -> None:
        self.writes.append(bytes(data).ljust(64, b"\x00"))
        if self.on_write:
            self.on_write(self, bytes(data))

    def read_report(self, timeout_ms: int) -> bytes | None:
        if self.reports:
            return self.reports.pop(0)
        return None

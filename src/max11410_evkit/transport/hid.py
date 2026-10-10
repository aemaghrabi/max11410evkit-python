"""USB-HID transport on top of the ``hidapi`` pip package (``import hid``, ``hid.device()``).

The ``hid`` module is imported lazily so that importing this package (and running the
test-suite) never requires the native hidapi library.

Board facts (enum_hid.h): VID 0x0B6A, PID 0x2B31, product "MAXQ622HID", vendor usage
page 0xFF00, no report IDs, 64-byte reports both ways, bInterval 1 ms.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from max11410_evkit.transport.base import REPORT_SIZE, Transport
from max11410_evkit.transport.errors import EvKitError, NotConnected

EVKIT_VID = 0x0B6A
EVKIT_PID = 0x2B31

log = logging.getLogger("max11410_evkit.transport")


def _import_hid() -> Any:
    try:
        import hid  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise EvKitError(
            "The 'hid' module is missing. Install it with: pip install hidapi"
        ) from exc
    except OSError as exc:  # pragma: no cover - native library failed to load
        raise EvKitError(f"Could not load the native hidapi library: {exc}") from exc
    if not hasattr(hid, "device"):
        raise EvKitError(
            "The installed 'hid' module is not the 'hidapi' package (no hid.device). "
            "Run: pip uninstall hid && pip install hidapi"
        )
    return hid


@dataclass(frozen=True)
class BoardInfo:
    """One EVKIT found by :func:`list_boards`."""

    path: bytes
    serial: str
    product: str
    manufacturer: str
    release: int
    interface: int
    usage_page: int

    @property
    def path_str(self) -> str:
        return (
            self.path.decode(errors="replace") if isinstance(self.path, bytes) else str(self.path)
        )

    def __str__(self) -> str:
        serial = self.serial or "-"
        return f"{self.product or 'MAXQ622HID'}  serial={serial}  path={self.path_str}"


def list_boards(vid: int = EVKIT_VID, pid: int = EVKIT_PID) -> list[BoardInfo]:
    """Enumerate attached EVKITs (deduplicated by OS path)."""
    hid = _import_hid()
    seen: set[bytes] = set()
    boards: list[BoardInfo] = []
    for d in hid.enumerate(vid, pid):
        path = d.get("path") or b""
        if isinstance(path, str):
            path = path.encode()
        if path in seen:
            continue
        seen.add(path)
        boards.append(
            BoardInfo(
                path=path,
                serial=d.get("serial_number") or "",
                product=d.get("product_string") or "",
                manufacturer=d.get("manufacturer_string") or "",
                release=int(d.get("release_number") or 0),
                interface=int(
                    d.get("interface_number") if d.get("interface_number") is not None else -1
                ),
                usage_page=int(d.get("usage_page") or 0),
            )
        )
    return boards


class HidTransport(Transport):
    """hidapi-backed transport.

    ``path`` (from :func:`list_boards`) selects a specific board; otherwise the first board
    matching VID/PID (and ``serial`` if given) is opened.
    """

    def __init__(
        self,
        path: bytes | str | None = None,
        *,
        serial: str | None = None,
        vid: int = EVKIT_VID,
        pid: int = EVKIT_PID,
    ) -> None:
        if isinstance(path, str):
            path = path.encode()
        self.path = path
        self.serial = serial
        self.vid = vid
        self.pid = pid
        self._dev: Any = None

    @property
    def description(self) -> str:
        if self.path:
            return f"HID {self.path.decode(errors='replace')}"
        return f"HID {self.vid:04X}:{self.pid:04X}" + (
            f" serial {self.serial}" if self.serial else ""
        )

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    def open(self) -> None:
        if self._dev is not None:
            return
        hid = _import_hid()
        dev = hid.device()
        try:
            if self.path:
                dev.open_path(self.path)
            elif self.serial:
                dev.open(self.vid, self.pid, self.serial)
            else:
                dev.open(self.vid, self.pid)
            # The board sends a filler report every ~1 ms; a blocking read(n) without a
            # timeout would make the pre-command drain loop never terminate.
            dev.set_nonblocking(1)
        except (OSError, ValueError) as exc:
            with contextlib.suppress(Exception):
                dev.close()
            raise NotConnected(
                f"could not open MAX11410EVKIT ({self.description}): {exc}. "
                "Is it plugged in? On Linux, install the udev rule from the README."
            ) from exc
        self._dev = dev
        log.debug("opened %s", self.description)

    def close(self) -> None:
        dev, self._dev = self._dev, None
        if dev is not None:
            with contextlib.suppress(Exception):
                dev.close()
            log.debug("closed %s", self.description)

    def _require(self) -> Any:
        if self._dev is None:
            raise NotConnected("device is not open")
        return self._dev

    def write_report(self, data: bytes) -> None:
        dev = self._require()
        if len(data) > REPORT_SIZE:
            raise ValueError(f"report too long ({len(data)} > {REPORT_SIZE})")
        # hidapi wants the report ID first; the EVKIT uses none, so 0x00 + 64 bytes.
        buf = b"\x00" + bytes(data).ljust(REPORT_SIZE, b"\x00")
        try:
            n = dev.write(buf)
        except (OSError, ValueError) as exc:
            self.close()
            raise NotConnected(f"HID write failed: {exc}") from exc
        if n is None or n < 0:
            self.close()
            raise NotConnected("HID write failed (device unplugged?)")

    def read_report(self, timeout_ms: int) -> bytes | None:
        dev = self._require()
        try:
            r = dev.read(REPORT_SIZE, timeout_ms) if timeout_ms > 0 else dev.read(REPORT_SIZE)
        except (OSError, ValueError) as exc:
            self.close()
            raise NotConnected(f"HID read failed: {exc}") from exc
        if not r:
            return None
        return bytes(r)

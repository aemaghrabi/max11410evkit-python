"""Abstract report-level transport.

A transport moves raw 64-byte HID reports. It knows nothing about the EVKIT command
protocol; that lives in :mod:`max11410_evkit.board.evkit`.

Conventions:
* ``write_report`` takes the 64-byte report payload *without* a report-ID byte (the
  EVKIT uses no report IDs). Shorter payloads are zero-padded by the transport.
* ``read_report(timeout_ms)`` returns one input report as ``bytes`` or ``None`` if none
  arrived in time. ``timeout_ms == 0`` must be non-blocking (return immediately), so
  callers can drain the queue of idle filler reports.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

REPORT_SIZE = 64


class Transport(ABC):
    """One open (or openable) connection to a board."""

    @abstractmethod
    def open(self) -> None:
        """Open the device. Raises NotConnected if it cannot be opened."""

    @abstractmethod
    def close(self) -> None:
        """Close the device. Safe to call more than once."""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """True while the device handle is usable."""

    @abstractmethod
    def write_report(self, data: bytes) -> None:
        """Send one output report (<= 64 bytes, zero-padded to 64)."""

    @abstractmethod
    def read_report(self, timeout_ms: int) -> bytes | None:
        """Return one input report, or None if nothing arrived within timeout_ms."""

    @property
    def description(self) -> str:
        """Human-readable name for logs and GUIs."""
        return type(self).__name__

    def __enter__(self) -> Transport:
        if not self.is_open:
            self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

"""Report-level transports: real USB HID and an emulated board."""

from max11410_evkit.transport.base import REPORT_SIZE, Transport
from max11410_evkit.transport.errors import (
    CommandFailed,
    EvKitError,
    NotConnected,
    ProtocolError,
    Timeout,
)
from max11410_evkit.transport.fake import FakeEvKitTransport
from max11410_evkit.transport.hid import EVKIT_PID, EVKIT_VID, BoardInfo, HidTransport, list_boards

__all__ = [
    "EVKIT_PID",
    "EVKIT_VID",
    "REPORT_SIZE",
    "BoardInfo",
    "CommandFailed",
    "EvKitError",
    "FakeEvKitTransport",
    "HidTransport",
    "NotConnected",
    "ProtocolError",
    "Timeout",
    "Transport",
    "list_boards",
]

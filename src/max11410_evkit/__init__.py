"""Host software for the MAX11410EVKIT (MAXQ622 USB-HID bridge + MAX11410 24-bit ADC).

Layers:
    transport/  raw 64-byte HID reports (HidTransport on hidapi, FakeEvKitTransport for tests/demo)
    board/      the EVKIT firmware command protocol (EvKitBoard)
"""

from max11410_evkit.board.evkit import EvKitBoard, FirmwareVersion, connect
from max11410_evkit.transport.errors import (
    CommandFailed,
    EvKitError,
    NotConnected,
    ProtocolError,
    Timeout,
)

__version__ = "0.1.0"

__all__ = [
    "CommandFailed",
    "EvKitBoard",
    "EvKitError",
    "FirmwareVersion",
    "NotConnected",
    "ProtocolError",
    "Timeout",
    "__version__",
    "connect",
]

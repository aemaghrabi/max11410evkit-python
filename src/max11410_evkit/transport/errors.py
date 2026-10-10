"""Exception hierarchy shared by the transport and board layers."""

from __future__ import annotations


class EvKitError(Exception):
    """Base class for every error raised by max11410_evkit."""


class NotConnected(EvKitError):
    """No board is open, or the board was unplugged / the HID handle failed."""


class Timeout(EvKitError, TimeoutError):
    """The board did not answer within the allotted time."""


class ProtocolError(EvKitError):
    """A reply was malformed or did not match what the command should return."""


class CommandFailed(EvKitError):
    """The firmware answered with a status byte other than 0xEE (normally 0xFA)."""

    def __init__(self, command: int, status: int, data: bytes = b"", message: str | None = None):
        self.command = command
        self.status = status
        self.data = bytes(data)
        super().__init__(message or f"command 0x{command:02X} failed with status 0x{status:02X}")

"""EVKIT firmware command protocol (MAXQ622 firmware, ``main.c`` ProcessUSBCommand).

Framing
    host -> board  ``[N][cmd][args...]``, N = number of bytes after it (<= 63), zero padded
                   to a 64-byte output report.
    board -> host  ``[N][status][data...]``; status 0xEE = ok, 0xFA = failed. Exception:
                   0x43 (SRAM read) returns N raw bytes with no status byte.
    While idle the board answers every IN poll (~1 ms) with a filler report whose first
    byte is 0. Those are skipped. Before every command the input queue is drained so a
    late reply to an earlier, timed-out command cannot be mistaken for the new reply.

One command is in flight at a time (``threading.RLock``): the firmware handles commands
strictly sequentially and silently drops commands that arrive during a 0x42 capture.

SPI passthrough sends the address byte unchanged, so a register *read* needs bit 7 set
(``reg | 0x80``). :meth:`EvKitBoard.read_register`, :meth:`wait_drdy_read` and
:meth:`capture` add it; :meth:`spi_read` / :meth:`spi_write` are raw and do not.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from enum import IntEnum
from typing import NamedTuple

from max11410_evkit.transport.base import REPORT_SIZE, Transport
from max11410_evkit.transport.errors import (
    Cancelled,
    CommandFailed,
    NotConnected,
    ProtocolError,
    Timeout,
)

log = logging.getLogger("max11410_evkit.board")

STATUS_OK = 0xEE
STATUS_FAIL = 0xFA
READ_FLAG = 0x80

MAX_PAYLOAD = REPORT_SIZE - 1  # bytes after the count byte, both directions
MAX_SPI_BYTES = 40  # firmware spi_buf_size
MAX_SRAM_CHUNK = 63  # 0x43 reply is [N][N raw bytes]
MAX_ECHO_BYTES = MAX_PAYLOAD - 2  # [N][0x01][n][bytes...]
MAX_SAMPLES = 0xFFFF  # 0x42 sample count is 16-bit
EVKIT_SRAM_BYTES = 32768  # measured 2026-10-11 (scripts/sram_size_check.py): wraps at 0x8000
SPI_BASE_CLOCK_HZ = 6_000_000  # fSCLK = 6 MHz / (div + 1)
PROJECT_NAME = "MAX11410EVK"

REG_CONV_START = 0x01
REG_PART_ID = 0x11
REG_DATA0 = 0x30
REG_STATUS = 0x38


class Cmd(IntEnum):
    ECHO = 0x01
    VERSION = 0x02
    PROJECT = 0x03
    SPI_WRITE = 0x40
    SPI_READ = 0x41
    CAPTURE = 0x42
    SRAM_READ = 0x43
    START_SAMPLING = 0x45  # never replies - not exposed
    STOP_SAMPLING = 0x46  # never replies - not exposed
    READ_INT_CS_LOW = 0x47
    SET_CS = 0x48
    SET_SPI_CLOCK = 0x49
    READ_INT = 0x50
    WAIT_DRDY_READ = 0x51


def _cmd_name(cmd: int) -> str:
    try:
        return Cmd(cmd).name
    except ValueError:
        return f"CMD_{cmd:02X}"


class FirmwareVersion(NamedTuple):
    major: int
    minor: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}"


def register_width(reg: int) -> int | None:
    """Byte width of a MAX11410 register when it is known, else None.

    0x00-0x10 are 8-bit and 0x11-0x39 are 24-bit. The sequencer registers 0x3A-0x6F
    are believed to be 16-bit but that is not confirmed, so callers must pass a width.
    """
    if 0x00 <= reg <= 0x10:
        return 1
    if 0x11 <= reg <= 0x39:
        return 3
    return None


def spi_clock_hz(divider: int) -> float:
    """SPI clock produced by :meth:`EvKitBoard.set_spi_divider`."""
    return SPI_BASE_CLOCK_HZ / (divider + 1)


def samples_from_bytes(data: bytes, width: int = 3) -> list[int]:
    """Split captured SRAM bytes into big-endian unsigned samples."""
    if width <= 0 or len(data) % width:
        raise ValueError(f"{len(data)} bytes is not a multiple of width {width}")
    return [int.from_bytes(data[i : i + width], "big") for i in range(0, len(data), width)]


def _check_u8(name: str, value: int) -> int:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be 0..255, got {value}")
    return value


def _check_reg(reg: int) -> int:
    if not 0 <= reg <= 0x7F:
        raise ValueError(f"register address must be 0x00..0x7F, got 0x{reg:X}")
    return reg


class EvKitBoard:
    """Typed access to every EVKIT firmware command.

    Parameters
    ----------
    transport:
        An open (or openable) :class:`~max11410_evkit.transport.base.Transport`.
    timeout:
        Default reply timeout in seconds for ordinary commands.
    drdy_timeout:
        Host-side timeout for commands that make the firmware busy-wait for the ADC's
        data-ready edge (0x51, and the per-sample margin of 0x42). The firmware gives up
        after a few seconds and replies 0xFA, so this must comfortably exceed that.
    sram_bytes:
        Capture-buffer size used to validate 0x42 requests.
    on_event:
        Optional ``callable(str)`` receiving a one-line description of every command and
        reply, e.g. for a GUI status log. Exceptions it raises are swallowed.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        timeout: float = 1.0,
        drdy_timeout: float = 8.0,
        sram_bytes: int = EVKIT_SRAM_BYTES,
        on_event: Callable[[str], None] | None = None,
        poll_ms: int = 20,
        max_drain: int = 4096,
    ) -> None:
        self.transport = transport
        self.timeout = timeout
        self.drdy_timeout = drdy_timeout
        self.sram_bytes = sram_bytes
        self.on_event = on_event
        self.poll_ms = poll_ms
        self.max_drain = max_drain
        self._lock = threading.RLock()
        self._resync_needed = False

    # ------------------------------------------------------------------ lifecycle
    def open(self) -> None:
        with self._lock:
            if not self.transport.is_open:
                self.transport.open()
            self._emit(f"connected: {self.transport.description}")

    def close(self) -> None:
        with self._lock:
            was_open = self.transport.is_open
            self.transport.close()
            if was_open:
                self._emit("disconnected")

    @property
    def is_open(self) -> bool:
        return self.transport.is_open

    def __enter__(self) -> EvKitBoard:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ logging
    def _emit(self, msg: str) -> None:
        log.debug(msg)
        cb = self.on_event
        if cb is not None:
            try:
                cb(msg)
            except Exception:  # pragma: no cover - never let a GUI hook break comms
                log.exception("on_event callback raised")

    # ------------------------------------------------------------------ framing
    def _drain(self) -> int:
        """Discard everything already queued. Returns the number of non-filler reports."""
        stale = 0
        for _ in range(self.max_drain):
            r = self.transport.read_report(0)
            if r is None:
                break
            if r and r[0]:
                stale += 1
                log.warning("discarded stale reply: %s", r[: 1 + min(r[0], MAX_PAYLOAD)].hex(" "))
        return stale

    def raw_transact(
        self, payload: bytes, *, timeout: float | None = None, expect_reply: bool = True
    ) -> bytes:
        """Send ``[len(payload)][payload]`` and return the reply body (bytes after the count).

        With ``expect_reply=False`` nothing is awaited and ``b""`` is returned. This is the
        low-level escape hatch; prefer the typed methods.

        The protocol has no sequence numbers, so after a :class:`Timeout` the late reply
        may still arrive. The next command therefore first resynchronises: it sends an
        echo with a random nonce and discards everything up to the matching echo reply.
        """
        payload = bytes(payload)
        if not 1 <= len(payload) <= MAX_PAYLOAD:
            raise ValueError(f"payload must be 1..{MAX_PAYLOAD} bytes, got {len(payload)}")
        timeout = self.timeout if timeout is None else timeout
        with self._lock:
            if not self.transport.is_open:
                raise NotConnected("board is not connected")
            if self._resync_needed:
                self._resync()
            self._drain()
            self._send(payload)
            if not expect_reply:
                return b""
            try:
                return self._await_reply(payload[0], timeout)
            except Timeout:
                self._resync_needed = True
                raise

    def _send(self, payload: bytes) -> None:
        self._emit(f"-> {_cmd_name(payload[0])} {payload[1:].hex(' ')}".rstrip())
        self.transport.write_report(bytes([len(payload)]) + payload)

    def _await_reply(self, cmd: int, timeout: float) -> bytes:
        name = _cmd_name(cmd)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._emit(f"!! {name}: no reply within {timeout:g} s")
                raise Timeout(f"no reply to {name} (0x{cmd:02X}) within {timeout:g} s")
            r = self.transport.read_report(max(1, min(self.poll_ms, int(remaining * 1000) + 1)))
            if not r or r[0] == 0:
                continue  # nothing yet, or an idle filler report
            n = r[0]
            if n > MAX_PAYLOAD or n > len(r) - 1:
                raise ProtocolError(f"{name}: malformed reply {r[:8].hex(' ')}... (count {n})")
            body = r[1 : 1 + n]
            self._emit(f"<- {name} {body.hex(' ')}")
            return body

    def _resync(self) -> None:
        """Echo a nonce and drop every reply before its echo (late replies after a timeout)."""
        nonce = os.urandom(8)
        expected = bytes([STATUS_OK]) + nonce
        self._emit("resynchronising after a timeout")
        self._drain()
        self._send(bytes([Cmd.ECHO, len(nonce)]) + nonce)
        deadline = time.monotonic() + max(self.timeout, self.drdy_timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Timeout("board did not answer the resync echo (still busy?)")
            body = self._await_reply(Cmd.ECHO, remaining)
            if body == expected:
                self._resync_needed = False
                return
            log.warning("discarded late reply: %s", body.hex(" "))

    def _command(
        self, cmd: Cmd, args: bytes = b"", n_data: int = 0, *, timeout: float | None = None
    ) -> bytes:
        """Run a status-byte command and return ``n_data`` bytes after the 0xEE."""
        body = self.raw_transact(bytes([cmd]) + bytes(args), timeout=timeout)
        status = body[0] if body else -1
        if status == STATUS_OK:
            if len(body) < 1 + n_data:
                raise ProtocolError(
                    f"{cmd.name}: expected {n_data} data bytes, got {len(body) - 1}"
                )
            return body[1 : 1 + n_data]
        if status == STATUS_FAIL:
            self._emit(f"!! {cmd.name} failed (0xFA)")
            raise CommandFailed(int(cmd), status, body[1:])
        raise ProtocolError(f"{cmd.name}: unexpected status byte {body[:1].hex() or '<empty>'}")

    # ------------------------------------------------------------------ identity
    def echo(self, data: bytes) -> bytes:
        """0x01: the board returns ``data`` unchanged (<= 61 bytes)."""
        data = bytes(data)
        if len(data) > MAX_ECHO_BYTES:
            raise ValueError(f"echo supports at most {MAX_ECHO_BYTES} bytes")
        return self._command(Cmd.ECHO, bytes([len(data)]) + data, len(data))

    def version(self) -> FirmwareVersion:
        """0x02: firmware version (1.1 on the shipped firmware)."""
        major, minor = self._command(Cmd.VERSION, n_data=2)
        return FirmwareVersion(major, minor)

    def project(self) -> str:
        """0x03: project name ("MAX11410EVK")."""
        body = self.raw_transact(bytes([Cmd.PROJECT]))
        if not body or body[0] != STATUS_OK:
            raise ProtocolError(f"PROJECT: unexpected reply {body.hex(' ')}")
        if len(body) < 2 or len(body) < 2 + body[1]:
            raise ProtocolError(f"PROJECT: truncated reply {body.hex(' ')}")
        return body[2 : 2 + body[1]].decode("ascii", errors="replace")

    # ------------------------------------------------------------------ raw SPI
    def spi_write(self, first_byte: int, data: bytes) -> None:
        """0x40: CS low, send ``first_byte`` then ``data`` (<= 40 bytes), CS high. Raw."""
        data = bytes(data)
        _check_u8("first_byte", first_byte)
        if not 0 <= len(data) <= MAX_SPI_BYTES:
            raise ValueError(f"spi_write supports at most {MAX_SPI_BYTES} data bytes")
        self._command(Cmd.SPI_WRITE, bytes([first_byte, len(data)]) + data)

    def spi_read(self, first_byte: int, n: int) -> bytes:
        """0x41: CS low, send ``first_byte`` unchanged, clock in ``n`` bytes. Raw: the caller
        must set the 0x80 read flag itself (see :meth:`read_register`)."""
        _check_u8("first_byte", first_byte)
        if not 1 <= n <= MAX_SPI_BYTES:
            raise ValueError(f"spi_read n must be 1..{MAX_SPI_BYTES}")
        return self._command(Cmd.SPI_READ, bytes([first_byte, n]), n)

    # ------------------------------------------------------------------ registers
    @staticmethod
    def _width(reg: int, width: int | None) -> int:
        w = register_width(reg) if width is None else width
        if w is None:
            raise ValueError(f"width of register 0x{reg:02X} is not known; pass width=")
        if not 1 <= w <= MAX_SPI_BYTES:
            raise ValueError(f"width must be 1..{MAX_SPI_BYTES}")
        return w

    def read_register_bytes(self, reg: int, width: int | None = None) -> bytes:
        _check_reg(reg)
        return self.spi_read(reg | READ_FLAG, self._width(reg, width))

    def read_register(self, reg: int, width: int | None = None) -> int:
        """Read a MAX11410 register (adds the 0x80 read flag). Big-endian unsigned."""
        return int.from_bytes(self.read_register_bytes(reg, width), "big")

    def write_register(self, reg: int, value: int | bytes, width: int | None = None) -> None:
        """Write a MAX11410 register (read flag cleared). ``value`` may be an int (encoded
        big-endian in ``width`` bytes) or raw bytes (written as given)."""
        _check_reg(reg)
        if isinstance(value, int):
            w = self._width(reg, width)
            if not 0 <= value < (1 << (8 * w)):
                raise ValueError(f"value 0x{value:X} does not fit in {w} byte(s)")
            data = value.to_bytes(w, "big")
        else:
            data = bytes(value)
            if width is not None and len(data) != width:
                raise ValueError(f"got {len(data)} bytes for width {width}")
        self.spi_write(reg & ~READ_FLAG & 0xFF, data)

    # ------------------------------------------------------------------ conversions
    def wait_drdy_read(
        self,
        reg: int = REG_DATA0,
        width: int = 3,
        *,
        start: bool = False,
        conv: int = 0x00,
        timeout: float | None = None,
    ) -> bytes:
        """0x51: optionally write CONV_START (0x01) = ``conv``, wait on the board for the
        data-ready edge on DOUT/INTB, then read ``width`` bytes of ``reg``.

        Raises CommandFailed (0xFA) if the firmware's own wait times out (no conversion
        pending / interrupt not enabled).
        """
        _check_reg(reg)
        _check_u8("conv", conv)
        if not 1 <= width <= MAX_SPI_BYTES:
            raise ValueError(f"width must be 1..{MAX_SPI_BYTES}")
        args = bytes([reg | READ_FLAG, width, 1 if start else 0, conv])
        return self._command(
            Cmd.WAIT_DRDY_READ,
            args,
            width,
            timeout=self.drdy_timeout if timeout is None else timeout,
        )

    def capture(
        self,
        n_samples: int,
        *,
        timeout: float,
        reg: int = REG_DATA0,
        width: int = 3,
        conv: int = 0x01,
        restart_each: bool = False,
        readback: bool = True,
        progress: Callable[[int, int], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        """0x42 block capture into board SRAM, then (by default) read it back with 0x43.

        The firmware writes CONV_START = ``conv`` and then, for each sample, waits for
        data-ready and stores ``width`` bytes of ``reg`` at SRAM address 0, 1, ...
        ``restart_each=True`` is single-conversion mode: after each data-ready it re-writes
        CONV_START (starting the next conversion) and then reads the previous result.
        Use ``conv=0x01`` (continuous) with ``restart_each=False`` and a single-conversion
        ``conv`` (e.g. 0x00) with ``restart_each=True``.

        ``timeout`` is the caller's estimate of how long the acquisition takes (seconds).
        The host waits ``1.5 * timeout + drdy_timeout`` for the firmware's reply. While a
        capture runs the firmware drops any other command, so the board lock is held for
        the whole capture + readback.

        Note: in continuous mode the ADC keeps converting afterwards; stop it yourself
        (e.g. write CONV_START) if needed.

        ``cancel`` is polled before the capture and between SRAM read chunks (the firmware
        cannot be interrupted during the 0x42 itself); when it returns True, :class:`Cancelled`
        is raised and the block is discarded.

        Returns the raw bytes (``n_samples * width``), or ``b""`` if ``readback`` is False.
        """
        if not 1 <= n_samples <= MAX_SAMPLES:
            raise ValueError(f"n_samples must be 1..{MAX_SAMPLES}")
        if not 1 <= width <= MAX_SPI_BYTES:
            raise ValueError(f"width must be 1..{MAX_SPI_BYTES}")
        _check_reg(reg)
        _check_u8("conv", conv)
        total = n_samples * width
        if total > self.sram_bytes:
            raise ValueError(
                f"{n_samples} samples x {width} B = {total} B exceeds the "
                f"{self.sram_bytes} B board SRAM (max {self.sram_bytes // width} samples)"
            )
        if timeout < 0:
            raise ValueError("timeout must be >= 0")
        host_timeout = 1.5 * timeout + self.drdy_timeout
        args = bytes(
            [
                reg | READ_FLAG,
                width,
                n_samples >> 8,
                n_samples & 0xFF,
                conv,
                1 if restart_each else 0,
            ]
        )
        with self._lock:
            if cancel is not None and cancel():
                raise Cancelled("capture cancelled before it started")
            self._command(Cmd.CAPTURE, args, timeout=host_timeout)
            if not readback:
                return b""
            return self.sram_read(0, total, progress=progress, cancel=cancel)

    def sram_read(
        self,
        address: int,
        n: int,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        """0x43: read ``n`` bytes of board SRAM starting at ``address`` (63-byte chunks).

        ``cancel`` is polled before each chunk; when it returns True, :class:`Cancelled` is
        raised (nothing on the board needs undoing: SRAM reads have no side effects).
        """
        if n <= 0:
            return b""
        if address < 0 or address + n > self.sram_bytes or address + n > 0x10000:
            raise ValueError(f"SRAM range 0x{address:X}+{n} is outside {self.sram_bytes} B")
        out = bytearray()
        with self._lock:
            while len(out) < n:
                if cancel is not None and cancel():
                    raise Cancelled(f"SRAM read cancelled after {len(out)} of {n} B")
                a = address + len(out)
                k = min(MAX_SRAM_CHUNK, n - len(out))  # never 0: the firmware would not reply
                chunk = self.raw_transact(bytes([Cmd.SRAM_READ, a >> 8, a & 0xFF, k]))
                if len(chunk) != k:
                    raise ProtocolError(f"SRAM_READ at 0x{a:04X}: asked {k} B, got {len(chunk)}")
                out += chunk
                if progress is not None:
                    progress(len(out), n)
        return bytes(out)

    # ------------------------------------------------------------------ pins / clock
    def set_cs(self, level: bool | int) -> None:
        """0x48: drive the ADC chip-select high (True) or low (False)."""
        self._command(Cmd.SET_CS, bytes([1 if level else 0]))

    def set_spi_divider(self, divider: int) -> float:
        """0x49: set the SPI clock divider; returns the resulting fSCLK in Hz."""
        _check_u8("divider", divider)
        self._command(Cmd.SET_SPI_CLOCK, bytes([divider]))
        return spi_clock_hz(divider)

    def read_int(self, *, cs_low: bool = False) -> int:
        """Read the INTB/DOUT pin level (0x50, or 0x47 which pulls CS low around the read)."""
        cmd = Cmd.READ_INT_CS_LOW if cs_low else Cmd.READ_INT
        return self._command(cmd, n_data=1)[0]


def connect(
    path: bytes | str | None = None,
    *,
    serial: str | None = None,
    transport: Transport | None = None,
    verify: bool = True,
    **board_kwargs: object,
) -> EvKitBoard:
    """Open the first (or the given) EVKIT and check that it identifies as MAX11410EVK.

    Pass ``transport=`` to use something other than USB (e.g. the fake board).
    """
    if transport is None:
        from max11410_evkit.transport.hid import HidTransport

        transport = HidTransport(path, serial=serial)
    board = EvKitBoard(transport, **board_kwargs)  # type: ignore[arg-type]
    board.open()
    if verify:
        try:
            name = board.project()
        except Exception:
            board.close()
            raise
        if name != PROJECT_NAME:
            board.close()
            raise ProtocolError(f"unexpected project name {name!r} (expected {PROJECT_NAME!r})")
    return board

"""A software EVKIT: emulates the MAXQ622 firmware (main.c) plus a toy MAX11410.

Used by the test-suite and by ``max11410-evkit --fake`` (and any GUI demo mode). It is a
:class:`Transport`, so it plugs in below :class:`~max11410_evkit.board.evkit.EvKitBoard`
exactly where :class:`HidTransport` does.

What is modelled (from main.c):
* count-prefixed framing; idle 3-byte filler reports whose first byte is 0, also
  interleaved around replies;
* commands are processed one at a time by a firmware thread; commands sent while a
  0x42 capture runs are dropped (service_irqs overwrites the buffer and the flag is reset);
  commands sent during other commands wait (USB NAK);
* 0x51 / 0x42 wait for a data-ready *edge*: they fail (0xFA, after ``drdy_timeout_s``)
  when no conversion is pending;
* 0x42 SRAM capture from address 0, restart_each order = wait, write CONV_START, read
  previous result; continuous mode just waits and reads;
* 0x43 replies with N raw bytes and no status; N=0 gives no reply;
* 0x45, 0x46 and unknown commands never reply; 0x51 failure still sends N+1 bytes.

The toy ADC: register widths 0x00-0x10 = 1 B, 0x11-0x39 = 3 B, 0x3A-0x6F = 2 B (assumed);
SPI first byte bit 7 = read; a "read" without the flag is a write of the dummy 0x00 bytes
(as on the real part); CONV_START (0x01) bits[2:0] = 1 -> continuous, else single;
CAL_START (0x03) sets CAL_READY; STATUS (0x38) bit0 CONV_READY and bit2 CAL_READY clear on
read; DATA0 (0x30) sample = 0x800000 + (AINP << 16) + index with AINP = MUX_CTRL0(0x0B)>>4.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque

from max11410_evkit.transport.base import REPORT_SIZE, Transport
from max11410_evkit.transport.errors import NotConnected

OK, FAIL = 0xEE, 0xFA
FILLER = b"\x00\x00\x00"
PART_ID = 0x000F02
PROJECT = b"MAX11410EVK"
FW_VERSION = (1, 1)

REG_CONV_START, REG_CAL_START, REG_MUX_CTRL0 = 0x01, 0x03, 0x0B
REG_PART_ID, REG_DATA0, REG_STATUS = 0x11, 0x30, 0x38
CONV_READY, CAL_READY = 0x01, 0x04


def reg_width(reg: int) -> int:
    if reg <= 0x10:
        return 1
    if reg <= 0x39:
        return 3
    return 2


class FakeAdc:
    """Minimal MAX11410 register model."""

    def __init__(self, conversion_time_s: float = 0.0) -> None:
        self.conversion_time_s = conversion_time_s
        self.regs = [0] * 0x80
        self.regs[REG_PART_ID] = PART_ID
        self.continuous = False
        self.pending = False  # a single conversion is in progress
        self.started_at = 0.0
        self.index = 0
        self.spi_log: list[tuple[int, bytes, bytes]] = []  # (first byte, MOSI data, MISO data)

    # conversions -------------------------------------------------------
    def _produce(self) -> None:
        ainp = self.regs[REG_MUX_CTRL0] >> 4
        self.regs[REG_DATA0] = (0x800000 + (ainp << 16) + self.index) & 0xFFFFFF
        self.index += 1
        self.regs[REG_STATUS] |= CONV_READY

    def _settle(self) -> None:
        """Complete any conversion whose time has elapsed (used on STATUS polling)."""
        if (self.pending or self.continuous) and (
            time.monotonic() - self.started_at >= self.conversion_time_s
        ):
            self._produce()
            self.pending = False
            self.started_at = time.monotonic()

    def wait_drdy(self, give_up_s: float) -> bool:
        """Firmware WaitForDataReady: True on a new data-ready edge, else False after give_up_s."""
        if self.pending or self.continuous:
            remaining = self.conversion_time_s - (time.monotonic() - self.started_at)
            if remaining > 0:
                time.sleep(remaining)
            self._produce()
            self.pending = False
            self.started_at = time.monotonic()
            return True
        if give_up_s > 0:
            time.sleep(give_up_s)
        return False

    @property
    def intb(self) -> int:
        return 0 if self.regs[REG_STATUS] & CONV_READY else 1

    # SPI -----------------------------------------------------------------
    def _write(self, reg: int, data: bytes) -> None:
        w = reg_width(reg)
        if reg == REG_PART_ID or not data:
            return
        self.regs[reg] = int.from_bytes(data[:w].ljust(w, b"\x00"), "big")
        if reg == REG_CONV_START:
            self.continuous = (self.regs[reg] & 0x07) == 1
            self.pending = not self.continuous
            self.started_at = time.monotonic()
        elif reg == REG_CAL_START:
            self.regs[REG_STATUS] |= CAL_READY

    def _read(self, reg: int, n: int) -> bytes:
        w = reg_width(reg)
        if reg == REG_STATUS:
            self._settle()
        v = self.regs[reg]
        if reg == REG_STATUS:
            self.regs[REG_STATUS] &= ~(CONV_READY | CAL_READY)
        return v.to_bytes(w, "big")[:n].ljust(n, b"\x00")

    def transfer_write(self, first: int, data: bytes) -> None:
        """SpiWrite: first byte then data."""
        if first & 0x80:
            miso = self._read(first & 0x7F, len(data))
        else:
            self._write(first & 0x7F, data)
            miso = b"\xff" * len(data)
        self.spi_log.append((first, bytes(data), miso))

    def transfer_read(self, first: int, n: int) -> bytes:
        """SpiRead: first byte then n dummy 0x00 bytes."""
        if first & 0x80:
            miso = self._read(first & 0x7F, n)
        else:  # no read flag: the part sees a write of the dummy zeros
            self._write(first & 0x7F, b"\x00" * n)
            miso = b"\xff" * n  # DOUT pulled up
        self.spi_log.append((first, b"\x00" * n, miso))
        return miso


class FakeEvKitTransport(Transport):
    """Emulated EVKIT behind the Transport interface.

    ``conversion_time_s``: per-conversion time of the toy ADC (0 = instant).
    ``drdy_timeout_s``: how long the firmware's data-ready wait lasts before 0xFA
    (the real firmware takes a few seconds; tests keep it short).
    """

    def __init__(
        self,
        *,
        conversion_time_s: float = 0.0,
        drdy_timeout_s: float = 0.05,
        sram_bytes: int = 32768,
        command_latency_s: float = 0.0,
    ) -> None:
        self.adc = FakeAdc(conversion_time_s)
        self.drdy_timeout_s = drdy_timeout_s
        self.command_latency_s = command_latency_s
        self.sram = bytearray(sram_bytes)
        self.cs_level = 1
        self.spi_divider = 0
        self.received: list[bytes] = []  # every command payload accepted by the firmware
        self.dropped: list[bytes] = []  # commands lost because a capture was running
        self.mute = False  # when True the firmware processes commands but sends no replies
        self._out: deque[bytes] = deque()
        self._cv = threading.Condition()
        self._inbox: queue.Queue[bytes | None] = queue.Queue()
        self._capturing = False
        self._processing = False
        self._open = False
        self._unplugged = False
        self._thread: threading.Thread | None = None

    # Transport -------------------------------------------------------------
    @property
    def description(self) -> str:
        return "fake MAX11410EVKIT"

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        if self._unplugged:
            raise NotConnected("fake board is unplugged")
        if self._open:
            return
        self._open = True
        self._thread = threading.Thread(target=self._firmware, name="fake-evkit", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if not self._open:
            return
        self._open = False
        self._inbox.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        with self._cv:
            self._out.clear()

    def write_report(self, data: bytes) -> None:
        self._check()
        if len(data) > REPORT_SIZE:
            raise ValueError("report too long")
        data = bytes(data).ljust(REPORT_SIZE, b"\x00")
        n = data[0]
        if n == 0 or n > REPORT_SIZE - 1:
            return  # read_OUT1 would read garbage; the firmware ignores it in practice
        payload = data[1 : 1 + n]
        if self._capturing:
            self.dropped.append(payload)
            return
        self._inbox.put(payload)

    def read_report(self, timeout_ms: int) -> bytes | None:
        self._check()
        with self._cv:
            if not self._out and timeout_ms > 0:
                self._cv.wait(min(timeout_ms, 1) / 1000)
                if not self._out:
                    self._check()
                    # The real board answers every 1 ms IN poll with a filler.
                    return FILLER
            if self._out:
                return self._out.popleft()
        return None

    # test hooks -------------------------------------------------------------
    def inject_report(self, report: bytes) -> None:
        """Queue an arbitrary input report (e.g. a stale reply from an earlier command)."""
        with self._cv:
            self._out.append(bytes(report).ljust(REPORT_SIZE, b"\x00"))
            self._cv.notify_all()

    def disconnect(self) -> None:
        """Simulate unplugging the board: every later read/write raises NotConnected."""
        self._unplugged = True
        self.close()

    @property
    def busy(self) -> bool:
        return self._processing or not self._inbox.empty()

    def wait_idle(self, timeout: float = 5.0) -> None:
        end = time.monotonic() + timeout
        while self.busy and time.monotonic() < end:
            time.sleep(0.001)

    # internals ---------------------------------------------------------------
    def _check(self) -> None:
        if self._unplugged or not self._open:
            raise NotConnected("fake board is not connected")

    def _reply(self, body: bytes) -> None:
        """do_IN3: [count][body], armed as a 64-byte packet, with fillers around it."""
        if self.mute:
            return
        report = (bytes([len(body)]) + body).ljust(REPORT_SIZE, b"\x00")
        with self._cv:
            self._out.extend([FILLER, report, FILLER])
            self._cv.notify_all()

    def _firmware(self) -> None:
        while True:
            p = self._inbox.get()
            if p is None or not self._open:
                return
            self._processing = True
            try:
                self.received.append(p)
                if self.command_latency_s:
                    time.sleep(self.command_latency_s)
                body = self._process(p)
                if body is not None:
                    self._reply(body)
            finally:
                self._processing = False

    def _process(self, p: bytes) -> bytes | None:  # noqa: C901 - mirrors the firmware switch
        p = p.ljust(REPORT_SIZE, b"\x00")
        cmd, adc = p[0], self.adc
        if cmd == 0x01:  # TEST_COMMAND (echo)
            n = p[1]
            return bytes([OK]) + p[2 : 2 + n]
        if cmd == 0x02:
            return bytes([OK, *FW_VERSION])
        if cmd == 0x03:
            return bytes([OK, len(PROJECT)]) + PROJECT
        if cmd == 0x49:
            self.spi_divider = p[1]
            return bytes([OK])
        if cmd == 0x41:
            n = p[2]
            return bytes([OK]) + adc.transfer_read(p[1], n)
        if cmd == 0x40:
            n = p[2]
            adc.transfer_write(p[1], p[3 : 3 + n])
            return bytes([OK])
        if cmd in (0x45, 0x46):
            return None  # never replies
        if cmd == 0x47:
            return bytes([OK, adc.intb])
        if cmd == 0x50:
            return bytes([OK, adc.intb])
        if cmd == 0x48:
            self.cs_level = 1 if p[1] else 0
            return bytes([OK])
        if cmd == 0x51:
            first, n, start, conv = p[1], p[2], p[3], p[4]
            if start:
                adc.transfer_write(REG_CONV_START, bytes([conv]))
            if adc.wait_drdy(self.drdy_timeout_s):
                return bytes([OK]) + adc.transfer_read(first, n)
            self.cs_level = 1
            return bytes([FAIL]) + bytes(n)  # firmware still sends n+1 bytes
        if cmd == 0x42:
            return self._capture(p)
        if cmd == 0x43:
            ptr, n = (p[1] << 8) | p[2], p[3]
            if n == 0:
                return None  # bytes_to_send = 0 -> nothing is sent
            size = len(self.sram)
            return bytes(self.sram[(ptr + i) % size] for i in range(n))
        if cmd == 0x52:  # TEST_AZ08_WAKE_UP; falls through to default with bytes_to_send = 1
            first, n = p[1], p[2]
            adc.transfer_write(first, p[3 : 3 + n])
            adc.transfer_write(0x0E, bytes([0x10]) * n)
            adc.transfer_read(first | 0x80, n)
            return bytes([OK])
        return None  # default: no reply

    def _capture(self, p: bytes) -> bytes:
        first, n, samples, conv, restart = p[1], p[2], (p[3] << 8) | p[4], p[5], p[6]
        adc, size = self.adc, len(self.sram)
        self._capturing = True
        try:
            cntr = 0
            adc.transfer_write(REG_CONV_START, bytes([conv]))
            for _ in range(samples):
                if not adc.wait_drdy(self.drdy_timeout_s):
                    self.cs_level = 1
                    return bytes([FAIL])
                if restart:  # start next conversion, then read the previous result
                    adc.transfer_write(REG_CONV_START, bytes([conv]))
                for b in adc.transfer_read(first, n):
                    self.sram[cntr % size] = b
                    cntr += 1
            return bytes([OK])
        finally:
            self._capturing = False

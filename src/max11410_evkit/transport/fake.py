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

The toy ADC (datasheet behaviour): register widths, access and reset values come from
device/regmap.yaml; SPI first byte bit 7 = R/WB (1 = read); a "read" without the flag is a
write of the dummy 0x00 bytes (as on the real part). CONV_START: DEST[6:4] selects DATA0-7,
CONV_TYPE[1:0] 0 = single, 1-3 = continuous/duty-cycled; writing it clears CONV_RDY. Start
commands wake the part (PD -> 0); PD = RESET restores POR values and leaves standby.
CAL_START sets CAL_RDY. Reading STATUS clears every flag except DATA_RDY, which clears when the
unread DATA registers are read. INTB = STATUS & STATUS_IE (reset: CONV_RDY only).
Sample = 0x800000 + (AINP << 16) + index (AINP 11-15 = unconnected count as 0), unless
``adc.inputs`` gives a voltage for AINP: then the code follows MUX_CTRL0, CTRL (REF_SEL, U_BN,
FORMAT: offset binary / two's complement), PGA (analog gain, or digital gain <= 4x on the
buffered/bypass paths) and ``adc.refs`` / ``adc.avdd`` (datasheet Table 8 transfer function).
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

REG_PD, REG_CONV_START, REG_SEQ_START, REG_CAL_START = 0x00, 0x01, 0x02, 0x03
REG_CTRL, REG_MUX_CTRL0, REG_PGA, REG_WAIT_START = 0x09, 0x0B, 0x0E, 0x10
REG_PART_ID, REG_DATA0, REG_STATUS, REG_STATUS_IE = 0x11, 0x30, 0x38, 0x39
CONV_RDY, CAL_RDY, DATA_RDY = 0x01, 0x04, 0x10
CONV_READY, CAL_READY = CONV_RDY, CAL_RDY  # Phase 2 names


def reg_width(reg: int) -> int:
    from max11410_evkit.device.regmap import load_regmap

    spec = load_regmap()
    return spec[reg].width if reg in spec else 1


class FakeAdc:
    """Minimal MAX11410 register model (datasheet behaviour where it matters to the host)."""

    def __init__(self, conversion_time_s: float = 0.0) -> None:
        from max11410_evkit.device.regmap import load_regmap

        self.spec = load_regmap()
        self.conversion_time_s = conversion_time_s
        self.regs = [0] * 0x80
        self._readonly: set[int] = set()
        for r in self.spec:
            if r.reset is not None:
                self.regs[r.addr] = r.reset
            if not r.writable:
                self._readonly.add(r.addr)
        self.regs[REG_PART_ID] = PART_ID
        self.continuous = False
        self.pending = False  # a single conversion is in progress
        self.dest = 0  # CONV_START.DEST of the running conversion
        self.unread: set[int] = set()  # DATA registers holding unread results (DATA_RDY)
        self.started_at = 0.0
        self.index = 0
        self.spi_log: list[tuple[int, bytes, bytes]] = []  # (first byte, MOSI data, MISO data)
        # Optional analog model: volts on AIN0..AIN9 (unset inputs -> legacy test pattern).
        self.inputs: dict[int, float] = {}
        self.avdd = 3.3
        self.refs = {0: 2.5, 1: 2.5, 2: 2.5}  # REFnP - REFnN in volts
        self.cal_log: list[tuple[int, int]] = []  # (CAL_START value, PGA register at the time)

    def _vref(self) -> float:
        refsel = self.regs[REG_CTRL] & 0x07
        if refsel in (3, 7):
            return self.avdd
        return self.refs.get(refsel % 4, 2.5)

    def _analog_code(self) -> int | None:
        mux = self.regs[REG_MUX_CTRL0]
        ainp, ainn = mux >> 4, mux & 0x0F
        if ainp not in self.inputs and ainp != 10:
            return None
        vp = self.avdd if ainp == 10 else self.inputs.get(ainp, 0.0)
        vn = 0.0 if ainn >= 10 else self.inputs.get(ainn, 0.0)
        pga = self.regs[REG_PGA]
        gain = 1 << (pga & 0x07)
        if (pga >> 4) & 0x03 != 2:  # buffered/bypass: digital gain saturating at 4x
            gain = min(gain, 4)
        vref = self._vref()
        ctrl = self.regs[REG_CTRL]
        if ctrl & 0x40:  # U_BN = 1: unipolar, straight binary
            code = round((vp - vn) * ((1 << 24) - 1) * gain / vref)
            return min(max(code, 0), (1 << 24) - 1)
        code = round((vp - vn) * ((1 << 23) - 1) * gain / vref)
        code = min(max(code, -(1 << 23)), (1 << 23) - 1)
        if ctrl & 0x20:  # FORMAT = 1: offset binary
            return code + (1 << 23)
        return code & 0xFFFFFF  # two's complement

    # conversions -------------------------------------------------------
    def _produce(self) -> None:
        code = self._analog_code()
        if code is None:
            ainp = self.regs[REG_MUX_CTRL0] >> 4
            ainp = ainp if ainp <= 10 else 0  # 11..15 = unconnected
            code = (0x800000 + (ainp << 16) + self.index) & 0xFFFFFF
        self.regs[REG_DATA0 + self.dest] = code
        self.unread.add(self.dest)
        self.index += 1
        self.regs[REG_STATUS] |= CONV_RDY | DATA_RDY

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
        """INTB is low while an enabled STATUS bit is set (STATUS & STATUS_IE)."""
        return 0 if self.regs[REG_STATUS] & self.regs[REG_STATUS_IE] else 1

    # SPI -----------------------------------------------------------------
    def _write(self, reg: int, data: bytes) -> None:
        w = reg_width(reg)
        if reg in self._readonly or not data:
            return
        self.regs[reg] = int.from_bytes(data[:w].ljust(w, b"\x00"), "big")
        if reg in (REG_CONV_START, REG_CAL_START, REG_SEQ_START, REG_WAIT_START):
            self.regs[REG_PD] = 0  # an asynchronous start command wakes the part
        if reg == REG_PD and self.regs[reg] & 0x03 == 3:  # RESET: POR state, then standby
            self.__init__(self.conversion_time_s)
            self.regs[REG_PD] = 0x01
        elif reg == REG_CONV_START:
            v = self.regs[reg]
            self.dest = (v >> 4) & 0x07
            self.continuous = (v & 0x03) != 0  # continuous or duty-cycled
            self.pending = not self.continuous
            self.regs[REG_STATUS] &= ~CONV_RDY
            self.started_at = time.monotonic()
        elif reg == REG_CAL_START:
            self.cal_log.append((self.regs[reg], self.regs[REG_PGA]))
            self.regs[REG_STATUS] |= CAL_RDY

    def _read(self, reg: int, n: int) -> bytes:
        w = reg_width(reg)
        if reg == REG_STATUS:
            self._settle()
        v = self.regs[reg]
        if reg == REG_STATUS:
            self.regs[REG_STATUS] &= DATA_RDY  # everything but DATA_RDY clears on read
        elif REG_DATA0 <= reg <= REG_DATA0 + 7:
            self.unread.discard(reg - REG_DATA0)
            if not self.unread:
                self.regs[REG_STATUS] &= ~DATA_RDY
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

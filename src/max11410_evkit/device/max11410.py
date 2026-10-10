"""MAX11410 device layer: identification, start-up, channels, conversions, calibration.

Behaviour follows the MAX11410 datasheet (rev 3) and the Linux IIO driver:

* configure channel (driver order): MUX_CTRL0 = AINP_SEL<<4 | AINN_SEL; CTRL REF_SEL /
  REFBUFP_EN / REFBUFN_EN / U_BN with FORMAT = 1; PGA = SIG_PATH<<4 | GAIN; FILTER LINEF/RATE;
  then wait ``settling_us``;
* single conversion on channel slot *n*: CONV_START = DEST n | SINGLE, wait for the data-ready
  interrupt (board command 0x51; STATUS_IE resets to CONV_RDY only) or poll STATUS.CONV_RDY,
  then read DATA<n>;
* continuous: CONV_START = DEST | CONTINUOUS; stop by writing CONV_START = SINGLE (driver);
* self-calibration: CAL_START = SELF (updates SELF_OFF, SELF_GAIN_1), then SIG_PATH = PGA and
  CAL_START = PGA for each gain 2..128 (SELF_GAIN_x). Each calibration runs two conversions at
  the current LINEF/RATE; the datasheet recommends calibrating no faster than the rate used for
  measurements. FILTER and PGA are restored afterwards.

Data format (datasheet Table 8). U_BN = 1: unipolar, straight binary, 0 .. +Vref.
U_BN = 0: bipolar, -Vref .. +Vref, offset binary when FORMAT = 1 (set by :meth:`MAX11410.init`)
or two's complement when FORMAT = 0 (the reset value). With gain G:
    unipolar  V = raw * Vref / ((2^24 - 1) * G)
    bipolar   V = (raw - 2^23) * Vref / ((2^23 - 1) * G)        (offset binary)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from typing import Literal

from max11410_evkit.board.evkit import EvKitBoard, samples_from_bytes
from max11410_evkit.device.regmap import RegisterMap, RegisterMapSpec
from max11410_evkit.transport.errors import CommandFailed, EvKitError, Timeout

log = logging.getLogger("max11410_evkit.device")

PART_ID = 0x000F02
NUM_CHANNELS = 8  # DATA0..DATA7 (CONV_START.DEST)
FULL_SCALE = 1 << 24
MID_SCALE = 1 << 23

GAINS = (1, 2, 4, 8, 16, 32, 64, 128)
DIGITAL_GAIN_MAX = 4  # buffered/bypass paths: GAIN is digital and saturates at 4x
SIG_PATHS = {"buffered": 0, "bypass": 1, "pga": 2}
LINEF = ("FIR5060", "FIR50", "FIR60", "SINC4")
REFSEL_NAMES = (
    "REF0", "REF1", "REF2", "AVDD", "REF0P_AGND", "REF1P_AGND", "REF2P_AGND", "AVDD_ALT",
)  # fmt: skip
INPUT_AVDD = 10  # AINP_SEL = 10 -> AVDD
INPUT_GND = 10  # AINN_SEL = 10 -> GND
INPUT_NC = 15  # 11..15 -> unconnected (reset value)
FILTER_RATE_MAX = 0x0F
GPIO_INTRB = 0xC1  # GPx_DIR = CMOS output, GPx_OSEL = INTRB
CONV_SINGLE, CONV_CONTINUOUS, CONV_DUTY_CYCLE = 0, 1, 2
CLOCK_HZ = 2_457_600  # nominal internal clock (datasheet quotes 2.456 / 2.4576 MHz)

RateMode = Literal["continuous", "single", "duty"]

# Datasheet Tables 9a-9d: samples/s per LINEF and RATE index for single-cycle, continuous and
# 1:4 duty-cycled conversions. RATE values past the end of a table repeat the last entry
# (e.g. FIR5060 0100-1111 = 16 sps single / 16.8 sps continuous).
RATE_TABLE_MODES: dict[str, dict[str, tuple[float, ...]]] = {
    "FIR5060": {
        "single": (1.0, 2.0, 4.0, 8.0, 16.0),
        "continuous": (1.1, 2.1, 4.2, 8.4, 16.8),
        "duty": (0.3, 0.5, 1.1, 2.1, 4.2),
    },
    "FIR50": {
        "single": (1.3, 2.5, 5.0, 10.0, 20.0, 35.6),
        "continuous": (1.3, 2.7, 5.3, 10.7, 21.3, 40.0),
        "duty": (0.3, 0.7, 1.3, 2.7, 5.3, 10.0),
    },
    "FIR60": {
        "single": (1.3, 2.5, 5.0, 10.0, 20.0, 35.6),
        "continuous": (1.3, 2.7, 5.3, 10.7, 21.3, 40.0),
        "duty": (0.3, 0.7, 1.3, 2.7, 5.3, 10.0),
    },
    "SINC4": {
        "single": (1, 2.5, 5, 10, 15, 30, 60, 120, 240, 480),
        "continuous": (4, 10, 20, 40, 60, 120, 240, 480, 960, 1920),
        "duty": (1, 2.5, 5, 10, 15, 30, 60, 120, 240, 480),
    },
}
# Continuous-mode tables (kept for callers of the Phase 2 API).
RATE_TABLES: dict[str, tuple[float, ...]] = {
    k: v["continuous"] for k, v in RATE_TABLE_MODES.items()
}
RATE_TABLES_CONFIRMED = True  # datasheet Tables 9a-9d
# In continuous SINC4 mode the first result arrives after 4 output periods (filter settling).
SINC4_CONTINUOUS_SETTLING_PERIODS = 4


def rate_index_count(linef: str) -> int:
    """Number of distinct RATE settings for ``linef`` (higher indices repeat the last one)."""
    return len(RATE_TABLES[linef.upper()])


def data_rate(
    linef: str, rate: int, *, continuous: bool = True, mode: RateMode | None = None
) -> float:
    """Output data rate in samples/s (datasheet Tables 9a-9d).

    ``mode`` is "continuous", "single" (single-cycle) or "duty" (1:4 duty-cycled); when it is
    omitted ``continuous`` selects continuous vs single-cycle.
    """
    m = mode or ("continuous" if continuous else "single")
    table = RATE_TABLE_MODES[linef.upper()][m]
    return float(table[min(max(rate, 0), len(table) - 1)])


def rate_index_for(linef: str, hz: float, *, mode: RateMode = "continuous") -> int:
    """Index of the table entry closest to ``hz``."""
    table = RATE_TABLE_MODES[linef.upper()][mode]
    return min(range(len(table)), key=lambda i: abs(table[i] - hz))


def wait_seconds(wait: int, wait_ext: int = 0, *, clock_hz: float = CLOCK_HZ) -> float:
    """Duration of a WAIT_START command: (WAIT_EXT*16)*(WAIT*16) clocks, or WAIT*16 if
    WAIT_EXT = 0 (datasheet p.52)."""
    if not (0 <= wait <= 0xFF and 0 <= wait_ext <= 0xFF):
        raise ValueError("wait and wait_ext are 8-bit values")
    clocks = wait * 16 * (wait_ext * 16 if wait_ext else 1)
    return clocks / clock_hz


class DeviceError(EvKitError):
    """The ADC is not answering as a MAX11410 should."""


@dataclass
class ChannelConfig:
    """Everything that defines one logical measurement channel."""

    name: str = "CH0"
    enabled: bool = True
    ainp: int = 2
    ainn: int = INPUT_GND
    refsel: int = 1  # REF1P/REF1N: EVKIT MAX6072 2.5 V via JP7 (CTRL reset value)
    vref_volts: float = 2.5
    vrefp_buf: bool = False
    vrefn_buf: bool = False
    path: Literal["buffered", "bypass", "pga"] = "buffered"
    gain: int = 1
    bipolar: bool = False
    linef: Literal["FIR5060", "FIR50", "FIR60", "SINC4"] = "SINC4"
    rate: int = 4  # SINC4 index 4 = 60 sps continuous / 15 sps single-cycle (Windows GUI default)
    settling_us: int = 0

    def validate(self) -> ChannelConfig:
        if not (0 <= self.ainp <= 10 or self.ainp == INPUT_NC):
            raise ValueError("ainp must be 0..9 (AINx), 10 (AVDD) or 15 (unconnected)")
        if not (0 <= self.ainn <= 10 or self.ainn == INPUT_NC):
            raise ValueError("ainn must be 0..9 (AINx), 10 (GND) or 15 (unconnected)")
        if not 0 <= self.refsel <= 7:
            raise ValueError("refsel must be 0..7")
        if self.path not in SIG_PATHS:
            raise ValueError(f"path must be one of {', '.join(SIG_PATHS)}")
        if self.gain not in GAINS:
            raise ValueError(f"gain must be one of {GAINS}")
        if self.path != "pga" and self.gain > DIGITAL_GAIN_MAX:
            raise ValueError(
                f"the {self.path} path only has digital gain 1, 2 or 4; use path='pga' for "
                f"gain {self.gain}"
            )
        if self.linef not in LINEF:
            raise ValueError(f"linef must be one of {LINEF}")
        if not 0 <= self.rate <= FILTER_RATE_MAX:
            raise ValueError("rate index must be 0..15")
        if self.vref_volts <= 0:
            raise ValueError("vref_volts must be > 0")
        if self.settling_us < 0:
            raise ValueError("settling_us must be >= 0")
        return self

    @property
    def gain_code(self) -> int:
        return GAINS.index(self.gain)

    @property
    def lsb_volts(self) -> float:
        return lsb_volts(self)

    def data_rate(self, *, continuous: bool = True, mode: RateMode | None = None) -> float:
        return data_rate(self.linef, self.rate, continuous=continuous, mode=mode)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> ChannelConfig:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


EVKIT_DEFAULT_CHANNEL = ChannelConfig()


def _signed24(raw: int) -> int:
    return raw - FULL_SCALE if raw & MID_SCALE else raw


def raw_to_volts(raw: int, cfg: ChannelConfig, *, offset_binary: bool = True) -> float:
    """Conversion code -> input voltage (datasheet Table 8).

    Bipolar codes are offset binary (FORMAT = 1, what :meth:`MAX11410.init` sets) unless
    ``offset_binary=False`` (FORMAT = 0, two's complement, the reset state). Unipolar codes are
    always straight binary.
    """
    if cfg.bipolar:
        code = raw - MID_SCALE if offset_binary else _signed24(raw)
        return code * cfg.vref_volts / ((MID_SCALE - 1) * cfg.gain)
    return raw * cfg.vref_volts / ((FULL_SCALE - 1) * cfg.gain)


def volts_to_raw(volts: float, cfg: ChannelConfig, *, offset_binary: bool = True) -> int:
    """Inverse of :func:`raw_to_volts`, rounded and clamped to the code range."""
    if cfg.bipolar:
        code = round(volts * (MID_SCALE - 1) * cfg.gain / cfg.vref_volts)
        code = min(max(code, -MID_SCALE), MID_SCALE - 1)
        return code + MID_SCALE if offset_binary else code & (FULL_SCALE - 1)
    code = round(volts * (FULL_SCALE - 1) * cfg.gain / cfg.vref_volts)
    return min(max(code, 0), FULL_SCALE - 1)


def lsb_volts(cfg: ChannelConfig) -> float:
    if cfg.bipolar:
        return cfg.vref_volts / ((MID_SCALE - 1) * cfg.gain)
    return cfg.vref_volts / ((FULL_SCALE - 1) * cfg.gain)


@dataclass(frozen=True)
class Reading:
    channel: int
    raw: int
    volts: float


class MAX11410:
    """The ADC as seen through the EVKIT.

    ``channels`` holds 8 :class:`ChannelConfig` slots; slot *n* converts into DATA<n>
    (CONV_START.DEST = n). Slot 0 is the EVKIT default (AIN2 vs GND, REF1 2.5 V, SINC4 60 sps).
    """

    def __init__(
        self,
        board: EvKitBoard,
        *,
        spec: RegisterMapSpec | None = None,
        poll_interval: float = 0.001,
        conversion_timeout: float = 3.0,
        calibration_timeout: float = 2.0,
    ) -> None:
        self.board = board
        self.regs = RegisterMap(board, spec)
        self.poll_interval = poll_interval
        self.conversion_timeout = conversion_timeout
        self.calibration_timeout = calibration_timeout
        self.channels: list[ChannelConfig] = [replace(EVKIT_DEFAULT_CHANNEL)] + [
            replace(EVKIT_DEFAULT_CHANNEL, name=f"CH{i}", enabled=False)
            for i in range(1, NUM_CHANNELS)
        ]
        self.active_channel: int | None = None
        self.board_wait_ok: bool | None = None  # None = untested; False = use STATUS polling
        self.continuous = False

    # ------------------------------------------------------------------ identity / init
    def identify(self) -> int:
        """Read PART_ID and check it is a MAX11410."""
        pid = self.regs.read("PART_ID")
        if pid == 0xFFFFFF or pid == 0:
            raise DeviceError(
                f"PART_ID reads 0x{pid:06X}: the ADC side looks unpowered or SPI is dead "
                "(see 'Board power-up sequence' in the README)"
            )
        if pid != PART_ID:
            raise DeviceError(f"unexpected PART_ID 0x{pid:06X} (MAX11410 is 0x{PART_ID:06X})")
        return pid

    def init(self, *, intrb_gpio: int | None = None, check_id: bool = True) -> None:
        """Bring the ADC to a known state.

        * PD = NORMAL. Not strictly needed (any CONV_START/CAL_START/SEQ_START/WAIT_START
          write wakes the part from standby/sleep), but it starts the wakeup timer early and
          makes register reads reflect the running state. Harmless.
        * CTRL.FORMAT = 1 (offset binary for bipolar data; reset is two's complement).
        * Optionally GPx_CTRL = 0xC1 (CMOS output, INTRB). Not needed on the EVKIT: the board
          watches DOUT/INTB, and STATUS_IE resets to CONV_RDY enabled.
        """
        if check_id:
            self.identify()
        self.regs.invalidate()
        self.regs.update("PD", PD="NORMAL")
        self.regs.update("CTRL", FORMAT="OFFSET_BINARY")
        if intrb_gpio is not None:
            if intrb_gpio not in (0, 1):
                raise ValueError("intrb_gpio must be 0 or 1")
            self.regs.write(f"GP{intrb_gpio}_CTRL", GPIO_INTRB)
        self.continuous = False
        self.active_channel = None

    # ------------------------------------------------------------------ channels
    def set_channel(self, index: int, cfg: ChannelConfig) -> None:
        self._check_index(index)
        self.channels[index] = cfg.validate()
        if self.active_channel == index:
            self.active_channel = None

    def _check_index(self, index: int) -> None:
        if not 0 <= index < NUM_CHANNELS:
            raise ValueError(f"channel must be 0..{NUM_CHANNELS - 1}")

    def _cfg(self, channel: int | ChannelConfig) -> tuple[int, ChannelConfig]:
        if isinstance(channel, ChannelConfig):
            return -1, channel.validate()
        self._check_index(channel)
        return channel, self.channels[channel].validate()

    def apply_channel(self, channel: int | ChannelConfig = 0) -> ChannelConfig:
        """Program MUX_CTRL0, CTRL, PGA and FILTER for one channel (driver order), then
        wait ``settling_us``."""
        index, cfg = self._cfg(channel)
        r = self.regs
        r.write("MUX_CTRL0", (cfg.ainp << 4) | cfg.ainn)
        r.update(
            "CTRL",
            REF_SEL=cfg.refsel,
            REFBUFP_EN=cfg.vrefp_buf,
            REFBUFN_EN=cfg.vrefn_buf,
            U_BN=not cfg.bipolar,
            FORMAT="OFFSET_BINARY",
        )
        r.write("PGA", r.spec["PGA"].encode(SIG_PATH=SIG_PATHS[cfg.path], GAIN=cfg.gain_code))
        r.update("FILTER", RESERVED=0, LINEF=cfg.linef, RATE=cfg.rate)
        if cfg.settling_us:
            time.sleep(cfg.settling_us / 1e6)
        self.active_channel = index if index >= 0 else None
        return cfg

    # ------------------------------------------------------------------ conversions
    @staticmethod
    def conv_start_value(dest: int, conv_type: int) -> int:
        """CONV_START byte: DEST[6:4] | CONV_TYPE[1:0]."""
        if not 0 <= dest < NUM_CHANNELS or not 0 <= conv_type <= 3:
            raise ValueError("dest must be 0..7 and conv_type 0..3")
        return (dest << 4) | conv_type

    def wait_status(self, bit: str, timeout: float) -> int:
        """Poll STATUS until ``bit`` (e.g. CONV_RDY / CAL_RDY) is set. Returns STATUS."""
        f = self.regs.spec["STATUS"].field(bit)
        end = time.monotonic() + timeout
        while True:
            st = self.regs.read("STATUS")
            if f.decode(st):
                return st
            if time.monotonic() > end:
                raise Timeout(f"STATUS.{f.name} not set within {timeout:g} s")
            time.sleep(self.poll_interval)

    def read_single(
        self, channel: int | ChannelConfig = 0, *, method: Literal["auto", "board", "poll"] = "auto"
    ) -> Reading:
        """One single-cycle conversion on ``channel`` (result in DATA<channel>).

        ``board`` uses firmware command 0x51 (the board waits for the DOUT/INTB data-ready
        edge); ``poll`` polls STATUS.CONV_RDY over USB; ``auto`` tries the board wait once and
        falls back to polling for good if the firmware reports 0xFA.
        """
        if self.continuous:
            raise EvKitError("stop continuous conversions first")
        index, cfg = self._cfg(channel)
        self.apply_channel(cfg if index < 0 else index)
        dest = max(index, 0)
        conv = self.conv_start_value(dest, CONV_SINGLE)
        data_reg = self.regs.spec[f"DATA{dest}"]
        period = 1.0 / cfg.data_rate(mode="single")
        use_board = method == "board" or (method == "auto" and self.board_wait_ok is not False)
        raw: int | None = None
        if use_board:
            timeout = max(self.board.drdy_timeout, 3 * period + 2.0)
            try:
                reply = self.board.wait_drdy_read(
                    data_reg.addr, 3, start=True, conv=conv, timeout=timeout
                )
                raw = int.from_bytes(reply, "big")
                self.board_wait_ok = True
            except CommandFailed:
                if method == "board":
                    raise
                log.warning("board-side data-ready wait failed; falling back to STATUS polling")
                self.board_wait_ok = False
        if raw is None:
            self.regs.write("CONV_START", conv)
            self.wait_status("CONV_RDY", max(self.conversion_timeout, 3 * period))
            raw = self.regs.read(data_reg)
        return Reading(dest, raw, raw_to_volts(raw, cfg))

    def read_many(self, channel: int = 0, n: int = 1, **kw) -> list[Reading]:
        return [self.read_single(channel, **kw) for _ in range(n)]

    def start_continuous(self, channel: int | ChannelConfig = 0) -> ChannelConfig:
        index, _ = self._cfg(channel)
        cfg = self.apply_channel(channel)
        self.regs.write("CONV_START", self.conv_start_value(max(index, 0), CONV_CONTINUOUS))
        self.continuous = True
        return cfg

    def stop_continuous(self) -> None:
        """Driver behaviour: writing CONV_START = SINGLE ends continuous mode (this also
        starts one last single conversion)."""
        self.regs.write("CONV_START", CONV_SINGLE)
        self.continuous = False

    def read_continuous(self, channel: int = 0, *, timeout: float | None = None) -> Reading:
        """Next continuous-mode result of ``channel`` (board-side wait, no new CONV_START)."""
        index, cfg = self._cfg(channel)
        addr = self.regs.spec[f"DATA{index}"].addr
        raw = int.from_bytes(
            self.board.wait_drdy_read(addr, 3, start=False, timeout=timeout), "big"
        )
        return Reading(index, raw, raw_to_volts(raw, cfg))

    def capture(
        self,
        channel: int = 0,
        n: int = 1000,
        *,
        continuous: bool = True,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[Reading]:
        """Block capture of ``n`` samples into board SRAM, then read back and convert."""
        index, cfg = self._cfg(channel)
        self.apply_channel(index)
        mode: RateMode = "continuous" if continuous else "single"
        expected = n / cfg.data_rate(mode=mode)
        if continuous and cfg.linef == "SINC4":
            expected += SINC4_CONTINUOUS_SETTLING_PERIODS / cfg.data_rate(mode=mode)
        data = self.board.capture(
            n,
            reg=self.regs.spec[f"DATA{index}"].addr,
            conv=self.conv_start_value(index, CONV_CONTINUOUS if continuous else CONV_SINGLE),
            restart_each=not continuous,
            timeout=expected,
            progress=progress,
        )
        if continuous:
            self.stop_continuous()
        return [Reading(index, raw, raw_to_volts(raw, cfg)) for raw in samples_from_bytes(data)]

    # ------------------------------------------------------------------ calibration
    def _calibration_timeout(self) -> float:
        """Two conversions at the current filter setting, with margin."""
        filt = self.regs.spec["FILTER"].decode(self.regs.get("FILTER"))
        linef = LINEF[filt["LINEF"]]
        period = 1.0 / data_rate(linef, filt["RATE"], mode="single")
        return max(self.calibration_timeout, 4 * period + 0.5)

    def calibrate(
        self,
        cal_type: Literal["SELF", "PGA", "SYS_OFF_A", "SYS_GAIN_A", "SYS_OFF_B", "SYS_GAIN_B"],
    ) -> None:
        """Run one CAL_START and wait for STATUS.CAL_RDY.

        System calibrations need the zero-/full-scale signal applied to the selected inputs
        first (datasheet "System Offset and Gain Calibration"); do offset before gain.
        """
        self.regs.read("STATUS")  # clear a stale CAL_RDY
        self.regs.update("CAL_START", CAL_TYPE=cal_type)
        self.wait_status("CAL_RDY", self._calibration_timeout())

    def self_calibrate(self, *, pga: bool = True, rate: int | None = None) -> None:
        """Self calibration (offset + gain 1x), then PGA gain calibration for 2x..128x.

        Calibration runs at the current LINEF/RATE, so apply the channel you will measure with
        first (the CLI does). ``rate`` overrides FILTER.RATE during calibration, e.g.
        ``FILTER_RATE_MAX`` for the Linux driver's fast calibration. FILTER and PGA are
        restored afterwards.
        """
        if self.continuous:
            raise EvKitError("stop continuous conversions first")
        filt = self.regs.read("FILTER")
        pga_reg = self.regs.read("PGA")
        try:
            if rate is not None:
                self.regs.update("FILTER", RATE=rate)
            self.calibrate("SELF")
            if pga:
                self.regs.update("PGA", SIG_PATH="PGA")
                for code in range(1, 8):
                    self.regs.update("PGA", GAIN=code)
                    self.calibrate("PGA")
        finally:
            self.regs.write("PGA", pga_reg)
            self.regs.write("FILTER", filt)
            self.active_channel = None

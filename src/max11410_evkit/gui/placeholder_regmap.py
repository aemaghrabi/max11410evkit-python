"""PLACEHOLDER register map and option tables for the GUI mockup.

!!! PLACEHOLDER — to be replaced by the Phase 2 ``regmap.yaml`` !!!

Everything in this module is a stand-in so the GUI can be laid out and clicked
through before the real register map exists. Register widths, bit-field layouts,
option encodings and FIR rate values below are NOT authoritative: only the fields
listed in the task brief (FILTER, CTRL, PGA, MUX_CTRL0, STATUS) are filled in, and
even those must be checked against the MAX11410 datasheet when Phase 2 lands.

The GUI only consumes this module through:
    REGISTERS        list[Register]
    MICROCODE        list[Register]        (uC0..uC52)
    register_by_addr(addr) -> Register | None
    option tables    (MUX_INPUTS, PGA_GAINS, FILTERS, RATES, REFSEL_OPTIONS, ...)
so swapping it for a regmap-backed adapter later is a one-module change.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PLACEHOLDER = True
PENDING = "description pending (Phase 2)"


@dataclass(frozen=True)
class BitField:
    name: str
    msb: int
    lsb: int
    description: str = ""

    @property
    def bits(self) -> str:
        return f"{self.msb}" if self.msb == self.lsb else f"{self.msb}:{self.lsb}"

    def extract(self, value: int) -> int:
        width = self.msb - self.lsb + 1
        return (value >> self.lsb) & ((1 << width) - 1)


@dataclass(frozen=True)
class Register:
    addr: int
    name: str
    width: int = 8  # bits  (PLACEHOLDER widths)
    fields: tuple[BitField, ...] = field(default_factory=tuple)
    description: str = PENDING

    @property
    def hex_digits(self) -> int:
        return (self.width + 3) // 4


_FIELDS: dict[str, tuple[BitField, ...]] = {
    "FILTER": (
        BitField(
            "RATE", 3, 0, "Output data rate select (meaning depends on LINEF and conversion mode)."
        ),
        BitField(
            "LINEF",
            5,
            4,
            "Digital filter: 00 = FIR 50/60 Hz, 01 = FIR 50 Hz, 10 = FIR 60 Hz, 11 = SINC4.",
        ),
    ),
    "CTRL": (
        BitField(
            "REFSEL", 2, 0, "Reference select (REF0P/N, REF1P/N, REF2P/N, AVDD/AGND, REFxP/AGND)."
        ),
        BitField("VREFN_BUF", 3, 3, "Enable negative reference input buffer."),
        BitField("VREFP_BUF", 4, 4, "Enable positive reference input buffer."),
        BitField("FORMAT", 5, 5, "Bipolar output format: 0 = two's complement, 1 = offset binary."),
        BitField("UNIPOLAR", 6, 6, "1 = unipolar input range, 0 = bipolar."),
    ),
    "PGA": (
        BitField("GAIN", 2, 0, "PGA gain = 2^GAIN (1 .. 128)."),
        BitField("SIG_PATH", 5, 4, "Signal path: 00 = buffered, 01 = bypass, 10 = PGA."),
    ),
    "MUX_CTRL0": (
        BitField("AINN", 3, 0, "Negative input select (AIN0..AIN9, AVDD, GND)."),
        BitField("AINP", 7, 4, "Positive input select (AIN0..AIN9, AVDD, GND)."),
    ),
    "STATUS": (
        BitField("CONV_READY", 0, 0, "Conversion result available."),
        BitField("CAL_READY", 2, 2, "Calibration complete."),
    ),
}

_24 = 24
_REG_LIST: list[tuple[int, str, int]] = [
    (0x00, "PD", 8),
    (0x01, "CONV_START", 8),
    (0x02, "SEQ_START", 8),
    (0x03, "CAL_START", 8),
    (0x04, "GP0_CTRL", 8),
    (0x05, "GP1_CTRL", 8),
    (0x06, "GP_CONV", 8),
    (0x07, "GP_SEQ_ADDR", 8),
    (0x08, "FILTER", 8),
    (0x09, "CTRL", 8),
    (0x0A, "SOURCE", 8),
    (0x0B, "MUX_CTRL0", 8),
    (0x0C, "MUX_CTRL1", 8),
    (0x0D, "MUX_CTRL2", 8),
    (0x0E, "PGA", 8),
    (0x0F, "WAIT_EXT", 8),
    (0x10, "WAIT_START", 8),
    (0x11, "PART_ID", _24),
    (0x12, "SYSC_SEL", 8),
    (0x13, "SYS_OFF_A", _24),
    (0x14, "SYS_OFF_B", _24),
    (0x15, "SYS_GAIN_A", _24),
    (0x16, "SYS_GAIN_B", _24),
    (0x17, "SELF_OFF", _24),
]
_REG_LIST += [(0x18 + i, f"SELF_GAIN_{1 << i}", _24) for i in range(8)]
_REG_LIST += [(0x20 + i, f"LTHRESH{i}", _24) for i in range(8)]
_REG_LIST += [(0x28 + i, f"UTHRESH{i}", _24) for i in range(8)]
_REG_LIST += [(0x30 + i, f"DATA{i}", _24) for i in range(8)]
_REG_LIST += [(0x38, "STATUS", _24), (0x39, "STATUS_IE", _24)]

REGISTERS: list[Register] = [
    Register(
        a, n, w, _FIELDS.get(n, ()), "Known bit-fields (placeholder)" if n in _FIELDS else PENDING
    )
    for a, n, w in _REG_LIST
]

# Sequencer microcode registers uC0..uC52 at 0x3A..0x6E (width is a PLACEHOLDER: 16 bits)
MICROCODE: list[Register] = [Register(0x3A + i, f"µC{i}", 16) for i in range(53)]

_BY_ADDR = {r.addr: r for r in REGISTERS + MICROCODE}


def register_by_addr(addr: int) -> Register | None:
    return _BY_ADDR.get(addr)


# ---------------------------------------------------------------- option tables
# "NC" = unconnected (MUX_CTRL0 codes 11-15; the reset value 0xFF reads as NC/NC).
MUX_INPUTS = [f"AIN{i}" for i in range(10)] + ["AVDD", "GND", "NC"]
SIG_PATHS = ["Buffered", "Bypass", "PGA"]
PGA_GAINS = [1, 2, 4, 8, 16, 32, 64, 128]
FILTERS = ["FIR 50/60 Hz", "FIR 50 Hz", "FIR 60 Hz", "SINC4"]  # LINEF 0..3
CONV_MODES = ["Single", "Continuous", "Duty cycle"]
REFSEL_OPTIONS = [
    "REF0P / REF0N",
    "REF1P / REF1N",
    "REF2P / REF2N",
    "AVDD / AGND",
    "REF0P / AGND",
    "REF1P / AGND",
    "REF2P / AGND",
]
POWER_MODES = ["Normal", "Standby", "Sleep"]
IDAC_CURRENTS_UA = [
    10,
    50,
    75,
    100,
    125,
    150,
    175,
    200,
    225,
    250,
    300,
    400,
    600,
    800,
    1000,
]  # PLACEHOLDER
BURNOUT_OPTIONS = ["Off", "0.5 µA", "1 µA", "10 µA"]  # PLACEHOLDER
VBIAS_MODES = ["Off", "Active", "Passive"]  # PLACEHOLDER
GPIO_MODES = [
    "Disabled",
    "Input",
    "Output low",
    "Output high",
    "INTB (interrupt)",
    "Conversion trigger",
    "Sequencer trigger",
    "Clock out",
]  # PLACEHOLDER
CLOCK_SOURCES = ["Internal (2.4576 MHz)", "External"]
THRESHOLD_UNITS = ["LSB", "V", "mV", "µV"]
SAMPLE_COUNTS = [256, 512, 1024, 2048, 4096, 8192]  # 8192 = max with 32 KB board SRAM

# Continuous-mode output rates (sps) per filter, indexed by RATE code.
# SINC4 values are from the EV kit brief (60 sps nominal reads back as 59.8 sps);
# FIR values are the continuous-mode columns of datasheet Tables 9a-9c (Phase 5).
# RATE codes past the end of a list repeat the last entry.
RATES: dict[str, list[float]] = {
    "SINC4": [4.0, 10.0, 20.0, 40.0, 59.8, 120.0, 240.0, 480.0, 960.0, 1920.0],
    "FIR 50/60 Hz": [1.1, 2.1, 4.2, 8.4, 16.8],
    "FIR 50 Hz": [1.3, 2.7, 5.3, 10.7, 21.3, 40.0],
    "FIR 60 Hz": [1.3, 2.7, 5.3, 10.7, 21.3, 40.0],
}
CAPTURE_RATES = RATES["SINC4"]

NUM_CHANNELS = 8  # host-side channels 0..7 <-> DATA0..7, LTHRESH/UTHRESH0..7, sequencer

# Per-channel configuration (host-side concept; applied to the global MUX/CTRL/PGA/FILTER
# registers by the backend before each conversion of that channel).
CHANNEL_KEYS = (
    "name", "enabled", "ainp", "ainn", "sig_path", "pga_gain", "refsel", "vrefp_buf",
    "vrefn_buf", "vref", "unipolar", "format", "filter", "rate", "settling_ms",
)  # fmt: skip
CHANNEL_DEFAULTS = {
    "name": "",  # "" -> displayed as "Channel n"
    "enabled": False,
    "ainp": "AIN2",
    "ainn": "GND",
    "sig_path": "Buffered",
    "pga_gain": 1,
    "refsel": "REF0P / REF0N",  # PLACEHOLDER: confirm EV kit power-on reference
    "vrefp_buf": False,
    "vrefn_buf": False,
    "vref": 2.5,
    "unipolar": False,
    "format": "Two's complement",
    "filter": "SINC4",
    "rate": 59.8,
    "settling_ms": 0.0,
}

# Device-wide (global) settings.
DEVICE_KEYS = (
    "conv_mode", "power", "idac_current_ua", "idac0_pin", "idac1_pin", "vbias_mode",
    "vbias_pins", "burnout",
)  # fmt: skip
DEVICE_DEFAULTS = {
    "conv_mode": "Continuous",
    "power": "Normal",
    "idac_current_ua": None,
    "idac0_pin": "AIN0",
    "idac1_pin": "AIN0",
    "vbias_mode": "Off",
    "vbias_pins": [],
    "burnout": "Off",
}

# EV kit software start-up state: channel 0 enabled (AIN2-GND, 60Hz SINC4 59.8 sps), Continuous.
STARTUP_DEFAULTS = {**CHANNEL_DEFAULTS, **DEVICE_DEFAULTS, "enabled": True, "channel": 0}
CLOCK_HZ = 2.4576e6

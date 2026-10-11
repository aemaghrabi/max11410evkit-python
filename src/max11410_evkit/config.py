"""Configuration files: a register snapshot plus the 8 host-side channel configs (Phase 4).

One JSON file holds both (decisions in ``docs/plan/p4-config-cli.md``)::

    {
      "format": "max11410-evkit-config", "version": 1,
      "software": "max11410-evkit 0.1.0", "saved": "2026-10-11T23:50:00+03:00",
      "part_id": "0x000F02",
      "registers": { "PD": "0x00", "GP0_CTRL": "0x00", ..., "UC52": "0x0000" },
      "channels": { "0": { ChannelConfig.to_dict() }, ..., "7": { ... } }
    }

Registers
    Every register that is readable, writable, not a write-trigger and not volatile, in
    address order: 0x00-0x39 and the sequencer µC0-µC52 (0x3A-0x6E). That leaves out
    CONV_START, SEQ_START, CAL_START, WAIT_START (writing them starts an action), DATA0-7,
    STATUS, PART_ID and UCADDR. Values are hex strings (plain integers are accepted too).

    Loading writes them in address order, except that **PD is written last**, so the power
    state is set after everything else. A µC entry that targets a 24-bit register or a µC
    address is rejected on load, because the chip would store 0x0000 instead. The calibration
    registers (SYS_OFF_A/B, SYS_GAIN_A/B, SELF_OFF, SELF_GAIN_1..128) are saved but only
    restored with ``with_cal=True``.

Channels
    ``ChannelConfig.to_dict()`` for slots 0-7 (the backend schema). A missing slot loads as
    the default config, disabled. There is no separate "device" section: the global settings
    (conversion mode, power, IDAC, VBIAS, ...) are in the register snapshot.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from max11410_evkit import __version__
from max11410_evkit.device.max11410 import (
    EVKIT_DEFAULT_CHANNEL,
    MAX11410,
    NUM_CHANNELS,
    ChannelConfig,
)
from max11410_evkit.device.regmap import (
    Register,
    RegisterMap,
    RegisterMapSpec,
    is_sequencer_entry,
    load_regmap,
    sequencer_entry_problem,
)

FORMAT = "max11410-evkit-config"
VERSION = 1

#: Saved, but restored (and compared) only with ``with_cal=True``.
CAL_REGISTERS = (
    "SYS_OFF_A",
    "SYS_OFF_B",
    "SYS_GAIN_A",
    "SYS_GAIN_B",
    "SELF_OFF",
    *(f"SELF_GAIN_{g}" for g in (1, 2, 4, 8, 16, 32, 64, 128)),
)
#: Written after every other register.
WRITE_LAST = ("PD",)


class ConfigError(ValueError):
    """The file is not a valid MAX11410 EVKIT config."""


def config_registers(spec: RegisterMapSpec | None = None) -> list[Register]:
    """The registers a config file holds, in address order."""
    spec = spec or load_regmap()
    regs = [r for r in spec if r.readable and r.writable and not r.trigger and not r.volatile]
    return sorted(regs, key=lambda r: r.addr)


def default_channels() -> list[ChannelConfig]:
    """Slot 0 = EVKIT default (enabled), slots 1-7 the same but disabled (as in MAX11410)."""
    return [replace(EVKIT_DEFAULT_CHANNEL)] + [
        replace(EVKIT_DEFAULT_CHANNEL, name=f"CH{i}", enabled=False) for i in range(1, NUM_CHANNELS)
    ]


@dataclass
class DeviceConfig:
    registers: dict[str, int] = field(default_factory=dict)
    channels: list[ChannelConfig] = field(default_factory=default_channels)
    part_id: int | None = None
    saved: str | None = None
    software: str | None = None

    # ------------------------------------------------------------------ JSON
    def to_json_dict(self, spec: RegisterMapSpec | None = None) -> dict[str, Any]:
        spec = spec or load_regmap()
        order = {r.name: r.addr for r in spec}
        regs = {
            name: spec[name].fmt(value)
            for name, value in sorted(self.registers.items(), key=lambda kv: order[kv[0]])
        }
        return {
            "format": FORMAT,
            "version": VERSION,
            "software": self.software or f"max11410-evkit {__version__}",
            "saved": self.saved or datetime.now().astimezone().isoformat(timespec="seconds"),
            "part_id": None if self.part_id is None else f"0x{self.part_id:06X}",
            "registers": regs,
            "channels": {str(i): c.to_dict() for i, c in enumerate(self.channels)},
        }

    @classmethod
    def from_json_dict(
        cls, data: Mapping[str, Any], spec: RegisterMapSpec | None = None
    ) -> DeviceConfig:
        spec = spec or load_regmap()
        if not isinstance(data, Mapping) or data.get("format") != FORMAT:
            raise ConfigError(f'not a {FORMAT} file ("format" key missing or different)')
        if data.get("version") != VERSION:
            raise ConfigError(f"unsupported config version {data.get('version')!r}")
        allowed = {r.name for r in config_registers(spec)}
        registers: dict[str, int] = {}
        for name, raw in (data.get("registers") or {}).items():
            if name not in spec:
                raise ConfigError(f"unknown register {name!r}")
            reg = spec[name]
            if reg.name not in allowed:
                raise ConfigError(f"register {reg.name} cannot be part of a config")
            value = _int(raw, reg.name)
            if not 0 <= value <= reg.max_value:
                raise ConfigError(f"{reg.name}: 0x{value:X} does not fit in {reg.width} byte(s)")
            if is_sequencer_entry(reg.addr):
                problem = sequencer_entry_problem(value, spec)
                if problem:
                    raise ConfigError(
                        f"{reg.name}: 0x{value:04X} {problem}; the chip would store 0x0000 "
                        "(datasheet Sequencer Notes)"
                    )
            registers[reg.name] = value
        channels = default_channels()
        for key, d in (data.get("channels") or {}).items():
            try:
                idx = int(key)
            except ValueError:
                raise ConfigError(f"channel key {key!r} is not a number") from None
            if not 0 <= idx < NUM_CHANNELS:
                raise ConfigError(f"channel {idx} is out of range 0..{NUM_CHANNELS - 1}")
            try:
                channels[idx] = ChannelConfig.from_dict(dict(d)).validate()
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"channel {idx}: {exc}") from None
        pid = data.get("part_id")
        return cls(
            registers=registers,
            channels=channels,
            part_id=None if pid is None else _int(pid, "part_id"),
            saved=data.get("saved"),
            software=data.get("software"),
        )

    def save(self, path: str | Path, spec: RegisterMapSpec | None = None) -> None:
        text = json.dumps(self.to_json_dict(spec), indent=2)
        Path(path).write_text(text + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path, spec: RegisterMapSpec | None = None) -> DeviceConfig:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path}: not valid JSON ({exc})") from None
        return cls.from_json_dict(data, spec)


def _int(raw: Any, what: str) -> int:
    if isinstance(raw, bool):
        raise ConfigError(f"{what}: {raw!r} is not a number")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            return int(raw, 0)
        except ValueError:
            pass
    raise ConfigError(f"{what}: {raw!r} is not a number")


# ---------------------------------------------------------------------- board side
def read_config(dev: MAX11410) -> DeviceConfig:
    """Read the config registers from the board; channels come from ``dev.channels``."""
    regs: RegisterMap = dev.regs
    values = {r.name: regs.read(r) for r in config_registers(regs.spec)}
    return DeviceConfig(
        registers=values,
        channels=[replace(c) for c in dev.channels],
        part_id=regs.read("PART_ID"),
    )


def write_order(names: Iterable[str], spec: RegisterMapSpec, *, with_cal: bool) -> list[Register]:
    """Registers to write, in address order, calibration ones only with ``with_cal``, PD last."""
    regs = [spec[n] for n in names]
    if not with_cal:
        regs = [r for r in regs if r.name not in CAL_REGISTERS]
    regs.sort(key=lambda r: (r.name in WRITE_LAST, r.addr))
    return regs


@dataclass
class LoadResult:
    written: list[str]
    skipped_cal: list[str]
    mismatches: list[tuple[str, int, int]]  # (name, wanted, read back); empty if not verified
    verified: bool


def apply_config(
    dev: MAX11410, cfg: DeviceConfig, *, with_cal: bool = False, verify: bool = True
) -> LoadResult:
    """Write ``cfg.registers`` to the board (PD last) and copy its channels into ``dev``.

    With ``verify`` every written register is read back and any difference is reported
    (not raised).
    """
    spec = dev.regs.spec
    order = write_order(cfg.registers, spec, with_cal=with_cal)
    for r in order:
        dev.regs.write(r, cfg.registers[r.name])
    for i, ch in enumerate(cfg.channels):
        dev.set_channel(i, ch)
    dev.active_channel = None
    mismatches: list[tuple[str, int, int]] = []
    if verify:
        for r in order:
            back = dev.regs.read(r)
            if back != cfg.registers[r.name]:
                mismatches.append((r.name, cfg.registers[r.name], back))
    skipped = [n for n in cfg.registers if n in CAL_REGISTERS and not with_cal]
    return LoadResult([r.name for r in order], skipped, mismatches, verify)


def diff_config(
    dev: MAX11410, cfg: DeviceConfig, *, with_cal: bool = False
) -> list[tuple[str, int, int]]:
    """Registers where the board differs from the file: [(name, file, board)], address order."""
    out = []
    for r in write_order(cfg.registers, dev.regs.spec, with_cal=with_cal):
        live = dev.regs.read(r)
        if live != cfg.registers[r.name]:
            out.append((r.name, cfg.registers[r.name], live))
    return sorted(out, key=lambda t: dev.regs.spec[t[0]].addr)

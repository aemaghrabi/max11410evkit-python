"""MAX11410 register map: static description (from ``regmap.yaml``) and a live, cached view.

Static side::

    spec = load_regmap()                 # cached; parses the packaged regmap.yaml
    spec["FILTER"].field("LINEF").encode("SINC4")   -> 0x30
    spec[0x09].decode(0x21)              -> {"REFSEL": 1, "VREFN_BUF": 0, ..., "FORMAT": 1, ...}

Live side (bound to an :class:`~max11410_evkit.board.evkit.EvKitBoard`)::

    regs = RegisterMap(board)
    regs.FILTER.LINEF = "SINC4"          # read-modify-write through the shadow cache
    regs.FILTER.RATE                     # FieldValue(4), .label is None (no enum)
    regs.CTRL.REFSEL == "REF1"           # FieldValue compares with enum names
    regs.read("PART_ID")                 # always goes to hardware
    snapshot = regs.read_all(); print(regs.dump(snapshot)); regs.diff(old, new)

Shadow-cache rules: every hardware read/write updates the cache. Field read-modify-writes
start from the cached value unless the register is ``volatile`` (STATUS, DATAx, CONV_START,
...), which is always read from hardware. :meth:`RegisterMap.invalidate` drops the cache
(e.g. after a device reset or reconnect).
"""

from __future__ import annotations

import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from functools import cache
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from max11410_evkit.board.evkit import EvKitBoard

TODO_MARK = "TODO(datasheet)"


# --------------------------------------------------------------------------- static model
@dataclass(frozen=True)
class Field:
    name: str
    msb: int
    lsb: int
    access: str = "rw"
    values: Mapping[str, int] = field(default_factory=dict)
    description: str = ""
    confirmed: bool = False
    aliases: tuple[str, ...] = ()

    def matches(self, name: str) -> bool:
        n = name.upper()
        return self.name.upper() == n or any(a.upper() == n for a in self.aliases)

    @property
    def width(self) -> int:
        return self.msb - self.lsb + 1

    @property
    def mask(self) -> int:
        return ((1 << self.width) - 1) << self.lsb

    @property
    def max_value(self) -> int:
        return (1 << self.width) - 1

    @property
    def bits(self) -> str:
        return str(self.lsb) if self.msb == self.lsb else f"{self.msb}:{self.lsb}"

    @property
    def writable(self) -> bool:
        return "w" in self.access

    def value_of(self, value: int | str | bool) -> int:
        """Translate an enum name / bool / int into the raw field value (unshifted)."""
        if isinstance(value, str):
            key = value.strip()
            for name, v in self.values.items():
                if name.upper() == key.upper():
                    return v
            try:
                v = int(key, 0)
            except ValueError:
                choices = ", ".join(self.values) or "an integer"
                raise ValueError(f"{self.name}: unknown value {value!r} (use {choices})") from None
        else:
            v = int(value)
        if not 0 <= v <= self.max_value:
            raise ValueError(f"{self.name}: {v} does not fit in {self.width} bit(s)")
        return v

    def encode(self, value: int | str | bool) -> int:
        """Shifted register bits for ``value``."""
        return self.value_of(value) << self.lsb

    def decode(self, regval: int) -> int:
        return (regval & self.mask) >> self.lsb

    def label(self, raw: int) -> str | None:
        for name, v in self.values.items():
            if v == raw:
                return name
        return None

    def insert(self, regval: int, value: int | str | bool) -> int:
        return (regval & ~self.mask) | self.encode(value)


@dataclass(frozen=True)
class Register:
    addr: int
    name: str
    width: int
    access: str = "rw"
    reset: int | None = None
    description: str = ""
    fields: tuple[Field, ...] = ()
    confirmed: bool = False
    reset_confirmed: bool = False
    width_confirmed: bool = True
    volatile: bool = False
    trigger: bool = False
    source: str = ""
    reset_source: str = ""
    measured_reset: int | None = None
    notes: str = ""

    @property
    def readable(self) -> bool:
        return "r" in self.access

    @property
    def writable(self) -> bool:
        return "w" in self.access

    @property
    def max_value(self) -> int:
        return (1 << (8 * self.width)) - 1

    def field(self, name: str) -> Field:
        """Field by datasheet name or alias (case-insensitive)."""
        for f in self.fields:
            if f.matches(name):
                return f
        raise KeyError(f"{self.name} has no field {name!r}")

    def has_field(self, name: str) -> bool:
        return any(f.matches(name) for f in self.fields)

    def decode(self, value: int) -> dict[str, int]:
        return {f.name: f.decode(value) for f in self.fields}

    def describe(self, value: int, *, compact: bool = False) -> str:
        """``U_BN=0(BIPOLAR) ... REF_SEL=1(REF1)``.

        ``compact`` hides whole-register fields and 1-bit flags without an enum that are 0
        (keeps STATUS / STATUS_IE readable)."""
        parts = []
        for f in self.fields:
            if compact and f.width == 8 * self.width:
                continue
            raw = f.decode(value)
            lab = f.label(raw)
            if compact and f.width == 1 and not f.values:
                if raw:
                    parts.append(f.name)
                continue
            parts.append(f"{f.name}={raw}" + (f"({lab})" if lab else ""))
        return " ".join(parts)

    def encode(self, **fields: int | str | bool) -> int:
        """Build a register value from field values, starting from 0."""
        v = 0
        for k, val in fields.items():
            v = self.field(k).insert(v, val)
        return v

    def fmt(self, value: int) -> str:
        return f"0x{value:0{2 * self.width}X}"


def _parse_bits(text: str | int) -> tuple[int, int]:
    s = str(text).strip()
    if ":" in s:
        hi, lo = (int(p) for p in s.split(":"))
    else:
        hi = lo = int(s)
    if hi < lo:
        hi, lo = lo, hi
    return hi, lo


class RegisterMapSpec:
    """The parsed register map, indexable by name (case-insensitive) or address."""

    def __init__(self, registers: Iterable[Register], *, version: int = 1) -> None:
        self.version = version
        self.registers: tuple[Register, ...] = tuple(sorted(registers, key=lambda r: r.addr))
        self._by_addr = {r.addr: r for r in self.registers}
        self._by_name = {r.name.upper(): r for r in self.registers}
        if len(self._by_addr) != len(self.registers) or len(self._by_name) != len(self.registers):
            raise ValueError("duplicate register address or name in register map")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RegisterMapSpec:
        regs = []
        for i, r in enumerate(data.get("registers") or []):
            try:
                width = int(r["width"])
                access = str(r.get("access", "rw"))
                fields = []
                for f in r.get("fields") or []:
                    msb, lsb = _parse_bits(f["bits"])
                    if msb >= 8 * width:
                        raise ValueError(f"field {f['name']} bits {f['bits']} exceed width")
                    vals = {str(k): int(v) for k, v in (f.get("values") or {}).items()}
                    fields.append(
                        Field(
                            name=str(f["name"]),
                            msb=msb,
                            lsb=lsb,
                            access=str(f.get("access", access)),
                            values=vals,
                            description=str(f.get("description", "")),
                            confirmed=bool(f.get("confirmed", False)),
                            aliases=tuple(str(a) for a in (f.get("aliases") or ())),
                        )
                    )
                masks = [f.mask for f in fields]
                if sum(masks) != _or(masks):
                    raise ValueError("overlapping fields")
                reset = r.get("reset")
                measured = r.get("measured_reset")
                regs.append(
                    Register(
                        addr=int(r["addr"]),
                        name=str(r["name"]),
                        width=width,
                        access=access,
                        reset=None if reset is None else int(reset),
                        description=str(r.get("description", "")),
                        fields=tuple(fields),
                        confirmed=bool(r.get("confirmed", False)),
                        reset_confirmed=bool(r.get("reset_confirmed", False)),
                        width_confirmed=bool(r.get("width_confirmed", True)),
                        volatile=bool(r.get("volatile", False)),
                        trigger=bool(r.get("trigger", False)),
                        source=str(r.get("source", "")),
                        reset_source=str(r.get("reset_source", "")),
                        measured_reset=None if measured is None else int(measured),
                        notes=str(r.get("notes", "")),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"regmap entry #{i} ({r.get('name', '?')}): {exc}") from exc
        return cls(regs, version=int(data.get("version", 1)))

    @classmethod
    def from_yaml(cls, text: str) -> RegisterMapSpec:
        return cls.from_dict(yaml.safe_load(text) or {})

    def __getitem__(self, key: int | str) -> Register:
        if isinstance(key, Register):
            return key
        if isinstance(key, int):
            try:
                return self._by_addr[key]
            except KeyError:
                raise KeyError(f"no register at 0x{key:02X}") from None
        try:
            return self._by_name[key.upper()]
        except KeyError:
            raise KeyError(f"no register named {key!r}") from None

    def __contains__(self, key: object) -> bool:
        if isinstance(key, int):
            return key in self._by_addr
        return isinstance(key, str) and key.upper() in self._by_name

    def __iter__(self) -> Iterator[Register]:
        return iter(self.registers)

    def __len__(self) -> int:
        return len(self.registers)

    def resolve(self, ref: str) -> tuple[Register, Field | None]:
        """``"FILTER.LINEF"`` -> (FILTER, LINEF); ``"FILTER"`` / ``"0x08"`` -> (FILTER, None)."""
        name, _, fname = ref.partition(".")
        try:
            key: int | str = int(name, 16) if name.lower().startswith("0x") else name
        except ValueError:
            key = name
        reg = self[key]
        return reg, (reg.field(fname) if fname else None)

    def coverage(self) -> dict[str, int]:
        """Counts of confirmed vs to-do items, for progress tracking."""
        fields = [f for r in self.registers for f in r.fields]
        return {
            "registers": len(self.registers),
            "registers_confirmed": sum(r.confirmed for r in self.registers),
            "widths_unconfirmed": sum(not r.width_confirmed for r in self.registers),
            "resets_known": sum(r.reset is not None for r in self.registers),
            "resets_confirmed": sum(r.reset_confirmed for r in self.registers),
            "resets_from_hardware_only": sum(r.reset_source == "hardware" for r in self.registers),
            "resets_disagreeing_with_hardware": sum(
                r.measured_reset is not None for r in self.registers
            ),
            "fields": len(fields),
            "fields_confirmed": sum(f.confirmed for f in fields),
        }


def _or(values: Iterable[int]) -> int:
    out = 0
    for v in values:
        out |= v
    return out


@cache
def load_regmap(path: str | Path | None = None) -> RegisterMapSpec:
    """Load the packaged ``regmap.yaml`` (or ``path``). Cached per path."""
    if path is None:
        text = resources.files("max11410_evkit.device").joinpath("regmap.yaml").read_text("utf-8")
    else:
        text = Path(path).read_text("utf-8")
    return RegisterMapSpec.from_yaml(text)


# --------------------------------------------------------------------------- live view
class FieldValue(int):
    """An int that also knows its enum label; ``== "SINC4"`` compares by label."""

    label: str | None

    def __new__(cls, value: int, label: str | None = None) -> FieldValue:
        obj = super().__new__(cls, value)
        obj.label = label
        return obj

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.label is not None and self.label.upper() == other.upper()
        return int(self) == other

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    __hash__ = int.__hash__

    def __repr__(self) -> str:
        return f"{int(self)}({self.label})" if self.label else str(int(self))


class RegisterMap:
    """Register access by name with a shadow cache, on top of an EvKitBoard."""

    def __init__(self, board: EvKitBoard, spec: RegisterMapSpec | None = None) -> None:
        self.board = board
        self.spec = spec or load_regmap()
        self.cache: dict[int, int] = {}
        self._lock = threading.RLock()

    # -- raw access ------------------------------------------------------------
    def reg(self, key: int | str | Register) -> Register:
        return key if isinstance(key, Register) else self.spec[key]

    def read(self, key: int | str | Register) -> int:
        """Read from hardware (updates the cache)."""
        r = self.reg(key)
        if not r.readable:
            raise PermissionError(f"{r.name} is write-only")
        with self._lock:
            v = self.board.read_register(r.addr, r.width)
            self.cache[r.addr] = v
            return v

    def write(self, key: int | str | Register, value: int) -> None:
        r = self.reg(key)
        if not r.writable:
            raise PermissionError(f"{r.name} is read-only")
        if not 0 <= value <= r.max_value:
            raise ValueError(f"{r.name}: 0x{value:X} does not fit in {r.width} byte(s)")
        with self._lock:
            self.board.write_register(r.addr, value, r.width)
            self.cache[r.addr] = value

    def get(self, key: int | str | Register) -> int:
        """Cached value for non-volatile registers, otherwise a hardware read."""
        r = self.reg(key)
        with self._lock:
            if not r.volatile and r.addr in self.cache:
                return self.cache[r.addr]
            return self.read(r)

    def invalidate(self, key: int | str | Register | None = None) -> None:
        with self._lock:
            if key is None:
                self.cache.clear()
            else:
                self.cache.pop(self.reg(key).addr, None)

    # -- fields ----------------------------------------------------------------
    def read_field(
        self, key: int | str | Register, field_name: str, *, fresh: bool = False
    ) -> FieldValue:
        r = self.reg(key)
        f = r.field(field_name)
        v = self.read(r) if fresh else self.get(r)
        raw = f.decode(v)
        return FieldValue(raw, f.label(raw))

    def write_field(
        self, key: int | str | Register, field_name: str, value: int | str | bool
    ) -> int:
        """Read-modify-write one field; returns the new register value."""
        return self.update(key, **{field_name: value})

    def update(self, key: int | str | Register, **fields: int | str | bool) -> int:
        """Read-modify-write several fields of one register in a single SPI write."""
        r = self.reg(key)
        with self._lock:
            base = 0 if r.trigger else self.get(r)
            new = base
            for name, val in fields.items():
                f = r.field(name)
                if not f.writable:
                    raise PermissionError(f"{r.name}.{f.name} is read-only")
                new = f.insert(new, val)
            if r.trigger or new != base or r.addr not in self.cache:
                self.write(r, new)
            return new

    # -- bulk ------------------------------------------------------------------
    def readable_registers(self, *, include_sequencer: bool = False) -> list[Register]:
        return [r for r in self.spec if r.readable and (include_sequencer or r.addr <= 0x39)]

    def read_all(
        self, *, include_sequencer: bool = False, include_volatile: bool = True
    ) -> dict[str, int]:
        """Read every readable register. Note that reading STATUS clears its ready bits."""
        out: dict[str, int] = {}
        for r in self.readable_registers(include_sequencer=include_sequencer):
            if r.volatile and not include_volatile:
                continue
            out[r.name] = self.read(r)
        return out

    def write_all(self, values: Mapping[str | int, int]) -> list[str]:
        """Write configuration registers from a snapshot (e.g. from :meth:`read_all`).

        Read-only, write-trigger (CONV_START, CAL_START, SEQ_START) and volatile registers
        are skipped. Returns the names written, in address order.
        """
        items = sorted(((self.reg(k), v) for k, v in values.items()), key=lambda rv: rv[0].addr)
        written = []
        for r, v in items:
            if not r.writable or r.trigger or r.volatile:
                continue
            self.write(r, v)
            written.append(r.name)
        return written

    def dump(self, values: Mapping[str, int] | None = None) -> str:
        """Human-readable table with field decode. ``values`` defaults to the cache."""
        if values is None:
            values = {self.spec[a].name: v for a, v in sorted(self.cache.items())}
        return format_dump(self.spec, values)

    @staticmethod
    def diff(
        old: Mapping[str, int], new: Mapping[str, int]
    ) -> list[tuple[str, int | None, int | None]]:
        return diff_snapshots(old, new)

    # -- attribute sugar: regs.FILTER.LINEF = "SINC4" ---------------------------
    def __getattr__(self, name: str) -> RegisterProxy:
        if name.startswith("_"):
            raise AttributeError(name)
        spec = self.__dict__.get("spec")
        if spec is None or name not in spec:
            raise AttributeError(f"no register named {name!r}")
        return RegisterProxy(self, spec[name])

    def __getitem__(self, key: int | str) -> RegisterProxy:
        return RegisterProxy(self, self.spec[key])

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | {r.name for r in self.spec})


class RegisterProxy:
    """``regs.FILTER``: field attributes read/write through the owning RegisterMap."""

    __slots__ = ("_map", "_reg")

    def __init__(self, regmap: RegisterMap, reg: Register) -> None:
        object.__setattr__(self, "_map", regmap)
        object.__setattr__(self, "_reg", reg)

    @property
    def register(self) -> Register:
        return self._reg

    @property
    def value(self) -> int:
        return self._map.get(self._reg)

    @value.setter
    def value(self, v: int) -> None:
        self._map.write(self._reg, v)

    def read(self) -> int:
        return self._map.read(self._reg)

    def write(self, v: int) -> None:
        self._map.write(self._reg, v)

    def update(self, **fields: int | str | bool) -> int:
        return self._map.update(self._reg, **fields)

    def __getattr__(self, name: str) -> FieldValue:
        reg = object.__getattribute__(self, "_reg")
        if reg.has_field(name):
            return self._map.read_field(reg, name)
        raise AttributeError(f"{reg.name} has no field {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "value":
            object.__setattr__(self, name, value)  # routes to the property setter
            return
        if self._reg.has_field(name):
            self._map.write_field(self._reg, name, value)
            return
        raise AttributeError(f"{self._reg.name} has no field {name!r}")

    def __dir__(self) -> list[str]:
        return ["value", "read", "write", "update", "register", *(f.name for f in self._reg.fields)]

    def __repr__(self) -> str:
        return f"<{self._reg.name} @0x{self._reg.addr:02X}>"


# --------------------------------------------------------------------------- helpers
def format_dump(spec: RegisterMapSpec, values: Mapping[str, int]) -> str:
    lines = []
    for r in spec:
        if r.name not in values:
            continue
        v = values[r.name]
        flags = ""
        if r.reset is not None and v != r.reset:
            flags = " *"  # differs from reset
        decoded = r.describe(v, compact=True)
        lines.append(f"0x{r.addr:02X} {r.name:<13} {r.fmt(v):>8}{flags:<2} {decoded}".rstrip())
    return "\n".join(lines)


def diff_snapshots(
    old: Mapping[str, int], new: Mapping[str, int]
) -> list[tuple[str, int | None, int | None]]:
    """Registers whose value differs: [(name, old or None, new or None)], in key order."""
    names = list(dict.fromkeys([*old, *new]))
    return [(n, old.get(n), new.get(n)) for n in names if old.get(n) != new.get(n)]

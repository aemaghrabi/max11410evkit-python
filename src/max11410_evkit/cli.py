"""``max11410-evkit`` command-line smoke test.

    max11410-evkit list
    max11410-evkit info                 # firmware 1.1, MAX11410EVK, echo, PART_ID
    max11410-evkit peek 11              # register 0x11, width from the register map
    max11410-evkit peek 3A 2            # explicit width (sequencer registers)
    max11410-evkit poke 0B 20           # write 0x20 to MUX_CTRL0
    max11410-evkit regs                 # dump 0x00-0x39 with field decode (--all: +sequencer)
    max11410-evkit get FILTER.LINEF     # one field (or a whole register: get CTRL)
    max11410-evkit set FILTER.LINEF SINC4
    max11410-evkit read --ch 0 -n 5     # single conversions (EVKIT default: AIN2-GND, REF1 2.5 V)
    max11410-evkit selfcal              # self + PGA calibration at channel 0's filter/rate
    max11410-evkit --fake info          # same, against the emulated board

peek/poke take hexadecimal addresses and values (with or without 0x). get/set take
register names (or 0xNN) and, for set, an enum name or a number (0x.. for hex).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import replace

from max11410_evkit import __version__
from max11410_evkit.board.evkit import (
    PROJECT_NAME,
    REG_PART_ID,
    EvKitBoard,
    connect,
    register_width,
)
from max11410_evkit.transport.errors import EvKitError


def _hex(text: str) -> int:
    try:
        return int(text, 16)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a hex number: {text!r}") from None


def _open(args: argparse.Namespace) -> EvKitBoard:
    if args.fake:
        from max11410_evkit.transport.fake import FakeEvKitTransport

        return connect(transport=FakeEvKitTransport())
    return connect(args.path)


def _width(reg: int, width: int | None) -> int:
    w = width if width is not None else register_width(reg)
    if w is None:
        raise SystemExit(f"width of register 0x{reg:02X} is not known; give it explicitly")
    return w


def cmd_list(args: argparse.Namespace) -> int:
    if args.fake:
        print("fake MAX11410EVKIT  path=fake")
        return 0
    from max11410_evkit.transport.hid import list_boards

    boards = list_boards()
    if not boards:
        print("no MAX11410EVKIT found (VID 0x0B6A, PID 0x2B31)", file=sys.stderr)
        return 1
    for b in boards:
        print(b)
    return 0


def cmd_info(board: EvKitBoard) -> int:
    ok = True
    ver = board.version()
    print(f"firmware : {ver}")
    name = board.project()
    print(f"project  : {name}")
    ok &= name == PROJECT_NAME
    pattern = bytes(os.urandom(8))
    echoed = board.echo(pattern)
    echo_ok = echoed == pattern
    print(f"echo     : {'ok' if echo_ok else 'MISMATCH'} ({pattern.hex(' ')})")
    ok &= echo_ok
    part = board.read_register(REG_PART_ID, 3)
    print(f"PART_ID  : 0x{part:06X}")
    return 0 if ok else 1


def cmd_peek(board: EvKitBoard, reg: int, width: int | None) -> int:
    w = _width(reg, width)
    v = board.read_register(reg, w)
    print(f"0x{reg:02X} = 0x{v:0{2 * w}X}")
    return 0


def cmd_poke(board: EvKitBoard, reg: int, value: int, width: int | None) -> int:
    w = _width(reg, width)
    board.write_register(reg, value, w)
    back = board.read_register(reg, w)
    print(f"0x{reg:02X} <- 0x{value:0{2 * w}X}   readback 0x{back:0{2 * w}X}")
    return 0


def _device(board: EvKitBoard):
    from max11410_evkit.device.max11410 import MAX11410

    return MAX11410(board)


def cmd_regs(board: EvKitBoard, include_sequencer: bool) -> int:
    dev = _device(board)
    snap = dev.regs.read_all(include_sequencer=include_sequencer)
    print(dev.regs.dump(snap))
    print("(* = differs from the datasheet reset value)")
    for r in dev.regs.spec:
        if r.measured_reset is not None and r.name in snap:
            print(
                f"note: {r.name} datasheet reset {r.fmt(r.reset)}, EVKIT measured "
                f"{r.fmt(r.measured_reset)}"
            )
    return 0


def cmd_get(board: EvKitBoard, ref: str) -> int:
    dev = _device(board)
    reg, field = dev.regs.spec.resolve(ref)
    if field is None:
        v = dev.regs.read(reg)
        print(f"{reg.name} = {reg.fmt(v)}  {reg.describe(v)}".rstrip())
    else:
        fv = dev.regs.read_field(reg, field.name, fresh=True)
        print(f"{reg.name}.{field.name} = {int(fv)}" + (f" ({fv.label})" if fv.label else ""))
    return 0


def cmd_set(board: EvKitBoard, ref: str, value: str) -> int:
    dev = _device(board)
    reg, field = dev.regs.spec.resolve(ref)
    if field is None:
        dev.regs.write(reg, int(value, 0))
    else:
        dev.regs.write_field(reg, field.name, value)
    back = dev.regs.read(reg)
    print(f"{reg.name} = {reg.fmt(back)}  {reg.describe(back)}".rstrip())
    return 0


def cmd_read(board: EvKitBoard, args: argparse.Namespace) -> int:
    dev = _device(board)
    dev.init()
    cfg = dev.channels[args.ch]
    overrides = {
        k: getattr(args, k)
        for k in ("ainp", "ainn", "refsel", "vref_volts", "rate", "linef", "gain", "path")
        if getattr(args, k) is not None
    }
    if args.bipolar:
        overrides["bipolar"] = True
    if overrides.get("gain", 1) != 1 and "path" not in overrides:
        overrides["path"] = "pga"
    cfg = replace(cfg, enabled=True, **overrides).validate()
    dev.set_channel(args.ch, cfg)
    print(
        f"ch{args.ch}: AINP={cfg.ainp} AINN={cfg.ainn} REFSEL={cfg.refsel} Vref={cfg.vref_volts} V "
        f"{'bipolar' if cfg.bipolar else 'unipolar'} {cfg.path} x{cfg.gain} "
        f"{cfg.linef} rate[{cfg.rate}]={cfg.data_rate():g} sps continuous / "
        f"{cfg.data_rate(mode='single'):g} sps single-cycle"
    )
    for _ in range(args.n):
        r = dev.read_single(args.ch, method=args.method)
        print(f"0x{r.raw:06X}  {r.volts:+.7f} V")
    if args.method == "auto" and dev.board_wait_ok is False:
        print("note: board-side data-ready wait failed; used STATUS polling", file=sys.stderr)
    return 0


def cmd_selfcal(board: EvKitBoard, pga: bool, fast: bool) -> int:
    from max11410_evkit.device.max11410 import FILTER_RATE_MAX

    dev = _device(board)
    dev.init()
    # Datasheet: calibrate at (or slower than) the rate used for conversions.
    cfg = dev.apply_channel(0)
    print(
        f"calibrating at {cfg.linef} rate[{FILTER_RATE_MAX if fast else cfg.rate}]"
        + (" (fast, Linux driver style)" if fast else "")
    )
    t0 = time.monotonic()
    dev.self_calibrate(pga=pga, rate=FILTER_RATE_MAX if fast else None)
    dt = time.monotonic() - t0
    for name in ("SELF_OFF", *(f"SELF_GAIN_{1 << i}" for i in range(8))):
        reg = dev.regs.spec[name]
        print(f"{name:<14} {reg.fmt(dev.regs.read(reg))}")
    print(f"self-calibration done in {dt:.2f} s")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="max11410-evkit",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--fake", action="store_true", help="use the emulated board")
    ap.add_argument("--path", help="HID path of the board (from 'list')")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every command")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list attached boards")
    sub.add_parser("info", help="firmware version, project name, echo test, PART_ID")
    p = sub.add_parser("peek", help="read a register")
    p.add_argument("reg", type=_hex)
    p.add_argument("width", type=int, nargs="?")
    p = sub.add_parser("poke", help="write a register")
    p.add_argument("reg", type=_hex)
    p.add_argument("value", type=_hex)
    p.add_argument("width", type=int, nargs="?")
    p = sub.add_parser("regs", help="dump registers with field decode")
    p.add_argument("--all", action="store_true", help="include sequencer registers 0x3A-0x6F")
    p = sub.add_parser("get", help="read REG or REG.FIELD by name")
    p.add_argument("ref")
    p = sub.add_parser("set", help="write REG or REG.FIELD (enum name or number)")
    p.add_argument("ref")
    p.add_argument("value")
    p = sub.add_parser("read", help="single conversions on a channel")
    p.add_argument("--ch", type=int, default=0, help="channel slot 0..7 (default 0)")
    p.add_argument("-n", type=int, default=1, help="number of conversions")
    p.add_argument("--method", choices=["auto", "board", "poll"], default="auto")
    p.add_argument("--ainp", type=int)
    p.add_argument("--ainn", type=int, help="10 = GND")
    p.add_argument("--refsel", type=int, help="0..6 (1 = REF1, the EVKIT 2.5 V reference)")
    p.add_argument("--vref", dest="vref_volts", type=float)
    p.add_argument("--rate", type=int, help="FILTER.RATE index")
    p.add_argument("--linef", choices=["FIR5060", "FIR50", "FIR60", "SINC4"])
    p.add_argument("--gain", type=int, choices=[1, 2, 4, 8, 16, 32, 64, 128])
    p.add_argument("--path", choices=["buffered", "bypass", "pga"])
    p.add_argument("--bipolar", action="store_true")
    p = sub.add_parser("selfcal", help="run the self + PGA calibration sequence")
    p.add_argument("--no-pga", action="store_true", help="offset/self calibration only")
    p.add_argument("--fast", action="store_true", help="calibrate at FILTER.RATE 0xF (driver)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(name)s: %(message)s",
    )
    try:
        if args.cmd == "list":
            return cmd_list(args)
        board = _open(args)
        try:
            if args.cmd == "info":
                return cmd_info(board)
            if args.cmd == "peek":
                return cmd_peek(board, args.reg, args.width)
            if args.cmd == "poke":
                return cmd_poke(board, args.reg, args.value, args.width)
            if args.cmd == "regs":
                return cmd_regs(board, args.all)
            if args.cmd == "get":
                return cmd_get(board, args.ref)
            if args.cmd == "set":
                return cmd_set(board, args.ref, args.value)
            if args.cmd == "read":
                return cmd_read(board, args)
            if args.cmd == "selfcal":
                return cmd_selfcal(board, not args.no_pga, args.fast)
        finally:
            board.close()
    except (EvKitError, ValueError, KeyError, PermissionError) as exc:
        msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"error: {msg}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

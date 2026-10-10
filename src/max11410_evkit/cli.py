"""``max11410-evkit`` command-line smoke test.

    max11410-evkit list
    max11410-evkit info                 # firmware 1.1, MAX11410EVK, echo, PART_ID
    max11410-evkit peek 11              # register 0x11, width from the register map
    max11410-evkit peek 3A 2            # explicit width (sequencer registers)
    max11410-evkit poke 0B 20           # write 0x20 to MUX_CTRL0
    max11410-evkit --fake info          # same, against the emulated board

Register addresses and values are hexadecimal (with or without 0x).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

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
        finally:
            board.close()
    except (EvKitError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
    max11410-evkit capture -n 1000      # block capture + statistics (alias: stats)
    max11410-evkit capture -n 50000 -o cap.csv   # any length: split into SRAM-sized blocks
    max11410-evkit hist -n 4096         # code histogram + noise / resolution figures
    max11410-evkit fft -n 8192 --window bh4      # spectrum metrics (one gap-free block)
    max11410-evkit --fake info          # same, against the emulated board

peek/poke take hexadecimal addresses and values (with or without 0x). get/set take
register names (or 0xNN) and, for set, an enum name or a number (0x.. for hex).
read/capture/hist/fft take the channel options (--ainp, --rate, --gain, ...); note that the
signal-path option after the command (--path pga) is not the HID --path before it.
Ctrl-C during a capture cancels it after the current block and discards all its data.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import replace

from max11410_evkit import __version__
from max11410_evkit.analysis.fft import DEFAULT_WINDOW
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


_CHANNEL_KEYS = ("ainp", "ainn", "refsel", "vref_volts", "rate", "linef", "gain")


def _setup_channel(dev, args: argparse.Namespace):
    """Apply the channel options to slot ``args.ch`` and print a one-line description."""
    cfg = dev.channels[args.ch]
    overrides = {k: getattr(args, k) for k in _CHANNEL_KEYS if getattr(args, k) is not None}
    if args.sig_path is not None:
        overrides["path"] = args.sig_path
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
    return cfg


def cmd_read(board: EvKitBoard, args: argparse.Namespace) -> int:
    dev = _device(board)
    dev.init()
    _setup_channel(dev, args)
    for _ in range(args.n):
        r = dev.read_single(args.ch, method=args.method)
        print(f"0x{r.raw:06X}  {r.volts:+.7f} V")
    if args.method == "auto" and dev.board_wait_ok is False:
        print("note: board-side data-ready wait failed; used STATUS polling", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- P3: capture / hist / fft
def _bits(v: float | None) -> str:
    return "n/a (noise < 1 LSB)" if v is None else f"{v:.2f} bits"


def _print_stats(st) -> None:
    print(f"samples        : {st.n}")
    print(f"average        : {st.average:+.9f} V   (code {st.average_code:.1f})")
    print(f"std dev (rms)  : {st.std_dev * 1e6:.3f} uV   ({st.std_dev_codes:.2f} LSB)")
    print(f"min / max      : {st.min:+.9f} / {st.max:+.9f} V")
    print(f"peak-to-peak   : {st.p2p * 1e6:.3f} uV   ({st.p2p_codes} LSB)")
    print(f"FSR            : {st.fsr:g} V")
    print(f"effective res. : {_bits(st.effective_resolution)}")
    print(f"noise-free res.: {_bits(st.noise_free_resolution)} (6.6 sigma, datasheet)")
    print(f"noise-free res.: {_bits(st.noise_free_resolution_measured)} (measured p-p)")


def _run_capture(board: EvKitBoard, args: argparse.Namespace, *, gap_free: bool = False):
    """init, apply the channel options, capture with Ctrl-C = cancel and a progress line."""
    from max11410_evkit.acquisition import acquire

    dev = _device(board)
    dev.init()
    _setup_channel(dev, args)
    cancel = threading.Event()
    tty = sys.stderr.isatty()

    def on_sigint(_sig, _frm) -> None:
        cancel.set()
        print("\ncancelling after the current block (all data discarded) ...", file=sys.stderr)

    def progress(done: int, total: int) -> None:
        if tty:
            print(f"\rreading back {done}/{total} samples", end="", file=sys.stderr, flush=True)

    mode = "continuous" if not args.single else "single-cycle"
    print(f"capturing {args.n} samples ({mode}) ...")
    in_main = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGINT, on_sigint) if in_main else None
    try:
        cap = acquire(
            dev,
            args.ch,
            args.n,
            continuous=not args.single,
            block_samples=getattr(args, "block", None),
            gap_free=gap_free,
            progress=progress,
            cancel=cancel,
        )
    finally:
        if in_main:
            signal.signal(signal.SIGINT, previous)
        if tty:
            print(file=sys.stderr)
    blocks = len(cap.block_starts)
    print(
        f"captured {cap.n} samples in {blocks} block{'s' if blocks != 1 else ''} "
        f"({cap.duration_s:.2f} s, nominal {cap.sample_rate:g} sps)"
        + ("  [CANCELLED, data discarded]" if cap.cancelled else "")
    )
    if blocks > 1:
        print("note: there are time gaps between blocks (fine for statistics, not for an FFT)")
    return cap


def cmd_capture(board: EvKitBoard, args: argparse.Namespace) -> int:
    from max11410_evkit.export import save_capture_csv

    cap = _run_capture(board, args)
    if cap.n == 0:
        return 1
    st = cap.stats()
    _print_stats(st)
    if args.output:
        print(f"saved {save_capture_csv(args.output, cap, stats=st)}")
    return 1 if cap.cancelled else 0


HIST_PRINT_MAX_BINS = 40


def cmd_hist(board: EvKitBoard, args: argparse.Namespace) -> int:
    from max11410_evkit.export import save_histogram_csv

    cap = _run_capture(board, args)
    if cap.n == 0:
        return 1
    st = cap.stats()
    hist = cap.histogram(max_bins=args.bins)
    _print_stats(st)
    nb = hist.counts.size
    width = f", {hist.bin_width} codes per bin" if hist.bin_width > 1 else ""
    print(f"histogram      : {nb} bins{width}")
    if nb <= HIST_PRINT_MAX_BINS:
        peak = int(hist.counts.max())
        for code, count in zip(hist.bin_codes.tolist(), hist.counts.tolist(), strict=True):
            bar = "#" * round(40 * count / peak) if peak else ""
            print(f"  0x{code:06X} {count:7d} {bar}")
    else:
        print(f"  (more than {HIST_PRINT_MAX_BINS} bins; use -o to save them)")
    if args.output:
        print(f"saved {save_histogram_csv(args.output, cap, hist, stats=st)}")
    return 1 if cap.cancelled else 0


def cmd_fft(board: EvKitBoard, args: argparse.Namespace) -> int:
    from max11410_evkit.acquisition import max_fft_samples
    from max11410_evkit.export import save_spectrum_csv

    limit = max_fft_samples(board)
    if args.n & (args.n - 1) or not 16 <= args.n <= limit:
        raise ValueError(f"FFT length must be a power of 2 from 16 to {limit}")
    cap = _run_capture(board, args, gap_free=True)
    if cap.cancelled or cap.n == 0:
        return 1
    spec = cap.spectrum(window=args.window)
    m = spec.metrics
    print(f"window         : {spec.window}, {spec.n} points, bin {cap.sample_rate / spec.n:.4g} Hz")
    print(f"fundamental    : {m.fundamental_hz:.4f} Hz at {m.fundamental_dbfs:.2f} dBFS")
    print(f"SNR            : {m.snr_db:.2f} dB")
    print(f"THD            : {m.thd_db:.2f} dB (harmonics 2-6)")
    print(f"SINAD          : {m.sinad_db:.2f} dB")
    print(f"SFDR           : {m.sfdr_db:.2f} dB")
    print(f"ENOB           : {m.enob:.2f} bits")
    print(f"noise floor    : {m.noise_floor_dbfs:.2f} dBFS per bin")
    for h in m.harmonics:
        level = "skipped (falls on DC or another tone)" if h.dbc is None else f"{h.dbc:.2f} dBc"
        print(f"  H{h.order} {h.freq_hz:10.4f} Hz  {level}")
    if not m.has_test_tone():
        from max11410_evkit.analysis.fft import MIN_TONE_DBFS

        print(
            f"warning: no test tone (fundamental {m.fundamental_dbfs:.1f} dBFS < "
            f"{MIN_TONE_DBFS:g} dBFS); SNR, THD, SINAD, SFDR and ENOB do not apply. "
            "Apply a sine close to full scale."
        )
    if args.output:
        print(f"saved {save_spectrum_csv(args.output, cap, spec)}")
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


def _channel_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--ch", type=int, default=0, help="channel slot 0..7 (default 0)")
    p.add_argument("--ainp", type=int)
    p.add_argument("--ainn", type=int, help="10 = GND")
    p.add_argument("--refsel", type=int, help="0..6 (1 = REF1, the EVKIT 2.5 V reference)")
    p.add_argument("--vref", dest="vref_volts", type=float)
    p.add_argument("--rate", type=int, help="FILTER.RATE index")
    p.add_argument("--linef", choices=["FIR5060", "FIR50", "FIR60", "SINC4"])
    p.add_argument("--gain", type=int, choices=[1, 2, 4, 8, 16, 32, 64, 128])
    # dest differs from the global HID --path (a shared dest would overwrite it)
    p.add_argument("--path", dest="sig_path", choices=["buffered", "bypass", "pga"])
    p.add_argument("--bipolar", action="store_true")


def _capture_args(p: argparse.ArgumentParser, *, default_n: int) -> None:
    p.add_argument("-n", type=int, default=default_n, help=f"samples (default {default_n})")
    p.add_argument(
        "--single", action="store_true", help="single-cycle conversions (default: continuous)"
    )


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
    _channel_args(p)
    p.add_argument("-n", type=int, default=1, help="number of conversions")
    p.add_argument("--method", choices=["auto", "board", "poll"], default="auto")
    p = sub.add_parser(
        "capture", aliases=["stats"], help="block capture of any length + statistics"
    )
    _channel_args(p)
    _capture_args(p, default_n=1000)
    p.add_argument(
        "--block", type=int, help="samples per board block (default: as many as the SRAM holds)"
    )
    p.add_argument("-o", "--output", help="save the samples as CSV")
    p = sub.add_parser("hist", help="capture + code histogram")
    _channel_args(p)
    _capture_args(p, default_n=4096)
    p.add_argument("--block", type=int, help="samples per board block")
    p.add_argument("--bins", type=int, default=4096, help="maximum number of bins (default 4096)")
    p.add_argument("-o", "--output", help="save the histogram as CSV")
    p = sub.add_parser("fft", help="gap-free capture + FFT dynamic performance")
    _channel_args(p)
    _capture_args(p, default_n=1024)
    p.add_argument(
        "--window",
        default=DEFAULT_WINDOW,
        help="blackman-harris-4 (bh4, default), hann, flat-top, rectangular (rect)",
    )
    p.add_argument("-o", "--output", help="save the spectrum as CSV")
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
            if args.cmd in ("capture", "stats"):
                return cmd_capture(board, args)
            if args.cmd == "hist":
                return cmd_hist(board, args)
            if args.cmd == "fft":
                return cmd_fft(board, args)
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

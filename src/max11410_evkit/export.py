"""CSV files for captures, histograms and spectra ("Save Graph").

Layout (our own; there is no reference file from the Windows software):

    # MAX11410 EVKIT capture                  <- comment block: "# key, value" lines
    # software, max11410-evkit 0.1.0
    # ...
    index,block,time_s,code,volts           <- one header row, then the data

``block`` is the 0x42 block a sample came from; a gap in time exists before every new
block number. ``time_s`` is nominal (index / data rate) and does not include those gaps.
Opens directly in Excel/LibreOffice/pandas (``pandas.read_csv(path, comment="#")``).
"""

from __future__ import annotations

import csv
import io
import math
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from max11410_evkit import __version__
from max11410_evkit.acquisition import Capture
from max11410_evkit.analysis import Histogram, Spectrum, Stats
from max11410_evkit.device.max11410 import INPUT_GND, REFSEL_NAMES, ChannelConfig


def _ain(code: int, *, negative: bool) -> str:
    if code <= 9:
        return f"AIN{code}"
    if code == INPUT_GND:
        return "GND" if negative else "AVDD"
    return "unconnected"


def config_lines(cfg: ChannelConfig, *, continuous: bool) -> list[tuple[str, str]]:
    mode = "continuous" if continuous else "single"
    return [
        ("channel_name", cfg.name),
        ("ainp", _ain(cfg.ainp, negative=False)),
        ("ainn", _ain(cfg.ainn, negative=True)),
        ("reference", f"{REFSEL_NAMES[cfg.refsel]} {cfg.vref_volts:g} V"),
        ("polarity", "bipolar (offset binary)" if cfg.bipolar else "unipolar"),
        ("signal_path", f"{cfg.path} x{cfg.gain}"),
        ("filter", f"{cfg.linef} rate[{cfg.rate}]"),
        ("conversion_mode", mode),
        ("data_rate_sps", f"{cfg.data_rate(mode=mode):g}"),
    ]


def _fmt(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return "inf" if math.isinf(v) else f"{v:.9g}"
    return str(v)


def stats_lines(stats: Stats) -> list[tuple[str, str]]:
    return [(f"stats.{k}", _fmt(v)) for k, v in stats.to_dict().items()]


def _header(title: str, items: Iterable[tuple[str, str]]) -> str:
    out = [f"# {title}", f"# software, max11410-evkit {__version__}"]
    out += [f"# {k}, {v}" for k, v in items]
    return "\n".join(out) + "\n"


def _capture_items(cap: Capture) -> list[tuple[str, str]]:
    return [
        ("timestamp", cap.started_at.isoformat(timespec="seconds")),
        ("channel", str(cap.channel)),
        *config_lines(cap.config, continuous=cap.continuous),
        ("lsb_volts", f"{cap.lsb_volts:.9g}"),
        ("fsr_volts", f"{cap.fsr:.9g}"),
        ("samples", str(cap.n)),
        ("blocks", str(len(cap.block_starts))),
        ("block_starts", " ".join(str(b) for b in cap.block_starts)),
        ("cancelled", str(cap.cancelled).lower()),
    ]


def _write(path: str | Path, text: str) -> Path:
    path = Path(path)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def capture_csv(cap: Capture, *, stats: Stats | None = None) -> str:
    items = _capture_items(cap) + (stats_lines(stats) if stats is not None else [])
    buf = io.StringIO()
    buf.write(_header("MAX11410 EVKIT capture", items))
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["index", "block", "time_s", "code", "volts"])
    blocks, times = cap.block_index, cap.times
    for i in range(cap.n):
        w.writerow([i, int(blocks[i]), f"{times[i]:.9g}", int(cap.codes[i]), f"{cap.volts[i]:.9e}"])
    return buf.getvalue()


def save_capture_csv(path: str | Path, cap: Capture, *, stats: Stats | None = None) -> Path:
    """Scope / DMM data: one row per sample."""
    return _write(path, capture_csv(cap, stats=stats))


def histogram_csv(cap: Capture, hist: Histogram, *, stats: Stats | None = None) -> str:
    items = _capture_items(cap) + [("bin_width_codes", str(hist.bin_width))]
    items += stats_lines(stats) if stats is not None else []
    buf = io.StringIO()
    buf.write(_header("MAX11410 EVKIT histogram", items))
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["code", "volts", "count"])
    lsb, offset = cap.lsb_volts, (1 << 23) if cap.config.bipolar else 0
    for code, count in zip(hist.bin_codes.tolist(), hist.counts.tolist(), strict=True):
        w.writerow([code, f"{(code - offset) * lsb:.9e}", count])
    return buf.getvalue()


def save_histogram_csv(
    path: str | Path, cap: Capture, hist: Histogram, *, stats: Stats | None = None
) -> Path:
    """Histogram: one row per code bin (empty bins included)."""
    return _write(path, histogram_csv(cap, hist, stats=stats))


def spectrum_csv(cap: Capture, spec: Spectrum) -> str:
    m = spec.metrics
    items = _capture_items(cap) + [("window", spec.window), ("fft_points", str(spec.n))]
    items += [(f"metrics.{k}", _fmt(v)) for k, v in m.to_dict().items() if k != "harmonics"]
    items += [
        (f"metrics.harmonic_{h.order}", f"{h.freq_hz:.6g} Hz {_fmt(h.dbc)} dBc")
        for h in m.harmonics
    ]
    buf = io.StringIO()
    buf.write(_header("MAX11410 EVKIT FFT", items))
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["freq_hz", "magnitude_dbfs"])
    for f, db in zip(spec.freqs_hz.tolist(), spec.mag_dbfs.tolist(), strict=True):
        w.writerow([f"{f:.9g}", f"{db:.4f}"])
    return buf.getvalue()


def save_spectrum_csv(path: str | Path, cap: Capture, spec: Spectrum) -> Path:
    """FFT: one row per frequency bin, metrics in the header."""
    return _write(path, spectrum_csv(cap, spec))


def read_csv(path: str | Path) -> tuple[dict[str, str], dict[str, np.ndarray]]:
    """Read back any of the files above: (header items, columns as arrays)."""
    meta: dict[str, str] = {}
    rows: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            key, _, value = line[1:].strip().partition(", ")
            if value or _:
                meta[key] = value
        elif line:
            rows.append(line)
    reader = csv.reader(rows)
    names = next(reader)
    data = list(reader)
    cols = {
        name: np.array([float(r[i]) if r[i] != "" else math.nan for r in data])
        for i, name in enumerate(names)
    }
    return meta, cols

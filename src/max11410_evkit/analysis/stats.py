"""Time-domain statistics of a capture (the DMM / Scope / Histogram statistics panels)."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

from max11410_evkit.device.max11410 import ChannelConfig

# Datasheet "Noise Performance": "p-p noise is 6.6 times the input-referred rms noise".
NFR_PP_FACTOR = 6.6


def full_scale_range(cfg: ChannelConfig) -> float:
    """Full-scale input range in volts: 2*VREF/gain bipolar, VREF/gain unipolar."""
    span = 2.0 * cfg.vref_volts if cfg.bipolar else cfg.vref_volts
    return span / cfg.gain


def _bits(fsr: float, noise: float) -> float | None:
    """log2(FSR / noise), or None when the noise is zero (below one LSB)."""
    if noise <= 0.0 or not math.isfinite(noise):
        return None
    return math.log2(fsr / noise)


@dataclass(frozen=True)
class Stats:
    """Statistics of ``n`` samples. Voltages in V, ``*_codes`` in LSB, resolutions in bits.

    ``std_dev`` is the sample standard deviation (ddof = 1), used as the rms noise.
    A resolution is ``None`` when its noise figure is zero (every sample on one code).
    """

    n: int
    average: float
    std_dev: float
    min: float
    max: float
    p2p: float
    average_code: float
    std_dev_codes: float
    min_code: int
    max_code: int
    p2p_codes: int
    fsr: float
    effective_resolution: float | None
    noise_free_resolution: float | None  # datasheet: log2(FSR / (6.6 * rms))
    noise_free_resolution_measured: float | None  # log2(FSR / (max - min))

    def to_dict(self) -> dict:
        return asdict(self)


def compute_stats(codes: np.ndarray, volts: np.ndarray, cfg: ChannelConfig) -> Stats:
    """Statistics of one capture. ``codes`` and ``volts`` are the same samples."""
    codes = np.asarray(codes)
    volts = np.asarray(volts, dtype=float)
    if codes.shape != volts.shape or codes.ndim != 1:
        raise ValueError("codes and volts must be 1-D arrays of the same length")
    n = int(volts.size)
    if n == 0:
        raise ValueError("no samples")
    ddof = 1 if n > 1 else 0
    std = float(np.std(volts, ddof=ddof))
    vmin, vmax = float(volts.min()), float(volts.max())
    fsr = full_scale_range(cfg)
    return Stats(
        n=n,
        average=float(volts.mean()),
        std_dev=std,
        min=vmin,
        max=vmax,
        p2p=vmax - vmin,
        average_code=float(codes.mean()),
        std_dev_codes=float(np.std(codes.astype(float), ddof=ddof)),
        min_code=int(codes.min()),
        max_code=int(codes.max()),
        p2p_codes=int(codes.max() - codes.min()),
        fsr=fsr,
        effective_resolution=_bits(fsr, std),
        noise_free_resolution=_bits(fsr, NFR_PP_FACTOR * std),
        noise_free_resolution_measured=_bits(fsr, vmax - vmin),
    )

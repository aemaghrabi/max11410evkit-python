"""Code histogram (the Histogram tab)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

DEFAULT_MAX_BINS = 4096


@dataclass(frozen=True)
class Histogram:
    """Counts per code bin.

    ``bin_codes[i]`` is the first code of bin *i*; each bin covers ``bin_width`` codes.
    ``bin_width`` is 1 (one bar per code) unless the codes span more than ``max_bins``.
    """

    bin_codes: np.ndarray
    counts: np.ndarray
    bin_width: int

    @property
    def n(self) -> int:
        return int(self.counts.sum())


def code_histogram(codes: np.ndarray, *, max_bins: int = DEFAULT_MAX_BINS) -> Histogram:
    """Histogram of the codes from min to max, empty bins included."""
    codes = np.asarray(codes).astype(np.int64)
    if codes.size == 0:
        raise ValueError("no samples")
    if max_bins < 1:
        raise ValueError("max_bins must be >= 1")
    lo, hi = int(codes.min()), int(codes.max())
    span = hi - lo + 1
    width = -(-span // max_bins)  # ceil
    nbins = -(-span // width)
    counts = np.bincount((codes - lo) // width, minlength=nbins)
    return Histogram(lo + width * np.arange(nbins, dtype=np.int64), counts, width)

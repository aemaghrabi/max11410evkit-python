"""Captures of any length, split into blocks that fit the EVKIT's capture SRAM.

The firmware's 0x42 command stores samples in the board SRAM (32 KB, measured on the EVKIT;
see ``EVKIT_SRAM_BYTES``), so one block holds at most ``sram_bytes // 3`` samples (10,922).
:func:`acquire` hides that limit: it runs as many blocks as needed and joins them.

Between blocks the SRAM is read back over USB while the ADC is idle, so a multi-block
capture has **gaps** in time at every block boundary (``Capture.block_starts``). That is fine
for statistics and histograms but not for an FFT, which needs a single gap-free block
(``gap_free=True``; :func:`max_fft_samples` gives the largest power-of-2 length).

Cancellation (``cancel`` = a ``threading.Event`` or any callable returning bool) is checked
before every block and between SRAM read chunks. The firmware ignores commands during a 0x42
block, so a running block always finishes on the board. A cancelled acquisition discards
**all** data, completed blocks included (Ahmed, 2026-10-11), and returns an empty
``Capture`` with ``cancelled=True``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime

import numpy as np

from max11410_evkit.analysis import (
    DEFAULT_WINDOW,
    Histogram,
    Spectrum,
    Stats,
    analyze_spectrum,
    code_histogram,
    compute_stats,
    full_scale_range,
)
from max11410_evkit.board.evkit import MAX_SAMPLES, EvKitBoard
from max11410_evkit.device.max11410 import MAX11410, MID_SCALE, ChannelConfig, lsb_volts
from max11410_evkit.transport.errors import Cancelled

SAMPLE_BYTES = 3  # DATA<n> registers are 24-bit


def max_block_samples(board: EvKitBoard) -> int:
    """Samples per 0x42 block: limited by the board SRAM and the 16-bit sample count."""
    return min(MAX_SAMPLES, board.sram_bytes // SAMPLE_BYTES)


def max_fft_samples(board: EvKitBoard) -> int:
    """Largest power-of-2 gap-free capture (8,192 with 32 KB of SRAM)."""
    return 1 << (max_block_samples(board).bit_length() - 1)


def codes_to_volts(codes: np.ndarray, cfg: ChannelConfig) -> np.ndarray:
    """Vectorised :func:`max11410_evkit.device.raw_to_volts` (offset binary when bipolar)."""
    c = np.asarray(codes, dtype=np.int64)
    if cfg.bipolar:
        c = c - MID_SCALE
    return c * lsb_volts(cfg)


def _as_cancel(cancel: object) -> Callable[[], bool] | None:
    if cancel is None:
        return None
    if hasattr(cancel, "is_set"):
        return cancel.is_set  # threading.Event
    if callable(cancel):
        return cancel  # type: ignore[return-value]
    raise TypeError("cancel must be a threading.Event or a callable returning bool")


@dataclass
class Capture:
    """Samples of one channel. ``volts[i]`` corresponds to ``codes[i]``."""

    channel: int
    config: ChannelConfig
    continuous: bool
    sample_rate: float  # nominal data rate (sps) for the conversion mode
    codes: np.ndarray
    volts: np.ndarray
    block_starts: tuple[int, ...] = (0,)  # sample index where each block begins; () if none
    started_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    duration_s: float = 0.0  # host wall time for the whole acquisition, readback included
    cancelled: bool = False  # True: cancelled, and every sample was discarded
    requested: int = 0

    @property
    def n(self) -> int:
        return int(self.codes.size)

    @property
    def gap_free(self) -> bool:
        return len(self.block_starts) <= 1

    @property
    def times(self) -> np.ndarray:
        """Nominal time of each sample (index / data rate). Gaps between blocks are not
        included; see ``block_starts``."""
        return np.arange(self.n) / self.sample_rate

    @property
    def block_index(self) -> np.ndarray:
        """Block number of each sample."""
        out = np.zeros(self.n, dtype=np.int64)
        for start in self.block_starts[1:]:
            out[start:] += 1
        return out

    @property
    def fsr(self) -> float:
        return full_scale_range(self.config)

    @property
    def lsb_volts(self) -> float:
        return lsb_volts(self.config)

    def stats(self) -> Stats:
        return compute_stats(self.codes, self.volts, self.config)

    def histogram(self, **kw) -> Histogram:
        return code_histogram(self.codes, **kw)

    def spectrum(self, window: str = DEFAULT_WINDOW, **kw) -> Spectrum:
        if not self.gap_free:
            raise ValueError("an FFT needs a gap-free (single-block) capture")
        return analyze_spectrum(self.volts, self.sample_rate, self.fsr, window=window, **kw)


def acquire(
    dev: MAX11410,
    channel: int = 0,
    n: int = 1000,
    *,
    continuous: bool = True,
    block_samples: int | None = None,
    gap_free: bool = False,
    progress: Callable[[int, int], None] | None = None,
    cancel: object = None,
) -> Capture:
    """Capture ``n`` samples of ``channel`` (no upper limit), in as many blocks as needed.

    ``block_samples`` caps the block size (default and maximum: :func:`max_block_samples`);
    smaller blocks make cancelling more responsive at the cost of more gaps.
    ``gap_free=True`` requires the whole capture to fit one block (for an FFT).
    ``progress(done_samples, n)`` is called during readback.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    max_block = max_block_samples(dev.board)
    block = max_block if block_samples is None else int(block_samples)
    if not 1 <= block <= max_block:
        raise ValueError(f"block_samples must be 1..{max_block}")
    if gap_free and n > block:
        raise ValueError(
            f"a gap-free capture is one block: at most {block} samples (asked for {n})"
        )
    if isinstance(channel, bool) or not isinstance(channel, int):
        raise TypeError("channel must be a slot number 0..7")
    is_cancelled = _as_cancel(cancel)
    index, cfg = dev._cfg(channel)  # validates the slot and its configuration
    cfg = replace(cfg)
    mode = "continuous" if continuous else "single"
    started = datetime.now().astimezone()
    t0 = time.monotonic()

    codes: list[int] = []
    starts: list[int] = []
    cancelled = False
    while len(codes) < n:
        k = min(block, n - len(codes))
        done = len(codes)

        def block_progress(done_bytes: int, _total: int, done: int = done) -> None:
            if progress is not None:
                progress(done + done_bytes // SAMPLE_BYTES, n)

        try:
            chunk = dev.capture_codes(
                index,
                k,
                continuous=continuous,
                apply=not starts,
                progress=block_progress,
                cancel=is_cancelled,
            )
        except Cancelled:
            cancelled = True
            codes, starts = [], []  # a cancelled capture keeps nothing
            break
        starts.append(done)
        codes.extend(chunk)

    arr = np.asarray(codes, dtype=np.int64)
    return Capture(
        channel=index,
        config=cfg,
        continuous=continuous,
        sample_rate=cfg.data_rate(mode=mode),
        codes=arr,
        volts=codes_to_volts(arr, cfg),
        block_starts=tuple(starts),  # empty if cancelled before the first block
        started_at=started,
        duration_s=time.monotonic() - t0,
        cancelled=cancelled,
        requested=n,
    )

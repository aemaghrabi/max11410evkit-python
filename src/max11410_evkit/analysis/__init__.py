"""Analysis of captured ADC data: statistics, code histogram, FFT dynamic performance.

Everything here is pure NumPy and works on arrays of codes / volts; nothing talks to the
board. Definitions (agreed with Ahmed 2026-10-11, see ``docs/plan/p3-acquisition-analysis.md``):

* FSR (full-scale range) = 2 * VREF / gain for bipolar channels, VREF / gain for unipolar
  (datasheet "Noise Performance": FSR = 5 V bipolar with a 2.5 V reference).
* Effective resolution = log2(FSR / rms noise) (datasheet).
* Noise-free resolution is reported twice: ``log2(FSR / (6.6 * rms noise))`` (datasheet
  definition, p-p = 6.6 x rms) and ``log2(FSR / measured max - min)``.
* FFT: 0 dBFS = a sine with peak amplitude FSR / 2; THD over harmonics 2..6;
  ENOB = (SINAD - 1.76) / 6.02; default window Blackman-Harris 4-term.
"""

from max11410_evkit.analysis.fft import (
    DEFAULT_WINDOW,
    MIN_TONE_DBFS,
    WINDOWS,
    Spectrum,
    SpectrumMetrics,
    analyze_spectrum,
    window_coefficients,
)
from max11410_evkit.analysis.histogram import Histogram, code_histogram
from max11410_evkit.analysis.stats import (
    NFR_PP_FACTOR,
    Stats,
    compute_stats,
    full_scale_range,
)

__all__ = [
    "DEFAULT_WINDOW",
    "MIN_TONE_DBFS",
    "NFR_PP_FACTOR",
    "WINDOWS",
    "Histogram",
    "Spectrum",
    "SpectrumMetrics",
    "Stats",
    "analyze_spectrum",
    "code_histogram",
    "compute_stats",
    "full_scale_range",
    "window_coefficients",
]

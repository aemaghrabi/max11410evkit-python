"""FFT dynamic performance (the FFT tab): spectrum in dBFS plus SNR, THD, SINAD, SFDR, ENOB.

Method
    1. Remove the mean, apply a periodic (DFT-even) cosine-sum window, real FFT.
    2. Power per bin ``P[k] = 2|X[k]|^2 / (N * sum(w^2))`` (V rms^2; DC and Nyquist not
       doubled). With this scaling the sum of ``P`` over a tone's main lobe is the tone's power
       and the sum over noise bins is the noise power, whatever the window.
    3. Fundamental = the largest bin above the DC region. Its power is the sum over
       +-``lobe`` bins; its frequency is the power-weighted centre of those bins.
    4. Harmonics 2..6 at ``h * f0``, folded into 0..fs/2 (the ADC output is sampled at the
       data rate, so higher harmonics alias). Each harmonic is the sum over +-``lobe`` bins
       around its expected bin. A harmonic that folds onto DC or onto a bin already used is
       skipped (reported as ``None``).
    5. Noise = mean power of the remaining bins x the number of bins in the band (DC region
       excluded), i.e. the excluded bins are filled with the average noise.
    6. SNR = 10log(Pf/Pn), THD = 10log(sum Ph / Pf), SINAD = 10log(Pf / (Pn + sum Ph)),
       SFDR = largest bin of the fundamental / largest other bin (harmonics included),
       ENOB = (SINAD - 1.76) / 6.02, 0 dBFS = a sine with peak FSR / 2.
    7. ``mag_dbfs`` (for plotting) uses the coherent-gain amplitude ``2|X[k]| / sum(w)``,
       so an on-bin tone reads its true amplitude. ``noise_floor_dbfs`` is the average level
       of the noise bins on that same plot.

``lobe`` (bins excluded either side of DC, the fundamental and each harmonic) defaults to the
window's main-lobe half width: rectangular 1, Hann 2, Blackman-Harris 4-term 4, flat top 5.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

THD_HARMONICS = range(2, 7)  # 2nd .. 6th
# Below this the "fundamental" is just the largest noise/pickup bin, not a test tone, and
# SNR/THD/SINAD/SFDR/ENOB do not apply (agreed with Ahmed 2026-10-11).
MIN_TONE_DBFS = -60.0
MIN_FFT_SAMPLES = 16


@dataclass(frozen=True)
class _Window:
    name: str
    coeffs: tuple[float, ...]  # cosine-sum a0, a1, a2, ...
    lobe: int  # main-lobe half width in bins


WINDOWS: dict[str, _Window] = {
    w.name: w
    for w in (
        _Window("blackman-harris-4", (0.35875, 0.48829, 0.14128, 0.01168), 4),
        _Window("hann", (0.5, 0.5), 2),
        _Window(
            "flat-top",
            (0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368),
            5,
        ),
        _Window("rectangular", (1.0,), 1),
    )
}
DEFAULT_WINDOW = "blackman-harris-4"
_ALIASES = {"bh4": "blackman-harris-4", "rect": "rectangular", "flattop": "flat-top"}


def _window(name: str) -> _Window:
    """Look up a window; accepts the GUI labels ("Blackman-Harris 4", "Flat top", ...)."""
    key = name.strip().lower().replace("_", "-").replace(" ", "-")
    key = _ALIASES.get(key, key)
    try:
        return WINDOWS[key]
    except KeyError:
        raise ValueError(f"unknown window {name!r}; choose from {', '.join(WINDOWS)}") from None


def window_coefficients(name: str, n: int) -> np.ndarray:
    """Periodic cosine-sum window of length ``n``."""
    w = _window(name)
    k = np.arange(n) * (2.0 * np.pi / n)
    out = np.zeros(n)
    for i, a in enumerate(w.coeffs):
        out += (-1) ** i * a * np.cos(i * k)
    return out


@dataclass(frozen=True)
class Harmonic:
    order: int
    freq_hz: float
    dbc: float | None  # None if skipped (folds onto DC or onto a bin already used)


@dataclass(frozen=True)
class SpectrumMetrics:
    fundamental_hz: float
    fundamental_dbfs: float
    thd_db: float
    snr_db: float
    sinad_db: float
    sfdr_db: float
    enob: float
    noise_floor_dbfs: float
    harmonics: tuple[Harmonic, ...] = field(default=())

    def to_dict(self) -> dict:
        return asdict(self)

    def has_test_tone(self, min_dbfs: float = MIN_TONE_DBFS) -> bool:
        """True if the fundamental is large enough (>= ``min_dbfs``) for the metrics to apply."""
        return self.fundamental_dbfs >= min_dbfs


@dataclass(frozen=True)
class Spectrum:
    freqs_hz: np.ndarray
    mag_dbfs: np.ndarray
    metrics: SpectrumMetrics
    window: str
    n: int
    sample_rate: float
    fsr: float


def _db(num: float, den: float = 1.0) -> float:
    """10*log10(num/den) in dB; -inf if num is 0, +inf if only den is 0 (constant input)."""
    if num <= 0:
        return -math.inf
    if den <= 0:
        return math.inf
    return 10.0 * math.log10(num / den)


def analyze_spectrum(
    volts: np.ndarray,
    sample_rate: float,
    fsr: float,
    *,
    window: str = DEFAULT_WINDOW,
    lobe: int | None = None,
) -> Spectrum:
    """Spectrum and dynamic-performance metrics of a gap-free capture."""
    x = np.asarray(volts, dtype=float)
    n = x.size
    if x.ndim != 1 or n < MIN_FFT_SAMPLES:
        raise ValueError(f"need a 1-D capture of at least {MIN_FFT_SAMPLES} samples")
    if sample_rate <= 0 or fsr <= 0:
        raise ValueError("sample_rate and fsr must be > 0")
    win = _window(window)
    hw = win.lobe if lobe is None else int(lobe)
    if hw < 0:
        raise ValueError("lobe must be >= 0")

    w = window_coefficients(win.name, n)
    spec = np.fft.rfft((x - x.mean()) * w)
    nb = spec.size
    single = np.full(nb, 2.0)
    single[0] = 1.0
    if n % 2 == 0:
        single[-1] = 1.0
    mag2 = np.abs(spec) ** 2
    power = single * mag2 / (n * float(np.sum(w * w)))  # V rms^2 per bin
    amp = single * np.abs(spec) / float(np.sum(w))  # V peak (coherent gain)
    half_fs = fsr / 2.0
    with np.errstate(divide="ignore"):
        mag_dbfs = 20.0 * np.log10(np.maximum(amp, 1e-300) / half_fs)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    bin_hz = sample_rate / n

    band_lo = hw + 1  # first bin above the DC region
    if band_lo >= nb - 1:
        raise ValueError("capture too short for this window")
    used = np.zeros(nb, dtype=bool)
    used[:band_lo] = True

    def lobe_bins(k: int) -> np.ndarray:
        return np.arange(max(k - hw, band_lo), min(k + hw, nb - 1) + 1)

    kf = band_lo + int(np.argmax(power[band_lo:]))
    fl = lobe_bins(kf)
    pf = float(power[fl].sum())
    f0 = float(np.sum(fl * power[fl]) / pf) * bin_hz if pf > 0 else kf * bin_hz
    used[fl] = True
    fund_lobe = used.copy()

    harmonics: list[Harmonic] = []
    ph_total = 0.0
    for h in THD_HARMONICS:
        fa = math.fmod(h * f0, sample_rate)
        if fa > sample_rate / 2:
            fa = sample_rate - fa
        kb = int(round(fa / bin_hz))
        if kb >= nb or used[kb]:
            harmonics.append(Harmonic(h, fa, None))
            continue
        hb = lobe_bins(kb)
        hb = hb[~used[hb]]
        ph = float(power[hb].sum())
        used[hb] = True
        ph_total += ph
        harmonics.append(Harmonic(h, fa, _db(ph, pf) if pf > 0 else None))

    noise = ~used
    noise[:band_lo] = False
    if not noise.any():
        raise ValueError(
            f"{n} samples is too short for the {win.name} window: the fundamental and its "
            "harmonics leave no noise bins; capture more samples"
        )
    n_band = nb - band_lo
    pn = float(power[noise].mean()) * n_band

    spur_mask = ~fund_lobe
    spur_mask[:band_lo] = False
    p_spur = float(power[spur_mask].max())
    sinad = _db(pf, pn + ph_total)
    metrics = SpectrumMetrics(
        fundamental_hz=f0,
        fundamental_dbfs=_db(pf, half_fs**2 / 2.0),
        thd_db=_db(ph_total, pf) if pf > 0 else -math.inf,
        snr_db=_db(pf, pn),
        sinad_db=sinad,
        sfdr_db=_db(float(power[kf]), p_spur),
        enob=(sinad - 1.76) / 6.02 if math.isfinite(sinad) else sinad,
        noise_floor_dbfs=_db(float(np.mean(amp[noise] ** 2)), half_fs**2),
        harmonics=tuple(harmonics),
    )
    return Spectrum(freqs, mag_dbfs, metrics, win.name, n, sample_rate, fsr)

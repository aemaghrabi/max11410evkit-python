"""Statistics, histogram and FFT metrics on synthetic data (no board)."""

import math

import numpy as np
import pytest

from max11410_evkit.analysis import (
    NFR_PP_FACTOR,
    analyze_spectrum,
    code_histogram,
    compute_stats,
    full_scale_range,
    window_coefficients,
)
from max11410_evkit.analysis.fft import WINDOWS
from max11410_evkit.device.max11410 import ChannelConfig


def test_full_scale_range():
    assert full_scale_range(ChannelConfig()) == 2.5  # unipolar: VREF
    assert full_scale_range(ChannelConfig(bipolar=True)) == 5.0  # datasheet: 5 V bipolar
    assert full_scale_range(ChannelConfig(bipolar=True, path="pga", gain=8)) == 5.0 / 8


def test_stats_definitions():
    cfg = ChannelConfig(bipolar=True)
    rng = np.random.default_rng(0)
    volts = 1.0 + rng.normal(0, 1e-6, 5000)
    codes = np.round(volts / cfg.lsb_volts).astype(np.int64) + (1 << 23)
    st = compute_stats(codes, volts, cfg)
    std = float(np.std(volts, ddof=1))
    assert st.n == 5000
    assert st.average == pytest.approx(volts.mean())
    assert st.std_dev == pytest.approx(std)
    assert st.p2p == pytest.approx(volts.max() - volts.min())
    assert st.p2p_codes == codes.max() - codes.min()
    assert st.effective_resolution == pytest.approx(math.log2(5.0 / std))
    assert st.noise_free_resolution == pytest.approx(math.log2(5.0 / (NFR_PP_FACTOR * std)))
    assert st.noise_free_resolution_measured == pytest.approx(math.log2(5.0 / st.p2p))
    # 6.6 sigma -> 2.72 bits below the effective resolution
    assert st.effective_resolution - st.noise_free_resolution == pytest.approx(
        math.log2(NFR_PP_FACTOR)
    )


def test_stats_zero_noise_has_no_resolution():
    cfg = ChannelConfig()
    st = compute_stats(np.full(10, 1234), np.full(10, 0.5), cfg)
    assert st.std_dev == 0 and st.p2p_codes == 0
    assert st.effective_resolution is None
    assert st.noise_free_resolution is None and st.noise_free_resolution_measured is None


def test_stats_rejects_bad_input():
    with pytest.raises(ValueError):
        compute_stats(np.array([]), np.array([]), ChannelConfig())
    with pytest.raises(ValueError):
        compute_stats(np.array([1, 2]), np.array([1.0]), ChannelConfig())


def test_histogram_includes_empty_bins():
    h = code_histogram(np.array([10, 10, 12, 13, 13, 13]))
    assert h.bin_width == 1
    assert h.bin_codes.tolist() == [10, 11, 12, 13]
    assert h.counts.tolist() == [2, 0, 1, 3]
    assert h.n == 6


def test_histogram_merges_bins_over_max():
    h = code_histogram(np.arange(0, 1000), max_bins=100)
    assert h.bin_width == 10 and h.counts.size == 100
    assert set(h.counts.tolist()) == {10}
    assert h.bin_codes[1] == 10


@pytest.mark.parametrize("name", ["Blackman-Harris 4", "Hann", "Flat top", "Rectangular", "bh4"])
def test_window_lookup_accepts_gui_labels(name):
    assert window_coefficients(name, 64).shape == (64,)


def test_window_unknown():
    with pytest.raises(ValueError, match="unknown window"):
        window_coefficients("kaiser", 64)


def test_bh4_is_periodic():
    w = window_coefficients("blackman-harris-4", 8)
    a = WINDOWS["blackman-harris-4"].coeffs
    assert w[0] == pytest.approx(a[0] - a[1] + a[2] - a[3])
    assert w[4] == pytest.approx(sum(a))  # peak at N/2 for a periodic window


def _tone(n=8192, fs=1000.0, f=49.3201953125, amp=2.0, h2=0.002, h3=0.001, sigma=50e-6, seed=1):
    t = np.arange(n) / fs
    rng = np.random.default_rng(seed)
    x = amp * np.sin(2 * np.pi * f * t)
    x += h2 * np.sin(2 * np.pi * 2 * f * t) + h3 * np.sin(2 * np.pi * 3 * f * t)
    return x + rng.normal(0, sigma, n), fs


@pytest.mark.parametrize("window", ["blackman-harris-4", "flat-top"])
def test_fft_metrics_match_theory(window):
    x, fs = _tone()
    fsr = 5.0
    m = analyze_spectrum(x, fs, fsr, window=window).metrics
    snr = 10 * math.log10(2.0**2 / 2 / 50e-6**2)
    thd = 10 * math.log10((0.002**2 + 0.001**2) / 2.0**2)
    assert m.fundamental_hz == pytest.approx(49.3202, abs=0.01)
    assert m.fundamental_dbfs == pytest.approx(20 * math.log10(2.0 / 2.5), abs=0.05)
    assert m.snr_db == pytest.approx(snr, abs=1.0)
    assert m.thd_db == pytest.approx(thd, abs=0.2)
    assert m.sinad_db == pytest.approx(
        -10 * math.log10(10 ** (-snr / 10) + 10 ** (thd / 10)), abs=0.3
    )
    assert m.sfdr_db == pytest.approx(60.0, abs=0.3)  # 2.0 V vs 2 mV H2
    assert m.enob == pytest.approx((m.sinad_db - 1.76) / 6.02)
    assert m.harmonics[0].order == 2
    assert m.harmonics[0].dbc == pytest.approx(-60.0, abs=0.2)


def test_fft_coherent_tone_reads_true_amplitude_on_plot():
    n, fs = 1024, 1024.0
    x = 1.25 * np.sin(2 * np.pi * 100 * np.arange(n) / fs)  # bin 100 exactly
    spec = analyze_spectrum(x, fs, 5.0, window="rectangular")
    assert spec.freqs_hz[100] == 100
    assert spec.mag_dbfs[100] == pytest.approx(20 * math.log10(1.25 / 2.5), abs=1e-6)
    assert spec.metrics.fundamental_hz == pytest.approx(100.0)


def test_fft_harmonics_fold_above_nyquist():
    n, fs = 4096, 100.0
    f = 40.0 + 0.3 * fs / n  # H2 = 80.x Hz -> folds to 19.x Hz
    x = np.sin(2 * np.pi * f * np.arange(n) / fs) + 1e-3 * np.sin(
        2 * np.pi * 2 * f * np.arange(n) / fs
    )
    x += np.random.default_rng(3).normal(0, 1e-6, n)
    m = analyze_spectrum(x, fs, 5.0).metrics
    h2 = m.harmonics[0]
    assert h2.freq_hz == pytest.approx(fs - 2 * f, abs=1e-6)
    assert h2.dbc == pytest.approx(-60.0, abs=0.3)


def test_fft_too_short():
    with pytest.raises(ValueError):
        analyze_spectrum(np.zeros(8), 100.0, 5.0)


def test_fft_too_short_for_window_has_clear_message():
    x = np.sin(2 * np.pi * 5 * np.arange(64) / 64)
    with pytest.raises(ValueError, match="too short for the blackman-harris-4 window"):
        analyze_spectrum(x, 64.0, 5.0)


def test_has_test_tone():
    x, fs = _tone()
    assert analyze_spectrum(x, fs, 5.0).metrics.has_test_tone()
    noise = np.random.default_rng(5).normal(0, 20e-6, 4096)  # DC input: no tone
    assert not analyze_spectrum(noise, fs, 2.5).metrics.has_test_tone()


def test_fft_constant_input_does_not_crash():
    m = analyze_spectrum(np.full(1024, 1.0), 960.0, 2.5).metrics
    assert m.fundamental_dbfs == -math.inf and not m.has_test_tone()

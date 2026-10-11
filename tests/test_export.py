"""CSV export round trips."""

import numpy as np
import pytest

from max11410_evkit.acquisition import acquire
from max11410_evkit.device.max11410 import MAX11410
from max11410_evkit.export import (
    read_csv,
    save_capture_csv,
    save_histogram_csv,
    save_spectrum_csv,
)


@pytest.fixture
def cap(board, fake):
    d = MAX11410(board)
    d.init()
    fake.adc.inputs[2] = 1.5
    return acquire(d, 0, 30, block_samples=20)


def test_capture_csv_round_trip(tmp_path, cap):
    st = cap.stats()
    path = save_capture_csv(tmp_path / "cap.csv", cap, stats=st)
    meta, cols = read_csv(path)
    assert list(cols) == ["index", "block", "time_s", "code", "volts"]
    assert cols["code"].astype(int).tolist() == cap.codes.tolist()
    assert np.allclose(cols["volts"], cap.volts)
    assert cols["block"].tolist() == [0] * 20 + [1] * 10
    assert cols["time_s"][1] == pytest.approx(1 / cap.sample_rate)
    assert meta["samples"] == "30" and meta["blocks"] == "2" and meta["block_starts"] == "0 20"
    assert meta["ainp"] == "AIN2" and meta["ainn"] == "GND"
    assert meta["reference"] == "REF1 2.5 V"
    assert float(meta["stats.average"]) == pytest.approx(st.average)
    assert meta["cancelled"] == "false"


def test_histogram_csv(tmp_path, cap):
    hist = cap.histogram()
    _, cols = read_csv(save_histogram_csv(tmp_path / "h.csv", cap, hist))
    assert cols["count"].sum() == cap.n
    assert np.allclose(cols["volts"], cols["code"] * cap.lsb_volts)


def test_spectrum_csv(tmp_path, board, fake):
    d = MAX11410(board)
    d.init()
    cap = acquire(d, 0, 64)
    spec = cap.spectrum(window="hann")
    meta, cols = read_csv(save_spectrum_csv(tmp_path / "f.csv", cap, spec))
    assert cols["freq_hz"].size == 33
    assert meta["window"] == "hann" and meta["fft_points"] == "64"
    assert "metrics.snr_db" in meta and "metrics.harmonic_2" in meta

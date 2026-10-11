"""Block-split captures, gaps, cancellation (fake board)."""

import threading

import numpy as np
import pytest

from max11410_evkit.acquisition import (
    acquire,
    codes_to_volts,
    max_block_samples,
    max_fft_samples,
)
from max11410_evkit.device.max11410 import MAX11410, ChannelConfig, raw_to_volts


@pytest.fixture
def dev(board):
    d = MAX11410(board)
    d.init()
    return d


def test_limits_for_32k_sram(board):
    assert max_block_samples(board) == 10922
    assert max_fft_samples(board) == 8192


@pytest.mark.parametrize("bipolar", [False, True])
def test_codes_to_volts_matches_scalar(bipolar):
    cfg = ChannelConfig(bipolar=bipolar, path="pga", gain=4)
    codes = np.array([0, 1, 0x7FFFFF, 0x800000, 0xFFFFFF])
    assert codes_to_volts(codes, cfg).tolist() == pytest.approx(
        [raw_to_volts(int(c), cfg) for c in codes]
    )


def test_single_block(dev, fake):
    fake.adc.inputs[2] = 1.0
    cap = acquire(dev, 0, 500)
    assert cap.n == 500 and cap.gap_free and cap.block_starts == (0,)
    assert not cap.cancelled and cap.requested == 500
    assert np.allclose(cap.volts, 1.0, atol=1e-6)
    assert cap.sample_rate == dev.channels[0].data_rate()
    assert not fake.adc.continuous  # stopped afterwards


def test_long_capture_is_split_into_blocks(dev, fake):
    progress = []
    cap = acquire(dev, 0, 25, block_samples=10, progress=lambda d, t: progress.append((d, t)))
    assert cap.n == 25
    assert cap.block_starts == (0, 10, 20)
    assert not cap.gap_free
    assert cap.block_index.tolist() == [0] * 10 + [1] * 10 + [2] * 5
    assert progress[-1] == (25, 25)
    assert all(t == 25 for _, t in progress)
    # three 0x42 commands with 10, 10 and 5 samples
    caps = [p for p in fake.received if p[0] == 0x42]
    assert [(p[3] << 8) | p[4] for p in caps] == [10, 10, 5]
    # legacy test pattern counts conversions: increasing within the whole capture
    assert np.all(np.diff(cap.codes) > 0)


def test_beyond_sram_needs_no_block_argument(dev, fake):
    fake.adc.inputs[2] = 0.5
    cap = acquire(dev, 0, 12000)  # > 10922 samples in one SRAM
    assert cap.n == 12000 and cap.block_starts == (0, 10922)


def test_single_cycle_mode(dev, fake):
    fake.adc.inputs[2] = 0.25
    cap = acquire(dev, 0, 30, continuous=False, block_samples=16)
    assert cap.n == 30 and cap.block_starts == (0, 16)
    assert cap.sample_rate == dev.channels[0].data_rate(mode="single")
    assert np.allclose(cap.volts, 0.25, atol=1e-6)


def test_channel_applied_once(dev, fake):
    start = len(fake.adc.spi_log)
    acquire(dev, 0, 30, block_samples=10)
    mux_writes = [f for f, _, _ in fake.adc.spi_log[start:] if f == 0x0B]
    assert len(mux_writes) == 1


def test_block_size_and_gap_free_checks(dev):
    with pytest.raises(ValueError):
        acquire(dev, 0, 10, block_samples=20000)
    with pytest.raises(ValueError):
        acquire(dev, 0, 10, block_samples=0)
    with pytest.raises(ValueError, match="gap-free"):
        acquire(dev, 0, 20, block_samples=10, gap_free=True)
    with pytest.raises(ValueError):
        acquire(dev, 0, 0)
    with pytest.raises(ValueError):
        acquire(dev, 8, 10)
    with pytest.raises(TypeError):
        acquire(dev, ChannelConfig(), 10)


def test_cancel_before_start(dev, fake):
    ev = threading.Event()
    ev.set()
    cap = acquire(dev, 0, 100, cancel=ev)
    assert cap.cancelled and cap.n == 0
    assert cap.block_starts == ()
    assert not [p for p in fake.received if p[0] == 0x42]
    assert not fake.adc.continuous


def test_cancel_between_blocks_discards_completed_blocks(dev, fake):
    ev = threading.Event()

    def progress(done, total):
        if done >= 10:
            ev.set()

    cap = acquire(dev, 0, 50, block_samples=10, progress=progress, cancel=ev)
    assert cap.cancelled
    assert cap.n == 0 and cap.block_starts == () and cap.volts.size == 0
    assert len([p for p in fake.received if p[0] == 0x42]) == 1  # stopped after block 1
    assert not fake.adc.continuous


def test_cancel_during_readback_discards_everything(dev, fake):
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        # block 1 (300 B): 1 check before the 0x42 + 5 chunk checks = 6; block 2: 7 = before
        # the 0x42, 8 = first chunk, 9 = second chunk -> cancelled inside block 2's readback
        return calls["n"] > 8

    cap = acquire(dev, 0, 200, block_samples=100, cancel=cancel)
    assert cap.cancelled and cap.n == 0 and cap.block_starts == ()
    assert not fake.adc.continuous


def test_spectrum_needs_gap_free(dev):
    cap = acquire(dev, 0, 40, block_samples=20)
    with pytest.raises(ValueError, match="gap-free"):
        cap.spectrum()
    cap = acquire(dev, 0, 256)
    assert cap.spectrum().n == 256

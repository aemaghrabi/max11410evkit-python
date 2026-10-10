"""0x42 block capture into SRAM and 0x43 readback."""

import threading

import pytest

from max11410_evkit.board.evkit import EvKitBoard, samples_from_bytes
from max11410_evkit.transport.errors import CommandFailed
from tests.fakes import FakeEvKitTransport


def _expected(ainp, n, start=0):
    return [0x800000 + (ainp << 16) + i for i in range(start, start + n)]


def test_continuous_capture_no_skipped_samples(board, fake):
    board.write_register(0x0B, 0x3A)
    data = board.capture(500, conv=0x01, restart_each=False, timeout=0.1)
    assert len(data) == 1500
    assert samples_from_bytes(data) == _expected(3, 500)
    cap = fake.received[-1 - 24]  # 1500 B -> 24 SRAM reads after the capture command
    assert cap[:7] == bytes([0x42, 0xB0, 3, 500 >> 8, 500 & 0xFF, 0x01, 0])


def test_single_restart_each_capture_no_skipped_samples(board, fake):
    board.write_register(0x0B, 0x10)
    data = board.capture(200, conv=0x00, restart_each=True, timeout=0.1)
    assert samples_from_bytes(data) == _expected(1, 200)
    # Firmware order per sample: wait, write CONV_START (next), then read previous DATA0.
    log = [first for first, _, _ in fake.adc.spi_log]
    i = log.index(0x01)  # initial CONV_START
    assert log[i + 1 : i + 7] == [0x01, 0xB0, 0x01, 0xB0, 0x01, 0xB0]


def test_restart_each_leaves_one_conversion_pending(board):
    board.capture(10, conv=0x00, restart_each=True, timeout=0.1)
    # The last CONV_START started an 11th conversion; the next wait sees it.
    assert int.from_bytes(board.wait_drdy_read(), "big") == 0x800000 + 10


def test_single_without_restart_fails_after_first_sample(board):
    with pytest.raises(CommandFailed) as ei:
        board.capture(5, conv=0x00, restart_each=False, timeout=0.1)
    assert ei.value.command == 0x42


def test_sram_readback_chunks(board, fake):
    fake.sram[:200] = bytes(i & 0xFF for i in range(200))
    progress = []
    data = board.sram_read(0, 200, progress=lambda a, b: progress.append((a, b)))
    assert data == fake.sram[:200]
    reads = [p for p in fake.received if p[0] == 0x43]
    assert [p[3] for p in reads] == [63, 63, 63, 11]
    assert [(p[1] << 8) | p[2] for p in reads] == [0, 63, 126, 189]
    assert progress[-1] == (200, 200)


def test_sram_read_offsets_and_bounds(board, fake):
    fake.sram[0x7FF0:0x8000] = bytes(range(16))
    assert board.sram_read(0x7FF0, 16) == bytes(range(16))
    assert board.sram_read(0, 0) == b""
    with pytest.raises(ValueError):
        board.sram_read(0x7FF0, 17)


def test_sram_limit_validation(board):
    with pytest.raises(ValueError, match="SRAM"):
        board.capture(32768 // 3 + 1, timeout=1)
    with pytest.raises(ValueError):
        board.capture(0, timeout=1)
    with pytest.raises(ValueError):
        board.capture(0x10000, width=1, timeout=1)
    small = EvKitBoard(board.transport, sram_bytes=300)
    with pytest.raises(ValueError):
        small.capture(101, timeout=1)
    assert len(small.capture(100, timeout=0.1)) == 300


def test_max_capture_fits(board):
    n = 32768 // 3
    data = board.capture(n, timeout=1)
    s = samples_from_bytes(data)
    assert len(s) == n and s[-1] == 0x800000 + n - 1


def test_capture_without_readback(board, fake):
    assert board.capture(10, readback=False, timeout=0.1) == b""
    assert samples_from_bytes(board.sram_read(0, 30)) == _expected(0, 10)


def test_fake_drops_commands_during_capture():
    t = FakeEvKitTransport(conversion_time_s=0.002)
    t.open()
    try:
        t.write_report(bytes([7, 0x42, 0xB0, 3, 0, 50, 0x01, 0]))
        t.wait_idle(0.0)  # let the firmware thread pick it up
        import time

        time.sleep(0.02)
        t.write_report(bytes([1, 0x02]))
        t.wait_idle()
        assert t.dropped and t.dropped[0][:1] == b"\x02"
    finally:
        t.close()


def test_lock_serializes_capture_and_other_commands():
    t = FakeEvKitTransport(conversion_time_s=0.002)
    b = EvKitBoard(t, timeout=0.5, drdy_timeout=1.0)
    b.open()
    try:
        result = {}

        def cap():
            result["data"] = b.capture(100, timeout=0.5)

        th = threading.Thread(target=cap)
        th.start()
        versions = [b.version() for _ in range(5)]  # would be dropped without the lock
        th.join()
        assert versions == [(1, 1)] * 5
        assert samples_from_bytes(result["data"]) == _expected(0, 100)
        assert t.dropped == []
    finally:
        b.close()


def test_samples_from_bytes_validation():
    assert samples_from_bytes(b"\x01\x02\x03\x04\x05\x06") == [0x010203, 0x040506]
    with pytest.raises(ValueError):
        samples_from_bytes(b"\x01\x02", 3)

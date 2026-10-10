"""One test (or more) per firmware command."""

import pytest

from max11410_evkit.board.evkit import (
    EvKitBoard,
    FirmwareVersion,
    ProtocolError,
    connect,
    register_width,
    spi_clock_hz,
)
from max11410_evkit.transport.errors import CommandFailed, Timeout
from tests.fakes import PART_ID, FakeEvKitTransport


def test_echo(board):
    data = bytes(range(61))
    assert board.echo(data) == data
    assert board.echo(b"") == b""
    with pytest.raises(ValueError):
        board.echo(bytes(62))


def test_version(board):
    v = board.version()
    assert v == FirmwareVersion(1, 1) and str(v) == "1.1"


def test_project(board):
    assert board.project() == "MAX11410EVK"


def test_raw_spi_write_and_read(board, fake):
    board.spi_write(0x0B, b"\x5a")
    assert board.spi_read(0x8B, 1) == b"\x5a"
    assert fake.adc.spi_log[-1][0] == 0x8B


def test_raw_spi_read_without_flag_clobbers_register(board, fake):
    """Why the 0x80 flag matters: the passthrough sends the byte unchanged."""
    board.write_register(0x0B, 0x5A)
    assert board.spi_read(0x0B, 1) == b"\xff"  # seen by the part as a write of 0x00
    assert board.read_register(0x0B) == 0x00


def test_read_register_applies_read_flag(board, fake):
    assert board.read_register(0x11) == PART_ID
    first, _, _ = fake.adc.spi_log[-1]
    assert first == 0x91
    payload = fake.received[-1]
    assert payload[:3] == bytes([0x41, 0x91, 3])


def test_write_register_clears_read_flag(board, fake):
    board.write_register(0x0B, 0x21)
    assert fake.received[-1][:4] == bytes([0x40, 0x0B, 1, 0x21])


@pytest.mark.parametrize(("reg", "value"), [(0x0B, 0xA5), (0x0E, 0x07), (0x12, 0x123456)])
def test_register_roundtrip(board, reg, value):
    board.write_register(reg, value)
    assert board.read_register(reg) == value


def test_register_bytes_and_explicit_width(board, fake):
    board.write_register(0x3A, b"\x12\x34", width=2)  # sequencer register: width required
    assert board.read_register(0x3A, 2) == 0x1234
    assert board.read_register_bytes(0x3A, 2) == b"\x12\x34"


def test_register_validation(board):
    with pytest.raises(ValueError):
        board.read_register(0x3A)  # width unknown
    with pytest.raises(ValueError):
        board.write_register(0x0B, 0x100)  # does not fit 1 byte
    with pytest.raises(ValueError):
        board.read_register(0x80, 1)
    with pytest.raises(ValueError):
        board.write_register(0x12, b"\x00", width=3)
    with pytest.raises(ValueError):
        board.spi_read(0x80, 41)
    with pytest.raises(ValueError):
        board.spi_write(0x00, bytes(41))


def test_register_width_map():
    assert register_width(0x00) == 1 and register_width(0x10) == 1
    assert register_width(0x11) == 3 and register_width(0x39) == 3
    assert register_width(0x3A) is None and register_width(0x6F) is None


def test_set_cs(board, fake):
    board.set_cs(False)
    assert fake.cs_level == 0
    board.set_cs(True)
    assert fake.cs_level == 1


def test_set_spi_divider(board, fake):
    assert board.set_spi_divider(5) == pytest.approx(1_000_000)
    assert fake.spi_divider == 5
    assert spi_clock_hz(0) == 6_000_000
    with pytest.raises(ValueError):
        board.set_spi_divider(256)


def test_read_int(board, fake):
    assert board.read_int() == 1
    assert board.read_int(cs_low=True) == 1
    assert fake.received[-1][0] == 0x47
    board.write_register(0x01, 0x00)  # start a single conversion
    board.read_register(0x38)  # STATUS completes it ... and clears CONV_READY
    assert board.read_int() == 1


def test_wait_drdy_read_with_start(board, fake):
    board.write_register(0x0B, 0x2A)  # AINP = 2
    raw = board.wait_drdy_read(0x30, 3, start=True, conv=0x00)
    assert int.from_bytes(raw, "big") == 0x800000 + (2 << 16) + 0
    assert fake.received[-1][:5] == bytes([0x51, 0xB0, 3, 1, 0x00])
    raw = board.wait_drdy_read(start=True)
    assert int.from_bytes(raw, "big") == 0x820001


def test_wait_drdy_read_fails_without_conversion(board):
    with pytest.raises(CommandFailed) as ei:
        board.wait_drdy_read()  # nothing started -> firmware gives up -> 0xFA
    assert ei.value.status == 0xFA and ei.value.command == 0x51
    assert board.version() == (1, 1)


def test_wait_drdy_read_host_timeout_exceeds_firmware_wait(fake):
    fake.drdy_timeout_s = 0.3
    b = EvKitBoard(fake, drdy_timeout=0.1)
    with pytest.raises(Timeout):  # host gave up before the firmware's 0xFA arrived
        b.wait_drdy_read()
    # The late 0xFA must not be taken as the reply to the next command (resync).
    b.write_register(0x01, 0x01)  # continuous conversions
    assert int.from_bytes(b.wait_drdy_read(timeout=1.0), "big") == 0x800000
    with pytest.raises(CommandFailed):  # a sufficient host timeout sees the 0xFA
        b.write_register(0x01, 0x00)
        b.read_register(0x38)  # consume the single conversion
        b.wait_drdy_read(timeout=1.0)


def test_wait_drdy_read_continuous(board):
    board.write_register(0x01, 0x01)
    vals = [int.from_bytes(board.wait_drdy_read(), "big") for _ in range(3)]
    assert vals == [0x800000, 0x800001, 0x800002]


def test_status_bits_clear_on_read(board):
    board.write_register(0x03, 0x00)  # CAL_START
    assert board.read_register(0x38) & 0x04
    assert not board.read_register(0x38) & 0x04
    board.write_register(0x01, 0x00)
    assert board.read_register(0x38) & 0x01
    assert not board.read_register(0x38) & 0x01
    assert board.read_register(0x30) == 0x800000


def test_connect_verifies_project():
    b = connect(transport=FakeEvKitTransport())
    assert b.is_open
    b.close()


class _WrongBoard(FakeEvKitTransport):
    def _process(self, p):
        if p[0] == 0x03:
            return b"\xee\x03ABC"
        return super()._process(p)


def test_connect_rejects_wrong_project():
    t = _WrongBoard()
    with pytest.raises(ProtocolError):
        connect(transport=t)
    assert not t.is_open

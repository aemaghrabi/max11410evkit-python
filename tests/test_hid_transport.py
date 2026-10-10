"""HidTransport against a fake ``hid`` module (no native hidapi needed)."""

import sys

import pytest

from max11410_evkit.board.evkit import EvKitBoard, connect
from max11410_evkit.transport.errors import NotConnected
from max11410_evkit.transport.hid import HidTransport, list_boards
from tests.fakes import FakeHidModule


@pytest.fixture
def fake_hid(monkeypatch):
    mod = FakeHidModule()
    monkeypatch.setitem(sys.modules, "hid", mod)
    return mod


def test_list_boards_dedupes(fake_hid):
    fake_hid.entries.append(dict(fake_hid.entries[0]))
    boards = list_boards()
    assert len(boards) == 1
    assert boards[0].path == b"/dev/hidraw7" and boards[0].usage_page == 0xFF00
    assert "MAXQ622HID" in str(boards[0])


def test_open_sets_nonblocking_and_pads_reports(fake_hid):
    t = HidTransport()
    t.open()
    dev = fake_hid.devices[0]
    assert dev.nonblocking == 1
    assert dev.opened_with == ("vidpid", 0x0B6A, 0x2B31, None)
    t.write_report(b"\x01\x02")
    assert dev.writes[-1] == b"\x00\x01\x02" + bytes(62)
    t.close()
    assert not t.is_open


def test_open_by_path(fake_hid):
    t = HidTransport("/dev/hidraw7")
    t.open()
    assert fake_hid.devices[0].opened_with == ("path", b"/dev/hidraw7")
    t.close()


def test_open_failure_is_not_connected(monkeypatch):
    monkeypatch.setitem(sys.modules, "hid", FakeHidModule(present=False))
    with pytest.raises(NotConnected):
        HidTransport().open()


def test_board_over_hid_end_to_end(fake_hid):
    b = connect()
    try:
        assert b.version() == (1, 1)
        assert b.read_register(0x11) == 0x000F02
        assert len(b.capture(30, timeout=0.1)) == 90
    finally:
        b.close()


def test_io_error_maps_to_not_connected(fake_hid):
    t = HidTransport()
    b = EvKitBoard(t)
    b.open()
    fake_hid.devices[0].fail_io = True
    with pytest.raises(NotConnected):
        b.version()
    assert not t.is_open


def test_write_report_too_long(fake_hid):
    t = HidTransport()
    t.open()
    with pytest.raises(ValueError):
        t.write_report(bytes(65))
    t.close()


def test_closed_transport_raises():
    with pytest.raises(NotConnected):
        HidTransport().read_report(0)

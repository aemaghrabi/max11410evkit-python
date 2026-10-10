"""Link-layer behaviour: framing, filler skipping, drain, timeouts, errors."""

import logging
import threading
import time

import pytest

from max11410_evkit.board.evkit import EvKitBoard
from max11410_evkit.transport.errors import CommandFailed, NotConnected, ProtocolError, Timeout
from tests.fakes import FakeEvKitTransport, SpyTransport

FILLER = b"\x00\x00\x00"


def _reply(body: bytes) -> bytes:
    return (bytes([len(body)]) + body).ljust(64, b"\x00")


def test_output_report_layout():
    spy = SpyTransport()
    spy.on_write = lambda t, d: t.reports.append(_reply(b"\xee\x01\x01"))
    EvKitBoard(spy).version()
    assert len(spy.writes) == 1
    w = spy.writes[0]
    assert len(w) == 64 and w[:2] == b"\x01\x02" and w[2:] == bytes(62)


def test_count_byte_matches_payload_length():
    spy = SpyTransport()
    spy.on_write = lambda t, d: t.reports.append(_reply(b"\xee"))
    EvKitBoard(spy).spi_write(0x0B, b"\x12")
    assert spy.writes[0][:5] == bytes([4, 0x40, 0x0B, 1, 0x12])


def test_fillers_are_skipped():
    spy = SpyTransport()

    def respond(t, d):
        t.reports += [FILLER, None, FILLER, bytes(64), None, _reply(b"\xee\x01\x01"), FILLER]

    spy.on_write = respond
    assert EvKitBoard(spy).version() == (1, 1)


def test_stale_reply_is_drained(board, fake, caplog):
    # A late reply to some earlier command, sitting in the queue between fillers.
    fake.inject_report(FILLER)
    fake.inject_report(_reply(b"\xee\x09\x09"))
    fake.inject_report(FILLER)
    with caplog.at_level(logging.WARNING, logger="max11410_evkit.board"):
        assert board.version() == (1, 1)
    assert "stale" in caplog.text


def test_stale_echo_does_not_satisfy_next_echo(board, fake):
    fake.inject_report(_reply(b"\xee\xaa\xbb"))
    assert board.echo(b"\x01\x02") == b"\x01\x02"


def test_fail_status_raises_command_failed():
    spy = SpyTransport()
    spy.on_write = lambda t, d: t.reports.append(_reply(b"\xfa\x00\x00\x00"))
    with pytest.raises(CommandFailed) as ei:
        EvKitBoard(spy).spi_write(0x01, b"\x00")
    assert ei.value.status == 0xFA and ei.value.command == 0x40


def test_unknown_status_is_protocol_error():
    spy = SpyTransport()
    spy.on_write = lambda t, d: t.reports.append(_reply(b"\x55"))
    with pytest.raises(ProtocolError):
        EvKitBoard(spy).set_cs(1)


def test_malformed_count_is_protocol_error():
    spy = SpyTransport()
    spy.on_write = lambda t, d: t.reports.append(b"\x40" + bytes(63))  # count 64 > 63
    with pytest.raises(ProtocolError):
        EvKitBoard(spy).set_cs(1)


def test_short_data_is_protocol_error():
    spy = SpyTransport()
    spy.on_write = lambda t, d: t.reports.append(_reply(b"\xee\x01"))  # 1 of 2 version bytes
    with pytest.raises(ProtocolError):
        EvKitBoard(spy).version()


def test_timeout_when_board_is_silent(board, fake):
    fake.mute = True
    t0 = time.monotonic()
    with pytest.raises(Timeout):
        board.raw_transact(b"\x02", timeout=0.1)
    assert 0.09 <= time.monotonic() - t0 < 1.0
    fake.mute = False
    assert board.version() == (1, 1)  # recovers


def test_late_reply_after_timeout_is_discarded(fake):
    fake.command_latency_s = 0.15  # every reply arrives after the host gave up
    b = EvKitBoard(fake, timeout=0.05, drdy_timeout=1.0)
    with pytest.raises(Timeout):
        b.version()
    fake.command_latency_s = 0.0
    # Without resync, the late version reply would be returned for this echo.
    assert b.echo(b"\x55\x66") == b"\x55\x66"
    assert b.version() == (1, 1)


def test_timeout_is_a_builtin_timeouterror(board, fake):
    fake.mute = True
    with pytest.raises(TimeoutError):
        board.version()


@pytest.mark.parametrize("cmd", [0x45, 0x46, 0x7E])
def test_no_reply_commands_time_out(board, cmd):
    with pytest.raises(Timeout):
        board.raw_transact(bytes([cmd, 0x30 | 0x80, 3, 0, 1]), timeout=0.1)
    assert board.version() == (1, 1)


def test_expect_reply_false_returns_immediately(board, fake):
    assert board.raw_transact(b"\x46", expect_reply=False) == b""
    fake.wait_idle()
    assert fake.received[-1][:1] == b"\x46"


def test_payload_length_validation(board):
    with pytest.raises(ValueError):
        board.raw_transact(b"")
    with pytest.raises(ValueError):
        board.raw_transact(bytes(64))


def test_not_connected_when_closed(fake):
    b = EvKitBoard(fake)
    b.close()
    assert not b.is_open
    with pytest.raises(NotConnected):
        b.version()


def test_disconnect_raises_not_connected(board, fake):
    assert board.version() == (1, 1)
    fake.disconnect()
    with pytest.raises(NotConnected):
        board.version()
    with pytest.raises(NotConnected):
        fake.open()


def test_disconnect_while_waiting(fake):
    b = EvKitBoard(fake, timeout=2.0)
    fake.mute = True
    threading.Timer(0.05, fake.disconnect).start()
    with pytest.raises(NotConnected):
        b.version()


def test_on_event_and_debug_logging(fake, caplog):
    events = []
    b = EvKitBoard(fake, on_event=events.append)
    with caplog.at_level(logging.DEBUG, logger="max11410_evkit.board"):
        b.version()
    assert any(e.startswith("-> VERSION") for e in events)
    assert any(e.startswith("<- VERSION ee 01 01") for e in events)
    assert "VERSION" in caplog.text


def test_context_manager_opens_and_closes():
    t = FakeEvKitTransport()
    with EvKitBoard(t) as b:
        assert b.is_open and b.version() == (1, 1)
    assert not t.is_open

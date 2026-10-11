"""MAX11410 device layer against the fake board."""

from dataclasses import replace

import pytest

from max11410_evkit.board.evkit import EvKitBoard
from max11410_evkit.device.max11410 import (
    EVKIT_DEFAULT_CHANNEL,
    MAX11410,
    RATE_TABLES,
    ChannelConfig,
    DeviceError,
    data_rate,
    lsb_volts,
    rate_index_for,
    raw_to_volts,
    volts_to_raw,
)
from max11410_evkit.transport.errors import CommandFailed, EvKitError
from tests.fakes import FakeEvKitTransport


@pytest.fixture
def dev(board):
    d = MAX11410(board)
    d.init()
    return d


def _writes(fake, start=0):
    """(register, value bytes) for every SPI write after index ``start`` of the SPI log."""
    return [(first, data) for first, data, _ in fake.adc.spi_log[start:] if not first & 0x80]


def test_identify(board):
    assert MAX11410(board).identify() == 0x000F02


@pytest.mark.parametrize("pid", [0xFFFFFF, 0x000000, 0x123456])
def test_identify_rejects_bad_part_id(board, fake, pid):
    fake.adc.regs[0x11] = pid
    with pytest.raises(DeviceError, match="PART_ID"):
        MAX11410(board).identify()


def test_init_sequence(board, fake):
    d = MAX11410(board)
    d.init(intrb_gpio=1)
    assert fake.adc.regs[0x00] == 0x00  # PD -> normal
    assert fake.adc.regs[0x09] == 0x21  # CTRL reset 0x01 + FORMAT
    assert fake.adc.regs[0x05] == 0xC1  # GP1_CTRL = INTRB
    with pytest.raises(ValueError):
        d.init(intrb_gpio=2)


def test_default_channel_is_evkit_default():
    c = EVKIT_DEFAULT_CHANNEL
    assert (c.ainp, c.ainn, c.refsel, c.vref_volts) == (2, 10, 1, 2.5)
    assert (c.linef, c.rate, c.data_rate()) == ("SINC4", 4, 60)
    assert not c.bipolar and c.path == "buffered" and c.gain == 1


def test_channel_slots(dev):
    assert len(dev.channels) == 8
    assert dev.channels[0].enabled and not any(c.enabled for c in dev.channels[1:])
    with pytest.raises(ValueError):
        dev.set_channel(8, ChannelConfig())


@pytest.mark.parametrize(
    "bad",
    [
        dict(ainp=11),
        dict(ainn=12),
        dict(refsel=8),
        dict(gain=3),
        dict(gain=8),  # buffered path: digital gain is 1, 2 or 4 only
        dict(path="bypass", gain=16),
        dict(path="x"),
        dict(linef="SINC3"),
        dict(rate=16),
        dict(vref_volts=0),
    ],
)
def test_channel_validation(bad):
    with pytest.raises(ValueError):
        replace(ChannelConfig(), **bad).validate()


def test_apply_channel_register_sequence(dev, fake):
    cfg = ChannelConfig(
        ainp=3,
        ainn=4,
        refsel=3,
        vref_volts=3.3,
        vrefp_buf=True,
        path="pga",
        gain=16,
        bipolar=True,
        linef="FIR50",
        rate=2,
    )
    dev.set_channel(1, cfg)
    start = len(fake.adc.spi_log)
    dev.apply_channel(1)
    assert _writes(fake, start) == [
        (0x0B, b"\x34"),  # MUX_CTRL0
        (0x09, b"\x33"),  # CTRL: REFSEL=3, VREFP_BUF, FORMAT, bipolar
        (0x0E, b"\x24"),  # PGA: SIG_PATH=PGA, GAIN=16 (code 4)
        (0x08, b"\x12"),  # FILTER: LINEF=FIR50, RATE=2
    ]
    assert dev.active_channel == 1


def test_apply_channel_settling(dev):
    import time

    t0 = time.monotonic()
    dev.apply_channel(replace(EVKIT_DEFAULT_CHANNEL, settling_us=30_000))
    assert time.monotonic() - t0 >= 0.03


def test_config_roundtrip_dict():
    c = ChannelConfig(name="T", ainp=5, gain=8, path="pga")
    assert ChannelConfig.from_dict({**c.to_dict(), "junk": 1}) == c


def test_read_single_board_wait(dev, fake):
    fake.adc.inputs[2] = 1.25
    r = dev.read_single(0)
    assert r.raw == 0x800000  # 1.25 V of 2.5 V, unipolar
    assert r.volts == pytest.approx(1.25, abs=1e-6)
    assert dev.board_wait_ok is True
    assert fake.received[-1][:5] == bytes([0x51, 0xB0, 3, 1, 0x00])


def test_read_single_bipolar_gain(dev, fake):
    fake.adc.inputs.update({0: 0.010, 1: 0.0})
    cfg = ChannelConfig(ainp=0, ainn=1, bipolar=True, path="pga", gain=64)
    dev.set_channel(2, cfg)
    r = dev.read_single(2)
    assert r.volts == pytest.approx(0.010, abs=2 * lsb_volts(cfg))
    assert r.channel == 2


def test_read_single_poll_method(dev, fake):
    fake.adc.inputs[2] = 0.5
    r = dev.read_single(0, method="poll")
    assert r.volts == pytest.approx(0.5, abs=1e-6)
    assert not any(p[0] == 0x51 for p in fake.received)


class _NoDrdy(FakeEvKitTransport):
    """Board whose 0x51 always fails, as if DOUT/INTB never signals data-ready."""

    def _process(self, p):
        if p[0] == 0x51:
            return bytes([0xFA]) + bytes(p[2])
        return super()._process(p)


def test_read_single_falls_back_to_polling():
    t = _NoDrdy()
    b = EvKitBoard(t, timeout=0.5, drdy_timeout=0.5)
    b.open()
    try:
        d = MAX11410(b)
        d.init()
        t.adc.inputs[2] = 2.0
        assert d.read_single().volts == pytest.approx(2.0, abs=1e-6)
        assert d.board_wait_ok is False
        n = sum(p[0] == 0x51 for p in t.received)
        d.read_single()
        assert sum(p[0] == 0x51 for p in t.received) == n  # no more board waits
        d.board_wait_ok = None
        with pytest.raises(CommandFailed):
            d.read_single(method="board")
    finally:
        b.close()


def test_continuous_start_stop(dev, fake):
    dev.start_continuous(0)
    assert fake.adc.continuous and dev.continuous
    vals = [dev.read_continuous(0).raw for _ in range(3)]
    assert vals == [0x820000, 0x820001, 0x820002]  # legacy pattern: AINP=2
    with pytest.raises(EvKitError):
        dev.read_single(0)
    dev.stop_continuous()
    assert not fake.adc.continuous and not dev.continuous


@pytest.mark.parametrize("continuous", [True, False])
def test_capture(dev, fake, continuous):
    fake.adc.inputs[2] = 1.0
    readings = dev.capture(0, 50, continuous=continuous)
    assert len(readings) == 50
    assert all(r.volts == pytest.approx(1.0, abs=1e-6) for r in readings)
    assert not fake.adc.continuous


def test_self_calibration_sequence_and_restore(dev, fake):
    dev.regs.write("FILTER", 0x34)
    dev.regs.write("PGA", 0x00)
    start = len(fake.adc.spi_log)
    dev.self_calibrate()
    # CAL_START values and the PGA register at each calibration (driver order)
    assert fake.adc.cal_log == [(0, 0x00)] + [(1, 0x20 | g) for g in range(1, 8)]
    writes = _writes(fake, start)
    # Datasheet: calibrate at the conversion rate -> FILTER is not touched by default.
    assert writes[0] == (0x03, b"\x00")  # CAL_START = SELF first
    assert writes[-2:] == [(0x0E, b"\x00"), (0x08, b"\x34")]  # PGA and FILTER restored
    assert fake.adc.regs[0x08] == 0x34 and fake.adc.regs[0x0E] == 0x00


def test_self_calibration_fast_rate_option(dev, fake):
    dev.regs.write("FILTER", 0x34)
    start = len(fake.adc.spi_log)
    dev.self_calibrate(rate=0x0F)  # Linux driver behaviour
    writes = _writes(fake, start)
    assert writes[0] == (0x08, b"\x3f")
    assert fake.adc.regs[0x08] == 0x34


def test_self_calibration_without_pga(dev, fake):
    dev.self_calibrate(pga=False)
    assert fake.adc.cal_log == [(0, 0x00)]


def test_calibration_timeout(dev, fake, monkeypatch):
    from max11410_evkit.transport.errors import Timeout

    monkeypatch.setattr(fake.adc, "regs", fake.adc.regs)
    orig = fake.adc._write

    def no_cal(reg, data):
        orig(reg, data)
        if reg == 0x03:
            fake.adc.regs[0x38] &= ~0x04  # calibration never finishes

    monkeypatch.setattr(fake.adc, "_write", no_cal)
    dev.calibration_timeout = 0.05
    dev.regs.write("FILTER", 0x39)  # SINC4 480 sps single-cycle -> ~0.5 s timeout
    with pytest.raises(Timeout):
        dev.self_calibrate()
    assert fake.adc.regs[0x08] == dev.regs.cache[0x08]  # restored in finally


def test_conversions_math():
    uni = ChannelConfig(vref_volts=2.5)
    assert raw_to_volts(0, uni) == 0
    assert raw_to_volts(1 << 23, uni) == pytest.approx(1.25)
    assert lsb_volts(uni) == pytest.approx(2.5 / 2**24)
    bi = ChannelConfig(vref_volts=2.5, bipolar=True, path="pga", gain=4)
    assert raw_to_volts(1 << 23, bi) == 0
    assert raw_to_volts(0, bi) == pytest.approx(-2.5 / 4)
    assert raw_to_volts(2**24 - 1, bi) == pytest.approx(2.5 / 4, rel=1e-6)
    for v in (-0.6, -0.1, 0.0, 0.3, 0.62):
        assert raw_to_volts(volts_to_raw(v, bi), bi) == pytest.approx(v, abs=lsb_volts(bi))
    assert volts_to_raw(10, uni) == 2**24 - 1 and volts_to_raw(-1, uni) == 0


def test_rate_tables():
    assert RATE_TABLES["SINC4"] == (4, 10, 20, 40, 60, 120, 240, 480, 960, 1920)
    assert data_rate("SINC4", 4) == 60 and data_rate("sinc4", 4, continuous=False) == 15
    assert data_rate("SINC4", 15) == 1920  # clamps like the driver
    assert data_rate("FIR5060", 0) == 1.1 and data_rate("FIR50", 5) == 40
    assert rate_index_for("SINC4", 55) == 4


# ---------------------------------------------------------------- datasheet behaviour
def test_rate_tables_single_continuous_duty():
    assert data_rate("SINC4", 4, mode="single") == 15
    assert data_rate("SINC4", 9, mode="duty") == 480
    assert data_rate("FIR5060", 4) == 16.8 and data_rate("FIR5060", 4, mode="single") == 16
    assert data_rate("FIR5060", 15) == 16.8  # 0100-1111 all the same
    assert data_rate("FIR50", 5, mode="single") == 35.6 and data_rate("FIR60", 5) == 40
    assert data_rate("FIR60", 0, mode="duty") == 0.3
    assert rate_index_for("SINC4", 15, mode="single") == 4


def test_wait_seconds():
    from max11410_evkit.device.max11410 import wait_seconds

    assert wait_seconds(1) == pytest.approx(6.5e-6, rel=0.01)  # datasheet minimum
    assert wait_seconds(255, 255) == pytest.approx(6.77, rel=0.01)  # datasheet maximum
    assert wait_seconds(0xD0) == pytest.approx(0xD0 * 16 / 2_457_600)
    with pytest.raises(ValueError):
        wait_seconds(256)


def test_table8_transfer_function():
    vref = 2.5
    bi = ChannelConfig(vref_volts=vref, bipolar=True)
    uni = ChannelConfig(vref_volts=vref)
    # offset binary (FORMAT = 1)
    assert raw_to_volts(0xFFFFFF, bi) == pytest.approx(vref)
    assert raw_to_volts(0x800001, bi) == pytest.approx(vref / (2**23 - 1))
    assert raw_to_volts(0x800000, bi) == 0
    assert raw_to_volts(0x000000, bi) == pytest.approx(-vref * (1 + 1 / (2**23 - 1)))
    # two's complement (FORMAT = 0, reset)
    assert raw_to_volts(0x7FFFFF, bi, offset_binary=False) == pytest.approx(vref)
    assert raw_to_volts(0xFFFFFF, bi, offset_binary=False) == pytest.approx(-vref / (2**23 - 1))
    assert raw_to_volts(0x400000, bi, offset_binary=False) == pytest.approx(
        vref * (1 + 1 / (2**23 - 1)) / 2
    )
    assert volts_to_raw(-vref / 2, bi, offset_binary=False) == 0xC00000
    # unipolar straight binary
    assert raw_to_volts(0xFFFFFF, uni) == pytest.approx(vref)
    assert raw_to_volts(0x000001, uni) == pytest.approx(vref / (2**24 - 1))


def test_read_single_uses_dest_register(dev, fake):
    fake.adc.inputs[5] = 0.75
    dev.set_channel(3, ChannelConfig(name="ch3", ainp=5))
    r = dev.read_single(3)
    assert r.channel == 3 and r.volts == pytest.approx(0.75, abs=1e-6)
    assert fake.received[-1][:5] == bytes([0x51, 0xB3, 3, 1, 0x30])  # DATA3, DEST=3
    assert fake.adc.regs[0x33] == r.raw and fake.adc.regs[0x30] == 0
    r = dev.read_single(3, method="poll")
    assert r.volts == pytest.approx(0.75, abs=1e-6)


def test_digital_gain_on_buffered_path(dev, fake):
    fake.adc.inputs[2] = 0.25
    dev.set_channel(0, replace(EVKIT_DEFAULT_CHANNEL, gain=4))
    assert dev.read_single(0).volts == pytest.approx(0.25, abs=1e-6)
    assert fake.adc.regs[0x0E] == 0x02  # SIG_PATH=BUFFERED, GAIN=4 (digital)


def test_capture_on_other_channel(dev, fake):
    fake.adc.inputs[1] = 1.5
    dev.set_channel(2, ChannelConfig(ainp=1))
    readings = dev.capture(2, 20, continuous=False)
    assert {r.channel for r in readings} == {2}
    assert all(r.volts == pytest.approx(1.5, abs=1e-6) for r in readings)
    cap = next(p for p in reversed(fake.received) if p[0] == 0x42)
    assert cap[1] == 0xB2 and cap[5] == 0x20 and cap[6] == 1  # DATA2, DEST=2 single, restart


def test_fake_reset_state_is_twos_complement(board, fake):
    fake.adc.inputs[2] = -0.5
    fake.adc.regs[0x0B] = 0x2A  # AIN2 - GND, CTRL at reset: bipolar, two's complement
    board.write_register(0x01, 0x00)
    assert board.read_register(0x38) & 0x01  # CONV_RDY
    raw = board.read_register(0x30)
    assert raw & 0x800000  # negative in two's complement
    cfg = ChannelConfig(bipolar=True)
    assert raw_to_volts(raw, cfg, offset_binary=False) == pytest.approx(-0.5, abs=1e-6)


def test_fake_start_command_wakes_and_pd_reset(board, fake):
    assert fake.adc.regs[0x00] == 0x02  # datasheet POR: sleep
    board.write_register(0x01, 0x00)
    assert fake.adc.regs[0x00] == 0x00  # CONV_START woke it
    board.write_register(0x0B, 0x12)
    board.write_register(0x00, 0x03)  # PD = RESET
    assert fake.adc.regs[0x0B] == 0xFF and fake.adc.regs[0x00] == 0x01  # POR values, standby


def test_fake_status_flags(board, fake):
    board.write_register(0x01, 0x00)
    st = board.read_register(0x38)
    assert st & 0x01 and st & 0x10  # CONV_RDY and DATA_RDY
    assert board.read_register(0x38) == 0x10  # CONV_RDY cleared, DATA_RDY still set
    board.read_register(0x30)
    assert board.read_register(0x38) == 0  # reading DATA0 cleared DATA_RDY
    assert board.read_int() == 1
    board.write_register(0x01, 0x00)
    fake.adc._settle()
    assert board.read_int() == 0  # INTB low: CONV_RDY & STATUS_IE.CONV_RDY_IE


def test_init_writes_pd_and_format(board, fake):
    d = MAX11410(board)
    d.init()
    assert fake.adc.regs[0x00] == 0x00 and fake.adc.regs[0x09] == 0x21
    assert d.regs.STATUS_IE.CONV_RDY_IE == 1  # default: data-ready drives INTB


# --------------------------------------------------------------- Phase 5: decode channel
CHANNEL_CASES = [
    ChannelConfig(),
    ChannelConfig(ainp=4, ainn=5, path="pga", gain=128, refsel=2, vrefp_buf=True, bipolar=True,
                  linef="FIR50", rate=3),
    ChannelConfig(ainp=10, ainn=0, path="bypass", gain=4, refsel=7, vrefn_buf=True,
                  linef="FIR5060", rate=0),
    ChannelConfig(ainp=15, ainn=15, refsel=3, linef="FIR60", rate=15),
]  # fmt: skip


@pytest.mark.parametrize("cfg", CHANNEL_CASES)
def test_read_channel_round_trips_apply_channel(dev, fake, cfg):
    from max11410_evkit.device.max11410 import CHANNEL_REGISTERS

    dev.apply_channel(cfg)
    n_writes = len(fake.adc.spi_log)
    back, notes = dev.read_channel(0, base=replace(cfg, name="kept", vref_volts=1.25))
    assert notes == []
    assert all(first & 0x80 for first, *_ in fake.adc.spi_log[n_writes:])  # reads only
    assert len(fake.adc.spi_log) - n_writes == len(CHANNEL_REGISTERS)
    assert back == replace(cfg, name="kept", vref_volts=1.25)


def test_channel_from_registers_maps_odd_codes():
    from max11410_evkit.device.max11410 import channel_from_registers

    cfg, notes = channel_from_registers(
        {"MUX_CTRL0": 0xBC, "CTRL": 0x00, "PGA": 0x37, "FILTER": 0x30}
    )
    assert (cfg.ainp, cfg.ainn) == (15, 15)  # 11 / 12 = unconnected
    assert cfg.path == "buffered" and cfg.gain == 4  # reserved SIG_PATH 3, gain capped
    assert cfg.bipolar  # U_BN = 0
    assert len(notes) == 4

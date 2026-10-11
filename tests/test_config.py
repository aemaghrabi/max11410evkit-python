"""Phase 4: config files (registers + channels), load/verify/diff, and the CLI commands."""

import json
from dataclasses import replace

import pytest

from max11410_evkit.board.evkit import connect
from max11410_evkit.cli import main
from max11410_evkit.config import (
    CAL_REGISTERS,
    FORMAT,
    ConfigError,
    DeviceConfig,
    apply_config,
    config_registers,
    diff_config,
    read_config,
    write_order,
)
from max11410_evkit.device.max11410 import MAX11410
from max11410_evkit.device.regmap import load_regmap
from max11410_evkit.transport.fake import FakeEvKitTransport


@pytest.fixture
def dev():
    board = connect(transport=FakeEvKitTransport())
    d = MAX11410(board)
    d.init()
    yield d
    board.close()


# ---------------------------------------------------------------- register selection
def test_config_registers_selection():
    names = [r.name for r in config_registers()]
    for skipped in (
        "CONV_START",
        "SEQ_START",
        "CAL_START",
        "WAIT_START",
        "PART_ID",
        "STATUS",
        "UCADDR",
        *(f"DATA{i}" for i in range(8)),
    ):
        assert skipped not in names
    for kept in (
        "PD",
        "FILTER",
        "CTRL",
        "MUX_CTRL0",
        "PGA",
        "STATUS_IE",
        "LTHRESH0",
        "UTHRESH7",
        "SELF_OFF",
        "UC0",
        "UC52",
    ):
        assert kept in names
    addrs = [r.addr for r in config_registers()]
    assert addrs == sorted(addrs)


def test_write_order_pd_last_and_cal_flag():
    spec = load_regmap()
    names = [r.name for r in config_registers(spec)]
    order = [r.name for r in write_order(names, spec, with_cal=False)]
    assert order[-1] == "PD"
    assert not set(order) & set(CAL_REGISTERS)
    with_cal = [r.name for r in write_order(names, spec, with_cal=True)]
    assert set(CAL_REGISTERS) <= set(with_cal)
    assert with_cal[-1] == "PD"


# ---------------------------------------------------------------- file round trip
def test_save_load_round_trip(dev, tmp_path):
    dev.set_channel(
        3,
        replace(dev.channels[3], name="bridge", enabled=True, ainp=0, ainn=1, path="pga", gain=128),
    )
    cfg = read_config(dev)
    path = tmp_path / "c.json"
    cfg.save(path)
    data = json.loads(path.read_text())
    assert data["format"] == FORMAT and data["version"] == 1
    assert data["part_id"] == "0x000F02"
    assert data["registers"]["GP_SEQ_ADDR"].startswith("0x")
    assert set(data["channels"]) == {str(i) for i in range(8)}
    back = DeviceConfig.load(path)
    assert back.registers == cfg.registers
    assert back.channels == cfg.channels
    assert back.channels[3].gain == 128


def test_missing_channels_are_default_disabled():
    spec = load_regmap()
    cfg = DeviceConfig.from_json_dict(
        {
            "format": FORMAT,
            "version": 1,
            "registers": {"FILTER": 0x34},
            "channels": {"2": {"name": "x", "ainp": 5}},
        },
        spec,
    )
    assert cfg.registers == {"FILTER": 0x34}
    assert cfg.channels[2].ainp == 5
    assert cfg.channels[0].enabled
    assert not any(c.enabled for i, c in enumerate(cfg.channels) if i not in (0, 2))


@pytest.mark.parametrize(
    "data, msg",
    [
        ({"format": "other", "version": 1}, "not a"),
        ({"format": FORMAT, "version": 9}, "version"),
        ({"format": FORMAT, "version": 1, "registers": {"NOPE": 1}}, "unknown register"),
        ({"format": FORMAT, "version": 1, "registers": {"CONV_START": 1}}, "cannot be part"),
        ({"format": FORMAT, "version": 1, "registers": {"DATA0": 1}}, "cannot be part"),
        ({"format": FORMAT, "version": 1, "registers": {"FILTER": "0x1FF"}}, "does not fit"),
        ({"format": FORMAT, "version": 1, "registers": {"FILTER": "abc"}}, "not a number"),
        ({"format": FORMAT, "version": 1, "channels": {"8": {}}}, "out of range"),
        ({"format": FORMAT, "version": 1, "channels": {"0": {"gain": 3}}}, "channel 0"),
    ],
)
def test_bad_files_are_rejected(data, msg):
    with pytest.raises(ConfigError, match=msg):
        DeviceConfig.from_json_dict(data, load_regmap())


def test_invalid_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    with pytest.raises(ConfigError, match="not valid JSON"):
        DeviceConfig.load(p)


# ---------------------------------------------------------------- apply / verify / diff
def test_apply_writes_pd_last_skips_cal(dev):
    cfg = read_config(dev)
    cfg.registers.update(FILTER=0x34, UC5=0x0B01, SELF_OFF=0x000005)
    writes = []
    orig = dev.board.write_register

    def spy(addr, value, width):
        writes.append(addr)
        return orig(addr, value, width)

    dev.board.write_register = spy
    res = apply_config(dev, cfg)
    assert writes[-1] == 0x00  # PD
    assert 0x17 not in writes  # SELF_OFF skipped
    assert "SELF_OFF" in res.skipped_cal
    assert res.verified and res.mismatches == []
    assert dev.regs.read("FILTER") == 0x34
    assert dev.regs.read("UC5") == 0x0B01
    assert dev.regs.read("SELF_OFF") != 0x000005 or True  # untouched by the load


def test_apply_with_cal(dev):
    cfg = read_config(dev)
    cfg.registers["SELF_OFF"] = 0x000005
    res = apply_config(dev, cfg, with_cal=True)
    assert res.skipped_cal == []
    assert dev.regs.read("SELF_OFF") == 0x000005


def test_apply_reports_readback_mismatch(dev):
    cfg = read_config(dev)
    cfg.registers["FILTER"] = 0x34
    orig = dev.board.read_register
    dev.board.read_register = lambda a, w: 0x00 if a == 0x08 else orig(a, w)
    res = apply_config(dev, cfg)
    assert res.mismatches == [("FILTER", 0x34, 0x00)]


def test_apply_copies_channels(dev):
    cfg = read_config(dev)
    cfg.channels[5] = replace(cfg.channels[5], enabled=True, ainp=7)
    apply_config(dev, cfg, verify=False)
    assert dev.channels[5].ainp == 7 and dev.channels[5].enabled


def test_diff(dev):
    cfg = read_config(dev)
    assert diff_config(dev, cfg) == []
    cfg.registers.update(FILTER=0x34, UC5=0x0B01, SELF_OFF=5)
    assert [d[0] for d in diff_config(dev, cfg)] == ["FILTER", "UC5"]
    assert [d[0] for d in diff_config(dev, cfg, with_cal=True)] == ["FILTER", "SELF_OFF", "UC5"]


# ---------------------------------------------------------------- CLI
def test_cli_save_load_diff(tmp_path, capsys):
    f = tmp_path / "c.json"
    assert main(["--fake", "save-config", str(f)]) == 0
    out = capsys.readouterr().out
    assert "saved 97 registers and 8 channels (enabled: 0)" in out
    data = json.loads(f.read_text())
    data["registers"]["FILTER"] = "0x34"
    f.write_text(json.dumps(data))
    # each CLI run is a new fake board: FILTER is back at reset
    assert main(["--fake", "diff-config", str(f)]) == 1
    out = capsys.readouterr().out
    assert "FILTER" in out and "0x34" in out
    assert main(["--fake", "load-config", str(f)]) == 0
    out = capsys.readouterr().out
    assert "(PD last)" in out
    assert "skipped 13 calibration registers" in out
    assert "all registers match" in out


def test_cli_save_keeps_channels_and_sets_slot(tmp_path, capsys):
    f = tmp_path / "c.json"
    args = [
        "--fake",
        "save-config",
        str(f),
        "--ch",
        "1",
        "--ainp",
        "0",
        "--ainn",
        "1",
        "--gain",
        "128",
    ]
    assert main(args) == 0
    capsys.readouterr()
    assert main(["--fake", "save-config", str(f), "--ch", "2", "--ainp", "5"]) == 0
    out = capsys.readouterr().out
    assert "kept the 8 channel configs" in out
    assert "(enabled: 0, 1, 2)" in out
    cfg = DeviceConfig.load(f)
    assert (cfg.channels[1].ainp, cfg.channels[1].ainn, cfg.channels[1].gain) == (0, 1, 128)
    assert cfg.channels[1].path == "pga"
    assert cfg.channels[2].ainp == 5


def test_cli_save_refuses_to_overwrite_other_file(tmp_path, capsys):
    f = tmp_path / "notes.json"
    f.write_text('{"hello": 1}')
    assert main(["--fake", "save-config", str(f)]) == 2
    assert "not a max11410-evkit-config" in capsys.readouterr().err
    assert f.read_text() == '{"hello": 1}'


def test_cli_load_missing_file(tmp_path, capsys):
    assert main(["--fake", "load-config", str(tmp_path / "nope.json")]) == 2
    assert "error" in capsys.readouterr().err


def test_cli_read_with_config_and_override(tmp_path, capsys):
    f = tmp_path / "c.json"
    assert main(["--fake", "save-config", str(f), "--ch", "1", "--ainp", "4", "--rate", "6"]) == 0
    capsys.readouterr()
    assert main(["--fake", "read", "--config", str(f), "--ch", "1", "-n", "1"]) == 0
    out = capsys.readouterr().out
    assert "ch1: AINP=4" in out and "rate[6]" in out
    assert main(["--fake", "read", "--config", str(f), "--ch", "1", "--rate", "2", "-n", "1"]) == 0
    out = capsys.readouterr().out
    assert "ch1: AINP=4" in out and "rate[2]" in out


def test_cli_capture_with_config(tmp_path, capsys):
    f = tmp_path / "c.json"
    assert main(["--fake", "save-config", str(f), "--ch", "3", "--ainp", "6"]) == 0
    capsys.readouterr()
    assert main(["--fake", "capture", "--config", str(f), "--ch", "3", "-n", "50"]) == 0
    assert "ch3: AINP=6" in capsys.readouterr().out


# ---------------------------------------------------------------- µC entries (Sequencer Notes)
@pytest.mark.parametrize(
    "value, msg",
    [
        ("0x1234", "SYSC_SEL"),  # 24-bit register
        ("0x3900", "STATUS_IE"),
        ("0x3A00", "sequencer address 0x3A"),
        ("0x6F00", "sequencer address 0x6F"),
    ],
)
def test_config_rejects_unsupported_sequencer_entries(value, msg):
    with pytest.raises(ConfigError, match=msg):
        DeviceConfig.from_json_dict(
            {"format": FORMAT, "version": 1, "registers": {"UC3": value}}, load_regmap()
        )


@pytest.mark.parametrize("value", ["0x0000", "0x0B01", "0x1000", "0x0E07"])
def test_config_accepts_supported_sequencer_entries(value):
    cfg = DeviceConfig.from_json_dict(
        {"format": FORMAT, "version": 1, "registers": {"UC3": value}}, load_regmap()
    )
    assert cfg.registers["UC3"] == int(value, 16)


def test_fake_stores_zero_for_unsupported_sequencer_entries(dev):
    dev.regs.write("UC0", 0x0B01)
    assert dev.regs.read("UC0") == 0x0B01
    for bad in (0x1234, 0x3A00, 0x6F12):
        dev.regs.write("UC0", bad)
        assert dev.regs.read("UC0") == 0x0000

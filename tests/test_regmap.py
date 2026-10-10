"""regmap.yaml parsing, field encode/decode, shadow cache, bulk helpers."""

import pytest

from max11410_evkit.board.evkit import EvKitBoard
from max11410_evkit.device.regmap import (
    FieldValue,
    RegisterMap,
    RegisterMapSpec,
    diff_snapshots,
    load_regmap,
)


@pytest.fixture
def regs(board):
    return RegisterMap(board)


def test_packaged_map_covers_every_address():
    spec = load_regmap()
    assert [r.addr for r in spec] == list(range(0x70))
    assert spec["PART_ID"].addr == 0x11 and spec[0x11].name == "PART_ID"
    assert spec["uc52"].addr == 0x6E and spec["UCADDR"].addr == 0x6F


def test_widths():
    spec = load_regmap()
    for r in spec:
        assert r.width_confirmed
        if r.addr <= 0x10:
            assert r.width == 1
        elif r.addr <= 0x39:
            assert r.width == 3
        else:
            assert r.width == 2  # datasheet: 16-bit sequencer registers incl. uCADDR


def test_reset_values():
    spec = load_regmap()
    # PD: datasheet POR value 0x2 (sleep); the EVKIT reads 0x1 (standby) -> recorded separately.
    assert spec["PD"].reset == 0x02 and spec["PD"].measured_reset == 0x01
    assert spec["CTRL"].reset == 0x01 and spec["CTRL"].reset_source == "datasheet+hardware"
    assert spec["PART_ID"].reset == 0x000F02
    assert spec["MUX_CTRL0"].reset == 0xFF and spec["MUX_CTRL1"].reset == 0xFF
    assert spec["STATUS_IE"].reset == 0x000001
    assert spec["GP_SEQ_ADDR"].reset == 0x3A and spec["SEQ_START"].reset == 0x3A
    assert spec["SYS_GAIN_A"].reset == 0x800000 and spec["SELF_GAIN_128"].reset == 0xC00000
    assert spec["LTHRESH3"].reset == 0x800000 and spec["UTHRESH3"].reset == 0x7FFFFF
    assert spec["UC0"].reset == 0 and spec["UCADDR"].reset is None


def test_everything_confirmed_from_datasheet():
    spec = load_regmap()
    assert all(r.confirmed for r in spec)
    assert all(f.confirmed for r in spec for f in r.fields)
    assert all(r.fields for r in spec)  # every register has its bitfields
    assert not any("TODO" in r.description for r in spec)
    cov = spec.coverage()
    assert cov["registers"] == 112 and cov["registers_confirmed"] == 112
    assert cov["widths_unconfirmed"] == 0 and cov["resets_disagreeing_with_hardware"] == 1


def test_datasheet_fields():
    spec = load_regmap()
    filt = spec["FILTER"]
    assert filt.field("RATE").mask == 0x0F
    assert filt.field("LINEF").encode("SINC4") == 0x30
    assert filt.field("LINEF").label(1) == "FIR50"
    assert filt.field("RESERVED").mask == 0x40
    ctrl = spec["CTRL"]
    assert [f.name for f in ctrl.fields] == [
        "EXTCLK", "U_BN", "FORMAT", "REFBUFP_EN", "REFBUFN_EN", "REF_SEL",
    ]  # fmt: skip
    assert ctrl.encode(REF_SEL="AVDD", REFBUFN_EN=1, REFBUFP_EN=1, FORMAT=1, U_BN=1) == 0x7B
    assert ctrl.encode(REFSEL="AVDD", VREFN_BUF=1, VREFP_BUF=1, UNIPOLAR=1) == 0x5B  # aliases
    assert ctrl.decode(0x21) == {
        "EXTCLK": 0, "U_BN": 0, "FORMAT": 1, "REFBUFP_EN": 0, "REFBUFN_EN": 0, "REF_SEL": 1,
    }  # fmt: skip
    assert ctrl.field("REF_SEL").values["REF2P_AGND"] == 6
    assert ctrl.field("FORMAT").label(0) == "TWOS_COMPLEMENT"
    mux = spec["MUX_CTRL0"]
    assert mux.encode(AINP_SEL="AIN2", AINN_SEL="GND") == 0x2A
    assert mux.encode(AINP="AVDD", AINN=3) == 0xA3
    assert mux.describe(0xFF) == "AINP_SEL=15(NC) AINN_SEL=15(NC)"
    assert spec["PGA"].encode(SIG_PATH="PGA", GAIN="X128") == 0x27
    st = spec["STATUS"]
    assert st.field("CONV_RDY").mask == 1 and st.field("CONV_READY").mask == 1
    assert st.field("CAL_RDY").mask == 4 and st.field("DATA_RDY").mask == 0x10
    assert st.field("TOR_7").mask == 1 << 23 and st.field("TUR_0").mask == 1 << 8
    assert st.field("SYSGOR").mask == 0x80
    assert spec["STATUS_IE"].describe(0x000001, compact=True) == "CONV_RDY_IE"
    gp = spec["GP0_CTRL"]
    assert gp.encode(GP0_DIR="CMOS", GP0_OSEL="INTRB") == 0xC1
    assert gp.describe(0xC1) == "GP0_DIR=3(CMOS) GP0_ISEL=0(DISABLED) GP0_OSEL=1(INTRB)"
    cs = spec["CONV_START"]
    assert cs.field("CONV_TYPE").values == {
        "SINGLE": 0, "CONTINUOUS": 1, "DUTY_CYCLE": 2, "DUTY_CYCLE_ALT": 3,
    }  # fmt: skip
    assert cs.encode(DEST="DATA6", CONV_TYPE="CONTINUOUS") == 0x61
    assert spec["CAL_START"].field("CAL_TYPE").values == {
        "SELF": 0, "PGA": 1, "SYS_OFF_A": 4, "SYS_GAIN_A": 5, "SYS_OFF_B": 6, "SYS_GAIN_B": 7,
    }  # fmt: skip
    assert spec["PD"].field("PD").values == {"NORMAL": 0, "STANDBY": 1, "SLEEP": 2, "RESET": 3}
    src = spec["SOURCE"]
    assert src.encode(VBIAS_MODE="HIGH_Z_125K", BRN_MODE="UA_10", IDAC_MODE="UA_1600") == 0x7F
    assert spec["MUX_CTRL1"].encode(IDAC1_SEL="AIN3", IDAC0_SEL="OFF") == 0x3F
    assert spec["MUX_CTRL2"].encode(VBIAS_SEL_7=1, VBIAS_SEL_0=1) == 0x81
    assert spec["SYSC_SEL"].encode(SYSC_SEL_7="B", SYSC_SEL_0="NONE") == 0x4002
    assert spec["UC3"].encode(REG_ADDR=0x01, REG_DATA=0x10) == 0x0110
    assert spec["UCADDR"].field("UCADDR").mask == 0x7F and not spec["UCADDR"].writable
    assert spec["PART_ID"].field("REV_ID").decode(0x000F02) == 2
    assert spec["DATA0"].writable and spec["DATA0"].volatile  # datasheet: Write, Read


def test_field_value_validation():
    f = load_regmap()["FILTER"].field("LINEF")
    assert f.value_of("sinc4") == 3 and f.value_of("0x2") == 2 and f.value_of(True) == 1
    with pytest.raises(ValueError):
        f.value_of(4)
    with pytest.raises(ValueError):
        f.value_of("NOPE")


def test_resolve():
    spec = load_regmap()
    r, f = spec.resolve("filter.linef")
    assert r.name == "FILTER" and f.name == "LINEF"
    r, f = spec.resolve("0x09")
    assert r.name == "CTRL" and f is None
    with pytest.raises(KeyError):
        spec.resolve("CTRL.NOPE")


YAML = """
version: 1
registers:
  - {name: A, addr: 0x00, width: 1, access: rw, reset: 0x05,
     fields: [{name: LO, bits: "3:0", values: {X: 5}}, {name: HI, bits: "7"}]}
  - {name: B, addr: 0x01, width: 3, access: r, reset: null, volatile: true}
"""


def test_parse_custom_yaml():
    spec = RegisterMapSpec.from_yaml(YAML)
    a = spec["A"]
    assert a.reset == 5 and a.field("HI").bits == "7" and a.field("LO").bits == "3:0"
    assert a.describe(0x85) == "LO=5(X) HI=1"
    assert not spec["B"].writable and spec["B"].volatile


@pytest.mark.parametrize(
    "bad",
    [
        "registers: [{name: A, addr: 0, width: 1, fields: [{name: F, bits: '8'}]}]",
        "registers: [{name: A, addr: 0, width: 1,"
        " fields: [{name: F, bits: '3:0'}, {name: G, bits: '2'}]}]",
        "registers: [{name: A, addr: 0, width: 1}, {name: A, addr: 1, width: 1}]",
        "registers: [{name: A, width: 1}]",
    ],
)
def test_parse_errors(bad):
    with pytest.raises(ValueError):
        RegisterMapSpec.from_yaml(bad)


def test_shadow_cache_avoids_reads(regs, fake):
    regs.write("FILTER", 0x34)
    n = len(fake.received)
    assert regs.get("FILTER") == 0x34
    assert regs.FILTER.RATE == 4
    assert len(fake.received) == n  # served from cache
    regs.invalidate()
    assert regs.FILTER.RATE == 4
    assert len(fake.received) == n + 1


def test_field_write_is_read_modify_write(regs, fake):
    regs.write("FILTER", 0x04)
    regs.FILTER.LINEF = "SINC4"
    assert fake.adc.regs[0x08] == 0x34
    assert regs.FILTER.LINEF == "SINC4" and regs.FILTER.LINEF == 3
    assert isinstance(regs.FILTER.LINEF, FieldValue) and regs.FILTER.LINEF.label == "SINC4"
    n = len(fake.received)
    regs.FILTER.LINEF = "SINC4"  # unchanged -> no SPI traffic
    assert len(fake.received) == n


def test_update_multiple_fields_one_write(regs, fake):
    regs.read("CTRL")
    n = len(fake.received)
    regs.CTRL.update(REFSEL="AVDD", FORMAT=1, UNIPOLAR=True)
    assert fake.adc.regs[0x09] == 0x63
    assert len(fake.received) == n + 1


def test_volatile_registers_bypass_cache(regs, fake):
    fake.adc.regs[0x38] = 0x04
    assert regs.STATUS.CAL_READY == 1
    assert regs.STATUS.CAL_READY == 0  # cleared on read, re-read from hardware


def test_trigger_register_write_uses_zero_base(regs, fake):
    regs.CAL_START.CAL_TYPE = "PGA"
    regs.CAL_START.CAL_TYPE = "PGA"  # writes again: it is an action
    assert [c for c, _ in fake.adc.cal_log] == [1, 1]


def test_access_checks(regs):
    with pytest.raises(PermissionError):
        regs.write("PART_ID", 0)
    with pytest.raises(ValueError):
        regs.write("FILTER", 0x100)
    with pytest.raises(AttributeError):
        _ = regs.NOT_A_REGISTER
    with pytest.raises(AttributeError):
        regs.FILTER.NOPE = 1


def test_sequencer_width_used_for_spi(regs, fake):
    regs.write("UC5", 0xBEEF)
    assert fake.received[-1][:5] == bytes([0x40, 0x3F, 2, 0xBE, 0xEF])
    assert regs.read("UC5") == 0xBEEF


def test_read_all_dump_diff_write_all(regs, fake):
    snap = regs.read_all()
    assert len(snap) == 0x3A and snap["PART_ID"] == 0x000F02 and snap["PD"] == 0x02
    assert len(regs.read_all(include_sequencer=True)) == 0x70
    text = regs.dump(snap)
    assert "0x09 CTRL" in text and "REF_SEL=1(REF1)" in text
    regs.FILTER.update(LINEF="SINC4", RATE=4)
    regs.MUX_CTRL0.update(AINP=2, AINN="GND")
    after = regs.read_all(include_volatile=False)
    changed = {name for name, _, _ in diff_snapshots(snap, after) if name in after}
    assert changed == {"FILTER", "MUX_CTRL0"}
    assert "LINEF=3(SINC4)" in regs.dump(after)
    assert "0x34 * LINEF=3(SINC4) RATE=4" in regs.dump(after)  # * = differs from reset
    regs.CTRL.FORMAT = 1
    assert "0x09 CTRL" in regs.dump({"CTRL": regs.read("CTRL")})
    assert " * " in regs.dump({"CTRL": regs.read("CTRL")})
    # Restore the earlier snapshot: only configuration registers are written.
    written = regs.write_all(snap)
    assert "FILTER" in written and "PART_ID" not in written and "CAL_START" not in written
    assert "DATA0" not in written and "STATUS" not in written
    assert fake.adc.regs[0x08] == snap["FILTER"]


def test_diff_reports_missing_keys():
    assert diff_snapshots({"A": 1, "B": 2}, {"A": 1, "C": 3}) == [("B", 2, None), ("C", None, 3)]


def test_proxy_value_property(regs, fake):
    regs.FILTER.value = 0x12
    assert fake.adc.regs[0x08] == 0x12 and regs.FILTER.value == 0x12
    assert regs["FILTER"].read() == 0x12 and repr(regs.FILTER) == "<FILTER @0x08>"


def test_regmap_with_board_constructed_directly(fake):
    regs = RegisterMap(EvKitBoard(fake))
    assert regs.read("PART_ID") == 0x000F02

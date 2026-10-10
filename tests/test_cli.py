import pytest

from max11410_evkit.cli import main


def test_info_fake(capsys):
    assert main(["--fake", "info"]) == 0
    out = capsys.readouterr().out
    assert "firmware : 1.1" in out
    assert "project  : MAX11410EVK" in out
    assert "echo     : ok" in out
    assert "PART_ID  : 0x000F02" in out


def test_peek_poke_fake(capsys):
    assert main(["--fake", "peek", "11"]) == 0
    assert "0x11 = 0x000F02" in capsys.readouterr().out
    assert main(["--fake", "poke", "0x0B", "2A"]) == 0
    assert "readback 0x2A" in capsys.readouterr().out
    assert main(["--fake", "poke", "3A", "1234", "2"]) == 0
    assert "readback 0x1234" in capsys.readouterr().out


def test_peek_unknown_width_needs_explicit_width():
    with pytest.raises(SystemExit):
        main(["--fake", "peek", "3A"])


def test_list_fake(capsys):
    assert main(["--fake", "list"]) == 0
    assert "fake" in capsys.readouterr().out


def test_value_too_large_is_an_error(capsys):
    assert main(["--fake", "poke", "0B", "1FF"]) == 2
    assert "error" in capsys.readouterr().err


def test_regs_fake(capsys):
    assert main(["--fake", "regs"]) == 0
    out = capsys.readouterr().out
    assert "0x09 CTRL" in out and "REF_SEL=1(REF1)" in out and "0x11 PART_ID" in out
    assert "0x39 STATUS_IE     0x000001   CONV_RDY_IE" in out
    assert "UC0" not in out
    assert main(["--fake", "regs", "--all"]) == 0
    assert "0x6F UCADDR" in capsys.readouterr().out


def test_get_set_fake(capsys):
    assert main(["--fake", "get", "CTRL.REF_SEL"]) == 0
    assert "CTRL.REF_SEL = 1 (REF1)" in capsys.readouterr().out
    assert main(["--fake", "get", "CTRL.REFSEL"]) == 0  # Phase 2 / driver alias
    assert "CTRL.REF_SEL = 1 (REF1)" in capsys.readouterr().out
    assert main(["--fake", "set", "FILTER.LINEF", "SINC4"]) == 0
    assert "LINEF=3(SINC4)" in capsys.readouterr().out
    assert main(["--fake", "set", "PGA", "0x24"]) == 0
    assert "SIG_PATH=2(PGA) GAIN=4(X16)" in capsys.readouterr().out
    assert main(["--fake", "get", "PART_ID"]) == 0
    assert "0x000F02" in capsys.readouterr().out


def test_get_set_errors(capsys):
    assert main(["--fake", "get", "NOPE"]) == 2
    assert main(["--fake", "set", "PART_ID", "1"]) == 2
    assert main(["--fake", "set", "FILTER.LINEF", "BOGUS"]) == 2
    err = capsys.readouterr().err
    assert "no register named 'NOPE'" in err and "read-only" in err and "BOGUS" in err


def test_read_fake(capsys):
    assert main(["--fake", "read", "--ch", "0", "-n", "3"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "AINP=2 AINN=10 REFSEL=1 Vref=2.5 V unipolar" in out[0] and "60 sps" in out[0]
    assert out[1].startswith("0x820000") and len(out) == 4
    assert main(["--fake", "read", "-n", "1", "--method", "poll", "--gain", "8", "--bipolar"]) == 0
    assert "pga x8" in capsys.readouterr().out


def test_selfcal_fake(capsys):
    assert main(["--fake", "selfcal"]) == 0
    out = capsys.readouterr().out
    assert "SELF_GAIN_128" in out and "self-calibration done" in out


def test_regs_notes_pd_measurement(capsys):
    assert main(["--fake", "regs"]) == 0
    out = capsys.readouterr().out
    assert "note: PD datasheet reset 0x02, EVKIT measured 0x01" in out
    assert "AINP_SEL=15(NC) AINN_SEL=15(NC)" in out  # MUX_CTRL0 reset 0xFF


def test_selfcal_fast_fake(capsys):
    assert main(["--fake", "selfcal", "--fast", "--no-pga"]) == 0
    out = capsys.readouterr().out
    assert "rate[15] (fast" in out and "self-calibration done" in out

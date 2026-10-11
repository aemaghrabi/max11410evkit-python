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


def test_capture_fake_with_csv(capsys, tmp_path):
    out_file = tmp_path / "c.csv"
    assert main(["--fake", "capture", "-n", "300", "--block", "100", "-o", str(out_file)]) == 0
    out = capsys.readouterr().out
    assert "captured 300 samples in 3 blocks" in out
    assert "effective res." in out and "noise-free res." in out
    assert out_file.exists()


def test_stats_alias(capsys):
    assert main(["--fake", "stats", "-n", "50"]) == 0
    assert "samples        : 50" in capsys.readouterr().out


def test_hist_fake_prints_bars(capsys):
    assert main(["--fake", "hist", "-n", "20"]) == 0
    out = capsys.readouterr().out
    assert "histogram      : 20 bins" in out and "#" in out


def test_fft_fake(capsys, tmp_path):
    assert (
        main(["--fake", "fft", "-n", "256", "--window", "hann", "-o", str(tmp_path / "f.csv")]) == 0
    )
    out = capsys.readouterr().out
    assert "window         : hann, 256 points" in out and "ENOB" in out


@pytest.mark.parametrize("n", ["100", "16384", "8"])
def test_fft_length_must_be_power_of_two_in_one_block(capsys, n):
    assert main(["--fake", "fft", "-n", n]) == 2
    assert "power of 2" in capsys.readouterr().err


def test_signal_path_option_does_not_replace_hid_path():
    from max11410_evkit.cli import build_parser

    args = build_parser().parse_args(["read", "--path", "pga", "--gain", "8"])
    assert args.path is None and args.sig_path == "pga"
    args = build_parser().parse_args(["--path", "DevSrvsID:1", "capture", "--path", "bypass"])
    assert args.path == "DevSrvsID:1" and args.sig_path == "bypass"


def test_capture_cancelled_before_start_reports_zero_blocks(capsys, monkeypatch):
    import max11410_evkit.acquisition as acq

    real = acq.acquire

    def cancelled_acquire(*args, cancel, **kw):
        cancel.set()  # as if Ctrl-C arrived before the first block
        return real(*args, cancel=cancel, **kw)

    monkeypatch.setattr(acq, "acquire", cancelled_acquire)
    assert main(["--fake", "capture", "-n", "50"]) == 1
    assert "captured 0 samples in 0 blocks" in capsys.readouterr().out


def test_fft_warns_without_test_tone(capsys, monkeypatch):
    from max11410_evkit.transport import fake as fake_mod

    orig = fake_mod.FakeEvKitTransport.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.adc.inputs[2] = 1.0  # constant DC input

    monkeypatch.setattr(fake_mod.FakeEvKitTransport, "__init__", init)
    assert main(["--fake", "fft", "-n", "256"]) == 0
    assert "warning: no test tone" in capsys.readouterr().out

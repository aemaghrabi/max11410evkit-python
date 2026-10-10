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

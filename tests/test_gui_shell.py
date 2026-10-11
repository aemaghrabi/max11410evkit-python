"""Phase 5: GUI shell with the DeviceController (real worker thread, fake EV kits)."""

from __future__ import annotations

import json
import time

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from max11410_evkit.device.max11410 import ChannelConfig  # noqa: E402
from max11410_evkit.gui.adapters import gui_channel, gui_device, gui_rate, input_name  # noqa: E402
from max11410_evkit.gui.app import build_window, main  # noqa: E402
from tests.gui_helpers import FakeUsb, qapp, wait_until  # noqa: E402


@pytest.fixture
def shell():
    qapp()
    made = []

    def _make(usb: FakeUsb, **kw):
        kw.setdefault("pick", lambda parent, entries: None)
        kw.setdefault("confirm_load", lambda parent, path: (True, False))
        win, ctrl = build_window(
            "light", list_boards=usb.list_boards, open_board=usb.open_board, poll_ms=20, **kw
        )
        made.append((win, ctrl))
        return win, ctrl

    yield _make
    for win, ctrl in made:
        ctrl.shutdown()
        win.close()
        win.deleteLater()
    qapp().processEvents()


def status_text(win) -> str:
    return win.conn_label.text()


# ------------------------------------------------------------------ adapters
def test_adapter_defaults():
    g = gui_channel(ChannelConfig(), ctrl=0x01)
    assert (g["ainp"], g["ainn"], g["sig_path"], g["filter"], g["rate"]) == (
        "AIN2", "GND", "Buffered", "SINC4", 59.8,
    )  # fmt: skip
    assert g["refsel"] == "REF1P / REF1N" and g["format"] == "Two's complement"
    assert g["unipolar"] is True and g["enabled"] is True


def test_adapter_codes():
    assert input_name(10, negative=False) == "AVDD" and input_name(10, negative=True) == "GND"
    assert input_name(15, negative=False) == "NC"
    assert gui_rate("FIR5060", 15) == 16.8  # past the end repeats the last entry
    assert gui_device({"PD": 1}) == {"power": "Standby"}
    assert gui_device({"PD": 3}) == {}


# ------------------------------------------------------------------ window + controller
def test_starts_disconnected_and_greyed(shell):
    usb = FakeUsb()
    win, ctrl = shell(usb)
    wait_until(lambda: "Disconnected" in status_text(win))
    assert all(not tab.isEnabled() for _name, tab in win.tab_items())
    assert "Phase 7" in win.tabs.tabToolTip(win.tabs.indexOf(win.scope_tab))
    assert not win.act_save.isEnabled() and not win.act_load.isEnabled()
    assert win.act_connect.isVisible() and not win.act_simulated.isChecked()


def test_connect_shows_board_state(shell):
    usb = FakeUsb("A")
    adc = usb.transports["A"].adc
    adc.regs[0x0B] = 0x45  # AIN4 - AIN5
    adc.regs[0x00] = 0x01  # PD = standby
    win, ctrl = shell(usb)
    wait_until(lambda: ctrl.snapshot is not None)
    assert "Hardware Connected" in status_text(win) and "fw 1.1" in status_text(win)
    ch0 = win.channels.get(0)
    assert (ch0["ainp"], ch0["ainn"], ch0["enabled"]) == ("AIN4", "AIN5", True)
    assert win.channels.enabled() == [0]
    assert win.adc_tab.device_config()["power"] == "Standby"
    assert win.registers_tab.table.value_of(0x0B) == 0x45
    assert win.act_save.isEnabled()
    assert usb.spi_writes("A") == []
    assert all(not tab.isEnabled() for _name, tab in win.tab_items())  # still greyed


def test_unplug_and_replug_updates_status(shell):
    usb = FakeUsb("A")
    win, ctrl = shell(usb)
    wait_until(lambda: "Hardware Connected" in status_text(win))
    usb.unplug("A")
    wait_until(lambda: "Disconnected" in status_text(win))
    assert not win.act_save.isEnabled()
    usb.plug("A")
    wait_until(lambda: "Hardware Connected" in status_text(win))


def test_picker_for_two_boards(shell):
    usb = FakeUsb("A", "B")
    asked = []

    def pick(parent, entries):
        asked.append([e.key for e in entries])
        return "B"

    win, ctrl = shell(usb, pick=pick)
    wait_until(lambda: ctrl.connected_info is not None)
    assert sorted(asked[0]) == ["A", "B"] and ctrl.connected_info["key"] == "B"


def test_simulated_menu(shell):
    usb = FakeUsb()
    win, ctrl = shell(usb)
    win.act_simulated.setChecked(True)
    wait_until(lambda: "Simulated board" in status_text(win))
    win.disconnectRequested.emit()
    wait_until(lambda: "Disconnected" in status_text(win))
    wait_until(lambda: not win.act_simulated.isChecked())


def test_offline_flag_uses_simulated_board(shell):
    usb = FakeUsb("A")
    win, ctrl = shell(usb, offline=True)
    wait_until(lambda: ctrl.snapshot is not None)
    assert "Simulated board" in status_text(win) and win.act_simulated.isChecked()
    assert usb.opened == []


def test_file_save_and_confirmed_load(shell, tmp_path):
    usb = FakeUsb("A")
    asked = []

    def confirm(parent, path):
        asked.append(path)
        return path.endswith("yes.json"), False

    win, ctrl = shell(usb, confirm_load=confirm)
    wait_until(lambda: ctrl.snapshot is not None)
    path = tmp_path / "yes.json"
    win._write_config(str(path))  # File > Save (dialog bypassed)
    wait_until(path.exists)
    assert json.loads(path.read_text())["format"] == "max11410-evkit-config"

    usb.transports["A"].adc.regs[0x08] = 0x34
    nogo = tmp_path / "no.json"
    nogo.write_text(path.read_text())
    n = len(usb.spi_writes("A"))
    win.loadConfigRequested.emit(str(nogo))  # declined
    end = time.monotonic() + 0.3
    while time.monotonic() < end:
        qapp().processEvents()
    assert len(usb.spi_writes("A")) == n

    win.loadConfigRequested.emit(str(path))  # confirmed
    wait_until(lambda: usb.transports["A"].adc.regs[0x08] == 0x00)
    assert asked == [str(nogo), str(path)]


def test_mock_mode_still_works():
    qapp()
    win, backend = build_window("light", mock=True)
    assert all(tab.isEnabled() for _name, tab in win.tab_items())
    assert not win.act_connect.isVisible()
    assert len(win.channels.enabled()) == 4
    win.close()


def test_cli_flags_are_exclusive():
    with pytest.raises(SystemExit):
        main(["--offline", "--mock"])

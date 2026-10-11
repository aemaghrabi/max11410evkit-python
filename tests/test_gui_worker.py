"""Phase 5: DeviceWorker on fake EV kits (driven directly, without its thread)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6")

from max11410_evkit.config import DeviceConfig  # noqa: E402
from max11410_evkit.gui.worker import DeviceWorker  # noqa: E402
from tests.gui_helpers import FakeUsb, qapp  # noqa: E402


class Recorder:
    def __init__(self, w: DeviceWorker) -> None:
        self.connected: list[dict] = []
        self.disconnected: list[str] = []
        self.offers: list[list] = []
        self.snaps: list[dict] = []
        self.logs: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []
        self.loaded: list[dict] = []
        self.sim: list[bool] = []
        w.connected.connect(self.connected.append)
        w.disconnected.connect(self.disconnected.append)
        w.boardsChanged.connect(self.offers.append)
        w.snapshotReady.connect(self.snaps.append)
        w.log.connect(lambda m, lvl: self.logs.append((m, lvl)))
        w.failed.connect(lambda a, m: self.failed.append((a, m)))
        w.configLoaded.connect(lambda p, r: self.loaded.append(r))
        w.simulatedChanged.connect(self.sim.append)


@pytest.fixture
def make():
    qapp()
    made: list[DeviceWorker] = []

    def _make(usb: FakeUsb, **kw) -> tuple[DeviceWorker, Recorder]:
        w = DeviceWorker(list_boards=usb.list_boards, open_board=usb.open_board, **kw)
        made.append(w)
        return w, Recorder(w)

    yield _make
    for w in made:
        w.stop()


def test_no_board_stays_disconnected(make):
    usb = FakeUsb()
    w, rec = make(usb)
    w.tick()
    assert not w.is_connected and rec.connected == [] and rec.offers == []


def test_one_board_connects_and_reads_without_writing(make):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    assert w.is_connected
    info = rec.connected[0]
    assert info["key"] == "A" and info["firmware"] == "1.1" and info["part_id"] == 0x000F02
    assert usb.spi_writes("A") == []  # decision 2: connecting never writes the board
    snap = rec.snaps[-1]
    assert "MUX_CTRL0" in snap["registers"] and "UC52" in snap["registers"]
    assert snap["addresses"]["FILTER"] == 0x08
    ch = snap["channels"]
    assert ch[0].enabled and not any(c.enabled for c in ch[1:])
    # reset value MUX_CTRL0 = 0xFF -> both inputs unconnected (code 15)
    assert (ch[0].ainp, ch[0].ainn) == (15, 15)


def test_channel0_reflects_board_registers(make):
    usb = FakeUsb("A")
    adc = usb.transports["A"].adc
    adc.regs[0x0B] = 0x45  # MUX_CTRL0: AIN4 - AIN5
    adc.regs[0x0E] = 0x24  # PGA: PGA path, x16
    adc.regs[0x08] = 0x32  # FILTER: SINC4, RATE 2
    adc.regs[0x09] = 0x5A  # CTRL: U_BN=1, REFBUFP=1, REFBUFN=1, REF_SEL=2
    w, rec = make(usb)
    w.tick()
    c = rec.snaps[-1]["channels"][0]
    assert (c.ainp, c.ainn, c.path, c.gain, c.linef, c.rate) == (4, 5, "pga", 16, "SINC4", 2)
    assert (c.refsel, c.vrefp_buf, c.vrefn_buf, c.bipolar) == (2, True, True, False)
    assert usb.spi_writes("A") == []


def test_two_boards_offer_picker_once(make):
    usb = FakeUsb("A", "B")
    w, rec = make(usb)
    w.tick()
    w.tick()
    assert not w.is_connected
    assert len(rec.offers) == 1 and {e.key for e in rec.offers[0]} == {"A", "B"}
    w.connect_to("B")
    assert w.is_connected and rec.connected[-1]["key"] == "B"
    usb.plug("C")
    w.tick()
    assert len(rec.offers) == 1  # connected: no more questions


def test_unplug_then_replug_reconnects_same_board(make):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    usb.unplug("A")
    w.tick()
    assert not w.is_connected and rec.disconnected
    usb.plug("B")  # a different single kit must not be picked up automatically
    w.tick()
    assert not w.is_connected
    usb.plug("A")
    w.tick()
    assert w.is_connected and rec.connected[-1]["key"] == "A"


def test_target_gone_and_two_others_asks(make):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    usb.unplug("A")
    usb.plug("B")
    usb.plug("C")
    w.tick()
    assert not w.is_connected
    assert {e.key for e in rec.offers[-1]} == {"B", "C"}


def test_command_failure_on_unplug_reports_disconnect(make):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    usb.transports["A"].disconnect()  # handle dies before the USB list notices
    w.read_all()
    assert not w.is_connected
    assert rec.disconnected and rec.failed and rec.failed[-1][0] == "Read All"


def test_disconnect_stops_auto_reconnect_until_connect(make):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    w.disconnect_board()
    w.tick()
    assert not w.is_connected
    w.connect_to("A")
    assert w.is_connected


def test_list_now_always_offers(make):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    w.list_now()
    assert [e.key for e in rec.offers[-1]] == ["A"]


def test_simulated_mode(make):
    usb = FakeUsb("A")
    w, rec = make(usb, simulated=True)
    w.start()
    assert w.is_connected and rec.connected[-1]["simulated"]
    assert usb.opened == []  # the USB kit is not touched in simulated mode
    w.set_simulated(False)
    assert rec.sim == [False]
    assert w.is_connected and rec.connected[-1]["key"] == "A"


def test_save_and_load_round_trip(make, tmp_path):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    adc = usb.transports["A"].adc
    path = tmp_path / "cfg.json"
    w.save_config(str(path))
    data = json.loads(path.read_text())
    assert data["format"] == "max11410-evkit-config" and len(data["registers"]) == 97
    assert data["registers"]["FILTER"] == "0x00"
    adc.regs[0x08] = 0x34  # change FILTER on the board
    w.read_all()
    assert rec.snaps[-1]["registers"]["FILTER"] == 0x34
    w.load_config(str(path), False)
    assert rec.loaded[-1]["mismatches"] == [] and rec.loaded[-1]["skipped_cal"] == 13
    assert adc.regs[0x08] == 0x00
    assert usb.spi_writes("A")[-1] == 0x00  # PD written last
    assert rec.snaps[-1]["registers"]["FILTER"] == 0x00  # Read All after the load


def test_load_keeps_file_channels(make, tmp_path):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    cfg = DeviceConfig.load
    path = tmp_path / "cfg.json"
    w.save_config(str(path))
    data = json.loads(path.read_text())
    data["channels"]["3"]["enabled"] = True
    data["channels"]["3"]["name"] = "Bridge"
    data["channels"]["0"]["name"] = "Input"
    path.write_text(json.dumps(data))
    assert cfg(path)  # still a valid file
    w.load_config(str(path), False)
    ch = rec.snaps[-1]["channels"]
    assert ch[3].enabled and ch[3].name == "Bridge" and ch[0].name == "Input"


def test_load_bad_file_reports_and_writes_nothing(make, tmp_path):
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    path = tmp_path / "bad.json"
    path.write_text('{"format": "something-else"}')
    w.load_config(str(path), False)
    assert rec.failed[-1][0] == "Load ADC config"
    assert usb.spi_writes("A") == []
    assert w.is_connected


def test_actions_without_board_fail_cleanly(make, tmp_path):
    w, rec = make(FakeUsb())
    w.save_config(str(tmp_path / "x.json"))
    assert rec.failed[-1] == ("Save ADC config", "no EV kit connected")


def test_open_failure_logged_once(make):
    usb = FakeUsb("A")
    usb.transports["A"].adc.regs[0x11] = 0xFFFFFF  # ADC side unpowered
    w, rec = make(usb)
    w.tick()
    w.tick()
    errors = [m for m, lvl in rec.logs if lvl == "error"]
    assert len(errors) == 1 and "PART_ID" in errors[0]
    assert not w.is_connected


def test_reference_voltage_comes_from_loaded_config_only(make, tmp_path):
    """Decision 2026-10-12 (a): vref lives in the config only; 2.5 V until a file is loaded."""
    usb = FakeUsb("A")
    w, rec = make(usb)
    w.tick()
    assert [c.vref_volts for c in rec.snaps[-1]["channels"]] == [2.5] * 8
    path = tmp_path / "cfg.json"
    w.save_config(str(path))
    data = json.loads(path.read_text())
    data["channels"]["0"]["vref_volts"] = 3.3
    data["channels"]["5"]["vref_volts"] = 1.25
    path.write_text(json.dumps(data))
    w.load_config(str(path), False)
    ch = rec.snaps[-1]["channels"]
    assert (ch[0].vref_volts, ch[5].vref_volts) == (3.3, 1.25)
    w.read_all()  # a later Read All keeps the loaded values
    assert rec.snaps[-1]["channels"][0].vref_volts == 3.3
    usb.unplug("A")
    w.tick()
    usb.plug("A")
    w.tick()  # a fresh connect has no config: back to the 2.5 V default
    assert rec.snaps[-1]["channels"][0].vref_volts == 2.5

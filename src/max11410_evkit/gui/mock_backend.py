"""Mock backend: makes the GUI mockup fully clickable without hardware.

It connects to every tab's intent signals, fabricates plausible per-channel results
(sine + noise captures, DC readings, random register values, ...) and writes a line to
the Status Log for each action. In Phase 5–7 this module is replaced by a controller
that talks to ``max11410_evkit.board``/``device`` — the tab APIs stay the same.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from . import placeholder_regmap as rm
from .channels import filter_text, input_text, lsb_volts, range_text

CAL_NAMES = {
    "self": "Self",
    "pga": "PGA",
    "sys_offset": "System offset",
    "sys_gain": "System gain",
}

# Example channel set loaded by the mock at start-up (a typical mixed-sensor board).
DEMO_CHANNELS = {
    0: {"name": "AIN2 single-ended", "enabled": True},
    1: {
        "name": "Load-cell bridge", "enabled": True, "ainp": "AIN0", "ainn": "AIN1",
        "sig_path": "PGA", "pga_gain": 128, "refsel": "REF1P / REF1N", "vref": 3.3,
        "vrefp_buf": True, "vrefn_buf": True, "rate": 20.0, "settling_ms": 5.0,
    },
    2: {
        "name": "PT100 RTD", "enabled": True, "ainp": "AIN4", "ainn": "AIN5",
        "sig_path": "PGA", "pga_gain": 16, "refsel": "REF2P / REF2N", "vref": 1.6,
        "unipolar": True, "filter": "FIR 50/60 Hz", "rate": 4.4, "settling_ms": 20.0,
    },
    3: {
        "name": "Thermocouple K", "enabled": True, "ainp": "AIN6", "ainn": "AIN7",
        "sig_path": "PGA", "pga_gain": 32, "rate": 59.8, "settling_ms": 2.0,
    },
}  # fmt: skip
DEMO_DEVICE = {"idac_current_ua": 250, "idac0_pin": "AIN3", "idac1_pin": "AIN3"}

# Fake signal per channel: dc level (V), sine amplitude (fraction of full scale),
# noise rms (V), scope cycles.
SIGNALS = {
    0: (1.234567, 0.36, 0.9e-6, 7),
    1: (4.8213e-3, 0.40, 45e-9, 5),
    2: (21.94e-3, 0.30, 110e-9, 3),
    3: (1.0213e-3, 0.45, 80e-9, 9),
}


class MockBackend(QObject):
    log = Signal(str, str)  # message, level

    def __init__(self, window, seed: int = 11410) -> None:
        super().__init__(window)
        self.w = window
        self.store = window.channels
        self.rng = np.random.default_rng(seed)
        self.device = dict(rm.DEVICE_DEFAULTS)
        self.regs: dict[int, int] = {r.addr: 0 for r in rm.REGISTERS}
        self.ucode: dict[int, int] = {r.addr: 0 for r in rm.MICROCODE}
        self._dmm_timer = QTimer(self)
        self._dmm_timer.setInterval(150)
        self._dmm_timer.timeout.connect(lambda: self._dmm_reading(self.w.dmm_tab.selected()))
        self._wire()
        # "Read All" at connect: load the demo channel set + device settings
        self.device.update(DEMO_DEVICE)
        self.w.adc_tab.set_device_config(self.device)
        self.w.adc_tab.set_channel_configs(DEMO_CHANNELS)
        self.w.adc_tab.set_active_channel(0)

    # ---------------------------------------------------------------- helpers
    def _signal(self, ch: int) -> tuple[float, float, float, int]:
        return SIGNALS.get(ch, (0.5e-3 * (ch + 1), 0.25, 0.5e-6, 4))

    def _codes(self, ch: int, volts: np.ndarray) -> np.ndarray:
        return np.round(volts / lsb_volts(self.store.get(ch))).astype(np.int64)

    def _fs(self, ch: int) -> float:
        cfg = self.store.get(ch)
        g = cfg["pga_gain"] if cfg["sig_path"] == "PGA" else 1
        return cfg["vref"] / g

    def _sine(self, ch: int, n: int, cycles: int | None = None) -> np.ndarray:
        rate = self.store.get(ch)["rate"]
        dc, amp_frac, noise, scope_cycles = self._signal(ch)
        k = cycles or max(3, int(n * 0.0479) | 1)  # coherent odd bin -> clean FFT
        f0 = rate * k / n
        t = np.arange(n) / rate
        a = amp_frac * self._fs(ch)
        x = a * np.sin(2 * np.pi * f0 * t)
        x += a * 10 ** (-105 / 20) * np.sin(2 * np.pi * 2 * f0 * t + 0.3)
        x += a * 10 ** (-112 / 20) * np.sin(2 * np.pi * 3 * f0 * t + 1.1)
        x += self.rng.normal(0, noise * 1.3, n)
        return x

    def _dc(self, ch: int, n: int) -> np.ndarray:
        dc, _amp, noise, _c = self._signal(ch)
        return dc + self.rng.normal(0, noise, n)

    def _tag(self, ch: int) -> str:
        return f"Ch {ch} ({self.store.get(ch)['name'] or f'Channel {ch}'})"

    # ---------------------------------------------------------------- wiring
    def _wire(self) -> None:
        w = self.w
        a = w.adc_tab
        a.settingChanged.connect(self._on_setting)
        a.channelConfigChanged.connect(self._on_channel_cfg)
        a.channelEnabledChanged.connect(
            lambda ch, on: self.log.emit(
                f"{self._tag(ch)} {'enabled' if on else 'disabled'}", "info"
            )
        )
        a.readAllRequested.connect(self._adc_read_all)
        a.convertRequested.connect(self._convert)
        a.convertAllRequested.connect(self._convert_all)
        a.calibrationRequested.connect(
            lambda k, ch: self.log.emit(
                f"{CAL_NAMES[k]} calibration with {self._tag(ch)} config complete (CAL_READY=1)",
                "ok",
            )
        )

        o = w.other_tab
        o.readAllRequested.connect(self._other_read_all)
        o.thresholdChanged.connect(
            lambda ch, which, v, u: self.log.emit(
                f"{self._tag(ch)} {which} threshold = {v:g} {u}", "info"
            )
        )
        o.thresholdInterruptChanged.connect(
            lambda ch, which, on: self.log.emit(
                f"{self._tag(ch)} {which.upper()} interrupt {'enabled' if on else 'disabled'}",
                "info",
            )
        )
        o.thresholdUnitChanged.connect(lambda u: self.log.emit(f"Threshold units: {u}", "info"))
        o.waitCalcRequested.connect(self._wait_calc)
        o.waitStartRequested.connect(
            lambda wv, we: self.log.emit(f"Wait timer started (WAIT={wv}, WAIT_EXT={we})", "ok")
        )
        o.clockSourceChanged.connect(lambda s: self.log.emit(f"Clock source: {s}", "info"))
        o.gpioModeChanged.connect(lambda g, m: self.log.emit(f"GPIO{g} mode: {m}", "info"))

        s = w.sequence_tab
        s.readAllRequested.connect(self._seq_read_all)
        s.microcodeWriteRequested.connect(
            lambda ad, v: self.log.emit(f"Write µC @0x{ad:02X} = 0x{v:04X}", "ok")
        )
        s.sequenceStartRequested.connect(
            lambda ad: self.log.emit(f"Sequencer started at 0x{ad:02X}", "ok")
        )
        s.currentAddressRequested.connect(self._seq_cur)

        r = w.registers_tab
        r.readAllRequested.connect(self._reg_read_all)
        r.registerReadRequested.connect(self._reg_read)
        r.registerWriteRequested.connect(self._reg_write)

        w.scope_tab.captureRequested.connect(self._scope_capture)
        w.histogram_tab.captureRequested.connect(self._hist_capture)
        w.fft_tab.captureRequested.connect(self._fft_capture)
        d = w.dmm_tab
        d.measureRequested.connect(self._dmm_reading)
        d.continuousToggled.connect(self._dmm_continuous)

    # ---------------------------------------------------------------- ADC config
    def _on_setting(self, key: str, value) -> None:
        self.device[key] = value
        self.log.emit(f"Device: {key} = {value}", "info")

    def _on_channel_cfg(self, ch: int, cfg: dict) -> None:
        self.log.emit(
            f"{self._tag(ch)}: {input_text(cfg)}, {range_text(cfg)}, {filter_text(cfg)}", "info"
        )

    def _adc_read_all(self) -> None:
        self.w.adc_tab.set_device_config(self.device)
        self.log.emit("ADC Config: device registers read — global controls refreshed", "ok")

    def _convert(self, ch: int) -> None:
        v = float(self._dc(ch, 1)[0])
        signed = int(self._codes(ch, np.array([v]))[0])
        code = signed & 0xFFFFFF
        lsb = lsb_volts(self.store.get(ch))
        self.w.adc_tab.show_read_data(ch, 0x000001, code, v, code=signed, lsb_volts=lsb)
        self.log.emit(f"Convert {self._tag(ch)}: DATA{ch} = 0x{code:06X} ({v:+.7f} V)", "ok")

    def _convert_all(self) -> None:
        for ch in self.store.enabled():
            self._convert(ch)

    def _reg_write(self, addr: int, value: int) -> None:
        self.regs[addr] = value
        self.log.emit(f"Write {rm.register_by_addr(addr).name} (0x{addr:02X}) = 0x{value:X}", "ok")

    # ---------------------------------------------------------------- other
    def _other_read_all(self) -> None:
        rows = [(-(i + 1) * 100000, (i + 1) * 100000) for i in range(8)]
        self.w.other_tab.set_thresholds(rows, "LSB")
        self.w.other_tab.set_threshold_interrupts([(i % 3 == 0, i % 2 == 0) for i in range(8)])
        self.log.emit("Other: thresholds, wait, clock and GPIO registers read", "ok")

    def _wait_calc(self, wait: int, ext: int) -> None:
        cycles = (wait + 1) * (ext + 1) * 256  # PLACEHOLDER formula
        t = cycles / rm.CLOCK_HZ
        self.w.other_tab.show_wait_time(t)
        self.log.emit(
            f"Wait time = {t * 1e3:.4f} ms (WAIT={wait}, WAIT_EXT={ext}, 2.4576 MHz)", "info"
        )

    # ---------------------------------------------------------------- sequence / registers
    def _seq_read_all(self) -> None:
        self.ucode = {a: int(self.rng.integers(0, 1 << 16)) for a in self.ucode}
        self.w.sequence_tab.set_microcode_values(self.ucode)
        self.log.emit(f"Sequence: read {len(self.ucode)} microcode registers", "ok")

    def _seq_cur(self) -> None:
        a = int(self.rng.integers(rm.MICROCODE[0].addr, rm.MICROCODE[-1].addr + 1))
        self.w.sequence_tab.show_current_address(a)
        self.log.emit(f"Sequencer current address = 0x{a:02X}", "info")

    def _reg_read_all(self) -> None:
        for reg in rm.REGISTERS:
            self.regs[reg.addr] = int(self.rng.integers(0, 1 << reg.width))
        self.regs[0x08] = (3 << 4) | 4  # FILTER: SINC4, RATE=4 (59.8 sps)
        self.regs[0x0B] = (2 << 4) | 11  # MUX_CTRL0: AINP=AIN2, AINN=GND
        self.regs[0x0E] = 0x00
        self.regs[0x38] = 0x000005
        self.w.registers_tab.set_register_values(self.regs)
        self.log.emit(f"Registers: read {len(self.regs)} registers", "ok")

    def _reg_read(self, addr: int) -> None:
        reg = rm.register_by_addr(addr)
        self.w.registers_tab.set_register_value(addr, self.regs.get(addr, 0))
        self.log.emit(f"Read {reg.name} (0x{addr:02X}) = 0x{self.regs.get(addr, 0):X}", "ok")

    # ---------------------------------------------------------------- captures
    def _scope_capture(self, chans: list, n: int) -> None:
        data, rates, lsbs, stats = {}, {}, {}, {}
        for ch in chans:
            cfg = self.store.get(ch)
            rate = cfg["rate"]
            x = self._sine(ch, n, cycles=self._signal(ch)[3])
            spec = np.abs(np.fft.rfft((x - x.mean()) * np.hanning(n)))
            f0 = np.argmax(spec[1:]) + 1
            data[ch], rates[ch], lsbs[ch] = x, rate, lsb_volts(cfg)
            stats[ch] = dict(
                average=x.mean(),
                std_dev=x.std(ddof=1),
                max=x.max(),
                min=x.min(),
                p2p=x.max() - x.min(),
                fundamental_hz=f0 * rate / n,
            )
        self.w.scope_tab.show_capture(data, rates, lsbs, stats)
        names = ", ".join(str(c) for c in chans)
        self.log.emit(f"Scope: captured {n} samples on channel(s) {names}", "ok")

    def _hist_capture(self, ch: int, n: int) -> None:
        x = self._dc(ch, n)
        codes = self._codes(ch, x)
        cfg = self.store.get(ch)
        fsr = self._fs(ch) * (1 if cfg["unipolar"] else 2)
        rms, p2p = x.std(ddof=1), x.max() - x.min()
        stats = dict(
            average=x.mean(),
            std_dev=rms,
            max=x.max(),
            min=x.min(),
            p2p_noise=p2p,
            effective_resolution=np.log2(fsr / rms),
            noise_free_resolution=np.log2(fsr / p2p),
        )
        self.w.histogram_tab.show_capture(ch, codes, stats)
        self.log.emit(f"Histogram: captured {n} samples on {self._tag(ch)}", "ok")

    def _fft_capture(self, ch: int, n: int) -> None:
        rate = self.store.get(ch)["rate"]
        x = self._sine(ch, n)
        win = np.blackman(n)
        spec = np.abs(np.fft.rfft((x - x.mean()) * win)) / (win.sum() / 2)
        db = 20 * np.log10(np.maximum(spec / self._fs(ch), 1e-12))
        freqs = np.fft.rfftfreq(n, 1 / rate)
        k = int(np.argmax(db[3:]) + 3)
        pw = spec**2
        sig = pw[k - 3 : k + 4].sum()
        harm = sum(pw[max(h * k - 3, 0) : h * k + 4].sum() for h in range(2, 6) if h * k < len(pw))
        mask = np.ones_like(pw, bool)
        mask[:4] = False
        for h in range(1, 6):
            if h * k < len(pw):
                mask[max(h * k - 3, 0) : h * k + 4] = False
        noise = pw[mask].sum()
        sinad = 10 * np.log10(sig / (noise + harm))
        metrics = dict(
            fundamental_hz=freqs[k],
            fundamental_dbfs=db[k],
            thd_db=10 * np.log10(harm / sig),
            snr_db=10 * np.log10(sig / noise),
            sinad_db=sinad,
            sfdr_db=db[k] - np.max(np.where(np.abs(np.arange(len(db)) - k) > 3, db, -400)[4:]),
            enob=(sinad - 1.76) / 6.02,
            noise_floor_dbfs=float(np.median(db[mask])),
        )
        self.w.fft_tab.show_spectrum(ch, freqs, db, metrics)
        self.log.emit(f"FFT: captured {n} samples on {self._tag(ch)}", "ok")

    # ---------------------------------------------------------------- DMM
    def _dmm_reading(self, chans: list) -> None:
        dmm = self.w.dmm_tab
        for ch in chans:
            dc, _a, noise, _c = self._signal(ch)
            k = dmm._count.get(ch, 0)
            v = float(self._dc(ch, 1)[0] + 3 * noise * np.sin(k / 12 + ch))
            dmm.show_reading(ch, v, int(self._codes(ch, np.array([v]))[0]))

    def _dmm_continuous(self, on: bool) -> None:
        (self._dmm_timer.start if on else self._dmm_timer.stop)()
        self.log.emit(f"DMM continuous {'started' if on else 'stopped'}", "info")

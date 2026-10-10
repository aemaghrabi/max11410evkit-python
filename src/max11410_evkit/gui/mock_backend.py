"""Mock backend: makes the GUI mockup fully clickable without hardware.

It connects to every tab's intent signals, fabricates plausible results
(sine + noise captures, random register values, ...) and writes a line to the
Status Log for each action. In Phase 5–7 this module is replaced by a
controller that talks to ``max11410_evkit.board`` — the tab APIs stay the same.
"""

from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from . import placeholder_regmap as rm

FULL_SCALE_CODES = 1 << 24
CAL_NAMES = {
    "self": "Self",
    "pga": "PGA",
    "sys_offset": "System offset",
    "sys_gain": "System gain",
}


class MockBackend(QObject):
    log = Signal(str, str)  # message, level

    def __init__(self, window, seed: int = 11410) -> None:
        super().__init__(window)
        self.w = window
        self.rng = np.random.default_rng(seed)
        self.cfg = dict(rm.STARTUP_DEFAULTS)
        self.regs: dict[int, int] = {r.addr: 0 for r in rm.REGISTERS}
        self.ucode: dict[int, int] = {r.addr: 0 for r in rm.MICROCODE}
        self.dc_level = 1.234567
        self._dmm_timer = QTimer(self)
        self._dmm_timer.setInterval(150)
        self._dmm_timer.timeout.connect(self._dmm_reading)
        self._wire()

    # ---------------------------------------------------------------- helpers
    @property
    def vref(self) -> float:
        return float(self.cfg.get("vref", 2.5))

    @property
    def lsb(self) -> float:
        gain = self.cfg.get("pga_gain", 1) if self.cfg.get("sig_path") == "PGA" else 1
        span = self.vref / gain if self.cfg.get("unipolar") else 2 * self.vref / gain
        return span / FULL_SCALE_CODES

    def _codes(self, volts: np.ndarray) -> np.ndarray:
        return np.round(volts / self.lsb).astype(np.int64)

    def _sine(self, rate: float, n: int, cycles: int | None = None) -> np.ndarray:
        k = cycles or max(3, int(n * 0.0479) | 1)  # coherent odd bin -> clean FFT
        f0 = rate * k / n
        t = np.arange(n) / rate
        x = 0.9 * np.sin(2 * np.pi * f0 * t)
        x += 0.9 * 10 ** (-105 / 20) * np.sin(2 * np.pi * 2 * f0 * t + 0.3)
        x += 0.9 * 10 ** (-112 / 20) * np.sin(2 * np.pi * 3 * f0 * t + 1.1)
        x += self.rng.normal(0, 1.2e-6, n)
        return x

    def _dc(self, n: int) -> np.ndarray:
        return self.dc_level + self.rng.normal(0, 0.9e-6, n)

    # ---------------------------------------------------------------- wiring
    def _wire(self) -> None:
        w = self.w
        a = w.adc_tab
        a.settingChanged.connect(self._on_setting)
        a.referenceVoltageChanged.connect(
            lambda v: (
                self.cfg.__setitem__("vref", v),
                self.log.emit(f"Reference voltage set to {v:.4f} V", "info"),
            )
        )
        a.readAllRequested.connect(self._adc_read_all)
        a.convertRequested.connect(self._convert)
        a.calibrationRequested.connect(
            lambda k: self.log.emit(f"{CAL_NAMES[k]} calibration complete (CAL_READY=1)", "ok")
        )

        o = w.other_tab
        o.readAllRequested.connect(self._other_read_all)
        o.thresholdChanged.connect(
            lambda ch, which, v, u: self.log.emit(
                f"Channel {ch} {which} threshold = {v:g} {u}", "info"
            )
        )
        o.thresholdInterruptChanged.connect(
            lambda ch, which, on: self.log.emit(
                f"Channel {ch} {which.upper()} interrupt {'enabled' if on else 'disabled'}", "info"
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
        r.registerWriteRequested.connect(
            lambda ad, v: (
                self.regs.__setitem__(ad, v),
                self.log.emit(f"Write {rm.register_by_addr(ad).name} (0x{ad:02X}) = 0x{v:X}", "ok"),
            )
        )

        w.scope_tab.captureRequested.connect(self._scope_capture)
        w.histogram_tab.captureRequested.connect(self._hist_capture)
        w.fft_tab.captureRequested.connect(self._fft_capture)
        d = w.dmm_tab
        d.measureRequested.connect(self._dmm_reading)
        d.continuousToggled.connect(self._dmm_continuous)

    # ---------------------------------------------------------------- ADC config
    def _on_setting(self, key: str, value) -> None:
        self.cfg[key] = value
        self.log.emit(f"Set {key} = {value}", "info")
        if key in ("ainp", "ainn"):
            self.w.dmm_tab.set_input_label(f"{self.cfg['ainp']} – {self.cfg['ainn']}")

    def _adc_read_all(self) -> None:
        self.w.adc_tab.set_config(self.cfg)
        self.log.emit("ADC Config: read all registers — controls refreshed", "ok")

    def _convert(self) -> None:
        v = float(self._dc(1)[0])
        signed = int(self._codes(np.array([v]))[0])
        code = signed & 0xFFFFFF
        status = 0x000001
        self.w.adc_tab.show_read_data(status, code, v, code=signed, lsb_volts=self.lsb)
        ch = self.cfg.get("channel", 0)
        self.log.emit(f"Convert: DATA{ch} = 0x{code:06X} ({v:+.7f} V)", "ok")

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
    def _scope_capture(self, rate: float, n: int) -> None:
        x = self._sine(rate, n, cycles=7)
        spec = np.abs(np.fft.rfft(x * np.hanning(n)))
        f0 = np.argmax(spec[1:]) + 1
        stats = dict(
            average=x.mean(),
            std_dev=x.std(ddof=1),
            max=x.max(),
            min=x.min(),
            p2p=x.max() - x.min(),
            fundamental_hz=f0 * rate / n,
        )
        self.w.scope_tab.show_capture(x, rate, self.lsb, stats)
        self.log.emit(f"Scope: captured {n} samples @ {rate:g} sps", "ok")

    def _hist_capture(self, rate: float, n: int) -> None:
        x = self._dc(n)
        codes = self._codes(x)
        fsr = 2 * self.vref
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
        self.w.histogram_tab.show_capture(codes, stats)
        self.log.emit(f"Histogram: captured {n} samples @ {rate:g} sps", "ok")

    def _fft_capture(self, rate: float, n: int) -> None:
        x = self._sine(rate, n)
        win = np.blackman(n)
        spec = np.abs(np.fft.rfft((x - x.mean()) * win)) / (win.sum() / 2)
        fs_amp = 2 * self.vref / 2  # full-scale sine amplitude
        db = 20 * np.log10(np.maximum(spec / fs_amp, 1e-12))
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
        self.w.fft_tab.show_spectrum(freqs, db, metrics)
        self.log.emit(f"FFT: captured {n} samples @ {rate:g} sps", "ok")

    # ---------------------------------------------------------------- DMM
    def _dmm_reading(self) -> None:
        v = float(self._dc(1)[0] + 3e-6 * np.sin(len(self.w.dmm_tab._all) / 12))
        self.w.dmm_tab.show_reading(v, int(self._codes(np.array([v]))[0]))

    def _dmm_continuous(self, on: bool) -> None:
        (self._dmm_timer.start if on else self._dmm_timer.stop)()
        self.log.emit(f"DMM continuous {'started' if on else 'stopped'}", "info")

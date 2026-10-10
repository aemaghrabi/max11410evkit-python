"""ADC Config tab — controls grouped along the MAX11410 block diagram.

Signal flow left -> right:  Input (MUX, excitation) -> Analog front end (signal
path / PGA, reference) -> Modulator & filter (filter, conversion, data format) ->
Control (calibration, power, serial interface).  "Read Data and Status" spans
the bottom.

Setting keys emitted by ``settingChanged`` (value type in brackets):
    ainp, ainn [str]        sig_path [str]   pga_gain [int]
    unipolar [bool]         format [str]     filter [str]   rate [float]
    conv_mode [str]         channel [int]    refsel [str]
    vrefp_buf, vrefn_buf [bool]              power [str]
    idac_current_ua [int|None]  idac0_pin, idac1_pin [str]
    vbias_mode [str]        vbias_pins [list[str]]          burnout [str]
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import placeholder_regmap as rm
from ..theme import mono_font
from ..widgets import Led, compact_form, fmt_hex, fmt_si, hex_output, muted, primary_button


class AdcConfigTab(QWidget):
    # ---- user intents
    settingChanged = Signal(str, object)
    readAllRequested = Signal()
    convertRequested = Signal()
    calibrationRequested = Signal(str)  # "self" | "pga" | "sys_offset" | "sys_gain"
    referenceVoltageChanged = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False

        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(12, 8, 12, 12)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        cols = [
            ("① Input", [self._build_mux(), self._build_excitation()]),
            ("② Analog front end", [self._build_signal_path(), self._build_reference()]),
            (
                "③ Modulator & filter",
                [self._build_filter(), self._build_conversion(), self._build_format()],
            ),
            ("④ Control", [self._build_calibration(), self._build_power(), self._build_serial()]),
        ]
        for c, (caption, boxes) in enumerate(cols):
            col = QVBoxLayout()
            col.setSpacing(8)
            cap = muted(caption.upper())
            cap.setStyleSheet("font-size: 10px; letter-spacing: 1px; font-weight: 600;")
            col.addWidget(cap)
            for b in boxes:
                col.addWidget(b)
            col.addStretch(1)
            grid.addLayout(col, 0, c)
            grid.setColumnStretch(c, 1)
        grid.addWidget(self._build_readout(), 1, 0, 1, 4)
        grid.setRowStretch(0, 1)
        grid.setRowStretch(1, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.set_config(rm.STARTUP_DEFAULTS)

    # ================================================================ builders
    def _combo(self, key: str, items, tooltip: str = "", data=None) -> QComboBox:
        cb = QComboBox()
        for i, it in enumerate(items):
            cb.addItem(str(it), data[i] if data else it)
        if tooltip:
            cb.setToolTip(tooltip)
        cb.currentIndexChanged.connect(lambda _i, k=key, w=cb: self._emit(k, w.currentData()))
        return cb

    def _radio_row(
        self, key: str | None, labels: list[str], tooltip: str = ""
    ) -> tuple[QWidget, QButtonGroup]:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        grp = QButtonGroup(w)
        for i, text in enumerate(labels):
            rb = QRadioButton(text)
            if tooltip:
                rb.setToolTip(tooltip)
            grp.addButton(rb, i)
            lay.addWidget(rb)
        lay.addStretch(1)
        if key:
            grp.idToggled.connect(lambda i, on, k=key, L=labels: on and self._emit(k, L[i]))
        return w, grp

    def _build_mux(self) -> QGroupBox:
        g = QGroupBox("Input Multiplexer")
        f = compact_form()
        self.ainp = self._combo("ainp", rm.MUX_INPUTS, "Positive ADC input (MUX_CTRL0.AINP).")
        self.ainn = self._combo("ainn", rm.MUX_INPUTS, "Negative ADC input (MUX_CTRL0.AINN).")
        f.addRow("AINP", self.ainp)
        f.addRow("AINN", self.ainn)
        g.setLayout(f)
        return g

    def _build_excitation(self) -> QGroupBox:
        g = QGroupBox("Excitation (IDAC / VBIAS)")
        f = compact_form()
        self.idac_current = self._combo(
            "idac_current_ua",
            ["Off"] + [f"{u} µA" for u in rm.IDAC_CURRENTS_UA],
            "Matched excitation current sources for RTDs / bridges (SOURCE register).",
            data=[None] + rm.IDAC_CURRENTS_UA,
        )
        self.idac0_pin = self._combo(
            "idac0_pin", rm.MUX_INPUTS[:10], "Pin driven by IDAC0 (MUX_CTRL1)."
        )
        self.idac1_pin = self._combo(
            "idac1_pin", rm.MUX_INPUTS[:10], "Pin driven by IDAC1 (MUX_CTRL1)."
        )
        self.vbias_mode = self._combo(
            "vbias_mode", rm.VBIAS_MODES, "Common-mode bias voltage source (MUX_CTRL2)."
        )
        pins = QWidget()
        pg = QGridLayout(pins)
        pg.setContentsMargins(0, 0, 0, 0)
        pg.setHorizontalSpacing(4)
        pg.setVerticalSpacing(2)
        self.vbias_pins: list[QCheckBox] = []
        for i in range(10):
            cb = QCheckBox(str(i))
            cb.setToolTip(f"Apply VBIAS to AIN{i}")
            cb.toggled.connect(lambda _on: self._emit("vbias_pins", self._vbias_pin_list()))
            pg.addWidget(cb, i // 5, i % 5)
            self.vbias_pins.append(cb)
        self.burnout = self._combo(
            "burnout", rm.BURNOUT_OPTIONS, "Burn-out current sources for open-sensor detection."
        )
        f.addRow("IDAC current", self.idac_current)
        f.addRow("IDAC0 → pin", self.idac0_pin)
        f.addRow("IDAC1 → pin", self.idac1_pin)
        f.addRow("VBIAS", self.vbias_mode)
        f.addRow("VBIAS on AIN", pins)
        f.addRow("Burn-out", self.burnout)
        g.setLayout(f)
        return g

    def _vbias_pin_list(self) -> list[str]:
        return [f"AIN{i}" for i, cb in enumerate(self.vbias_pins) if cb.isChecked()]

    def _build_signal_path(self) -> QGroupBox:
        g = QGroupBox("Signal Path")
        f = compact_form()
        w, self.sig_path = self._radio_row(
            "sig_path",
            rm.SIG_PATHS,
            "Buffered: unity-gain input buffers. Bypass: direct to modulator. "
            "PGA: programmable gain amplifier (PGA.SIG_PATH).",
        )
        self.pga_gain = self._combo(
            "pga_gain",
            [f"×{x}" for x in rm.PGA_GAINS],
            "PGA gain (PGA.GAIN). Only used on the PGA path.",
            data=rm.PGA_GAINS,
        )
        self.sig_path.idToggled.connect(
            lambda _i, _on: self.pga_gain.setEnabled(self.sig_path.checkedId() == 2)
        )
        f.addRow("Path", w)
        f.addRow("PGA gain", self.pga_gain)
        g.setLayout(f)
        return g

    def _build_reference(self) -> QGroupBox:
        g = QGroupBox("Reference")
        f = compact_form()
        self.refsel = self._combo(
            "refsel", rm.REFSEL_OPTIONS, "Reference input pair (CTRL.REFSEL)."
        )
        bufs = QWidget()
        bl = QHBoxLayout(bufs)
        bl.setContentsMargins(0, 0, 0, 0)
        self.vrefp_buf = QCheckBox("VREFP buffer")
        self.vrefn_buf = QCheckBox("VREFN buffer")
        self.vrefp_buf.setToolTip("Enable the positive reference input buffer (CTRL.VREFP_BUF).")
        self.vrefn_buf.setToolTip("Enable the negative reference input buffer (CTRL.VREFN_BUF).")
        self.vrefp_buf.toggled.connect(lambda on: self._emit("vrefp_buf", on))
        self.vrefn_buf.toggled.connect(lambda on: self._emit("vrefn_buf", on))
        bl.addWidget(self.vrefp_buf)
        bl.addWidget(self.vrefn_buf)
        bl.addStretch(1)
        self.vref = QDoubleSpinBox()
        self.vref.setDecimals(4)
        self.vref.setRange(0.5, 5.5)
        self.vref.setSingleStep(0.001)
        self.vref.setSuffix(" V")
        self.vref.setToolTip("Reference voltage used by the software to convert codes to volts.")
        self.vref.valueChanged.connect(
            lambda v: (not self._loading) and self.referenceVoltageChanged.emit(v)
        )
        f.addRow("REFSEL", self.refsel)
        f.addRow("", bufs)
        f.addRow("Ref. voltage", self.vref)
        g.setLayout(f)
        return g

    def _build_filter(self) -> QGroupBox:
        g = QGroupBox("Digital Filter")
        f = compact_form()
        self.filter = self._combo("filter", rm.FILTERS, "Digital filter type (FILTER.LINEF).")
        self.rate = QComboBox()
        self.rate.setToolTip(
            "Output data rate (FILTER.RATE). Options depend on filter and conversion mode."
        )
        self.rate.currentIndexChanged.connect(
            lambda _i: self._emit("rate", self.rate.currentData())
        )
        self.filter.currentIndexChanged.connect(lambda _i: self._refresh_rates())
        self.filter_summary = muted("")
        f.addRow("Filter", self.filter)
        f.addRow("Data rate", self.rate)
        f.addRow("", self.filter_summary)
        g.setLayout(f)
        return g

    def _refresh_rates(self, keep: float | None = None) -> None:
        name = self.filter.currentData()
        prev = keep if keep is not None else self.rate.currentData()
        self.rate.blockSignals(True)
        self.rate.clear()
        for r in rm.RATES[name]:
            self.rate.addItem(f"{r:g} sps", r)
        idx = self.rate.findData(prev)
        self.rate.setCurrentIndex(idx if idx >= 0 else len(rm.RATES[name]) // 2)
        self.rate.blockSignals(False)
        self._update_filter_summary()
        if keep is None:
            self._emit("rate", self.rate.currentData())

    def _update_filter_summary(self) -> None:
        name = self.filter.currentData()
        r = self.rate.currentData()
        if name == "SINC4" and r is not None:
            nominal = {59.8: 60}.get(r, r)
            self.filter_summary.setText(f"{nominal:g}Hz SINC4 · {r:g} sps")
        else:
            self.filter_summary.setText(f"{name} · {r:g} sps" if r is not None else "")

    def _build_conversion(self) -> QGroupBox:
        g = QGroupBox("Conversion")
        f = compact_form()
        self.conv_mode = self._combo(
            "conv_mode", rm.CONV_MODES, "Conversion type written to CONV_START."
        )
        self.channel = self._combo(
            "channel",
            [f"Channel {i}  (DATA{i})" for i in range(8)],
            "Destination data register for the conversion result.",
            data=list(range(8)),
        )
        f.addRow("Mode", self.conv_mode)
        f.addRow("Channel", self.channel)
        g.setLayout(f)
        return g

    def _build_format(self) -> QGroupBox:
        g = QGroupBox("Data Format")
        f = compact_form()
        w, self.polarity = self._radio_row(
            None, ["Bipolar", "Unipolar"], "Input range (CTRL.UNIPOLAR)."
        )
        self.polarity.idToggled.connect(lambda i, on: on and self._emit("unipolar", i == 1))
        self.format = self._combo(
            "format", ["Two's complement", "Offset binary"], "Bipolar output coding (CTRL.FORMAT)."
        )
        f.addRow("Range", w)
        f.addRow("Coding", self.format)
        g.setLayout(f)
        return g

    def _build_calibration(self) -> QGroupBox:
        g = QGroupBox("Calibration")
        lay = QGridLayout(g)
        lay.setHorizontalSpacing(6)
        lay.setVerticalSpacing(6)
        specs = [
            ("Self", "self", "Internal offset + gain self-calibration."),
            ("PGA", "pga", "PGA gain calibration."),
            (
                "System Offset",
                "sys_offset",
                "Apply zero-scale to the inputs, then calibrate offset.",
            ),
            ("System Gain", "sys_gain", "Apply full-scale to the inputs, then calibrate gain."),
        ]
        for i, (text, kind, tip) in enumerate(specs):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, k=kind: self.calibrationRequested.emit(k))
            lay.addWidget(b, i // 2, i % 2)
        return g

    def _build_power(self) -> QGroupBox:
        g = QGroupBox("Power")
        f = compact_form()
        w, self.power = self._radio_row("power", rm.POWER_MODES, "Power-down mode (PD register).")
        f.addRow(w)
        g.setLayout(f)
        return g

    def _build_serial(self) -> QGroupBox:
        g = QGroupBox("Serial Interface")
        lay = QVBoxLayout(g)
        lay.setSpacing(6)
        row = QHBoxLayout()
        self.read_all_btn = QPushButton("Read All")
        self.read_all_btn.setToolTip(
            "Read every register back from the ADC and refresh all controls."
        )
        self.convert_btn = primary_button(
            "Convert", "Start a conversion with the current settings."
        )
        self.read_all_btn.clicked.connect(self.readAllRequested)
        self.convert_btn.clicked.connect(self.convertRequested)
        row.addWidget(self.read_all_btn)
        row.addWidget(self.convert_btn)
        lay.addLayout(row)
        lay.addWidget(muted("Changes are written immediately."))
        return g

    def _build_readout(self) -> QGroupBox:
        g = QGroupBox("Read Data and Status")
        lay = QHBoxLayout(g)
        lay.setSpacing(16)
        self.status_hex = hex_output(8, "STATUS register (0x38)")
        self.data_hex = hex_output(8, "DATAn register of the selected channel")
        self.code_dec = hex_output(10, "Signed conversion result in LSB")
        self.lsb_out = hex_output(10, "Weight of one LSB for the current reference / gain / range")
        self.conv_led = Led()
        self.cal_led = Led()
        self.data_channel = muted("DATA0")

        regs = QGridLayout()
        regs.setHorizontalSpacing(8)
        regs.setVerticalSpacing(6)
        regs.addWidget(muted("STATUS"), 0, 0, Qt.AlignRight)
        regs.addWidget(self.status_hex, 0, 1)
        flags = QHBoxLayout()
        flags.setSpacing(6)
        for led, name in ((self.conv_led, "CONV_READY"), (self.cal_led, "CAL_READY")):
            flags.addWidget(led)
            flags.addWidget(muted(name))
            flags.addSpacing(8)
        flags.addStretch(1)
        regs.addLayout(flags, 0, 2, 1, 2)
        regs.addWidget(self.data_channel, 1, 0, Qt.AlignRight)
        regs.addWidget(self.data_hex, 1, 1)
        regs.addWidget(muted("Code"), 1, 2, Qt.AlignRight)
        regs.addWidget(self.code_dec, 1, 3)
        regs.addWidget(muted("1 LSB"), 1, 4, Qt.AlignRight)
        regs.addWidget(self.lsb_out, 1, 5)
        lay.addLayout(regs)
        lay.addStretch(1)

        frame = QFrame()
        frame.setObjectName("readoutFrame")
        fl = QHBoxLayout(frame)
        fl.setContentsMargins(16, 6, 16, 6)
        self.volts = QLabel("—")
        self.volts.setProperty("role", "readout")
        f = mono_font(26)
        f.setBold(True)
        self.volts.setFont(f)
        self.volts.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.volts.setMinimumWidth(self.volts.fontMetrics().horizontalAdvance("+0.0000000 V") + 8)
        fl.addWidget(muted("Voltage"), 0, Qt.AlignTop)
        fl.addWidget(self.volts, 1)
        lay.addWidget(frame)
        return g

    # ================================================================ helpers
    def _emit(self, key: str, value) -> None:
        if key == "rate":
            self._update_filter_summary()
        if key == "channel":
            self.data_channel.setText(f"DATA{value}")
        if not self._loading:
            self.settingChanged.emit(key, value)

    # ================================================================ public API (slots)
    @Slot(dict)
    def set_config(self, cfg: dict) -> None:
        """Populate controls from a config dict (same keys as settingChanged) without emitting."""
        self._loading = True
        try:

            def sel(cb: QComboBox, value) -> None:
                i = cb.findData(value)
                if i >= 0:
                    cb.setCurrentIndex(i)

            if "ainp" in cfg:
                sel(self.ainp, cfg["ainp"])  # noqa: E701
            if "ainn" in cfg:
                sel(self.ainn, cfg["ainn"])  # noqa: E701
            if "sig_path" in cfg:
                self.sig_path.button(rm.SIG_PATHS.index(cfg["sig_path"])).setChecked(True)
            if "pga_gain" in cfg:
                sel(self.pga_gain, cfg["pga_gain"])  # noqa: E701
            if "unipolar" in cfg:
                self.polarity.button(1 if cfg["unipolar"] else 0).setChecked(True)
            if "format" in cfg:
                sel(self.format, cfg["format"])  # noqa: E701
            if "filter" in cfg:
                sel(self.filter, cfg["filter"])
            self._refresh_rates(keep=cfg.get("rate", self.rate.currentData()))
            if "conv_mode" in cfg:
                sel(self.conv_mode, cfg["conv_mode"])  # noqa: E701
            if "channel" in cfg:
                sel(self.channel, cfg["channel"])
                self.data_channel.setText(f"DATA{cfg['channel']}")
            if "refsel" in cfg:
                sel(self.refsel, cfg["refsel"])  # noqa: E701
            if "vrefp_buf" in cfg:
                self.vrefp_buf.setChecked(cfg["vrefp_buf"])  # noqa: E701
            if "vrefn_buf" in cfg:
                self.vrefn_buf.setChecked(cfg["vrefn_buf"])  # noqa: E701
            if "vref" in cfg:
                self.vref.setValue(cfg["vref"])  # noqa: E701
            if "power" in cfg:
                self.power.button(rm.POWER_MODES.index(cfg["power"])).setChecked(True)
            if "idac_current_ua" in cfg:
                sel(self.idac_current, cfg["idac_current_ua"])  # noqa: E701
            if "idac0_pin" in cfg:
                sel(self.idac0_pin, cfg["idac0_pin"])  # noqa: E701
            if "idac1_pin" in cfg:
                sel(self.idac1_pin, cfg["idac1_pin"])  # noqa: E701
            if "vbias_mode" in cfg:
                sel(self.vbias_mode, cfg["vbias_mode"])  # noqa: E701
            if "vbias_pins" in cfg:
                for i, cb in enumerate(self.vbias_pins):
                    cb.setChecked(f"AIN{i}" in cfg["vbias_pins"])
            if "burnout" in cfg:
                sel(self.burnout, cfg["burnout"])  # noqa: E701
            self.pga_gain.setEnabled(self.sig_path.checkedId() == 2)
        finally:
            self._loading = False

    def config(self) -> dict:
        """Current control state as a config dict (for File > Save ADC Config)."""
        return {
            "ainp": self.ainp.currentData(),
            "ainn": self.ainn.currentData(),
            "sig_path": rm.SIG_PATHS[max(self.sig_path.checkedId(), 0)],
            "pga_gain": self.pga_gain.currentData(),
            "unipolar": self.polarity.checkedId() == 1,
            "format": self.format.currentData(),
            "filter": self.filter.currentData(),
            "rate": self.rate.currentData(),
            "conv_mode": self.conv_mode.currentData(),
            "channel": self.channel.currentData(),
            "refsel": self.refsel.currentData(),
            "vrefp_buf": self.vrefp_buf.isChecked(),
            "vrefn_buf": self.vrefn_buf.isChecked(),
            "vref": self.vref.value(),
            "power": rm.POWER_MODES[max(self.power.checkedId(), 0)],
            "idac_current_ua": self.idac_current.currentData(),
            "idac0_pin": self.idac0_pin.currentData(),
            "idac1_pin": self.idac1_pin.currentData(),
            "vbias_mode": self.vbias_mode.currentData(),
            "vbias_pins": self._vbias_pin_list(),
            "burnout": self.burnout.currentData(),
        }

    @Slot(int, int, float)
    def show_read_data(
        self,
        status: int,
        data: int,
        volts: float,
        code: int | None = None,
        lsb_volts: float | None = None,
    ) -> None:
        """Show STATUS / DATAn raw values and the converted voltage (code, LSB optional)."""
        self.status_hex.setText(fmt_hex(status, 6))
        self.data_hex.setText(fmt_hex(data, 6))
        self.code_dec.setText("—" if code is None else f"{code:+,d}")
        self.lsb_out.setText("—" if lsb_volts is None else fmt_si(lsb_volts, "V", 3))
        self.volts.setText(f"{volts:+.7f} V")
        self.conv_led.set_state("ok" if status & 0x1 else "off")
        self.cal_led.set_state("ok" if status & 0x4 else "off")

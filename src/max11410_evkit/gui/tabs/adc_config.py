"""ADC Config tab — per-channel configuration + device-wide (global) settings.

Layout
------
Left  (PER-CHANNEL):  "Channels" table (0..7: enabled, name, input, range, filter, last
                      reading) with Add / Duplicate / Reset, then the editor for the selected
                      channel (input, path & reference, filter & format), then
                      "Read Data and Status" for the last converted channel.
Right (GLOBAL):       Conversion & Power, Calibration, Excitation (IDAC / VBIAS / burn-out),
                      Serial Interface (Read All, Convert channel, Convert all enabled).

Per-channel keys (``channelConfigChanged`` dict, see placeholder_regmap.CHANNEL_KEYS):
    name enabled ainp ainn sig_path pga_gain refsel vrefp_buf vrefn_buf vref
    unipolar format filter rate settling_ms
Global keys (``settingChanged``, see placeholder_regmap.DEVICE_KEYS):
    conv_mode power idac_current_ua idac0_pin idac1_pin vbias_mode vbias_pins burnout
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import placeholder_regmap as rm
from ..channels import (
    ChannelStore,
    channel_label,
    channel_name,
    default_channel,
    filter_text,
    input_text,
    range_text,
)
from ..theme import ThemeManager, channel_color, mono_font
from ..widgets import (
    Led,
    compact_form,
    dot_icon,
    fmt_hex,
    fmt_si,
    hex_output,
    muted,
    primary_button,
)

COL_CH, COL_ON, COL_NAME, COL_INPUT, COL_RANGE, COL_FILTER, COL_LAST = range(7)
HEADERS = ["Ch", "On", "Name", "Input", "Range", "Filter / rate", "Last reading"]
ROW_H = 23


def _caption(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setProperty("role", "caption")
    return lbl


def _section_header(badge: str, badge_role: str, text: str) -> QWidget:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(2, 0, 0, 0)
    lay.setSpacing(8)
    b = QLabel(badge)
    b.setProperty("role", badge_role)
    lay.addWidget(b)
    lay.addWidget(muted(text))
    lay.addStretch(1)
    return w


def _select(cb: QComboBox, value) -> None:
    i = cb.findData(value)
    if i >= 0:
        cb.setCurrentIndex(i)


class AdcConfigTab(QWidget):
    # ---- per-channel intents
    channelConfigChanged = Signal(int, dict)  # ch, full channel config after the edit
    channelSelected = Signal(int)  # ch selected in the table (editor now shows it)
    channelEnabledChanged = Signal(int, bool)
    # ---- global intents
    settingChanged = Signal(str, object)  # global (device) keys only
    readAllRequested = Signal()
    convertRequested = Signal(int)  # convert one channel (backend applies its config first)
    convertAllRequested = Signal()  # convert every enabled channel in order
    calibrationRequested = Signal(str, int)  # kind, channel whose config to calibrate with

    def __init__(self, store: ChannelStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store or ChannelStore(self)
        self._loading = False
        self._last: dict[int, str] = {}

        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(12, 6, 12, 8)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)

        left = QVBoxLayout()
        left.setSpacing(8)
        left.addWidget(
            _section_header(
                "PER-CHANNEL",
                "badge",
                "Stored per channel; applied to the ADC before each conversion of that channel.",
            )
        )
        left.addWidget(self._build_channel_list())
        left.addWidget(self._build_editor())
        left.addWidget(self._build_readout())
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(8)
        right.addWidget(_section_header("GLOBAL", "badge-global", "Device-wide settings."))
        right.addWidget(self._build_conversion())
        right.addWidget(self._build_calibration())
        right.addWidget(self._build_excitation())
        right.addWidget(self._build_serial())
        right.addStretch(1)

        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnMinimumWidth(1, 300)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(content)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self.store.changed.connect(self._on_store_changed)
        self.store.activeChanged.connect(self._on_active_changed)
        ThemeManager.instance().changed.connect(self._refresh_table)
        self.set_device_config(rm.DEVICE_DEFAULTS)
        self._refresh_table()
        self._load_editor(self.store.active)
        self.table.selectRow(self.store.active)

    # ================================================================ channel list
    def _build_channel_list(self) -> QGroupBox:
        g = QGroupBox("Channels")
        lay = QHBoxLayout(g)
        lay.setSpacing(10)
        t = QTableWidget(rm.NUM_CHANNELS, len(HEADERS))
        t.setHorizontalHeaderLabels(HEADERS)
        t.verticalHeader().setVisible(False)
        t.verticalHeader().setDefaultSectionSize(ROW_H)
        t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.setSelectionMode(QAbstractItemView.SingleSelection)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        h = t.horizontalHeader()
        for c in (COL_CH, COL_ON, COL_INPUT, COL_RANGE, COL_FILTER, COL_LAST):
            h.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_NAME, QHeaderView.Stretch)
        self._on_boxes: list[QCheckBox] = []
        for ch in range(rm.NUM_CHANNELS):
            for c in range(len(HEADERS)):
                if c != COL_ON:
                    t.setItem(ch, c, QTableWidgetItem(""))
            for c in (COL_RANGE, COL_LAST):
                t.item(ch, c).setFont(mono_font())
            t.item(ch, COL_LAST).setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            cb = QCheckBox()
            cb.setToolTip(f"Enable channel {ch} (included in scans, captures and sequences)")
            cb.toggled.connect(lambda on, c=ch: self._set_enabled(c, on))
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setAlignment(Qt.AlignCenter)
            hl.addWidget(cb)
            t.setCellWidget(ch, COL_ON, holder)
            self._on_boxes.append(cb)
        header_h = t.horizontalHeader().sizeHint().height()
        t.setFixedHeight(header_h + ROW_H * rm.NUM_CHANNELS + 4)
        t.currentCellChanged.connect(lambda r, *_: r >= 0 and self.store.set_active(r))
        self.table = t
        lay.addWidget(t, 1)

        btns = QVBoxLayout()
        btns.setSpacing(6)
        self.add_btn = QPushButton("Add")
        self.add_btn.setToolTip("Enable the next free channel with default settings.")
        self.dup_btn = QPushButton("Duplicate")
        self.dup_btn.setToolTip("Copy the selected channel's settings into the next free channel.")
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setToolTip("Restore the selected channel to default settings.")
        for b in (self.add_btn, self.dup_btn, self.reset_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        self.add_btn.clicked.connect(self._add_channel)
        self.dup_btn.clicked.connect(self._duplicate_channel)
        self.reset_btn.clicked.connect(self._reset_channel)
        return g

    def _refresh_table(self, only: int | None = None) -> None:
        c = ThemeManager.instance().colors
        rows = range(rm.NUM_CHANNELS) if only is None or only < 0 else [only]
        for ch in rows:
            cfg = self.store.get(ch)
            on = bool(cfg["enabled"])
            fg = QColor(c.text if on else c.muted)
            texts = {
                COL_CH: str(ch),
                COL_NAME: channel_name(ch, cfg),
                COL_INPUT: input_text(cfg),
                COL_RANGE: range_text(cfg),
                COL_FILTER: filter_text(cfg),
                COL_LAST: self._last.get(ch, "—") if on else "—",
            }
            for col, text in texts.items():
                it = self.table.item(ch, col)
                it.setText(text)
                it.setForeground(fg)
            self.table.item(ch, COL_CH).setIcon(dot_icon(channel_color(ch) if on else c.border))
            box = self._on_boxes[ch]
            box.blockSignals(True)
            box.setChecked(on)
            box.blockSignals(False)
        free = len(self.store.enabled()) < rm.NUM_CHANNELS
        self.add_btn.setEnabled(free)
        self.dup_btn.setEnabled(free)

    def _first_free(self) -> int | None:
        for ch in range(rm.NUM_CHANNELS):
            if not self.store.get(ch)["enabled"]:
                return ch
        return None

    def _commit(self, ch: int) -> None:
        self.channelConfigChanged.emit(ch, self.store.get(ch))

    def _set_enabled(self, ch: int, on: bool) -> None:
        self.store.update(ch, enabled=on)
        self.channelEnabledChanged.emit(ch, on)
        self._commit(ch)

    def _add_channel(self) -> None:
        ch = self._first_free()
        if ch is None:
            return
        cfg = default_channel(ch)
        cfg["enabled"] = True
        self.store.replace(ch, cfg)
        self.channelEnabledChanged.emit(ch, True)
        self._commit(ch)
        self.table.selectRow(ch)

    def _duplicate_channel(self) -> None:
        ch = self._first_free()
        if ch is None:
            return
        src = self.store.active
        cfg = self.store.get(src)
        cfg["name"] = f"{channel_name(src, cfg)} (copy)"
        cfg["enabled"] = True
        self.store.replace(ch, cfg)
        self.channelEnabledChanged.emit(ch, True)
        self._commit(ch)
        self.table.selectRow(ch)

    def _reset_channel(self) -> None:
        ch = self.store.active
        cfg = default_channel(ch)
        cfg["enabled"] = self.store.get(ch)["enabled"]
        self.store.replace(ch, cfg)
        self._commit(ch)

    # ================================================================ editor
    @staticmethod
    def _combo(items, tooltip: str = "", data=None) -> QComboBox:
        cb = QComboBox()
        for i, it in enumerate(items):
            cb.addItem(str(it), data[i] if data else it)
        if tooltip:
            cb.setToolTip(tooltip)
        return cb

    @staticmethod
    def _radio_row(labels: list[str], tooltip: str = "") -> tuple[QWidget, QButtonGroup]:
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
        return w, grp

    def _build_editor(self) -> QGroupBox:
        g = QGroupBox("Channel settings")
        self.editor_box = g
        lay = QHBoxLayout(g)
        lay.setSpacing(18)

        # -- input
        self.name_edit = QLineEdit()
        self.name_edit.setToolTip("Free-text label shown in every channel selector.")
        self.ainp = self._combo(rm.MUX_INPUTS, "Positive ADC input (MUX_CTRL0.AINP).")
        self.ainn = self._combo(rm.MUX_INPUTS, "Negative ADC input (MUX_CTRL0.AINN).")
        self.settling = QDoubleSpinBox()
        self.settling.setRange(0, 10000)
        self.settling.setDecimals(1)
        self.settling.setSuffix(" ms")
        self.settling.setToolTip(
            "Delay after applying this channel's config (MUX/reference switch) before converting."
        )
        f1 = compact_form()
        f1.addRow(_caption("Input"))
        f1.addRow("Name", self.name_edit)
        f1.addRow("AINP", self.ainp)
        f1.addRow("AINN", self.ainn)
        f1.addRow("Settling", self.settling)

        # -- path & reference
        path_w, self.sig_path = self._radio_row(
            rm.SIG_PATHS,
            "Buffered: unity-gain input buffers. Bypass: direct to modulator. "
            "PGA: programmable gain amplifier (PGA.SIG_PATH).",
        )
        self.pga_gain = self._combo(
            [f"×{x}" for x in rm.PGA_GAINS], "PGA gain (PGA.GAIN). PGA path only.", rm.PGA_GAINS
        )
        self.refsel = self._combo(rm.REFSEL_OPTIONS, "Reference input pair (CTRL.REFSEL).")
        bufs = QWidget()
        bl = QHBoxLayout(bufs)
        bl.setContentsMargins(0, 0, 0, 0)
        self.vrefp_buf = QCheckBox("VREFP buf")
        self.vrefn_buf = QCheckBox("VREFN buf")
        self.vrefp_buf.setToolTip("Positive reference input buffer (CTRL.VREFP_BUF).")
        self.vrefn_buf.setToolTip("Negative reference input buffer (CTRL.VREFN_BUF).")
        bl.addWidget(self.vrefp_buf)
        bl.addWidget(self.vrefn_buf)
        bl.addStretch(1)
        self.vref = QDoubleSpinBox()
        self.vref.setDecimals(4)
        self.vref.setRange(0.5, 5.5)
        self.vref.setSingleStep(0.001)
        self.vref.setSuffix(" V")
        self.vref.setToolTip("Reference voltage value used to convert this channel's codes to V.")
        f2 = compact_form()
        f2.addRow(_caption("Path & reference"))
        f2.addRow("Path", path_w)
        f2.addRow("PGA gain", self.pga_gain)
        f2.addRow("REFSEL", self.refsel)
        f2.addRow("Ref. voltage", self.vref)
        f2.addRow("", bufs)

        # -- filter & format
        self.filter = self._combo(rm.FILTERS, "Digital filter type (FILTER.LINEF).")
        self.rate = QComboBox()
        self.rate.setToolTip("Output data rate (FILTER.RATE); options depend on the filter.")
        pol_w, self.polarity = self._radio_row(["Bipolar", "Unipolar"], "Input range (UNIPOLAR).")
        self.format = self._combo(
            ["Two's complement", "Offset binary"], "Bipolar output coding (CTRL.FORMAT)."
        )
        self.filter_summary = muted("")
        f3 = compact_form()
        f3.addRow(_caption("Filter & format"))
        f3.addRow("Filter", self.filter)
        f3.addRow("Data rate", self.rate)
        f3.addRow("Range", pol_w)
        f3.addRow("Coding", self.format)
        f3.addRow("", self.filter_summary)

        for f in (f1, f2, f3):
            lay.addLayout(f, 1)

        # wiring: every editor change commits the whole channel config
        self.name_edit.editingFinished.connect(self._editor_changed)
        for cb in (self.ainp, self.ainn, self.pga_gain, self.refsel, self.format, self.rate):
            cb.currentIndexChanged.connect(self._editor_changed)
        self.filter.currentIndexChanged.connect(lambda _i: self._refresh_rates())
        for box in (self.vrefp_buf, self.vrefn_buf):
            box.toggled.connect(self._editor_changed)
        for spin in (self.vref, self.settling):
            spin.valueChanged.connect(self._editor_changed)
        self.sig_path.idToggled.connect(lambda _i, on: on and self._editor_changed())
        self.polarity.idToggled.connect(lambda _i, on: on and self._editor_changed())
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
        if keep is None:
            self._editor_changed()

    def _editor_values(self) -> dict:
        return {
            "name": self.name_edit.text().strip(),
            "ainp": self.ainp.currentData(),
            "ainn": self.ainn.currentData(),
            "settling_ms": self.settling.value(),
            "sig_path": rm.SIG_PATHS[max(self.sig_path.checkedId(), 0)],
            "pga_gain": self.pga_gain.currentData(),
            "refsel": self.refsel.currentData(),
            "vrefp_buf": self.vrefp_buf.isChecked(),
            "vrefn_buf": self.vrefn_buf.isChecked(),
            "vref": self.vref.value(),
            "filter": self.filter.currentData(),
            "rate": self.rate.currentData(),
            "unipolar": self.polarity.checkedId() == 1,
            "format": self.format.currentData(),
        }

    def _editor_changed(self, *_args) -> None:
        self.pga_gain.setEnabled(self.sig_path.checkedId() == 2)
        if self._loading:
            return
        ch = self.store.active
        values = self._editor_values()
        old = self.store.get(ch)
        if all(old.get(k) == v for k, v in values.items()):
            return
        self.store.update(ch, **values)
        self._commit(ch)

    def _update_editor_title(self, ch: int) -> None:
        cfg = self.store.get(ch)
        state = "" if cfg["enabled"] else "   (disabled)"
        self.editor_box.setTitle(f"Channel {ch} — {channel_name(ch, cfg)}{state}")
        self.filter_summary.setText(filter_text(cfg))
        self.convert_btn.setText(f"Convert Ch {ch}")

    def _load_editor(self, ch: int) -> None:
        cfg = self.store.get(ch)
        self._loading = True
        try:
            self.name_edit.setText(cfg.get("name", ""))
            self.name_edit.setPlaceholderText(f"Channel {ch}")
            _select(self.ainp, cfg["ainp"])
            _select(self.ainn, cfg["ainn"])
            self.settling.setValue(float(cfg.get("settling_ms", 0.0)))
            self.sig_path.button(rm.SIG_PATHS.index(cfg["sig_path"])).setChecked(True)
            _select(self.pga_gain, cfg["pga_gain"])
            _select(self.refsel, cfg["refsel"])
            self.vrefp_buf.setChecked(cfg["vrefp_buf"])
            self.vrefn_buf.setChecked(cfg["vrefn_buf"])
            self.vref.setValue(cfg["vref"])
            _select(self.filter, cfg["filter"])
            self._refresh_rates(keep=cfg["rate"])
            self.polarity.button(1 if cfg["unipolar"] else 0).setChecked(True)
            _select(self.format, cfg["format"])
            self.pga_gain.setEnabled(cfg["sig_path"] == "PGA")
        finally:
            self._loading = False
        self._update_editor_title(ch)

    # ================================================================ device (global)
    def _global_combo(self, key: str, items, tooltip: str = "", data=None) -> QComboBox:
        cb = self._combo(items, tooltip, data)
        cb.currentIndexChanged.connect(
            lambda _i, k=key, w=cb: self._emit_global(k, w.currentData())
        )
        return cb

    def _emit_global(self, key: str, value) -> None:
        if not self._loading:
            self.settingChanged.emit(key, value)

    def _build_conversion(self) -> QGroupBox:
        g = QGroupBox("Conversion && Power")
        f = compact_form()
        self.conv_mode = self._global_combo(
            "conv_mode", rm.CONV_MODES, "Conversion type (CONV_START)."
        )
        pw, self.power = self._radio_row(rm.POWER_MODES, "Power-down mode (PD register).")
        self.power.idToggled.connect(
            lambda i, on: on and self._emit_global("power", rm.POWER_MODES[i])
        )
        f.addRow("Mode", self.conv_mode)
        f.addRow("Power", pw)
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
            ("System Offset", "sys_offset", "Apply zero-scale to the inputs, then calibrate."),
            ("System Gain", "sys_gain", "Apply full-scale to the inputs, then calibrate."),
        ]
        for i, (text, kind, tip) in enumerate(specs):
            b = QPushButton(text)
            b.setToolTip(tip + " Uses the selected channel's configuration.")
            b.clicked.connect(
                lambda _c=False, k=kind: self.calibrationRequested.emit(k, self.store.active)
            )
            lay.addWidget(b, i // 2, i % 2)
        return g

    def _build_excitation(self) -> QGroupBox:
        g = QGroupBox("Excitation (IDAC / VBIAS)")
        f = compact_form()
        self.idac_current = self._global_combo(
            "idac_current_ua",
            ["Off"] + [f"{u} µA" for u in rm.IDAC_CURRENTS_UA],
            "Matched excitation current sources for RTDs / bridges (SOURCE register).",
            data=[None] + rm.IDAC_CURRENTS_UA,
        )
        self.idac0_pin = self._global_combo("idac0_pin", rm.MUX_INPUTS[:10], "IDAC0 output pin.")
        self.idac1_pin = self._global_combo("idac1_pin", rm.MUX_INPUTS[:10], "IDAC1 output pin.")
        pins_row = QWidget()
        pr = QHBoxLayout(pins_row)
        pr.setContentsMargins(0, 0, 0, 0)
        pr.setSpacing(6)
        pr.addWidget(self.idac0_pin)
        pr.addWidget(self.idac1_pin)
        self.vbias_mode = self._global_combo(
            "vbias_mode", rm.VBIAS_MODES, "Common-mode bias voltage source (MUX_CTRL2)."
        )
        pins = QWidget()
        pg = QGridLayout(pins)
        pg.setContentsMargins(0, 0, 0, 0)
        pg.setHorizontalSpacing(2)
        pg.setVerticalSpacing(2)
        self.vbias_pins: list[QCheckBox] = []
        for i in range(10):
            cb = QCheckBox(str(i))
            cb.setToolTip(f"Apply VBIAS to AIN{i}")
            cb.toggled.connect(lambda _on: self._emit_global("vbias_pins", self._vbias_pin_list()))
            pg.addWidget(cb, i // 5, i % 5)
            self.vbias_pins.append(cb)
        self.burnout = self._global_combo(
            "burnout", rm.BURNOUT_OPTIONS, "Burn-out current sources for open-sensor detection."
        )
        f.addRow("IDAC", self.idac_current)
        f.addRow("IDAC0 / 1", pins_row)
        f.addRow("VBIAS", self.vbias_mode)
        f.addRow("VBIAS AIN", pins)
        f.addRow("Burn-out", self.burnout)
        g.setLayout(f)
        return g

    def _vbias_pin_list(self) -> list[str]:
        return [f"AIN{i}" for i, cb in enumerate(self.vbias_pins) if cb.isChecked()]

    def _build_serial(self) -> QGroupBox:
        g = QGroupBox("Serial Interface")
        lay = QGridLayout(g)
        lay.setHorizontalSpacing(6)
        lay.setVerticalSpacing(6)
        self.read_all_btn = QPushButton("Read All")
        self.read_all_btn.setToolTip("Read every register back from the ADC.")
        self.convert_btn = primary_button("Convert Ch 0", "Apply the selected channel and convert.")
        self.convert_all_btn = QPushButton("Convert All Enabled")
        self.convert_all_btn.setToolTip("Convert every enabled channel in turn.")
        self.read_all_btn.clicked.connect(self.readAllRequested)
        self.convert_btn.clicked.connect(lambda: self.convertRequested.emit(self.store.active))
        self.convert_all_btn.clicked.connect(self.convertAllRequested)
        lay.addWidget(self.read_all_btn, 0, 0)
        lay.addWidget(self.convert_btn, 0, 1)
        lay.addWidget(self.convert_all_btn, 1, 0, 1, 2)
        return g

    # ================================================================ readout
    def _build_readout(self) -> QGroupBox:
        g = QGroupBox("Read Data and Status")
        lay = QHBoxLayout(g)
        lay.setSpacing(8)
        lay.setContentsMargins(10, 6, 10, 6)
        self.status_hex = hex_output(8, "STATUS register (0x38)")
        self.data_hex = hex_output(8, "DATAn register of the converted channel")
        self.code_dec = hex_output(10, "Signed conversion result in LSB")
        self.conv_led = Led()
        self.cal_led = Led()
        self.data_channel = muted("DATA0")
        lay.addWidget(muted("STATUS"))
        lay.addWidget(self.status_hex)
        for led, name in ((self.conv_led, "CONV_READY"), (self.cal_led, "CAL_READY")):
            lay.addWidget(led)
            lay.addWidget(muted(name))
        lay.addSpacing(10)
        lay.addWidget(self.data_channel)
        lay.addWidget(self.data_hex)
        lay.addWidget(muted("Code"))
        lay.addWidget(self.code_dec)
        lay.addStretch(1)

        frame = QFrame()
        frame.setObjectName("readoutFrame")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(12, 3, 12, 3)
        fl.setSpacing(0)
        self.readout_chan = muted("—")
        self.readout_chan.setToolTip("Converted channel and the weight of one LSB")
        self.volts = QLabel("—")
        self.volts.setProperty("role", "readout")
        f = mono_font(18)
        f.setBold(True)
        self.volts.setFont(f)
        self.volts.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.volts.setMinimumWidth(self.volts.fontMetrics().horizontalAdvance("+0.0000000 V") + 8)
        fl.addWidget(self.readout_chan)
        fl.addWidget(self.volts)
        lay.addWidget(frame)
        return g

    # ================================================================ store sync
    def _on_store_changed(self, ch: int) -> None:
        self._refresh_table(ch)
        if ch < 0 or ch == self.store.active:
            self._load_editor(self.store.active)

    def _on_active_changed(self, ch: int) -> None:
        if self.table.currentRow() != ch:
            self.table.selectRow(ch)
        self._load_editor(ch)
        self.channelSelected.emit(ch)

    # ================================================================ public API (slots)
    @Slot(dict)
    def set_channel_configs(self, configs: dict) -> None:
        """{ch: cfg} for all channels (missing channels are reset/disabled). Does not emit."""
        self.store.set_all(configs)

    def channel_configs(self) -> dict[int, dict]:
        return self.store.all()

    @Slot(int)
    def set_active_channel(self, ch: int) -> None:
        self.store.set_active(ch)

    def active_channel(self) -> int:
        return self.store.active

    @Slot(dict)
    def set_device_config(self, cfg: dict) -> None:
        """Populate the global controls (DEVICE_KEYS) without emitting."""
        self._loading = True
        try:
            for key, cb in (
                ("conv_mode", self.conv_mode),
                ("idac_current_ua", self.idac_current),
                ("idac0_pin", self.idac0_pin),
                ("idac1_pin", self.idac1_pin),
                ("vbias_mode", self.vbias_mode),
                ("burnout", self.burnout),
            ):
                if key in cfg:
                    _select(cb, cfg[key])
            if "power" in cfg:
                self.power.button(rm.POWER_MODES.index(cfg["power"])).setChecked(True)
            if "vbias_pins" in cfg:
                for i, cb in enumerate(self.vbias_pins):
                    cb.setChecked(f"AIN{i}" in cfg["vbias_pins"])
        finally:
            self._loading = False

    def device_config(self) -> dict:
        return {
            "conv_mode": self.conv_mode.currentData(),
            "power": rm.POWER_MODES[max(self.power.checkedId(), 0)],
            "idac_current_ua": self.idac_current.currentData(),
            "idac0_pin": self.idac0_pin.currentData(),
            "idac1_pin": self.idac1_pin.currentData(),
            "vbias_mode": self.vbias_mode.currentData(),
            "vbias_pins": self._vbias_pin_list(),
            "burnout": self.burnout.currentData(),
        }

    def config(self) -> dict:
        """Whole ADC configuration for File ▸ Save: {"device", "active_channel", "channels"}."""
        return {
            "format": "max11410-evkit-adc-config",
            "version": 2,
            "device": self.device_config(),
            "active_channel": self.store.active,
            "channels": {str(ch): cfg for ch, cfg in self.store.all().items()},
        }

    def set_config(self, cfg: dict) -> None:
        """Inverse of ``config()``. Does not emit intents."""
        self.set_device_config(cfg.get("device", {}))
        self.set_channel_configs(cfg.get("channels", {}))
        self.set_active_channel(int(cfg.get("active_channel", 0)))
        self._load_editor(self.store.active)

    @Slot(int, int, int, float)
    def show_read_data(
        self,
        ch: int,
        status: int,
        data: int,
        volts: float,
        code: int | None = None,
        lsb_volts: float | None = None,
    ) -> None:
        """Show STATUS / DATAn and the converted voltage of channel ``ch``."""
        self.status_hex.setText(fmt_hex(status, 6))
        self.data_hex.setText(fmt_hex(data, 6))
        self.data_channel.setText(f"DATA{ch}")
        self.code_dec.setText("—" if code is None else f"{code:+,d}")
        lsb = "" if lsb_volts is None else f"  ·  1 LSB = {fmt_si(lsb_volts, 'V', 3)}"
        self.volts.setText(f"{volts:+.7f} V")
        self.readout_chan.setText(channel_label(ch, self.store.get(ch)) + lsb)
        self.conv_led.set_state("ok" if status & 0x1 else "off")
        self.cal_led.set_state("ok" if status & 0x4 else "off")
        self._last[ch] = f"{volts:+.6f} V"
        self._refresh_table(ch)

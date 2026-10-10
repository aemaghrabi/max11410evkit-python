# MAX11410 EV Kit GUI: design draft

Status: **clickable mockup** (Phase 1 design). This is PySide6 + pyqtgraph. It is *not* wired to
hardware yet. `gui/mock_backend.py` fakes every result. Run it with:

```bash
PYTHONPATH=src python -m max11410_evkit.gui            # --theme light|dark|system, --no-mock
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python docs/gui/render_screenshots.py   # PNGs
```

Screenshots are in `docs/gui/screenshots/<tab>.png` (light) and `docs/gui/screenshots/dark/<tab>.png`.

## Package layout

```
src/max11410_evkit/gui/
  __init__.py, __main__.py   python -m max11410_evkit.gui
  app.py                     main(), build_window(theme, mock)
  main_window.py             MainWindow: tabs, Status Log, menus, status bar
  status_log.py              StatusLog group box (timestamped, levelled lines, Clear Log)
  theme.py                   Fusion + light/dark QPalette + stylesheet, ThemeManager.changed
  widgets.py                 CaptureBar, StatsPanel, ThemedPlot, IntAxis, Led, BitStrip,
                             HexDelegate, fmt_si/fmt_hex
  placeholder_regmap.py      PLACEHOLDER registers / option tables (replaced by Phase 2 regmap.yaml)
  mock_backend.py            MockBackend(QObject): fake data + log lines
  tabs/
    adc_config.py other.py sequence.py registers.py scope.py dmm.py histogram.py fft.py
    _regtable.py             RegisterTable (shared by Registers + Sequence)
```

**Rule:** nothing under `gui/` imports `max11410_evkit.board` or `max11410_evkit.transport`. Later
wiring goes into a controller (Phase 5–7). The controller replaces `MockBackend`. It connects to
the tab **signals** (user intents) and calls the tab **slots** (results). That is the same pattern
`MockBackend._wire()` uses today, so that method is the reference for the wiring.

## Main window

- `QTabWidget` on top, in this order: ADC Config, Other, Sequence, Registers, Scope, DMM, Histogram, FFT.
- A vertical `QSplitter` sits between the tabs and the **Status Log** group box. The log shows a
  timestamp, a level tag (OK / WARN / ERR) and a message, with a **Clear Log** button on the right.
- The status bar has a permanent widget at the bottom right: a LED plus "EV Kit Hardware Connected" or
  "EV Kit Hardware Disconnected", with an optional detail (for example `MOCK`, or the serial/HID path later).
- Menus:
  - File: Save ADC Config As…, Save ADC Config, Load ADC Config…, Exit
  - Options: Save Graph ▸ Scope / Histogram / FFT (PNG via `QWidget.grab`)
  - View: Theme ▸ Follow System / Light / Dark
  - Help: About

| MainWindow API | Kind | Notes |
|---|---|---|
| `set_connected(connected: bool, detail: str = "")` | slot | status-bar LED and text |
| `log(message: str, level="info")` | slot | level ∈ info/ok/warn/error |
| `saveConfigRequested(str path)` / `loadConfigRequested(str path)` | signal | emitted *after* the GUI wrote or read the JSON |
| `themeChanged(str mode)` | signal | |
| `adc_tab, other_tab, sequence_tab, registers_tab, scope_tab, dmm_tab, histogram_tab, fft_tab` | attrs | tab instances |

## Tabs: layout and signal/slot contract

The conventions are the same on every tab:

- Signals are named `…Requested` or `…Changed`.
- Slots are named `show_*` or `set_*`.
- Slots never emit intent signals. They load values with signals blocked or behind a `_loading` guard.
- Voltages are in volts, frequencies in Hz, rates in sps.

### ADC Config (`AdcConfigTab`)

The layout has four columns that follow the block diagram from left to right:

1. **Input**: Input Multiplexer (AINP/AINN: AIN0–9, AVDD, GND) and Excitation (IDAC current, IDAC0/1
   pins, VBIAS mode, VBIAS pins AIN0–9, Burn-out).
2. **Analog front end**: Signal Path (Buffered / Bypass / PGA radios; the PGA gain ×1…×128 control
   is enabled only on the PGA path) and Reference (REFSEL with 7 options, VREFP/VREFN buffer
   checkboxes, Ref. voltage in V).
3. **Modulator & filter**:
   - Digital Filter: filter type, plus a data-rate list that depends on the filter, plus a summary
     such as "60Hz SINC4 · 59.8 sps".
   - Conversion: mode (Single, Continuous or Duty cycle) and Channel 0–7 (DATAn).
   - Data Format: Bipolar/Unipolar, and Two's complement/Offset binary.
4. **Control**: Calibration (Self, PGA, System Offset, System Gain), Power (Normal, Standby or Sleep) and
   Serial Interface (**Read All**, **Convert**).

A **Read Data and Status** strip spans the bottom. It shows:

- STATUS in hex, with CONV_READY and CAL_READY LEDs
- DATAn in hex, the signed code, and 1 LSB
- a large voltage readout

The startup defaults come from `placeholder_regmap.STARTUP_DEFAULTS`: 60Hz SINC4, 59.8 sps,
AIN2/GND, Channel 0, Continuous.

| Signal | Args |
|---|---|
| `settingChanged` | `(key: str, value)`. Keys: `ainp ainn sig_path pga_gain unipolar format filter rate conv_mode channel refsel vrefp_buf vrefn_buf power idac_current_ua idac0_pin idac1_pin vbias_mode vbias_pins burnout` |
| `referenceVoltageChanged` | `(volts: float)` |
| `readAllRequested` | `()` |
| `convertRequested` | `()` |
| `calibrationRequested` | `(kind: str)`. `self` / `pga` / `sys_offset` / `sys_gain` |

| Slot / method | Args |
|---|---|
| `set_config(cfg: dict)` | Uses the same keys as `settingChanged`, plus `vref`. Partial dicts are OK. Does not emit. |
| `config() -> dict` | Snapshot of the full control state. Used by File ▸ Save. |
| `show_read_data(status, data, volts, code=None, lsb_volts=None)` | `status` and `data` are raw register ints. `code` is signed LSB. |

### Other (`OtherTab`)

- **Left:** a Channel Thresholds table. It has rows Channel 0–7 and columns Lower and Upper threshold,
  plus TUR INT and TOR INT checkboxes. A Threshold units combo (LSB, V, mV, µV) sits above it.
- **Right:** three group boxes:
  - **Wait Timer**: Wait, Wait extension, the computed wait time, a note that the clock is
    2.4576 MHz, and the Calculate Wait Time and Start Wait Time buttons.
  - **Clock**: Internal or External.
  - **GPIO**: GPIO0 and GPIO1 mode.
- **Read All** sits at the bottom right.

| Signal | Args |
|---|---|
| `readAllRequested` | `()` |
| `thresholdUnitChanged` | `(unit: str)` |
| `thresholdChanged` | `(channel: int, which: "lower"\|"upper", value: float, unit: str)` |
| `thresholdInterruptChanged` | `(channel: int, which: "tur"\|"tor", enabled: bool)` |
| `waitCalcRequested` / `waitStartRequested` | `(wait: int, wait_ext: int)` |
| `clockSourceChanged` | `("internal"\|"external")` |
| `gpioModeChanged` | `(gpio: int, mode: str)` |

| Slot | Args |
|---|---|
| `set_thresholds(rows, unit)` | 8 × `(lower, upper)` already in `unit` |
| `set_threshold_interrupts(flags)` | 8 × `(tur: bool, tor: bool)` |
| `show_wait_time(seconds)` | |
| `set_wait(wait, wait_ext)`, `set_clock_source(src)`, `set_gpio_mode(gpio, mode)` | |

### Sequence (`SequenceTab`)

- **Left:** a microcode table for µC0–µC52 at 0x3A–0x6E. Its columns are Address, Name and an
  editable Value (Hex). **Read All** is below the table.
- **Right:**
  - **Sequencer**: Start address (hex spin box, 0x3A–0x6E) and **Start Sequence**.
  - **Current Address**: a read-only hex display, the name of the µC register, and
    **Read Current Address**.
  - A short explainer.

| Signal | Args |
|---|---|
| `readAllRequested` | `()` |
| `microcodeWriteRequested` | `(addr: int, value: int)`. Emitted when a Value cell is committed. |
| `sequenceStartRequested` | `(start_addr: int)` |
| `currentAddressRequested` | `()` |

| Slot | Args |
|---|---|
| `set_microcode_values(values: dict[int, int])` | |
| `show_current_address(addr: int)` | |

### Registers (`RegistersTab`)

- **Left (splitter):** a Register Map. It has a filter box (name or address), **Read** (the
  selected register), **Read All**, and a table with columns Address, Name and an editable Value (Hex).
- **Right:** a Bit Description panel. It shows:
  - the register name and its address, width and value
  - a painted **bit strip** (MSB→LSB with field brackets)
  - a field table with columns Bits, Field, Value and Description

Registers that have no known fields show "description pending (Phase 2)".

| Signal | Args |
|---|---|
| `readAllRequested` | `()` |
| `registerReadRequested` | `(addr: int)` |
| `registerWriteRequested` | `(addr: int, value: int)`. The value is masked to the register width. |

| Slot | Args |
|---|---|
| `set_register_value(addr, value)` | |
| `set_register_values(values: dict[int, int])` | |

### Scope (`ScopeTab`)

- **Toolbar:** Display unit (Voltage or LSB), Sample rate (SINC4 rates of 4…1920 sps), Samples
  (256…8192) and **Capture**.
- **Plot:** a pyqtgraph plot of amplitude against time in seconds.
- **Right:** a Statistics panel with Average, Std. deviation, Maximum, Minimum, Peak-to-peak,
  Fundamental, Samples and Sample rate.

Switching Display unit rescales the stored capture locally. It does not trigger a new capture.

| Signal | Args |
|---|---|
| `captureRequested` | `(rate_sps: float, n_samples: int)` |
| `displayUnitChanged` | `("Voltage"\|"LSB")` (informational) |

| Slot | Args |
|---|---|
| `show_capture(volts: ndarray, rate_sps, lsb_volts, stats)` | `stats` has the keys `average std_dev max min p2p fundamental_hz`, in volts and Hz |
| `set_busy(bool)` | Disables Capture and shows "Capturing…". |

### DMM (`DmmTab`)

- **Readout:** a large readout card. It shows "DC VOLTS", the input pair, a 64 pt signed value with
  auto V/mV/µV units, the raw code and the rate.
- **Trend:** a strip chart of the last 300 readings.
- **Right:**
  - **Measurement**: Rate, Averaging (Off/4/16/64), **Capture** (single reading), **Run continuous**
    (checkable; it turns into Stop) and Reset statistics.
  - **Statistics**: Mean, Std, Max, Min and the number of readings. The tab computes these itself
    from the readings it is fed.

| Signal | Args |
|---|---|
| `measureRequested` | `()` |
| `continuousToggled` | `(on: bool)`. The controller owns the timer or thread. |
| `rateChanged` | `(sps: float)` |
| `averagingChanged` | `(n: int)` |

| Slot | Args |
|---|---|
| `show_reading(volts: float, code: int = 0)` | |
| `set_input_label(text)` | |
| `reset_statistics()` | |

### Histogram (`HistogramTab`)

- **Toolbar:** **Disable Histogram**, Sample rate, Samples and **Capture**.
- **Plot:** a bar plot of occurrences against output code. It uses a one-bin-per-LSB integer axis.
- **Right:** a Noise Analysis panel with Average, Std. dev. (RMS noise), Max, Min, Peak-to-peak
  noise, Effective resolution, Noise-free resolution and Samples.

| Signal | Args |
|---|---|
| `captureRequested` | `(rate_sps, n_samples)` |
| `histogramEnabledChanged` | `(enabled: bool)` |

| Slot | Args |
|---|---|
| `show_capture(codes: ndarray[int], stats: dict)` | `stats` has the keys `average std_dev max min p2p_noise` (V) and `effective_resolution noise_free_resolution` (bits) |
| `set_busy(bool)` | |

### FFT (`FftTab`)

- **Toolbar:** Window (Blackman-Harris 4, Hann, Flat top or Rectangular), Sample rate, Samples and
  **Capture**.
- **Plot:** magnitude in dBFS against frequency in Hz. The fundamental has a marker and a label.
- **Right:** a Dynamic Performance panel with Fundamental, Fund. amplitude, THD, SNR, SINAD, SFDR,
  ENOB and Noise floor.

| Signal | Args |
|---|---|
| `captureRequested` | `(rate_sps, n_samples)` |
| `windowChanged` | `(name: str)` |

| Slot | Args |
|---|---|
| `show_spectrum(freqs_hz, mag_dbfs, metrics)` | `metrics` has the keys `fundamental_hz fundamental_dbfs thd_db snr_db sinad_db sfdr_db enob noise_floor_dbfs` |
| `set_busy(bool)` | |

## Style decisions

- **Fusion everywhere.** The app uses Fusion with an explicit `QPalette`, so it renders the same on
  macOS, Windows and Linux. Native styles fight with dark palettes and with group-box styling.
- **Light and dark palettes** are defined in `theme.py`. View ▸ Theme switches between Follow
  System (`QStyleHints.colorScheme()`, Qt ≥ 6.5), Light and Dark.
  - The dark palette makes input fields *lighter* than the group background. That keeps Fusion's
    checkbox and radio indicators visible.
  - pyqtgraph plots, LEDs and the bit strip re-colour themselves on `ThemeManager.changed`.
- **Accent colours.** There is one accent (blue) for primary actions (Capture, Convert, Start…) and for traces. A
  second colour (orange) is used for markers and highlights. Green, amber and red appear only on
  status LEDs and log tags.
- **Monospace numbers.** All numeric outputs use the system fixed-width font. That covers hex
  fields, statistics, the log and readouts, so digits don't jitter. Values are formatted with
  engineering prefixes (`fmt_si`: 900.480 nV, 2.862 Hz).
- **Group boxes and spacing.** Every group box has a title. Spacing is consistent: 12 px outer
  margins, 8–10 px between groups and 6 px between form rows. Form labels are right-aligned, and
  non-fixed fields stretch.
- **Tooltips** name the register or field a control maps to, for example "(FILTER.LINEF)".
- **Scroll safety.** The ADC Config tab is wrapped in a `QScrollArea`, so small laptop screens don't
  clip it. Every other tab is built around a table or plot that stretches.

## Open questions

1. **Register map details.** Register widths are placeholders: DATA, THRESH, SYS_*, SELF_*, STATUS,
   STATUS_IE and PART_ID are assumed to be 24-bit, and µC registers 16-bit. Option encodings for the
   IDAC currents, VBIAS modes, burn-out, GPIO modes and the FIR rate tables are also placeholders. All
   of these need to come from Phase 2 `regmap.yaml` and the datasheet.
2. **SINC4 rate list.** The mockup shows actual rates, with 59.8 for nominal 60. Should the combo
   show nominal and actual rates side by side (for example "60 Hz (59.8 sps)") once the real table
   is known? Do the rates differ between single-cycle and continuous mode? If they do, the rate
   combo must also depend on `conv_mode`.
3. **Filter naming.** The original software shows "60Hz SINC4" as one filter choice. Here it is
   LINEF = SINC4 plus a rate, with a summary label. Should the EV-kit-style combined label be kept
   as the only selector?
4. **Wait time formula.** The mock uses `(WAIT+1)·(WAIT_EXT+1)·256 / 2.4576 MHz` as a placeholder.
   The backend should own the real formula.
5. **Write policy.** Should a change be written immediately (as the mockup assumes, with "Changes
   are written immediately"), or staged until an "Apply" step? Should **Read All** on the ADC
   Config tab also refresh the Registers tab?
6. **Config file format.** File ▸ Save/Load currently dumps `AdcConfigTab.config()` as JSON on the
   GUI side. Should it be a register-image file owned by the backend instead, for compatibility with
   the Windows `.cfg` files?
7. **Capture threading.** The controller should run captures in a worker (`QThread`) and call
   `set_busy(True/False)` around them. Should a progress bar or cancel button be added for 8192-sample
   captures at low rates? 8192 samples at 4 sps takes about 34 minutes.
8. **Display unit for thresholds.** When the unit changes, should the backend convert and push back
   with `set_thresholds`, or should the GUI convert locally using the LSB size? The mockup only
   emits the change.
9. **Reference voltage default.** The power-on REFSEL and Vref of the EV kit, and whether Vref can
   be measured, are still to be confirmed. The mockup assumes REF0P/REF0N at 2.5 V.
10. **Multiple channels on Scope/DMM.** Should the scope overlay DATA0–7 when the sequencer is
    running? That would need `show_capture` to take a dict of channel → array.

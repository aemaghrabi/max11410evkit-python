# MAX11410 EV Kit GUI: design draft (rev 2, per-channel configuration)

Status: this is a **clickable mockup** built with PySide6 and pyqtgraph. It is *not* wired to
hardware yet; `gui/mock_backend.py` fakes every result. When the mock starts, it loads a demo set of
four enabled channels:

| Ch | Name | Input | Path | Reference |
|---|---|---|---|---|
| 0 | AIN2 single-ended | AIN2 – GND | Buffered | REF0, 2.5 V |
| 1 | Load-cell bridge | AIN0 – AIN1 | PGA ×128 | REF1, ratiometric |
| 2 | PT100 RTD | AIN4 – AIN5 | PGA ×16 | REF2, with IDAC |
| 3 | Thermocouple K | AIN6 – AIN7 | PGA ×32 | REF0 |

> **Phase 5 (2026-10-12):** the real app now runs on `DeviceController` + `DeviceWorker`
> (`docs/plan/p5-gui-shell.md`). The mockup described here is started with `--mock`; in real
> and `--offline` modes every tab is greyed out until Phases 6/7 wire it.

```bash
PYTHONPATH=src python -m max11410_evkit.gui --mock     # --theme light|dark|system; or --offline / (none) = USB
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python docs/gui/render_screenshots.py   # PNGs
```

Screenshots are in `docs/gui/screenshots/<tab>.png` (light) and `docs/gui/screenshots/dark/<tab>.png` (dark).

## What changed in rev 2

Rev 2 makes the ADC configurable **per channel**. On the MAX11410 the MUX, CTRL, PGA and FILTER
registers are *global*, so a "channel" is a host-side concept. It works like the per-channel
device-tree nodes in the Linux driver. There are 8 channels (0–7), and they line up with:

- DATA0–DATA7
- LTHRESH/UTHRESH0–7
- the sequencer

Before the backend converts a channel, it writes that channel's configuration into the global
registers.

| Per-channel (`CHANNEL_KEYS`) | Global (`DEVICE_KEYS`) |
|---|---|
| `name`, `enabled` | `conv_mode` (single / continuous / duty cycle) |
| `ainp`, `ainn` | `power` (normal / standby / sleep) |
| `sig_path`, `pga_gain` | `idac_current_ua`, `idac0_pin`, `idac1_pin` |
| `refsel`, `vrefp_buf`, `vrefn_buf`, `vref` (V) | `vbias_mode`, `vbias_pins` |
| `unipolar`, `format` | `burnout` |
| `filter`, `rate` (sps), `settling_ms` | *(Other tab: clock, GPIO, wait timer; Sequence tab: sequencer; calibration actions)* |

The defaults are in `placeholder_regmap.CHANNEL_DEFAULTS` and `DEVICE_DEFAULTS`. Channel 0 is
enabled at startup with the EV-kit defaults: AIN2–GND, 60Hz SINC4, 59.8 sps, Continuous.

### Shared channel model

`gui/channels.py` provides `ChannelStore(QObject)`, the single in-GUI source of truth. It holds the
8 configs and the *active* channel. `MainWindow` creates one store (`win.channels`) and passes it
to the tabs:

- The **ADC Config** tab edits the store.
- **Other**, **Scope**, **DMM**, **Histogram** and **FFT** only read it, to get names, colours,
  enabled flags and rates. They follow `ChannelStore.changed(int ch)` (where `-1` means all) and
  `activeChanged(int)`.

`channels.py` also contains small helpers: `channel_label`, `input_text`, `range_text`,
`filter_text` and `lsb_volts`.

Every channel has a fixed colour (`theme.channel_color(ch)`, one palette for light and one for
dark). The colour is used everywhere: the dot in the channel table, the selector chips, the plot
traces, the DMM tiles and the threshold row headers.

## Package layout

```
src/max11410_evkit/gui/
  __init__.py, __main__.py   python -m max11410_evkit.gui
  app.py                     main(), build_window(theme, mock)
  main_window.py             MainWindow: ChannelStore, tabs, Status Log, menus, status bar
  channels.py                ChannelStore + channel text/LSB helpers
  status_log.py              StatusLog group box
  theme.py                   Fusion + light/dark palettes, stylesheet, channel colours
  widgets.py                 ChannelChips, CaptureBar, StatsPanel, ThemedPlot, IntAxis, Led,
                             BitStrip, HexDelegate, dot_icon, fmt_si/fmt_hex
  placeholder_regmap.py      PLACEHOLDER registers / options / CHANNEL_ & DEVICE_DEFAULTS
  mock_backend.py            MockBackend: per-channel fake data + log lines
  tabs/ adc_config.py other.py sequence.py registers.py scope.py dmm.py histogram.py fft.py _regtable.py
```

**Rule:** nothing under `gui/` imports `board`, `transport` or `device`. The Phase 5–7 controller
replaces `MockBackend`. It connects to the tab **signals** (user intents) and calls the tab **slots**
(results). `MockBackend._wire()` is the reference for that wiring.

## Main window

- Tabs on top: ADC Config, Other, Sequence, Registers, Scope, DMM, Histogram, FFT.
- **Status Log** below the tabs, separated by a splitter. The status bar shows the connection state
  at the bottom right.
- Menus:
  - **File:** Save ADC Config As…, Save, Load…, Exit
  - **Options:** Save Graph ▸ Scope / Histogram / FFT
  - **View:** Theme ▸ System / Light / Dark
  - **Help:** About

| MainWindow API | Kind | Notes |
|---|---|---|
| `channels` | attr | shared `ChannelStore` |
| `set_connected(connected, detail="")`, `log(message, level="info")` | slots | |
| `saveConfigRequested(path)`, `loadConfigRequested(path)`, `themeChanged(mode)` | signals | |

### Config file (File ▸ Save / Load)

The file is JSON and contains the whole ADC configuration, **including every channel**:

```json
{ "format": "max11410-evkit-adc-config", "version": 2,
  "device": { "conv_mode": "Continuous", "power": "Normal", "idac_current_ua": 250, ... },
  "active_channel": 1,
  "channels": { "0": { "name": "AIN2 single-ended", "enabled": true, "ainp": "AIN2", ... },
                "1": { ... }, ..., "7": { ... } } }
```

`AdcConfigTab.config()` produces this structure and `set_config()` reads it back. Any channel
missing from the file is reset to the defaults and disabled.

## Tabs: layout and signal/slot contract

These conventions apply to every tab:

- Signals are named `…Requested`, `…Changed` or `…Selected`.
- Slots are named `show_*` or `set_*`, and never emit intent signals.
- Units are V, Hz and sps.
- Wherever several channels are involved, results are passed as **dicts keyed by channel**.

### ADC Config (`AdcConfigTab(store)`)

The tab is split into two parts with explicit badges.

**PER-CHANNEL** (left), from top to bottom:

1. A **Channels** table with columns Ch (colour dot), On (checkbox), Name, Input, Range (for
   example `±25.78 mV · ×128`), Filter / rate and Last reading. Disabled rows are dimmed. Buttons
   on the right:
   - **Add** enables the next free channel with the default settings.
   - **Duplicate** copies the selected channel into the next free slot as "… (copy)".
   - **Reset** restores the selected channel to the defaults.
2. A **Channel n — name** editor for the selected row, in three columns:
   - **Input:** Name, AINP, AINN, Settling
   - **Path & reference:** Buffered/Bypass/PGA, PGA gain, REFSEL, Ref. voltage, VREFP/VREFN buffers
   - **Filter & format:** Filter, Data rate, Bipolar/Unipolar, Coding, plus a summary such as
     "20Hz SINC4 · 20 sps"
3. **Read Data and Status** for the last conversion: STATUS with the CONV_READY and CAL_READY LEDs,
   DATAn, the code, and a readout with the channel name, 1 LSB and the voltage.

**GLOBAL** (right): Conversion & Power, Calibration, Excitation (IDAC / VBIAS / burn-out) and Serial
Interface. Serial Interface has three buttons: **Read All**, **Convert Ch n** (the selected channel)
and **Convert All Enabled**.

| Signal | Args |
|---|---|
| `channelConfigChanged` | `(ch: int, cfg: dict)`. `cfg` is the **full** channel config after the edit; any editor change, enable toggle, Add, Duplicate or Reset emits it. |
| `channelSelected` | `(ch: int)`. The editor now shows `ch`. |
| `channelEnabledChanged` | `(ch: int, enabled: bool)`. Emitted before the matching `channelConfigChanged`. |
| `settingChanged` | `(key: str, value)`. **Global keys only** (`DEVICE_KEYS`). |
| `readAllRequested` | `()` |
| `convertRequested` | `(ch: int)`. The backend applies `ch`'s config, then converts into DATAch. |
| `convertAllRequested` | `()`. Convert every enabled channel in order. |
| `calibrationRequested` | `(kind: "self"\|"pga"\|"sys_offset"\|"sys_gain", ch: int)`. Calibrate using `ch`'s config. |

| Slot / method | Args |
|---|---|
| `set_channel_configs(configs: dict[int\|str, dict])` | Replaces all 8 configs (missing ones are reset and disabled). Does not emit. |
| `channel_configs() -> dict[int, dict]` | |
| `set_active_channel(ch)`, `active_channel()` | |
| `set_device_config(cfg)`, `device_config() -> dict` | global keys |
| `config() -> dict`, `set_config(cfg)` | file format above |
| `show_read_data(ch, status, data, volts, code=None, lsb_volts=None)` | Also fills the channel's "Last reading" cell. |

### Other (`OtherTab(store)`)

The Channel Thresholds rows are labelled with the **channel names and colours**, for example
"1 · Load-cell bridge". Disabled channels are dimmed. Wait Timer, Clock and GPIO stay global.

The signals and slots are unchanged from rev 1:

- **Signals:** `readAllRequested`, `thresholdUnitChanged`, `thresholdChanged(ch, which, value, unit)`,
  `thresholdInterruptChanged(ch, which, on)`, `waitCalcRequested(wait, ext)`,
  `waitStartRequested(wait, ext)`, `clockSourceChanged`, `gpioModeChanged`.
- **Slots:** `set_thresholds`, `set_threshold_interrupts`, `show_wait_time`, `set_wait`,
  `set_clock_source`, `set_gpio_mode`.

### Measurement tabs: channel selection

Each measurement tab has a row of **channel chips**. There is one chip per *enabled* channel,
showing the colour dot and "n · name". The chips follow the store live.

- **Scope** and **DMM** use multi-select. The first selected channel is the *primary* one.
- **Histogram** and **FFT** use single-select.

The data rate belongs to the channel config, so the capture bar no longer has a rate control. It
shows "59.8 sps (from channel config)", or "mixed rates" when the selected channels differ.

#### Scope (`ScopeTab(store)`)

- **Controls:** channel chips (multi), Display unit (Voltage or LSB), Samples, Capture.
- **Plot:** one coloured trace per channel with a legend. Each trace uses its own time axis,
  `t = i / rate[ch]`.
- **Right:** a Statistics panel with a channel combo at the top.

| Signal | Args |
|---|---|
| `captureRequested` | `(channels: list[int], n_samples: int)` |
| `channelsSelected` | `(channels: list[int])` |
| `displayUnitChanged` | `(unit: "Voltage"\|"LSB")` |

| Slot | Args |
|---|---|
| `show_capture(data, rates, lsb_volts, stats)` | Every argument is a dict keyed by channel: `data[ch]` is an ndarray in V, `rates[ch]` in sps, `lsb_volts[ch]` in V/LSB, and `stats[ch]` has the keys `average std_dev max min p2p fundamental_hz`. |
| `set_selected_channels(chs)`, `set_busy(bool)` | |

#### DMM (`DmmTab(store)`)

- **Big readout:** the primary channel, showing its name in the channel colour, the input, a 56 pt
  value with automatic V/mV/µV units, the code and the filter/rate.
- **Mini tiles:** one tile per *other* selected channel. Clicking a tile makes that channel the
  primary one.
- **Trend:** shows either "Deviation from mean (all selected)" (the default, so channels with mV and
  V ranges can be compared) or "Absolute (primary only)".
- **Right:**
  - **Measurement:** Averaging, Capture, Run continuous, Reset statistics.
  - **Statistics:** for the primary channel.

| Signal | Args |
|---|---|
| `measureRequested` | `(channels: list[int])`. Read each channel once. |
| `continuousToggled` | `(on: bool)`. The controller keeps reading `selected()`. |
| `channelsSelected` | `(channels: list[int])` |
| `averagingChanged` | `(n: int)` |

| Slot | Args |
|---|---|
| `show_reading(ch, volts, code=0)` | |
| `selected() -> list[int]`, `set_selected_channels(chs)`, `reset_statistics()` | |

#### Histogram (`HistogramTab(store)`)

The tab has channel chips (single-select), Disable Histogram, Samples and Capture. Bars are drawn
in the channel colour, one bin per LSB. The panel title reads "Noise Analysis — n · name".

| Signal | Args |
|---|---|
| `captureRequested` | `(ch: int, n_samples: int)` |
| `channelSelected` | `(ch)` |
| `histogramEnabledChanged` | `(bool)` |

| Slot | Args |
|---|---|
| `show_capture(ch, codes, stats)` | `stats` has the keys `average std_dev max min p2p_noise` (V) and `effective_resolution noise_free_resolution` (bits) |
| `set_selected_channel(ch)`, `set_busy` | |

#### FFT (`FftTab(store)`)

The tab has channel chips (single-select), Window, Samples and Capture. The spectrum is drawn in
the channel colour with a marker on the fundamental. The panel title reads
"Dynamic Performance — n · name".

| Signal | Args |
|---|---|
| `captureRequested` | `(ch: int, n_samples: int)` |
| `channelSelected` | `(ch)` |
| `windowChanged` | `(name)` |

| Slot | Args |
|---|---|
| `show_spectrum(ch, freqs_hz, mag_dbfs, metrics)` | `metrics` has the keys `fundamental_hz fundamental_dbfs thd_db snr_db sinad_db sfdr_db enob noise_floor_dbfs` |
| `set_selected_channel(ch)`, `set_busy` | |

### Sequence and Registers

These tabs are unchanged from rev 1:

- `SequenceTab` signals: `readAllRequested`, `microcodeWriteRequested`, `sequenceStartRequested`,
  `currentAddressRequested`.
- `RegistersTab` signals: `readAllRequested`, `registerReadRequested`, `registerWriteRequested`.

## Rationale

- **Selector + editor rather than 8 copies of the controls.** One table shows every channel at a
  glance (input, range, rate, last value). One editor holds the full control set, the same controls
  as rev 1, so the screen stays as dense as the original EV-kit software.
- **The PER-CHANNEL / GLOBAL split is explicit.** The two halves are separated by badges and their
  position on the page, so a user can see which settings follow the channel and which affect the
  whole device. Calibration sits under GLOBAL because it is a device action, but it uses the
  selected channel's configuration (the `ch` argument).
- **The rate lives in the channel config.** This avoids two sources of truth (the capture-bar rate
  against the channel rate). Captures always run with exactly what the channel table shows.
- **Results are keyed by channel.** That lets Scope and DMM show several channels without changing
  the API, and Histogram and FFT pass `ch` explicitly so their panel titles can never go stale.
- **The style is unchanged:** Fusion with light/dark palettes, monospace numbers, group boxes and
  one accent colour. Channel colours are the only new colours.

## Decisions for Phase 6 (Ahmed, 2026-10-12 00:41)

The open questions of rev 2 are answered:

| # | Question | Decision |
|---|---|---|
| 1 | Excitation (IDAC, VBIAS, burn-out) | **Per channel.** It becomes part of the channel config and is applied together with the MUX before each conversion of that channel. |
| 2 | Thresholds (LTHRESH/UTHRESH) | **In the channel config**, and so in the config file. |
| 3 | `n_samples` in multi-channel captures | **Per channel.** The backend reports each channel's effective rate. |
| 4 | Convert All Enabled / multi-channel captures | **Sequencer microcode.** The Sequence tab shows the generated microcode. |
| 5 | Disabling a channel that a measurement tab has selected | **Warn first.** |
| 6 | Data rate shown | **The rate for the current conversion mode** (single-cycle / continuous / duty-cycle, datasheet Tables 9a–9d). |
| 7 | Confirming board writes | **Per-session "don't ask again"** option on the confirmation dialog. |
| 8 | Windows `.cfg` import/export | **Not needed.** |
| — | Register widths, option codes, FIR rates | From `regmap.yaml` and the datasheet tables (Phase 2). |
| — | Reference voltage | Not a register value; used for conversion only. Stored in the config only, read from a config loaded with File ▸ Load; 2.5 V otherwise (a fresh connect has no config). (2026-10-12 00:34 / 00:44) |

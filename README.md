# max11410-evkit

Cross-platform (macOS, Linux, Windows) host software for the Analog Devices/Maxim
**MAX11410EVKIT** — 24-bit ADC behind a MAXQ622 USB-HID bridge. No drivers needed: the board
is a vendor HID device (VID `0x0B6A`, PID `0x2B31`, "MAXQ622HID").

```
src/max11410_evkit/
  transport/   64-byte HID reports: HidTransport (hidapi), FakeEvKitTransport (emulated board)
  board/       EVKIT firmware command protocol: EvKitBoard (typed method per command)
  device/      MAX11410: regmap.yaml + RegisterMap (named registers/fields, shadow cache),
               MAX11410 (init, channels, conversions, self-calibration, volts)
  acquisition.py  acquire(): captures of any length, split into board-SRAM-sized blocks
  analysis/    statistics (effective / noise-free resolution), code histogram, FFT metrics
  export.py    CSV files for captures, histograms and spectra ("Save Graph")
  cli.py       max11410-evkit smoke-test CLI
  gui/         desktop app (PySide6 + pyqtgraph)
```

## Install

```sh
# pip
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"          # add ,gui for the desktop app: ".[dev,gui]"

# or uv
uv venv && uv pip install -e ".[dev]"
```

`hidapi` ships wheels with the native library bundled for macOS, Linux and Windows.

### Linux: device permissions

Create `/etc/udev/rules.d/70-max11410-evkit.rules` containing

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0b6a", ATTRS{idProduct}=="2b31", MODE="0660", TAG+="uaccess"
```

then `sudo udevadm control --reload-rules && sudo udevadm trigger` and re-plug the board.

## Board power-up sequence

Do this every time before connecting the board. If you skip it, the USB link still works but the ADC side of the
isolator stays unpowered.

1. With USB unplugged, set the jumpers:
   - [ ] **JP12 (VDUT source) → VADJ** (the 3.3 V regulator)
   - [ ] **JP13 (5V Enable) → shorted**
   - [ ] **JP1, JP2, JP3 → shorted** (VDUT to AVDD, VDDREG and VDDIO)
2. Plug in USB.
3. Run `max11410-evkit info`. It should show firmware 1.1, MAX11410EVK, echo ok and **PART_ID 0x000F02**.

## Troubleshooting

**`info` works but every register reads 0xFF (PART_ID 0xFFFFFF).** The ADC side isn't powered. Redo the
power-up sequence above, then unplug and replug USB.

## Smoke test (Phase 1 gate)

```sh
max11410-evkit list              # board path / serial
max11410-evkit info              # expect: firmware 1.1, project MAX11410EVK, echo ok, PART_ID
max11410-evkit peek 11           # PART_ID (24-bit)
max11410-evkit poke 0B 2A        # write MUX_CTRL0, read back
max11410-evkit -v info           # log every command/reply
max11410-evkit --fake info       # same against the emulated board
```

Register addresses and values are hex. Registers 0x00–0x10 are 8-bit, 0x11–0x39 24-bit; for
0x3A–0x6F give the width explicitly (`peek 3A 2`).

## Register map and conversions (Phase 2)

```sh
max11410-evkit regs                  # 0x00-0x39 with field decode; --all adds the sequencer
max11410-evkit get CTRL.REFSEL       # -> 1 (REF1)
max11410-evkit set FILTER.LINEF SINC4
max11410-evkit set FILTER.RATE 4     # numbers: decimal, or 0x.. for hex
max11410-evkit selfcal               # self + PGA calibration, FILTER/PGA restored
max11410-evkit read --ch 0 -n 5      # EVKIT default: AIN2 vs GND, REF1 2.5 V, SINC4 60 sps
max11410-evkit read -n 5 --ainp 3 --gain 8 --bipolar --method poll
```

The register map lives in `src/max11410_evkit/device/regmap.yaml`. Each register has its
address, width, access, reset value and fields (bits, enum values). Anything not yet checked
against the datasheet is marked `confirmed: false` and "TODO(datasheet)". Sources: the Linux
`max11410` IIO driver, and values measured on the EVKIT (PD reset 0x01, CTRL reset 0x01,
PART_ID 0x000F02). The sequencer registers 0x3A–0x6F are assumed to be 16-bit (unconfirmed).

```python
from max11410_evkit import connect
from max11410_evkit.device import MAX11410, ChannelConfig

with connect() as board:
    adc = MAX11410(board)
    adc.init()  # PART_ID check, PD=0, CTRL.FORMAT=1
    adc.regs.FILTER.LINEF = "SINC4"  # read-modify-write via the shadow cache
    print(adc.regs.CTRL.REFSEL)  # 1(REF1)
    adc.self_calibrate()
    adc.set_channel(
        1, ChannelConfig(name="bridge", ainp=0, ainn=1, bipolar=True, path="pga", gain=64)
    )
    print(adc.read_single(1).volts)
    snap = adc.regs.read_all()
    print(adc.regs.dump(snap))
```

Conversions use offset binary (FORMAT = 1): bipolar `V = (raw − 2^23)·2·Vref/(2^24·G)`,
unipolar `V = raw·Vref/(2^24·G)`.

## Captures and analysis (Phase 3)

```sh
max11410-evkit capture -n 1000                 # statistics (alias: stats)
max11410-evkit capture -n 50000 -o cap.csv     # any length; saved as CSV
max11410-evkit capture -n 2000 --single        # single-cycle conversions
max11410-evkit hist -n 4096 -o hist.csv        # code histogram
max11410-evkit fft -n 8192 --window bh4 -o fft.csv   # SNR, THD, SINAD, SFDR, ENOB
```

All four accept the same channel options as `read` (`--ch --ainp --ainn --refsel --vref --rate
--linef --gain --path --bipolar`). Ctrl-C cancels after the current block and discards all
the data of that capture.

* **Blocks and gaps.** The firmware captures into the board SRAM (32 KB assumed, so at most
  10,922 samples per block). Longer captures are split into blocks automatically. The ADC is
  idle while a block is read back over USB, so there is a time gap before every new block (the
  `block` column of the CSV). An FFT therefore always uses one gap-free block: a power of 2, at
  most 8,192 samples.
* **Definitions.** FSR = 2·VREF/gain bipolar, VREF/gain unipolar. Effective resolution =
  log2(FSR/rms noise). Noise-free resolution is shown twice: log2(FSR/(6.6·rms)) as in the
  datasheet, and log2(FSR/measured p-p). FFT: 0 dBFS = sine of peak FSR/2, THD over harmonics
  2–6, ENOB = (SINAD − 1.76)/6.02, default window Blackman-Harris 4-term. Hann and rectangular
  leak too much for a tone that is not exactly on an FFT bin; use them only for coherent tones.

```python
import threading
from max11410_evkit import connect
from max11410_evkit.acquisition import acquire
from max11410_evkit.device import MAX11410
from max11410_evkit.export import save_capture_csv

with connect() as board:
    adc = MAX11410(board)
    adc.init()
    stop = threading.Event()  # stop.set() from another thread cancels
    cap = acquire(adc, channel=0, n=20000, cancel=stop, progress=lambda d, t: None)
    st = cap.stats()
    print(st.average, st.effective_resolution, st.noise_free_resolution)
    save_capture_csv("cap.csv", cap, stats=st)
    fft = acquire(adc, 0, 8192, gap_free=True).spectrum("blackman-harris-4")
    print(fft.metrics.snr_db, fft.metrics.enob)
```

## Library use (board level)

```python
from max11410_evkit import connect
from max11410_evkit.board import samples_from_bytes

with connect() as board:  # first board; verifies "MAX11410EVK"
    print(board.version(), board.project())
    print(hex(board.read_register(0x11)))  # 0x80 read flag added for you
    board.write_register(0x0B, 0x2A)  # MUX_CTRL0: AIN2 vs GND
    raw = board.wait_drdy_read(0x30, 3, start=True, conv=0x00)  # one conversion
    data = board.capture(1000, conv=0x01, timeout=1.0)  # SRAM block capture
    samples = samples_from_bytes(data, 3)
```

`EvKitBoard(..., on_event=callback)` receives a one-line string per command/reply (for a GUI
log); the same lines go to the `max11410_evkit.board` logger at DEBUG.

## Tests

```sh
pytest                 # no hardware, no native hidapi needed
ruff check . && ruff format --check .
```

The tests run against `FakeEvKitTransport`, an emulation of the board firmware (`main.c`) plus
a toy MAX11410 register model.

## Protocol notes

* Host → board: `[N][cmd][args…]` in a 64-byte report (hidapi: `0x00` report ID + 64 bytes).
  Board → host: `[N][status][data…]`, `0xEE` ok / `0xFA` failed. `0x43` (SRAM read) has no
  status byte.
* The idle board sends a filler report (first byte 0) every ~1 ms; stale reports are drained
  before each command and fillers are skipped while waiting.
* One command in flight at a time — the firmware silently drops commands sent during a `0x42`
  capture. `EvKitBoard` serialises callers with a lock.
* SPI passthrough sends the address byte unchanged: reads need `reg | 0x80`. A "read" without
  the flag is a write of zeros to the register.
* Commands `0x45`/`0x46` never reply and are not exposed.
* Board SRAM is assumed to be 32 KB (`EVKIT_SRAM_BYTES`) until the BOM confirms the part.

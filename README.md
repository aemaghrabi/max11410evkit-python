# max11410-evkit

Cross-platform (macOS, Linux, Windows) host software for the Analog Devices/Maxim
**MAX11410EVKIT** — 24-bit ADC behind a MAXQ622 USB-HID bridge. No drivers needed: the board
is a vendor HID device (VID `0x0B6A`, PID `0x2B31`, "MAXQ622HID").

```
src/max11410_evkit/
  transport/   64-byte HID reports: HidTransport (hidapi), FakeEvKitTransport (emulated board)
  board/       EVKIT firmware command protocol: EvKitBoard (typed method per command)
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

## Library use

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

## GUI screenshots

The tab screenshots in `docs/gui/screenshots/` are generated, not committed (`screenshots/` is in
`.gitignore`). To regenerate them:

```sh
pip install -e ".[gui]"          # PySide6, pyqtgraph, numpy
QT_QPA_PLATFORM=offscreen PYTHONPATH=src python docs/gui/render_screenshots.py --theme both
```

`--theme` is `light`, `dark` or `both`. Light shots go to `docs/gui/screenshots/<tab>.png`, dark
to `docs/gui/screenshots/dark/<tab>.png`. `QT_QPA_PLATFORM=offscreen` renders without opening a
window, so no display is needed.

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

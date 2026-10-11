"""Find the real size of the EVKIT capture SRAM (measured 32 KB on 2026-10-11).

Method
    The firmware addresses the SRAM with 16 bits (``uint16_t data_cntr`` in main.c), so it can
    reach at most 64 KB. One 0x42 capture of 21,845 samples (65,535 bytes) writes the whole
    16-bit range. An SRAM smaller than that ignores the upper address bits, so later samples
    overwrite earlier ones and reading address A + size returns the same byte as address A.
    Reading all 64 KB back and checking for a repeat at 8, 16, 32 KB gives the size. ADC
    noise makes an accidental match of thousands of bytes impossible.

Usage (board powered as in the README; takes about 30 s at the default SINC4 rate 8)

    python scripts/sram_size_check.py            # real board
    python scripts/sram_size_check.py --fake     # emulated board (32 KB), to check the script
"""

from __future__ import annotations

import argparse
import sys
import time

from max11410_evkit.board.evkit import connect
from max11410_evkit.device.max11410 import MAX11410

FULL_RANGE = 0x10000  # 16-bit SRAM address in the firmware
SAMPLES = (FULL_RANGE - 1) // 3  # 21,845 samples = 65,535 bytes
CANDIDATES = (0x2000, 0x4000, 0x8000)  # 8, 16, 32 KB


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fake", action="store_true", help="use the emulated board")
    ap.add_argument("--rate", type=int, default=8, help="SINC4 FILTER.RATE index (default 8)")
    args = ap.parse_args()

    if args.fake:
        from max11410_evkit.transport.fake import FakeEvKitTransport

        board = connect(transport=FakeEvKitTransport())
    else:
        board = connect()
    with board:
        adc = MAX11410(board)
        adc.init()
        adc.channels[0].rate = args.rate
        board.sram_bytes = FULL_RANGE  # lift the 32 KB guard for this test only
        rate = adc.channels[0].data_rate()
        print(f"capturing {SAMPLES} samples ({SAMPLES * 3} B) at {rate:g} sps ...")
        t0 = time.monotonic()
        adc.capture_codes(0, SAMPLES)
        print(f"capture done in {time.monotonic() - t0:.1f} s; reading back 64 KB ...")
        data = board.sram_read(0, FULL_RANGE - 1)
        print(f"read back {len(data)} B")

    if len(set(data[:4096])) < 16:
        print("error: the data has almost no variation, so the check is not reliable")
        return 2
    for size in CANDIDATES:
        n = len(data) - size
        same = sum(a == b for a, b in zip(data[:n], data[size:], strict=True))
        print(f"  {size // 1024:2d} KB: {same}/{n} bytes repeat at +{size:#06x}")
        if same == n:
            print(f"RESULT: the SRAM is {size // 1024} KB (addresses wrap at {size:#06x})")
            print(
                f"        max block = {size // 3} samples, max FFT = "
                f"{1 << ((size // 3).bit_length() - 1)}"
            )
            return 0
    print("RESULT: no wrap up to 32 KB -> the SRAM holds the full 64 KB the firmware can address")
    print(f"        max block = {SAMPLES} samples, max FFT = 16384")
    return 0


if __name__ == "__main__":
    sys.exit(main())

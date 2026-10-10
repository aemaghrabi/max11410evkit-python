"""EVKIT firmware command layer."""

from max11410_evkit.board.evkit import (
    EVKIT_SRAM_BYTES,
    PROJECT_NAME,
    READ_FLAG,
    Cmd,
    EvKitBoard,
    FirmwareVersion,
    connect,
    register_width,
    samples_from_bytes,
    spi_clock_hz,
)

__all__ = [
    "EVKIT_SRAM_BYTES",
    "PROJECT_NAME",
    "READ_FLAG",
    "Cmd",
    "EvKitBoard",
    "FirmwareVersion",
    "connect",
    "register_width",
    "samples_from_bytes",
    "spi_clock_hz",
]

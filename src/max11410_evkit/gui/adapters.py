"""Backend → GUI value mapping (Phase 5: read direction only).

The tabs speak the GUI channel schema (``placeholder_regmap.CHANNEL_KEYS``: option strings,
rates in sps). The backend speaks :class:`~max11410_evkit.device.max11410.ChannelConfig`
(register codes). This module is the only place that knows both. The GUI → backend direction
comes with Phase 6, when the tabs are wired.
"""

from __future__ import annotations

from collections.abc import Mapping

from max11410_evkit.device.max11410 import ChannelConfig

from . import placeholder_regmap as rm

_LINEF_TO_GUI = {
    "FIR5060": "FIR 50/60 Hz",
    "FIR50": "FIR 50 Hz",
    "FIR60": "FIR 60 Hz",
    "SINC4": "SINC4",
}
_PATH_TO_GUI = {"buffered": "Buffered", "bypass": "Bypass", "pga": "PGA"}
_FORMATS = ("Two's complement", "Offset binary")  # CTRL.FORMAT 0 / 1


def input_name(code: int, *, negative: bool) -> str:
    """MUX_CTRL0 input code → GUI option text."""
    if 0 <= code <= 9:
        return f"AIN{code}"
    if code == 10:
        return "GND" if negative else "AVDD"
    return "NC"  # 11..15 (INPUT_NC)


def gui_rate(linef: str, rate: int) -> float:
    """FILTER.RATE code → the GUI's sps value (continuous-mode table, repeats the last entry)."""
    table = rm.RATES[_LINEF_TO_GUI[linef]]
    return table[min(max(rate, 0), len(table) - 1)]


def gui_channel(cfg: ChannelConfig, *, ctrl: int | None = None, name: str | None = None) -> dict:
    """Backend channel → GUI channel dict. ``ctrl`` (the CTRL register) supplies FORMAT."""
    out = dict(rm.CHANNEL_DEFAULTS)
    out.update(
        name=cfg.name if name is None else name,
        enabled=bool(cfg.enabled),
        ainp=input_name(cfg.ainp, negative=False),
        ainn=input_name(cfg.ainn, negative=True),
        sig_path=_PATH_TO_GUI[cfg.path],
        pga_gain=int(cfg.gain),
        refsel=rm.REFSEL_OPTIONS[cfg.refsel],
        vrefp_buf=bool(cfg.vrefp_buf),
        vrefn_buf=bool(cfg.vrefn_buf),
        vref=float(cfg.vref_volts),
        unipolar=not cfg.bipolar,
        filter=_LINEF_TO_GUI[cfg.linef],
        rate=gui_rate(cfg.linef, cfg.rate),
        settling_ms=cfg.settling_us / 1000.0,
    )
    if ctrl is not None:
        out["format"] = _FORMATS[(ctrl >> 5) & 1]
    return out


def gui_channels(channels: list[ChannelConfig], *, ctrl: int | None = None) -> dict[int, dict]:
    """All 8 backend channels → ``{ch: GUI dict}``."""
    return {i: gui_channel(c, ctrl=ctrl) for i, c in enumerate(channels)}


def gui_device(registers: Mapping[str, int]) -> dict:
    """Register values → GUI global (device) keys. Phase 5 maps the power state only."""
    out: dict = {}
    if "PD" in registers:
        pd = registers["PD"] & 0x3
        if pd < len(rm.POWER_MODES):  # 3 = RESET reads back as 0 after the reset
            out["power"] = rm.POWER_MODES[pd]
    return out


__all__ = ["gui_channel", "gui_channels", "gui_device", "gui_rate", "input_name"]

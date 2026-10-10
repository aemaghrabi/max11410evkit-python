"""Host-side channel model shared by all tabs.

On the MAX11410 the MUX/CTRL/PGA/FILTER registers are global. A *channel* is a
host-side bundle of settings (AINP/AINN, reference, path/gain, polarity, format,
filter/rate, settling time, name, enabled) — like the Linux driver's per-channel
device-tree nodes. Channels 0..7 line up with DATA0..7, LTHRESH/UTHRESH0..7 and
the sequencer. The backend applies a channel's config to the global registers
before converting it.

``ChannelStore`` is the single in-GUI source of truth for the 8 channel configs.
The ADC Config tab edits it; the measurement tabs and the Other tab only read it
(names, enabled flags, rates) and follow ``changed``.
"""

from __future__ import annotations

import copy

from PySide6.QtCore import QObject, Signal

from . import placeholder_regmap as rm


def default_channel(ch: int) -> dict:
    cfg = copy.deepcopy(rm.CHANNEL_DEFAULTS)
    if ch == 0:
        cfg["enabled"] = True
    return cfg


def channel_name(ch: int, cfg: dict) -> str:
    return cfg.get("name") or f"Channel {ch}"


def channel_label(ch: int, cfg: dict) -> str:
    """Short label used in selectors and table headers: '0 · Bridge'."""
    return f"{ch} · {channel_name(ch, cfg)}"


def input_text(cfg: dict) -> str:
    return f"{cfg['ainp']} – {cfg['ainn']}"


def gain(cfg: dict) -> int:
    return int(cfg.get("pga_gain", 1)) if cfg.get("sig_path") == "PGA" else 1


def full_scale(cfg: dict) -> float:
    return float(cfg.get("vref", 2.5)) / gain(cfg)


def range_text(cfg: dict) -> str:
    fs = full_scale(cfg)
    val = f"{fs:.4g} V" if fs >= 0.1 else f"{fs * 1e3:.4g} mV"
    g = f" · ×{gain(cfg)}" if cfg.get("sig_path") == "PGA" else ""
    return (f"0 … {val}" if cfg.get("unipolar") else f"±{val}") + g


def filter_text(cfg: dict) -> str:
    r = cfg.get("rate")
    if cfg.get("filter") == "SINC4":
        nominal = {59.8: 60}.get(r, r)
        return f"{nominal:g}Hz SINC4 · {r:g} sps"
    return f"{cfg.get('filter')} · {r:g} sps"


def lsb_volts(cfg: dict) -> float:
    span = full_scale(cfg) if cfg.get("unipolar") else 2 * full_scale(cfg)
    return span / (1 << 24)


class ChannelStore(QObject):
    """Holds the 8 channel configs + the active (edited/selected) channel."""

    changed = Signal(int)  # channel index whose config changed; -1 = all
    activeChanged = Signal(int)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cfg: dict[int, dict] = {ch: default_channel(ch) for ch in range(rm.NUM_CHANNELS)}
        self._active = 0

    # ---- read
    def get(self, ch: int) -> dict:
        return copy.deepcopy(self._cfg[ch])

    def all(self) -> dict[int, dict]:
        return copy.deepcopy(self._cfg)

    def enabled(self) -> list[int]:
        return [ch for ch, c in self._cfg.items() if c.get("enabled")]

    def label(self, ch: int) -> str:
        return channel_label(ch, self._cfg[ch])

    @property
    def active(self) -> int:
        return self._active

    # ---- write
    def update(self, ch: int, **values) -> None:
        self._cfg[ch].update(copy.deepcopy(values))
        self.changed.emit(ch)

    def replace(self, ch: int, cfg: dict) -> None:
        merged = default_channel(ch)
        merged.update(copy.deepcopy(cfg))
        self._cfg[ch] = merged
        self.changed.emit(ch)

    def set_all(self, configs: dict) -> None:
        """configs: {ch: cfg}; keys may be int or str (from JSON). Missing channels reset."""
        norm = {int(k): v for k, v in configs.items()}
        for ch in range(rm.NUM_CHANNELS):
            merged = default_channel(ch)
            merged["enabled"] = False
            merged.update(copy.deepcopy(norm.get(ch, {})))
            self._cfg[ch] = merged
        self.changed.emit(-1)

    def set_active(self, ch: int) -> None:
        if ch != self._active:
            self._active = ch
            self.activeChanged.emit(ch)

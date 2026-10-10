"""Render every tab of the GUI mockup to PNG (headless-friendly).

Usage:
    QT_QPA_PLATFORM=offscreen PYTHONPATH=src \
        python docs/gui/render_screenshots.py [--theme light|dark|both]

Writes docs/gui/screenshots/<tab>.png (light) and docs/gui/screenshots/dark/<tab>.png (dark).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from max11410_evkit.gui.app import build_window  # noqa: E402
from max11410_evkit.gui.theme import ThemeManager  # noqa: E402

OUT = Path(__file__).resolve().parent / "screenshots"
SIZE = (1280, 860)


def slug(name: str) -> str:
    return name.lower().replace(" ", "_")


def pump(app: QApplication, n: int = 5) -> None:
    for _ in range(n):
        app.processEvents()


def populate(win) -> None:
    """Drive the mock backend so every tab shows representative data."""
    win.adc_tab.convert_all_btn.click()
    win.adc_tab.set_active_channel(1)  # show a PGA bridge channel in the editor
    win.other_tab.read_all_btn.click()
    win.other_tab.set_wait(100, 2)
    win.other_tab.waitCalcRequested.emit(100, 2)
    win.sequence_tab.read_all.click()
    win.sequence_tab.currentAddressRequested.emit()
    win.registers_tab.read_all.click()
    win.scope_tab.set_selected_channels([1, 3])
    win.scope_tab.bar.samples.setCurrentIndex(1)  # 512
    win.scope_tab.bar.capture.click()
    win.dmm_tab.set_selected_channels([0, 1, 2, 3])
    for _ in range(140):
        win.dmm_tab.measureRequested.emit(win.dmm_tab.selected())
    win.histogram_tab.set_selected_channel(1)
    win.histogram_tab.bar.samples.setCurrentIndex(4)  # 4096
    win.histogram_tab.bar.capture.click()
    win.fft_tab.set_selected_channel(0)
    win.fft_tab.bar.samples.setCurrentIndex(4)
    win.fft_tab.bar.capture.click()


def render(theme: str, out_dir: Path) -> list[Path]:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    win, _backend = build_window(theme, mock=True)
    win.resize(*SIZE)
    win.show()
    pump(app)
    populate(win)
    pump(app)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for i, (name, _tab) in enumerate(win.tab_items()):
        win.tabs.setCurrentIndex(i)
        pump(app, 10)
        path = out_dir / f"{slug(name)}.png"
        win.grab().save(str(path))
        written.append(path)
    win.close()
    win.deleteLater()
    pump(app)
    return written


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", choices=["light", "dark", "both"], default="both")
    args = ap.parse_args()
    themes = ["light", "dark"] if args.theme == "both" else [args.theme]
    for t in themes:
        paths = render(t, OUT if t == "light" else OUT / "dark")
        for p in paths:
            print(p)
    ThemeManager.instance()


if __name__ == "__main__":
    main()

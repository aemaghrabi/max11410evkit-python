"""Entry point: ``python -m max11410_evkit.gui [--theme light|dark|system] [--no-mock]``."""

from __future__ import annotations

import argparse
import sys

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .mock_backend import MockBackend
from .theme import MODES, ThemeManager


def build_window(theme: str = "system", mock: bool = True) -> tuple[MainWindow, MockBackend | None]:
    """Create the themed main window (and the mock backend unless ``mock=False``)."""
    pg.setConfigOptions(antialias=True)
    ThemeManager.instance().apply(theme)
    win = MainWindow()
    backend = None
    if mock:
        backend = MockBackend(win)
        backend.log.connect(win.log)
        win.set_connected(True, "MOCK")
        win.log("MAX11410 EV Kit software started (mock backend — no hardware)", "info")
        win.log("EV kit hardware connected (simulated)", "ok")
        win.log(
            "Defaults: 60Hz SINC4, 59.8 sps, AINP=AIN2, AINN=GND, Channel 0, Continuous", "info"
        )
    return win, backend


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="max11410-evkit-gui")
    p.add_argument("--theme", choices=MODES, default="system")
    p.add_argument("--no-mock", action="store_true", help="do not attach the mock backend")
    args = p.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("MAX11410 EV Kit")
    app.setOrganizationName("max11410-evkit")
    win, _backend = build_window(args.theme, mock=not args.no_mock)
    win.resize(1280, 860)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

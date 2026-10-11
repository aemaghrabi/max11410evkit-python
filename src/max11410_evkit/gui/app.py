"""Entry point: ``max11410-evkit-gui [--theme light|dark|system] [--offline | --mock]``.

* default    real EV kit over USB (shows Disconnected until one is attached)
* --offline  the simulated (fake) EV kit, no hardware
* --mock     the Phase 5-7 GUI mockup on fabricated data (kept until the project is finished)
"""

from __future__ import annotations

import argparse
import sys

import pyqtgraph as pg
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .theme import MODES, ThemeManager


def build_window(
    theme: str = "system", mock: bool = False, offline: bool = False, **controller_kwargs
):
    """Create the themed main window plus its backend.

    Returns ``(window, backend)``: a ``MockBackend`` with ``mock=True``, otherwise a started
    ``DeviceController`` (simulated board with ``offline=True``).
    """
    pg.setConfigOptions(antialias=True)
    ThemeManager.instance().apply(theme)
    win = MainWindow()
    if mock:
        from .mock_backend import MockBackend

        backend = MockBackend(win)
        backend.log.connect(win.log)
        win.set_board_actions_visible(False)
        win.set_connected(True, "MOCK")
        win.log("MAX11410 EV Kit software started (mock backend — no hardware)", "info")
        win.log("EV kit hardware connected (simulated)", "ok")
        win.log("Demo channel set loaded: 4 channels enabled (Ch 0 = AIN2–GND, 60Hz SINC4)", "info")
        return win, backend

    from .controller import DeviceController

    win.log(
        "MAX11410 EV Kit software started"
        + (" (simulated board)" if offline else " — waiting for an EV kit on USB"),
        "info",
    )
    ctrl = DeviceController(win, simulated=offline, **controller_kwargs)
    ctrl.start()
    return win, ctrl


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="max11410-evkit-gui")
    p.add_argument("--theme", choices=MODES, default="system")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="use the simulated EV kit")
    mode.add_argument("--mock", action="store_true", help="GUI mockup on fabricated data")
    args = p.parse_args(argv)

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("MAX11410 EV Kit")
    app.setOrganizationName("max11410-evkit")
    win, _backend = build_window(args.theme, mock=args.mock, offline=args.offline)
    win.resize(1280, 860)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

"""Entry point.

A ``QApplication`` (not ``QGuiApplication``): QtWidgets is what brings the platform style, so
Breeze draws every control, menu and dialog, and ``QFileDialog`` gets the desktop's own chooser.
Getting those for free is the reason this app is no longer QML.

No virtualenv, no pip: every dependency comes from Portage. ``run.sh`` runs it from source.
"""

from __future__ import annotations

import logging
import signal
import sys
from pathlib import Path

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from hardware_ui import APP_NAME, __version__
from hardware_ui.core import ModuleRegistry

from .app import Controller
from .asyncbridge import AsyncBridge
from .window import MainWindow

ICON_FILE = Path(__file__).parent / "icon" / "hardware-ui.svg"


SHUTDOWN_TIMEOUT = 3.0
"""Long enough to close a device politely, short enough that quitting still feels instant."""


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv
    logging.basicConfig(
        level=logging.DEBUG if "-v" in args else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    # Before QApplication: this becomes the Wayland xdg-toplevel app_id, which is how the
    # compositor finds hardware-ui.desktop.
    QApplication.setDesktopFileName("hardware-ui")

    app = QApplication(args)
    # applicationName stays the id -- it keys QStandardPaths and the desktop entry. Only the
    # display name is the title, and "Hardware" on its own was too generic to identify.
    app.setApplicationName("hardware-ui")
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("hardware-ui")
    # A no-op on Wayland (no protocol for a client to set its own window icon -- the compositor
    # reads the .desktop file), kept for X11. Loaded from the file rather than by theme name:
    # Breeze colours this artwork only at 64px, so a small theme lookup returns monochrome.
    app.setWindowIcon(QIcon(str(ICON_FILE)))

    # Breeze supplies the palette, metrics and every control. Nothing to theme by hand.
    QIcon.setFallbackThemeName("breeze")

    registry = ModuleRegistry.discover()
    log = logging.getLogger(__name__)
    log.info("discovered %d module(s): %s", len(registry), ", ".join(m.id for m in registry))

    bridge = AsyncBridge()
    bridge.start()

    window = MainWindow()
    controller = Controller(registry, bridge, window)
    controller.paint_from_cache()
    window.show()

    def _quit() -> None:
        # Wait for it. `submit` only schedules, and `stop` cancels every pending task -- including
        # the shutdown that was just handed over, so devices were never actually closed on exit.
        # That matters for a YubiKey: the smartcard interface it holds stays claimed until the
        # process dies, and gpg-agent or Kleopatra cannot have it back in the meantime.
        future = bridge.submit(controller.shutdown())
        try:
            future.result(timeout=SHUTDOWN_TIMEOUT)
        except Exception:  # noqa: BLE001 - a device that will not close must not block exit
            log.debug("shutdown did not complete cleanly", exc_info=True)
        bridge.stop()

    app.aboutToQuit.connect(_quit)
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    bridge.spawn(controller.enumerate(), label="initial-enumerate")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())

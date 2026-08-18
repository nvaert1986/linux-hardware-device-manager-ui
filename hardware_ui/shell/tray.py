"""System tray icon: keep the application reachable after its window is closed.

Small on purpose. The menu is Open and Quit, and left-clicking toggles the window, which is what
every tray icon on the desktop does.

**Closing the window stops meaning "quit"** once there is a tray to go back to -- otherwise the
menu's Open would be unreachable, since the application would already have exited. The first time a
window is hidden this way it says so in a notification, because an application that vanishes with no
explanation is indistinguishable from one that crashed.

**If the desktop has no system tray, nothing here changes anything.** `Tray.attach` returns None,
the window keeps quitting the application when closed, and the rest of the shell is untouched. A
tray icon that silently swallows the close button on a desktop that cannot display it would make
the application impossible to quit.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

log = logging.getLogger(__name__)

ICON_FILE = Path(__file__).parent / "icon" / "hardware-ui.svg"

#: The application's own artwork, loaded **from the file** rather than by theme name.
#:
#: This is the same thing ``__main__.py`` does for the window icon, and for the same reason its
#: comment already gives: a theme lookup of this artwork does not survive small sizes. Getting here
#: took two wrong answers, both caused by reaching for theme names at all:
#:
#: * ``preferences-devices-tree`` -- a hierarchy diagram, which this project uses for *docks*.
#: * ``preferences-desktop-peripherals`` -- Breeze's peripherals icon, but it is a pale window with
#:   a stylus drawn for a 32 px settings header. At 22 px in a panel it reads as a blank document.
#:
#: And every *monochrome* Breeze candidate (``device-notifier-symbolic``, ``computer-symbolic``,
#: ``input-mouse``) is dark ink, legible on a light panel and invisible on a dark one unless the
#: desktop substitutes the breeze-dark variant -- which is not something to depend on.
#:
#: The bundled SVG reads on both, is distinctive at 22 px, and is the application's own identity,
#: which is what a tray icon is for. No lookup, no ordering, nothing to get wrong.
HIDDEN_TITLE = "Still running"
HIDDEN_BODY = (
    "The window is closed but the application is still in the system tray. Click the tray icon to "
    "open it again, or use Quit from its menu to exit."
)


def _icon() -> QIcon:
    """The tray icon. Deliberately one line: see the note above ICON_FILE."""
    return QIcon(str(ICON_FILE))


class Tray:
    """A tray icon bound to one window.

    Holds a reference to its own ``QSystemTrayIcon`` and ``QMenu``: Qt does not take ownership of
    either, and a menu that goes out of scope is a menu that does not open.
    """

    def __init__(self, window: QWidget) -> None:
        self._window = window
        self._warned = False
        self._quitting = False

        self._icon = QSystemTrayIcon(_icon(), window)
        self._icon.setToolTip(QApplication.applicationDisplayName() or "hardware-ui")

        self._menu = QMenu()
        self._open = QAction("Open", self._menu)
        self._open.triggered.connect(self.show_window)
        self._quit = QAction("Quit", self._menu)
        self._quit.triggered.connect(self.quit)
        self._menu.addAction(self._open)
        self._menu.addSeparator()
        self._menu.addAction(self._quit)

        self._icon.setContextMenu(self._menu)
        self._icon.activated.connect(self._on_activated)
        self._icon.show()

    # ------------------------------------------------------------------ actions

    def show_window(self) -> None:
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Only a plain click toggles. A right-click is the context menu's, and reacting to it here
        # as well would fight the menu Qt is already opening.
        if reason is not QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self._window.isVisible() and not self._window.isMinimized():
            self._window.hide()
        else:
            self.show_window()

    def quit(self) -> None:
        """Really exit.

        The flag is set *before* asking the application to quit, because Qt 6 implements
        ``QApplication.quit`` by closing every window -- which runs the close handler below. Without
        it, quitting looked exactly like the user closing the window: the same "still running"
        notification appeared, which is what made this visible.
        """
        self._quitting = True
        QApplication.quit()

    def note_hidden(self) -> None:
        """Say that the application is still running -- once, the first time it hides."""
        if self._warned:
            return
        self._warned = True
        if self._icon.supportsMessages():
            self._icon.showMessage(HIDDEN_TITLE, HIDDEN_BODY, _icon())

    # ------------------------------------------------------------------ setup

    @classmethod
    def attach(cls, window: QWidget) -> Tray | None:
        """Install a tray icon and make the window hide instead of quitting. None if no tray.

        The close override is installed here rather than in ``MainWindow`` so that a build with no
        tray keeps its ordinary behaviour: the window owns no knowledge of the tray, and there is no
        branch inside ``closeEvent`` that has to be right in both cases.
        """
        if not QSystemTrayIcon.isSystemTrayAvailable():
            log.info("no system tray on this desktop; the window will quit on close as usual")
            return None

        tray = cls(window)
        # This one line is what keeps the application alive without its window. Verified: with it
        # set, an ordinary accepted close hides the window, the process survives, and `show()`
        # brings the window back with nothing lost.
        QApplication.setQuitOnLastWindowClosed(False)

        original = window.closeEvent

        def closeEvent(event) -> None:  # noqa: N802 - Qt's own spelling
            # **Accepted, never vetoed.** An earlier version called `event.ignore()` here, which
            # also vetoed `QApplication.quit` -- Qt 6 implements quitting by closing every window,
            # and a window that refuses to close cancels the quit. So the tray's own Quit item did
            # not quit: it hid the window and announced that the application was still running.
            #
            # Accepting is enough. `setQuitOnLastWindowClosed(False)` already stops the close from
            # ending the process, and the widget is hidden rather than destroyed, so re-opening it
            # costs nothing and no device state is lost.
            original(event)
            if event.isAccepted() and not tray._quitting:
                tray.note_hidden()

        window.closeEvent = closeEvent  # type: ignore[method-assign]
        return tray


__all__ = ["ICON_FILE", "Tray"]

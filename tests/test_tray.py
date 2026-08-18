"""The system tray icon.

Two things are worth proving rather than assuming, and both are easy to get wrong.

**That the close override actually fires.** ``Tray.attach`` replaces ``closeEvent`` on the
*instance* rather than subclassing. PyQt resolves virtual methods through ordinary Python attribute
lookup, so that works -- but it is exactly the kind of thing that silently does nothing, leaving
the window to quit the application and the tray's Open unreachable.

**That a desktop with no tray is left completely alone.** Swallowing the close button where no tray
can be displayed would make the application impossible to quit.

A headless platform reports no system tray, so availability is forced where the tray path is under
test. The icon and menu are real ``QSystemTrayIcon`` and ``QMenu`` objects either way.
"""

from __future__ import annotations

import pytest
from PyQt6.QtWidgets import QMainWindow, QSystemTrayIcon

from hardware_ui.shell import tray as tray_module
from hardware_ui.shell.tray import Tray


@pytest.fixture
def window(qapp):
    win = QMainWindow()
    win.show()
    yield win
    win.hide()


@pytest.fixture
def with_tray(monkeypatch, qapp):
    """Pretend the desktop has a tray, and restore the close-on-quit default afterwards."""
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    original = qapp.quitOnLastWindowClosed()
    yield
    qapp.setQuitOnLastWindowClosed(original)


def test_an_icon_always_resolves(qapp):
    """Themed first, because a tray renders around 22 px where Breeze's artwork is monochrome --
    which is what a tray wants. The bundled colour SVG is the fallback, so this cannot be null."""
    assert not tray_module._icon().isNull()


def test_the_icon_comes_from_the_bundled_file_not_from_a_theme():
    """Two wrong answers came from reaching for theme names, so this pins the file.

    `preferences-devices-tree` is a hierarchy diagram used here for docks. Breeze's peripherals
    icon is a pale window with a stylus, drawn for a 32 px settings header, and at panel size it
    reads as a blank document. Every monochrome candidate is dark ink and disappears on a dark
    panel. The bundled artwork reads on both and is the application's own identity.
    """
    import inspect

    assert tray_module.ICON_FILE.is_file()
    assert not hasattr(tray_module, "THEME_NAMES"), "no theme lookup: it is what went wrong twice"
    assert "fromTheme" not in inspect.getsource(tray_module._icon)


def test_the_icon_renders_at_panel_size_on_light_and_dark(qapp):
    """Null-checking is not enough -- the peripherals icon was never null, it was just unreadable.
    A tray icon has to produce actual pixels at 22 px."""
    from PyQt6.QtCore import QSize

    pixmap = tray_module._icon().pixmap(QSize(22, 22))
    assert not pixmap.isNull()
    assert pixmap.size().width() > 0

    image = pixmap.toImage()
    opaque = sum(1 for y in range(image.height()) for x in range(image.width())
                 if image.pixelColor(x, y).alpha() > 0)
    # Coloured artwork, so it does not depend on the panel's own colour to be visible.
    assert opaque > 40, f"only {opaque} visible pixels at 22px -- it would read as a blank square"


def test_the_menu_is_open_and_quit(window, with_tray):
    tray = Tray.attach(window)
    assert tray is not None
    labels = [action.text() for action in tray._menu.actions() if action.text()]
    assert labels == ["Open", "Quit"]


def test_closing_the_window_hides_it_instead_of_quitting(window, with_tray, qapp):
    """The point of the whole class. If this does not fire, Open is unreachable because the
    application has already exited."""
    tray = Tray.attach(window)
    assert tray is not None
    assert not qapp.quitOnLastWindowClosed(), "the application must survive its last window"

    window.close()
    assert not window.isVisible()
    # Hidden, not destroyed: re-opening costs nothing and no device state is lost.
    assert window.isHidden()


def test_open_brings_the_window_back(window, with_tray):
    tray = Tray.attach(window)
    window.close()
    assert not window.isVisible()
    tray.show_window()
    assert window.isVisible()


def test_a_plain_click_toggles_and_a_right_click_does_not(window, with_tray):
    """A right-click belongs to the context menu Qt is already opening; reacting to it here as well
    would fight that menu."""
    tray = Tray.attach(window)
    reason = QSystemTrayIcon.ActivationReason

    tray._on_activated(reason.Trigger)
    assert not window.isVisible()
    tray._on_activated(reason.Trigger)
    assert window.isVisible()

    tray._on_activated(reason.Context)
    assert window.isVisible(), "a right-click must not toggle the window"


def test_the_still_running_notice_is_shown_once(window, with_tray):
    """An application that vanishes with no explanation is indistinguishable from one that crashed
    -- but saying so on every close would be nagging."""
    tray = Tray.attach(window)
    assert not tray._warned
    tray.note_hidden()
    assert tray._warned
    tray.note_hidden()          # must not raise, and must not re-warn
    assert tray._warned


def test_the_close_handler_never_vetoes_the_event(window, with_tray):
    """The bug this file now exists to prevent.

    An earlier version called ``event.ignore()`` to keep the application alive. That also vetoed
    ``QApplication.quit``, because Qt 6 implements quitting by closing every window and a window
    that refuses to close cancels the quit -- so the tray's own Quit item did not quit. It hid the
    window and announced that the application was still running, which is exactly what it looked
    like from the outside.

    Accepting is enough on its own: ``setQuitOnLastWindowClosed(False)`` is what keeps the process
    alive, and the widget is hidden rather than destroyed.
    """
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QCloseEvent

    Tray.attach(window)
    event = QCloseEvent()
    assert event.type() is QEvent.Type.Close
    window.closeEvent(event)
    assert event.isAccepted(), "vetoing here also cancels QApplication.quit()"


def test_quitting_does_not_announce_that_the_application_is_still_running(window, with_tray):
    """Qt closes every window on the way out, which runs the close handler. Without the flag,
    quitting produced the same "still running" notice as closing the window -- the symptom that
    exposed the veto."""
    from PyQt6.QtGui import QCloseEvent

    tray = Tray.attach(window)
    tray._quitting = True
    window.closeEvent(QCloseEvent())
    assert not tray._warned, "no 'still running' notice while actually quitting"


def test_quit_sets_the_flag_before_asking_the_application_to_exit(window, with_tray):
    """Order matters: the flag has to be set first, because `quit` triggers the window close that
    reads it."""
    tray = Tray.attach(window)
    assert not tray._quitting
    calls = []

    import unittest.mock
    with unittest.mock.patch("hardware_ui.shell.tray.QApplication.quit",
                             side_effect=lambda: calls.append(tray._quitting)):
        tray.quit()
    assert calls == [True], "the flag must already be set when quit() is called"


def test_a_desktop_without_a_tray_is_left_alone(window, monkeypatch, qapp):
    """No tray, no behaviour change: the window still quits the application when closed. Otherwise
    the application would be impossible to quit."""
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    before = qapp.quitOnLastWindowClosed()

    assert Tray.attach(window) is None
    assert qapp.quitOnLastWindowClosed() is before

    # And closeEvent is untouched, so the window really does close.
    window.close()
    assert not window.isVisible()


def test_the_caption_of_a_diagram_is_not_a_window_of_its_own(qapp):
    """A stray empty window opened on the desktop whenever a diagram page was built.

    `caption_label` called `setVisible(True)` on a widget with no parent, and a parentless widget
    *is* a top-level window — so Qt opened one. The caller reparented it into a layout a moment
    later, which closed it again, so it read as a flicker rather than as a window and survived
    review. The callers already skip the caption when there is none, so there was nothing to hide.
    """
    from hardware_ui.core.diagram import Diagram
    from hardware_ui.shell.diagram import caption_label

    before = set(qapp.topLevelWidgets())
    holder = caption_label(Diagram(image="none.svg", caption="The controller as it faces you."))

    assert holder.parent() is None, "still parentless — the caller adds it to a layout"
    assert not holder.isVisible(), "and must not put itself on screen while it is"
    strays = [w for w in qapp.topLevelWidgets() if w not in before and w.isVisible()]
    assert not strays, f"opened {len(strays)} window(s) nobody asked for"


def test_only_one_thing_can_re_show_a_hidden_window(qapp):
    """The window reappearing on its own was reported from a colleague's machine.

    Whatever the cause, it has to arrive through `Tray.show_window` — nothing else in the shell
    re-shows a hidden top-level window. This pins that, so a future change that adds a second route
    has to notice it is doing so.
    """
    import pathlib

    shell = pathlib.Path("hardware_ui/shell")
    # By file, not by line: a line number pins the formatting rather than the rule.
    offenders = sorted(
        path.name
        for path in shell.glob("*.py")
        if any(
            "showNormal()" in line.split("#", 1)[0] or "showFullScreen()" in line.split("#", 1)[0]
            for line in path.read_text().splitlines()
        )
    )
    assert offenders == ["tray.py"], (
        f"a new way to re-show the window appeared in {offenders}. If intended, it needs the "
        "logging tray.show_window has, or a reappearing-window report is unanswerable."
    )

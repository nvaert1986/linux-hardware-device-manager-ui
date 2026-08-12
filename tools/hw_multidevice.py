#!/usr/bin/env python3
"""Hardware smoke test — needs real monitors, so it is not part of pytest.

Run: QT_QPA_PLATFORM=offscreen python3 tools/hw_multidevice.py [screenshot-dir]

Drive the real app headlessly through the multi-device sequence.

Same startup as hardware_ui.shell.__main__ -- real registry, real AsyncBridge, real Dell module,
real monitors. Actions go through the widgets (sidebar selection, Connect button clicks) rather
than by calling the controller, so this exercises the same path a user does.

Only the two monitors are touched. The headsets are left alone: opening a Sony config channel
powers the headset on, and nothing here needs that.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication

from hardware_ui.core import ModuleRegistry
from hardware_ui.shell.app import Controller
from hardware_ui.shell.asyncbridge import AsyncBridge
from hardware_ui.shell.window import MainWindow

SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

app = QApplication([])
QIcon.setFallbackThemeName("breeze")
registry = ModuleRegistry.discover()
bridge = AsyncBridge()
bridge.start()
window = MainWindow()
window.resize(1000, 620)
controller = Controller(registry, bridge, window)
controller.paint_from_cache()
window.show()
bridge.spawn(controller.enumerate(), label="initial-enumerate")

results: list[tuple[str, bool, str]] = []
DP3 = DP4 = ""


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}", flush=True)


def shot(name: str) -> None:
    window.grab().save(str(SHOTS / f"md-{name}.png"))


def sidebar_rows() -> list[str]:
    lst = window.sidebar._list
    return [lst.item(i).text().replace("\n", " / ") for i in range(lst.count())]


def tabs() -> list[str]:
    return list(window.page.forms())


def click_connect() -> None:
    window.page._connect_button.click()


# --- a tiny step machine: each step is (do, until, then) -------------------------------
steps: list = []
step_i = 0
waited = 0


def advance() -> None:
    global step_i, waited
    if step_i >= len(steps):
        finish()
        return
    do, until, then = steps[step_i]
    if waited == 0 and do is not None:
        do()
    waited += 1
    if until is None or until():
        if then is not None:
            then()
        step_i += 1
        waited = 0
    elif waited > 120:  # 60 s per step
        check(f"step {step_i} timed out", False)
        step_i += 1
        waited = 0
    QTimer.singleShot(500, advance)


def finish() -> None:
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed", flush=True)
    if failed:
        print("FAILED: " + ", ".join(failed), flush=True)
    app.quit()


def find_monitors() -> bool:
    global DP3, DP4
    mons = sorted(
        (d for d in controller._devices if d.module_id == "dell_monitors"),
        key=lambda d: str(d.properties.get("connector", "")),
    )
    if len(mons) < 2:
        return False
    DP3, DP4 = mons[0].uid, mons[1].uid
    return True


def selected(get):
    """Deferred: the uids are not known until enumeration finishes, which is step 0."""
    return lambda: controller._selected is not None and controller._selected.uid == get()


steps = [
    # 0. wait for enumeration to find both panels
    (None, find_monitors, lambda: check(
        "both monitors enumerated", bool(DP3 and DP4), f"{DP3} / {DP4}")),
    # 1. select + connect the first
    (lambda: window.sidebar.select(DP3), selected(lambda: DP3), None),
    (click_connect, lambda: DP3 in controller._open, lambda: (
        check("first monitor connects", controller.connected),
        check("its page has tabs", len(tabs()) == 3, " · ".join(tabs())),
        shot("1-first-connected"),
    )),
    # 2. select + connect the second
    (lambda: window.sidebar.select(DP4), selected(lambda: DP4), None),
    (click_connect, lambda: DP4 in controller._open, lambda: (
        check("second monitor connects", controller.connected),
        check("FIRST MONITOR IS STILL OPEN", DP3 in controller._open,
              f"open: {sorted(controller._open)}"),
        check("both rows show connected",
              sum("connected" in r for r in sidebar_rows()) >= 2,
              " | ".join(r for r in sidebar_rows() if "DELL" in r)),
        shot("2-both-connected"),
    )),
    # 3. go back to the first -- its page must return, not blank
    (lambda: window.sidebar.select(DP3), selected(lambda: DP3), None),
    (None, lambda: True, lambda: (
        check("returning shows it as connected", controller.connected),
        check("its page repopulates, not blank", len(tabs()) == 3, " · ".join(tabs())),
        shot("3-back-to-first"),
    )),
    # 4. disconnect the first; the second must be untouched
    (click_connect, lambda: DP3 not in controller._open, lambda: (
        check("disconnect closes only that one", sorted(controller._open) == [DP4],
              f"open: {sorted(controller._open)}"),
        check("its page clears", len(tabs()) == 0),
        check("its row is no longer connected",
              not any("DP-3" in r and "connected" in r for r in sidebar_rows()),
              " | ".join(r for r in sidebar_rows() if "DELL" in r)),
        shot("4-first-disconnected"),
    )),
    # 5. rescan while the second is still open
    (lambda: controller.rescan(), lambda: controller._idle_status != "", None),
    (None, lambda: True, lambda: (
        check("RESCAN LEAVES THE OPEN ONE OPEN", DP4 in controller._open,
              f"open: {sorted(controller._open)}"),
        check("and it still reads as connected",
              any("DP-4" in r and "connected" in r for r in sidebar_rows()),
              " | ".join(r for r in sidebar_rows() if "DELL" in r)),
        shot("5-after-rescan"),
    )),
    # 6. selecting the still-open one shows its page
    (lambda: window.sidebar.select(DP4), selected(lambda: DP4), None),
    (None, lambda: True, lambda: (
        check("still-open device shows its page after a rescan",
              controller.connected and len(tabs()) == 3, " · ".join(tabs())),
        shot("6-final"),
    )),
]


def _quit() -> None:
    bridge.submit(controller.shutdown())
    bridge.stop()


app.aboutToQuit.connect(_quit)
QTimer.singleShot(1000, advance)
sys.exit(app.exec())

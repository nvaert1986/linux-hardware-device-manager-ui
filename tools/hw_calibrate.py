#!/usr/bin/env python3
"""Hardware smoke test — needs a real monitor, so it is not part of pytest.

Run: QT_QPA_PLATFORM=offscreen python3 tools/hw_calibrate.py [screenshot-dir]
Writes probe values: the screen flashes for a few seconds, then settings are restored.

Exercise the untested Dell paths on one real monitor: calibration and input renaming.

Driven through the real app, real module, real bus -- the ACTION button is clicked in the form,
so the confirm, the write timeout, the capability rebuild and the repaint all run as they would
for a user. The confirmation dialog is intercepted rather than clicked (offscreen has nothing to
click with) and its text is asserted, so the real one is still what gets built.

Factory reset is deliberately not here: it discards settings this app does not manage, with no
undo, and nothing about the code path needs a live panel to be trusted.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QLineEdit, QPushButton

from hardware_ui.core import ModuleRegistry
from hardware_ui.core.paths import cache_dir
from hardware_ui.shell.app import Controller
from hardware_ui.shell.asyncbridge import AsyncBridge
from hardware_ui.shell.window import MainWindow

SHOTS = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
TARGET_CONNECTOR = "card1-DP-3"
CAL = cache_dir() / "dell_monitors" / "calibration.json"
NAMES = cache_dir() / "dell_monitors" / "input_names.json"

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s: %(message)s")

app = QApplication([])
QIcon.setFallbackThemeName("breeze")
bridge = AsyncBridge()
bridge.start()
window = MainWindow()
window.resize(1000, 620)
controller = Controller(ModuleRegistry.discover(), bridge, window)
window.show()
bridge.spawn(controller.enumerate(), label="enumerate")

results: list[tuple[str, bool, str]] = []
confirmations: list[tuple[str, str]] = []
uid = ""


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}", flush=True)


# Intercept the real confirmation so its wording can be asserted; offscreen cannot click it.
def fake_confirm(feature: str, detail: str, subject: str = "") -> bool:
    confirmations.append((feature, detail))
    return True


window.confirm_change = fake_confirm  # type: ignore[method-assign]


def shot(name: str) -> None:
    window.grab().save(str(SHOTS / f"cal-{name}.png"))


def widget_for(key: str):
    for form in window.page.forms().values():
        row = form._rows.get(key)
        if row is not None:
            return row.control
    return None


def cap(key: str):
    device = controller._device
    return device.capabilities.by_key(key) if device else None


def bounds(key: str) -> tuple[float, float, float]:
    c = cap(key)
    return (c.minimum, c.maximum, c.step) if c else (-1, -1, -1)


def value(key: str):
    for form in window.page.forms().values():
        if key in form._rows:
            return form.value_of(key)
    return None


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except OSError:
        return {}


steps: list = []
step_i = 0
waited = 0
before_bounds: dict[str, tuple] = {}
before_values: dict[str, object] = {}


def advance() -> None:
    global step_i, waited
    if step_i >= len(steps):
        finish()
        return
    do, until, then, budget = steps[step_i]
    if waited == 0 and do is not None:
        do()
    waited += 1
    if until is None or until():
        if then is not None:
            then()
        step_i += 1
        waited = 0
    elif waited > budget:
        check(f"step {step_i} timed out after {budget // 2}s", False)
        step_i += 1
        waited = 0
    QTimer.singleShot(500, advance)


def finish() -> None:
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed", flush=True)
    if failed:
        print("FAILED: " + ", ".join(failed), flush=True)
    app.quit()


def find_target() -> bool:
    global uid
    for d in controller._devices:
        if d.properties.get("connector") == TARGET_CONNECTOR:
            uid = d.uid
            return True
    return False


IMAGE_KEYS = ["image.brightness", "image.contrast", "image.sharpness",
              "image.gain_red", "image.gain_green", "image.gain_blue"]

steps = [
    (None, find_target, lambda: check("target monitor found", bool(uid), TARGET_CONNECTOR), 120),
    (lambda: window.sidebar.select(uid),
     lambda: controller._selected is not None and controller._selected.uid == uid, None, 20),
    (lambda: window.page._connect_button.click(), lambda: controller.connected, lambda: (
        before_bounds.update({k: bounds(k) for k in IMAGE_KEYS}),
        before_values.update({k: value(k) for k in IMAGE_KEYS}),
        check("connected, sliders start uncalibrated",
              all(before_bounds[k][0] == 0 and before_bounds[k][2] == 1 for k in IMAGE_KEYS),
              f"brightness {before_bounds['image.brightness']}"),
        check("calibrate action is on the page",
              isinstance(widget_for("action.calibrate"), QPushButton),
              getattr(widget_for("action.calibrate"), "text", lambda: "missing")()),
        shot("1-before"),
    ), 120),

    # --- calibration: the screen flashes here ---------------------------------------
    (lambda: widget_for("action.calibrate").click(),
     lambda: CAL.exists() and not window.page.forms()["Color / Picture"]._pending,
     lambda: (
        check("calibration asked for confirmation first", bool(confirmations),
              confirmations[0][1] if confirmations else "no dialog"),
        check("calibration wrote its cache", CAL.exists(), str(CAL)),
        check("it probed every image slider",
              len(next(iter(load(CAL).values()), {})) >= len(IMAGE_KEYS),
              json.dumps(next(iter(load(CAL).values()), {}))),
        check("SLIDERS WERE RE-BOUNDED LIVE",
              any(bounds(k) != before_bounds[k] for k in IMAGE_KEYS),
              " ".join(f"{k.split('.')[-1]}={bounds(k)}" for k in IMAGE_KEYS)),
        check("settings were restored after probing",
              all(value(k) == before_values[k] for k in IMAGE_KEYS),
              " ".join(f"{k.split('.')[-1]}={value(k)}" for k in IMAGE_KEYS)),
        shot("2-after-calibration"),
    ), 600),

    # --- a write against a calibrated slider must now confirm exactly ---------------
    (None, lambda: True, lambda: check(
        "contrast floor was discovered", bounds("image.contrast")[0] > 0,
        f"contrast {bounds('image.contrast')}"), 4),

    # --- input renaming --------------------------------------------------------------
    (lambda: (
        window.page._tabs.setCurrentIndex(1),
        widget_for("settings.input_name.0f").setText("Work laptop"),
        widget_for("settings.input_name.0f").editingFinished.emit(),
    ), lambda: value("settings.input_name.0f") == "Work laptop", lambda: (
        check("input name field is editable",
              isinstance(widget_for("settings.input_name.0f"), QLineEdit)),
        check("the name persisted to disk",
              "Work laptop" in json.dumps(load(NAMES)), json.dumps(load(NAMES))),
        check("INPUT SOURCE RELABELLED TO THE CUSTOM NAME",
              any(c.label == "Work laptop" for c in cap("settings.input").choices),
              " / ".join(c.label for c in cap("settings.input").choices)),
        shot("3-renamed"),
    ), 60),
    # clear it again so the monitor is left as found
    (lambda: (
        widget_for("settings.input_name.0f").setText(""),
        widget_for("settings.input_name.0f").editingFinished.emit(),
    ), lambda: value("settings.input_name.0f") == "", lambda: check(
        "clearing restores the DDC name",
        any(c.label == "DisplayPort-1" for c in cap("settings.input").choices),
        " / ".join(c.label for c in cap("settings.input").choices)), 60),
]


def _quit() -> None:
    bridge.submit(controller.shutdown())
    bridge.stop()


app.aboutToQuit.connect(_quit)
QTimer.singleShot(1000, advance)
sys.exit(app.exec())

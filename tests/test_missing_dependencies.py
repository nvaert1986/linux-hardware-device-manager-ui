"""A module whose dependency is absent must say which package, not leak an ImportError.

`DependencyMissing` is shown to the user **verbatim**, because it carries install instructions;
every other error gets wrapped in the shell's "switch it on, then Rescan" phrasing. So the type is
not cosmetic — raising the wrong one produces advice that cannot possibly help.

Found by checking rather than assuming, 2026-08-11: the Logitech module leaked
``ImportError: No module named 'yaml'`` on Connect, and a missing ``ddcutil`` came back as a plain
device error and so was reported as a monitor that needed switching on. Both are fixed; this keeps
them fixed, and covers the modules that were already right so a refactor cannot quietly undo them.

Dependencies are blocked at the import hook rather than uninstalled, so the suite runs the same on
a machine that happens to have all of them.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.abc
import shutil
import sys

import pytest

from hardware_ui.core import DependencyMissing, DeviceError
from hardware_ui.core.device import Category, DeviceInfo, Transport


class Blocker(importlib.abc.MetaPathFinder):
    """Make a package unimportable, as if it were not installed."""

    def __init__(self, names: set[str]) -> None:
        self.names = names

    def find_spec(self, name, path=None, target=None):  # noqa: ANN001, ANN201
        top = name.split(".")[0]
        if top in self.names:
            raise ImportError(f"No module named {name!r}", name=top)
        return None


#: Evicted alongside a blocked package, because they import it at *their* import time. Without
#: this, a test that ran earlier and imported the vendored tree while pyudev was present leaves it
#: cached, the block has no effect, and the test passes for the wrong reason.
DEPENDENTS = {
    "pyudev": {"hidapi", "logitech_receiver", "solaar"},
    "yaml": {"hidapi", "logitech_receiver", "solaar"},
}


def without(names: set[str]):
    """Context manager blocking *names*, and evicting anything already imported from them."""
    import contextlib

    @contextlib.contextmanager
    def blocked():
        evict = set(names)
        for name in names:
            evict |= DEPENDENTS.get(name, set())
        saved = {n: m for n, m in sys.modules.items() if n.split(".")[0] in evict}
        for name in saved:
            del sys.modules[name]
        blocker = Blocker(names)
        sys.meta_path.insert(0, blocker)
        try:
            yield
        finally:
            sys.meta_path.remove(blocker)
            sys.modules.update(saved)

    return blocked()


def info(vendor_id: int, transport: Transport = Transport.HID, path: str = "/dev/hidraw5"):
    return DeviceInfo(
        uid="test", name="Test device", transport=transport, category=Category.OTHER,
        vendor_id=vendor_id, path=path, address="00:11:22:33:44:55",
    )


def load(dotted: str):
    module_path, attr = dotted.split(":")
    return getattr(importlib.import_module(module_path), attr)


CASES = [
    pytest.param(
        "hardware_ui.modules.razer_peripherals.device:RazerDevice",
        {"openrazer"}, 0x1532, "OpenRazer", id="razer-without-openrazer",
    ),
    pytest.param(
        "hardware_ui.modules.fido2_security_keys.device:Fido2SecurityKey",
        {"fido2"}, 0x1050, "FIDO2", id="fido2-without-fido2",
    ),
    pytest.param(
        "hardware_ui.modules.logitech_peripherals.device:LogitechDevice",
        {"yaml"}, 0x046D, "pyyaml", id="logitech-without-pyyaml",
    ),
    pytest.param(
        "hardware_ui.modules.logitech_peripherals.device:LogitechDevice",
        {"pyudev"}, 0x046D, "pyudev", id="logitech-without-pyudev",
    ),
    pytest.param(
        "hardware_ui.modules.creative_peripherals.device:CreativeDevice",
        {"usb"}, 0x041E, "pyusb", id="creative-without-pyusb",
    ),
]


@pytest.mark.parametrize(("dotted", "blocked", "vendor_id", "mentions"), CASES)
def test_a_missing_dependency_names_its_package(dotted, blocked, vendor_id, mentions):
    with without(blocked):
        device = load(dotted)(info(vendor_id))
        with pytest.raises(DependencyMissing) as caught:
            asyncio.run(device.connect())
    assert mentions.casefold() in str(caught.value).casefold(), (
        f"the message must name what to install, not just that something failed: {caught.value}"
    )


@pytest.mark.parametrize(("dotted", "blocked", "vendor_id", "mentions"), CASES)
def test_the_failure_is_never_a_bare_import_error(dotted, blocked, vendor_id, mentions):
    """The regression this file exists for. An ImportError reaching the shell reads as a bug in
    this application rather than a package that was never installed."""
    with without(blocked):
        device = load(dotted)(info(vendor_id))
        try:
            asyncio.run(device.connect())
        except DependencyMissing:
            pass
        except ImportError as exc:  # pragma: no cover - the bug this guards
            pytest.fail(f"raw ImportError leaked to the caller: {exc}")
        except DeviceError:
            pytest.fail("reported as a device problem; the device is fine, the package is missing")


@pytest.mark.skipif(shutil.which("ddcutil") is None, reason="ddcutil not installed")
def test_a_missing_ddcutil_is_a_dependency_not_a_dead_monitor(monkeypatch):
    """It used to be a plain device error, so the shell advised switching DDC/CI on -- for a
    monitor that was working fine and a package that was absent."""
    from hardware_ui.modules.dell_monitors.protocol import ddcutil

    monkeypatch.setattr(ddcutil.shutil, "which", lambda name: None)
    from hardware_ui.modules.dell_monitors.device import DellMonitor

    device = DellMonitor(
        info(0x413C, transport=Transport.DISPLAY, path="/sys/class/drm/card1-DP-3")
    )
    with pytest.raises(DependencyMissing, match="ddcutil"):
        asyncio.run(device.connect())


#: Third-party packages each module may import, and nothing else. Every entry here is documented
#: in ``docs/INSTALL.md``; the point of pinning the exact set is that a *new* import shows up as a
#: failure here rather than as a missing row in the install guide, which is how Sony's PyBluez and
#: dbus-python went unlisted until someone asked.
ALLOWED_IMPORTS = {
    "poly_headsets": set(),
    "jabra_headsets": set(),
    "dell_docks": set(),
    "sony_headsets": {"bluetooth", "dbus"},       # both optional; see the guard test below
    "dell_monitors": set(),                       # ddcutil is a binary, not an import
    "razer_peripherals": {"openrazer", "dbus"},   # dbus only to widen an except clause
    "fido2_security_keys": {"fido2"},
    "yubikeys": {"fido2", "ykman", "yubikit"},
    # The vendored Solaar tree, reached by name because it uses absolute imports. In-tree, not
    # packages -- what it needs from outside (pyudev, PyYAML) is listed against those instead.
    "logitech_peripherals": {"hidapi", "logitech_receiver", "solaar"},
}


def third_party_imports(directory) -> set[str]:
    import ast

    known = {"hardware_ui", "PyQt6"}
    found: set[str] = set()
    for path in directory.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
    return {
        n for n in found - known
        if n not in sys.stdlib_module_names and not n.startswith("_")
    }


@pytest.mark.parametrize("module", sorted(ALLOWED_IMPORTS))
def test_a_module_imports_only_what_is_documented(module):
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "hardware_ui" / "modules"
    found = third_party_imports(root / module)
    unexpected = found - ALLOWED_IMPORTS[module]
    assert not unexpected, (
        f"{module} imports {sorted(unexpected)}, which docs/INSTALL.md does not list. "
        "Add a row there and a case here, or remove the import."
    )


def test_sonys_two_optional_imports_are_both_guarded():
    """Sony works without PyBluez and without dbus-python: the first is one of several ways to
    resolve an RFCOMM channel, the second only supplies battery and codec readings. Neither may be
    imported at module scope, or a missing package would take the whole module down."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "hardware_ui" / "modules"
    for path in (root / "sony_headsets").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:                      # module scope only
            names: set[str] = set()
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            assert not (names & {"bluetooth", "dbus"}), (
                f"{path.name} imports an optional package at module scope; "
                "it must be imported inside the function that needs it"
            )

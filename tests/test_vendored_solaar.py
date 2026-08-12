"""The vendored Solaar subset must stay free of a GUI stack.

`hardware_ui/third_party/` exists to avoid pulling GTK3, pygobject and python-xlib in for a library
this Qt application merely calls. That property is not self-evident from reading the tree -- it
holds only because one file was dropped and two call sites patched -- so it is asserted rather than
trusted. A re-sync that quietly reintroduces `diversion.py` would otherwise be found by a user.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

VENDOR = pathlib.Path(__file__).resolve().parent.parent / "hardware_ui" / "third_party"

pytestmark = pytest.mark.skipif(
    not VENDOR.is_dir(), reason="Solaar not vendored yet — run tools/vendor_solaar.py"
)

#: Anything importing these at runtime drags in the stack the vendoring exists to avoid.
FORBIDDEN = {"gi", "Xlib", "psutil", "evdev", "keysyms"}


def runtime_imports(path: pathlib.Path) -> set[str]:
    """Top-level packages imported when the module is *executed*.

    Imports under ``if TYPE_CHECKING`` and inside functions do not count: ``base.py`` names GLib
    for annotations only, which is why it is safe to keep.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    found: set[str] = set()

    def walk(node: ast.AST, executed: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Try)):
                walk(child, executed=False)
                continue
            if isinstance(child, ast.If):
                test = ast.dump(child.test)
                walk(child, executed=executed and "TYPE_CHECKING" not in test)
                continue
            if executed and isinstance(child, ast.Import):
                found.update(a.name.split(".")[0] for a in child.names)
            elif (executed and isinstance(child, ast.ImportFrom)
                    and child.module and child.level == 0):
                found.add(child.module.split(".")[0])
            walk(child, executed)

    walk(tree, executed=True)
    return found


def test_nothing_vendored_imports_a_gui_stack():
    offenders = {
        str(p.relative_to(VENDOR)): sorted(runtime_imports(p) & FORBIDDEN)
        for p in sorted(VENDOR.rglob("*.py"))
        if runtime_imports(p) & FORBIDDEN
    }
    assert not offenders, (
        "vendored code imports a GUI stack at runtime, which is the thing this subset exists to "
        f"avoid: {offenders}"
    )


def test_the_rule_engine_is_not_carried():
    """`diversion.py` is the one file that hard-requires GTK, Xlib, psutil and evdev."""
    assert not (VENDOR / "logitech_receiver" / "diversion.py").exists()


def test_the_falsely_guarded_notification_module_is_not_carried():
    """It catches only ``ValueError``, so a missing pygobject raises ``ImportError`` through it.

    It looks safe and is not, which is exactly the kind of file that survives review and fails on
    a user's machine.
    """
    assert not (VENDOR / "logitech_receiver" / "desktop_notifications.py").exists()


def test_the_patched_call_sites_stay_patched():
    text = (VENDOR / "logitech_receiver" / "settings_templates.py").read_text()
    assert "import diversion" not in text
    assert "desktop_notifications.show" not in text


def test_the_licence_and_attribution_travel_with_the_code():
    """GPL-2.0-or-later: the licence text and the authors have to come along."""
    assert (VENDOR / "LICENSE").is_file()
    assert (VENDOR / "COPYRIGHT").is_file()
    assert "GNU GENERAL PUBLIC LICENSE" in (VENDOR / "LICENSE").read_text()
    # settings_validator.py ships without a header upstream; it is not stripped here. Asserting
    # the exact set means a *newly* headerless file is a failure rather than a silent precedent.
    headerless = {
        p.name for p in (VENDOR / "logitech_receiver").glob("*.py")
        if "GNU General Public License" not in p.read_text(errors="replace")
    }
    assert headerless == {"settings_validator.py"}, (
        f"licence headers changed unexpectedly: {headerless}"
    )


def test_the_mit_subtree_carries_its_own_notice():
    """`hid_parser` is MIT, and the GPL text beside it does not speak for it.

    Solaar bundles these files with an SPDX identifier and no licence text at all, so the notice
    is fetched from `hid_parser`'s own release. Asserted because it is exactly the kind of file
    that disappears in a re-vendor and is never missed until someone downstream checks.
    """
    licence = VENDOR / "hid_parser" / "LICENSE"
    assert licence.is_file(), "hid_parser's MIT notice is missing"
    text = licence.read_text()
    assert "Permission is hereby granted, free of charge" in text
    assert "Copyright" in text, "MIT without the copyright line is not the licence"
    # Every file in the subtree must actually be the MIT code this notice covers.
    for module in (VENDOR / "hid_parser").glob("*.py"):
        assert "SPDX-License-Identifier: MIT" in module.read_text(errors="replace"), module.name


def test_provenance_records_what_was_taken():
    provenance = (VENDOR / "PROVENANCE.md").read_text()
    for expected in ("Upstream:", "sha256:", "Patches applied", "GPL-2.0-or-later", "MIT"):
        assert expected in provenance


def test_the_library_is_actually_usable():
    """A subset that imports but exposes no settings would be a silent failure."""
    import sys

    sys.path.insert(0, str(VENDOR))
    try:
        import logitech_receiver.hidpp20_constants as constants
        import logitech_receiver.settings_templates as templates
    finally:
        sys.path.remove(str(VENDOR))
    assert len(templates.SETTINGS) >= 50
    assert len(list(constants.SupportedFeature)) >= 100

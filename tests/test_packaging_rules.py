"""The shipped udev rules and the install documentation must agree.

Two copies of the same thing drift, and the failure is silent and remote: a user follows the
documentation, gets a rule the application no longer relies on, and reports that their device is
missing. The file is the source of truth; this checks the docs still describe it.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RULES = ROOT / "packaging" / "70-hardware-ui.rules"
INSTALL = ROOT / "docs" / "INSTALL.md"

_RULE = re.compile(r'^\s*(SUBSYSTEM=="[^"]+".*)$', re.M)


def active_rules(text: str) -> set[str]:
    """Rule lines, ignoring comments -- a commented example is documentation, not a rule."""
    return {
        line.strip()
        for line in _RULE.findall(text)
        if not line.lstrip().startswith("#")
    }


def test_the_rules_file_ships_with_the_source():
    """A documented rule nobody can install is a rule that gets retyped wrongly."""
    assert RULES.is_file()


def test_every_shipped_rule_appears_in_the_install_guide():
    shipped = active_rules(RULES.read_text())
    documented = active_rules(INSTALL.read_text())
    assert shipped, "the rules file has no active rules"
    assert shipped <= documented, f"undocumented rules: {sorted(shipped - documented)}"


def test_the_guide_promises_nothing_the_file_does_not_grant():
    shipped = active_rules(RULES.read_text())
    documented = active_rules(INSTALL.read_text())
    assert documented <= shipped, f"documented but not shipped: {sorted(documented - shipped)}"


def test_razer_is_explained_rather_than_ruled():
    """This application never opens a Razer device -- it asks OpenRazer's daemon -- so a rule here
    would be cargo cult. The reason has to be written down, or it looks like an oversight."""
    text = RULES.read_text()
    assert "plugdev" in text and "OpenRazer" in text
    assert not any("046d" in r.lower() or "1532" in r for r in active_rules(text)), (
        "rules are matched on node type, never per vendor"
    )


# --------------------------------------------------------------------------- the bundled icon
#
# The icon is Breeze artwork under LGPL-3.0-or-later -- a different licence from the rest of this
# project, and one whose text has to travel with it. It lives in two places (installed from
# packaging/, loaded from the package at runtime), which is exactly how one copy ends up carrying
# the notice and the other not.

ICON_DIRS = (ROOT / "packaging" / "icons", ROOT / "hardware_ui" / "shell" / "icon")


def test_the_icon_is_the_same_file_in_both_places():
    digests = {hashlib.sha256((d / "hardware-ui.svg").read_bytes()).hexdigest() for d in ICON_DIRS}
    assert len(digests) == 1, "the two copies of the icon have drifted apart"


def test_the_icons_licence_travels_with_it():
    for directory in ICON_DIRS:
        notice = directory / "COPYING-ICONS"
        licence = directory / "LICENSE.LGPL-3"
        assert notice.is_file(), f"{directory.name}: upstream's icon notice is missing"
        assert licence.is_file(), f"{directory.name}: the LGPL-3 text is missing"
        assert "Breeze Icon Theme" in notice.read_text(errors="replace")
        assert "GNU LESSER GENERAL PUBLIC LICENSE" in licence.read_text(errors="replace")
        # Saying LGPL where it is actually GPL would be the whole point of the mistake.
        readme = (directory / "README.md").read_text()
        assert "LGPL-3.0-or-later" in readme

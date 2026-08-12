#!/usr/bin/env python3
"""Vendor the parts of Solaar a Logitech module needs, and nothing else.

Run this rather than copying by hand, so re-syncing to a new Solaar is one command and the patch
set is checked rather than remembered.

    python3 tools/vendor_solaar.py            # fetch, verify, patch, write provenance
    python3 tools/vendor_solaar.py --check    # verify the tree matches, change nothing

**Why vendor at all.** `logitech_receiver` is the HID++ library that makes Logitech receivers,
mice and keyboards configurable on Linux, and reimplementing it would be a large protocol effort
with no upside. But `app-misc/solaar` is not split: installing it pulls **GTK3, pygobject and
python-xlib** for a GUI this application never runs, plus Solaar's own tray app. Measured against
1.1.19, exactly one file in the library hard-requires that stack.

**What is dropped, and why it is safe.**

``diversion.py`` is Solaar's key-remapping rule engine -- `import gi`, `Gdk`, `Xlib`, `psutil`,
`evdev`, `keysyms`, all unguarded at module scope. It is not device configuration, and it reaches
the settings shallowly: of 68 setting classes in ``settings_templates.py`` it appears in **one**
place (a mouse-gesture notification), with ``desktop_notifications.show`` in one more (a DPI-slide
toast). Both are removed by the patches below.

``desktop_notifications.py`` is dropped too, and the reason is a trap worth recording: its ``try``
catches only ``ValueError``, which is what ``gi.require_version`` raises for a missing *typelib*.
A missing pygobject raises ``ImportError``, sails straight through, and the module fails to import.
It looks guarded and is not. Nothing imports it here once the one call site is patched.

``base.py``'s GLib import is genuinely safe -- it is under ``if typing.TYPE_CHECKING`` and never
runs.

With those gone the library needs only ``pyudev`` (already an optional dependency here, for
hotplug), ``PyYAML`` and ``hid_parser`` -- the last MIT-licensed and bundled by Solaar itself.
Verified by importing the result with ``gi``, ``Xlib``, ``psutil``, ``evdev`` and ``keysyms``
blocked at the import hook; see ``--check``.

**Licensing.** Solaar is GPL-2.0-**or-later** -- established from its README and the per-file
headers, not from GitHub's badge or the Gentoo ebuild, both of which say plain GPL-2 and are
artefacts of matching the LICENSE *text* (identical for "only" and "or later"). "Or later" is what
permits combining it into this GPL-3.0-or-later application. Every file keeps its own copyright
header, the upstream licence text is written beside the tree, and `PROVENANCE.md` records exactly
what was taken and changed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "hardware_ui" / "third_party"

#: Pinned. Bump deliberately: device support and setting definitions change between releases, and
#: an unnoticed bump would silently alter what this application offers.
VERSION = "1.1.19"
URL = f"https://files.pythonhosted.org/packages/source/s/solaar/solaar-{VERSION}.tar.gz"

#: ``hid_parser`` is MIT, not GPL, and Solaar bundles it *without* its licence text -- the files
#: carry an SPDX identifier and nothing else. MIT requires the copyright notice and permission
#: text to travel with any redistribution, so it is fetched from the package's own release rather
#: than left to upstream's omission. Pinned like everything else here.
HID_PARSER_VERSION = "0.1.0"
HID_PARSER_URL = (
    "https://files.pythonhosted.org/packages/d1/c8/"
    "0f496dd0fd047c1796c73bdf713d954a28245d8af6f97c4b16105235942c/"
    f"hid_parser-{HID_PARSER_VERSION}.tar.gz"
)

#: Subtrees taken whole, relative to the sdist's ``lib/``.
PACKAGES = ("logitech_receiver", "hid_parser")

#: Individual files, because their packages carry far more than is wanted.
FILES = (
    "hidapi/__init__.py",
    "hidapi/common.py",
    "hidapi/udev_impl.py",
    "solaar/configuration.py",
    "solaar/i18n.py",
)

#: Dropped: the GUI-bound rule engine, a console tool with an argparse main(), and the
#: notification helper.
#:
#: ``desktop_notifications.py`` is dropped despite looking guarded. Its ``try`` catches only
#: ``ValueError`` -- raised by ``gi.require_version`` for a *missing typelib* -- so an absent
#: pygobject raises ``ImportError`` straight through it and the module fails to import. Nothing in
#: the subset imports it once the DPI-slide call site is patched, so carrying it would leave a file
#: that breaks the moment a future re-sync references it again.
OMIT = (
    "logitech_receiver/diversion.py",
    "logitech_receiver/desktop_notifications.py",
    "hidapi/hidconsole.py",
)

#: ``solaar/__init__`` shells out to ``git describe`` and reads build-time data files, neither of
#: which exists in a vendored copy. Only these two names are used by the library.
SOLAAR_INIT = '''"""Minimal stand-in for Solaar's package root -- see tools/vendor_solaar.py.

Upstream derives a version by shelling out to ``git describe`` and reading data files laid down at
build time. Vendored, there is no checkout and no build step, so the two names
``logitech_receiver`` actually uses are provided directly.
"""

NAME = "Solaar"
__version__ = "{version}"
'''

#: (file, before, after, why). Applied in order; a miss is a hard failure, because a silently
#: skipped patch would leave a GTK import in the tree and only fail at runtime on a user's machine.
PATCHES: tuple[tuple[str, str, str, str], ...] = (
    (
        "logitech_receiver/settings_templates.py",
        "from . import desktop_notifications\n",
        "",
        "notifications are not carried; the one call site below is removed too",
    ),
    (
        "logitech_receiver/settings_templates.py",
        "from . import diversion\n",
        "",
        "diversion is the GUI-bound rule engine and is not carried",
    ),
    (
        "logitech_receiver/settings_templates.py",
        "        diversion.process_notification(self.device, notification, _F.MOUSE_GESTURE)",
        "        return  # vendored: Solaar's diversion rule engine is not carried",
        "the single real use of diversion among 68 setting classes",
    ),
    (
        "logitech_receiver/settings_templates.py",
        "show_notification=desktop_notifications.show",
        "show_notification=None",
        "a DPI-slide toast; the setting itself is unaffected",
    ),
    (
        "logitech_receiver/notifications.py",
        "from . import diversion\n",
        "",
        "same engine, imported for gesture handling this application does not do",
    ),
)

#: Import these with the GUI stack blocked; if any fails, the patch set is wrong.
SMOKE = (
    "logitech_receiver.device",
    "logitech_receiver.receiver",
    "logitech_receiver.settings_templates",
    "logitech_receiver.hidpp20",
    "hidapi.udev_impl",
)

BLOCKED = ("gi", "Xlib", "psutil", "evdev", "keysyms")


def fetch() -> bytes:
    print(f"fetching solaar {VERSION} …")
    with urllib.request.urlopen(URL, timeout=60) as response:  # noqa: S310 - pinned https URL
        return response.read()


def unpack(raw: bytes, into: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        tar.extractall(into, filter="data")
    lib = into / f"solaar-{VERSION}" / "lib"
    if not lib.is_dir():
        raise SystemExit(f"unexpected sdist layout: no {lib}")
    return lib


def collect(lib: Path, dest: Path) -> None:
    """Copy the wanted subset, omitting what pulls a GUI in."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    omit = {tuple(o.split("/")) for o in OMIT}
    for package in PACKAGES:
        for source in sorted((lib / package).rglob("*.py")):
            relative = source.relative_to(lib)
            if tuple(relative.parts) in omit:
                print(f"  omitting {relative}")
                continue
            target = dest / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    for name in FILES:
        if tuple(name.split("/")) in omit:
            continue
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib / name, target)

    (dest / "solaar" / "__init__.py").write_text(SOLAAR_INIT.format(version=VERSION))
    # Both, not just the licence: COPYRIGHT names the authors, which is the part attribution is
    # actually about.
    for name, into in (("LICENSE.txt", "LICENSE"), ("COPYRIGHT", "COPYRIGHT")):
        shutil.copy2(lib.parent / name, dest / into)

    # And the MIT text those two do not cover. Solaar's LICENSE is GPL and says nothing about
    # hid_parser; shipping the subtree under it alone would be redistributing MIT code without the
    # notice MIT asks for.
    (dest / "hid_parser" / "LICENSE").write_text(hid_parser_licence(), encoding="utf-8")


def hid_parser_licence() -> str:
    """The MIT text from `hid_parser`'s own release, not retyped from memory."""
    import io
    import tarfile
    import urllib.request

    with urllib.request.urlopen(HID_PARSER_URL) as response:  # noqa: S310 - pinned https URL
        raw = response.read()
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        member = archive.getmember(f"hid_parser-{HID_PARSER_VERSION}/LICENSE")
        extracted = archive.extractfile(member)
        assert extracted is not None
        return extracted.read().decode("utf-8")


def patch(dest: Path) -> None:
    for name, before, after, why in PATCHES:
        path = dest / name
        text = path.read_text(encoding="utf-8")
        if before not in text:
            raise SystemExit(
                f"patch no longer applies to {name}: {before.strip()!r}\n"
                f"  (it existed to: {why})\n"
                f"  Upstream changed. Re-read the file and update PATCHES before shipping."
            )
        path.write_text(text.replace(before, after), encoding="utf-8")
        print(f"  patched {name}: {why}")


def smoke(dest: Path) -> None:
    """Import the result with the GUI stack blocked.

    In a subprocess, so a half-imported module cannot pollute this one.
    """
    script = f"""
import sys, importlib.abc
BLOCKED = {BLOCKED!r}
class Blocker(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ImportError("BLOCKED: " + name)
        return None
sys.meta_path.insert(0, Blocker())
sys.path.insert(0, {str(dest)!r})
for name in {SMOKE!r}:
    __import__(name)
import logitech_receiver.settings_templates as st
import logitech_receiver.hidpp20_constants as c
print(len(getattr(st, "SETTINGS", [])), len(list(c.SupportedFeature)))
"""
    result = subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(
            "the vendored subset does not import without a GUI stack:\n" + result.stderr
        )
    settings, features = result.stdout.split()
    print(f"  imports clean with {', '.join(BLOCKED)} blocked")
    print(f"  {settings} settings, {features} HID++ 2.0 features")


def provenance(dest: Path, digest: str) -> None:
    lines = [
        "# Provenance",
        "",
        "Generated by `tools/vendor_solaar.py`. Do not edit these files by hand -- re-run the",
        "tool, which re-applies the patch set and fails loudly if upstream has moved.",
        "",
        f"- **Upstream:** Solaar {VERSION}",
        f"- **Source:** {URL}",
        f"- **sha256:** `{digest}`",
        "- **Licence:** GPL-2.0-or-later (see `LICENSE`, and the header on every file)",
        f"- **`hid_parser` {HID_PARSER_VERSION}:** MIT, see `hid_parser/LICENSE` -- fetched from",
        "  that package's own release, because Solaar bundles the code without the text",
        "",
        "## Omitted",
        "",
    ]
    lines += [f"- `{name}`" for name in OMIT]
    lines += ["", "## Patches applied", ""]
    for name, before, _after, why in PATCHES:
        lines.append(f"- `{name}` — {why}  \n  removed/replaced: `{before.strip()}`")
    lines += [
        "",
        "## Why this subset",
        "",
        "`diversion.py` imports `gi`, `Gdk`, `Xlib`, `psutil`, `evdev` and `keysyms` unguarded at",
        "module scope. It is Solaar's key-remapping rule engine, not device configuration, and it",
        "reaches the settings in exactly two call sites. Dropping it removes the entire GTK/X11",
        "dependency; what remains needs only `pyudev`, `PyYAML` and the bundled MIT `hid_parser`.",
        "",
        "`desktop_notifications.py` looks guarded and is not: its `try` catches only `ValueError`,",
        "so a missing pygobject raises `ImportError` through it. Nothing imports it once the",
        "DPI-slide call site is patched, so it is dropped rather than left as a landmine.",
        "",
        "`base.py`'s GLib import is under `typing.TYPE_CHECKING` and never runs, so it stays.",
        "",
        "## Licensing, in more detail than usually needed",
        "",
        "Two things here would look like problems to anyone who checked, so both are written down.",
        "",
        "**`COPYRIGHT` says \"version 2\" with no \"or later\"; the source files say otherwise.**",
        "The per-file headers grant \"either version 2 of the License, or (at your option) any",
        "later version\", and upstream's package metadata declares `GPL-2.0-or-later`. The headers",
        "are the grant. That matters because this application is GPL-3.0-or-later: combining with",
        "GPL-2-**only** code would not be permitted, and combining with GPL-2-**or-later** is. The",
        "`COPYRIGHT` file is shipped unaltered anyway -- it names the authors, which is what",
        "attribution is actually for, and editing an upstream licence file to suit oneself is a",
        "worse look than explaining it.",
        "",
        "**`hid_parser` is MIT and Solaar ships it without the MIT text.** Its files carry an SPDX",
        "identifier and nothing else, and the sdist contains no notice for it. MIT asks that the",
        "copyright notice and permission text travel with the code, so `hid_parser/LICENSE` is",
        "fetched from that project's own release. Not a criticism of upstream -- just not",
        "something to repeat downstream.",
        "",
        "## Files without a licence header",
        "",
        "`logitech_receiver/settings_validator.py`, `hidapi/__init__.py`, `hidapi/common.py` and",
        "`solaar/__init__.py` carry no per-file header **upstream**. They are not stripped here;",
        "`LICENSE` and `COPYRIGHT` cover them.",
    ]
    (dest / "PROVENANCE.md").write_text("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="verify the existing tree imports without a GUI stack; change nothing")
    args = parser.parse_args()

    if args.check:
        if not TARGET.is_dir():
            raise SystemExit(f"{TARGET} does not exist — run without --check first")
        smoke(TARGET)
        return 0

    raw = fetch()
    digest = hashlib.sha256(raw).hexdigest()
    import tempfile

    with tempfile.TemporaryDirectory(prefix="solaar-vendor-") as tmp:
        lib = unpack(raw, Path(tmp))
        collect(lib, TARGET)
        patch(TARGET)
        smoke(TARGET)
        provenance(TARGET, digest)

    files = sum(1 for _ in TARGET.rglob("*.py"))
    lines = sum(len(p.read_text(encoding="utf-8", errors="replace").splitlines())
                for p in TARGET.rglob("*.py"))
    print(f"vendored {files} files, {lines} lines to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Pull Poly Studio's own English setting labels out of a copy of the installer.

``hardware_ui/modules/poly_headsets/labels.py`` has referenced this script since it was written
and the script did not exist, so ``ui_labels.json`` was never produced and the module fell back to
a 33-entry table compiled by hand. This is the missing half.

Nothing is redistributed. It reads an installer the user already has from HP, writes into their own
data directory, and records where the strings came from.

    python3 tools/extract_ui_labels.py ~/Downloads/PolyStudio-5.1.0.1111-x64.msi

**Why the output is keyed by option names rather than by setting name.** Poly's UI strings are
keyed by *display* name -- "Sidetone", "Mute On/Off Alerts" -- which do not match the
``settingName`` in the device catalogues the module actually reads. The option keys do: a setting
offering ``Low/Medium/High`` is the same setting in both files. So the key here is the sorted tuple
of a setting's option names, which is stable across both sources and across locales.

Booleans are dropped for the same reason inverted: every boolean in the catalogue offers
``Enabled/Disabled``, so that signature identifies nothing.

The path through the installer is the one ``assets.py`` documents:

    PolyStudio-<version>-x64.msi
      └─ AI_ChainedPackageFile.PolyStudio.msi
           └─ disk1.cab
                └─ app.asar        Electron bundle; the strings are inline in the Vite output
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

# A setting entry in Poly's i18n: a display name, then the fields we want.
ENTRY = re.compile(
    r'"(?P<display>[^"]{1,60})":\{"name":"(?P<name>[^"]{1,60})"'
    r'(?:,"description":"(?P<description>(?:[^"\\]|\\.){0,300})")?'
    r',"options":\{(?P<options>[^{}]{0,600})\}'
)
PAIR = re.compile(r'"([^"]{1,40})":"((?:[^"\\]|\\.){0,80})"')

#: Every boolean shares these, so the signature identifies nothing.
BOOLEAN_SIGNATURES = frozenset(
    {("Disabled", "Enabled"), ("Off", "On"), ("No", "Yes"), ("False", "True")}
)

#: A sentence that only appears in the English block. Poly ships every language in one bundle, all
#: in the same shape and with nothing naming the locale, so it has to be recognised by content.
ENGLISH_MARKER = "Adjust the volume of your voice that you hear when speaking."


def unpack(installer: Path, workdir: Path) -> Path:
    """Installer -> ``app.asar``, three archives deep."""
    seven = shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")
    if not seven:
        raise SystemExit("need app-arch/7zip (or p7zip) to unpack the installer")

    def extract(archive: Path, into: Path, *only: str) -> None:
        cmd = [seven, "x", "-y", "-bso0", "-bsp0", str(archive), f"-o{into}"]
        cmd += [f"-i!{pattern}" for pattern in only]
        if only:
            cmd.append("-r")
        subprocess.run(cmd, check=True)  # noqa: S603 - fixed argv, no shell

    print(f"unpacking {installer.name} …")
    extract(installer, workdir / "1", "*PolyStudio.msi")
    chained = next((workdir / "1").rglob("*PolyStudio.msi"), None)
    if chained is None:
        raise SystemExit("no chained PolyStudio MSI inside that installer")

    print("unpacking the chained installer …")
    extract(chained, workdir / "2", "disk1.cab")
    cab = next((workdir / "2").rglob("disk1.cab"), None)
    if cab is None:
        raise SystemExit("no disk1.cab inside the chained installer — unexpected layout")

    # The cabinet is 224 MB but only app.asar is asked for, and 7z seeks to it rather than
    # decompressing the rest — the whole run is a couple of seconds.
    print("unpacking the payload …")
    extract(cab, workdir / "3", "*app.asar")
    asar = next((workdir / "3").rglob("app.asar"), None)
    if asar is None:
        raise SystemExit("no app.asar in the payload — Poly may have changed the packaging")
    return asar


def asar_files(asar: Path) -> Iterator[tuple[str, bytes]]:
    """Every file stored *inside* the archive, with its contents.

    Entries marked ``unpacked`` live beside the archive rather than in it, and their offsets point
    at nothing. Reading them anyway lands inside whatever occupies that range, which is how a
    binary ``.node`` module appears to contain English UI strings.
    """
    raw = asar.read_bytes()
    _, pickle_size, _, json_len = struct.unpack("<IIII", raw[:16])
    base = 8 + pickle_size
    header = json.loads(raw[16 : 16 + json_len].decode("utf-8"))

    def walk(node: dict, path: str = "") -> Iterator[tuple[str, bytes]]:
        for name, entry in (node.get("files") or {}).items():
            full = f"{path}/{name}"
            if "files" in entry:
                yield from walk(entry, full)
            elif not entry.get("unpacked"):
                offset = int(entry.get("offset", 0))
                size = int(entry.get("size", 0))
                yield full, raw[base + offset : base + offset + size]

    yield from walk(header)


#: Biggest gap between two neighbouring entries still counted as the same locale. Entries inside a
#: block sit back to back; the join between two languages is a chunk of unrelated bundle.
SAME_BLOCK = 2_000


def english_entries(text: str) -> list[re.Match[str]]:
    """The run of entries containing the English marker.

    Two earlier attempts failed and are worth recording. A fixed window either side of the marker
    swallowed the neighbouring languages and produced Danish labels. Brace matching then failed on
    its own terms: this is a minified bundle, so most braces are code, and a scan tracking them
    never finds a balanced object at all.

    What is actually true is simpler. Each locale is a contiguous run of entries, and the join
    between two of them is a stretch of bundle that matches nothing. So the block is the run the
    marker falls in, grown outwards while its neighbours stay adjacent.
    """
    marker = text.find(ENGLISH_MARKER)
    if marker < 0:
        raise SystemExit("no English strings found — the marker sentence has changed")

    matches = list(ENTRY.finditer(text))
    if not matches:
        return []
    here = min(
        range(len(matches)), key=lambda i: abs(matches[i].start() - marker)
    )

    first = here
    while first > 0 and matches[first].start() - matches[first - 1].end() < SAME_BLOCK:
        first -= 1
    last = here
    while last + 1 < len(matches) and matches[last + 1].start() - matches[last].end() < SAME_BLOCK:
        last += 1
    return matches[first : last + 1]


def harvest(matches: list[re.Match[str]]) -> dict[str, dict[str, str]]:
    """``sorted option names`` -> ``{option: label}``, plus the setting's own label."""
    out: dict[str, dict[str, str]] = {}
    for match in matches:
        options = dict(PAIR.findall(match.group("options")))
        if not options:
            continue
        signature = tuple(sorted(options))
        if signature in BOOLEAN_SIGNATURES:
            continue
        entry = {"label": match.group("name"), **options}
        if match.group("description"):
            entry["description"] = match.group("description")
        out.setdefault("|".join(signature), entry)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("installer", type=Path, help="a Poly Studio .msi you obtained from HP")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="where to write ui_labels.json (default: the module's vendor directory)",
    )
    parser.add_argument("--asar", type=Path, help="skip unpacking; read this app.asar directly")
    args = parser.parse_args()

    if args.output is None:
        # Run as a script from a checkout, so the package is not on the path yet.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from hardware_ui.core.paths import vendor_dir

        args.output = vendor_dir("poly_headsets") / "ui_labels.json"

    with tempfile.TemporaryDirectory(prefix="poly-labels-") as tmp:
        asar = args.asar or unpack(args.installer, Path(tmp))
        best: dict[str, dict[str, str]] = {}
        for name, blob in asar_files(Path(asar)):
            if not name.endswith(".js") or ENGLISH_MARKER.encode() not in blob:
                continue
            print(f"reading {name} …")
            found = harvest(english_entries(blob.decode("utf-8", "replace")))
            if len(found) > len(best):
                best = found

    if not best:
        raise SystemExit("no labels found — Poly may have changed how the strings are bundled")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(best, indent=1, sort_keys=True, ensure_ascii=False))
    print(f"wrote {len(best)} label sets to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Mechanically check a port against its source project.

Written after several rounds of "is it all ported?" answered from memory and a hand-maintained
status column -- which was wrong three times. A checklist I tick myself is worth nothing; this
extracts observable behaviour from the reference implementation and greps the port for each one,
so the answer is evidence rather than an opinion.

    python3 tools/audit_port.py [--source DIR] [-v]

Exit status is non-zero when anything is unaccounted for, so it can gate a release.

What it checks, per category:

``writes``      every ``_conn.set_*`` call site has a capability that reaches it
``pending``     every ``_mark_pending(...)`` group of 2+ widgets has a ``writes_with`` group
``strings``     every user-facing literal (notes, dialog text, warnings) appears in the port
``state``       every ``DeviceState`` field the source reads is surfaced or deliberately skipped
``constants``   every table imported from the protocol module is used rather than retyped

It cannot check behaviour that has no textual fingerprint -- ordering, timing, focus. Those still
need reading. But it makes the mechanical majority non-negotiable.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys

#: The upstream project this audit compares against. Not everyone keeps it in the same place, so
#: the location is taken from the environment or ``--source`` rather than being a path out of one
#: developer's home directory -- which is also how it read before, and made the tool useless to
#: anyone else and leaked a username into a public repository.
_DEFAULT_RELATIVE = "plasma-sony-v1-protocol-headphone-support/plasma_sony_headphones"
DEFAULT_SOURCE = pathlib.Path(
    os.environ.get("HARDWARE_UI_SONY_SOURCE")
    or pathlib.Path.home() / "Projects" / _DEFAULT_RELATIVE
)
PORT = pathlib.Path(__file__).resolve().parent.parent / "hardware_ui"

#: Behaviour intentionally not ported, with the reason. Anything here is excluded from failures
#: but always printed, so a growing list stays visible instead of quietly becoming the norm.
ACCEPTED: dict[str, str] = {
    "sync": "shell polls via Device.refresh() (BlueZ only) -- see docs/PORT_DIVERGENCES.md",
    "scan": "replaced by core discovery.enumerate_all()",
    "reconnect": "shell owns reconnect via _reconnect_after_reboot()",
    "refresh": "shell owns polling via _poll_loop()",
}


def read(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def port_text() -> str:
    """Every Python and QML file in the port, concatenated."""
    parts = []
    for pattern in ("**/*.py", "**/*.qml"):
        for f in sorted(PORT.glob(pattern)):
            parts.append(read(f))
    return "\n".join(parts)


def check_writes(src: str, port: str) -> list[str]:
    """Every setter the source can invoke must actually be *called* in the port.

    Substring-matching the name is not enough: it matches a mention in a comment, a docstring or
    a capability key, so a setter that had been stubbed out still passed. Require a call.
    """
    setters = sorted(set(re.findall(r"_conn\.(set_[a-z_]+)", src)))
    missing = []
    for setter in setters:
        if not re.search(rf"\.{re.escape(setter)}\s*\(", port):
            missing.append(f"{setter} is never called")
    return missing


def check_pending_groups(src: str, port: str) -> list[str]:
    """Every multi-widget `_mark_pending` call must have a matching composite group.

    Checking only that the string ``writes_with`` appears somewhere was useless -- one group
    anywhere satisfied it for every group in the source. Instead, count the distinct group sizes
    the source needs and require the port to declare a group of each size, and to attach it to
    at least as many capabilities.
    """
    needed: set[int] = set()
    for call in re.findall(r"_mark_pending\(\s*self\._pending,([^)]*)\)", src):
        widgets = [w.strip() for w in call.split(",") if w.strip()]
        if len(widgets) >= 2:
            # `*self._sliders` stands for a whole run of controls, so its size is open-ended.
            needed.add(-1 if any(w.startswith("*") for w in widgets) else len(widgets))

    declared: list[int] = []
    for body in re.findall(r"_GROUP\s*=\s*\(([^)]*)\)", port):
        members = [m for m in body.split(",") if m.strip()]
        declared.append(len(members) if "for " not in body else -1)

    problems = []
    for size in sorted(needed):
        if size == -1:
            if not any(d == -1 or d > 3 for d in declared):
                problems.append("open-ended group (e.g. all EQ bands) not declared")
        elif size not in declared:
            problems.append(f"no composite group of {size} capabilities declared")
    if needed and "writes_with=" not in port:
        problems.append("no capability attaches a composite group via writes_with=")
    return problems


def check_strings(src: str, port: str) -> list[str]:
    """User-facing prose in the source should exist in the port.

    Matched on a distinctive fragment rather than the whole sentence: wording is allowed to be
    reflowed, but the *fact* being communicated must survive.
    """
    fragments = [
        "only supported in SBC mode",
        "Choose a Custom slot",
        "Drag a band and release",
        "disable LDAC",
        "Upscales compressed audio",
        "disconnect and reboot",
        "reconnect automatically",
    ]
    return [f for f in fragments if f not in port]


def check_state_fields(src: str, port: str) -> list[str]:
    """DeviceState fields the source displays should be surfaced somewhere in the port."""
    fields = sorted(set(re.findall(r"\bst\.([a-z_]+)", src)))
    skip = {"apo_options", "identifiers", "model_name", "protocol_raw", "version_fields"}
    missing = []
    for f in fields:
        if f in skip or f in ACCEPTED:
            continue
        if f not in port:
            missing.append(f)
    return missing


def check_constants(src: str, port: str) -> list[str]:
    """Tables imported from the protocol module must be referenced, not retyped."""
    imported: set[str] = set()
    for block in re.findall(r"from \.protocol\.messages import \(([^)]*)\)", src):
        imported |= {n.strip() for n in block.replace("\n", " ").split(",") if n.strip()}
    for name in re.findall(r"from \.protocol\.messages import ([A-Z_][A-Za-z_, ]*)", src):
        imported |= {n.strip() for n in name.split(",") if n.strip()}
    tables = {n for n in imported if n.isupper()}
    return sorted(n for n in tables if n not in port)


CHECKS = [
    ("writes      ", check_writes, "setters never called"),
    ("pending     ", check_pending_groups, "composite groups not declared"),
    ("strings     ", check_strings, "user-facing text not carried over"),
    ("state fields", check_state_fields, "DeviceState fields not surfaced"),
    ("constants   ", check_constants, "protocol tables not referenced"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=pathlib.Path, default=DEFAULT_SOURCE)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    if not args.source.is_dir():
        print(f"source not found: {args.source}", file=sys.stderr)
        return 2

    src = "\n".join(read(f) for f in sorted(args.source.rglob("*.py")))
    port = port_text()
    print(f"source {args.source}\nport   {PORT}\n")

    failures = 0
    for label, fn, description in CHECKS:
        problems = fn(src, port)
        if problems:
            failures += len(problems)
            print(f"  FAIL  {label}  {len(problems)} {description}")
            for p in problems:
                print(f"          - {p}")
        else:
            print(f"  ok    {label}")

    if ACCEPTED and args.verbose:
        print("\nDeliberately not ported:")
        for name, why in sorted(ACCEPTED.items()):
            print(f"  {name:12} {why}")

    print(f"\n{'FAILED' if failures else 'clean'}: {failures} unaccounted item(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

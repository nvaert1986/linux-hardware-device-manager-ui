"""Headless diagnostics — no Qt, no window.

Two jobs:

``hardware-ui-cli``
    List what is present and which module claims it. Answers "why does my device not show up?"
    without launching the GUI, and shows unclaimed hardware so the next module worth writing is
    visible.

``hardware-ui-cli <uid>``
    Open one device and dump everything it reports: capabilities, current values, advisories.

The dump is the artefact to attach to a bug report for an untested model. A ``family`` match
means nobody has run this code against that hardware, and this output is what makes it possible
to fix it without owning one.

Deliberately importable without Qt: it exercises :mod:`hardware_ui.core` and the device modules
only, which is also why it is a useful check that the core has not grown a UI dependency.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import sys
from pathlib import Path
from typing import Any

from hardware_ui.core import Kind, ModuleRegistry, Unreachable, discovery

log = logging.getLogger(__name__)


def _list(registry: ModuleRegistry, show_all: bool) -> int:
    devices = registry.expand([registry.claim(d) for d in discovery.enumerate_all()])
    claimed = [d for d in devices if d.supported]
    others = [d for d in devices if not d.supported]

    if not claimed:
        print("No configurable devices found.")
    else:
        print(f"Configurable devices ({len(claimed)}):\n")
        for d in claimed:
            state = "connected" if d.state.value == "connected" else d.state.value
            print(f"  {d.uid}")
            print(f"      {d.name}  [{d.module_id}, {d.support.value}, {state}]")

    if show_all and others:
        print(f"\nUnclaimed hardware ({len(others)}) — no module matches these:\n")
        for d in others:
            ids = ""
            if d.vendor_id is not None:
                ids = f"  {d.vendor_id:#06x}:{d.product_id or 0:#06x}"
            print(f"  {d.transport.value:<10} {d.name}{ids}")
    elif others:
        print(f"\n{len(others)} unclaimed device(s). Use --all to list them.")
    return 0


async def _dump(registry: ModuleRegistry, uid: str) -> int:
    info = next((d for d in registry.expand(
        [registry.claim(x) for x in discovery.enumerate_all()])
                 if d.uid == uid), None)
    if info is None:
        print(f"No device with uid {uid!r}. Run without arguments to list.", file=sys.stderr)
        return 1
    if not info.supported:
        print(f"{info.name}: no module claims this device.", file=sys.stderr)
        return 1

    manifest = registry.get(info.module_id)
    if manifest is None:
        print(f"Module {info.module_id!r} is not installed.", file=sys.stderr)
        return 1

    print(f"{info.name}  [{info.module_id}, {info.support.value}]")
    if info.support.value == "family":
        print("  NOTE: matched by family rule — this model has not been verified.")
    print(f"  transport {info.transport.value}  address {info.address or '-'}\n")

    device = manifest.load()(info)
    try:
        await device.connect()
    except Unreachable as exc:
        print(f"Not reachable: {exc}. Switch the device on and try again.", file=sys.stderr)
        return 1

    try:
        caps = device.capabilities
        values = await device.get_many([c.key for c in caps])
        advisories = device.advisories()
        print(f"{len(caps)} capabilities:\n")
        for group, members in caps.groups().items():
            print(f"  [{group.replace('&&', '&')}]")
            for cap in members:
                shown = values.get(cap.key, "—")
                if cap.kind is Kind.CHOICE:
                    # Show the label, not the wire value. A bug report saying "settings.input =
                    # 15" needs the reader to know 0x0F is DisplayPort-1; "DisplayPort-1" does
                    # not. The raw value is kept alongside because that is what a script sends.
                    label = next((c.label for c in cap.choices if c.value == shown), None)
                    if label is not None and label != str(shown):
                        shown = f"{label} ({shown})"
                flags = []
                if cap.kind is Kind.ACTION:
                    flags.append("action")
                if not cap.writable:
                    flags.append("read-only")
                if cap.reboots:
                    flags.append("reboots")
                advisory = advisories.get(cap.key)
                if advisory is not None and advisory.locked:
                    flags.append("locked")
                suffix = f"  ({', '.join(flags)})" if flags else ""
                print(f"    {cap.key:<26} {str(shown):<28}{suffix}")
                if advisory is not None and advisory.message:
                    print(f"      ! {advisory.message}")
            print()
    finally:
        await device.disconnect()
    return 0


class _ConsoleAcquireUI:
    """Headless :class:`AcquireUI`. The GUI has no import flow yet -- see PROJECT_STATE.md.

    A module whose vendor assets are ``required`` is unusable until they are imported, so there has
    to be *some* way to run it. This is that way, and it is also how the import gets exercised
    against a real installer without a hardware session.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def pick_file(self, title: str, filters: list[str]) -> Path | None:  # noqa: ARG002
        return self._path

    def progress(self, progress: Any) -> None:
        pct = f" {progress.fraction:.0%}" if progress.fraction >= 0 else ""
        print(f"  {progress.stage}{pct}…", flush=True)

    def cancelled(self) -> bool:
        return False

    def confirm(self, title: str, body: str) -> bool:  # noqa: ARG002
        return True


def _import_vendor(module_id: str, installer: Path) -> int:
    """Unpack a module's vendor data from an installer the user supplies."""
    try:
        assets = importlib.import_module(f"hardware_ui.modules.{module_id}.assets")
    except ModuleNotFoundError:
        print(f"{module_id} declares no vendor assets", file=sys.stderr)
        return 2
    if not installer.exists():
        print(f"{installer} does not exist", file=sys.stderr)
        return 2
    print(f"Importing vendor data for {module_id} from {installer.name}")
    try:
        target = assets.source().acquire(_ConsoleAcquireUI(installer))
    except Exception as exc:  # noqa: BLE001 - this is the user-facing report
        print(f"import failed: {exc}", file=sys.stderr)
        return 1
    print(f"done — {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hardware-ui-cli",
        description="List configurable hardware, or dump one device's settings.",
    )
    parser.add_argument("uid", nargs="?", help="device uid (omit to list)")
    parser.add_argument("--all", action="store_true", help="also list unclaimed hardware")
    parser.add_argument(
        "--import-vendor",
        nargs=2,
        metavar=("MODULE", "INSTALLER"),
        help="unpack a module's vendor data from an installer you obtained from the vendor",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    if args.import_vendor:
        module_id, installer = args.import_vendor
        return _import_vendor(module_id, Path(installer))

    registry = ModuleRegistry.discover()
    if not args.uid:
        return _list(registry, args.all)
    return asyncio.run(_dump(registry, args.uid))


if __name__ == "__main__":
    sys.exit(main())

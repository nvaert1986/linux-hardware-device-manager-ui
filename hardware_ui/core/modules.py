"""Module manifests, matching, and the enable/disable registry.

The performance rule this file exists to enforce: **a module's Python is never imported until the
user opens one of its devices.** Manifests are TOML, parsed at startup for a few hundred
microseconds each. Matching runs against enumeration data only.

Modules therefore do not implement ``detect()``. If each module could run code to decide whether
its hardware is present, startup time would be set by the least careful module, and one vendor
library with a slow import would tax everyone.
"""

from __future__ import annotations

import dataclasses
import enum
import fnmatch
import logging
import os
import tomllib
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from .device import Category, DeviceInfo, Support, Transport
from .paths import config_dir, data_dir

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "hardware_ui.modules"


class Enablement(enum.StrEnum):
    """Tri-state, because "on" conflates two different intentions.

    A plain boolean cannot distinguish "show it when the hardware is there" from "show it always,
    because I am testing something or the device enumerates oddly".
    """

    AUTO = "auto"
    """Default. Active when a device matches."""

    ALWAYS = "always"
    """Active even with nothing matching -- for development, or hardware that enumerates badly."""

    OFF = "off"
    """Never matched, never imported."""


@dataclass(frozen=True, slots=True)
class MatchRule:
    """One way a manifest claims a device.

    Every field is optional and all present fields must match. Fields are compared against
    :class:`~hardware_ui.core.device.DeviceInfo`, which never involves touching the device.
    """

    transport: Transport | None = None
    vendor_id: int | None = None
    product_id: int | None = None
    name_glob: str = ""
    uuid: str = ""
    properties: tuple[tuple[str, str], ...] = ()
    """Exact, case-insensitive matches against :attr:`DeviceInfo.properties`, compared as strings.

    The strongest signal a transport offers is rarely the name. A display's EDID vendor id is
    ``DEL`` whatever the panel calls itself, and some panels publish no name descriptor at all --
    so ``properties = {edid_vendor = "DEL"}`` claims every Dell, where ``name_glob = "DELL *"``
    would silently miss one.
    """

    support: Support = Support.FAMILY

    def matches(self, info: DeviceInfo) -> bool:
        if self.transport is not None and info.transport is not self.transport:
            return False
        if self.vendor_id is not None and info.vendor_id != self.vendor_id:
            return False
        if self.product_id is not None and info.product_id != self.product_id:
            return False
        if self.name_glob and not fnmatch.fnmatch(info.name.casefold(), self.name_glob.casefold()):
            return False
        if self.uuid and self.uuid.casefold() not in {u.casefold() for u in info.uuids}:
            return False
        for key, want in self.properties:
            if str(info.properties.get(key, "")).casefold() != want.casefold():
                return False
        return any(
            (
                self.transport is not None,
                self.vendor_id is not None,
                self.product_id is not None,
                self.name_glob,
                self.uuid,
                self.properties,
            )
        )


@dataclass(frozen=True, slots=True)
class VendorAssets:
    """Declares that a module needs data it cannot ship itself.

    Two acquisition modes exist, for two different reasons -- see
    :mod:`hardware_ui.core.assets`. ``required = false`` means the module still works for its
    hand-authored verified devices without any import, which is what keeps first run from being a
    dead end.
    """

    provider: str = ""
    """``registry_fetch`` or ``extract_installer``."""

    required: bool = False
    source_page: str = ""
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModuleManifest:
    """A module's declarative half. Cheap to read, safe to read for every installed module."""

    id: str
    name: str
    category: Category
    implementation: str
    """``package.module:ClassName``, imported lazily and only on demand."""

    description: str = ""

    extends: str = ""
    """Id of a module this one specialises.

    A base module handles a whole standard -- every CTAP authenticator, say -- and a specialising
    module adds what one vendor does beyond it. Both match the same hardware, so without this the
    winner would be whichever manifest happened to be read first.

    Two things follow. The device is claimed by the **most specialised** module that matches, so it
    appears **once**; and that module's implementation is expected to subclass the base's, so it
    inherits every capability the standard provides rather than reimplementing them.
    """

    match: tuple[MatchRule, ...] = ()
    vendor_assets: VendorAssets | None = None
    manifest_path: Path | None = None

    expands: bool = False
    """This module can report devices that enumeration cannot see on its own.

    Almost nothing needs it. Discovery reads sysfs and BlueZ, which is why the application starts
    quickly, and a module that wanted to *open* hardware during a scan would undo that.

    The exception is a device reachable only through another one. A Logitech receiver is the case:
    its paired mouse and keyboard have no node of their own unless ``hid-logitech-dj`` binds, and
    for a Bolt receiver no kernel version binds it -- so without this they are invisible, while
    being perfectly configurable through the receiver's own channel.

    An expander must be cheap and must not wake anything: the Logitech one reads the receiver's
    pairing registers, which took ~100 ms and answered even for a keyboard that would not respond
    to a ping. Declaring this makes the registry import ``<module>.children``, and nothing else.
    """

    @classmethod
    def from_toml(cls, path: Path) -> ModuleManifest:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        rules = tuple(
            MatchRule(
                transport=Transport(r["transport"]) if "transport" in r else None,
                vendor_id=_as_int(r.get("vendor_id")),
                product_id=_as_int(r.get("product_id")),
                name_glob=r.get("name_glob", ""),
                uuid=r.get("uuid", ""),
                properties=tuple(
                    sorted((k, str(v)) for k, v in (r.get("properties") or {}).items())
                ),
                support=Support(r.get("status", "family")),
            )
            for r in data.get("match", [])
        )
        va = data.get("vendor_assets")
        return cls(
            id=data["id"],
            name=data["name"],
            category=Category(data.get("category", "other")),
            implementation=data["implementation"],
            description=data.get("description", ""),
            extends=data.get("extends", ""),
            expands=bool(data.get("expands", False)),
            match=rules,
            vendor_assets=(
                VendorAssets(
                    provider=va.get("provider", ""),
                    required=va.get("required", False),
                    source_page=va.get("source_page", ""),
                    config={
                        k: v
                        for k, v in va.items()
                        if k not in {"provider", "required", "source_page"}
                    },
                )
                if va
                else None
            ),
            manifest_path=path,
        )

    def match_for(self, info: DeviceInfo) -> MatchRule | None:
        """The best rule claiming *info*, preferring a verified match over a family one."""
        hits = [r for r in self.match if r.matches(info)]
        if not hits:
            return None
        return next((r for r in hits if r.support is Support.VERIFIED), hits[0])

    def load(self) -> type:
        """Import the implementation. The expensive step, deferred until a device is opened."""
        module_path, _, attr = self.implementation.partition(":")
        import importlib

        return getattr(importlib.import_module(module_path), attr)


def _as_int(value: Any) -> int | None:
    """Accept ``0x0b0e``, ``"0b0e"`` and ``2830`` alike -- manifests are written by humans."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return int(str(value), 16)


class ModuleRegistry:
    """Discovers manifests, applies user enablement, and matches enumerated devices."""

    def __init__(self, manifests: Iterable[ModuleManifest] = ()) -> None:
        self._manifests: dict[str, ModuleManifest] = {m.id: m for m in manifests}
        self._enablement: dict[str, Enablement] = {}
        self._config_path = config_dir() / "modules.toml"
        self._load_enablement()

    @classmethod
    def discover(cls) -> ModuleRegistry:
        """Find every available module by scanning for ``module.toml`` manifests.

        Three sources, in precedence order:

        1. ``hardware_ui/modules/*/module.toml`` -- the in-tree modules. Just iterate the folder;
           dropping a new subdirectory in there is all it takes to add a device family, whether
           running from a checkout or from an installed package.
        2. ``$XDG_DATA_HOME/hardware-ui/modules/*/module.toml`` -- user-supplied modules, so
           someone can add hardware support without touching the installation.
        3. The ``hardware_ui.modules`` entry point group -- for modules shipped as their own
           distribution, which cannot live inside our directory.

        Note what this does *not* do: no Python is imported. Only TOML is read, a few hundred
        microseconds per module. Disabled modules are still scanned so the Modules page can list
        and re-enable them; enablement filters :meth:`active`, which is what matching consults.
        The implementation is imported only when the user opens a matching device.
        """
        found: dict[str, ModuleManifest] = {}

        def scan(root: Path, origin: str) -> None:
            if not root.is_dir():
                return
            for path in sorted(root.glob("*/module.toml")):
                try:
                    manifest = ModuleManifest.from_toml(path)
                except Exception:
                    log.exception("%s: manifest unreadable, skipping", path)
                    continue
                # The id keys config, cached device records and the vendor-asset directory, so a
                # drifting id silently orphans a user's settings. Requiring it to match the
                # directory also keeps ids unique and specific: "sony" would collide the moment
                # Sony monitors arrive, whereas "sony_headsets" cannot.
                if manifest.id != path.parent.name:
                    log.warning(
                        "%s: id %r does not match its directory %r; using the directory name",
                        path, manifest.id, path.parent.name,
                    )
                    manifest = dataclasses.replace(manifest, id=path.parent.name)
                # First source wins, so a user copy cannot silently shadow a built-in of the
                # same id -- and a broken third-party module cannot displace a working one.
                if manifest.id not in found:
                    found[manifest.id] = manifest
                    log.debug("module %s from %s", manifest.id, origin)

        scan(_builtin_modules_dir(), "built-in")
        scan(data_dir() / "modules", "user")
        for extra in filter(None, os.environ.get("HARDWARE_UI_MODULE_PATH", "").split(os.pathsep)):
            scan(Path(extra), "HARDWARE_UI_MODULE_PATH")

        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                manifest = ModuleManifest.from_toml(_manifest_path(ep))
            except Exception:
                log.exception("module %s: manifest unreadable, skipping", ep.name)
                continue
            if manifest.id not in found:
                found[manifest.id] = manifest
                log.debug("module %s from entry point", manifest.id)

        return cls(found.values())

    def __iter__(self) -> Iterator[ModuleManifest]:
        return iter(self._manifests.values())

    def __len__(self) -> int:
        return len(self._manifests)

    def get(self, module_id: str) -> ModuleManifest | None:
        return self._manifests.get(module_id)

    def enablement(self, module_id: str) -> Enablement:
        return self._enablement.get(module_id, Enablement.AUTO)

    def set_enablement(self, module_id: str, value: Enablement) -> None:
        self._enablement[module_id] = value
        self._save_enablement()

    def active(self) -> list[ModuleManifest]:
        """Manifests eligible for matching -- everything not switched off."""
        return [m for m in self._manifests.values() if self.enablement(m.id) is not Enablement.OFF]

    def claim(self, info: DeviceInfo) -> DeviceInfo:
        """Return *info* with ``module_id`` and ``support`` filled in, if a module claims it.

        Where several modules match, **the most specialised one wins** and the device appears
        once. A YubiKey is a CTAP authenticator and is matched by the generic FIDO module, but a
        YubiKey module declaring ``extends = "fido2_security_keys"`` is a better answer for it --
        it inherits everything the standard offers and adds what the vendor does on top.

        Unclaimed devices come back unchanged, with an empty ``module_id``. They are not an error:
        the Modules page can list them behind "show unsupported devices", which is how the next
        module worth writing gets found.
        """
        hits = [
            (manifest, rule)
            for manifest in self.active()
            if (rule := manifest.match_for(info)) is not None
        ]
        if not hits:
            return info
        manifest, rule = max(hits, key=lambda hit: self._specificity(hit[0]))
        return dataclasses.replace(info, module_id=manifest.id, support=rule.support)

    def expand(self, devices: Sequence[DeviceInfo]) -> list[DeviceInfo]:
        """*devices*, plus any a module can reach only through one of them.

        Called after :meth:`claim`, because which module owns a device is what decides who gets
        asked. Returns a new list; the originals come first and keep their order, so a slow or
        broken expander can never cost a device that enumeration already found.

        Every failure is contained. An expander that raises, or takes an unreasonable time, loses
        its children and nothing else -- the receiver itself still appears and is still
        configurable, which is exactly the degradation a user can understand.
        """
        import importlib

        out: list[DeviceInfo] = list(devices)
        seen = {d.uid for d in out}
        for parent in devices:
            manifest = self.get(parent.module_id) if parent.module_id else None
            if manifest is None or not manifest.expands:
                continue
            try:
                children = importlib.import_module(
                    f"hardware_ui.modules.{manifest.id}.children"
                )
                found = children.discover(parent) or []
            except Exception:
                log.exception("%s: could not expand %s", manifest.id, parent.uid)
                continue
            for child in found:
                if child.uid in seen:
                    continue
                seen.add(child.uid)
                # Run the normal rules over it first, so a manifest can name a model it has been
                # tested against -- a child has no product id, so a `name_glob` rule is the only
                # way to say "this one is verified" without saying it about every device that
                # happens to sit behind the same receiver.
                claimed = self.claim(child)
                if not claimed.module_id:
                    # Nothing matched: it still belongs to the module that found it, which is the
                    # only thing that knows how to reach a device nothing enumerated.
                    claimed = dataclasses.replace(
                        child, module_id=manifest.id, support=parent.support
                    )
                out.append(claimed)
        return out

    def _specificity(self, manifest: ModuleManifest) -> tuple[int, int]:
        """How specialised a module is: the length of its ``extends`` chain, then verified-ness.

        Ranking by chain depth means a module three layers down still beats its own base, and a
        module that extends nothing stays where it is. The verified flag breaks ties so a module
        that claims to have met the hardware wins over one that only matches a family.
        """
        depth, seen, current = 0, {manifest.id}, manifest
        while current.extends and current.extends not in seen:
            parent = self._manifests.get(current.extends)
            if parent is None:
                break
            seen.add(parent.id)
            current = parent
            depth += 1
        verified = any(r.support is Support.VERIFIED for r in manifest.match)
        return (depth, int(verified))

    def base_chain(self, module_id: str) -> list[str]:
        """A module and the modules it specialises, nearest first. Cycles are broken."""
        chain: list[str] = []
        seen: set[str] = set()
        current = self._manifests.get(module_id)
        while current is not None and current.id not in seen:
            chain.append(current.id)
            seen.add(current.id)
            current = self._manifests.get(current.extends) if current.extends else None
        return chain

    def _load_enablement(self) -> None:
        try:
            data = tomllib.loads(self._config_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, tomllib.TOMLDecodeError):
            log.exception("modules.toml unreadable; using defaults")
            return
        for module_id, cfg in (data.get("modules") or {}).items():
            try:
                self._enablement[module_id] = Enablement(cfg.get("enabled", "auto"))
            except ValueError:
                log.warning("modules.toml: bad state for %s, using auto", module_id)

    def _save_enablement(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        lines = ["# Written by hardware-ui. Values: auto | always | off\n"]
        for module_id, state in sorted(self._enablement.items()):
            lines.append(f'\n[modules."{module_id}"]\nenabled = "{state.value}"\n')
        tmp = self._config_path.with_suffix(".toml.tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(self._config_path)


def _builtin_modules_dir() -> Path:
    """The directory this package's own modules live in.

    Resolved from the package rather than from ``__file__`` arithmetic, so it works from an
    installed wheel as well as a source checkout.
    """
    from importlib.resources import files

    return Path(str(files("hardware_ui.modules")))


def _manifest_path(ep: Any) -> Path:
    """Resolve an entry point to its ``module.toml``.

    The entry point value is a package path such as ``hardware_ui.modules.sony``; the manifest
    sits beside its ``__init__.py``. Using ``importlib.resources`` keeps this working from a zip
    or a wheel, not just a source checkout.
    """
    from importlib.resources import files

    return Path(str(files(ep.value) / "module.toml"))

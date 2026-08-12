"""Acquiring vendor data that a module may not ship itself.

Two modules need this, for two genuinely different reasons, and the abstraction has to cover both:

``RegistryFetch`` -- Jabra
    ``@gnaudio/jabra-properties-definition`` is ISC and freely redistributable, but the published
    package contains no licence text at all, only a ``"license": "ISC"`` field. ISC requires the
    copyright and permission notice to travel with every copy, and GN Audio published neither, so
    shipping it means authoring someone else's notice for them. Fetched from GN Audio's own
    publication on request instead, which also keeps this tree free of third-party material.

``ExtractInstaller`` -- Poly
    The device catalogue exists only inside Poly Studio, and there is no public data source. The
    user obtains HP's installer from HP, and we unpack their copy on their machine. Nothing
    vendor-owned is ever redistributed. This is the model Debian's ``ttf-mscorefonts-installer``
    and Wine's Mono prompt have used for two decades.

Both deposit a validated payload in ``$XDG_DATA_HOME/hardware-ui/vendor/<module>/`` with a
``.provenance.json`` recording where it came from, and both surface identically in the UI.
"""

from __future__ import annotations

import abc
import enum
import hashlib
import json
import logging
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .paths import ensure, vendor_dir

log = logging.getLogger(__name__)

TIMEOUT = 30


class AssetStatus(enum.StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    STALE = "stale"
    """Present, but produced by a different source version than the module now pins."""


class AssetError(Exception):
    """Acquisition failed in a way worth showing the user verbatim."""


class ToolMissing(AssetError):
    """A required unpacking tool is not installed.

    Carries an actionable message rather than a traceback -- "emerge app-arch/msitools" is a fix,
    ``FileNotFoundError: msiextract`` is not.
    """


@dataclass(slots=True)
class Progress:
    stage: str
    fraction: float = -1.0
    """0.0-1.0, or negative when indeterminate."""


class AcquireUI(Protocol):
    """What an :class:`AssetSource` may ask of the user.

    A protocol rather than a Qt type so the core stays headless and the CLI can implement it with
    prompts and a progress bar.
    """

    def confirm(self, title: str, body: str, source_page: str = "") -> bool:
        """Ask permission before any network access or file read. Never assume consent."""

    def pick_file(self, title: str, patterns: Sequence[str]) -> Path | None:
        """Show a file chooser. Returns None if the user cancelled."""

    def progress(self, progress: Progress) -> None: ...

    def cancelled(self) -> bool:
        """Polled during long unpacks so a 315 MB installer can be abandoned."""


@dataclass(slots=True)
class Provenance:
    """Where a module's assets came from, written beside them.

    Recorded so a format change upstream produces "imported from 5.1.0.1111, expected 5.2 layout"
    instead of a mystery, and so the Modules page can show what is installed.
    """

    module_id: str
    source: str
    source_version: str = ""
    source_sha256: str = ""
    imported: str = ""
    entries: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def write(self, target: Path) -> None:
        (target / ".provenance.json").write_text(
            json.dumps(dataclasses_asdict(self), indent=2) + "\n", encoding="utf-8"
        )

    @staticmethod
    def read(target: Path) -> Provenance | None:
        try:
            data = json.loads((target / ".provenance.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Provenance(**data)


def dataclasses_asdict(obj: Any) -> dict[str, Any]:
    import dataclasses as _dc

    return _dc.asdict(obj)


class AssetSource(abc.ABC):
    """Base for both acquisition modes."""

    def __init__(self, module_id: str, *, source_page: str = "", required: bool = False) -> None:
        self.module_id = module_id
        self.source_page = source_page
        self.required = required

    @property
    def target(self) -> Path:
        return vendor_dir(self.module_id)

    def status(self) -> AssetStatus:
        if not self.target.is_dir() or not any(self.target.iterdir()):
            return AssetStatus.MISSING
        prov = Provenance.read(self.target)
        if prov and self.expected_version() and prov.source_version != self.expected_version():
            return AssetStatus.STALE
        return AssetStatus.PRESENT

    def expected_version(self) -> str:
        return ""

    @abc.abstractmethod
    def acquire(self, ui: AcquireUI) -> Path:
        """Obtain the assets, with the user's explicit consent. Returns :attr:`target`."""


class RegistryFetch(AssetSource):
    """Download a pinned artefact from a public registry, with consent.

    Pinning the version matters: an upstream change must never alter behaviour silently. The size
    cap and the sanity floor exist because "it parsed as JSON" is not the same as "it is the file
    we expect".
    """

    def __init__(
        self,
        module_id: str,
        *,
        url: str,
        version: str,
        member: str = "",
        filename: str = "",
        sha256: str = "",
        max_bytes: int = 8 << 20,
        min_entries: int = 1,
        source_page: str = "",
        required: bool = False,
        consent: str = "",
    ) -> None:
        super().__init__(module_id, source_page=source_page, required=required)
        self.url = url
        self.version = version
        self.member = member
        """Path inside the archive, for tar/zip artefacts. Empty means the URL is the file."""
        self.filename = filename or (Path(member).name if member else Path(url).name)
        self.sha256 = sha256
        self.max_bytes = max_bytes
        self.min_entries = min_entries
        self.consent = consent
        """What the user is agreeing to, in the module's own words.

        A module may have something specific to say -- why the file is not shipped, what it costs
        to decline -- and a generic "fetch this file?" understates it. Empty falls back to the
        wording below, which is right whenever there is nothing extra to explain.
        """

    def expected_version(self) -> str:
        return self.version

    def acquire(self, ui: AcquireUI) -> Path:
        body = self.consent or (
            f"This will fetch {self.filename} ({self.version}) from the vendor's own "
            f"publication.\n\n{self.url}\n\nNothing is uploaded."
        )
        if not ui.confirm(f"Download {self.filename}?", body, self.source_page):
            raise AssetError("cancelled by user")

        ui.progress(Progress("Downloading", -1.0))
        raw = self._download()

        if self.sha256:
            got = hashlib.sha256(raw).hexdigest()
            if got != self.sha256:
                raise AssetError(
                    f"checksum mismatch: expected {self.sha256[:16]}…, got {got[:16]}…"
                )

        ui.progress(Progress("Extracting", -1.0))
        payload = self._member(raw) if self.member else raw

        entries = _count_json_entries(payload)
        if entries < self.min_entries:
            raise AssetError(f"file has {entries} entries, expected at least {self.min_entries}")

        ensure(self.target)
        (self.target / self.filename).write_bytes(payload)
        Provenance(
            module_id=self.module_id,
            source=self.url,
            source_version=self.version,
            source_sha256=hashlib.sha256(raw).hexdigest(),
            imported=_now(),
            entries=entries,
        ).write(self.target)
        log.info("%s: fetched %s (%d entries)", self.module_id, self.filename, entries)
        return self.target

    def _download(self) -> bytes:
        req = urllib.request.Request(self.url, headers={"User-Agent": "hardware-ui"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:  # noqa: S310 - pinned https URL
            raw = resp.read(self.max_bytes + 1)
        if len(raw) > self.max_bytes:
            raise AssetError(f"refusing file larger than {self.max_bytes} bytes")
        return raw

    def _member(self, raw: bytes) -> bytes:
        import io

        if self.url.endswith((".tgz", ".tar.gz")):
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
                fh = tf.extractfile(self.member)
                if fh is None:
                    raise AssetError(f"{self.member} not found in archive")
                return fh.read()
        if self.url.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                return zf.read(self.member)
        raise AssetError(f"cannot extract a member from {self.url}")


class ExtractInstaller(AssetSource):
    """Unpack vendor data out of an installer the user supplies.

    The user downloads the vendor's software from the vendor, points us at the file, and we unpack
    their copy locally. We redistribute nothing.

    Generic on purpose -- more vendors will land in the same position, and adding one should mean
    a manifest entry plus a ``transform`` function, not a new dialog.
    """

    #: Magic bytes, because a file called ``.msi`` need not be one and users rename things.
    MAGIC: dict[bytes, str] = {
        b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1": "msi",  # OLE compound file
        b"PK\x03\x04": "zip",  # .apk, .zip, Electron .asar containers
        b"xar!": "pkg",  # macOS flat package
        b"\x1f\x8b": "gz",
        b"7z\xbc\xaf\x27\x1c": "7z",
        b"MSCF": "cab",  # Microsoft cabinet -- where an MSI's actual payload lives
    }

    def __init__(
        self,
        module_id: str,
        *,
        locate: Sequence[str],
        transform: Callable[[Path, Path], int],
        accepts: Sequence[str] = ("msi", "zip", "pkg", "cab", "dir"),
        source_page: str = "",
        required: bool = False,
        max_depth: int = 3,
    ) -> None:
        super().__init__(module_id, source_page=source_page, required=required)
        self.locate = tuple(locate)
        """Glob patterns searched in the unpacked tree. Never hardcode a full path -- vendors
        move things between releases, and a glob degrades into a clear error instead of a crash."""

        self.transform = transform
        """``(found_root, target) -> entry_count``. Module-supplied. This is where vendor data
        becomes our schema rather than a copy of theirs."""

        self.accepts = tuple(accepts)
        self.max_depth = max_depth
        """Installers nest: the Poly bootstrapper MSI contains a 230 MB chained MSI which contains
        the zip we actually want."""

    def acquire(self, ui: AcquireUI) -> Path:
        chosen = ui.pick_file(
            "Select the vendor installer",
            ["Installer (*.msi *.exe *.pkg *.dmg *.apk *.zip)", "All files (*)"],
        )
        if chosen is None:
            raise AssetError("cancelled by user")
        if not chosen.exists():
            raise AssetError(f"{chosen} does not exist")

        with tempfile.TemporaryDirectory(prefix="hardware-ui-") as tmp:
            work = Path(tmp)
            ui.progress(Progress("Unpacking installer", 0.1))
            root = chosen if chosen.is_dir() else self._unpack(chosen, work / "unpacked", ui, 0)

            ui.progress(Progress("Locating device data", 0.6))
            found = self._locate(root)
            if found is None:
                raise AssetError(
                    f"This installer does not contain the expected data "
                    f"(looked for {', '.join(self.locate)}). It may be a version we do not yet "
                    f"understand -- please report the installer version."
                )

            ui.progress(Progress("Converting", 0.8))
            staging = ensure(work / "staging")
            entries = self.transform(found, staging)
            if entries <= 0:
                raise AssetError("conversion produced no entries")

            ui.progress(Progress("Installing", 0.95))
            if self.target.exists():
                shutil.rmtree(self.target)
            ensure(self.target.parent)
            shutil.move(str(staging), str(self.target))

        Provenance(
            module_id=self.module_id,
            source=chosen.name,
            source_sha256=_sha256_file(chosen),
            imported=_now(),
            entries=entries,
        ).write(self.target)
        log.info("%s: imported %d entries from %s", self.module_id, entries, chosen.name)
        return self.target

    def _unpack(self, archive: Path, dest: Path, ui: AcquireUI, depth: int) -> Path:
        """Unpack *archive* into *dest*, recursing into nested installers."""
        ensure(dest)
        kind = self._sniff(archive)
        if kind == "msi":
            _run_msi(archive, dest)
        elif kind in {"zip", "pkg", "7z", "gz", "cab"}:
            _run_7z(archive, dest)
        else:
            raise AssetError(f"unrecognised installer format for {archive.name}")

        # Installers nest, and not only as MSIs. Poly Studio is a bootstrapper MSI containing a
        # 230 MB chained MSI whose entire payload is a single 224 MB `disk1.cab` -- stopping at
        # the MSI layer finds nothing at all. Recurse into both, sniffing by magic rather than by
        # extension so a renamed file is still handled.
        if depth < self.max_depth:
            for pattern in ("*.msi", "*.cab"):
                for nested in sorted(dest.rglob(pattern)):
                    if self._sniff(nested) in {"msi", "cab"}:
                        if ui.cancelled():
                            raise AssetError("cancelled by user")
                        self._unpack(nested, dest / f"_nested_{nested.stem}", ui, depth + 1)
        return dest

    def _sniff(self, path: Path) -> str:
        with path.open("rb") as fh:
            head = fh.read(8)
        for magic, kind in self.MAGIC.items():
            if head.startswith(magic):
                return kind
        return ""

    def _locate(self, root: Path) -> Path | None:
        """First path matching any :attr:`locate` glob, or None.

        Returns the containing directory for a file match so the transform gets a stable root.
        """
        for pattern in self.locate:
            for hit in sorted(root.rglob(pattern.removeprefix("**/"))):
                return hit if hit.is_dir() else hit.parent
        return None


def safe_extract(zf: zipfile.ZipFile, dest: Path) -> int:
    """Extract *zf* into *dest*, refusing entries that escape it.

    Zip-slip: an archive entry named ``../../.bashrc`` writes outside the destination. We are
    auto-extracting untrusted vendor archives, so every path is resolved and checked before any
    write. This is the one genuine security issue in the import path.
    """
    dest = dest.resolve()
    written = 0
    for member in zf.infolist():
        if member.is_dir():
            continue
        out = (dest / member.filename).resolve()
        if not out.is_relative_to(dest):
            log.warning("refusing archive entry escaping destination: %s", member.filename)
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, out.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        written += 1
    return written


def _run_msi(archive: Path, dest: Path) -> None:
    """Prefer msitools, fall back to 7z. Both are common; neither is guaranteed."""
    if shutil.which("msiextract"):
        cmd = ["msiextract", "--directory", str(dest), str(archive)]
    elif shutil.which("7z"):
        cmd = ["7z", "x", "-y", f"-o{dest}", str(archive)]
    else:
        raise ToolMissing(
            "Unpacking an MSI needs msitools or 7-Zip.\n\n"
            "On Gentoo:  emerge app-arch/msitools\n"
            "or:         emerge app-arch/7zip"
        )
    _run(cmd)


def _run_7z(archive: Path, dest: Path) -> None:
    if not shutil.which("7z"):
        raise ToolMissing("Unpacking this archive needs 7-Zip.\n\nOn Gentoo:  emerge app-arch/7zip")
    _run(["7z", "x", "-y", f"-o{dest}", str(archive)])


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
        raise AssetError(f"{cmd[0]} failed: {' / '.join(tail)}")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _count_json_entries(payload: bytes) -> int:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 0
    if isinstance(data, dict):
        for key in ("properties", "settings", "items", "devices"):
            if isinstance(data.get(key), (list, dict)):
                return len(data[key])
        return len(data)
    return len(data) if isinstance(data, list) else 0


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")

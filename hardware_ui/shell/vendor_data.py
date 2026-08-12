"""Asking for a module's vendor data, when the module cannot work without it.

Some devices only make sense with data the manufacturer ships and nobody may redistribute. A Poly
headset is the case here: the message ids and payload types live in per-device catalogues inside
Poly Studio, so without them the page is a list of unnamed settings, and the module says as much.

``AcquireUI`` was written for this and nothing implemented it, so the only way to get that data in
was ``hardware_ui.cli --import-vendor`` — which a person who has just pressed Connect has no
reason to know exists. This is the missing half.

**Consent, every time.** The protocol's own words: *never assume consent*. Nothing is read from
disk and nothing is fetched until the user has chosen a file, and the dialog says plainly what will
be read and why. The file is one the user already has, from the vendor, on their own terms.

**Nothing is redistributed.** Their copy is unpacked into their own data directory, with
provenance recorded beside it. This project ships none of it.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
    QWidget,
)

from hardware_ui.core.assets import AssetStatus, Progress

log = logging.getLogger(__name__)


class QtAcquireUI:
    """:class:`~hardware_ui.core.assets.AcquireUI`, in dialogs.

    Runs on the Qt thread, which is where the acquisition runs too: unpacking a 224 MB cabinet is
    slow enough to need a progress dialog, and that dialog is the thing keeping the interface
    answering. ``QProgressDialog`` pumps events while it is up, so Cancel stays live.
    """

    def __init__(self, parent: QWidget | None = None, *, title: str = "Vendor data") -> None:
        self._parent = parent
        self._title = title
        self._progress: QProgressDialog | None = None

    def confirm(self, title: str, body: str, source_page: str = "") -> bool:
        text = body
        if source_page:
            text = f"{body}\n\nThe vendor publishes it at:\n{source_page}"
        answer = QMessageBox.question(
            self._parent,
            title,
            text,
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Ok,
        )
        return answer == QMessageBox.StandardButton.Ok

    def pick_file(self, title: str, patterns: Sequence[str]) -> Path | None:
        filters = "All files (*)"
        if patterns:
            filters = f"Installer ({' '.join(patterns)});;All files (*)"
        chosen, _ = QFileDialog.getOpenFileName(self._parent, title, "", filters)
        return Path(chosen) if chosen else None

    def progress(self, progress: Progress) -> None:
        if self._progress is None:
            self._progress = QProgressDialog(progress.stage, "Cancel", 0, 100, self._parent)
            self._progress.setWindowTitle(self._title)
            self._progress.setWindowModality(Qt.WindowModality.WindowModal)
            self._progress.setMinimumDuration(0)
            self._progress.setAutoClose(False)
            self._progress.setAutoReset(False)
        self._progress.setLabelText(progress.stage)
        if progress.fraction < 0:
            # Indeterminate: a busy bar rather than a number, because an unpack that cannot say
            # how far along it is should not pretend.
            self._progress.setRange(0, 0)
        else:
            self._progress.setRange(0, 100)
            self._progress.setValue(int(progress.fraction * 100))
        QApplication.processEvents()

    def cancelled(self) -> bool:
        return self._progress is not None and self._progress.wasCanceled()

    def close(self) -> None:
        if self._progress is not None:
            self._progress.close()
            self._progress = None


def source_for(module_id: str):  # noqa: ANN201 - an AssetSource, imported lazily
    """The module's own asset source, or ``None`` if it declares none.

    Imports ``<module>.assets`` and nothing else. That submodule exists to describe where the data
    comes from, so it must not need the device's dependencies -- a headset module whose protocol
    imports are missing should still be able to tell you what data it wants.
    """
    import importlib

    try:
        assets = importlib.import_module(f"hardware_ui.modules.{module_id}.assets")
    except ModuleNotFoundError:
        return None
    factory = getattr(assets, "source", None)
    return factory() if callable(factory) else None


def ensure_vendor_data(module_id: str, module_name: str, parent: QWidget | None = None) -> bool:
    """Offer to import a module's required data if it is not there yet.

    Returns whether opening the device is worth attempting. Declining is not an error and not a
    failure to report -- the module still opens, and says on its own page what it cannot label.
    """
    source = source_for(module_id)
    if source is None or source.status() is AssetStatus.PRESENT:
        return True

    missing = source.status() is AssetStatus.MISSING
    ui = QtAcquireUI(parent, title=f"{module_name} — vendor data")
    wanted = QMessageBox.question(
        parent,
        f"{module_name} needs data from the manufacturer",
        (
            f"{module_name} reads the settings your device supports from files that ship with the "
            "manufacturer's own software. They cannot be distributed with this application, so "
            "they have to be taken from a copy you already have.\n\n"
            + (
                "Without them the device still opens, but its settings are shown with generated "
                "names instead of the manufacturer's."
                if missing
                else "The data present was produced by a different version than this module "
                "expects, and may not match your device."
            )
            + "\n\nImport it now?"
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if wanted != QMessageBox.StandardButton.Yes:
        log.info("%s: vendor data declined; opening without it", module_id)
        return True

    try:
        target = source.acquire(ui)
    except Exception as exc:  # noqa: BLE001 - shown to the user, who chose the file
        log.exception("%s: vendor import failed", module_id)
        ui.close()
        QMessageBox.warning(
            parent,
            "Import failed",
            f"{exc}\n\nThe device will still open, without the manufacturer's own names.",
        )
        return True
    finally:
        ui.close()

    log.info("%s: vendor data imported to %s", module_id, target)
    QMessageBox.information(
        parent,
        "Imported",
        f"{module_name} now has the manufacturer's own settings names.",
    )
    return True


__all__ = ["QtAcquireUI", "ensure_vendor_data", "source_for"]

"""The Modules page: which device families this installation will look for.

Enabling and disabling has worked since the registry was written -- ``modules.toml``, three states,
persisted -- and until now the only way to reach it was to edit that file by hand. This is the
control that was missing, not the mechanism.

**Three states, not a checkbox.** A boolean conflates two different intentions: "look for this when
the hardware is there" and "show it regardless, because I am testing something or my device
enumerates oddly". ``Enablement`` keeps those apart and so does this page.

**Nothing is imported to draw it.** Every field comes from the manifest, which is TOML the registry
already read at startup. Switching a module off is therefore also the way to stop its Python ever
being loaded -- useful when a module's dependency is broken, which is precisely when you cannot
afford the page that fixes it to import that module.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from hardware_ui.core.modules import Enablement, ModuleManifest, ModuleRegistry

log = logging.getLogger(__name__)

CHOICES: tuple[tuple[Enablement, str, str], ...] = (
    (
        Enablement.AUTO,
        "Automatic",
        "Used when a device it recognises is present. The right answer for almost everything.",
    ),
    (
        Enablement.ALWAYS,
        "Always active",
        "Matched even with nothing plugged in — for testing, or hardware that enumerates oddly.",
    ),
    (
        Enablement.OFF,
        "Off",
        "Never matched and never imported, so its dependencies are never loaded either.",
    ),
)

NOTE = (
    "A module is a device family this application knows how to talk to. Switching one off does not "
    "uninstall anything — the devices simply stop being offered, and the module's Python is never "
    "imported.\n\n"
    "Changes apply on the next scan, which happens automatically when you close this."
)


class ModulesDialog(QDialog):
    """One row per installed module, with its state and what it is for."""

    #: Emitted when at least one module's state changed, so the shell can re-scan.
    changed = pyqtSignal()

    def __init__(self, registry: ModuleRegistry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._registry = registry
        self._dirty = False

        self.setWindowTitle("Modules")
        self.setMinimumSize(620, 520)

        outer = QVBoxLayout(self)
        note = QLabel(NOTE)
        note.setWordWrap(True)
        outer.addWidget(note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        self._rows = QVBoxLayout(body)
        self._rows.setSpacing(2)
        for manifest in sorted(registry, key=lambda m: m.name.casefold()):
            self._rows.addWidget(self._row(manifest))
        self._rows.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        buttons.accepted.connect(self.accept)
        outer.addWidget(buttons)

    # ---------------------------------------------------------------- rows

    def _row(self, manifest: ModuleManifest) -> QWidget:
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(0, 8, 0, 8)
        box.setSpacing(2)

        title = QLabel(f"<b>{manifest.name}</b>  <span>{manifest.id}</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(title)

        if manifest.description:
            description = QLabel(manifest.description)
            description.setWordWrap(True)
            box.addWidget(description)

        detail = _detail(manifest, self._registry)
        if detail:
            line = QLabel(detail)
            line.setWordWrap(True)
            box.addWidget(line)

        combo = QComboBox()
        for state, label, hint in CHOICES:
            combo.addItem(label, state)
            combo.setItemData(combo.count() - 1, hint, Qt.ItemDataRole.ToolTipRole)
        current = combo.findData(self._registry.enablement(manifest.id))
        combo.setCurrentIndex(max(current, 0))
        combo.activated.connect(
            lambda index, mid=manifest.id, c=combo: self._set(mid, c.itemData(index))
        )
        combo.setMaximumWidth(220)
        box.addWidget(combo)

        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setFrameShadow(QFrame.Shadow.Plain)
        box.addWidget(rule)
        return holder

    def _set(self, module_id: str, state: Enablement) -> None:
        if state is None or self._registry.enablement(module_id) is state:
            return
        log.info("module %s -> %s", module_id, state.value)
        self._registry.set_enablement(module_id, state)
        self._dirty = True

    def accept(self) -> None:
        # Re-scan only if something actually changed. Closing a page you merely looked at should
        # not make the device list flicker.
        if self._dirty:
            self.changed.emit()
        super().accept()


def _detail(manifest: ModuleManifest, registry: ModuleRegistry) -> str:
    """The one line worth saying about a module beyond its description.

    What it needs and what it specialises -- the two things that decide whether switching it off
    is a good idea. Deliberately not a rule dump: how a module matches is its own business, and a
    reader deciding whether to disable it does not need the vendor ids.
    """
    parts: list[str] = []

    chain = registry.base_chain(manifest.id)
    if len(chain) > 1:
        parent = registry.get(chain[1])
        parts.append(
            f"Extends {parent.name if parent else chain[1]} — switching this off leaves its "
            "devices working through that, with fewer settings."
        )

    assets = manifest.vendor_assets
    if assets is not None:
        need = "needs" if assets.required else "can use"
        parts.append(
            f"{need.capitalize()} vendor data, imported from the manufacturer's own files."
        )

    return "  ".join(parts)


__all__ = ["ModulesDialog"]

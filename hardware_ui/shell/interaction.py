"""A dialog a module can drive while its operation is still running.

:class:`~hardware_ui.core.interaction.Interaction`, in Qt. The motivating case is Bolt pairing: the
receiver produces a passkey that has to be typed **on the device being paired**, before the pairing
lock closes, so the text has to appear while the write is still in flight.

**The threading is the whole difficulty.** ``Device.set`` is dispatched with ``asyncio.to_thread``,
so a module calls ``message()`` from a worker thread, and Qt widgets may only be touched from the
GUI thread. Every call therefore goes through a signal with a queued connection: the module's
thread emits, the GUI thread draws. Doing it directly works right up until it doesn't, and the
failure is a crash inside Qt with no useful traceback.

**Cancellation is advisory and one-way.** Pressing Cancel sets a flag; the module notices between
steps and stops. Nothing is interrupted mid-write, because a half-finished pairing is worse than
one that takes a few seconds longer to give up.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

log = logging.getLogger(__name__)


class QtInteraction(QObject):
    """Shows one dialog per operation, updated in place."""

    _show = pyqtSignal(str, str)
    _hide = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._parent = parent
        self._dialog: QDialog | None = None
        self._label: QLabel | None = None
        self._cancelled = False
        # Queued explicitly rather than relying on Qt's auto-detection: a module may legitimately
        # call from the GUI thread too, and Auto would then draw re-entrantly from inside a write.
        self._show.connect(self._on_show, Qt.ConnectionType.QueuedConnection)
        self._hide.connect(self._on_hide, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------------ Interaction

    def message(self, title: str, body: str) -> None:
        self._show.emit(title, body)

    def cancelled(self) -> bool:
        return self._cancelled

    def close(self) -> None:
        self._hide.emit()

    # ------------------------------------------------------------------ GUI thread

    @pyqtSlot(str, str)
    def _on_show(self, title: str, body: str) -> None:
        if self._dialog is None:
            self._build()
        assert self._dialog is not None and self._label is not None
        self._dialog.setWindowTitle(title)
        self._label.setText(body)
        # Modeless: the operation is still running and the rest of the window should stay usable.
        # A modal dialog here would also deadlock anything that needed the GUI thread to finish.
        self._dialog.show()
        self._dialog.raise_()

    @pyqtSlot()
    def _on_hide(self) -> None:
        if self._dialog is not None:
            self._dialog.hide()

    def _build(self) -> None:
        dialog = QDialog(self._parent)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        # No close button: the operation owns this window's lifetime, and a dialog the user can
        # dismiss while pairing continues invites exactly the confusion it exists to prevent.
        dialog.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        dialog.setMinimumWidth(420)

        label = QLabel()
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self._on_cancel)

        layout = QVBoxLayout(dialog)
        layout.addWidget(label, 1)
        layout.addWidget(buttons)

        self._dialog, self._label = dialog, label

    def _on_cancel(self) -> None:
        self._cancelled = True
        if self._label is not None:
            # The module decides when to stop, so say what is actually happening rather than
            # vanishing and leaving a pairing scan running invisibly.
            self._label.setText(self._label.text() + "\n\nStopping…")

    def reset(self) -> None:
        """Forget a previous cancellation. Called before each operation that uses this."""
        self._cancelled = False


__all__ = ["QtInteraction"]

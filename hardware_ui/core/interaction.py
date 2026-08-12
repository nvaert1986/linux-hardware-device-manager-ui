"""Telling the user something *while* an operation is still running.

Every other conversation this application has with a user happens before or after the work: a
confirmation, a prompt for a value, a result. Pairing a Logi Bolt device does not fit that shape.
The receiver hands back a passkey that has to be typed **on the device being paired**, while the
pairing lock is open, and the operation only completes once the user has done it. A button that
runs to completion cannot say "type 4816 on the new keyboard now".

So this is a narrow channel, deliberately: a module can replace one line of text and ask whether
the user has cancelled. It cannot ask a question, collect a value, or block — anything that shape
belongs in ``Capability.prompt`` before the action starts, or in its result afterwards.

The precedent is :class:`~hardware_ui.core.assets.AcquireUI`, which lets a vendor-data import drive
a file picker and a progress bar without the core knowing Qt exists. Same idea, same reason: the
module owns the sequence, the shell owns the widgets.

**Threading is the caller's problem, not the module's.** A module calls these from whatever thread
its work runs on -- usually a worker, since ``Device.set`` is dispatched with ``asyncio.to_thread``.
An implementation that touches widgets must marshal to the GUI thread itself.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Interaction(Protocol):
    """What a module may say to the user mid-operation."""

    def message(self, title: str, body: str) -> None:
        """Show *body*, replacing anything shown before for this operation.

        Called repeatedly as an operation moves through its steps. Implementations should update
        in place rather than stacking windows -- pairing shows three messages in sequence and three
        dialogs would be worse than none.
        """
        ...

    def cancelled(self) -> bool:
        """Whether the user has asked to stop.

        Advisory: a module checks it between steps and gives up politely. Nothing is interrupted
        mid-write, because a half-written pairing is worse than a slow one.
        """
        ...

    def close(self) -> None:
        """The operation is over. Called from a ``finally``, so it must tolerate never having
        shown anything."""
        ...


class Silent:
    """The default: says nothing, cancels nothing.

    Used headlessly (``hardware_ui.cli``), in tests, and by any module that never speaks. A module
    can therefore call ``self.interaction.message(...)`` unconditionally without checking for None,
    which is the whole point of having a null implementation rather than an optional attribute.
    """

    def message(self, title: str, body: str) -> None:
        return None

    def cancelled(self) -> bool:
        return False

    def close(self) -> None:
        return None


SILENT = Silent()

__all__ = ["SILENT", "Interaction", "Silent"]

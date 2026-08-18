"""A picture of the device, with capabilities pinned to the parts they control.

Some settings are *about a place on the hardware*. "Left paddle" and "D-pad Up" mean nothing until
you can see which piece of plastic they are; a column of nineteen dropdowns is a list of names, and
the vendor configurators for controllers, mice and keyboards all draw the device instead. So does
this, for the same reason.

It stays inside the rule that modules contribute capabilities rather than UI. A module hands over a
file and a table of fractions -- data, not widgets -- and the shell decides how big the drawing is,
which side each control sits on, where the leader lines go and how they are painted. Nothing here
imports Qt, and a module that supplies a diagram still renders as a plain form wherever the drawing
cannot be shown.

The 8BitDo controller is the motivating case and shows why the anchors are **fractions of the
image** rather than pixels: the panel scales the drawing to whatever room the window has, so any
fixed coordinate would be wrong at every size but one.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Diagram:
    """One drawing and the capabilities it can point at.

    Keyed into a page by :attr:`Capability.section`: a section is already "the rows that belong
    together", which is exactly the set a single view of the hardware can show. A controller needs
    three drawings -- the bumpers are a sliver from the front and the paddles are on the back -- and
    three sections is how that is expressed without inventing a second grouping mechanism.
    """

    image: str
    """Absolute path to the drawing. SVG is expected; the panel scales it to the space it has."""

    anchors: Mapping[str, tuple[float, float]] = field(default_factory=dict)
    """``{capability key: (x, y)}`` as fractions of the image, ``(0, 0)`` top-left.

    A key with no anchor still gets its row -- it is simply drawn without a leader line, below the
    picture. That is the honest outcome for a control the drawing does not show, and it beats both
    hiding the row and pointing a line at nothing.
    """

    caption: str = ""
    """Optional line under the drawing, e.g. which side is being shown."""

    def side_of(self, key: str) -> str:
        """Which column a control belongs in: ``"L"`` or ``"R"``.

        Decided from the anchor rather than declared, so a control cannot end up in the column
        opposite the part it points at -- which is what makes leader lines cross. Anything at or
        right of the midline goes right; a control with no anchor has no side.
        """
        anchor = self.anchors.get(key)
        return "" if anchor is None else ("L" if anchor[0] < 0.5 else "R")


__all__ = ["Diagram"]

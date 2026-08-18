"""Draws a :class:`~hardware_ui.core.diagram.Diagram`: the hardware in the middle, its controls
around it, a leader line from each control to the part it changes.

One widget, used by any module that supplies a drawing. It is deliberately the *only* place that
knows what a diagram looks like -- a module hands over a path and a table of fractions and gets
this, the same way it hands over a :class:`Capability` and gets a combo box. Adding a drawing for a
mouse or a keyboard later needs no code here.

The controls are the form's own widgets, reparented rather than rebuilt. That matters more than it
looks: every value, pending-write, failure and gating rule in :mod:`.form` operates on
``row.control`` and ``row.label``, so a control that moves into a diagram keeps all of it. A second
set of widgets would be a second set of bindings to keep in step, which is exactly the bug class
this application's one-renderer rule exists to avoid.

Ported in spirit from the source project's ``leader.py``, with its measurement bug designed out:
its anchors were fractions typed in by hand against a render they then drifted from, which put
overlapping dropdowns around the D-pad. Here the fractions are read out of the drawing itself.
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import QPoint, QRect, QRectF, Qt
from PyQt6.QtGui import QColor, QPainter, QPen, QPolygon
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from hardware_ui.core.diagram import Diagram

log = logging.getLogger(__name__)

#: Leader lines are drawn in the palette's highlight colour, so they match the user's theme rather
#: than the fixed cornflower blue the source project used -- which disappeared against a blue
#: accent and shouted against an orange one.
LINE_ALPHA = 200

#: Room reserved for one column of controls. Wide enough for a combo box and its label side by
#: side; the drawing gets whatever is left.
COLUMN_WIDTH = 232

#: Distance the line runs straight out from the anchor before turning towards its control. Without
#: it every line would meet the drawing at a different angle and read as a starburst.
ELBOW = 26

MARGIN = 12
ROW_GAP = 6

#: Below this the drawing is a smudge and the leader lines meet it at absurd angles, so the panel
#: asks for at least this much. It is a floor, not a size: inside its sub-tab the panel expands to
#: whatever the window gives it and the drawing grows with it.
MIN_IMAGE = 260

#: And a ceiling on what the panel *asks* for, so a tall drawing does not open a window that will
#: not fit on a laptop screen. It can still grow past this when the user makes the window bigger.
MAX_IMAGE = 520


class DiagramPanel(QWidget):
    """A drawing with controls arranged around it.

    Controls are added with :meth:`add`; the panel positions them on the side its anchor falls,
    ordered top to bottom by the anchor's height so that no two leader lines cross.
    """

    def __init__(self, diagram: Diagram, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._diagram = diagram
        self._renderer = _renderer(diagram.image)
        self._items: list[tuple[QFrame, tuple[float, float], str]] = []
        self._image = QRect()
        # Expanding in both directions: the panel lives in a sub-tab of its own, so the drawing
        # should take the height the window has rather than a height derived from how many rows
        # happen to point at it. `minimumSizeHint` still guarantees the floor.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    @property
    def diagram(self) -> Diagram:
        return self._diagram

    @property
    def usable(self) -> bool:
        """Whether this panel can actually draw. False means the caller should use a plain form.

        Checked by the caller rather than handled here, because a panel that silently renders an
        empty rectangle is worse than no panel: the controls would still be laid out around a
        drawing that is not there.
        """
        return self._renderer is not None

    # ------------------------------------------------------------------ building

    def add(self, key: str, label: QLabel, control: QWidget) -> bool:
        """Take over one row's widgets. Returns False if the drawing cannot point at this key.

        The caller keeps such a row in its ordinary form, which is the honest outcome for a control
        this view does not show.
        """
        anchor = self._diagram.anchors.get(key)
        side = self._diagram.side_of(key)
        if anchor is None or not side:
            return False

        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        box = QHBoxLayout(frame)
        box.setContentsMargins(8, 4, 8, 4)
        box.setSpacing(6)
        # The label leans towards the drawing on both sides, so the eye runs label -> line -> part
        # in one direction instead of doubling back across the control.
        if side == "L":
            box.addWidget(control)
            box.addWidget(label, 1)
            label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        else:
            box.addWidget(label, 1)
            box.addWidget(control)
            label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        frame.setFixedWidth(COLUMN_WIDTH - 2 * MARGIN)
        frame.show()
        self._items.append((frame, anchor, side))
        return True

    def _column(self, side: str) -> list[tuple[QFrame, tuple[float, float], str]]:
        """One side's items, ordered by how far down the drawing they point.

        Sorting by the anchor rather than by declaration order is what keeps the lines from
        crossing: two controls listed in one order and anchored in the other would otherwise draw
        an X across the picture.
        """
        return sorted((it for it in self._items if it[2] == side), key=lambda it: it[1][1])

    # ------------------------------------------------------------------ geometry

    def _row_height(self) -> int:
        return max((f.sizeHint().height() for f, _, _ in self._items), default=36)

    def minimumSizeHint(self):  # noqa: N802 - Qt naming
        """Tall enough for both columns of controls, and for the drawing not to be a smudge."""
        rows = max(len(self._column("L")), len(self._column("R")), 1)
        height = max(rows * (self._row_height() + ROW_GAP) + 2 * MARGIN, MIN_IMAGE + 2 * MARGIN)
        return QRect(0, 0, 2 * COLUMN_WIDTH + MIN_IMAGE, height).size()

    def sizeHint(self):  # noqa: N802 - Qt naming
        """Ask for enough height to show the drawing at the width it will probably get.

        Without this the panel opens at its minimum and a wide, short drawing -- the top edge of a
        controller is nearly three to one -- sits in a letterbox with most of the tab empty. Derived
        from the drawing's own proportions so each view asks for what it actually needs.
        """
        floor = self.minimumSizeHint()
        if self._renderer is None:
            return floor
        size = self._renderer.defaultSize()
        wanted = int(MIN_IMAGE * 2.2 * size.height() / size.width()) + 2 * MARGIN
        return QRect(0, 0, floor.width(), max(floor.height(), min(wanted, MAX_IMAGE))).size()

    def resizeEvent(self, event) -> None:  # noqa: N802,ANN001 - Qt signature
        super().resizeEvent(event)
        self._relayout()

    def _relayout(self) -> None:
        width, height = self.width(), self.height()
        available = QRect(
            COLUMN_WIDTH, MARGIN, max(width - 2 * COLUMN_WIDTH, MIN_IMAGE), height - 2 * MARGIN
        )
        self._image = _fit(self._renderer, available) if self._renderer else QRect()

        for side in ("L", "R"):
            column = self._column(side)
            if not column:
                continue
            x = MARGIN if side == "L" else width - column[0][0].width() - MARGIN
            for frame, y in zip(column, self._place(column, height), strict=True):
                frame[0].move(x, y)

    def _place(self, column, height: int) -> list[int]:
        """Where each control in one column sits, top to bottom.

        **Beside the part it points at**, not spread evenly down the panel. Spreading was the first
        rule and it is wrong whenever a view has few controls: the top edge of a controller has two
        per side, which went to the very top and the very bottom of the tab and drew two leader
        lines the height of the window to reach two pads sitting together in the middle.

        So each control starts at its anchor's own height and is then pushed just far enough to
        stop overlapping its neighbour -- one downward sweep, then an upward one for anything that
        ran off the bottom. Standard label placement, and it degrades into the old even spread by
        itself when a column is full, because then every control is being pushed anyway.
        """
        image = self._image if not self._image.isEmpty() else QRect(0, MARGIN, 1, height)
        rows = [f.height() for f, _, _ in column]
        wanted = [
            int(image.y() + anchor[1] * image.height() - h / 2)
            for (_, anchor, _), h in zip(column, rows, strict=True)
        ]

        placed: list[int] = []
        floor_ = MARGIN
        for y, h in zip(wanted, rows, strict=True):
            y = max(y, floor_)
            placed.append(y)
            floor_ = y + h + ROW_GAP

        # Anything pushed past the bottom comes back up, taking its neighbours with it.
        ceiling = height - MARGIN
        for index in range(len(placed) - 1, -1, -1):
            if placed[index] + rows[index] > ceiling:
                placed[index] = ceiling - rows[index]
            ceiling = placed[index] - ROW_GAP
        return [max(y, MARGIN) for y in placed]

    # ------------------------------------------------------------------ painting

    def paintEvent(self, event) -> None:  # noqa: N802,ANN001 - Qt signature
        super().paintEvent(event)
        if self._renderer is None or self._image.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._renderer.render(painter, QRectF(self._image))

        colour = QColor(self.palette().highlight().color())
        colour.setAlpha(LINE_ALPHA)
        pen = QPen(colour, 1.6)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        for frame, (ax, ay), side in self._items:
            target = QPoint(
                self._image.x() + int(ax * self._image.width()),
                self._image.y() + int(ay * self._image.height()),
            )
            geometry = frame.geometry()
            start = QPoint(
                geometry.right() if side == "L" else geometry.left(), geometry.center().y()
            )
            elbow = target.x() - ELBOW if side == "L" else target.x() + ELBOW
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(pen)
            painter.drawPolyline(
                QPolygon([start, QPoint(elbow, start.y()), QPoint(elbow, target.y()), target])
            )
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawEllipse(target, 3, 3)
        painter.end()


def _renderer(path: str):
    """A validated SVG renderer, or None.

    Every failure is the same answer -- no drawing, plain form -- but they are logged differently
    on purpose. A missing QtSvg is somebody's installation; invalid SVG is a file we shipped, and
    XML forbidding a double hyphen inside a comment is how that happened twice already.
    """
    try:
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError:
        log.debug("PyQt6.QtSvg is not installed; diagrams render as plain forms")
        return None
    renderer = QSvgRenderer(path)
    if not renderer.isValid():
        log.warning("%s is not valid SVG; rendering that section as a plain form", path)
        return None
    if renderer.defaultSize().width() <= 0 or renderer.defaultSize().height() <= 0:
        log.warning("%s has no intrinsic size", path)
        return None
    return renderer


def _fit(renderer, area: QRect) -> QRect:
    """The largest rectangle inside *area* with the drawing's aspect ratio, centred."""
    size = renderer.defaultSize()
    scale = min(area.width() / size.width(), area.height() / size.height())
    width, height = int(size.width() * scale), int(size.height() * scale)
    return QRect(
        area.x() + (area.width() - width) // 2, area.y() + (area.height() - height) // 2,
        width, height,
    )


def caption_label(diagram: Diagram) -> QWidget:
    """The line under a drawing, or an empty widget when there is nothing to say."""
    holder = QWidget()
    box = QVBoxLayout(holder)
    box.setContentsMargins(0, 0, 0, 8)
    label = QLabel(diagram.caption)
    label.setWordWrap(True)
    label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
    box.addWidget(label)
    holder.setVisible(bool(diagram.caption))
    return holder


__all__ = ["COLUMN_WIDTH", "DiagramPanel", "caption_label"]

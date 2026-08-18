"""Where each control sits, read out of the drawings rather than written down twice.

That is the whole reason this module exists, and it is worth stating because the source project got
it wrong in an instructive way: its anchor fractions were measured by eye against a vendor render,
drifted from it, and the dropdowns ended up overlapping around the D-pad cluster. Its own notes
record the bug and propose re-measuring by hand, which would only postpone it.

Here the drawing *is* the coordinate table. Every configurable control carries ``id="anchor-KEY"``,
Qt hands back the bounding box of any element by id, and the fractions come from that.

**Three drawings, not one.** A controller cannot be shown from one side. Trying to put everything on
a face-on view was the original mistake: the bumpers are barely a sliver from the front, the
triggers are invisible behind them, and the rear paddles sit directly behind the D-pad and the right
stick. Each attempt either floated a control clear of the shell or drew a line straight through one.
So each view carries only what that view can honestly show, and :data:`VIEWS` is the split.

No Qt import at module scope: ``core`` and the CLI must stay importable without a GUI, and this is
asked for the anchors only when a page is being drawn.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parent / "assets"

#: The model these drawings are of, and the prefix of their filenames.
#:
#: Named rather than called ``controller-front.svg``, because this module claims one product id
#: today and the reason is the record layout, not the shape of the plastic: 8BitDo's other families
#: use different offsets and would arrive as their own match rules with their own drawings. A
#: generic filename is fine for exactly as long as there is one controller, and misleading for ever
#: afterwards -- and the rename is free now and awkward once something references it.
#:
#: Adding a model: drop in ``<model>-front.svg`` and friends, and give :data:`VIEWS` a second entry
#: keyed by model. Nothing else here assumes there is only one.
MODEL = "ultimate-wired-xbox"

#: The product ids these drawings are of. A controller that is not one of them must not be shown
#: them: the picture is the part of the page a user trusts to say which button they are editing, and
#: a plausible drawing of the wrong controller is worse than no drawing at all.
MODEL_PRODUCT_IDS = (0x2002,)

FRONT = ASSETS / f"{MODEL}-front.svg"
TOP = ASSETS / f"{MODEL}-top.svg"
BACK = ASSETS / f"{MODEL}-back.svg"

#: Which drawing carries which controls, and therefore which anchors each file must contain.
#: Asserted rather than discovered: a typo in an id would otherwise produce a control with no
#: leader line, which looks like a rendering glitch rather than a missing anchor.
VIEWS: dict[str, tuple[Path, tuple[str, ...]]] = {
    "front": (FRONT, (
        "A", "B", "X", "Y", "L3", "R3", "VIEW", "MENU", "STAR",
        "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
    )),
    "top": (TOP, ("LB", "RB", "LT", "RT")),
    "back": (BACK, ("PADDLE_L", "PADDLE_R")),
}

#: Human wording for each view, for a tab or heading.
VIEW_LABELS = {"front": "Front", "top": "Shoulders and triggers", "back": "Back paddles"}

#: The line under each drawing. Line art does not say which way round it is, and on the paddles
#: that is not a detail: getting it backwards means remapping the wrong one.
#:
#: The paddle view keeps the front view's silhouette, so it shows the rear controls *in place*
#: rather than mirroring the shell -- the left paddle is on the left, where your left hand is.
#: Said out loud because the opposite convention is equally common and both look identical.
VIEW_CAPTIONS = {
    "front": "The controller as it faces you.",
    "top": "The top edge: bumpers above, triggers below.",
    "back": "The paddles behind the shell, drawn in place — left on the left.",
}

#: Every control across every view. The union has to match the module's remappable set exactly, or
#: some control has no way to be pointed at.
EXPECTED: tuple[str, ...] = tuple(
    key for _, keys in VIEWS.values() for key in keys
)


@lru_cache(maxsize=8)
def anchors(view: str) -> dict[str, tuple[float, float]]:
    """``{key: (x, y)}`` for one view, as fractions of that drawing, or ``{}`` if unreadable.

    Fractions rather than pixels because the panel scales each drawing to whatever room it has.
    Absence is a normal answer: without Qt's SVG module, or with an asset missing, the page still
    builds as a plain form. Nothing here raises.
    """
    try:
        path, keys = VIEWS[view]
    except KeyError:
        log.warning("no such view: %s", view)
        return {}

    try:
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError:
        log.debug("no PyQt6.QtSvg; the page will render without the controller drawings")
        return {}

    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        # A warning, not a debug line: the file is ours, so an invalid one is a bug we introduced.
        # XML forbidding a double hyphen inside a comment is how it happened, twice.
        log.warning("%s is not valid SVG; falling back to a plain form", path.name)
        return {}

    size = renderer.defaultSize()
    if size.width() <= 0 or size.height() <= 0:
        log.warning("%s has no intrinsic size", path.name)
        return {}

    found: dict[str, tuple[float, float]] = {}
    for key in keys:
        element = f"anchor-{key}"
        if not renderer.elementExists(element):
            continue
        box = renderer.boundsOnElement(element)
        found[key] = (box.center().x() / size.width(), box.center().y() / size.height())

    missing = [k for k in keys if k not in found]
    if missing:
        log.warning("%s is missing anchors: %s", path.name, ", ".join(missing))
    return found


def all_anchors() -> dict[str, dict[str, tuple[float, float]]]:
    """Every view's anchors, keyed by view name."""
    return {view: anchors(view) for view in VIEWS}


def view_of(key: str) -> str | None:
    """Which drawing shows this control, or None if none of them do."""
    for view, (_, keys) in VIEWS.items():
        if key in keys:
            return view
    return None


__all__ = ["ASSETS", "BACK", "EXPECTED", "FRONT", "MODEL", "TOP", "VIEWS", "VIEW_CAPTIONS",
           "VIEW_LABELS",
           "all_anchors", "anchors", "view_of"]

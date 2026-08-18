"""The controller drawings, and the anchors read out of them.

The source project's leader lines were hand-measured against a vendor render and drifted from it, so
its dropdowns overlapped around the D-pad cluster. Here the drawings *are* the coordinate table, and
these tests keep that true: every control has an anchor, no two land on top of each other, and they
all fall inside their picture.

**Three drawings, because a controller has more than one side.** Putting everything on a face-on
view was the original mistake and it failed three ways — bumpers reduced to a sliver, triggers
invisible behind them, rear paddles colliding with the D-pad and the right stick. Each view now
carries only what it can honestly show, and the tests check that the split is complete rather than
that any one file has everything.

They also guard one specific, embarrassing failure mode. XML forbids a double hyphen inside a
comment, and this project writes `--` as an em dash everywhere else, so a drawing was twice silently
invalid: Qt returned `isValid() == False`, every anchor vanished, and the page fell back to a plain
form with no visible error. `test_every_drawing_is_valid_svg_to_qt` is cheap and catches it at once.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from hardware_ui.modules.eightbitdo_controllers import anchors as A
from hardware_ui.modules.eightbitdo_controllers.protocol import fieldmap as fm

VIEW_NAMES = sorted(A.VIEWS)


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_every_drawing_ships_with_the_module(view):
    assert A.VIEWS[view][0].is_file()


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_every_drawing_is_valid_xml(view):
    """Parsed with the stdlib as well as Qt, because the stdlib says *why* it failed."""
    ET.parse(A.VIEWS[view][0])


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_no_double_hyphen_hides_in_a_comment(view):
    """The exact bug above, as a rule rather than left for the XML parser to imply."""
    raw = A.VIEWS[view][0].read_text()
    index = 0
    while (start := raw.find("<!--", index)) != -1:
        end = raw.find("-->", start + 4)
        assert end != -1, "unterminated comment"
        assert "--" not in raw[start + 4:end], "XML forbids `--` in a comment; use an em dash"
        index = end + 3


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_every_drawing_is_valid_svg_to_qt(view):
    pytest.importorskip("PyQt6.QtSvg")
    from PyQt6.QtSvg import QSvgRenderer

    path = A.VIEWS[view][0]
    renderer = QSvgRenderer(str(path))
    assert renderer.isValid(), f"{path.name} is not valid SVG to Qt"
    assert renderer.defaultSize().width() > 0


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_each_view_carries_exactly_its_own_controls(view):
    pytest.importorskip("PyQt6.QtSvg")
    A.anchors.cache_clear()
    assert set(A.anchors(view)) == set(A.VIEWS[view][1])


def test_the_three_views_between_them_cover_every_control():
    """The property that matters. A control with no anchor on any view cannot be pointed at."""
    pytest.importorskip("PyQt6.QtSvg")
    found = {key for view in A.VIEWS for key in A.anchors(view)}
    needed = set(fm.REMAPPABLE) | set(fm.PADDLES)
    assert needed <= found, f"no anchor anywhere for: {sorted(needed - found)}"
    assert not found - needed, f"anchors for controls that do not exist: {sorted(found - needed)}"


def test_no_control_appears_on_two_views():
    """Two drawings claiming the same control would give it two leader lines in two places."""
    seen: dict[str, str] = {}
    for view, (_, keys) in A.VIEWS.items():
        for key in keys:
            assert key not in seen, f"{key} is on both {seen.get(key)} and {view}"
            seen[key] = view


def test_view_of_agrees_with_the_split():
    for view, (_, keys) in A.VIEWS.items():
        for key in keys:
            assert A.view_of(key) == view
    assert A.view_of("NOT_A_CONTROL") is None


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_no_anchor_shares_a_position_with_another(view):
    """The collision the source project hit. Four D-pad directions pointing at one centre is the
    likeliest way to reintroduce it, so the tolerance is generous rather than exact."""
    pytest.importorskip("PyQt6.QtSvg")
    found = A.anchors(view)
    keys = sorted(found)
    for i, first in enumerate(keys):
        for second in keys[i + 1:]:
            (x1, y1), (x2, y2) = found[first], found[second]
            assert abs(x1 - x2) > 0.01 or abs(y1 - y2) > 0.01, (
                f"{first} and {second} anchor to the same point on {view}"
            )


@pytest.mark.parametrize("view", VIEW_NAMES)
def test_every_anchor_is_inside_its_picture(view):
    pytest.importorskip("PyQt6.QtSvg")
    for key, (x, y) in A.anchors(view).items():
        assert 0.0 < x < 1.0 and 0.0 < y < 1.0, f"{key} is outside {view} at ({x}, {y})"


@pytest.mark.parametrize(("view", "pairs"), [
    ("top", (("LB", "RB"), ("LT", "RT"))),
    ("back", (("PADDLE_L", "PADDLE_R"),)),
])
def test_mirrored_hardware_is_mirrored_in_the_drawing(view, pairs):
    """Bumpers, triggers and paddles are mirror images on the real controller. Catches a one-sided
    edit. The sticks deliberately are *not* mirrored, so the front view is excluded."""
    pytest.importorskip("PyQt6.QtSvg")
    found = A.anchors(view)
    for left, right in pairs:
        lx, ly = found[left]
        rx, ry = found[right]
        assert abs((1.0 - lx) - rx) < 0.01, f"{left}/{right} are not mirrored on {view}"
        assert abs(ly - ry) < 0.01, f"{left}/{right} sit at different heights on {view}"


def test_the_front_view_no_longer_claims_what_it_cannot_show():
    """The regression this restructuring exists to prevent: bumpers, triggers and paddles back on
    the face-on view, where they either float clear of the shell or cross a control in front."""
    front = A.FRONT.read_text()
    for key in ("LB", "RB", "LT", "RT", "PADDLE_L", "PADDLE_R"):
        assert f'anchor-{key}"' not in front, f"{key} is back on the front view"


def test_the_back_view_reuses_the_front_silhouette():
    """Same shell from the other side, so the path is shared rather than measured twice. Keeps the
    two drawings the same size and shape by construction."""
    import re

    def body(path):
        match = re.search(r'<path class="body" d="(.*?)"', path.read_text(), re.S)
        return re.sub(r"\s+", " ", match.group(1)).strip() if match else None

    assert body(A.FRONT) is not None
    assert body(A.BACK) == body(A.FRONT)


def test_a_missing_drawing_is_not_an_error():
    """Absence is a normal answer: the page still builds as a plain form."""
    A.anchors.cache_clear()
    assert A.anchors("nonexistent-view") == {}
    A.anchors.cache_clear()


def test_the_assets_are_small_enough_to_ship():
    """The point of drawing them rather than importing the vendor's: theirs run to about 2 MB of
    PNGs across the screens, plus a shadow asset per screen."""
    total = sum(path.stat().st_size for path, _ in A.VIEWS.values())
    assert total < 64_000, f"{total} bytes of artwork"

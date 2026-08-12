"""The Poly label extractor's block detection.

The interesting part is not the regex but deciding *where English stops*. Poly ships every language
in one bundle, in the same shape, with nothing naming the locale — so a wrong boundary silently
yields another language's labels rather than an error. That happened during development: an earlier
window-based attempt produced a confident, complete, Danish table.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "extract_ui_labels", Path(__file__).resolve().parent.parent / "tools" / "extract_ui_labels.py"
)
assert spec and spec.loader
extract = importlib.util.module_from_spec(spec)
spec.loader.exec_module(extract)


def entry(display: str, name: str, options: dict[str, str], description: str = "") -> str:
    body = ",".join(f'"{k}":"{v}"' for k, v in options.items())
    middle = f',"description":"{description}"' if description else ""
    return f'"{display}":{{"name":"{name}"{middle},"options":{{{body}}}}}'


ENGLISH = ",".join(
    [
        entry("Sidetone", "Sidetone", {"Off": "Off", "Low": "Low", "High": "High"},
              extract.ENGLISH_MARKER),
        entry("ANC", "ANC Mode", {"Adaptive": "Adaptive", "Standard": "Standard"}),
    ]
)
DANISH = ",".join(
    [
        entry("Sidetone", "Medhør", {"Off": "Fra", "Low": "Lav", "High": "Høj"}),
        entry("ANC", "ANC-tilstand", {"Adaptive": "Tilpasset", "Standard": "Standard"}),
    ]
)
GAP = "x" * (extract.SAME_BLOCK * 2)


def test_stops_at_the_locale_boundary() -> None:
    """The neighbouring language is separated by bundle, not by a name — the gap is the boundary."""
    found = extract.harvest(extract.english_entries(f"{{{DANISH}{GAP}{ENGLISH}{GAP}{DANISH}}}"))
    assert found["High|Low|Off"]["label"] == "Sidetone"
    assert found["High|Low|Off"]["Low"] == "Low"
    assert not any(ord(c) > 127 for e in found.values() for c in e["label"])


def test_english_first_and_last_are_both_reached() -> None:
    """Growing outwards must cover the whole run, not just from the marker onwards."""
    for text in (f"{{{ENGLISH}{GAP}{DANISH}}}", f"{{{DANISH}{GAP}{ENGLISH}}}"):
        found = extract.harvest(extract.english_entries(text))
        assert set(found) == {"High|Low|Off", "Adaptive|Standard"}


def test_booleans_are_dropped() -> None:
    """Every boolean shares Enabled/Disabled, so the signature identifies no particular setting."""
    text = f'{{{entry("Mute", "Mute", {"Enabled": "Enabled", "Disabled": "Disabled"})},{ENGLISH}}}'
    assert "Disabled|Enabled" not in extract.harvest(extract.english_entries(text))


def test_a_missing_marker_is_an_error_not_an_empty_table() -> None:
    """If Poly rewords the sentence, say so — silently writing nothing looks like success."""
    with pytest.raises(SystemExit):
        extract.english_entries(f"{{{DANISH}}}")


def test_descriptions_come_through_when_present() -> None:
    found = extract.harvest(extract.english_entries(f"{{{ENGLISH}}}"))
    assert found["High|Low|Off"]["description"] == extract.ENGLISH_MARKER
    assert "description" not in found["Adaptive|Standard"]

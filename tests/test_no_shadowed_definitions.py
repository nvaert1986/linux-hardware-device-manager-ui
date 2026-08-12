"""No class may define the same method twice.

Found on real hardware, 2026-08-11: the Jabra module had two ``_route`` methods -- one returning
the connection route, one routing pushed events. The second silently replaced the first, and
``connection_label()`` then called it with the wrong signature. The failure surfaced only when a
device was actually connected.

**Ruff does not catch this.** F811 is *"redefinition of unused name"*, and here the first
definition was used in between (``connection_label`` calls ``_route``), so the name is not unused
and the rule stays silent. A minimal two-method repro *does* trip F811, which is exactly why the
gap is easy to talk yourself out of.

The same check covers module-level functions, where the trap is identical.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
#: Ours only. Vendored third-party code is upstream's to style, and a finding there would be a
#: bug report for them rather than something to fix here.
SOURCES = sorted(
    p for p in (ROOT / "hardware_ui").rglob("*.py")
    if "third_party" not in p.parts
)

#: ``@overload``, ``@property``/``@x.setter`` and ``@singledispatch`` pairs are deliberate
#: redefinitions of one name and must not be reported.
ALLOWED = {"overload", "setter", "getter", "deleter", "register"}


def _decorated_with_allowed(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", []):
        name = decorator
        if isinstance(name, ast.Call):
            name = name.func
        if isinstance(name, ast.Attribute) and name.attr in ALLOWED:
            return True
        if isinstance(name, ast.Name) and name.id in ALLOWED:
            return True
    return False


def _duplicates(body: list[ast.stmt]) -> list[str]:
    seen: dict[str, int] = {}
    clashes: list[str] = []
    for node in body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _decorated_with_allowed(node):
            continue
        if node.name in seen:
            clashes.append(f"{node.name} (lines {seen[node.name]} and {node.lineno})")
        seen[node.name] = node.lineno
    return clashes


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_nothing_is_defined_twice(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    problems = [f"module: {c}" for c in _duplicates(tree.body)]
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            problems += [f"{node.name}: {c}" for c in _duplicates(node.body)]
    assert not problems, f"{path.relative_to(ROOT)} defines a name twice -- " + "; ".join(problems)

"""Ported DDC/CI protocol code (Qt-free, unit-testable).

``ddcutil`` and ``features`` are byte-identical to their origin in
``plasma-dell-monitor-support`` so they can be diffed and re-synced; ``calibration`` differs only
in where its results are stored. All three are excluded from ruff for that reason.
"""

from . import calibration, ddcutil, features  # noqa: F401

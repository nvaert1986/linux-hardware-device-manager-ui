"""Per-monitor calibration of continuous-feature ranges.

DDC/CI only lets us *read* a continuous feature's current value and maximum; the
usable **minimum** and **step** are not queryable — some Dell panels clamp the
DDC range (e.g. contrast >= 25, gain >= 30) or quantise it (sharpness in 10s)
even though the OSD offers the full 0-100. The only way to learn those limits is
to write probe values and read back what the panel accepts.

Calibration results are cached on disk, keyed by the monitor's serial, so the
(screen-flashing) probe only runs when the user asks for it.

Ported from ``plasma-dell-monitor-support/plasma_dell_monitor/calibration.py``. ``probe_range``
and ``Range`` are unchanged; the origin's ``load``/``save`` lived here and wrote to the app's own
config file, so they moved to ``device.py``, which owns this module's cache directory. See
``docs/PORT_DIVERGENCES.md``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .ddcutil import get_vcp, set_vcp

_PROBE_SETTLE = 0.1  # seconds between a write and reading it back


@dataclass
class Range:
    minimum: int
    maximum: int
    step: int


def probe_range(bus: int, code: int, reported_max: int) -> Range:
    """Discover (min, max, step) for one continuous feature, then restore it.

    Writes several low probe values and inspects the distinct read-backs:
      * minimum = what the panel reports after we ask for 0
      * step    = smallest gap between distinct accepted values near the floor
    The feature is returned to its original value before we return.
    """
    original = get_vcp(bus, code).value

    def _set_read(v: int) -> int:
        set_vcp(bus, code, v)
        time.sleep(_PROBE_SETTLE)
        return get_vcp(bus, code).value

    try:
        minimum = _set_read(0)
        # Spread of low targets to expose quantisation without a full sweep.
        targets = sorted({minimum + d for d in (1, 2, 4, 7, 14)})
        reads = sorted({minimum, *(_set_read(t) for t in targets)})
        gaps = [b - a for a, b in zip(reads, reads[1:]) if b > a]
        step = min(gaps) if gaps else 1
    finally:
        set_vcp(bus, code, original)  # always put it back

    return Range(minimum=minimum, maximum=reported_max, step=max(1, step))

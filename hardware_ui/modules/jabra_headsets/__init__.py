"""Jabra headsets, over GN Audio's own GNP protocol on a vendor HID usage page.

Ported from the standalone ``plasma-jabra-headphone-support`` project, whose protocol, transport
and interpreter layers were verified against a Link 390 and an Evolve2 85. Its GUI is not ported:
that is what this application replaces.
"""

from .device import JabraHeadsetDevice

__all__ = ["JabraHeadsetDevice"]

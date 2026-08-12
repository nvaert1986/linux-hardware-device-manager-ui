"""Minimal stand-in for Solaar's package root -- see tools/vendor_solaar.py.

Upstream derives a version by shelling out to ``git describe`` and reading data files laid down at
build time. Vendored, there is no checkout and no build step, so the two names
``logitech_receiver`` actually uses are provided directly.
"""

NAME = "Solaar"
__version__ = "1.1.19"

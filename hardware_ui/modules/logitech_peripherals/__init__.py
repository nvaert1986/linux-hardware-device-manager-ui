"""Logitech mice, keyboards and receivers, over HID++.

The protocol and the setting definitions are Solaar's, vendored under ``hardware_ui/third_party``
rather than depended on, because ``app-misc/solaar`` is not split and installing it would pull an
entire GTK stack in for a library this Qt application only calls. See ``tools/vendor_solaar.py``.
"""

from .device import LogitechDevice

__all__ = ["LogitechDevice"]

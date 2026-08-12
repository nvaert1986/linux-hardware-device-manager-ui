"""Device modules.

One subpackage per device family, each contributing protocol code and a ``module.toml`` manifest
and no UI whatsoever. Names are short Python identifiers (``sony``, ``poly``); the human-readable
label lives in the manifest's ``name`` field.

Out-of-tree modules are equally first-class: they register through the ``hardware_ui.modules``
entry point group and need not live here.
"""

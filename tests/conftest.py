"""Shared pytest fixtures.

The only interesting one is ``qapp``. A ``QApplication`` created inside a test is destroyed when
that test's locals go out of scope, while ``QApplication.instance()`` keeps handing out the
dangling pointer -- the next widget built then aborts the interpreter. One session-scoped
instance, held for the whole run, is the fix.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def qapp():
    pytest.importorskip("PyQt6.QtWidgets")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def isolate_user_config(tmp_path, monkeypatch):
    """Point every XDG directory at a temporary one, for every test.

    Autouse and unconditional: a test that enabled or disabled a module wrote to the *user's*
    ``~/.config/hardware-ui/modules.toml``, and nothing in the suite should be able to touch real
    user data by accident. Modules also cache and store there.
    """
    for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    import hardware_ui.core.paths as paths

    monkeypatch.setattr(paths, "config_dir", lambda: tmp_path / "config" / "hardware-ui")
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data" / "hardware-ui")
    monkeypatch.setattr(paths, "cache_dir", lambda: tmp_path / "cache" / "hardware-ui")
    import hardware_ui.core.modules as modules

    monkeypatch.setattr(modules, "config_dir", lambda: tmp_path / "config" / "hardware-ui")

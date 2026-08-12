"""Runs the asyncio event loop beside Qt's, using only the standard library.

The usual answer here is qasync, which unifies the two loops. It has no ebuild in any Gentoo
repository, and pulling it in by pip would mean an unpackaged dependency in a project meant to be
installable from an overlay. So instead the asyncio loop gets its own thread.

That turns out to be the better structure anyway:

* Device I/O physically cannot block the UI thread -- not by convention, but because it runs
  somewhere else.
* The core stays a plain asyncio library, testable headless with ``asyncio.run`` and no Qt.

The cost is a marshalling rule, and it is absolute: **Qt models may only be mutated on the GUI
thread.** Coroutines therefore never touch a model directly; they hand a callable to
:meth:`AsyncBridge.call_on_ui`, which delivers it via a queued signal.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
from collections.abc import Callable, Coroutine
from typing import Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

log = logging.getLogger(__name__)


class AsyncBridge(QObject):
    """Owns the asyncio loop and ferries work between it and the GUI thread."""

    _marshal = pyqtSignal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hardware-ui-asyncio", daemon=True)
        # Queued explicitly rather than relying on auto-detection, so this stays correct even if a
        # caller happens to already be on the GUI thread.
        self._marshal.connect(self._invoke, Qt.ConnectionType.QueuedConnection)

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5)
        log.debug("asyncio loop running in %s", self._thread.name)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def stop(self, timeout: float = 5.0) -> None:
        """Cancel outstanding work and shut the loop down.

        Called from ``aboutToQuit``. Best-effort: a device that refuses to close must not stop the
        application from exiting.
        """
        if not self._thread.is_alive():
            return

        async def _cancel_all() -> None:
            # Exclude ourselves: this coroutine runs as a task, so cancelling the full set would
            # cancel this task mid-gather and recurse until the stack blows.
            me = asyncio.current_task()
            pending = [
                t for t in asyncio.all_tasks(self._loop) if t is not me and not t.done()
            ]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        try:
            concurrent.futures.wait([self.submit(_cancel_all())], timeout=timeout)
        except Exception:
            log.debug("shutdown cancellation failed", exc_info=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)

    # ---------------------------------------------------------------- submission

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        """Schedule *coro* on the asyncio thread. Safe to call from the GUI thread."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, label: str = "") -> None:
        """Fire and forget, with exceptions logged rather than swallowed.

        A bare ``run_coroutine_threadsafe`` discards the future, so a crashing coroutine vanishes
        silently -- the classic way an async UI develops mysterious dead buttons.
        """
        future = self.submit(coro)

        def _report(f: concurrent.futures.Future) -> None:
            try:
                f.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception:
                log.exception("unhandled error in task %s", label or "<anonymous>")

        future.add_done_callback(_report)

    # ---------------------------------------------------------------- marshalling

    def call_on_ui(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Run *fn* on the GUI thread. The only legal way to touch a model from a coroutine."""
        self._marshal.emit(lambda: fn(*args, **kwargs))

    @pyqtSlot(object)
    def _invoke(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            log.exception("error in UI callback")

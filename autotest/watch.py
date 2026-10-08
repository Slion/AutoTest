"""Crash detection: notice when the app under test dies mid-run.

A :class:`CrashWatcher` polls ``device.app_alive()`` on a background thread
while a test runs. It records a crash only on an alive -> dead *transition*:
the app not being up when the watcher starts is normal (the test launches it),
so the watcher arms itself the first time it sees the process alive. When the
process dies, the watcher captures ``device.crash_evidence()`` once and stops.

The runner uses the result to fail the in-flight test with the log excerpt and
stop the rest of the run for that device — a dead app makes every following
test fail for the same reason, so running on would only burn time.
"""
from __future__ import annotations

import threading

from .device import Device


class CrashWatcher:
    """Poll ``device.app_alive()`` in the background; record a crash on death."""

    def __init__(self, device: Device, interval: float = 1.0):
        self._device = device
        self._interval = interval
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._was_alive = False
        self._crashed = False
        self._evidence = ""

    def start(self) -> None:
        """Start polling; the watcher stops itself once a crash is recorded."""
        self._thread = threading.Thread(
            target=self._loop, name="autotest-crash-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and join the thread (safe to call before :meth:`start`)."""
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    @property
    def crashed(self) -> bool:
        """True if the app died while this watcher was running."""
        with self._lock:
            return self._crashed

    @property
    def evidence(self) -> str:
        """The crash log captured at the moment of death (may be empty)."""
        with self._lock:
            return self._evidence

    # -- internals ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_evt.wait(self._interval):
            self._poll_once()

    def _poll_once(self) -> None:
        with self._lock:
            if self._crashed:
                return
            try:
                alive = self._device.app_alive()
            except Exception:  # noqa: BLE001 - an adb hiccup is not a crash
                return
            if alive:
                self._was_alive = True
            elif self._was_alive:
                self._crashed = True
                try:
                    self._evidence = self._device.crash_evidence().strip()
                except Exception:  # noqa: BLE001 - evidence is best effort
                    self._evidence = ""
                self._stop_evt.set()

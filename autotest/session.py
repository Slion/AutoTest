"""Per-run session state: restart policy and tab hygiene.

This replaces the old module-level globals that lived in the adb helper. A
:class:`Session` is created by the runner for one run; each :class:`Device` is
given the shared instance so every test operates on the same run-wide policy.

The runner configures it up front (``session.set_restarts_between_tests(...)``,
``session.set_keep_opened(...)``) and each test resets the opened-counter before
it runs (``session.reset_opened()``) so the end-of-test cleanup closes exactly
the tabs the test created.

A "tab" here is deliberately generic: it is whatever unit of state the app
accumulates that the runner should clean up afterwards (a browser tab, a
document, …). The app profile decides what "close one" means (e.g. CTRL+W).
"""
from __future__ import annotations


class Session:
    """Run-wide state shared by every device in a test run."""

    #: Whether ``navigate``/``settle`` restarts the app when not told explicitly.
    restart_between_tests: bool = True
    #: Whether the runner leaves test-created tabs open (default: close them).
    keep_tabs: bool = False
    #: Number of tabs the current test has opened (reset before each test).
    tabs_opened: int = 0

    def set_restarts_between_tests(self, restart: bool) -> None:
        self.restart_between_tests = restart

    def set_keep_tabs(self, keep: bool) -> None:
        self.keep_tabs = keep

    def reset_opened(self) -> None:
        """Reset the per-test opened-tab count (called by the runner before each test)."""
        self.tabs_opened = 0

    def note_opened(self, n: int = 1) -> None:
        """Record that the test opened ``n`` tab(s) outside of navigate()."""
        self.tabs_opened += n

    @property
    def opened(self) -> int:
        return self.tabs_opened

    @property
    def keep(self) -> bool:
        return self.keep_tabs

"""The platform-agnostic ``Device`` contract that tests are written against.

A test receives a :class:`Device` and drives it with high-level, semantic calls
(``device.key(keys.DPAD_DOWN)``, ``device.field_text()``) that carry no assumption
about *how* the device is reached. The concrete :class:`~autotest.android.device.AndroidDevice`
implements this over adb today; another platform only needs to provide its own
``Device`` subclass and transport, and the existing tests run unchanged.

Only *generic* capability lives on this contract: identity, input, lifecycle,
the raw UI hierarchy, the display and orientation. App-specific behavior (the
address bar, tabs, an in-app cursor, app notifications, …) is deliberately NOT
here — an app profile (e.g. Fulguris) subclasses the platform device and adds
it, so the contract stays usable for any app on any platform.

A :class:`~autotest.session.Session` is attached to every device so a run-wide
policy (restart between tests, close the tabs a test opened) is shared by all of
them. See :meth:`Device.session`.
"""
from __future__ import annotations

import abc
import re

from .node import Node
from .session import Session
from .transport import Transport


class Device(abc.ABC):
    """A target under test, addressed through a platform-neutral API.

    Implementations bind this contract to a concrete platform + transport (e.g.
    :class:`~autotest.android.device.AndroidDevice` over adb). Every method here
    is what a cross-platform test may rely on; platform-only or app-only extras
    live on the subclass.
    """

    #: The transport used to reach this device (e.g. an adb transport).
    transport: Transport
    #: The shared per-run session (restart policy, opened-tab hygiene).
    session: Session

    # --- session policy (concrete, delegate to the shared session) ---------

    def restart_between_tests(self) -> bool:
        """Whether ``navigate`` restarts the app when not told explicitly."""
        return self.session.restart_between_tests

    def note_tab_opened(self, n: int = 1) -> None:
        """Record that the test opened ``n`` tab(s) outside of navigate()."""
        self.session.note_opened(n)

    # --- identity ----------------------------------------------------------

    @property
    @abc.abstractmethod
    def id(self) -> str:
        """A stable unique identifier for this device (e.g. an adb serial)."""

    @property
    def safe_id(self) -> str:
        """A filesystem-safe form of :attr:`id`, for artifact filenames."""
        return re.sub(r"[^A-Za-z0-9_.-]", "_", self.id)

    @property
    @abc.abstractmethod
    def package(self) -> str:
        """The app package / bundle id under test."""

    @property
    @abc.abstractmethod
    def platform(self) -> str:
        """Short platform name, e.g. ``"android"``."""

    @abc.abstractmethod
    def label(self) -> str:
        """Human-readable device label for logs (id + friendly name)."""

    @abc.abstractmethod
    def config(self) -> dict:
        """Describe the device + current screen configuration (see config_id)."""

    @abc.abstractmethod
    def is_leanback(self) -> bool:
        """True on a TV-style (D-pad/remote, no touch) device."""

    @abc.abstractmethod
    def foreground_package(self) -> str | None:
        """Package of the top resumed activity, if any."""

    # --- input -------------------------------------------------------------

    @abc.abstractmethod
    def key(self, code: int, wait: float = 0.5) -> None:
        """Send a single key (see :mod:`autotest.keys`)."""

    @abc.abstractmethod
    def key_longpress(self, code: int, wait: float = 0.8) -> None:
        """Send a long-press key (sets the platform long-press flag)."""

    @abc.abstractmethod
    def key_hold(self, code: int, ms: int, wait: float = 0.3) -> None:
        """Press and hold a key for ``ms`` milliseconds, then release it."""

    @abc.abstractmethod
    def key_combination(self, *codes: int, wait: float = 0.6) -> None:
        """Send a chord of keys pressed together (e.g. CTRL+TAB)."""

    @abc.abstractmethod
    def tap(self, x: int, y: int, wait: float = 0.7) -> None:
        """Tap absolute screen coordinates."""

    @abc.abstractmethod
    def tap_longpress(self, x: int, y: int, hold_ms: int = 800, wait: float = 1.0) -> None:
        """Hold a touch at (x, y) for ``hold_ms`` (touch long-press / context menu)."""

    @abc.abstractmethod
    def type_text(self, text: str, wait: float = 0.5) -> None:
        """Type text into the focused field."""

    @abc.abstractmethod
    def clear_field(self) -> None:
        """Clear the focused edit field."""

    # --- app lifecycle -----------------------------------------------------

    @abc.abstractmethod
    def launch(self, wait: float = 5.0) -> None:
        """Bring the app to the foreground (launching it if needed)."""

    @abc.abstractmethod
    def restart(self, wait: float = 5.0) -> None:
        """Force-stop then relaunch the app for a clean state."""

    @abc.abstractmethod
    def force_stop(self) -> None:
        """Force-stop the app."""

    @abc.abstractmethod
    def settle(self, timeout: float = 60.0) -> bool:
        """Wait until the app is foregrounded and its main UI is ready.

        Generic in the sense that every app has *some* notion of "ready"; the
        app profile decides what that means (e.g. a toolbar is present).
        """

    # --- UI state ----------------------------------------------------------

    @abc.abstractmethod
    def nodes(self) -> list[Node]:
        """The current UI hierarchy."""

    @abc.abstractmethod
    def find_node(self, id_suffix: str) -> Node | None:
        """The first node whose resource id ends with ``id_suffix``."""

    @abc.abstractmethod
    def find_node_by_text(self, text: str) -> Node | None:
        """The first node whose text exactly equals ``text``."""

    def tap_text(self, text: str, timeout: float = 10.0) -> bool:
        """Tap the node whose text exactly equals ``text``. Returns False if none appears."""
        import time

        deadline = time.time() + timeout
        while True:
            n = self.find_node_by_text(text)
            if n and n.center:
                self.tap(*n.center)
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    @abc.abstractmethod
    def webview_focused(self) -> bool:
        """True if a WebView node has focus."""

    @abc.abstractmethod
    def ime_shown(self, timeout: float = 0.0) -> bool:
        """True if the on-screen keyboard is shown.

        ``timeout > 0`` polls until the keyboard is reported shown or the
        deadline passes (some OEM builds update the shown-state flag a few
        seconds late); 0 keeps a single-shot read.
        """

    @abc.abstractmethod
    def dropdown_present(self) -> bool:
        """True if a suggestions/autocomplete popup is present."""

    # --- display -----------------------------------------------------------

    @abc.abstractmethod
    def screen_size(self) -> tuple[int, int]:
        """Logical screen size in pixels."""

    @abc.abstractmethod
    def screenshot(self, path: str) -> None:
        """Capture the screen to ``path`` (PNG) on the host."""

    @abc.abstractmethod
    def logcat(self, grep: str, clear: bool = False) -> str:
        """Dump the device log, keeping only lines containing ``grep``."""

    # --- orientation -------------------------------------------------------

    @abc.abstractmethod
    def orientation_state(self):
        """Snapshot the orientation so it can be restored later."""

    @abc.abstractmethod
    def set_orientation(self, orientation: str, wait: float = 1.5) -> None:
        """Force the device orientation (portrait/landscape/sensor)."""

    @abc.abstractmethod
    def restore_orientation(self, *state) -> None:
        """Restore a state captured by :meth:`orientation_state`."""

    # --- optional hooks (concrete no-ops; app/profiles override) ----------
    # The runner relies on these existing, so they are concrete here. A profile
    # that has no equivalent simply inherits the no-op.

    def close_tabs(self, count: int, wait: float = 0.9) -> None:
        """Close ``count`` opened units the test created (e.g. browser tabs).

        Called by the runner's end-of-test cleanup. The base is a no-op; an app
        profile (e.g. a tabbed browser) overrides it with the real mechanism.
        """

    def post_notification(self, text: str) -> None:
        """Show a device progress notification with ``text`` (best effort)."""

    def dismiss_notification(self) -> None:
        """Dismiss the progress notification posted by :meth:`post_notification`."""

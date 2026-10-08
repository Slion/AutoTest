"""Android implementation of the :class:`~autotest.device.Device` contract.

``AndroidDevice`` binds the generic contract to one adb device serial + app
package. Every method forwards to the corresponding function in
:mod:`autotest.android.adb` — the proven low-level driver — with the
serial/package already applied. Keeping this a thin delegation (rather than
re-deriving behaviour) means the existing adb logic, timings and retries are
unchanged; this class only presents them as the platform-neutral, object-oriented
surface the framework and tests use.

Android/adb-only extras that have no cross-platform meaning (``reverse`` tunnels,
reading/writing a file in the app sandbox via ``run-as``, the raw input-device
injection helpers) live here on the subclass, not on the base ``Device``.

App-specific behavior (the address bar, tabs, an in-app cursor, …) is NOT here —
an app profile subclasses :class:`AndroidDevice` to add it.
"""
from __future__ import annotations

import os

from . import adb
from ..device import Device
from ..node import Node
from ..session import Session
from ..transport import Transport


class AdbTransport:
    """Android Debug Bridge transport, bound to one device serial."""

    name = "adb"

    def __init__(self, serial: str):
        self.serial = serial

    def shell(self, args: list[str], timeout: int = 30) -> str:
        """Run a raw adb command (e.g. ``["shell", "wm", "size"]``)."""
        return adb._adb(self.serial, args, timeout)

    def install(self, apk: str) -> bool:
        return adb.install_apk(self.serial, apk)

    def screencap(self, path: str) -> None:
        adb.screenshot(self.serial, path)

    def push(self, local: str, remote: str) -> None:
        adb.push(self.serial, local, remote)

    def pull(self, remote: str, local: str) -> None:
        adb.pull(self.serial, remote, local)

    def reverse(self, remote_port: int, local_port: int | None = None) -> None:
        """Forward ``localhost:remote_port`` on the device to the host's ``local_port``."""
        local_port = local_port if local_port is not None else remote_port
        self.shell(["reverse", f"tcp:{remote_port}", f"tcp:{local_port}"])

    def reverse_remove(self, remote_port: int) -> None:
        self.shell(["reverse", "--remove", f"tcp:{remote_port}"])


class AndroidDevice(Device):
    def __init__(self, serial: str, package: str, session: Session | None = None):
        self.serial = serial
        self._package = package
        self.transport: Transport = AdbTransport(serial)
        self.session = session or Session()

    # --- identity ----------------------------------------------------------

    @property
    def id(self) -> str:
        return self.serial

    @property
    def package(self) -> str:
        return self._package

    @property
    def platform(self) -> str:
        return "android"

    def label(self) -> str:
        return adb.device_label(self.serial)

    def config(self) -> dict:
        return adb.device_config(self.serial)

    def is_leanback(self) -> bool:
        return adb.is_leanback(self.serial)

    def foreground_package(self) -> str | None:
        return adb.foreground_package(self.serial)

    # --- input -------------------------------------------------------------

    def key(self, code: int, wait: float = 0.5) -> None:
        adb.key(self.serial, code, wait)

    def key_longpress(self, code: int, wait: float = 0.8) -> None:
        adb.key_longpress(self.serial, code, wait)

    def key_hold(self, code: int, ms: int, wait: float = 0.3) -> None:
        adb.key_hold(self.serial, code, ms, wait)

    def key_combination(self, *codes: int, wait: float = 0.6) -> None:
        adb.key_combination(self.serial, *codes, wait=wait)

    def ctrl_tab(self, wait: float = 0.9) -> None:
        """Switch to the next/most-recent tab with CTRL+TAB, as a keyboard user would."""
        adb.ctrl_tab(self.serial, wait)

    def tap(self, x: int, y: int, wait: float = 0.7) -> None:
        adb.tap(self.serial, x, y, wait)

    def tap_longpress(self, x: int, y: int, hold_ms: int = 800, wait: float = 1.0) -> None:
        adb.tap_longpress(self.serial, x, y, hold_ms, wait)

    def type_text(self, text: str, wait: float = 0.5) -> None:
        adb.type_text(self.serial, text, wait)

    def clear_field(self) -> None:
        adb.clear_field(self.serial)

    # --- app lifecycle -----------------------------------------------------

    def launch(self, wait: float = 5.0) -> None:
        """Bring the app's default launcher activity to the foreground.

        Uses ``monkey`` to launch the package's launcher activity without needing to
        know its name. An app profile overrides this to launch a specific activity
        (e.g. a splash screen) and tighten :meth:`settle`.
        """
        adb._adb(self.serial, ["shell", "monkey", "-p", self._package,
                               "-c", "android.intent.category.LAUNCHER", "1"], timeout=20)
        self.settle()

    def start_component(self, component: str, action: str | None = None, wait: float = 2.0) -> None:
        """Start an activity by fully-qualified component (``package/.Activity``)."""
        adb.start_component(self.serial, component, action, wait)

    def restart(self, wait: float = 5.0) -> None:
        adb.force_stop(self.serial, self._package)
        self.launch(wait)
        self.session.reset_opened()

    def force_stop(self) -> None:
        adb.force_stop(self.serial, self._package)

    def app_alive(self) -> bool:
        return bool(adb.app_pid(self.serial, self._package))

    def crash_evidence(self) -> str:
        return adb.crash_logcat(self.serial)

    def settle(self, timeout: float = 60.0) -> bool:
        # Generic readiness: the app is at least foregrounded. The app profile
        # tightens this to "its main UI is up".
        def ready() -> bool:
            return adb.foreground_package(self.serial) == self._package

        return adb.wait_until(ready, timeout, interval=0.5)

    # --- UI state ----------------------------------------------------------

    def nodes(self) -> list[Node]:
        return adb.nodes(self.serial)

    def find_node(self, id_suffix: str) -> Node | None:
        return adb.find_node(self.serial, id_suffix)

    def find_node_by_text(self, text: str) -> Node | None:
        return adb.find_node_by_text(self.serial, text)

    def webview_focused(self) -> bool:
        return adb.webview_focused(self.serial)

    def ime_shown(self, timeout: float = 0.0) -> bool:
        return adb.ime_shown(self.serial, timeout=timeout)

    def dropdown_present(self) -> bool:
        return adb.dropdown_present(self.serial)

    # --- display -----------------------------------------------------------

    def screen_size(self) -> tuple[int, int]:
        return adb.screen_size(self.serial)

    def screenshot(self, path: str) -> None:
        adb.screenshot(self.serial, path)

    def logcat(self, grep: str, clear: bool = False) -> str:
        return adb.logcat(self.serial, grep, clear=clear)

    # --- orientation -------------------------------------------------------

    def orientation_state(self):
        return adb.orientation_state(self.serial)

    def set_orientation(self, orientation: str, wait: float = 1.5) -> None:
        adb.set_orientation(self.serial, orientation, wait)

    def restore_orientation(self, *state) -> None:
        adb.restore_orientation(self.serial, *state)

    # --- Android/adb-only extras (not part of the cross-platform contract) --

    def reverse(self, remote_port: int, local_port: int | None = None) -> None:
        """Set up an adb reverse tunnel: device localhost:remote -> host local."""
        self.transport.reverse(remote_port, local_port)

    def reverse_remove(self, remote_port: int) -> None:
        self.transport.reverse_remove(remote_port)

    def read_prefs(self, rel_path: str) -> str:
        """Read a file from the app sandbox via ``run-as`` (e.g. a shared-prefs XML)."""
        return self.transport.shell(["shell", "run-as", self._package, "cat", rel_path])

    def write_prefs(self, rel_path: str, content: str) -> None:
        """Write ``content`` into the app sandbox at ``rel_path`` via a pushed temp file."""
        import subprocess
        import tempfile
        fd, local = tempfile.mkstemp(suffix=".xml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            dev_tmp = "/data/local/tmp/fw_prefs.xml"
            self.transport.push(local, dev_tmp)
            self.transport.shell(
                ["shell", "run-as", self._package, "cp", dev_tmp, rel_path])
        finally:
            os.remove(local)

    def input_devices(self) -> list[tuple[str, str]]:
        """The device input nodes as ``[(node_path, name), ...]``."""
        return adb.input_devices(self.serial)

    def find_input_node(self, name: str) -> str | None:
        """The input node of the first device whose name contains ``name``."""
        return adb.find_input_node(self.serial, name)

    def inject_hat_press(self, node: str, x: int, y: int = 0, wait: float = 0.8) -> None:
        """Push a gamepad D-pad hat (ABS_HAT0X/0Y) one step — a D-pad key from that device."""
        adb.inject_hat_press(self.serial, node, x, y, wait)

    def inject_hat_hold(self, node: str, x: int, y: int, hold: float, wait: float = 0.5) -> None:
        """Hold a gamepad D-pad hat pushed for ``hold`` seconds (virtual D-pad key held)."""
        adb.inject_hat_hold(self.serial, node, x, y, hold, wait)

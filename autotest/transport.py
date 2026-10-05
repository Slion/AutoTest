"""Transport layer: how the framework physically reaches a target device.

A ``Transport`` is the low-level pipe used to run commands, move files and grab
the screen on a device. Today the only implementation is :class:`~autotest.android.adb.AdbTransport`
(Android Debug Bridge), but isolating the pipe behind this small protocol is what
lets the rest of the framework stay transport-agnostic — a future iOS/web/serial
backend only has to provide its own ``Transport`` (and matching ``Device``).
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """The minimal pipe a :class:`~autotest.device.Device` needs to a target."""

    #: Short name of the transport, e.g. ``"adb"``.
    name: str

    def shell(self, args: list[str], timeout: int = 30) -> str:
        """Run a command on the device and return its stdout."""
        ...

    def screencap(self, path: str) -> None:
        """Capture the device screen to a PNG at ``path`` on the host."""
        ...

    def install(self, apk: str) -> bool:
        """Install an app package; return whether it succeeded."""
        ...

    def push(self, local: str, remote: str) -> None:
        """Copy a host file to a path on the device."""
        ...

    def pull(self, remote: str, local: str) -> None:
        """Copy a file from the device to a host path."""
        ...

    def reverse(self, remote_port: int, local_port: int | None = None) -> None:
        """Forward ``localhost:remote_port`` on the device to the host's port."""
        ...

    def reverse_remove(self, remote_port: int) -> None:
        """Remove a :meth:`reverse` tunnel."""
        ...

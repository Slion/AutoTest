"""autotest — a small, platform-agnostic device-automation and test framework.

Tests and tools talk to a :class:`~autotest.device.Device` (semantic,
platform-neutral calls); each ``Device`` is backed by a
:class:`~autotest.transport.Transport` (the pipe to the target). Today the only
implementation is Android over adb
(:class:`~autotest.android.device.AndroidDevice` / :class:`~autotest.android.adb.AdbTransport`),
but every test is written to the ``Device`` contract, so adding another platform
is additive — no test changes.

The framework is deliberately app-agnostic. It ships a generic
:class:`~autotest.runner.Runner` (named feature groups, per-test tab hygiene,
orientation handling, live notifications) and a
:mod:`autotest.results` recorder (per device-model + configuration, with
regression comparison). An **app profile** — a ``Device`` subclass that knows the
concrete app under test (its package, activities, address bar, tabs, …) — plugs
in on top; see the host repo's Fulguris profile for a reference.

Public surface::

    from autotest import Device, AndroidDevice, keys, Runner, Suite, resolve_android_devices
    for device in resolve_android_devices(serial, use_all, package):
        device.key(keys.DPAD_DOWN)

Example (host-side)::

    suite = Suite(tests=ALL_TESTS, descriptions=TEST_DESCRIPTIONS, groups=FEATURE_GROUPS)
    runner = Runner(resolve_android_devices, suite, results_dir="...")
    sys.exit(runner.run(device=args.device, use_all=args.all, group=args.group, ...))
"""
from __future__ import annotations

from . import keys, results  # noqa: F401
from .device import Device
from .node import Node
from .session import Session
from .transport import Transport
from .android.adb import resolve_devices as _adb_resolve_devices
from .android.device import AdbTransport, AndroidDevice
from .runner import Runner, Suite, select_tests

__all__ = [
    "keys",
    "results",
    "Device",
    "Node",
    "Session",
    "Transport",
    "AdbTransport",
    "AndroidDevice",
    "Runner",
    "Suite",
    "select_tests",
    "resolve_android_devices",
    "ORIENTATIONS",
]

# Re-export the orientation names the runner's --orientation flag accepts.
from .android.adb import ORIENTATIONS  # noqa: E402


def resolve_android_devices(device: str | None, use_all: bool,
                            package: str | None = None,
                            package_substring: str = "fulguris",
                            prefer: tuple[str, ...] = (".agent.", ".debug")) -> list[AndroidDevice]:
    """Resolve the selected target(s) into :class:`AndroidDevice` objects.

    Mirrors the adb device selection (exiting with a message when ambiguous).
    When ``package`` is None it is auto-detected per device: the installed
    package whose id contains ``package_substring`` (preferring ``prefer``
    suffixes when several match). Pass ``package`` explicitly to skip detection.
    This is the default device resolver for Android-based runs.
    """
    from .android import adb
    serials = _adb_resolve_devices(device, use_all)
    devices: list[AndroidDevice] = []
    for serial in serials:
        pkg = package or adb.detect_package(serial, package_substring, prefer)
        devices.append(AndroidDevice(serial, pkg))
    return devices

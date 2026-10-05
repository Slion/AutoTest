"""Android-over-adb backend for :mod:`autotest`.

Exposes the low-level :mod:`autotest.android.adb` driver and the
:class:`~autotest.android.device.AndroidDevice` platform implementation of the
generic :class:`~autotest.device.Device` contract.
"""
from __future__ import annotations

from . import adb
from .device import AdbTransport, AndroidDevice

__all__ = ["adb", "AdbTransport", "AndroidDevice"]

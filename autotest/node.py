"""The UI-hierarchy node shape returned by :meth:`Device.nodes`.

Defined here (in the contract layer, not the Android backend) so both the
platform-neutral :class:`~autotest.device.Device` contract and the Android
backend that produces nodes share one type without the contract depending on any
implementation.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Node:
    """One element of a device's UI hierarchy.

    ``bounds`` is ``(x1, y1, x2, y2)`` in screen pixels, or ``None`` when the
    backend did not report a geometry. ``content_desc`` is the accessibility
    description (empty when unset); ``enabled`` mirrors the platform flag.
    """

    resource_id: str
    cls: str
    text: str
    focused: bool
    bounds: tuple[int, int, int, int] | None
    content_desc: str = ""
    enabled: bool = True

    @property
    def center(self) -> tuple[int, int] | None:
        """The node's screen center, or ``None`` without bounds."""
        if not self.bounds:
            return None
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

"""Platform-neutral key symbols used by tests and tools.

Tests refer to keys by these semantic names (``keys.DPAD_CENTER``) rather than
raw platform codes, so a test reads the same on any platform. The integer values
happen to be Android ``KeyEvent`` codes today — the only platform we drive — and
a future platform's ``Device`` implementation is responsible for mapping these
symbols onto its own input system.
"""
from __future__ import annotations

BACK = 4
HOME = 3
DPAD_UP = 19
DPAD_DOWN = 20
DPAD_LEFT = 21
DPAD_RIGHT = 22
DPAD_CENTER = 23
ENTER = 66
SEARCH = 84
BUTTON_A = 96
BUTTON_L1 = 102  # gamepad left shoulder (LB)
BUTTON_R1 = 103  # gamepad right shoulder (RB)
APP_SWITCH = 187
MEDIA_FAST_FORWARD = 90
MEDIA_PLAY_PAUSE = 85
MEDIA_REWIND = 89

#: Key codes used for common chords (CTRL+TAB, CTRL+W, …).
KEY_CTRL_LEFT = 113
KEY_TAB = 61
KEY_CTRL_W = 51  # closes the current tab in a tabbed app
KEY_B = 30  # letter B (CTRL+B chords, e.g. "add bookmark")
DEL = 67  # backspace (deletes the selection when one is active)
A = 29  # letter A (CTRL+A = select all)

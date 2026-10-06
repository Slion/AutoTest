"""Generic Android-over-adb driver.

Pure standard library so it runs anywhere Python 3 is available. This is the
platform backend of :mod:`autotest`: it exposes the *generic* adb plumbing
(device discovery, raw shell, key/tap/type input, the UI hierarchy, the display
and orientation) with no assumption about *which app* is under test.

App-specific behavior (which package to launch, which activity, which views make
the UI "ready", the address bar, tabs, an in-app cursor, …) is NOT here — an app
profile (see ``fulguris`` in the host repo) layers that on top of
:class:`~autotest.android.device.AndroidDevice`.

This module is the low-level function layer; :class:`~autotest.android.device.AndroidDevice`
presents it as the object-oriented, platform-neutral surface the framework and
tests use.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# --- Key codes used for chords (see autotest.keys for the semantic symbols) --
KEY_CTRL_LEFT = 113
KEY_TAB = 61
KEY_CTRL_W = 51  # closes the current tab in a tabbed app
KEY_MOVE_END = 123
KEY_DEL = 67

# User-friendly product names for known devices, keyed by ro.product.model.
# ro.product.brand gives the vendor but Android exposes no marketing name via
# getprop, so known models are mapped here.
FRIENDLY_NAMES = {
    "SM-A225F": "Galaxy A22 5G",
    "Pi Compute Module 5 Rev 1.0": "Raspberry Pi 5 TV box",
}


def product_name(model: str) -> str:
    """User-friendly name for a device model, falling back to the model string."""
    return FRIENDLY_NAMES.get(model, model)


def device_label(serial: str) -> str:
    model = _adb(serial, ["shell", "getprop", "ro.product.model"]).strip()
    name = product_name(model)
    return f"{serial} ({name})" if name else serial


# --- adb plumbing ----------------------------------------------------------


def _adb(serial: str | None, args: list[str], timeout: int = 30) -> str:
    """Run an adb command, retrying a few times on timeout / transient failure.

    Network adb devices (e.g. an Android TV over Wi-Fi) can be slow enough for
    commands like `uiautomator dump` to intermittently time out, so we retry
    before giving up rather than letting one slow round trip fail a test.
    """
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += args
    last_error: Exception | None = None
    for _ in range(3):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            out = result.stdout or ""
            if "offline" in out or "error: device" in out or "no devices" in out:
                # Connection dropped; retry so the device has a moment to come back.
                time.sleep(1.0)
                continue
            return out
        except subprocess.TimeoutExpired as e:
            last_error = e
            time.sleep(1.0)
    if last_error:
        raise last_error
    return ""


def list_devices() -> list[str]:
    out = _adb(None, ["devices"])
    devices = []
    for line in out.splitlines()[1:]:
        line = line.strip()
        if line and "\tdevice" in line:
            devices.append(line.split("\t", 1)[0])
    return devices


def resolve_devices(device: str | None, use_all: bool) -> list[str]:
    """Resolve which devices to act on, exiting with a message when ambiguous."""
    connected = list_devices()
    if not connected:
        print("No devices connected over adb.")
        sys.exit(2)
    if device:
        if device not in connected:
            print(f"Device {device} not found. Connected: {', '.join(connected)}")
            sys.exit(2)
        return [device]
    if use_all or len(connected) == 1:
        return connected
    print("Multiple devices connected; pass --device SERIAL or --all:")
    for d in connected:
        print(f"  {device_label(d)}")
    sys.exit(2)


def detect_package(serial: str, contains: str,
                   prefer: tuple[str, ...] = (".agent.", ".debug")) -> str:
    """The installed package whose id contains ``contains``.

    When several match (e.g. an agent build and a plain debug build) the first
    suffix in ``prefer`` that appears wins, then the first match otherwise.
    Raises ``LookupError`` if nothing matches.
    """
    out = _adb(serial, ["shell", "pm", "list", "packages", contains])
    packages = [l.replace("package:", "").strip() for l in out.splitlines() if l.strip()]
    if not packages:
        raise LookupError(f"no installed package matching {contains!r} on {serial}")
    for suffix in prefer:
        hit = [p for p in packages if suffix in p]
        if hit:
            return hit[0]
    return packages[0]


def install_apk(serial: str, apk: str) -> bool:
    result = subprocess.run(
        ["adb", "-s", serial, "install", "-r", "-t", apk],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    return "Success" in (result.stdout or "")


def push(serial: str, local: str, remote: str) -> None:
    _adb(serial, ["push", local, remote])


def pull(serial: str, remote: str, local: str) -> None:
    _adb(serial, ["pull", remote, local])


def force_stop(serial: str, package: str) -> None:
    _adb(serial, ["shell", "am", "force-stop", package])


_leanback_cache: dict[str, bool] = {}


def is_leanback(serial: str) -> bool:
    """True if the device advertises the Android TV (leanback) system feature."""
    if serial not in _leanback_cache:
        out = _adb(serial, ["shell", "pm", "list", "features"])
        _leanback_cache[serial] = "android.software.leanback" in out
    return _leanback_cache[serial]


def screen_size(serial: str) -> tuple[int, int]:
    """Logical (app) screen size in pixels, parsed from `wm size`.

    `input tap` / `keyevent` use LOGICAL coordinates, which is the *override* size when one is
    set (e.g. a 4K panel driven at 1080p: `Physical size: 3840x2160` / `Override size: 1920x1080`)
    and the *physical* size otherwise. The physical line is printed first, so taking the first
    ``NxN`` would tap the wrong place on such a device (a "center" tap lands off-page). Prefer an
    override when present, then the physical size, then any size. Falls back to a sane default.
    """
    out = _adb(serial, ["shell", "wm", "size"])
    m = re.search(r"Override size:\s*(\d+)x(\d+)", out)
    if not m:
        m = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
    if not m:
        m = re.search(r"(\d+)x(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (1920, 1080)


def foreground_package(serial: str) -> str | None:
    """Package of the top resumed (foreground) activity, if any.

    Handles both dumpsys formats: newer Android prints ``mResumedActivity=``
    while Android 11 (e.g. the SHIELD TV) prints ``mResumedActivity:`` — the
    separator is ``=`` or ``:`` (the colon form must not be confused with the
    ``ActivityRecord{... u0 pkg/act`` token, which is why the package is
    captured from after the ``u0`` user marker).
    """
    out = _adb(serial, ["shell", "dumpsys", "activity", "activities"])
    m = re.search(r"(?:topResumedActivity|mResumedActivity)[=:].*?([\w.]+)/", out)
    return m.group(1) if m else None


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0, interval: float = 0.3) -> bool:
    """Poll a predicate until it is true or the timeout elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def start_component(serial: str, component: str, action: str | None = None, wait: float = 2.0) -> None:
    """Start an activity by its fully-qualified ``component``.

    ``component`` is ``package/.Activity`` or ``package/package.Activity``. This
    can open a specific (e.g. secondary/settings) activity. An optional custom
    intent ``action`` is added. Waits ``wait`` seconds before returning.
    """
    cmd = ["shell", "am", "start", "-n", component]
    if action:
        cmd += ["-a", action]
    _adb(serial, cmd)
    time.sleep(wait)


# --- Device notification (optional test progress indicator) ----------------
# ``cmd notification post`` posts as the shell uid; re-posting the same tag
# updates the notification in place, so a run always owns exactly one.

DEFAULT_NOTIFY_PKG = "com.android.shell"
DEFAULT_NOTIFY_TAG = "autotest-run"
DEFAULT_NOTIFY_ID = 2020


def post_notification(serial: str, text: str, *,
                      pkg: str = DEFAULT_NOTIFY_PKG, tag: str = DEFAULT_NOTIFY_TAG,
                      nid: int = DEFAULT_NOTIFY_ID, title: str = "Tests") -> None:
    """Post/replace a single device notification that shows progress.

    The same tag is re-posted so the text updates in place rather than stacking
    one notification per update. Best effort (callers swallow errors).
    """
    text = text.replace("'", "")
    _adb(serial, ["shell", f"cmd notification post -t '{title}' {tag} '{text}'"])


def dismiss_notification(serial: str, *,
                         pkg: str = DEFAULT_NOTIFY_PKG, tag: str = DEFAULT_NOTIFY_TAG,
                         nid: int = DEFAULT_NOTIFY_ID) -> None:
    """Cancel a notification posted by :func:`post_notification`.

    ``cmd notification`` has no cancel subcommand, so this calls
    ``INotificationManager.cancelNotificationWithTag(pkg, opPkg, tag, id, userId)``
    directly via ``service call``. That is binder transaction 8 — the position of
    the method in the AIDL, verified identical on Android 13 and 16.
    """
    _adb(serial, ["shell",
                  f"service call notification 8 s16 {pkg} s16 {pkg} "
                  f"s16 {tag} i32 {nid} i32 0"])


def view_present(serial: str, view_id: str) -> bool:
    """Fast check whether a view with the given resource id is in the top activity.

    Uses `dumpsys activity top` (~0.2s) instead of a full uiautomator dump
    (1-3s). Matches the `app:id/<view_id>` token in a view line.
    """
    out = _adb(serial, ["shell", "dumpsys", "activity", "top"])
    return f"app:id/{view_id}" in out


# --- Input ------------------------------------------------------------------


def key(serial: str, keycode: int, wait: float = 0.5) -> None:
    _adb(serial, ["shell", "input", "keyevent", str(keycode)])
    time.sleep(wait)


def key_longpress(serial: str, keycode: int, wait: float = 0.8) -> None:
    """Send a system long-press key event (sets FLAG_LONG_PRESS).

    The key is held for the *system* long-press timeout only (~400-500 ms),
    which is inside the "hesitant click" territory — use :func:`key_hold` for a
    deliberate, arbitrary hold.
    """
    _adb(serial, ["shell", "input", "keyevent", "--longpress", str(keycode)])
    time.sleep(wait)


_api_levels: dict = {}


def _api_level(serial: str) -> int:
    """The device's API level, cached per serial (one extra shell round trip per run)."""
    if serial not in _api_levels:
        _api_levels[serial] = int(_adb(serial, ["shell", "getprop", "ro.build.version.sdk"]).strip() or 0)
    return _api_levels[serial]


def key_hold(serial: str, keycode: int, ms: int, wait: float = 0.3) -> None:
    """Press and hold a key for ``ms`` milliseconds, then release it.

    Unlike :func:`key_longpress` (capped at the system long-press timeout), this
    holds for an *arbitrary* duration, so a test can produce either a
    deliberately hesitant-but-short press or a deliberate long hold.

    The call blocks for the hold duration (both paths do):

    - Android 14+ (API 34): ``input keyevent --duration <ms>``.
    - Older: ``input keycombination -t <ms> <CTRL_LEFT> <key>`` — a single-key chord held
      for ``ms``; CTRL_LEFT is an inert partner for a lone key.
    """
    if _api_level(serial) >= 34:
        _adb(serial, ["shell", "input", "keyevent", "--duration", str(ms), str(keycode)], timeout=max(30, ms // 1000 + 5))
    else:
        _adb(serial, ["shell", "input", "keycombination", "-t", str(ms), str(KEY_CTRL_LEFT), str(keycode)], timeout=max(30, ms // 1000 + 5))
    time.sleep(wait)


def tap(serial: str, x: int, y: int, wait: float = 0.7) -> None:
    _adb(serial, ["shell", "input", "tap", str(x), str(y)])
    time.sleep(wait)


def tap_longpress(serial: str, x: int, y: int, hold_ms: int = 800, wait: float = 1.0) -> None:
    """Touch-down at (x, y), hold ``hold_ms``, touch-up — a touch long-press.

    ``input swipe x y x y <ms>`` is the reliable way to synthesize a held
    touch (a plain ``input tap`` cannot hold); used for context menus on
    list rows and similar long-press UI.
    """
    _adb(serial, ["shell", "input", "swipe", str(x), str(y), str(x), str(y), str(hold_ms)])
    time.sleep(wait)


def key_combination(serial: str, *keycodes: int, wait: float = 0.6) -> None:
    """Send a chord of keys pressed together (e.g. CTRL+TAB).

    Uses `input keycombination` (Android 10+), which is the only reliable way to
    deliver a modified key like CTRL+TAB over adb; plain `input keyevent` cannot
    hold a modifier down across another key.
    """
    _adb(serial, ["shell", "input", "keycombination", *[str(k) for k in keycodes]])
    time.sleep(wait)


def ctrl_tab(serial: str, wait: float = 0.9) -> None:
    """Switch to the next/most-recent tab with CTRL+TAB, as a keyboard user would."""
    key_combination(serial, KEY_CTRL_LEFT, KEY_TAB, wait=wait)


def type_text(serial: str, text: str, wait: float = 0.5) -> None:
    """Type ``text`` into the focused field.

    Spaces become ``%s`` (``input text``'s convention) and the payload is
    single-quoted for the device shell, so shell metacharacters in the text
    (``& < > "`` …) are typed literally instead of being interpreted.
    """
    payload = text.replace(" ", "%s").replace("'", "'\\''")
    _adb(serial, ["shell", "input", "text", f"'{payload}'"])
    time.sleep(wait)


def clear_field(serial: str) -> None:
    """Clear the focused edit field: move to the end then delete a generous number of chars.

    Uses the `input keyevent -n <count>` repeat flag so the deletes are one adb
    call instead of one per key (161 round trips -> 2), which is much faster on
    Windows where each adb invocation is a subprocess.
    """
    _adb(serial, ["shell", "input", "keyevent", str(KEY_MOVE_END)])  # KEYCODE_MOVE_END
    _adb(serial, ["shell", "input", "keyevent", "-n", "160", str(KEY_DEL)])  # KEYCODE_DEL x160
    time.sleep(0.3)


# --- Raw input-device injection --------------------------------------------
# For driving input that `input keyevent` cannot: a gamepad's virtual D-pad (an
# ABS_HAT axis) or analog sticks. `getevent -pl` lists the input nodes and their
# capabilities; `sendevent` injects raw events into one.

EV_KEY, EV_ABS, EV_SYN = 0x01, 0x03, 0x01
SYN_REPORT = 0x00
ABS_HAT0X, ABS_HAT0Y = 0x16, 0x17


def input_devices(serial: str) -> list[tuple[str, str]]:
    """The device input nodes as ``[(node_path, name), ...]`` (from ``getevent -pl``)."""
    out = _adb(serial, ["shell", "getevent", "-pl"], timeout=30)
    devices: list[tuple[str, str]] = []
    node: str | None = None
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("add device"):
            node = s.rsplit(":", 1)[1].strip()
        elif node and s.startswith("name:"):
            devices.append((node, s.split(":", 1)[1].strip().strip('"')))
            node = None
    return devices


def find_input_node(serial: str, name: str) -> str | None:
    """The input node (``/dev/input/eventN``) of the first device whose name contains ``name``."""
    for node, dev in input_devices(serial):
        if name.lower() in dev.lower():
            return node
    return None


def _inject_hat(serial: str, node: str, x: int, y: int) -> None:
    """Set the hat axis to (x, y) in one shell round trip."""
    def ev(t: int, code: int, val: int) -> str:
        return f"sendevent {node} {t:04x} {code:04x} {val & 0xffff:04x}"
    _adb(serial, ["shell", " && ".join(
        [ev(EV_ABS, ABS_HAT0X, x), ev(EV_ABS, ABS_HAT0Y, y), ev(EV_SYN, SYN_REPORT, 0)]
    )], timeout=20)


def inject_hat_press(serial: str, node: str, x: int, y: int = 0, wait: float = 0.8) -> None:
    """Push a gamepad D-pad hat (ABS_HAT0X/0Y, -1..1) one step and release it.

    The input reader turns the hat axis into a virtual D-pad key press (one DOWN/UP
    pair, no auto-repeat), so this delivers a D-pad key event *from that device* —
    something `input keyevent` cannot do. One shell round trip per half.
    """
    _inject_hat(serial, node, x, y)
    time.sleep(0.15)  # let the down propagate, then release
    _inject_hat(serial, node, 0, 0)
    time.sleep(wait)


def inject_hat_hold(serial: str, node: str, x: int, y: int, hold: float, wait: float = 0.5) -> None:
    """Hold a gamepad D-pad hat pushed for ``hold`` seconds, then release it.

    The input reader keeps the virtual D-pad key DOWN for the whole hold (no
    auto-repeat), which is exactly what a continuous-movement consumer expects.
    """
    _inject_hat(serial, node, x, y)
    time.sleep(hold)
    _inject_hat(serial, node, 0, 0)
    time.sleep(wait)


# --- UI hierarchy -----------------------------------------------------------


def dump_ui(serial: str) -> str:
    _adb(serial, ["shell", "uiautomator", "dump", "/sdcard/w.xml"], timeout=30)
    return _adb(serial, ["shell", "cat", "/sdcard/w.xml"])


@dataclass
class Node:
    resource_id: str
    cls: str
    text: str
    focused: bool
    bounds: tuple[int, int, int, int] | None
    content_desc: str = ""
    enabled: bool = True

    @property
    def center(self) -> tuple[int, int] | None:
        if not self.bounds:
            return None
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2


def _parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value or "")
    if not m:
        return None
    return tuple(int(g) for g in m.groups())  # type: ignore[return-value]


def nodes(serial: str) -> list[Node]:
    xml = dump_ui(serial)
    result: list[Node] = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return result
    for el in root.iter("node"):
        result.append(
            Node(
                resource_id=el.get("resource-id", ""),
                cls=el.get("class", ""),
                text=el.get("text", ""),
                focused=el.get("focused", "false") == "true",
                bounds=_parse_bounds(el.get("bounds", "")),
                content_desc=el.get("content-desc", ""),
                enabled=el.get("enabled", "true") == "true",
            )
        )
    return result


def find_node(serial: str, id_suffix: str) -> Node | None:
    for n in nodes(serial):
        if n.resource_id.endswith(id_suffix):
            return n
    return None


def find_node_by_text(serial: str, text: str) -> Node | None:
    for n in nodes(serial):
        if n.text == text:
            return n
    return None


def webview_focused(serial: str) -> bool:
    for n in nodes(serial):
        if n.cls == "android.webkit.WebView" and n.focused:
            return True
    return False


def ime_shown(serial: str) -> bool:
    out = _adb(serial, ["shell", "dumpsys", "input_method"])
    m = re.search(r"mInputShown=(\w+)", out)
    return bool(m and m.group(1) == "true")


def dropdown_present(serial: str) -> bool:
    """True if a suggestion list popup window is present.

    We look at the window list rather than the view hierarchy because uiautomator
    does not reliably capture the dropdown popup window while the keyboard is up.
    """
    out = _adb(serial, ["shell", "dumpsys", "window", "windows"])
    return "PopupWindow" in out


def screenshot(serial: str, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        cmd = ["adb", "-s", serial, "exec-out", "screencap", "-p"]
        f.write(subprocess.run(cmd, capture_output=True).stdout)


def logcat(serial: str, grep: str, clear: bool = False) -> str:
    """Dump the (optionally pre-cleared) device log, keeping only lines matching ``grep``."""
    if clear:
        _adb(serial, ["logcat", "-c"], timeout=15)
    out = _adb(serial, ["logcat", "-d"], timeout=60)
    return "\n".join(l for l in out.splitlines() if grep in l)


# --- Orientation & device configuration ------------------------------------
# A device's "configuration" is orientation + rotation + smallest-width-dp.
# Foldables get distinct configs because smallestScreenWidthDp changes between
# inner/outer screens. The helpers below force the device orientation and report
# the same triplet so a test run can be recorded against the exact configuration
# it ran in.

# adb user_rotation values (Surface rotation constants).
ROTATION_0 = 0    # natural
ROTATION_90 = 1
ROTATION_180 = 2
ROTATION_270 = 3

# Orientation names accepted by set_orientation / the runner's --orientation flag.
ORIENTATIONS = ("portrait", "landscape", "sensor")


def _int(value: str, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def wm_size(serial: str) -> tuple[int, int]:
    """Physical (natural-orientation) display size in pixels, e.g. (1080, 2340)."""
    out = _adb(serial, ["shell", "wm", "size"])
    m = re.search(r"Physical size:\s*(\d+)x(\d+)", out)
    if not m:
        m = re.search(r"(\d+)x(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def wm_density(serial: str) -> int:
    """Display density in dpi (e.g. 420). Falls back to 160 (1x) if unknown."""
    out = _adb(serial, ["shell", "wm", "density"])
    m = re.search(r"Physical density:\s*(\d+)", out)
    if not m:
        m = re.search(r"(\d+)", out)
    return int(m.group(1)) if m else 160


def user_rotation(serial: str) -> int:
    """Current forced display rotation as a 0..3 Surface constant."""
    return _int(_adb(serial, ["shell", "settings", "get", "system", "user_rotation"]), 0)


def auto_rotate(serial: str) -> bool:
    """Whether accelerometer (auto) rotation is enabled."""
    return _int(_adb(serial, ["shell", "settings", "get", "system", "accelerometer_rotation"]), 0) == 1


def orientation_state(serial: str) -> tuple[int, int]:
    """Snapshot (accelerometer_rotation, user_rotation) so it can be restored later."""
    accel = _int(_adb(serial, ["shell", "settings", "get", "system", "accelerometer_rotation"]), 0)
    return accel, user_rotation(serial)


def restore_orientation(serial: str, accel: int, rotation: int) -> None:
    """Restore a state captured by orientation_state()."""
    _adb(serial, ["shell", "settings", "put", "system", "user_rotation", str(rotation)])
    _adb(serial, ["shell", "settings", "put", "system", "accelerometer_rotation", str(accel)])


def set_orientation(serial: str, orientation: str, wait: float = 1.5) -> None:
    """Force the device orientation.

    "portrait"/"landscape" disable auto-rotate and pin the display so the natural
    orientation ends up portrait/landscape respectively (works on both
    portrait-native phones and landscape-native TVs/tablets). "sensor" re-enables
    auto-rotation. Devices that ignore user_rotation (e.g. a fixed-orientation
    TV) are left as-is.
    """
    if orientation == "sensor":
        _adb(serial, ["shell", "settings", "put", "system", "accelerometer_rotation", "1"])
        time.sleep(wait)
        return
    if orientation not in ("portrait", "landscape"):
        raise ValueError(f"unknown orientation '{orientation}'")
    phys_w, phys_h = wm_size(serial)
    natural_landscape = phys_w > phys_h
    want_landscape = orientation == "landscape"
    # Rotate 90° from natural when the natural orientation is the opposite one.
    rotation = ROTATION_90 if (want_landscape != natural_landscape) else ROTATION_0
    _adb(serial, ["shell", "settings", "put", "system", "accelerometer_rotation", "0"])
    _adb(serial, ["shell", "settings", "put", "system", "user_rotation", str(rotation)])
    time.sleep(wait)


def smallest_width_dp(serial: str) -> int:
    """Smallest screen width in dp (density-independent), matching Android's swNNN.

    This does not change with rotation, so it distinguishes device/screen classes
    (e.g. a foldable's inner vs outer screen).
    """
    w, h = wm_size(serial)
    density = wm_density(serial)
    if density <= 0:
        density = 160
    return round(min(w, h) / (density / 160.0))


def device_config(serial: str) -> dict:
    """Describe the device and its current screen configuration.

    Returns orientation/rotation/smallest_width_dp plus a ``config_id`` string of
    the form ``landscape-90-sw360``, so runs can be grouped and compared per
    configuration.
    """
    phys_w, phys_h = wm_size(serial)
    natural_landscape = phys_w > phys_h
    rot = user_rotation(serial)
    rotated = rot in (ROTATION_90, ROTATION_270)
    is_landscape = natural_landscape != rotated
    orientation = "landscape" if is_landscape else "portrait"
    sw_dp = smallest_width_dp(serial)
    model = _adb(serial, ["shell", "getprop", "ro.product.model"]).strip()
    brand = _adb(serial, ["shell", "getprop", "ro.product.brand"]).strip()
    android = _adb(serial, ["shell", "getprop", "ro.build.version.release"]).strip()
    return {
        "serial": serial,
        "model": model,
        "brand": brand.title() if brand else "",
        "product_name": product_name(model),
        "android": android,
        "orientation": orientation,
        "rotation": rot * 90,
        "smallest_width_dp": sw_dp,
        "auto_rotate": auto_rotate(serial),
        "config_id": f"{orientation}-{rot * 90}-sw{sw_dp}",
    }


# --- View visibility (fast, via dumpsys) -----------------------------------


def view_flag(serial: str, view_id: str) -> str | None:
    """First character of the FLAGS field for a view in ``dumpsys activity top``.

    ``V``=visible, ``I``=invisible, ``G``=gone. Returns ``None`` if the view is not
    in the dump. Much faster than a uiautomator dump (~0.2s vs several seconds), so
    it is the cheap way to ask "is this specific view shown right now?".
    """
    out = _adb(serial, ["shell", "dumpsys", "activity", "top"])
    m = re.search(
        r"\{[0-9a-f]+ ([VIG])[A-Z.]* [A-Z.]* \d+,\d+-\d+,\d+ #[0-9a-f]+ app:id/" + re.escape(view_id) + r"\}",
        out,
    )
    return m.group(1) if m else None


def view_visible(serial: str, view_id: str) -> bool:
    """True if the view with resource id ``view_id`` is currently VISIBLE."""
    return view_flag(serial, view_id) == "V"


def broadcast(serial: str, package: str, action: str,
              float_extras: dict | None = None, wait: float = 0.0) -> None:
    """Send a broadcast to a package, optionally carrying float extras.

    A generic way to reach an app's debug receivers (e.g. "teleport the cursor to
    (x, y)"). ``float_extras`` maps extra names to numeric values, sent as ``--ef``.
    """
    args = ["shell", "am", "broadcast", "-p", package, "-a", action]
    for name, value in (float_extras or {}).items():
        args += ["--ef", name, str(float(value))]
    _adb(serial, args)
    if wait:
        time.sleep(wait)


# --- Multi-display & standalone-tooling layer --------------------------------
#
# The functions above assume a single default display. Multi-display hosts
# (dual-screen phones, foldables, dock add-ons) need per-display addressing:
# `input -d <id>`, `screencap -d <id>`, `uiautomator dump --display <id>` and
# the real display id (which is NOT the enumeration order — add-on displays get
# non-contiguous ids that churn across connect/disconnect). These helpers are
# app-agnostic; a device that hosts several logical displays targets them by id.
#
# :class:`Adb` is the object-oriented facade over the same set, kept for the
# standalone diagnostic scripts that drive a device directly (they read ``a.tap``,
# ``a.logcat_dump``, ``a.displays`` …). Both surfaces call the same primitives.


def _raw(serial: str | None, args: list, binary: bool = False, check: bool = False,
         timeout: int = 60):
    """Run a raw adb command and return its stdout (str, or bytes if ``binary``).

    Unlike :func:`_adb` this does not retry on a transient ``offline`` — it is
    the low pipe the :class:`Adb` facade and the byte-oriented helpers use, and
    it raises :class:`subprocess.TimeoutExpired` on timeout (matching how the
    standalone tools surface a hung command).
    """
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += [str(a) for a in args]
    proc = subprocess.run(cmd, capture_output=True, text=not binary, timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"adb {' '.join(map(str, args))} failed "
                           f"(rc={proc.returncode}): {proc.stderr!r}")
    return proc.stdout


def shell_bytes(serial: str | None, cmd: str, timeout: int = 60) -> bytes:
    """Run an on-device shell command, returning raw stdout bytes (no text decode)."""
    return _raw(serial, ["shell", cmd], binary=True, timeout=timeout)


def install(serial: str | None, apk: str, flags: tuple = ("-r",)) -> str:
    """Install an APK; returns adb's stdout (contains ``Success`` on success)."""
    return _raw(serial, ["install", *flags, apk], timeout=300)


@dataclass(frozen=True)
class DisplayInfo:
    """One ``DisplayDeviceInfo`` row from ``dumpsys display``, plus current rotation."""
    display_id: int     # the REAL display id (screencap -d / input --display), not an index
    name: str
    width: int          # physical (native) width
    height: int         # physical (native) height
    rotation: int       # 0..3 (ROTATION_0/90/180/270), current
    state: str          # "ON"/"OFF"/"?"
    presentation: bool = False  # in the presentation (external/auxiliary) category

    @property
    def is_landscape(self) -> bool:
        return self.rotation in (1, 3)

    @property
    def logical_size(self) -> tuple[int, int]:
        """(w, h) in the CURRENT rotation -- what ``input`` coordinates use."""
        portrait = (min(self.width, self.height), max(self.width, self.height))
        return (portrait[1], portrait[0]) if self.is_landscape else portrait


def displays(serial: str | None) -> list[DisplayInfo]:
    """All displays in dump order, keyed by their REAL display id.

    The id comes from the ``mBaseDisplayInfo=DisplayInfo{"<name>", displayId N"``
    line (not the enumeration order): on add-on-display devices the ids are
    non-contiguous (e.g. 0, 12, 17) and churn across connect/disconnect, so an
    index-based id silently breaks ``screencap -d`` / ``input --display``. The
    ``FLAG_PRESENTATION`` flag from the same line is surfaced as ``presentation``
    — the stable signal for an external/auxiliary panel.
    """
    out = _adb(serial, ["shell", "dumpsys", "display"])
    blocks: list[dict] = []
    for line in out.splitlines():
        m = re.search(r'DisplayDeviceInfo\{"([^"]+)".*?(\d+) x (\d+)', line)
        if m:
            blocks.append({"name": m.group(1),
                           "size": (int(m.group(2)), int(m.group(3))),
                           "rotation": 0, "state": "?"})
        elif blocks:
            b = blocks[-1]
            if (r := re.search(r'mCurrentOrientation=(\d+)', line)):
                b["rotation"] = int(r.group(1))
            if "state ON" in line:
                b["state"] = "ON"
            elif "state OFF" in line:
                b["state"] = "OFF"
    # Second pass for ids/flags: the base-info line repeats the display name,
    # so it is joined onto the block with the same name (in dump order).
    for line in out.splitlines():
        m = re.search(r'mBaseDisplayInfo=DisplayInfo\{"([^"]+)", displayId (\d+)"', line)
        if not m:
            continue
        name, did = m.group(1), int(m.group(2))
        for b in blocks:
            if b["name"] == name and "display_id" not in b:
                b["display_id"] = did
                b["presentation"] = "FLAG_PRESENTATION" in line
                break
    return [DisplayInfo(b.get("display_id", i), b["name"], b["size"][0], b["size"][1],
                        b["rotation"], b["state"], b.get("presentation", False))
            for i, b in enumerate(blocks)]


def display_rotation(serial: str | None, display: int = 0) -> int:
    """Current rotation (0..3) of a display, from ``mCurrentOrientation``."""
    rots = re.findall(r'mCurrentOrientation=(\d+)', _adb(serial, ["shell", "dumpsys", "display"]))
    return int(rots[display]) if display < len(rots) else 0


def display_size(serial: str | None, display: int = 0) -> tuple[int, int]:
    """Current logical (w, h) of a display, accounting for rotation.

    Physical panel size comes from dumpsys (no per-device hard-coding); rotation
    comes from ``mCurrentOrientation``.
    """
    infos = displays(serial)
    rot = display_rotation(serial, display)
    if display < len(infos):
        w, h = infos[display].width, infos[display].height
    else:
        w, h = (infos[0].width, infos[0].height) if infos else (1080, 1920)
    portrait = (min(w, h), max(w, h))
    return (portrait[1], portrait[0]) if rot in (1, 3) else portrait


def resumed_activities(serial: str | None) -> list[str]:
    """``mResumedActivity`` components, one per display group (dump order)."""
    out = _adb(serial, ["shell", "dumpsys", "activity", "activities"])
    return [m.group(1) for line in out.splitlines()
            if (m := re.search(
                r'mResumedActivity:\s+ActivityRecord\{\S+ \S+ (\S+)', line))]


def focused_window(serial: str | None) -> str | None:
    """The focused window's package, from ``mCurrentFocus`` in ``dumpsys window``."""
    out = _adb(serial, ["shell", "dumpsys", "window", "displays"])
    focus = None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("mCurrentFocus="):
            val = line.split("=", 1)[1]
            if val and val != "null":
                m = re.search(r'u\d+\s+(\S+)', val)
                focus = m.group(1) if m else val
    return focus


def pidof(serial: str | None, package: str) -> str | None:
    """The pid(s) of ``package`` as a string, or None if not running."""
    out = _adb(serial, ["shell", "pidof", package]).strip()
    return out or None


def is_process_running(serial: str | None, package: str) -> bool:
    """True if ``package`` has a live process."""
    return pidof(serial, package) is not None


def start_activity_display(serial: str | None, component: str, *,
                           display: int | None = None, extras: dict | None = None,
                           flags: tuple = ()) -> str:
    """Start an activity, optionally pinned to a display and carrying extras."""
    args = ["am", "start"]
    if display is not None:
        args += ["--display", str(display)]
    args += list(flags)
    args += ["-n", component]
    for k, v in (extras or {}).items():
        if isinstance(v, bool):
            args += ["--ez", k, "true" if v else "false"]
        elif isinstance(v, int):
            args += ["--ei", k, str(v)]
        else:
            args += ["--es", k, str(v)]
    return _raw(serial, ["shell", " ".join(args)])


def remove_task(serial: str | None, task_id: int | str) -> str:
    """Remove a task from the recents stack.

    ``am task remove`` is not exposed on stock Android; this drives
    IActivityManager.removeTask (transaction 133) directly.
    """
    return _raw(serial, ["shell", f"service call activity 133 i32 {task_id} i32 0"])


def clear_stale_tasks(serial: str | None, package: str) -> list[int]:
    """Remove ``package``'s ghost entries from recents (only when not running).

    A force-stop leaves the task shells in recents (``hasTask=false``); call this
    after a stop to keep the list from accumulating one entry per run.
    """
    if is_process_running(serial, package):
        return []
    removed: list[int] = []
    out = _adb(serial, ["shell", "dumpsys", "activity", "recents"])
    for tid in re.findall(
        r"Recent #\d+: Task\{[0-9a-f]+ #(\d+)\s[^}]*" + re.escape(package) + r"[^}]*\}", out):
        remove_task(serial, tid)
        removed.append(int(tid))
    return removed


# --- Per-display synthetic input --------------------------------------------


def _disp_args(display: int | None) -> list[str]:
    return ["-d", str(display)] if display is not None else []


def tap_display(serial: str | None, x: int, y: int, display: int | None = 0) -> str:
    """Tap absolute coordinates on a specific display."""
    return _raw(serial, ["shell", " ".join(
        ["input", *_disp_args(display), "tap", str(int(x)), str(int(y))])])


def swipe_display(serial: str | None, x1: int, y1: int, x2: int, y2: int,
                  ms: int = 300, display: int | None = 0) -> str:
    """A quick swipe on a specific display (lifts instantly on arrival)."""
    return _raw(serial, ["shell", " ".join(
        ["input", *_disp_args(display), "swipe", str(int(x1)), str(int(y1)),
         str(int(x2)), str(int(y2)), str(int(ms))])])


def motionevent(serial: str | None, action: str, x: int, y: int,
                display: int | None = 0) -> str:
    """Send a raw pointer ``motionevent`` (``DOWN``/``MOVE``/``UP``) on a display."""
    return _raw(serial, ["shell", " ".join(
        ["input", *_disp_args(display), "motionevent", action, str(int(x)), str(int(y))])])


def keyevent_display(serial: str | None, code: int | str, display: int | None = 0,
                     longpress: bool = False) -> str:
    """Send a key event on a display. ``code`` is a keycode int or a ``KEYCODE_*`` name."""
    lp = "--longpress " if longpress else ""
    return _raw(serial, ["shell", " ".join(
        ["input", *_disp_args(display), "keyevent", lp, str(code)])])


def type_text_display(serial: str | None, text: str, display: int | None = 0) -> str:
    """Type text on a display (spaces become ``%s``, payload single-quoted)."""
    payload = text.replace(" ", "%s").replace("'", "'\\''")
    return _raw(serial, ["shell", " ".join(
        ["input", *_disp_args(display), "text", f"'{payload}'"])] )


def swipe_hold(serial: str | None, x1: int, y1: int, x2: int, y2: int, *,
               steps: int = 8, hold_ms: int = 500, move_ms: int = 40,
               display: int | None = 0) -> None:
    """A finger swipe that DWELLS at the end before lifting.

    ``input swipe`` lifts instantly, so it cannot express the "swipe up and
    pause" gesture Android's gesture-nav needs to open Overview. Drive it with
    explicit motionevents: DOWN, interpolated MOVEs, hold, then UP.
    """
    motionevent(serial, "DOWN", x1, y1, display=display)
    for i in range(1, steps + 1):
        mx = round(x1 + (x2 - x1) * i / steps)
        my = round(y1 + (y2 - y1) * i / steps)
        motionevent(serial, "MOVE", mx, my, display=display)
        time.sleep(move_ms / 1000.0)
    time.sleep(hold_ms / 1000.0)
    motionevent(serial, "UP", x2, y2, display=display)


def open_recents_gesture(serial: str | None, *, display: int | None = 0,
                         repeats: int = 1, settle_ms: int = 1200) -> None:
    """Open Recents/overview via a real swipe-up-and-hold on ``display``.

    Issued in the display's CURRENT logical coordinates, so it works for a
    landscape or portrait foreground app. Immersive/fullscreen apps hide the
    gesture bar, so a quick reveal swipe is sent first to surface the transient
    system bars; the follow-up swipe-up-and-hold is then seen by the system's
    overview gesture detector. The hold dwells near mid-screen (a full swipe to
    the top would trigger HOME instead of Overview).
    """
    for _ in range(max(1, repeats)):
        w, h = display_size(serial, display)
        cx = w // 2
        swipe_display(serial, cx, h - 1, cx, int(h * 0.6), 120, display=display)
        time.sleep(0.3)
        swipe_hold(serial, cx, h - 1, cx, int(h * 0.4), steps=10, hold_ms=700, display=display)
        time.sleep(settle_ms / 1000.0)


# --- Per-display UI hierarchy -----------------------------------------------


def ui_dump_display(serial: str | None, display: int | None = 0):
    """The raw ``uiautomator`` hierarchy root for a display, or None on failure.

    Dumps to a per-display temp file (the framework's ``/sdcard/w.xml`` is shared,
    which races between two displays), pulls it and parses it.
    """
    flag = f" --display {display}" if display else ""
    remote = f"/sdcard/uiauto_{display}.xml"
    _adb(serial, ["shell", f"uiautomator dump{flag} {remote}"], timeout=30)
    local = Path(tempfile.gettempdir()) / f"uiauto_{display}.xml"
    _raw(serial, ["pull", remote, str(local)])
    try:
        return ET.parse(local).getroot()
    except Exception:
        return None


def find_node_center(serial: str | None, text: str, *, display: int | None = 0,
                     exact: bool = False) -> tuple[int, int] | None:
    """Center of the first node on ``display`` whose text/content-desc matches ``text``."""
    root = ui_dump_display(serial, display)
    if root is None:
        return None
    for node in root.iter("node"):
        node_text = node.get("text", "")
        desc = node.get("content-desc", "")
        match = (node_text == text or desc == text) if exact else (
            text in node_text or text in desc)
        if match:
            m = re.match(r"\[(\d+),(\d+)]\[(\d+),(\d+)]", node.get("bounds", ""))
            if m:
                x1, y1, x2, y2 = map(int, m.groups())
                return ((x1 + x2) // 2, (y1 + y2) // 2)
    return None


# --- Per-display capture & logs ---------------------------------------------


def screencap_display(serial: str | None, display: int | None = 0,
                      out: str | Path | None = None) -> bytes:
    """Capture one display via screencap-to-file + pull (robust over TCP adb).

    Returns the PNG bytes; also writes them to ``out`` when given. Some firmwares
    yield 0 bytes for ``screencap -d <id>`` on non-zero displays — the caller
    must treat empty output as "no capture", not an error.
    """
    remote = f"/sdcard/adb_cap_{display}.png"
    _adb(serial, ["shell", f"screencap -d {display} {remote}"])
    local = Path(out) if out else Path(tempfile.gettempdir()) / f"adb_cap_{display}.png"
    _raw(serial, ["pull", remote, str(local)])
    try:
        return local.read_bytes()
    except FileNotFoundError:
        return b""


def logcat_clear(serial: str | None) -> None:
    """Clear all logcat buffers."""
    _raw(serial, ["logcat", "-c"], timeout=15)


def logcat_dump(serial: str | None, *, pid: str | None = None, tail: int | None = None,
                buffer: str | None = None) -> str:
    """A full logcat dump (``-d -v time``), optionally filtered by pid/tail/buffer.

    Read as bytes and decoded leniently: logcat is not guaranteed to be decodable
    by the console locale, and a stray byte must not take the whole dump down.
    """
    args = ["logcat", "-d", "-v", "time"]
    if buffer:
        args += ["-b", buffer]
    if pid:
        args += [f"--pid={pid}"]
    if tail:
        args += ["-t", str(tail)]
    return _raw(serial, args, binary=True, timeout=60).decode("utf-8", errors="replace")


def crash_log(serial: str | None) -> str:
    """The contents of the ``crash`` logcat buffer."""
    return logcat_dump(serial, buffer="crash")


def has_crashed(serial: str | None) -> bool:
    """True if the crash buffer holds a fatal exception / signal."""
    c = crash_log(serial)
    return "FATAL EXCEPTION" in c or "Fatal signal" in c


def default_device() -> str | None:
    """Pick a device without requiring any env var: env override, else the single
    attached device, else None (adb targets the sole device)."""
    env = os.environ.get("ANDROID_SERIAL")
    if env:
        return env
    devs = list_devices()
    return devs[0] if devs else None


class Adb:
    """A thin, fully-typed adb client bound to a single device.

    The object-oriented facade over the module functions above, for standalone
    diagnostic scripts that drive one device directly (``a.tap``, ``a.screencap``,
    ``a.logcat_dump``, ``a.displays`` …). Framework tests use the module
    functions / :class:`~autotest.android.device.AndroidDevice` instead.
    """

    def __init__(self, device: str | None = None, *, timeout: int = 60, verbose: bool = False):
        self.device = device if device is not None else default_device()
        self.timeout = timeout
        self.verbose = verbose

    # --- core plumbing -----------------------------------------------------

    def raw(self, *args: str, binary: bool = False, check: bool = False,
            timeout: int | None = None):
        """Run ``adb [-s <device>] <args...>``; return stdout (str or bytes)."""
        base = ["adb", "-s", self.device] if self.device else ["adb"]
        if self.verbose:
            print(f"    $ {' '.join([*base, *map(str, args)])}")
        return _raw(self.device, args, binary=binary, check=check,
                    timeout=timeout if timeout is not None else self.timeout)

    def shell(self, cmd: str, *, check: bool = False, timeout: int | None = None) -> str:
        return self.raw("shell", cmd, check=check, timeout=timeout)

    def shell_bytes(self, cmd: str, *, timeout: int | None = None) -> bytes:
        return self.raw("shell", cmd, binary=True, timeout=timeout)

    def push(self, local: str | Path, remote: str) -> str:
        return self.raw("push", str(local), remote)

    def pull(self, remote: str, local: str | Path) -> str:
        return self.raw("pull", remote, str(local))

    def install(self, apk: str, *, flags: tuple = ("-r",)) -> str:
        return install(self.device, apk, flags=flags)

    # --- app / activity control -------------------------------------------

    def start_activity(self, component: str, *, display: int | None = None,
                       extras: dict | None = None, flags: tuple = ()) -> str:
        return start_activity_display(self.device, component, display=display,
                                      extras=extras, flags=flags)

    def force_stop(self, package: str) -> str:
        return self.shell(f"am force-stop {package}")

    def remove_task(self, task_id: str | int) -> str:
        return remove_task(self.device, task_id)

    def clear_stale_tasks(self, package: str) -> list[int]:
        return clear_stale_tasks(self.device, package)

    def pidof(self, package: str) -> str | None:
        return pidof(self.device, package)

    def is_running(self, package: str) -> bool:
        return is_process_running(self.device, package)

    # --- synthetic input ---------------------------------------------------

    def tap(self, x: int, y: int, *, display: int | None = 0) -> str:
        return tap_display(self.device, x, y, display=display)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, ms: int = 300,
              *, display: int | None = 0) -> str:
        return swipe_display(self.device, x1, y1, x2, y2, ms, display=display)

    def motionevent(self, action: str, x: int, y: int, *, display: int | None = 0) -> str:
        return motionevent(self.device, action, x, y, display=display)

    def keyevent(self, code: str | int, *, display: int | None = 0, longpress: bool = False) -> str:
        return keyevent_display(self.device, code, display=display, longpress=longpress)

    def text(self, s: str, *, display: int | None = 0) -> str:
        return type_text_display(self.device, s, display=display)

    def swipe_hold(self, x1: int, y1: int, x2: int, y2: int, *, steps: int = 8,
                   hold_ms: int = 500, move_ms: int = 40, display: int | None = 0) -> None:
        return swipe_hold(self.device, x1, y1, x2, y2, steps=steps, hold_ms=hold_ms,
                          move_ms=move_ms, display=display)

    def press_home(self, *, display: int | None = 0) -> str:
        return self.keyevent("KEYCODE_HOME", display=display)

    def press_back(self, *, display: int | None = 0) -> str:
        return self.keyevent("KEYCODE_BACK", display=display)

    def press_app_switch(self, *, display: int | None = 0) -> str:
        return self.keyevent("KEYCODE_APP_SWITCH", display=display)

    def open_recents_gesture(self, *, display: int | None = 0, repeats: int = 1,
                             settle_ms: int = 1200) -> None:
        return open_recents_gesture(self.device, display=display, repeats=repeats,
                                    settle_ms=settle_ms)

    def tap_center(self, *, display: int | None = 0) -> str:
        w, h = self.display_size(display)
        return self.tap(w // 2, h // 2, display=display)

    # --- screenshots & UI hierarchy ----------------------------------------

    def screencap(self, display: int | None = 0, *, out: str | Path | None = None) -> bytes:
        return screencap_display(self.device, display, out=out)

    def ui_dump(self, display: int | None = 0):
        return ui_dump_display(self.device, display)

    def find_node_center(self, text: str, *, display: int | None = 0, exact: bool = False):
        return find_node_center(self.device, text, display=display, exact=exact)

    # --- displays & rotation -----------------------------------------------

    def displays(self) -> list[DisplayInfo]:
        return displays(self.device)

    def rotation(self, display: int = 0) -> int:
        return display_rotation(self.device, display)

    def display_size(self, display: int = 0) -> tuple[int, int]:
        return display_size(self.device, display)

    # --- activities & focus ------------------------------------------------

    def resumed_activities(self) -> list[str]:
        return resumed_activities(self.device)

    def resumed_activity_for(self, needle: str) -> str | None:
        for a in self.resumed_activities():
            if needle in a:
                return a
        return None

    def is_resumed(self, needle: str) -> bool:
        return self.resumed_activity_for(needle) is not None

    def focused_window(self) -> str | None:
        return focused_window(self.device)

    # --- logcat ------------------------------------------------------------

    def logcat_clear(self) -> None:
        return logcat_clear(self.device)

    def logcat_dump(self, *, pid: str | None = None, tail: int | None = None,
                    buffer: str | None = None) -> str:
        return logcat_dump(self.device, pid=pid, tail=tail, buffer=buffer)

    def crash_log(self) -> str:
        return crash_log(self.device)

    def has_crashed(self) -> bool:
        return has_crashed(self.device)

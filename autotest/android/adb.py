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
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

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

# AutoTest

A small, **platform-agnostic** device-automation and device-UI test framework.

Tests drive a :class:`autotest.device.Device` through high-level, semantic calls
(``device.key(keys.DPAD_DOWN)``, ``device.field_text()``) that carry no assumption
about *how* the device is reached. Each `Device` is backed by a
:class:`autotest.transport.Transport` (the pipe to the target). Today the only
backend is Android over adb, but every test is written to the `Device` contract,
so adding another platform is **additive — no test changes**.

The framework is deliberately **app-agnostic**. It ships a generic runner and a
results recorder; an **app profile** (a `Device` subclass that knows the concrete
app under test) plugs in on top.

## Layout

```
autotest/
├── keys.py          # platform-neutral key symbols (DPAD_CENTER, ENTER, …)
├── transport.py     # Transport protocol (the low-level pipe)
├── node.py          # the UI-hierarchy Node shape
├── device.py        # the generic Device contract (abstract)
├── session.py       # per-run state: restart policy + opened-unit (tab) hygiene
├── results.py       # per device-model + config persistence & regression diff
├── runner.py        # the group-based Runner + Suite
├── watch.py         # CrashWatcher — detect the app dying mid-run
└── android/
    ├── adb.py       # the generic Android-over-adb driver (pure stdlib)
    └── device.py    # AndroidDevice — the platform impl of Device
```

## The two layers

* **Generic (this package).** `Device`, `Transport`, `AndroidDevice`, the adb
  driver, `Session`, `Runner`, `Suite`, `results`. Nothing here knows about any
  particular app.
* **App profile (in the host repo).** Subclasses `AndroidDevice` to add the
  concrete app's behavior — its package and activities, how a URL is loaded in an
  address bar, the toolbar view ids, tab open/close, debug hooks, the progress
  notification. The Fulguris browser ships this as `FulgurisDevice`.

## Usage (host side)

```python
from autotest import Runner, Suite, AndroidDevice, keys

# 1. An app profile: a Device subclass that knows the app (see appdevice.py).
#    Here FulgurisDevice adds navigate()/field_text()/close_tabs()/…

# 2. Assemble the suites (tests are plain test_*(device, ctx) functions).
suite = Suite(tests=ALL_TESTS, descriptions=TEST_DESCRIPTIONS,
              groups=FEATURE_GROUPS, default_group="smoke")

# 3. A device resolver: (device, use_all, package) -> [Device]
def resolve(device, use_all, package):
    ...
    return [FulgurisDevice(serial, package) for serial in serials]

# 4. Run it.
runner = Runner(resolve, suite, results_dir=".../results")
sys.exit(runner.run(device=args.device, use_all=args.all,
                    group=args.group, restart=args.restart,
                    keep_tabs=args.keep_tabs, orientation=args.orientation,
                    notify=args.notify))
```

### Tests

A test is a plain function `test_<name>(device, ctx) -> None` that raises
`AssertionError` on failure. It only calls the generic `Device` API plus whatever
the app profile adds:

```python
def test_smoke_open_website(device, ctx):
    device.navigate("example.com")
    assert device.foreground_package() == device.package
    assert device.field_text(), "address bar should show the page label"
```
### Crash detection

The runner wraps every test in a `CrashWatcher`, which polls
`device.app_alive()` in a background thread. When the app process dies mid-test
the test is recorded as `CRASH` with the crash-log tail (`device.crash_evidence()`,
from the logcat crash buffer) and the run **stops for that device** — a dead app
makes every following test fail for the same reason. A test that ends with the
app process gone fails the same way, so a never-launching app is caught on the
first test instead of burning the whole suite. Tests must therefore leave the
app process running (restart it at the end if they need it stopped). Disable
with `watch_crashes=False` on `Runner.run()`.
Group it via each suite module's `FEATURE_GROUPS`; the reserved group `"all"`
runs every test. The runner records results per device model + configuration
with regression comparison (see `autotest.results`).

## Requirements

Python 3.10+ and `PyYAML` (for the results recorder). The Android backend needs
`adb` on `PATH`.

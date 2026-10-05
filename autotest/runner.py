"""The generic, group-based device test runner.

This is the framework's entry point: it takes a :class:`Suite` (the tests, their
descriptions and their named feature groups), a device-resolution callback and a
results directory, and runs the selected tests on the selected device(s) with
timing, per-test tab hygiene, an optional live device notification, orientation
handling and result persistence/regression comparison (see :mod:`autotest.results`).

It knows nothing about any particular app or platform — it only drives the
:class:`~autotest.device.Device` contract. An app profile (e.g. Fulguris)
constructs the :class:`Suite`, resolves its own :class:`Device` objects, and calls
:meth:`Runner.run` (usually from a thin ``run.py`` that maps CLI flags).
"""
from __future__ import annotations

import dataclasses
import os
import time
import traceback
from collections.abc import Callable

from . import results as results_store
from .device import Device
from .session import Session


@dataclasses.dataclass
class Suite:
    """The collection of tests a runner executes, plus how to select a subset.

    ``groups`` maps a named feature group (e.g. ``"cursor"``) to the list of
    tests in it; the reserved name ``"all"`` is added automatically and runs
    every test. ``default_group`` is what runs when neither ``--test`` nor
    ``--group`` is given (the cheap sanity layer).
    """

    tests: list
    descriptions: dict
    groups: dict | None = None
    default_group: str = "smoke"

    def __post_init__(self):
        self.groups = dict(self.groups or {})
        self.groups["all"] = list(self.tests)
        if self.default_group not in self.groups:
            self.default_group = "all"

    @property
    def names(self) -> set[str]:
        return {t.__name__ for t in self.tests}


def select_tests(suite: Suite, name: str | None = None, group: str | None = None) -> tuple[list, str | None]:
    """Resolve the tests to run. Returns ``(tests, group_or_None)``.

    ``--test`` (a name substring) searches every test; ``--group`` selects a named
    feature group; with neither, the suite's ``default_group`` is used.
    """
    if name:
        tests = [t for t in suite.tests if name in t.__name__]
        if not tests:
            raise LookupError(f"No test matches '{name}'.")
        return tests, None
    if group is None:
        group = suite.default_group
    if group not in suite.groups:
        avail = ", ".join(sorted(suite.groups))
        raise LookupError(f"Unknown group '{group}'. Available: {avail}")
    return list(suite.groups[group]), group


class Runner:
    """Run a :class:`Suite` of device tests and record the results.

    ``resolve`` maps ``(device, use_all, package)`` to a list of
    :class:`Device` (exiting on an ambiguous/absent selection); the runner shares
    one :class:`Session` across all of them for the run's tab/restart policy.
    """

    def __init__(self, resolve: Callable[[str | None, bool, str | None], list[Device]],
                 suite: Suite, results_dir: str):
        self._resolve = resolve
        self.suite = suite
        self.results_dir = results_dir

    # -- single test --------------------------------------------------------

    def _run_one(self, t, device: Device, ctx: dict) -> tuple[float, str | None]:
        """Run one test with timing. Returns (elapsed seconds, error line or None).

        The tabs the test created are closed again afterwards (hygiene) unless
        keep-tabs is set; see :class:`~autotest.session.Session`.
        """
        device.session.reset_opened()
        t0 = time.monotonic()
        try:
            t(device, ctx)
            result: str | None = None
        except AssertionError as e:
            result = f"FAIL  {t.__name__}: {e}"
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            result = f"ERROR {t.__name__}: {e}"
        elapsed = time.monotonic() - t0
        if not device.session.keep and device.session.opened > 0:
            device.close_tabs(device.session.opened)
        return elapsed, result

    @staticmethod
    def _status(error: str | None) -> str:
        if error is None:
            return "pass"
        return "error" if error.startswith("ERROR") else "fail"

    # -- full run -----------------------------------------------------------

    def run(self, *, device: str | None = None, use_all: bool = False,
            package: str | None = None, test: str | None = None, group: str | None = None,
            restart: bool = False, keep_tabs: bool = False, orientation: str | None = None,
            no_save: bool = False, notify: bool = False, list: bool = False) -> int:
        """Execute the selected tests on the selected device(s); return an exit code."""
        if list:
            for t in self.suite.tests:
                print(t.__name__)
            return 0

        session = Session()
        session.set_restarts_between_tests(restart)
        session.set_keep_tabs(keep_tabs)

        try:
            devices = self._resolve(device, use_all, package)
        except SystemExit:  # resolve exits on ambiguous/absent selection
            raise

        try:
            tests, selected_group = select_tests(self.suite, test, group)
        except LookupError as e:
            print(str(e))
            return 2
        if not test and not group:
            print(f"No --test/--group given; running the default "
                  f"'{self.suite.default_group}' group ({len(tests)} tests). "
                  f"Use --group <name> or --group all to select otherwise.")

        overall_ok = True
        total_start = time.monotonic()
        for dev in devices:
            dev.session = session
            config = dev.config()
            print(f"\n=== {dev.label()}  [{dev.package}] ===")
            print(f"  config: {config['config_id']}  "
                  f"({config['orientation']}, rot {config['rotation']}°, "
                  f"sw{config['smallest_width_dp']}dp, Android {config['android']})")

            saved_state = None
            if orientation:
                saved_state = dev.orientation_state()
                dev.set_orientation(orientation)

            ctx: dict = {"notes": []}
            passed = 0
            timings: list[tuple[str, float]] = []
            test_records: list[dict] = []
            device_start = time.monotonic()

            if notify:
                try:
                    dev.dismiss_notification()
                    dev.post_notification(f"Running {len(tests)} test(s) on {dev.label()}")
                except Exception as e:  # noqa: BLE001 - never let a notification fail a run
                    print(f"  note: --notify unavailable on this device ({e}); continuing without it")

            for i, t in enumerate(tests, 1):
                if notify:
                    try:
                        dev.post_notification(f"({i}/{len(tests)}) {t.__name__}")
                    except Exception:  # noqa: BLE001 - notification is cosmetic
                        pass
                elapsed, error = self._run_one(t, dev, ctx)
                timings.append((t.__name__, elapsed))
                record = {"name": t.__name__, "status": self._status(error),
                          "duration_s": round(elapsed, 1)}
                if error:
                    overall_ok = False
                    record["message"] = error.split(": ", 1)[-1]
                    print(f"  {error}  ({elapsed:.1f}s)")
                else:
                    passed += 1
                    print(f"  PASS  {t.__name__}  ({elapsed:.1f}s)")
                test_records.append(record)

            device_elapsed = time.monotonic() - device_start
            print(f"  -> {passed}/{len(tests)} passed in {device_elapsed:.1f}s")
            if timings:
                slowest = max(timings, key=lambda item: item[1])
                print(f"  slowest: {slowest[0]} ({slowest[1]:.1f}s)")
            for note in ctx["notes"]:
                print(f"  note: {note}")

            if notify:
                try:
                    summary = f"Done: {passed}/{len(tests)} passed" + (
                        f" on {dev.label()}" if len(devices) > 1 else "")
                    dev.post_notification(summary)
                    dev.dismiss_notification()
                except Exception:  # noqa: BLE001 - best effort
                    pass

            if not no_save:
                previous = results_store.load_last_run(config["model"], config["config_id"],
                                                       self.results_dir)
                record = results_store.build_record(
                    config, dev.package,
                    {"restart": restart, "keep_tabs": keep_tabs, "orientation": orientation,
                     "test_filter": test, "group": selected_group},
                    test_records, device_elapsed,
                    prev=previous,
                    known=self.suite.names,
                )
                diff = results_store.compare(previous, record)
                yaml_path, md_path = results_store.save_run(record, self.suite.descriptions,
                                                            self.results_dir)
                if diff["regressions"]:
                    print(f"  REGRESSIONS vs last run: {', '.join(diff['regressions'])}")
                if diff["fixes"]:
                    print(f"  fixed since last run: {', '.join(diff['fixes'])}")
                if previous is None:
                    first = f"  saved (no previous run to compare) -> {os.path.relpath(yaml_path)}"
                else:
                    first = f"  saved (compared to {previous['timestamp']}) -> {os.path.relpath(yaml_path)}"
                print(first + f"  [+ {os.path.basename(md_path)}]")

            if saved_state is not None:
                dev.restore_orientation(*saved_state)

        print(f"\nTotal: {time.monotonic() - total_start:.1f}s across {len(devices)} device(s)")
        return 0 if overall_ok else 1

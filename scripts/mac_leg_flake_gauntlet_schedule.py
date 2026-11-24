"""Opt-in exit-scheduling witness for nine existing native concurrency tests.

Select explicitly with ``python -m unittest
scripts.mac_leg_flake_gauntlet_schedule``. Discovery does not select this file.
Only the fixture module's child factories and directory teardown are wrapped. Native
targets, advisory locks, product clocks, queue payloads and assertions run
unchanged. Process scheduling gates use pipes so terminating a gated owner
cannot strand an Event condition mutex. A child awaiting its final timeslice remains genuinely alive when
a legacy short join expires. Event-driven joins with a generous watchdog
release that timeslice and observe the actual exit. No readiness sleeps are
introduced, and every child is released and reaped after each original test.
"""

import ast
from collections import Counter
import importlib
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
from time import monotonic as _budget_monotonic
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import test_gauntlet_concurrency as fixture


SCENARIOS = (
    (fixture.ConcurrentWriterGauntletTests,
     "test_epoch_contention_is_counted_retried_and_has_one_send"),
    (fixture.ConcurrentWriterGauntletTests,
     "test_ledger_lock_contention_is_typed_and_bounded"),
    (fixture.ConcurrentWriterGauntletTests,
     "test_cas_lock_contention_is_typed_and_bounded"),
    (fixture.WakeHoldConcurrencyTests,
     "test_concurrent_same_key_creates_one_hold_row"),
    (fixture.WakeHoldConcurrencyTests,
     "test_competing_keys_do_not_present_one_fresh_message_twice"),
    (fixture.WakeHoldConcurrencyTests,
     "test_identical_keys_are_independent_only_across_session_ledgers"),
    (fixture.WakeHoldConcurrencyTests,
     "test_two_sessions_for_one_seat_share_one_exclusive_lane_lease"),
    (fixture.WakeHoldConcurrencyTests,
     "test_evaluation_and_acknowledgment_are_serialized_in_both_lock_orders"),
    (fixture.WakeHoldConcurrencyTests,
     "test_evaluation_and_retraction_are_serialized_in_both_lock_orders"),
)

_LEGACY_JOIN_BOUNDS = frozenset((2, 5, 10))
_WATCHDOG_SECONDS = 60


class _PipeGate:
    """One bounded notification, with no mutex a terminated child can retain."""

    def __init__(self, context):
        self.reader, self.writer = context.Pipe(duplex=False)
        self.sent = False
        self.observed = False

    def set(self):
        if not self.sent:
            self.writer.send_bytes(b"1")
            self.sent = True

    def wait(self, timeout=None):
        if self.observed:
            return True
        if not self.reader.poll(timeout):
            return False
        self.observed = self.reader.recv_bytes() == b"1"
        return self.observed

    def is_set(self):
        return self.sent or self.wait(0)

    def close(self):
        self.reader.close()
        self.writer.close()


def _scheduled_target(target, arguments, keywords, finished, exit_schedule):
    try:
        if target is not None:
            target(*arguments, **keywords)
    finally:
        finished.set()
        if not exit_schedule.wait(_WATCHDOG_SECONDS):
            raise AssertionError("native child exit scheduling event was not released")


class _ScheduledChild:
    def __init__(self, scheduler, factory, event_factory, *arguments, **keywords):
        if arguments:
            raise TypeError("this witness requires named native child arguments")
        self.exit_schedule = event_factory()
        self.finished = event_factory()
        target = keywords.pop("target", None)
        target_arguments = keywords.pop("args", ())
        target_keywords = keywords.pop("kwargs", {})
        self.child = factory(target=_scheduled_target,
            args=(target, target_arguments, target_keywords, self.finished, self.exit_schedule), **keywords)
        self.started = False
        scheduler.children.append(self)

    def __getattr__(self, name):
        return getattr(self.child, name)

    def start(self):
        self.child.start()
        self.started = True

    def close_gates(self):
        # Process gates retain both endpoints until the actual owner exits.
        # Threading Events have no handles to close.
        for gate in (self.finished, self.exit_schedule):
            close = getattr(gate, "close", None)
            if close is not None:
                close()

    def join(self, timeout=None):
        if timeout in _LEGACY_JOIN_BOUNDS and not self.exit_schedule.is_set():
            # Model expiry before the child's final timeslice. The gate, not
            # a fabricated is_alive/exitcode value, retains the actual child.
            # Finish native file operations before the old assertion unwinds
            # its TemporaryDirectory; only process exit remains unscheduled.
            if not self.finished.wait(_WATCHDOG_SECONDS):
                raise AssertionError("native target did not finish before modeled join expiry")
            return
        self.exit_schedule.set()
        return self.child.join(timeout)


class _ScheduledQueue:
    def __init__(self, scheduler, queue):
        self.scheduler, self.queue = scheduler, queue

    def __getattr__(self, name):
        return getattr(self.queue, name)

    def get(self, *arguments, **keywords):
        timeout = keywords.get("timeout", arguments[1] if len(arguments) > 1 else None)
        if timeout is None or timeout > max(_LEGACY_JOIN_BOUNDS):
            self.scheduler.release_exits()
        return self.queue.get(*arguments, **keywords)


class _ScheduledContext:
    def __init__(self, scheduler, context):
        self.scheduler, self.context = scheduler, context

    def __getattr__(self, name):
        return getattr(self.context, name)

    def Event(self):
        event = self.context.Event()
        self.scheduler.events.append(event)
        return event

    def Queue(self, *arguments, **keywords):
        queue = self.context.Queue(*arguments, **keywords)
        self.scheduler.queues.append(queue)
        return _ScheduledQueue(self.scheduler, queue)

    def Process(self, *arguments, **keywords):
        return _ScheduledChild(self.scheduler, self.context.Process, lambda: _PipeGate(self.context),
                               *arguments, **keywords)


class _ScheduledTemporaryDirectory:
    def __init__(self, scheduler, directory):
        self.scheduler, self.directory = scheduler, directory

    def __getattr__(self, name):
        return getattr(self.directory, name)

    def __enter__(self):
        return self.directory.__enter__()

    def _reap_owners(self):
        try:
            self.scheduler.cleanup()
        except BaseException:
            # Neither context unwinding nor object finalization may remove
            # fixture bytes while an owned native writer could still use them.
            self.directory._finalizer.detach()
            raise

    def __exit__(self, *exception):
        self._reap_owners()
        return self.directory.__exit__(*exception)

    def cleanup(self):
        self._reap_owners()
        return self.directory.cleanup()


class _Scheduler:
    def __init__(self):
        self.children, self.events, self.queues = [], [], []

    def context(self, *arguments, **keywords):
        return _ScheduledContext(self, multiprocessing.get_context(*arguments, **keywords))

    def event(self):
        event = threading.Event()
        self.events.append(event)
        return event

    def thread(self, *arguments, **keywords):
        return _ScheduledChild(self, threading.Thread, threading.Event, *arguments, **keywords)

    def temporary_directory(self, *arguments, **keywords):
        return _ScheduledTemporaryDirectory(self,
            tempfile.TemporaryDirectory(*arguments, **keywords))

    def release_exits(self):
        for child in self.children:
            child.exit_schedule.set()

    def cleanup(self):
        # Legacy tests do not all own a finally block. Release fixture gates
        # before reaping so their TemporaryDirectory cannot leave live writers.
        for event in self.events:
            event.set()
        self.release_exits()
        errors = []
        for owned in self.children:
            if not owned.started:
                owned.close_gates()
                continue
            child = owned.child
            child.join(_WATCHDOG_SECONDS)
            if child.is_alive() and isinstance(child, multiprocessing.process.BaseProcess):
                child.terminate()
                child.join(_WATCHDOG_SECONDS)
                if child.is_alive():
                    child.kill()
                    child.join(_WATCHDOG_SECONDS)
            if child.is_alive():
                errors.append(AssertionError("owned native child did not exit during witness cleanup"))
            else:
                owned.close_gates()
        for queue in self.queues:
            queue.close()
            queue.join_thread()
        if errors:
            raise errors[0]
        self.children.clear()
        self.events.clear()
        self.queues.clear()


class ScheduledSuite(unittest.TestSuite):
    def run(self, result, debug=False):
        for test in self:
            if result.shouldStop:
                break
            scheduler = _Scheduler()
            try:
                with mock.patch.object(fixture, "multiprocessing", SimpleNamespace(
                        get_context=scheduler.context)),\
                        mock.patch.object(fixture, "threading", SimpleNamespace(
                            Thread=scheduler.thread, Event=scheduler.event)),\
                        mock.patch.object(fixture, "tempfile", SimpleNamespace(
                            TemporaryDirectory=scheduler.temporary_directory)):
                    if debug:
                        test.debug()
                    else:
                        test(result)
            finally:
                try:
                    scheduler.cleanup()
                except Exception:
                    if debug:
                        raise
                    result.addError(test, sys.exc_info())
        return result


_BUDGET_MODULES = (
    "tests.test_gauntlet_concurrency",
    "tests.test_waiter_lock_retry",
    "tests.test_message_consumption_command",
)
_RESULT_LISTS = ("errors", "failures", "skipped", "expectedFailures", "unexpectedSuccesses")


def _budget_root():
    value = os.environ.get("FLOATI_TEST_TEMP_ROOT", "")
    prefix = os.environ.get("FLOATI_TEST_SCRATCH_PREFIX", "")
    if not value:
        raise ValueError("scratch_root_required: set FLOATI_TEST_TEMP_ROOT to the runner's scratch directory")
    if not prefix:
        raise ValueError("scratch_prefix_required: set FLOATI_TEST_SCRATCH_PREFIX to the runner's scratch prefix")
    root = Path(value)
    if (str(root) != value or root.parent != Path("\x2fprivate/tmp")
            or not root.name.startswith(prefix)):
        raise ValueError("scratch_invalid: use the runner's scratch directory with the supplied prefix")
    for component in (*reversed(root.parents), root):
        if not stat.S_ISDIR(component.lstat().st_mode):
            raise ValueError("budget scratch ancestry must contain only ordinary directories")
    metadata = root.stat()
    if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ValueError("budget scratch must match runner ownership and mode 0700")
    return root


def _flatten_cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten_cases(item)
        else:
            yield item


def _budget_car_selection():
    from scripts.mac_leg_flake_schedule import SCENARIOS as waiter_scenarios

    fixture_callers = []
    source = Path(fixture.__file__).read_text(encoding="utf-8")
    for declaration in ast.parse(source).body:
        if not isinstance(declaration, ast.ClassDef) or declaration.name not in {
                "ConcurrentWriterGauntletTests", "WakeHoldConcurrencyTests"}:
            continue
        for method in declaration.body:
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)) or not method.name.startswith("test"):
                continue
            if any(isinstance(node, ast.Call) and (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "_native_fixture_directory"
                    or isinstance(node.func, ast.Name) and node.func.id == "_native_fixture_directory")
                    for node in ast.walk(method)):
                fixture_callers.append(f"{fixture.__name__}.{declaration.name}.{method.name}")
    explicit = [
        "tests.test_waiter_lock_retry.WaiterLockRetryTests.test_native_event_lock_release_recovers_mail_in_same_hold",
        "tests.test_message_consumption_command.MessageConsumptionCommandTests.test_current_native_bundle_incompatibility_precedes_and_survives_consumption",
        *(f"tests.test_waiter_lock_retry.WaiterLockRetryTests.{name}" for name in waiter_scenarios),
    ]
    return set(explicit + fixture_callers), {
        "explicit_cases": explicit,
        "native_fixture_callers": fixture_callers,
        "native_fixture_caller_count": len(fixture_callers),
        "selection_basis": "diagnostic AST caller metadata; no implementation-shape assertion",
    }


class _BudgetSuite(unittest.TestSuite):
    def __init__(self, cases, loaded_from, selected, selection):
        super().__init__(cases)
        self.rows = [{"test": case.id(), "loaded_from": module,
                      "car_selected": case.id() in selected,
                      "outcome": "not_run", "seconds": None}
                     for case, module in zip(cases, loaded_from)]
        self.case_rows = {id(case): row for case, row in zip(cases, self.rows)}
        self.selection = dict(selection, selected_cases=sorted(selected),
            selected_but_not_loaded=sorted(selected - {case.id() for case in cases}))
        self.sources = {name: {"path": str(Path(sys.modules[name].__file__)),
            "sha256": hashlib.sha256(Path(sys.modules[name].__file__).read_bytes()).hexdigest()}
            for name in _BUDGET_MODULES}

    def run(self, result, debug=False):
        root = _budget_root()
        path = root / "test-budget.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        started, snapshots = {}, {}
        latest = [None]
        native_start, native_stop = result.startTest, result.stopTest
        def start(test):
            latest[0] = id(test)
            started[id(test)] = _budget_monotonic()
            snapshots[id(test)] = {name: len(getattr(result, name, ())) for name in _RESULT_LISTS}
            native_start(test)
        def stop(test):
            row = self.case_rows[id(test)]
            row["seconds"] = _budget_monotonic() - started[id(test)]
            counts = {name: len(getattr(result, name, ())) - snapshots[id(test)][name]
                      for name in _RESULT_LISTS}
            row["result_counts"] = counts
            row["outcome"] = next((outcome for name, outcome in (
                ("errors", "error"), ("failures", "failure"),
                ("unexpectedSuccesses", "unexpected_success"),
                ("skipped", "skipped"), ("expectedFailures", "expected_failure"))
                if counts[name]), "passed")
            native_stop(test)
        completed = False
        began = _budget_monotonic()
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            try:
                # Standard TestSuite preserves module/class fixtures and every
                # TestCase cleanup. Only result callbacks measure real time.
                with mock.patch.object(result, "startTest", start), mock.patch.object(result, "stopTest", stop):
                    super().run(result, debug=debug)
                completed = True
            except BaseException as failure:
                if latest[0] is not None:
                    self.case_rows[latest[0]].update(
                        outcome="interrupted" if isinstance(failure, KeyboardInterrupt) else "aborted",
                        exception_type=type(failure).__name__)
                raise
            finally:
                for identity, stamp in started.items():
                    row = self.case_rows[identity]
                    if row["seconds"] is None:
                        row["seconds"] = _budget_monotonic() - stamp
                        if row["outcome"] == "not_run":
                            row["outcome"] = "interrupted"
                executed = [row for row in self.rows if row["seconds"] is not None]
                car = [row for row in self.rows if row["car_selected"]]
                def totals(rows):
                    measured = [row for row in rows if row["seconds"] is not None]
                    return {"selected_tests": len(rows), "executed_tests": len(measured),
                            "test_seconds": sum(row["seconds"] for row in measured),
                            "outcomes": dict(Counter(row["outcome"] for row in rows))}
                document = {"kind": "mac_leg_flake_test_budget", "version": 1,
                    "status": "complete" if completed and len(executed) == len(self.rows) else "partial",
                    "scratch": str(root), "pid": os.getpid(), "uid": os.getuid(),
                    "clock": "script-captured real monotonic; no scheduler patches",
                    "elapsed_seconds": _budget_monotonic() - began,
                    "modules": {module: totals([row for row in self.rows if row["loaded_from"] == module])
                                for module in _BUDGET_MODULES},
                    "all_tests": totals(self.rows), "car": totals(car),
                    "sources": self.sources,
                    "car_selection": self.selection, "case_timings": self.rows}
                json.dump(document, output, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
                print(json.dumps({"kind": document["kind"], "status": document["status"],
                    "path": str(path), "all_tests": document["all_tests"], "car": document["car"]},
                    sort_keys=True), flush=True)
        return result


def budget():
    """Explicit three-module real timing battery; never selected by discovery."""
    _budget_root()
    loader = unittest.TestLoader()
    cases, loaded_from = [], []
    for name in _BUDGET_MODULES:
        module_cases = list(_flatten_cases(loader.loadTestsFromModule(importlib.import_module(name))))
        cases.extend(module_cases)
        loaded_from.extend([name] * len(module_cases))
    selected, selection = _budget_car_selection()
    return _BudgetSuite(cases, loaded_from, selected, selection)


def car_budget():
    """Remeasure only the same conservative car after module readers pass."""
    full = budget()
    selected = set(full.selection["selected_cases"])
    cases = [case for case in full if case.id() in selected]
    return _BudgetSuite(cases, [case.__class__.__module__ for case in cases],
                        selected, full.selection)


def members():
    """The twenty scoped cases with normal fixtures and no schedule injection."""
    from tests.test_message_consumption_command import MessageConsumptionCommandTests
    from tests.test_waiter_lock_retry import WaiterLockRetryTests
    from scripts.mac_leg_flake_schedule import SCENARIOS as waiter_scenarios

    cases = [
        WaiterLockRetryTests("test_native_event_lock_release_recovers_mail_in_same_hold"),
        MessageConsumptionCommandTests("test_current_native_bundle_incompatibility_precedes_and_survives_consumption"),
        fixture.ConcurrentWriterGauntletTests("test_twelve_process_send_register_ack_and_claim_torture_has_no_double_effects"),
        *(WaiterLockRetryTests(name) for name in waiter_scenarios),
        *(case(name) for case, name in SCENARIOS),
    ]
    return unittest.TestSuite(cases)


def load_tests(loader, tests, pattern):
    return ScheduledSuite(case(name) for case, name in SCENARIOS)

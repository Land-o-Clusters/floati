"""Closed native fixture ownership controls; never full qualification."""
from __future__ import annotations

import json
import multiprocessing
from multiprocessing import queues, resource_tracker, synchronize, util
import os
from pathlib import Path
import sys
import tempfile
from unittest import mock


METHODS = {
    "gauntlet": "tests.test_gauntlet_concurrency.EffectLedgerConcurrencyTests."
                "test_effect_intent_and_run_acceptance_race_has_one_durable_winner",
    "runtruth": "tests.test_runtruth.EffectAcceptanceTests."
                "test_no_new_intent_can_race_after_effect_snapshot_and_acceptance_append",
}
MODES = ("owned", "resource-error", "partial-start", "borrowed", "collection-error")


class InjectedFixtureFailure(RuntimeError):
    """A closed fixture fault after real resource construction or child start."""


def observe(root, output, selection):
    import unittest
    import parallel_unittest as native
    import parallel_plan as planner
    from harbor_subtest_parity import source_inventory

    root, output = Path(root).resolve(strict=True), Path(output).resolve(strict=True)
    group, mode = selection.removeprefix("tracker-").split("-", 1)
    if group not in METHODS or mode not in MODES:
        raise ValueError("unknown closed tracker control")
    before = source_inventory(root)
    tracker = resource_tracker._resource_tracker
    if tracker._pid is not None or tracker._fd is not None:
        raise ValueError("tracker control requires a fresh process without a borrowed ambient tracker")
    baseline_finalizers = frozenset(util._finalizer_registry)
    context = multiprocessing.get_context("spawn")
    borrowed = context.Event() if mode == "borrowed" else None
    borrowed_pid = tracker._pid
    borrowed_finalizers = frozenset(util._finalizer_registry) - baseline_finalizers
    processes, owned_queues, owned_events, calls, tracker_pids = [], [], [], [], []
    stage = "fixture"
    injected = None
    injected_exceptions, native_exceptions = [], []
    guard_codes, owned_temporaries = [], []
    starts = 0
    result = None
    report = {"scope": "actual_fixture_tracker_ownership_not_qualification", "qualified": False,
              "status": "invalid", "successful": False, "selection": selection,
              "method": METHODS[group], "leader_pid": os.getpid(), "source_before": before,
              "borrowed_tracker_pid": borrowed_pid, "historical_pid_attribution": "unproved"}
    process_type = context.Process
    native_init, native_start = process_type.__init__, process_type.start
    native_join, native_close = process_type.join, process_type.close
    queue_init, queue_close, queue_join = queues.Queue.__init__, queues.Queue.close, queues.Queue.join_thread
    event_init = synchronize.Event.__init__
    tracker_type = type(tracker)
    ensure_running, stop = tracker_type.ensure_running, tracker_type._stop
    native_add_error = native._RecordingResult.addError
    native_add_failure = native._RecordingResult.addFailure
    native_add_cleanup = unittest.TestCase.addCleanup

    def observed_add_cleanup(test, function, *args, **kwargs):
        value = native_add_cleanup(test, function, *args, **kwargs)
        if (test is actual_cases[0]
                and getattr(function, "__name__", None) == "close_owned_tracker"
                and getattr(function, "__module__", None) == type(test).__module__):
            guard_codes.append(function.__code__)
            record("tracker_guard_registered", file=function.__code__.co_filename,
                   first_line=function.__code__.co_firstlineno)
        return value

    def fault(point):
        error = InjectedFixtureFailure(point)
        injected_exceptions.append(error)
        return error

    def observe_exception(callback, test, err):
        frames, trace = [], err[2]
        while trace is not None:
            frames.append(trace.tb_frame.f_code)
            trace = trace.tb_next
        point = {"resource-error": "after-real-event-and-queue-before-first-start",
                 "partial-start": "before-second-start-after-first-real-start",
                 "collection-error": "retained-first-handle-join-failure"}.get(mode)
        origin = {"resource-error": observed_process_init,
                  "partial-start": observed_start,
                  "collection-error": observed_join}.get(mode)
        exact_injected = (callback == "addError" and test is actual_cases[0]
                          and bool(injected_exceptions) and err[1] is injected_exceptions[-1]
                          and err[0] is InjectedFixtureFailure
                          and str(err[1]) == point
                          and origin is not None and origin.__code__ in frames)
        exact_guard = (mode == "collection-error" and callback == "addFailure"
                       and test is actual_cases[0] and err[0] is test.failureException
                       and type(err[1]) is test.failureException
                       and str(err[1]) == "False is not true : effect race child collection remains unproved"
                       and len(guard_codes) == 1 and guard_codes[0] in frames)
        native_exceptions.append({"callback": callback, "test_id": test.id(),
                                  "actual_selected_object": test is actual_cases[0],
                                  "exception_type": err[0].__module__ + "." + err[0].__qualname__,
                                  "text": str(err[1]), "exact_injected_exception": exact_injected,
                                  "exact_collection_guard": exact_guard,
                                  "injected_exception_ordinal": len(injected_exceptions) - 1 if exact_injected else None,
                                  "traceback": [{"file": code.co_filename, "name": code.co_name,
                                                 "first_line": code.co_firstlineno} for code in frames]})

    def observed_add_error(result, test, err):
        value = native_add_error(result, test, err)
        observe_exception("addError", test, err)
        return value

    def observed_add_failure(result, test, err):
        value = native_add_failure(result, test, err)
        observe_exception("addFailure", test, err)
        return value

    def record(kind, **values):
        calls.append(dict(stage=stage, kind=kind, **values))

    def process_identity(process):
        return {"ordinal": processes.index(process), "pid": process.pid, "exitcode": process.exitcode}

    def observed_ensure(owner):
        value = ensure_running(owner)
        if owner is tracker and owner._pid not in tracker_pids:
            tracker_pids.append(owner._pid)
            record("tracker_started", pid=owner._pid)
        return value

    native_waitpid = os.waitpid

    def observed_stop(owner):
        pid = owner._pid
        waits = []
        record("tracker_stop_enter", pid=pid)
        def observed_waitpid(wait_pid, flags):
            value = native_waitpid(wait_pid, flags)
            row = {"requested_pid": wait_pid, "flags": flags,
                   "returned_pid": value[0], "wait_status": value[1],
                   "exited": os.WIFEXITED(value[1]),
                   "exit_code": os.WEXITSTATUS(value[1]) if os.WIFEXITED(value[1]) else None}
            waits.append(row)
            record("native_waitpid_return", **row)
            return value
        # _stop's actual wait result is retained unchanged. Never replace a
        # wait outcome or infer collection just from the cleared _pid field.
        with mock.patch.object(resource_tracker.os, "waitpid", observed_waitpid):
            value = stop(owner)
        collected = (pid is not None and len(waits) == 1
                     and waits[0]["requested_pid"] == waits[0]["returned_pid"] == pid
                     and waits[0]["flags"] == 0 and waits[0]["exited"]
                     and waits[0]["exit_code"] == 0)
        record("tracker_stop_return", pid=pid, after_pid=owner._pid,
               after_fd=owner._fd, actual_wait_collected=collected, waits=waits)
        return value

    def observed_process_init(process, *args, **kwargs):
        nonlocal injected
        native_init(process, *args, **kwargs)
        processes.append(process)
        if mode == "resource-error" and len(processes) == 1:
            injected = "after-real-event-and-queue-before-first-start"
            record("injected", point=injected)
            raise fault(injected)

    def observed_start(process):
        nonlocal starts, injected
        starts += 1
        if mode == "partial-start" and starts == 2:
            injected = "before-second-start-after-first-real-start"
            # Explicit observer safety release: it does not count as fixture
            # joining/queue cleanup, and prevents the old fixture from hanging
            # inside its tracker cleanup while a real first child waits.
            for event in owned_events:
                event.set()
            record("fault_safety_release", point=injected)
            raise fault(injected)
        try:
            return native_start(process)
        finally:
            record("process_start_return", **process_identity(process))

    def observed_join(process, *args, **kwargs):
        nonlocal injected
        if mode == "collection-error" and stage == "fixture" and processes.index(process) == 0:
            injected = "retained-first-handle-join-failure"
            record("process_join_fault", **process_identity(process))
            raise fault(injected)
        value = native_join(process, *args, **kwargs)
        record("process_join_return", **process_identity(process))
        return value

    def observed_close(process):
        identity = process_identity(process)
        value = native_close(process)
        record("process_close_return", **identity)
        return value

    def observed_queue_init(queue, *args, **kwargs):
        queue_init(queue, *args, **kwargs)
        owned_queues.append(queue)
        record("queue_created", ordinal=owned_queues.index(queue))

    def observed_queue_close(queue):
        value = queue_close(queue)
        record("queue_close_return", ordinal=owned_queues.index(queue))
        return value

    def observed_queue_join(queue):
        value = queue_join(queue)
        record("queue_join_return", ordinal=owned_queues.index(queue))
        return value

    def observed_event_init(event, *args, **kwargs):
        event_init(event, *args, **kwargs)
        owned_events.append(event)

    original_out, original_err = sys.stdout, sys.stderr
    try:
        with (output / "unittest.log").open("x") as log:
            sys.stdout = sys.stderr = log
            suite = unittest.defaultTestLoader.loadTestsFromName(METHODS[group])
            def cases(value):
                if isinstance(value, unittest.TestCase):
                    return [value]
                return [case for child in value for case in cases(child)]
            actual_cases = cases(suite)
            if len(actual_cases) != 1:
                raise ValueError("tracker control needs exactly one real fixture instance")
            census = native._census(suite)
            if census["ids"] != [METHODS[group]]:
                raise ValueError("tracker observer changed the actual selected method")
            for unit in census["units"]:
                unit["serial_reason"] = "single actual tracker fixture control"
            plan = planner.make_plan(census, 1)
            for name, value in (("census.json", census), ("plan.json", plan)):
                (output / name).write_text(json.dumps(value, indent=2) + "\n")
            with (mock.patch.object(process_type, "__init__", observed_process_init),
                  mock.patch.object(process_type, "start", observed_start),
                  mock.patch.object(process_type, "join", observed_join),
                  mock.patch.object(process_type, "close", observed_close),
                  mock.patch.object(queues.Queue, "__init__", observed_queue_init),
                  mock.patch.object(queues.Queue, "close", observed_queue_close),
                  mock.patch.object(queues.Queue, "join_thread", observed_queue_join),
                  mock.patch.object(synchronize.Event, "__init__", observed_event_init),
                  mock.patch.object(tracker_type, "ensure_running", observed_ensure),
                  mock.patch.object(tracker_type, "_stop", observed_stop),
                  mock.patch.object(unittest.TestCase, "addCleanup", observed_add_cleanup),
                  mock.patch.object(native._RecordingResult, "addError", observed_add_error),
                  mock.patch.object(native._RecordingResult, "addFailure", observed_add_failure)):
                with (output / "events.jsonl").open("x") as journal:
                    sequence = 0
                    def emit(event):
                        nonlocal sequence
                        journal.write(json.dumps({"sequence": sequence, "record": event}, sort_keys=True) + "\n")
                        journal.flush()
                        sequence += 1
                    emit({"kind": "header", "selected_ids": census["ids"],
                          "population_digest": census["population_digest"]})
                    result = native.run_suite(suite, plan["assignments"][0], census["population_digest"],
                                              log, event_sink=emit)
                    emit({"kind": "footer", "result": result})
                    os.fsync(journal.fileno())
                report["tracker_after_fixture"] = {"pid": tracker._pid, "fd": tracker._fd,
                    "pipe_alive": tracker._check_alive() if tracker._fd is not None else False}
                report["borrowed_finalizers_preserved"] = borrowed_finalizers <= set(util._finalizer_registry)
                report["fixture_collection_state"] = getattr(actual_cases[0], "_effect_race_children_collected", None)
                owned_temporaries = list(getattr(actual_cases[0], "_effect_owned_temporaries", []))
                report["roots_after_fixture"] = [
                    {"path": str(Path(temporary.name)), "exists": Path(temporary.name).exists(),
                     "automatic_finalizer_detached": not temporary._finalizer.alive}
                    for temporary in owned_temporaries]
                report["tracker_guard_registrations"] = len(guard_codes)
                report["tracker_pids_created"] = tracker_pids
                report["fixture_calls"] = list(calls)
            (output / "native-result.json").write_text(json.dumps(result, indent=2) + "\n")
            # Real injected faults stay native errors; an expected error is
            # separately adjudicated as a cleanup control, never a passing test.
            expected_fault = mode in ("resource-error", "partial-start", "collection-error")
            kinds = [event["kind"] for event in result["events"]]
            expected_kinds = ["error"] if expected_fault else ["success"]
            kinds_ok = (kinds == ["error", "failure"]
                        if mode == "collection-error" else kinds == expected_kinds)
            if mode == "collection-error":
                exceptions_ok = (len(native_exceptions) == 2
                                 and native_exceptions[0]["exact_injected_exception"]
                                 and native_exceptions[1]["exact_collection_guard"])
            elif expected_fault:
                exceptions_ok = (len(native_exceptions) == 1
                                 and native_exceptions[0]["exact_injected_exception"])
            else:
                exceptions_ok = native_exceptions == []
            outcomes_ok = (result["status"] == "complete" and result["tests_run"] == 1
                           and result["selected_ids"] == result["started_ids"] == result["stopped_ids"] == census["ids"]
                           and result["fixture_blocked_ids"] == [] and kinds_ok
                           and bool(injected) == expected_fault and exceptions_ok)
            roots_ok = (len(owned_temporaries) == 1
                        and all(row["automatic_finalizer_detached"]
                                and row["exists"] is (mode == "collection-error")
                                for row in report["roots_after_fixture"]))
            outcomes_ok = outcomes_ok and len(guard_codes) == 1
            started = {row["ordinal"] for row in calls if row["kind"] == "process_start_return" and row["pid"] is not None}
            joined = {row["ordinal"] for row in calls if row["kind"] == "process_join_return" and row["exitcode"] is not None}
            closed = {row["ordinal"] for row in calls if row["kind"] == "queue_close_return"}
            joined_queues = {row["ordinal"] for row in calls if row["kind"] == "queue_join_return"}
            resources_ok = (started <= joined and len(owned_queues) == 1
                            and closed == joined_queues == {0})
            if mode == "collection-error":
                # A real retained handle is deliberately not collectable by
                # the fixture's join call. Its outer owner collects it later.
                # Never reinterpret this branch as a successful native test.
                tracker_ok = (len(tracker_pids) == 1 and tracker._pid == tracker_pids[0]
                              and tracker._check_alive() and report["fixture_collection_state"] is False
                              and not any(row["kind"] == "tracker_stop_enter" for row in calls))
                resources_ok = (started - joined == {0} and closed == joined_queues == {0}
                                and sum(row["kind"] == "process_join_fault" for row in calls) >= 2)
            elif mode == "borrowed":
                tracker_ok = (tracker._pid == borrowed_pid and tracker._check_alive()
                              and report["borrowed_finalizers_preserved"]
                              and not any(row["kind"] == "tracker_stop_enter" for row in calls))
            else:
                stopped = [row for row in calls if row["kind"] == "tracker_stop_return"]
                tracker_ok = (len(tracker_pids) == 1 and tracker_pids[0] is not None
                              and tracker._pid is None and tracker._fd is None
                              and len(stopped) == 1 and stopped[0]["pid"] == tracker_pids[0]
                              and stopped[0]["actual_wait_collected"] is True)
            report.update(native_outcomes_expected=outcomes_ok, native_exceptions_expected=exceptions_ok,
                          fixture_resources_collected=resources_ok,
                          fixture_tracker_contract=tracker_ok, fixture_roots_contract=roots_ok, injected=injected,
                          status="complete", successful=outcomes_ok and resources_ok and tracker_ok and roots_ok)
    except Exception as error:
        report.update(status="invalid", successful=False,
                      error=type(error).__name__ + ": " + str(error))
    finally:
        sys.stdout, sys.stderr = original_out, original_err
        stage = "observer_safety_cleanup"
        cleanup_errors = []
        # This cleanup owns only objects retained by this control. It occurs
        # after fixture testimony and cannot turn a failed fixture check green.
        for event in owned_events:
            try:
                event.set()
            except Exception as error:
                cleanup_errors.append(repr(error))
        for process in processes:
            if getattr(process, "_closed", False) or process.pid is None:
                continue
            try:
                native_join(process, 5)
                record("process_join_return", **process_identity(process))
                if process.is_alive():
                    process.terminate()
                    record("owned_process_terminate", **process_identity(process))
                    native_join(process, 5)
                    record("process_join_return", **process_identity(process))
                if process.is_alive() or process.exitcode is None:
                    raise RuntimeError("control-owned process remained uncollected")
            except Exception as error:
                cleanup_errors.append(repr(error))
        for queue in owned_queues:
            try:
                queue.close()
                queue.join_thread()
            except Exception as error:
                cleanup_errors.append(repr(error))
        if not cleanup_errors:
            try:
                for key, finalizer in list(util._finalizer_registry.items()):
                    if key not in baseline_finalizers and finalizer._callback == synchronize.SemLock._cleanup:
                        finalizer()
                if tracker._pid is not None:
                    # Either the retained original fixture tracker or the
                    # sentinel tracker explicitly created by this outer owner.
                    observed_stop(tracker)
            except Exception as error:
                cleanup_errors.append(repr(error))
        all_stops = [row for row in calls if row["kind"] == "tracker_stop_return"]
        exact_collected = (len(all_stops) == 1 and all_stops[0]["actual_wait_collected"]
                           and all_stops[0]["pid"] == (borrowed_pid if borrowed is not None else tracker_pids[0] if tracker_pids else None))
        started_pids = {row["pid"] for row in calls
                        if row["kind"] == "process_start_return" and row["pid"] is not None}
        joined_outcomes = {row["pid"]: row["exitcode"] for row in calls
                           if row["kind"] == "process_join_return" and row["exitcode"] is not None}
        children_collected = started_pids <= joined_outcomes.keys()
        child_outcomes_expected = (children_collected
                                   and all(joined_outcomes[pid] == 0 for pid in started_pids))
        collection_proved = (not cleanup_errors and children_collected
                             and tracker._pid is None and tracker._fd is None and exact_collected)
        # A failed child can still be collected. Deletion follows collection;
        # the separate exit-status predicate keeps its control verdict failed.
        if collection_proved:
            for temporary in owned_temporaries:
                try:
                    path = Path(temporary.name)
                    existed = path.exists()
                    tempfile.TemporaryDirectory._rmtree(temporary.name)
                    record("owned_root_cleanup_return", path=str(path), existed=existed,
                           exists_after=path.exists())
                    if path.exists():
                        raise RuntimeError("collected control's owned root remains")
                except Exception as error:
                    cleanup_errors.append(repr(error))
        report["roots_after_observer"] = [
            {"path": str(Path(temporary.name)), "exists": Path(temporary.name).exists()}
            for temporary in owned_temporaries]
        report["child_outcomes_expected"] = child_outcomes_expected
        report["collected_child_outcomes"] = [
            {"pid": pid, "collected": pid in joined_outcomes, "exitcode": joined_outcomes.get(pid)}
            for pid in sorted(started_pids)]
        report["observer_safety_cleanup"] = {"errors": cleanup_errors,
            "successful": collection_proved and not cleanup_errors
                          and all(not row["exists"] for row in report["roots_after_observer"]),
            "calls": calls[len(report.get("fixture_calls", [])):], "borrowed_sentinel_owned_by_observer": borrowed is not None}
        report["native_exception_observations"] = native_exceptions
        report["source_after"] = source_inventory(root)
        if (report["source_after"] != before or not report["observer_safety_cleanup"]["successful"]
                or not child_outcomes_expected):
            report["successful"] = False
        (output / "tracker-observation.json").write_text(json.dumps(report, indent=2) + "\n")
    return report

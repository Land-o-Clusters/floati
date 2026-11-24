"""Pure replay controls from real native execution and worker transcripts."""
import tests

from contextlib import ExitStack
from copy import deepcopy
import json
import os
from unittest import mock
import unittest

from floati import archive_execution_replay as replay
from floati.bus_epoch import epoch_guard
from floati.errors import FloatiError
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import read_records_snapshot
from floati.events import EVENT_KINDS
from floati.seat_execution import _history
from floati.seat_teardown_activity import _worker_pid_receipts
from floati.snapshot import _owned_epoch_archives
from floati.workers import WorkerReceipts
from tests import test_archive_execution_checkpoint as checkpoint_fixture
from tests import test_seat_execution as fixture


class UnexpectedIO(RuntimeError):
    pass


class ArchiveExecutionReplayTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller
    invocation = fixture.SeatExecutionTests.invocation
    prepare = checkpoint_fixture.ArchiveExecutionCheckpointTests.prepare
    automatic_roll = checkpoint_fixture.ArchiveExecutionCheckpointTests.automatic_roll
    tree = checkpoint_fixture.ArchiveExecutionCheckpointTests.tree

    def capture(self, payload=None):
        unused, controller, unused_invocation, accepted = self.prepare(payload)
        with epoch_guard(self.root, exclusive=True):
            history = _history(self.root, snapshot=True)
            workers = WorkerReceipts(self.root).records()
            pids = _worker_pid_receipts(self.root, "builder", snapshot=True)
        return controller, accepted, history, workers, pids

    def pure(self, operation, *args, **kwargs):
        # Product calls run with native filesystem entry points unavailable.
        # Source fixtures and snapshots are captured before this boundary.
        error, result = None, None
        with ExitStack() as stack:
            for target in ("builtins.open", "io.open", "os.open", "os.stat", "os.lstat",
                           "os.fstat", "os.scandir", "os.listdir", "os.readlink", "os.access"):
                stack.enter_context(mock.patch(target, side_effect=UnexpectedIO(target)))
            try:
                result = operation(*args, **kwargs)
            except UnexpectedIO as caught:
                error = caught
        self.assertIsNone(error, "pure semantic replay attempted filesystem I/O")
        return result

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(FloatiError) as caught:
            self.pure(operation, *args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_cold_full_result_and_worker_join_equal_native_without_io(self):
        payload = {"event": "unknown", "code": "worker_cleanup_unconfirmed",
                   "detail": json.dumps(checkpoint_fixture.AUDIT)}
        controller, accepted, history, workers, expected_pids = self.capture(payload)
        for relative in ("events.jsonl.lock", "receipts/workers.jsonl.lock"):
            (self.root.tenant_home / relative).unlink(missing_ok=True)
        with epoch_guard(self.root, exclusive=True):
            before = self.tree()
            actual_history = self.pure(replay.execution_history, self.root.tenant_id, [history])
            actual = self.pure(replay.execution_result, self.root.tenant_id, accepted["execution"], actual_history)
            pids = self.pure(replay.worker_pid_receipts, self.root.tenant_id,
                             actual_history, workers, node="builder")
            self.assertEqual(before, self.tree())
        self.assertEqual(history, actual_history)
        self.assertEqual(json.dumps(accepted, sort_keys=True), json.dumps(actual, sort_keys=True))
        self.assertEqual(expected_pids, pids)
        self.assertIn(987655, pids)
        self.assertNotIn(987656, pids, "a process group is not a PID ownership join")

    def test_two_actual_automatic_epochs_coalesce_exact_carried_rows(self):
        unused, accepted, unused_history, unused_workers, unused_pids = self.capture()
        self.automatic_roll("pure-one")
        self.automatic_roll("pure-two")
        with epoch_guard(self.root, exclusive=True):
            batches = [read_records_snapshot(self.root,
                archive.relative_to(self.root.tenant_home) / "events.jsonl", allowed_kinds=EVENT_KINDS)
                for archive in reversed(_owned_epoch_archives(self.root))]
            batches.append(EventLog(self.root).compatible_event_records(snapshot=True)[0])
            expected = _history(self.root, snapshot=True)
        self.assertGreater(len([r for batch in batches for r in batch]), len(expected))
        actual = self.pure(replay.execution_history, self.root.tenant_id, batches)
        self.assertEqual(expected, actual)
        self.assertEqual(2, len([r for r in actual if r["kind"] == "seat_execution"]))
        self.assertEqual(json.dumps(accepted, sort_keys=True), json.dumps(self.pure(
            replay.execution_result, self.root.tenant_id, accepted["execution"], actual), sort_keys=True))

    def test_conflicting_carried_identity_preserves_native_refusal(self):
        unused, unused_accepted, history, unused_workers, unused_pids = self.capture()
        changed = deepcopy(history)
        next(row for row in changed if row["kind"] == "seat_execution")["detail"] = "changed native body"
        self.assert_code("epoch_carry_evidence_invalid", replay.execution_history,
                         self.root.tenant_id, [history, changed])

    def test_acceptance_key_duplicate_with_new_id_refuses(self):
        unused, accepted, history, unused_workers, unused_pids = self.capture()
        duplicate = deepcopy(accepted["execution"])
        duplicate["id"] = "seat-execution-" + uuid7_hex()
        self.assert_code("seat_execution_history_invalid", replay.execution_history,
                         self.root.tenant_id, [history + [duplicate]])

    def test_missing_or_later_start_preserves_native_continuity_refusal(self):
        unused, unused_accepted, history, unused_workers, unused_pids = self.capture()
        start = next(row for row in history if row["kind"] == "seat_execution" and row["event"] == "running")
        missing = [row for row in history if row is not start]
        for rows in (missing, missing + [start]):
            with self.subTest(later=bool(rows and rows[-1] is start)):
                self.assert_code("seat_execution_history_invalid", replay.execution_history,
                                 self.root.tenant_id, [rows])

    def test_missing_notice_preserves_result_refusal(self):
        unused, accepted, history, unused_workers, unused_pids = self.capture()
        rows = [row for row in history if row["kind"] != "message_envelope"]
        self.assert_code("seat_execution_notice_missing", replay.execution_result,
                         self.root.tenant_id, accepted["execution"], rows)

    def test_missing_readiness_strict_history_refuses_but_result_fallback_is_exact(self):
        controller, accepted, history, unused_workers, unused_pids = self.capture()
        rows = [row for row in history if row["kind"] != "send_readiness_observation"]
        self.assert_code("seat_execution_notice_invalid", replay.execution_history,
                         self.root.tenant_id, [rows])
        expected = controller._result(accepted["execution"], rows)
        actual = self.pure(replay.execution_result, self.root.tenant_id, accepted["execution"], rows)
        self.assertEqual(json.dumps(expected, sort_keys=True), json.dumps(actual, sort_keys=True))
        self.assertEqual({"state": "unknown", "reason": "historical_readiness_missing",
                          "observed_at": accepted["notice"]["message"]["timestamp"],
                          "checked_mechanisms": [], "mechanism_evidence": []},
                         actual["notice"]["recipient_readiness"])

    def test_terminal_worker_join_is_absent_without_source(self):
        payload = {"event": "unknown", "code": "worker_cleanup_unconfirmed",
                   "detail": json.dumps(checkpoint_fixture.AUDIT)}
        unused, unused_accepted, history, unused_workers, unused_pids = self.capture(payload)
        self.assertEqual({}, self.pure(replay.worker_pid_receipts,
            self.root.tenant_id, history, [], node="builder"))

    def test_retained_contradictory_spawn_join_refuses(self):
        unused, unused_accepted, history, workers, unused_pids = self.capture()
        changed = [dict(row, node_id="other") for row in workers]
        self.assert_code("seat_teardown_activity_unknown", replay.worker_pid_receipts,
                         self.root.tenant_id, history, changed, node="builder")

    def test_worker_chronology_and_duplicate_sources_preserve_native_refusal(self):
        unused, unused_accepted, history, workers, unused_pids = self.capture()
        for transcript in (list(reversed(workers)), workers + [workers[-1]]):
            with self.subTest(duplicate=bool(transcript[-1] is workers[-1])):
                self.assert_code("worker_transition_invalid", replay.worker_pid_receipts,
                                 self.root.tenant_id, history, transcript, node="builder")
        self.assertEqual(WorkerReceipts(self.root)._project(workers),
            self.pure(replay.project_worker_records, self.root.tenant_id, workers))

    def test_unsupported_and_null_audits_remain_non_authoritative_data(self):
        unused, accepted, history, workers, unused_pids = self.capture()
        for detail in ("PID 987655, cleanup unknown", json.dumps(dict(checkpoint_fixture.AUDIT,
                adapter_pid=None, alive_after_cleanup=[])),
                '{"adapter_pid":987655,"adapter_pid":987655}',
                json.dumps(dict(checkpoint_fixture.AUDIT, adapter_pid=True))):
            with self.subTest(detail=detail):
                changed = dict(accepted["execution"], detail=detail)
                rows = [changed if row["id"] == changed["id"] else row for row in history]
                self.assertIsNone(self.pure(replay.worker_audit_pid, changed))
                self.assertEqual({}, self.pure(replay.worker_pid_receipts,
                    self.root.tenant_id, rows, workers, node="builder"))

    def test_explicit_tenant_validates_records_and_empty_collections(self):
        unused, accepted, history, workers, unused_pids = self.capture()
        for operation, args, kwargs in (
                (replay.execution_history, ([history],), {}),
                (replay.validate_execution_history, (history,), {}),
                (replay.execution_result, (accepted["execution"], history), {}),
                (replay.worker_pid_receipts, (history, workers), {"node": "builder"}),
                (replay.project_worker_records, (workers,), {})):
            with self.subTest(operation=operation.__name__):
                self.assert_code("tenant_mismatch", operation, "other-tenant", *args, **kwargs)
        self.assertEqual([], self.pure(replay.execution_history, self.root.tenant_id, []))
        self.assertEqual({}, self.pure(replay.worker_pid_receipts, self.root.tenant_id, [], [], node="builder"))
        for operation, args, kwargs in (
                (replay.execution_history, ([],), {}),
                (replay.validate_execution_history, ([],), {}),
                (replay.project_worker_records, ([],), {}),
                (replay.worker_pid_receipts, ([], []), {"node": "builder"})):
            with self.subTest(empty=operation.__name__):
                self.assert_code("tenant_invalid", operation, "", *args, **kwargs)

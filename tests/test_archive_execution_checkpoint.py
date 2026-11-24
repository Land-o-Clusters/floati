"""Native execution replay and worker/PID survivor planning controls."""
from __future__ import annotations

import tests  # Confine HOME/XDG before product imports.

from copy import deepcopy
from datetime import datetime, timezone
import json
import unittest
from unittest import mock

from floati import archive_execution_checkpoint as checkpoint
from floati.bus_epoch import epoch_guard, roll_bus_epoch, roll_automatic_epoch
from floati.errors import FloatiError, ProtocolRefusal
from floati.events import EventLog
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.seat_execution import _history
from floati.workers import WorkerReceipts
from tests import test_seat_execution as fixture

PAYLOAD = {"event": "ended", "code": "process_died", "detail": "native cleanup completed"}
AUDIT = {"adapter_pid": 987655, "registered_process_groups": [987656],
         "alive_after_cleanup": [987656], "isolation_backend": "macos-sandbox"}


class ArchiveExecutionCheckpointTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller
    invocation = fixture.SeatExecutionTests.invocation

    def prepare(self, payload=None):
        item = self.car()
        self.claim(item)
        controller = self.controller()
        invocation = self.invocation(controller, item)
        accepted = controller.callback(invocation, PAYLOAD if payload is None else payload)
        return item, controller, invocation, accepted

    def plan(self):
        with epoch_guard(self.root, exclusive=True):
            return checkpoint.plan_execution_checkpoint(self.root)

    def roll(self, key):
        AuthorityGrantStore(self.root).grant_exact("bus-epoch-roll", "architect", 1,
                                                  datetime.now(timezone.utc))
        return roll_bus_epoch(self.root, actor="architect", idempotency_key=key)

    def replace(self, rows):
        (self.root.tenant_home / "events.jsonl").write_bytes(b"".join(encode_frame(row) for row in rows))

    def tree(self):
        return {path.relative_to(self.root.tenant_home).as_posix():
                ("directory" if path.is_dir() else path.read_bytes())
                for path in self.root.tenant_home.rglob("*")}

    def automatic_roll(self, key):
        from floati.cursor import SparseCursor
        from floati.epoch_carry import build_carry_plan
        message = EventLog(self.root).send("architect", "other", **fixture.BINDING,
                                          note="completed independent mail", idempotency_key=key)
        EventLog(self.root).present("other")
        SparseCursor(self.root).ack("other", [message["id"]], acting_session_id="other-session")
        def planner(root, old):
            plan = build_carry_plan(root, old)
            return plan.live_payloads, {"trigger": "ack", "policy": {"max_bytes": 1,
                "max_age_days": 7, "source": "operator_declared"},
                "observed_bytes": next(len(raw) for name, plane, raw in old if name == "events.jsonl"),
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                "epoch_started_at": None, "due_reasons": ["bytes"]}
        result = roll_automatic_epoch(self.root, actor="builder", planner=planner)
        self.assertFalse(result.get("no_op", False), "fixture must perform a real native automatic roll")
        return result


    def test_plan_keeps_exact_transcript_replay_and_readiness_across_roll(self):
        from floati.epoch_carry import carry_dependency_witness
        item, controller, invocation, accepted = self.prepare()
        self.work.complete(item["id"], "builder", [fixture.BINDING], now=fixture.NOW)
        self.roll("checkpoint")
        before = carry_dependency_witness(self.root)
        result = self.plan()
        with epoch_guard(self.root):
            executions = [row for row in _history(self.root) if row["kind"] == "seat_execution"]
        self.assertEqual(executions, result["executions"])
        replay = next(row for row in result["replays"] if row["execution_id"] == accepted["execution"]["id"])
        self.assertEqual(accepted, replay["result"])
        self.assertEqual(accepted["notice"]["recipient_readiness"],
                         replay["result"]["notice"]["recipient_readiness"])
        self.assertEqual(before, carry_dependency_witness(self.root), "planning must not write")
        self.assertEqual("planned", result["state"])
        json.dumps(result, allow_nan=False)
        replay["result"]["execution"]["detail"] = "caller mutation"
        self.assertEqual(accepted, controller.callback(invocation, PAYLOAD))
        self.assertNotEqual("caller mutation", self.plan()["replays"][-1]["result"]["execution"]["detail"])

    def test_existing_exclusive_epoch_ownership_is_required(self):
        self.prepare()
        with self.assertRaises(ProtocolRefusal) as caught:
            checkpoint.plan_execution_checkpoint(self.root)
        self.assertEqual("epoch_lock_required", caught.exception.code)
        with epoch_guard(self.root):
            with self.assertRaises(ProtocolRefusal) as caught:
                checkpoint.plan_execution_checkpoint(self.root)
        self.assertEqual("epoch_lock_required", caught.exception.code)

    def test_missing_start_blocks_instead_of_minting_replay(self):
        for case in ("missing start", "duplicate key", "missing notice", "missing readiness", "worker contradiction", "carried conflict"):
            with self.subTest(case=case):
                fixture = type(self)()
                fixture.setUp()
                try:
                    payload = {"event": "unknown", "code": "worker_cleanup_unconfirmed", "detail": json.dumps(AUDIT)}
                    fixture.prepare(payload if case == "worker contradiction" else None)
                    if case == "carried conflict":
                        fixture.automatic_roll("conflicting-copy")
                    rows = EventLog(fixture.root).event_records()
                    if case == "missing start":
                        rows = [row for row in rows if not (row["kind"] == "seat_execution" and row["event"] == "running")]
                    elif case == "duplicate key":
                        duplicate = deepcopy(next(row for row in rows if row["kind"] == "seat_execution" and row["event"] != "running"))
                        duplicate["id"] = "seat-execution-" + uuid7_hex()
                        rows.append(duplicate)
                    elif case in ("missing notice", "missing readiness"):
                        kind = "message_envelope" if case == "missing notice" else "send_readiness_observation"
                        rows = [row for row in rows if row["kind"] != kind]
                    elif case == "worker contradiction":
                        workers = WorkerReceipts(fixture.root).records()
                        workers[-1] = dict(workers[-1], node_id="other")
                        (fixture.root.tenant_home / "receipts/workers.jsonl").write_bytes(b"".join(encode_frame(row) for row in workers))
                    else:
                        next(row for row in rows if row["kind"] == "seat_execution" and row["event"] != "running")["detail"] = "conflicting carried callback body"
                    fixture.replace(rows)
                    with self.assertRaises(FloatiError) as caught:
                        fixture.plan()
                    expected = {"missing notice": "send_readiness_order_invalid", "missing readiness": "seat_execution_notice_invalid",
                                "worker contradiction": "worker_session_mismatch", "carried conflict": "epoch_carry_evidence_invalid"}
                    self.assertEqual(expected.get(case, "seat_execution_history_invalid"), caught.exception.code)
                finally:
                    fixture.doCleanups()


    def test_failed_worker_cleanup_retains_exact_spawn_join_and_pid_evidence(self):
        unused, unused_controller, unused_invocation, accepted = self.prepare(
            {"event": "unknown", "code": "worker_cleanup_unconfirmed", "detail": json.dumps(AUDIT)})
        sources = WorkerReceipts(self.root).records()
        self.roll("failed-worker")
        result = self.plan()
        self.assertEqual(sources, result["worker_records"])
        execution = accepted["execution"]
        self.assertIn({"node_id": "builder", "pid": 987655, "execution_ids": [execution["id"]]},
                      result["pid_receipts"])
        self.assertFalse(any(row["pid"] == 987656 for row in result["pid_receipts"]),
                         "a process-group identifier is not PID ownership")
        joined = next(row for row in result["worker_joins"] if row["execution_id"] == execution["id"])
        self.assertEqual(execution["source_receipt_id"], joined["source"]["id"])
        self.assertEqual(execution["detail"], next(row for row in result["executions"] if row["id"] == execution["id"])["detail"])

    def test_absent_worker_source_does_not_create_pid_ownership(self):
        unused, unused_controller, unused_invocation, accepted = self.prepare(
            {"event": "unknown", "code": "worker_cleanup_unconfirmed", "detail": json.dumps(AUDIT)})
        (self.root.tenant_home / "receipts/workers.jsonl").unlink()
        (self.root.tenant_home / "receipts/workers.jsonl.lock").unlink(missing_ok=True)
        with epoch_guard(self.root, exclusive=True):
            before = self.tree()
            result = checkpoint.plan_execution_checkpoint(self.root)
            self.assertEqual(before, self.tree(), "absent worker plane must remain physically absent")
        self.assertEqual([], result["pid_receipts"])
        self.assertTrue(result["worker_joins"], "missing source absence testimony for native worker execution")
        joined = next(row for row in result["worker_joins"] if row["execution_id"] == accepted["execution"]["id"])
        self.assertIsNone(joined["source"])

    def test_generic_pid_detail_remains_raw_failed_evidence_without_pid_authority(self):
        unused, unused_controller, unused_invocation, accepted = self.prepare(
            {"event": "unknown", "code": "worker_cleanup_unconfirmed", "detail": "PID 987655, cleanup unknown"})
        result = self.plan()
        self.assertEqual([], result["pid_receipts"])
        self.assertIn(accepted["execution"], result["executions"])
        joined = next(row for row in result["worker_joins"] if row["execution_id"] == accepted["execution"]["id"])
        self.assertIsNone(joined["audit_pid"])


    def test_dependency_substitution_during_native_read_blocks(self):
        for relative, mutation in (("events.jsonl", "replace"), ("receipts/workers.jsonl", "replace"),
                                   ("receipts/workers.jsonl", "remove")):
            with self.subTest(relative=relative, mutation=mutation):
                fixture = type(self)()
                fixture.setUp()
                try:
                    fixture.prepare()
                    native = checkpoint._history
                    source = fixture.root.tenant_home / relative
                    lock = source.with_name(source.name + ".lock")
                    if mutation == "remove":
                        lock.unlink(missing_ok=True)
                    def changed(root, **kwargs):
                        result = native(root, **kwargs)
                        if mutation == "remove":
                            source.unlink(missing_ok=True)
                        else:
                            replacement = root.tenant_home / "replacement"
                            replacement.write_bytes(source.read_bytes())
                            replacement.replace(source)
                        return result
                    with mock.patch.object(checkpoint, "_history", changed):
                        with self.assertRaises(ProtocolRefusal) as caught:
                            fixture.plan()
                    self.assertEqual("archive_execution_checkpoint_blocked", caught.exception.code)
                    if mutation == "remove":
                        self.assertFalse(source.exists())
                        self.assertFalse(lock.exists(), "missing worker evidence must not initialize a lock")
                finally:
                    fixture.doCleanups()

    def test_unavailable_dependency_witness_blocks(self):
        self.prepare()
        with mock.patch.object(checkpoint, "carry_dependency_witness", return_value=None):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.plan()
        self.assertEqual("archive_execution_checkpoint_blocked", caught.exception.code)

    def test_strict_history_depth_blocks_instead_of_accepting_legacy_prefix(self):
        from floati import snapshot
        self.prepare()
        self.roll("first")
        self.roll("second")
        with mock.patch.object(snapshot, "MAX_LEDGER_RECORDS", 1):
            with self.assertRaises(FloatiError):
                self.plan()

    def test_empty_execution_domain_remains_a_valid_read_only_plan(self):
        result = self.plan()
        self.assertEqual([], result["executions"])
        self.assertEqual([], result["replays"])
        self.assertEqual([], result["worker_joins"])
        self.assertEqual([], result["pid_receipts"])

    def test_carried_exact_execution_copies_coalesce_across_two_automatic_rolls(self):
        unused, unused_controller, unused_invocation, accepted = self.prepare()
        self.automatic_roll("automatic-one")
        self.automatic_roll("automatic-two")
        result = self.plan()
        self.assertEqual(2, len(result["executions"]))
        self.assertEqual(2, len(result["acceptance_keys"]))
        replay = next(row for row in result["replays"] if row["execution_id"] == accepted["execution"]["id"])
        self.assertEqual(accepted, replay["result"])


    def test_cold_root_with_missing_events_and_worker_locks_is_physically_read_only(self):
        self.prepare()
        for relative in ("events.jsonl.lock", "receipts/workers.jsonl.lock"):
            (self.root.tenant_home / relative).unlink(missing_ok=True)
        with epoch_guard(self.root, exclusive=True):
            before = self.tree()
            error = None
            try:
                result = checkpoint.plan_execution_checkpoint(self.root)
            except FloatiError as caught:
                error = caught
            self.assertEqual(before, self.tree(), "cold planning wrote files before its final witness refusal")
            self.assertIsNone(error, "valid cold planning must succeed without initializing lockfiles")
            self.assertEqual("planned", result["state"])

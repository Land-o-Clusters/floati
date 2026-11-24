"""Governed worker execution retains the native Run/attempt/session join."""
import unittest

import tests  # Pin HOME before resolving any product defaults.
from floati.cancellation import CancellationCoordinator
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.seat_execution import SeatExecutionController
from floati.work import WORK_KINDS, WorkLog
from floati.workers import WorkerReceipts
from tests.test_spawn_groups import _Task2Case


ARTIFACT = {"repo": "floati", "sha": "a" * 40, "doc": "docs/car.md"}
CAR = {"row": "F2-SEAT-STALL-VISIBLE-1", "repository": "floati", "branch": "codex/car"}


class _NativeWorkerCase:
    def __init__(self, testcase, *, started=True, node="node-a"):
        self.run = _Task2Case(testcase)
        self.run.prepare_parent(mode="disabled", start=started)
        self.root = self.run.root
        self.node = node
        registry = Registry(self.root)
        for registered in ("node-a", "architect", "other"):
            registry.register(registered, "Codex")
        self.workspace = self.root.path.parent / "worker-workspace"
        self.workspace.mkdir()
        # The native admitted Run already names this work ID. Use the same
        # WorkLog setup as _EffectWorkerCase, with an actual grant and claim.
        append_record(self.root, "work/items.jsonl", {
            "schema_version": 1, "id": self.run.parent,
            "tenant_id": self.root.tenant_id, "timestamp": "2026-08-10T12:00:07.000Z",
            "kind": "work_item", "title": "governed native car",
            "owner": self.node, "artifact_bindings": [ARTIFACT], "car_binding": CAR,
        }, allowed_kinds=WORK_KINDS)
        self.work = WorkLog(self.root)
        grant = AuthorityGrantStore(self.root).claim("work-claims", self.node, 120, 120, self.run.now(7))
        self.grant = grant
        self.work_claim = self.work.claim(self.run.parent, self.node, "work-claims", grant["epoch"], now=self.run.now(7))
        self.session = "worker-" + uuid7_hex()
        self.receipts = WorkerReceipts(self.root)
        self.receipts.append(self.session, self.run.parent, self.node, "codex", "claim", None, [], now=self.run.now(7))
        self.spawn = self.receipts.append(self.session, self.run.parent, self.node, "codex", "spawn", None, [], now=self.run.now(8))
        self.controller = SeatExecutionController(self.root, recipient="architect")
        self.coordinator = CancellationCoordinator(self.run.ledger)

    def bind_session(self, *, session=None):
        # These four opaque scratch coordinates acquire testimony exclusively
        # through CancellationCoordinator's native, validated writer seam.
        worker_session = session or self.session
        return self.coordinator.bind_harness_session(self.run.run_id, self.run.parent,
            self.run.opened["attempt_id"], claim_id="claim-" + uuid7_hex(),
            lease_id="lease-" + uuid7_hex(), worker_session_id=worker_session,
            harness_segments=[{"ordinal": 1, "harness_session_id": worker_session}], now=self.run.now(8))

    def bind_execution(self, **changes):
        arguments = {"run_id": self.run.run_id, "attempt_id": self.run.opened["attempt_id"]}
        arguments.update(changes)
        return self.controller.bind_worker(self.spawn, self.workspace, **arguments)

    def no_execution(self, testcase):
        testcase.assertFalse(any(row.get("kind") == "seat_execution"
            for row in EventLog(self.root).event_records()))


class WorkerBindingTests(unittest.TestCase):
    def test_native_full_binding_is_retained_by_running_terminal_and_notice(self):
        case = _NativeWorkerCase(self)
        native = case.bind_session()
        invocation = case.bind_execution()
        expected_attempt = {key: native[key] for key in
            ("attempt_id", "claim_id", "lease_id", "worker_session_id")}
        expected_source = {"run_id": case.run.run_id, "attempt_id": case.run.opened["attempt_id"],
            "binding_record_id": native["id"], "attempt_binding": expected_attempt}
        result = case.controller.callback(invocation, {"event": "ended",
            "code": "approval_required_unattended", "detail": "native worker ended and cleaned"})
        rows = [row for row in EventLog(case.root).event_records() if row.get("kind") == "seat_execution"]
        self.assertEqual(["running", "ended"], [row["event"] for row in rows])
        self.assertTrue(all(row["source_binding"] == expected_source for row in rows))
        self.assertTrue(all(row["work_claim_id"] == case.work_claim["id"] for row in rows))
        notice = result["notice"]["message"]
        self.assertEqual(expected_attempt, notice["attempt_binding"])
        self.assertEqual(case.session, notice["worker_session_id"])

    def test_ungoverned_source_is_explicitly_legacy_instead_of_inferred(self):
        case = _NativeWorkerCase(self)
        invocation = case.controller.bind_worker(case.spawn, case.workspace)
        result = case.controller.callback(invocation, {"event": "ended", "code": "process_died", "detail": "cleaned"})
        self.assertEqual("absent_legacy", result["execution"]["source_binding"])
        self.assertEqual("absent_legacy", result["notice"]["message"]["attempt_binding"])

    def test_governed_start_without_native_harness_binding_refuses(self):
        case = _NativeWorkerCase(self)
        with self.assertRaises(ProtocolRefusal):
            case.bind_execution()
        case.no_execution(self)

    def test_native_harness_binding_for_another_worker_session_refuses(self):
        case = _NativeWorkerCase(self)
        case.bind_session(session="worker-" + uuid7_hex())
        with self.assertRaises(ProtocolRefusal):
            case.bind_execution()
        case.no_execution(self)

    def test_dispatch_for_another_worker_does_not_authenticate_spawn(self):
        case = _NativeWorkerCase(self, node="other")
        case.bind_session()
        with self.assertRaises(ProtocolRefusal):
            case.bind_execution()
        case.no_execution(self)

    def test_native_open_dispatched_but_unstarted_attempt_refuses(self):
        case = _NativeWorkerCase(self, started=False)
        case.bind_session()
        with self.assertRaises(ProtocolRefusal):
            case.bind_execution()
        case.no_execution(self)

    def test_another_owned_work_item_cannot_reuse_the_governed_attempt_binding(self):
        case = _NativeWorkerCase(self)
        session = "worker-" + uuid7_hex()
        case.bind_session(session=session)
        other = case.work.add("another actual car", case.node, [ARTIFACT], car_binding=CAR, now=case.run.now(7))
        case.work.claim(other["id"], case.node, "work-claims", case.grant["epoch"], now=case.run.now(7))
        case.receipts.append(session, other["id"], case.node, "codex", "claim", None, [], now=case.run.now(7))
        spawn = case.receipts.append(session, other["id"], case.node, "codex", "spawn", None, [], now=case.run.now(8))
        with self.assertRaises(ProtocolRefusal):
            case.controller.bind_worker(spawn, case.workspace,
                run_id=case.run.run_id, attempt_id=case.run.opened["attempt_id"])
        case.no_execution(self)

    def test_partial_governed_selector_never_falls_back_to_legacy(self):
        case = _NativeWorkerCase(self)
        case.bind_session()
        for case_index, selector in enumerate(({"run_id": case.run.run_id}, {"attempt_id": case.run.opened["attempt_id"]})):
            with self.subTest(case=(
                "run-id-only",
                "attempt-id-only",
            )[case_index]), self.assertRaises(ProtocolRefusal):
                case.controller.bind_worker(case.spawn, case.workspace, **selector)
        case.no_execution(self)

    def test_ambiguous_native_session_bindings_refuse_without_selecting_one(self):
        case = _NativeWorkerCase(self)
        case.bind_session()
        case.bind_session()
        with self.assertRaises(ProtocolRefusal):
            case.bind_execution()
        case.no_execution(self)

    def test_terminal_attempt_cannot_start_a_new_execution_invocation(self):
        case = _NativeWorkerCase(self)
        case.bind_session()
        case.run.scheduler.terminal_attempt(case.run.run_id, case.run.parent,
            case.run.opened["attempt_id"], "failed", "permanent", "process_died", "idempotent", now=case.run.now(9))
        with self.assertRaises(ProtocolRefusal):
            case.bind_execution()
        case.no_execution(self)

    def test_callback_cannot_override_retained_native_attempt_coordinates(self):
        case = _NativeWorkerCase(self)
        case.bind_session()
        invocation = case.bind_execution()
        with self.assertRaises(ProtocolRefusal):
            case.controller.callback(invocation, {"event": "ended", "code": "process_died", "detail": "cleaned",
                "source_binding": "absent_legacy"})
        self.assertFalse(any(row.get("kind") == "message_envelope" for row in EventLog(case.root).event_records()))

from __future__ import annotations

import tempfile
import time
import unittest
import multiprocessing
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from floati.contracts import TaskContract, contract_digest


def _task6_acceptance_process(base, record, start, outcomes):
    root = FloatiRoot.open_direct_home(Path(base), create=False)
    start.wait(10)
    try:
        accepted = RunLedger(root).append(record)
        outcomes.put(("acceptance_ok", accepted["id"]))
    except ProtocolRefusal as exc:
        outcomes.put(("acceptance_refused", exc.code))


def _task6_intent_process(base, policy_path, arguments, start, outcomes):
    from floati.approvals import ApprovalLedger
    from floati.effects import EffectController, EffectLedger
    from floati.policy import RepositoryPolicy

    root = FloatiRoot.open_direct_home(Path(base), create=False)
    controller = EffectController(
        EffectLedger(root), RunLedger(root),
        RepositoryPolicy.load(Path(policy_path)), ApprovalLedger(root),
    )
    start.wait(10)
    try:
        intent = controller.intent(**arguments)
        outcomes.put(("intent_ok", intent["id"]))
    except ProtocolRefusal as exc:
        outcomes.put(("intent_refused", exc.code))


def _hold_acceptance_lock_beyond_generic_budget(base, ready):
    import fcntl

    root = FloatiRoot.open_direct_home(Path(base), create=False)
    lock_path = root.resolve_relative("effects/acceptance.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        ready.set()
        time.sleep(1.25)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

try:
    from floati.runtruth import DependencyEdge, RUN_KINDS, RunLedger, RunProjection
except ModuleNotFoundError:
    DependencyEdge = RUN_KINDS = RunLedger = RunProjection = None

try:
    from floati.scheduler import RetryPolicy, RunScheduler
except ModuleNotFoundError:
    RetryPolicy = RunScheduler = None


NOW = "2026-08-02T12:00:00.000Z"
DIGEST = "a" * 64


class RunTruthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.assertIsNotNone(RunLedger, "floati.runtruth must provide RunLedger")
        self.ledger = RunLedger(self.root)
        self.scheduler = None if RunScheduler is None else RunScheduler(self.ledger)
        self.run_id = "run-" + uuid7_hex()
        self.first, self.second = sorted(("work-" + uuid7_hex(), "work-" + uuid7_hex()))
        self.attempt = "attempt-" + uuid7_hex()
        self.contract_bound = False

    def record(self, kind: str, **fields: object) -> dict:
        prefixes = {"task_contract": "task-contract-", "plan_amendment": "plan-amendment-", "run_created": "run-created-", "run_policy_bound": "run-policy-bound-", "worker_pool_bound": "run-worker-pool-bound-", "attempt_opened": "attempt-opened-", "dispatch_decision": "run-dispatch-decision-", "result_produced": "run-result-produced-", "result_verified": "run-result-verified-", "acceptance_receipt": "acceptance-receipt-", "result_accepted": "run-result-accepted-", "run_terminal": "run-terminal-"}
        return {"schema_version": 0, "id": prefixes[kind] + uuid7_hex(),
                "tenant_id": "alpha", "timestamp": NOW, "kind": kind, **fields}

    def create(self, *, edges=(), policy_digest=None):
        fields = {"run_id": self.run_id, "plan_digest": DIGEST,
                  "item_ids": [self.first, self.second], "dependency_edges": list(edges)}
        if policy_digest is not None:
            fields["policy_digest"] = policy_digest
        return self.ledger.append(self.record("run_created", **fields))

    def bind(self):
        self.ledger.append(self.record("run_policy_bound", run_id=self.run_id, policy_digest=DIGEST))
        self.ledger.append(self.record("worker_pool_bound", run_id=self.run_id, worker_ids=["worker-a", "worker-b"]))

    def dispatch(self, item=None, attempt=None):
        """Catches dispatches that bypass the first-class opened attempt boundary."""
        self.assertIsNotNone(self.scheduler, "floati.scheduler must provide RunScheduler")
        if not self.contract_bound:
            self.bind_contract()
        opened = self.scheduler.open_attempt(
            self.run_id, item or self.first,
            RetryPolicy(max_attempts=2, base_delay_ms=10, cap_delay_ms=10), 1,
            now=NOW,
        )
        self.attempt = opened["attempt_id"]
        return self.ledger.append(self.record("dispatch_decision", run_id=self.run_id,
            item_id=item or self.first, attempt_id=attempt or self.attempt,
            eligible_workers=["worker-a", "worker-b"], chosen_worker="worker-a",
            capability_digest=DIGEST, reason_code="policy.route", policy_digest=DIGEST,
            routing_rank=0, scheduler_epoch=1))

    def test_minimal_run_lifecycle_projects_in_physical_append_order(self) -> None:
        self.create(edges=[{"source": self.first, "target": self.second}])
        self.bind_contract()
        self.bind(); dispatch = self.dispatch()
        self.scheduler.start_attempt(self.run_id, self.first, self.attempt, dispatch["id"], now=NOW)
        receipt = self.receipt()
        produced = self.ledger.append(self.record("result_produced", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, dispatch_decision_id=dispatch["id"], worker_receipt_ids=[receipt["id"]]))
        verified = self.ledger.append(self.record("result_verified", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, result_produced_id=produced["id"], worker_receipt_ids=[receipt["id"]]))
        acceptance = self.acceptance_receipt(evidence_bindings=[receipt["id"]])
        self.ledger.append(self.record("result_accepted", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, predecessor_result_id=verified["id"], acceptance_mode="verified",
            acceptance_receipt_id=acceptance["id"], worker_receipt_ids=[receipt["id"]]))
        projection = self.ledger.project().run(self.run_id)
        self.assertEqual([self.first, self.second], projection["item_ids"])
        self.assertEqual("accepted", self.ledger.project().edges(self.run_id)[0].requires)

    def receipt(self) -> dict:
        # Raw evidence is deliberately external to the run ledger.
        from floati.jsonl import append_record
        receipt = {"schema_version": 0, "id": "worker-receipt-" + uuid7_hex(), "tenant_id": "alpha", "timestamp": NOW,
            "kind": "worker_receipt", "session_id": "worker-" + uuid7_hex(), "work_item_id": self.first,
            "node_id": "worker-a", "adapter": "codex", "transition": "claim", "outcome_code": None,
            "authority_subject": "authority", "authority_epoch": 1, "artifact_bindings": []}
        append_record(self.root, "receipts/workers.jsonl", receipt, allowed_kinds={"worker_receipt"})
        return receipt

    def contract(self, *, objective="durable acceptance") -> TaskContract:
        return TaskContract.create(
            objective=objective, non_goals=["no semantic score"],
            areas_to_avoid=[{"path": "slip/graph.py", "region": "all"}],
            input_hashes={"brief": DIGEST}, acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"}, risk_class="high",
            retry_policy={"max_attempts": 2, "backoff": {"base_delay_ms": 10, "cap_delay_ms": 10, "strategy": "exponential"}},
            dependencies=[],
        )

    def bind_contract(self, contract=None, *, repository=None) -> dict:
        contract = contract or self.contract()
        fields = {
            "run_id": self.run_id, "item_id": self.first,
            **contract.canonical(), "contract_digest": contract_digest(contract),
        }
        if repository is not None:
            fields["repository"] = repository
        record = self.ledger.append(self.record("task_contract", **fields))
        self.contract_bound = True
        return record

    def acceptance_receipt(self, *, item_id=None, attempt_id=None, evidence_bindings=()) -> dict:
        return self.ledger.append(self.record("acceptance_receipt", run_id=self.run_id,
            item_id=item_id or self.first, attempt_id=attempt_id or self.attempt,
            contract_digest=contract_digest(self.contract()), check_ids=["tests.unit"], reviewer="reviewer-a",
            evidence_bindings=list(evidence_bindings), deviations=[], result="accepted"))

    def test_task_contract_and_amendment_replay_as_the_item_current_contract(self) -> None:
        """Catches SHA-shaped task contracts/amendments that are not durable, item-bound physical-order transitions."""
        self.create()
        initial = self.bind_contract()
        replacement = self.contract(objective="amended durable acceptance")
        amendment = self.ledger.append(self.record("plan_amendment", run_id=self.run_id, item_id=self.first,
            task_contract_id=initial["id"], previous_digest=initial["contract_digest"],
            replacement_fields={"objective": "amended durable acceptance"}, contract_digest=contract_digest(replacement)))
        projected = self.ledger.project().task_contract(self.run_id, self.first)
        self.assertEqual(contract_digest(replacement), projected["contract_digest"])
        self.assertEqual([initial["id"], amendment["id"]], projected["history_ids"])

    def test_optional_contract_repository_is_preserved_outside_the_governed_digest(self) -> None:
        """Catches a repository binding that is dropped, inferred into task intent, or mutable through an amendment."""
        self.create()
        contract = self.contract()
        initial = self.bind_contract(contract, repository="Owner/Repo")
        projected = self.ledger.project().task_contract(self.run_id, self.first)
        self.assertEqual("Owner/Repo", projected["repository"])
        self.assertNotIn("repository", projected["contract"])
        self.assertEqual(contract_digest(contract), initial["contract_digest"])

        legacy_root = FloatiRoot.open(Path(self.temp.name) / "legacy", "alpha")
        legacy_ledger = RunLedger(legacy_root)
        legacy_run = "run-" + uuid7_hex()
        legacy_item = "work-" + uuid7_hex()
        legacy_ledger.append(self.record("run_created", run_id=legacy_run, plan_digest=DIGEST,
            item_ids=[legacy_item], dependency_edges=[]))
        legacy_ledger.append(self.record("task_contract", run_id=legacy_run, item_id=legacy_item,
            **contract.canonical(), contract_digest=contract_digest(contract)))
        legacy = legacy_ledger.project().task_contract(legacy_run, legacy_item)
        self.assertNotIn("repository", legacy)

    def test_persisted_repository_amendment_tampering_refuses_on_physical_replay(self) -> None:
        """Catches an injected repository rewrite even though task semantics retain their existing digest domain."""
        from floati.framing import encode_frame

        self.create()
        initial = self.bind_contract(repository="Owner/Repo")
        tampered = self.record(
            "plan_amendment",
            run_id=self.run_id,
            item_id=self.first,
            task_contract_id=initial["id"],
            previous_digest=initial["contract_digest"],
            replacement_fields={"repository": "Other/Repo"},
            contract_digest="b" * 64,
        )
        path = self.root.resolve_relative(self.ledger.relative_path)
        path.write_bytes(path.read_bytes() + encode_frame(tampered))
        with self.assertRaises(IntegrityFailure) as replay:
            self.ledger.project()
        self.assertEqual("replacement_fields_invalid", replay.exception.code)

    def test_projection_refuses_persisted_attempt_with_a_noncontract_retry_policy(self) -> None:
        """Catches replay accepting a scheduler-authenticated attempt whose retry terms bypass its frozen contract."""
        from floati.runtruth import attempt_fence_token

        self.create()
        self.bind_contract()
        self.bind()
        scheduler = RunScheduler(self.ledger)
        candidate = self.record("attempt_opened", run_id=self.run_id, item_id=self.first,
            attempt_id="attempt-" + uuid7_hex(), ordinal=1, scheduler_epoch=1,
            fence_token=attempt_fence_token(self.run_id, self.first, 1, 1), max_attempts=2,
            backoff={"strategy": "fixed", "base_delay_ms": 10, "cap_delay_ms": 10,
                     "jitter": "sha256_25pct"})
        with self.assertRaises(ProtocolRefusal) as mismatch:
            self.ledger._append_scheduler(candidate, scheduler._RunScheduler__scheduler_capability)
        self.assertEqual("task_contract_policy_mismatch", mismatch.exception.code)

    def test_contract_and_receipt_refuse_stale_or_fake_provenance(self) -> None:
        """Catches a cross-item/stale amendment or receipt with undeclared checks and nonexistent evidence."""
        self.create()
        initial = self.bind_contract()
        with self.assertRaises(ProtocolRefusal):
            self.ledger.append(self.record("plan_amendment", run_id=self.run_id, item_id=self.second,
                task_contract_id=initial["id"], previous_digest=initial["contract_digest"],
                replacement_fields={"objective": "wrong item"}, contract_digest="b" * 64))
        invalid = self.contract()
        with self.assertRaises(ProtocolRefusal) as digest:
            self.ledger.append(self.record("task_contract", run_id=self.run_id, item_id=self.second,
                **invalid.canonical(), contract_digest="b" * 64))
        self.assertEqual("task_contract_digest_invalid", digest.exception.code)
        self.bind(); dispatch = self.dispatch(); self.scheduler.start_attempt(self.run_id, self.first, self.attempt, dispatch["id"], now=NOW)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.append(self.record("acceptance_receipt", run_id=self.run_id, item_id=self.first,
                attempt_id=self.attempt, contract_digest=initial["contract_digest"], check_ids=["not.declared"],
                reviewer="reviewer-a", evidence_bindings=["worker-receipt-" + uuid7_hex()], deviations=[], result="accepted"))
        self.assertEqual("acceptance_receipt_invalid", caught.exception.code)
        with self.assertRaises(ProtocolRefusal) as evidence:
            self.ledger.append(self.record("acceptance_receipt", run_id=self.run_id, item_id=self.first,
                attempt_id=self.attempt, contract_digest=initial["contract_digest"], check_ids=["tests.unit"],
                reviewer="reviewer-a", evidence_bindings=["worker-receipt-" + uuid7_hex()], deviations=[], result="accepted"))
        self.assertEqual("acceptance_receipt_invalid", evidence.exception.code)

    def test_contract_freezes_before_the_first_attempt_and_cannot_rewrite_acceptance(self) -> None:
        """Catches an amendment that changes acceptance authority after an item has begun work."""
        self.create(); initial = self.bind_contract(); self.bind(); dispatch = self.dispatch()
        self.scheduler.start_attempt(self.run_id, self.first, self.attempt, dispatch["id"], now=NOW)
        worker = self.receipt()
        produced = self.ledger.append(self.record("result_produced", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, dispatch_decision_id=dispatch["id"], worker_receipt_ids=[worker["id"]]))
        verified = self.ledger.append(self.record("result_verified", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, result_produced_id=produced["id"], worker_receipt_ids=[worker["id"]]))
        amended = self.contract().replaced(acceptance_checks={"post.work": "must not apply"})
        with self.assertRaises(ProtocolRefusal) as frozen:
            self.ledger.append(self.record("plan_amendment", run_id=self.run_id, item_id=self.first,
                task_contract_id=initial["id"], previous_digest=initial["contract_digest"],
                replacement_fields={"acceptance_checks": {"post.work": "must not apply"}},
                contract_digest=contract_digest(amended)))
        self.assertEqual("task_contract_frozen", frozen.exception.code)
        acceptance = self.acceptance_receipt(evidence_bindings=[worker["id"]])
        self.ledger.append(self.record("result_accepted", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, predecessor_result_id=verified["id"], acceptance_mode="verified",
            acceptance_receipt_id=acceptance["id"], worker_receipt_ids=[worker["id"]]))

    def test_attempt_and_unverified_acceptance_refuse_without_a_task_contract(self) -> None:
        """Catches result work that starts or reaches accepted_unverified without a bound task contract."""
        self.create(); self.bind()
        with self.assertRaises(ProtocolRefusal) as opened:
            self.scheduler.open_attempt(self.run_id, self.first, RetryPolicy(max_attempts=1, base_delay_ms=0, cap_delay_ms=0), 1, now=NOW)
        self.assertEqual("task_contract_missing", opened.exception.code)
        self.bind_contract()
        dispatch = self.dispatch(); self.scheduler.start_attempt(self.run_id, self.first, self.attempt, dispatch["id"], now=NOW)
        worker = self.receipt()
        produced = self.ledger.append(self.record("result_produced", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, dispatch_decision_id=dispatch["id"], worker_receipt_ids=[worker["id"]]))
        accepted = self.ledger.append(self.record("result_accepted", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, predecessor_result_id=produced["id"], acceptance_mode="accepted_unverified",
            acceptance_receipt_id=None, worker_receipt_ids=[worker["id"]]))
        self.assertEqual("accepted_unverified", accepted["acceptance_mode"])

    def test_run_records_refuse_unknown_fields_without_append(self) -> None:
        row = self.record("run_created", run_id=self.run_id, plan_digest=DIGEST, item_ids=[self.first], dependency_edges=[], extra=True)
        with self.assertRaises(ProtocolRefusal): self.ledger.append(row)
        self.assertEqual([], self.ledger.records())

    def test_forward_and_cross_run_references_refuse(self) -> None:
        self.create(); self.bind()
        with self.assertRaises(ProtocolRefusal):
            self.ledger.append(self.record("result_produced", run_id=self.run_id, item_id=self.first, attempt_id=self.attempt,
                dispatch_decision_id="run-dispatch-decision-" + uuid7_hex(), worker_receipt_ids=[]))
        other = "run-" + uuid7_hex()
        with self.assertRaises(ProtocolRefusal):
            self.ledger.append(self.record("run_policy_bound", run_id=other, policy_digest=DIGEST))

    def test_duplicate_bindings_results_and_terminal_refuse(self) -> None:
        self.create(); self.ledger.append(self.record("run_policy_bound", run_id=self.run_id, policy_digest=DIGEST))
        with self.assertRaises(ProtocolRefusal): self.ledger.append(self.record("run_policy_bound", run_id=self.run_id, policy_digest=DIGEST))

    def test_run_created_crash_rollback_and_idempotent_retry(self) -> None:
        row = self.record("run_created", run_id=self.run_id, plan_digest=DIGEST, item_ids=[self.first, self.second], dependency_edges=[])
        self.ledger.append(row)
        self.assertEqual(row, self.ledger.append(row))
        self.assertEqual([row], self.ledger.records())

    def test_active_append_preserves_legacy_bytes_and_writes_only_segment(self) -> None:
        """Catches RunLedger appending to the immutable legacy prefix after explicit activation."""
        from floati.run_segments import SegmentedRunStore

        created = self.create()
        legacy = self.root.resolve_relative(RunLedger.relative_path)
        legacy_bytes = legacy.read_bytes()
        store = SegmentedRunStore(self.root, RUN_KINDS)
        store.activate(now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc))
        bound = self.ledger.append(self.record(
            "run_policy_bound", run_id=self.run_id, policy_digest=DIGEST
        ))

        self.assertEqual(legacy_bytes, legacy.read_bytes())
        self.assertGreater(
            self.root.resolve_relative("runs/segments/00000000.jsonl").stat().st_size,
            0,
        )
        self.assertEqual([created, bound], store.records())

    def test_records_and_project_replay_legacy_prefix_then_segment_records(self) -> None:
        """Catches canonical reads or projection dropping active segment records or exposing metadata."""
        from floati.run_segments import SegmentedRunStore

        created = self.create()
        store = SegmentedRunStore(self.root, RUN_KINDS)
        store.activate(now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc))
        bound = self.record("run_policy_bound", run_id=self.run_id, policy_digest=DIGEST)
        store.transact(lambda snapshot: (bound, bound))

        self.assertEqual([created, bound], self.ledger.records())
        projected = self.ledger.project().run(self.run_id)
        self.assertEqual(bound, projected["policy"])
        self.assertEqual([created, bound], projected["records"])
        self.assertNotIn("segment_number", projected["records"][1])

    def test_project_streams_without_materializing_through_records(self) -> None:
        """Catches projection rebuilding the canonical store iterator through its full-copy records API."""
        from floati.run_segments import SegmentedRunStore

        self.create()
        SegmentedRunStore(self.root, RUN_KINDS).activate(
            now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
        )
        bound = self.ledger.append(self.record(
            "run_policy_bound", run_id=self.run_id, policy_digest=DIGEST
        ))

        with mock.patch.object(
            self.ledger._store,
            "records",
            side_effect=AssertionError("projection materialized canonical records"),
        ):
            projected = self.ledger.project().run(self.run_id)
        self.assertEqual(bound, projected["policy"])

    def test_exact_retry_is_idempotent_on_active_root(self) -> None:
        """Catches an active-root retry duplicating a frame or falling back to legacy storage."""
        from floati.run_segments import SegmentedRunStore

        store = SegmentedRunStore(self.root, RUN_KINDS)
        store.activate(now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc))
        row = self.record(
            "run_created", run_id=self.run_id, plan_digest=DIGEST,
            item_ids=[self.first, self.second], dependency_edges=[],
        )

        self.assertEqual(row, self.ledger.append(row))
        self.assertEqual(row, self.ledger.append(dict(row)))
        self.assertEqual([row], store.records())
        self.assertFalse(self.root.resolve_relative(RunLedger.relative_path).exists())

    def test_divergent_retry_refuses_without_mutating_active_root(self) -> None:
        """Catches RunLedger treating a divergent active-root record ID as an exact retry."""
        from floati.run_segments import SegmentedRunStore

        store = SegmentedRunStore(self.root, RUN_KINDS)
        store.activate(now=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc))
        row = self.record(
            "run_created", run_id=self.run_id, plan_digest=DIGEST,
            item_ids=[self.first, self.second], dependency_edges=[],
        )
        self.ledger.append(row)
        segment = self.root.resolve_relative("runs/segments/00000000.jsonl")
        metadata = self.root.resolve_relative("runs/segments/events.jsonl")
        before = (segment.read_bytes(), metadata.read_bytes())

        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.append(dict(row, plan_digest="b" * 64))

        self.assertEqual("duplicate_record_id", caught.exception.code)
        self.assertEqual(before, (segment.read_bytes(), metadata.read_bytes()))
        self.assertEqual([row], store.records())

    def test_empty_legacy_read_and_project_do_not_activate_segments(self) -> None:
        """Catches a read-only RunLedger operation creating the segmented namespace."""
        segments = self.root.resolve_relative("runs/segments")

        self.assertEqual([], self.ledger.records())
        self.assertIsInstance(self.ledger.project(), RunProjection)
        self.assertFalse(segments.exists())

    def test_run_policy_binding_must_match_the_durable_admitted_pair(self) -> None:
        """Catches a later policy frame that changes the policy admitted with run creation."""
        self.create(policy_digest=DIGEST)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.append(self.record(
                "run_policy_bound", run_id=self.run_id, policy_digest="b" * 64
            ))
        self.assertEqual("admitted_pair_policy_mismatch", caught.exception.code)

    def test_persisted_admitted_pair_mismatch_is_an_integrity_failure(self) -> None:
        """Catches a malformed durable history whose later policy frame changes the admitted pair."""
        from floati.jsonl import append_record

        append_record(
            self.root,
            RunLedger.relative_path,
            self.record(
                "run_created", run_id=self.run_id, plan_digest=DIGEST,
                policy_digest=DIGEST, item_ids=[self.first, self.second], dependency_edges=[],
            ),
            allowed_kinds={"run_created", "run_policy_bound"},
        )
        append_record(
            self.root,
            RunLedger.relative_path,
            self.record("run_policy_bound", run_id=self.run_id, policy_digest="b" * 64),
            allowed_kinds={"run_created", "run_policy_bound"},
        )

        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.project()
        self.assertEqual("admitted_pair_policy_mismatch", caught.exception.code)

    def test_durable_pair_is_pending_after_the_first_append_until_it_is_bound(self) -> None:
        """Catches recovery that discards a persisted pair during the permitted append gap."""
        self.create(policy_digest=DIGEST)

        self.assertEqual(
            {
                "status": "pending",
                "plan_digest": DIGEST,
                "policy_digest": DIGEST,
            },
            self.ledger.project().run(self.run_id).get("admitted_pair_proof"),
        )

    def test_projection_run_returns_owned_mappings(self) -> None:
        """Catches callers mutating canonical spawn or legacy projection state through reads."""
        self.create()
        projection = self.ledger.project()
        first = projection.run(self.run_id)
        first["item_ids"].clear()
        first["spawn_groups"]["forged"] = {}

        second = projection.run(self.run_id)
        self.assertEqual([self.first, self.second], second["item_ids"])
        self.assertNotIn("forged", second["spawn_groups"])

    def test_legacy_run_created_projects_typed_pair_proof_unavailable_without_backfill(self) -> None:
        """Catches recovery that infers a historical admission pair from a later binding."""
        self.create()
        self.bind()

        projection = self.ledger.project().run(self.run_id)

        self.assertEqual(
            {
                "status": "unavailable",
                "reason_code": "admitted_pair_proof_unavailable",
            },
            projection.get("admitted_pair_proof"),
        )
        self.assertNotIn("policy_digest", projection["records"][0])

    def test_run_ledger_fuzz_malformed_truncated_duplicate_and_oversize_fail_closed(self) -> None:
        absolute = self.root.resolve_relative(RunLedger.relative_path)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        for payload in (b'{bad}\n', b'{"kind":"run_created"}', b'x' * 65537 + b'\n'):
            absolute.write_bytes(payload)
            with self.assertRaises(IntegrityFailure): self.ledger.records()

    def test_dispatch_requires_sorted_unique_eligible_workers(self) -> None:
        self.create(); self.bind()
        row = self.record("dispatch_decision", run_id=self.run_id, item_id=self.first, attempt_id=self.attempt,
            eligible_workers=["worker-b", "worker-a", "worker-a"], chosen_worker="worker-a", capability_digest=DIGEST,
            reason_code="policy.route", policy_digest=DIGEST, routing_rank=0, scheduler_epoch=1)
        with self.assertRaises(ProtocolRefusal): self.ledger.append(row)

    def test_dispatch_requires_chosen_worker_in_bound_pool(self) -> None:
        self.create(); self.bind()
        row = self.record("dispatch_decision", run_id=self.run_id, item_id=self.first, attempt_id=self.attempt,
            eligible_workers=["worker-a", "worker-z"], chosen_worker="worker-z", capability_digest=DIGEST,
            reason_code="policy.route", policy_digest=DIGEST, routing_rank=0, scheduler_epoch=1)
        with self.assertRaises(ProtocolRefusal): self.ledger.append(row)

    def test_dependency_edge_defaults_to_accepted(self) -> None:
        edge = DependencyEdge(self.first, self.second)
        self.assertEqual("accepted", edge.requires)

    def test_run_created_persists_defaulted_dependency_requirement(self) -> None:
        self.create(edges=[{"source": self.first, "target": self.second}])
        raw = self.ledger.records()[0]
        self.assertEqual(
            [{"source": self.first, "target": self.second, "requires": "accepted", "failure_policy": "fail_run"}],
            raw["dependency_edges"],
        )

    def test_dependency_edge_accepts_only_produced_verified_accepted(self) -> None:
        for value in ("produced", "verified", "accepted"):
            self.assertEqual(value, DependencyEdge(self.first, self.second, value).requires)
        with self.assertRaises(ProtocolRefusal): DependencyEdge(self.first, self.second, "done")

    def test_fail_run_precedes_cancelled_skipped_without_spawn_override(self) -> None:
        def cancelled_source(failure_policy: str):
            root = FloatiRoot.open(
                Path(self.temp.name) / failure_policy, "alpha",
            )
            ledger = RunLedger(root)
            scheduler = RunScheduler(ledger)
            run_id = "run-" + uuid7_hex()
            source, target = sorted((
                "work-" + uuid7_hex(), "work-" + uuid7_hex(),
            ))

            def record(kind: str, prefix: str, **fields: object) -> dict:
                return {
                    "schema_version": 0,
                    "id": prefix + uuid7_hex(),
                    "tenant_id": "alpha",
                    "timestamp": NOW,
                    "kind": kind,
                    **fields,
                }

            ledger.append(record(
                "run_created", "run-created-", run_id=run_id,
                plan_digest=DIGEST, item_ids=[source, target],
                dependency_edges=[{
                    "source": source, "target": target,
                    "requires": "accepted", "failure_policy": failure_policy,
                }],
            ))
            contract = self.contract()
            ledger.append(record(
                "task_contract", "task-contract-", run_id=run_id,
                item_id=source, **contract.canonical(),
                contract_digest=contract_digest(contract),
            ))
            ledger.append(record(
                "run_policy_bound", "run-policy-bound-",
                run_id=run_id, policy_digest=DIGEST,
            ))
            ledger.append(record(
                "worker_pool_bound", "run-worker-pool-bound-",
                run_id=run_id, worker_ids=["worker-a"],
            ))
            policy = RetryPolicy(2, 10, 10)
            opened = scheduler.open_attempt(run_id, source, policy, 1, now=NOW)
            dispatch = ledger.append(record(
                "dispatch_decision", "run-dispatch-decision-", run_id=run_id,
                item_id=source, attempt_id=opened["attempt_id"],
                eligible_workers=["worker-a"], chosen_worker="worker-a",
                capability_digest=DIGEST, reason_code="policy.route",
                policy_digest=DIGEST, routing_rank=0, scheduler_epoch=1,
            ))
            scheduler.start_attempt(
                run_id, source, opened["attempt_id"], dispatch["id"], now=NOW,
            )
            scheduler.terminal_attempt(
                run_id, source, opened["attempt_id"], "cancelled", "cancelled",
                "operator_cancellation", "idempotent", now=NOW,
            )
            return ledger, run_id, record

        lawful, lawful_run_id, lawful_record = cancelled_source("skip_dependent")
        self.assertEqual("cancelled", lawful.project().run_outcome(lawful_run_id))
        lawful_terminal = lawful.append(lawful_record(
            "run_terminal", "run-terminal-", run_id=lawful_run_id,
            outcome="cancelled",
        ))
        self.assertEqual("cancelled", lawful_terminal["outcome"])

        hostile, hostile_run_id, hostile_record = cancelled_source("fail_run")
        with self.subTest(contract="projection"):
            self.assertEqual("failed", hostile.project().run_outcome(hostile_run_id))
        before = hostile.records()
        with self.subTest(contract="terminal_refusal"):
            with self.assertRaises(ProtocolRefusal) as refusal:
                hostile.append(hostile_record(
                    "run_terminal", "run-terminal-", run_id=hostile_run_id,
                    outcome="cancelled",
                ))
            self.assertEqual("run_terminal_invalid", refusal.exception.code)
            self.assertEqual(before, hostile.records())

    def test_result_transitions_require_prior_matching_raw_receipts(self) -> None:
        self.create(); self.bind(); dispatch = self.dispatch()
        with self.assertRaises(ProtocolRefusal):
            self.ledger.append(self.record("result_produced", run_id=self.run_id, item_id=self.first, attempt_id=self.attempt,
                dispatch_decision_id=dispatch["id"], worker_receipt_ids=["worker-receipt-" + uuid7_hex()]))

    def test_accepted_unverified_requires_produced_and_no_verified_result(self) -> None:
        self.create(); self.bind_contract(); self.bind(); dispatch = self.dispatch()
        self.scheduler.start_attempt(self.run_id, self.first, self.attempt, dispatch["id"], now=NOW)
        receipt = self.receipt()
        produced = self.ledger.append(self.record("result_produced", run_id=self.run_id, item_id=self.first, attempt_id=self.attempt,
            dispatch_decision_id=dispatch["id"], worker_receipt_ids=[receipt["id"]]))
        accepted = self.ledger.append(self.record("result_accepted", run_id=self.run_id, item_id=self.first, attempt_id=self.attempt,
            predecessor_result_id=produced["id"], acceptance_mode="accepted_unverified",
            acceptance_receipt_id=None, worker_receipt_ids=[receipt["id"]]))
        self.assertEqual("accepted_unverified", accepted["acceptance_mode"])

    def test_verified_result_acceptance_requires_a_matching_durable_receipt(self) -> None:
        """Catches verified acceptance that has checks but no durable contract-bound reviewer receipt."""
        self.create(); self.bind(); dispatch = self.dispatch()
        self.scheduler.start_attempt(self.run_id, self.first, self.attempt, dispatch["id"], now=NOW)
        worker = self.receipt()
        produced = self.ledger.append(self.record("result_produced", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, dispatch_decision_id=dispatch["id"], worker_receipt_ids=[worker["id"]]))
        verified = self.ledger.append(self.record("result_verified", run_id=self.run_id, item_id=self.first,
            attempt_id=self.attempt, result_produced_id=produced["id"], worker_receipt_ids=[worker["id"]]))
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.append(self.record("result_accepted", run_id=self.run_id, item_id=self.first,
                attempt_id=self.attempt, predecessor_result_id=verified["id"], acceptance_mode="verified",
                acceptance_receipt_id="acceptance-receipt-" + uuid7_hex(), worker_receipt_ids=[worker["id"]]))
        self.assertEqual("acceptance_receipt_missing", caught.exception.code)

    def test_persisted_causal_reordering_is_integrity_failure(self) -> None:
        row = self.record("run_policy_bound", run_id=self.run_id, policy_digest=DIGEST)
        from floati.jsonl import append_record
        append_record(self.root, RunLedger.relative_path, row, allowed_kinds={"run_policy_bound"})
        with self.assertRaises(IntegrityFailure): self.ledger.project()


class EffectAcceptanceTests(unittest.TestCase):
    """Cross-ledger acceptance is exact, retry-safe, and race-free."""

    def test_acceptance_guard_waits_beyond_generic_ledger_lock_budget(self) -> None:
        """Lawful queued fences use their own bounded concurrency budget."""
        from floati.runtruth import effect_acceptance_guard

        case = self._case()
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        holder = context.Process(
            target=_hold_acceptance_lock_beyond_generic_budget,
            args=(str(case.root.tenant_home), ready),
        )
        holder.start()
        self.assertTrue(ready.wait(5))
        try:
            with effect_acceptance_guard(case.root):
                pass
        finally:
            holder.join(5)
            if holder.is_alive():
                holder.terminate()
                holder.join(5)
        self.assertEqual(0, holder.exitcode)

    def _case(self):
        from tests.test_effect_controller import _EffectCase

        return _EffectCase(self)

    @staticmethod
    def _confirmed_effect(case, *, key="effect-one", spend=1):
        from floati.effects import EffectLedger
        from floati.framing import encode_frame
        from floati.records import EFFECT_BINDING_FIELDS

        intent = case.controller.intent(**case.intent_args(idempotency_key=key))
        dispatched = case.controller.dispatched(
            intent["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64,
            now=case.intent_args()["now"],
        )
        acknowledged = case.controller.acknowledged(
            intent["operation_id"], acknowledgement_digest="e" * 64,
            now=case.intent_args()["now"],
        )
        confirmed = {
            "schema_version": 1,
            "id": "effect-confirmed-" + uuid7_hex(),
            "tenant_id": case.root.tenant_id,
            "timestamp": "2026-08-09T14:00:22.000Z",
            "kind": "effect_confirmed",
            **{field: intent[field] for field in EFFECT_BINDING_FIELDS},
            "effect_intent_id": intent["id"],
            "effect_dispatched_id": dispatched["id"],
            "effect_acknowledged_id": acknowledged["id"],
            "confirmation": intent["expected_confirmation"],
            "confirmation_evidence_digest": "f" * 64,
            "measured_spend": [{"budget_id": "build", "amount": spend}],
            "confirmed_at_testimony": "2026-08-09T14:00:22.000Z",
        }
        path = case.root.resolve_relative(EffectLedger.relative_path)
        path.write_bytes(path.read_bytes() + encode_frame(confirmed))
        return intent, confirmed

    @staticmethod
    def _bound(candidate, evidence):
        return {
            **candidate,
            "schema_version": 1,
            "effect_operation_ids": list(evidence.operation_ids),
            "effect_ledger_high_watermark": evidence.high_watermark,
            "effect_evidence_digest": evidence.evidence_digest,
        }

    @staticmethod
    def _advance_effect_prefix(case, intent, confirmed):
        from tests.test_effects import EffectRecordFixture
        from floati.effects import EffectLedger
        from floati.framing import encode_frame
        from floati.records import EFFECT_BINDING_FIELDS

        proposed = EffectRecordFixture().rows()["compensation_proposed"]
        proposed.update({field: intent[field] for field in EFFECT_BINDING_FIELDS})
        proposed.update({
            "tenant_id": case.root.tenant_id,
            "effect_intent_id": intent["id"],
            "source_effect_evidence_id": confirmed["id"],
        })
        path = case.root.resolve_relative(EffectLedger.relative_path)
        path.write_bytes(path.read_bytes() + encode_frame(proposed))
        return proposed

    @staticmethod
    def _append_post_acceptance_intent(case):
        from floati.effects import EffectLedger
        from floati.framing import encode_frame
        from floati.records import EFFECT_BINDING_FIELDS
        from tests.test_effects import EffectRecordFixture

        fixture = EffectRecordFixture()
        later = fixture.rows()["effect_intent"]
        later_binding = {
            **fixture.binding(),
            "run_id": case.run.run_id,
            "item_id": case.item_id,
            "attempt_id": case.opened["attempt_id"],
            "attempt_started_id": case.started["id"],
            "fence_token": case.opened["fence_token"],
            "idempotency_key": "post-acceptance-hostile-intent",
            "budget_claim": [{"budget_id": "build", "amount": 0}],
        }
        later.update({
            field: deepcopy(later_binding[field])
            for field in EFFECT_BINDING_FIELDS
        })
        later["tenant_id"] = case.root.tenant_id
        path = case.root.resolve_relative(EffectLedger.relative_path)
        path.write_bytes(path.read_bytes() + encode_frame(later))
        return later

    @staticmethod
    def _complete_no_effect_item(case, item_id, worker="node-b"):
        from floati.jsonl import append_record

        dispatch = case.run.dispatch(item_id, worker)
        opened = case.run.opened[item_id]
        case.run.scheduler.start_attempt(
            case.run.run_id, item_id, opened["attempt_id"], dispatch["id"],
            now=case.intent_args()["now"],
        )
        receipt = {
            "schema_version": 0,
            "id": "worker-receipt-" + uuid7_hex(),
            "tenant_id": case.root.tenant_id,
            "timestamp": "2026-08-09T14:00:22.000Z",
            "kind": "worker_receipt",
            "session_id": "worker-" + uuid7_hex(),
            "work_item_id": item_id,
            "node_id": worker,
            "adapter": "codex",
            "transition": "claim",
            "outcome_code": None,
            "authority_subject": "execute-run",
            "authority_epoch": 1,
            "artifact_bindings": [],
        }
        append_record(
            case.root, "receipts/workers.jsonl", receipt,
            allowed_kinds={"worker_receipt"},
        )
        produced = case.run._append(
            "result_produced", "run-result-produced-", item_id=item_id,
            attempt_id=opened["attempt_id"], dispatch_decision_id=dispatch["id"],
            worker_receipt_ids=[receipt["id"]],
        )
        case.run.ledger.append({
            "schema_version": 0,
            "id": "run-result-accepted-" + uuid7_hex(),
            "tenant_id": case.root.tenant_id,
            "timestamp": "2026-08-09T14:00:22.000Z",
            "kind": "result_accepted", "run_id": case.run.run_id,
            "item_id": item_id, "attempt_id": opened["attempt_id"],
            "predecessor_result_id": produced["id"],
            "acceptance_mode": "accepted_unverified",
            "acceptance_receipt_id": None,
            "worker_receipt_ids": [receipt["id"]],
        })
        return case.run.scheduler.terminal_attempt(
            case.run.run_id, item_id, opened["attempt_id"], "completed",
            None, "completed", "idempotent", now=case.intent_args()["now"],
        )

    @staticmethod
    def _run_terminal_candidate(case):
        return {
            "schema_version": 0,
            "id": "run-terminal-" + uuid7_hex(),
            "tenant_id": case.root.tenant_id,
            "timestamp": "2026-08-09T14:00:22.000Z",
            "kind": "run_terminal", "run_id": case.run.run_id,
            "outcome": "succeeded",
        }

    def test_v0_acceptance_remains_valid_for_attempt_with_no_effects(self) -> None:
        case = self._case()
        accepted = case.accept_result()
        self.assertEqual((0, "result_accepted"), (
            accepted["schema_version"], accepted["kind"],
        ))

    def test_v0_acceptance_refuses_when_attempt_has_effect_intent(self) -> None:
        case = self._case()
        candidate = case.result_acceptance_candidate()
        case.controller.intent(**case.intent_args())
        with self.assertRaises(ProtocolRefusal) as caught:
            case.run_ledger.append(candidate)
        self.assertEqual("effect_binding_required", caught.exception.code)

    def test_v1_acceptance_requires_exact_sorted_operation_set_high_watermark_and_digest(self) -> None:
        case = self._case()
        candidate = case.result_acceptance_candidate()
        self._confirmed_effect(case)
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        lawful = self._bound(candidate, evidence)
        accepted = case.run_ledger.append(lawful)
        self.assertEqual(list(evidence.operation_ids), accepted["effect_operation_ids"])

        for field, value in (
            ("effect_operation_ids", []),
            ("effect_ledger_high_watermark", evidence.high_watermark + 1),
            ("effect_evidence_digest", "0" * 64),
        ):
            changed = dict(lawful, id="run-result-accepted-" + uuid7_hex())
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ProtocolRefusal):
                case.run_ledger.append(changed)

    def test_unconfirmed_failed_unknown_or_incomplete_spend_blocks_acceptance(self) -> None:
        for state in ("intent", "dispatched", "failed", "unknown"):
            with self.subTest(state=state):
                case = self._case()
                candidate = case.result_acceptance_candidate()
                intent = case.controller.intent(**case.intent_args())
                if state != "intent":
                    case.controller.dispatched(
                        intent["operation_id"], dispatch_adapter="git_local",
                        dispatch_evidence_digest="d" * 64,
                        now=case.intent_args()["now"],
                    )
                if state == "failed":
                    case.controller.failed(
                        intent["operation_id"], reason_code="effect_not_applied",
                        evidence_digest="e" * 64, spend_status="partial",
                        measured_spend=[{"budget_id": "build", "amount": 0}],
                        now=case.intent_args()["now"],
                    )
                if state == "unknown":
                    case.controller.unknown(
                        intent["operation_id"], reason_code="confirmation_absent",
                        evidence_digest="e" * 64, spend_status="unknown",
                        measured_spend=None, now=case.intent_args()["now"],
                    )
                evidence = case.effect_ledger.project().acceptance_evidence(
                    case.run.run_id, case.opened["attempt_id"],
                )
                with self.assertRaises(ProtocolRefusal) as caught:
                    case.run_ledger.append(self._bound(candidate, evidence))
                self.assertEqual("effect_unknown_blocks_acceptance", caught.exception.code)

    def test_later_confirmed_prefix_allows_exact_acceptance_retry_after_crash(self) -> None:
        case = self._case()
        candidate = case.result_acceptance_candidate()
        self._confirmed_effect(case)
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        lawful = self._bound(candidate, evidence)
        accepted = case.run_ledger.append(lawful)
        self.assertEqual(accepted, case.run_ledger.append(dict(lawful)))
        retry = dict(lawful, id="run-result-accepted-" + uuid7_hex())
        self.assertEqual(accepted, case.run_ledger.append(retry))
        self.assertEqual(1, sum(
            row["kind"] == "result_accepted" for row in case.run_ledger.records()
        ))

    def test_exact_acceptance_retry_allows_lawful_later_effect_tail(self) -> None:
        """Catches retry comparing immutable accepted evidence to the whole later tail."""
        case = self._case()
        candidate = case.result_acceptance_candidate()
        intent, confirmed = self._confirmed_effect(case)
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        lawful = self._bound(candidate, evidence)
        case.run_ledger.append(lawful)

        self._advance_effect_prefix(case, intent, confirmed)

        for retry in (
            dict(lawful),
            dict(lawful, id="run-result-accepted-" + uuid7_hex()),
        ):
            with self.subTest(same_id=retry["id"] == lawful["id"]):
                self.assertEqual(lawful, case.run_ledger.append(retry))

    def test_general_projection_serializes_effect_then_run_snapshot(self) -> None:
        from floati.effects import EffectLedger

        case = self._case()
        candidate = case.result_acceptance_candidate()
        self._confirmed_effect(case)
        original_project = EffectLedger.project
        reader_snapshot = threading.Event()
        release_reader = threading.Event()
        reader_identity = []
        reader_results = []
        reader_errors = []
        writer_results = []

        def observed_project(ledger):
            snapshot = original_project(ledger)
            if reader_identity and threading.get_ident() == reader_identity[0]:
                reader_snapshot.set()
                if not release_reader.wait(5):
                    raise RuntimeError("projection race release timed out")
            return snapshot

        def read_projection():
            reader_identity.append(threading.get_ident())
            try:
                reader_results.append(case.run_ledger.project())
            except BaseException as exc:
                reader_errors.append(exc)

        def append_acceptance():
            try:
                current = case.effect_ledger.project().acceptance_evidence(
                    case.run.run_id, case.opened["attempt_id"],
                )
                writer_results.append(case.run_ledger.append(
                    self._bound(candidate, current)
                ))
            except BaseException as exc:
                writer_results.append(exc)

        with mock.patch.object(EffectLedger, "project", observed_project):
            reader = threading.Thread(target=read_projection)
            reader.start()
            self.assertTrue(reader_snapshot.wait(5))
            writer = threading.Thread(target=append_acceptance)
            writer.start()
            writer.join(0.1)
            self.assertTrue(writer.is_alive(), "writer escaped the cross-ledger guard")
            release_reader.set()
            reader.join(10); writer.join(10)

        self.assertFalse(reader.is_alive() or writer.is_alive())
        self.assertEqual([], reader_errors)
        self.assertEqual(1, len(reader_results))
        self.assertEqual({}, reader_results[0].run(case.run.run_id)["accepted"])
        self.assertFalse(
            isinstance(writer_results[0], BaseException), repr(writer_results[0]),
        )
        self.assertEqual("result_accepted", writer_results[0]["kind"])
        self.assertIn(case.item_id, case.run_ledger.project().run(case.run.run_id)["accepted"])

    def test_general_projection_does_not_exclusively_block_non_effect_append(self) -> None:
        """Catches read/general coordination serializing unrelated Run writers."""
        from floati.effects import EffectLedger

        case = self._case()
        original_project = EffectLedger.project
        reader_snapshot = threading.Event()
        release_reader = threading.Event()
        reader_identity = []
        reader_errors = []
        writer_results = []
        candidate = {
            "schema_version": 0,
            "id": "run-created-" + uuid7_hex(),
            "tenant_id": case.root.tenant_id,
            "timestamp": "2026-08-09T14:00:22.000Z",
            "kind": "run_created",
            "run_id": "run-" + uuid7_hex(),
            "plan_digest": "a" * 64,
            "item_ids": ["work-" + uuid7_hex()],
            "dependency_edges": [],
        }

        def observed_project(ledger):
            snapshot = original_project(ledger)
            if reader_identity and threading.get_ident() == reader_identity[0]:
                reader_snapshot.set()
                if not release_reader.wait(5):
                    raise RuntimeError("shared projection release timed out")
            return snapshot

        def read_projection():
            reader_identity.append(threading.get_ident())
            try:
                case.run_ledger.project()
            except BaseException as exc:
                reader_errors.append(exc)

        def append_general_record():
            try:
                writer_results.append(case.run_ledger.append(candidate))
            except BaseException as exc:
                writer_results.append(exc)

        with mock.patch.object(EffectLedger, "project", observed_project):
            reader = threading.Thread(target=read_projection)
            reader.start()
            self.assertTrue(reader_snapshot.wait(5))
            writer = threading.Thread(target=append_general_record)
            writer.start()
            writer.join(0.2)
            writer_blocked = writer.is_alive()
            release_reader.set()
            reader.join(10); writer.join(10)

        self.assertFalse(reader.is_alive() or writer.is_alive())
        self.assertFalse(writer_blocked, "general append waited on a read-only guard")
        self.assertEqual([], reader_errors)
        self.assertFalse(
            isinstance(writer_results[0], BaseException), repr(writer_results[0]),
        )
        self.assertEqual(candidate, writer_results[0])

    def test_current_effect_binding_allows_successful_attempt_and_run_terminal(self) -> None:
        from tests.test_admission import ITEM_B, ITEM_C

        case = self._case()
        candidate = case.result_acceptance_candidate()
        self._confirmed_effect(case)
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        case.run_ledger.append(self._bound(candidate, evidence))
        terminal = case.run.scheduler.terminal_attempt(
            case.run.run_id, case.item_id, case.opened["attempt_id"],
            "completed", None, "completed", "idempotent",
            now=case.intent_args()["now"],
        )
        self.assertEqual("completed", terminal["terminal_state"])
        self._complete_no_effect_item(case, ITEM_B)
        self._complete_no_effect_item(case, ITEM_C)
        self.assertEqual(
            "succeeded",
            case.run_ledger.append(self._run_terminal_candidate(case))["outcome"],
        )

    def test_completed_attempt_allows_lawful_later_effect_tail(self) -> None:
        """Catches a later compensation proposal invalidating an accepted prefix."""
        case = self._case()
        candidate = case.result_acceptance_candidate()
        intent, confirmed = self._confirmed_effect(case)
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        case.run_ledger.append(self._bound(candidate, evidence))
        self._advance_effect_prefix(case, intent, confirmed)
        terminal = case.run.scheduler.terminal_attempt(
            case.run.run_id, case.item_id, case.opened["attempt_id"],
            "completed", None, "completed", "idempotent",
            now=case.intent_args()["now"],
        )
        self.assertEqual("completed", terminal["terminal_state"])

    def test_successful_run_terminal_allows_lawful_later_effect_tail(self) -> None:
        """Catches run success comparing accepted evidence to unrelated later rows."""
        from tests.test_admission import ITEM_B, ITEM_C

        case = self._case()
        candidate = case.result_acceptance_candidate()
        intent, confirmed = self._confirmed_effect(case)
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        case.run_ledger.append(self._bound(candidate, evidence))
        case.run.scheduler.terminal_attempt(
            case.run.run_id, case.item_id, case.opened["attempt_id"],
            "completed", None, "completed", "idempotent",
            now=case.intent_args()["now"],
        )
        self._complete_no_effect_item(case, ITEM_B)
        self._complete_no_effect_item(case, ITEM_C)
        self._advance_effect_prefix(case, intent, confirmed)
        terminal = case.run_ledger.append(self._run_terminal_candidate(case))
        self.assertEqual("succeeded", terminal["outcome"])

    def test_persisted_post_acceptance_same_attempt_intent_refuses(self) -> None:
        """Catches a malicious later intent expanding an already accepted attempt."""
        case = self._case()
        candidate = case.result_acceptance_candidate()
        self._confirmed_effect(case)
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        case.run_ledger.append(self._bound(candidate, evidence))
        self._append_post_acceptance_intent(case)

        with self.assertRaises(IntegrityFailure) as caught:
            case.run_ledger.project()
        self.assertEqual("effect_evidence_overtaken", caught.exception.code)

    def test_no_new_intent_can_race_after_effect_snapshot_and_acceptance_append(self) -> None:
        case = self._case()
        candidate = case.result_acceptance_candidate()
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        outcomes = context.Queue()
        acceptance = context.Process(
            target=_task6_acceptance_process,
            args=(str(case.root.tenant_home), candidate, start, outcomes),
        )
        intent = context.Process(
            target=_task6_intent_process,
            args=(str(case.root.tenant_home), str(case.run.policy_path),
                  case.intent_args(), start, outcomes),
        )
        acceptance.start(); intent.start(); start.set()
        acceptance.join(20); intent.join(20)
        self.assertEqual((0, 0), (acceptance.exitcode, intent.exitcode))
        observed = {outcomes.get(timeout=5)[0:2] for _ in range(2)}
        labels = {row[0] for row in observed}
        self.assertIn(labels, (
            {"acceptance_ok", "intent_refused"},
            {"intent_ok", "acceptance_refused"},
        ))
        refusals = {row[1] for row in observed if row[0].endswith("refused")}
        self.assertTrue(refusals <= {
            "effect_attempt_accepted", "effect_binding_required",
        })
        before_run = case.run_ledger.records()
        before_effect = case.effect_ledger.records()
        lock_path = case.root.resolve_relative("effects/acceptance.lock")
        self.assertEqual(b"", lock_path.read_bytes())
        lock_path.unlink()
        self.assertEqual(before_run, case.run_ledger.records())
        self.assertEqual(before_effect, case.effect_ledger.records())

    def test_completed_attempt_and_successful_run_terminal_require_effect_binding(self) -> None:
        case = self._case()
        candidate = case.result_acceptance_candidate()
        case.controller.intent(**case.intent_args())
        with self.assertRaises(ProtocolRefusal):
            case.run_ledger.append(candidate)
        with self.assertRaises(ProtocolRefusal):
            case.run.scheduler.terminal_attempt(
                case.run.run_id, case.item_id, case.opened["attempt_id"],
                "completed", None, "completed", "idempotent",
                now=case.intent_args()["now"],
            )
        with self.assertRaises(ProtocolRefusal):
            case.run_ledger.append({
                "schema_version": 0,
                "id": "run-terminal-" + uuid7_hex(),
                "tenant_id": case.root.tenant_id,
                "timestamp": "2026-08-09T14:00:22.000Z",
                "kind": "run_terminal", "run_id": case.run.run_id,
                "outcome": "succeeded",
            })


if __name__ == "__main__": unittest.main()

from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.contracts import contract_digest
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from floati.policy import RepositoryPolicy
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.scheduler import RetryPolicy, RunScheduler
from floati.sequencer import SequencerClient, SequencerConfig, SequencerService
from tests.test_admission import ITEM_A, ITEM_B, VALID_POLICY

try:
    from floati.admission import AdmissionBinder, AdmissionPlan
    from floati.records import run_admission_digest
except (ImportError, ModuleNotFoundError):
    AdmissionBinder = AdmissionPlan = run_admission_digest = None


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _contract(dependencies=()):
    return {
        "objective": "bind durable admission semantics",
        "non_goals": ["no sequencer authority"],
        "areas_to_avoid": [{"path": "bundle/c7.1", "region": "all"}],
        "input_hashes": {"brief": "a" * 64},
        "acceptance_checks": {"tests.unit": "python3 -m unittest"},
        "constraints": {"network": "dark"},
        "risk_class": "low",
        "retry_policy": {
            "max_attempts": 1,
            "backoff": {
                "base_delay_ms": 0,
                "cap_delay_ms": 0,
                "strategy": "fixed",
            },
        },
        "dependencies": list(dependencies),
    }


def _item(item_id, suffix):
    return {
        "item_id": item_id,
        "contract": _contract(),
        "capability_selector": "review_write",
        "requires_cancellation": True,
        "requires_callback": True,
        "workspace_key": "workspace-" + suffix,
        "concurrency_key": "concurrency-" + suffix,
        "retry_class": "transient",
        "effect_safety": "idempotent",
        "merge_gate": None,
    }


class RunAdmissionBoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(AdmissionBinder, "floati.admission must provide AdmissionBinder")
        self.assertIsNotNone(run_admission_digest, "floati.records must provide run_admission_digest")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.policy_path = base / "FLOATI.toml"
        self.policy_path.write_text(VALID_POLICY, encoding="utf-8")
        self.policy = RepositoryPolicy.load(self.policy_path)
        self.plan_path = base / "admission-plan.json"
        self.plan_value = {
            "schema_version": 0,
            "workers": [
                {"node_id": "node-a", "worker_profile": "good"},
                {"node_id": "node-b", "worker_profile": "good"},
            ],
            "max_active_attempts": 2,
            "budget_reservations": [{"budget_id": "build", "amount": 1}],
            "items": [_item(ITEM_A, "a"), _item(ITEM_B, "b")],
            "dependency_edges": [],
        }
        self.plan = self._load_plan(self.plan_value)
        self.root = FloatiRoot.open_direct_home(base / "alpha", create=True)
        self.ledger = RunLedger(self.root)
        self.run_id = "run-" + uuid7_hex()
        self._append(
            "run_created",
            "run-created-",
            plan_digest=self.plan.digest,
            policy_digest=self.policy.digest,
            item_ids=[ITEM_A, ITEM_B],
            dependency_edges=[],
        )
        for item in self.plan.items:
            contract = item.contract
            self._append(
                "task_contract",
                "task-contract-",
                item_id=item.item_id,
                **contract.canonical(),
                contract_digest=contract_digest(contract),
            )
        self._append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
        self._append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=["node-a", "node-b"])

    def _load_plan(self, value):
        self.plan_path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        return AdmissionPlan.load(self.plan_path)

    def _append(self, kind, prefix, **fields):
        return self.ledger.append(
            {
                "schema_version": 0,
                "id": prefix + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-09T12:00:00.000Z",
                "kind": kind,
                "run_id": self.run_id,
                **fields,
            }
        )

    def test_binding_persists_exact_lexical_tables_and_sensitive_digest(self) -> None:
        """Catches omitted fields, caller ordering, or a digest that ignores governed semantics."""
        record = AdmissionBinder.bind(
            self.ledger, self.run_id, self.plan, self.policy, now=NOW
        )
        semantic_fields = {
            "schema_version",
            "kind",
            "run_id",
            "plan_digest",
            "policy_digest",
            "max_active_attempts",
            "workers",
            "budget_reservations",
            "items",
            "admission_digest",
        }
        self.assertEqual(
            semantic_fields | {"id", "tenant_id", "timestamp"}, set(record)
        )
        expected_workers = [
            {"node_id": "node-a", "worker_profile": "good"},
            {"node_id": "node-b", "worker_profile": "good"},
        ]
        expected_reservations = [{"budget_id": "build", "amount": 1}]
        expected_items = [
            {
                "item_id": ITEM_A,
                "workspace_key": "workspace-a",
                "concurrency_key": "concurrency-a",
                "capability_selector": "review_write",
            },
            {
                "item_id": ITEM_B,
                "workspace_key": "workspace-b",
                "concurrency_key": "concurrency-b",
                "capability_selector": "review_write",
            },
        ]
        self.assertEqual(expected_workers, record["workers"])
        self.assertEqual(expected_reservations, record["budget_reservations"])
        self.assertEqual(expected_items, record["items"])
        baseline = run_admission_digest(
            expected_workers, 2, expected_reservations, expected_items
        )
        self.assertEqual(baseline, record["admission_digest"])
        mutations = (
            ([dict(expected_workers[0], worker_profile="unrouted"), expected_workers[1]], 2, expected_reservations, expected_items),
            (expected_workers, 1, expected_reservations, expected_items),
            (expected_workers, 2, [{"budget_id": "build", "amount": 2}], expected_items),
            (expected_workers, 2, expected_reservations, [dict(expected_items[0], capability_selector="other"), expected_items[1]]),
        )
        for changed in mutations:
            with self.subTest(changed=changed):
                self.assertNotEqual(baseline, run_admission_digest(*changed))

    def test_managed_admission_binding_is_evaluated_and_appended_by_same_service_ledger(self) -> None:
        """Catches managed admission evaluation authorizing a different append ledger."""
        service = SequencerService(
            self.root,
            "sequencer-a",
            config=SequencerConfig(select_timeout=0.01),
        )
        stop = threading.Event()
        worker = threading.Thread(target=service.serve_forever, args=(stop,), daemon=True)
        worker.start()

        def cleanup() -> None:
            stop.set()
            worker.join(3)
            service.close()

        self.addCleanup(cleanup)
        managed = RunLedger(
            self.root,
            sequencer_client=SequencerClient(
                service.socket_path, service.epoch, "managed-admission-binder"
            ),
        )
        bound = AdmissionBinder.bind(managed, self.run_id, self.plan, self.policy, now=NOW)

        self.assertEqual("run_admission_bound", bound["kind"])
        self.assertEqual(bound, RunLedger(self.root).records()[-1])

    def test_binding_rejects_wrong_evidence_duplicate_and_late_lifecycle(self) -> None:
        """Catches a binding escaping its exact run/plan/policy and pre-attempt window."""
        before = self.ledger.records()
        with self.assertRaises(ProtocolRefusal) as missing:
            AdmissionBinder.bind(
                self.ledger, "run-018f7e9b3c167abc8def0123456789ab",
                self.plan, self.policy, now=NOW,
            )
        self.assertEqual("run_missing", missing.exception.code)
        changed_plan = dict(self.plan_value, max_active_attempts=1)
        with self.assertRaises(ProtocolRefusal) as wrong_plan:
            AdmissionBinder.bind(
                self.ledger, self.run_id, self._load_plan(changed_plan), self.policy, now=NOW
            )
        self.assertEqual("run_admission_plan_mismatch", wrong_plan.exception.code)
        changed_policy_path = self.plan_path.parent / "changed-policy" / "FLOATI.toml"
        changed_policy_path.parent.mkdir()
        changed_policy_path.write_text(
            VALID_POLICY.replace("max_active_attempts = 2", "max_active_attempts = 1"),
            encoding="utf-8",
        )
        changed_policy = RepositoryPolicy.load(changed_policy_path)
        with self.assertRaises(ProtocolRefusal) as wrong_policy:
            AdmissionBinder.bind(
                self.ledger, self.run_id, self.plan, changed_policy, now=NOW
            )
        self.assertEqual("run_admission_policy_mismatch", wrong_policy.exception.code)
        self.assertEqual(before, self.ledger.records())

        AdmissionBinder.bind(self.ledger, self.run_id, self.plan, self.policy, now=NOW)
        with self.assertRaises(ProtocolRefusal) as duplicate:
            AdmissionBinder.bind(self.ledger, self.run_id, self.plan, self.policy, now=NOW)
        self.assertEqual("run_admission_duplicate", duplicate.exception.code)

    def test_binding_required_run_refuses_attempt_until_binding_is_physical(self) -> None:
        """Catches an admitted-pair run opening work before its durable binding."""
        scheduler = RunScheduler(self.ledger)
        projected = self.ledger.project().run(self.run_id)
        self.assertEqual("pending", projected["admission_binding"]["status"])
        before = self.ledger.records()
        with self.assertRaises(ProtocolRefusal) as missing:
            scheduler.open_attempt(
                self.run_id, ITEM_A, RetryPolicy(1, 0, 0, strategy="fixed"),
                1, now=NOW,
            )
        self.assertEqual("run_admission_missing", missing.exception.code)
        self.assertEqual(before, self.ledger.records())
        AdmissionBinder.bind(self.ledger, self.run_id, self.plan, self.policy, now=NOW)
        opened = scheduler.open_attempt(
            self.run_id, ITEM_A, RetryPolicy(1, 0, 0, strategy="fixed"),
            1, now=NOW,
        )
        self.assertEqual(ITEM_A, opened["item_id"])

    def test_true_legacy_attempt_remains_available_and_binding_is_late(self) -> None:
        """Catches the new requirement being backfilled onto a genuine legacy run."""
        root = FloatiRoot.open_direct_home(
            self.plan_path.parent / "legacy-attempt" / "alpha", create=True
        )
        ledger = RunLedger(root)
        run_id = "run-" + uuid7_hex()

        def append(kind, prefix, **fields):
            return ledger.append({
                "schema_version": 0,
                "id": prefix + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-09T12:00:00.000Z",
                "kind": kind,
                "run_id": run_id,
                **fields,
            })

        append(
            "run_created", "run-created-", plan_digest=self.plan.digest,
            item_ids=[ITEM_A, ITEM_B], dependency_edges=[],
        )
        for item in self.plan.items:
            append(
                "task_contract", "task-contract-", item_id=item.item_id,
                **item.contract.canonical(),
                contract_digest=contract_digest(item.contract),
            )
        append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
        append(
            "worker_pool_bound", "run-worker-pool-bound-",
            worker_ids=["node-a", "node-b"],
        )
        scheduler = RunScheduler(ledger)
        opened = scheduler.open_attempt(
            run_id, ITEM_A, RetryPolicy(1, 0, 0, strategy="fixed"), 1, now=NOW
        )
        self.assertEqual(ITEM_A, opened["item_id"])
        with self.assertRaises(ProtocolRefusal) as late:
            AdmissionBinder.bind(ledger, run_id, self.plan, self.policy, now=NOW)
        self.assertEqual("run_admission_late", late.exception.code)

    def test_public_raw_admission_append_is_refused(self) -> None:
        """Catches caller-authored durable admission authority."""
        candidate = {
            "schema_version": 1,
            "id": "run-admission-bound-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "run_admission_bound",
            "run_id": self.run_id,
            "plan_digest": self.plan.digest,
            "policy_digest": self.policy.digest,
            "max_active_attempts": 2,
            "workers": [{"node_id": "node-a", "worker_profile": "good"}],
            "budget_reservations": [],
            "items": [{
                "item_id": ITEM_A,
                "workspace_key": "workspace-a",
                "concurrency_key": "concurrency-a",
                "capability_selector": "review_write",
            }],
            "admission_digest": "b" * 64,
        }
        with self.assertRaises(ProtocolRefusal) as public:
            self.ledger.append(candidate)
        self.assertEqual("admission_binder_only", public.exception.code)

    def test_forged_semantic_caches_cannot_author_binding(self) -> None:
        """Catches live plan or policy drift hidden behind stale canonical bytes and digests."""
        forged_plan = self.plan
        object.__setattr__(forged_plan, "max_active_attempts", 1)
        with self.assertRaises(ProtocolRefusal) as bad_plan:
            AdmissionBinder.bind(
                self.ledger, self.run_id, forged_plan, self.policy, now=NOW
            )
        self.assertEqual("admission_plan_integrity_invalid", bad_plan.exception.code)

        self.plan = self._load_plan(self.plan_value)
        forged_profile = dataclasses.replace(
            self.policy.worker_profiles["good"], max_concurrency=1
        )
        forged_policy = dataclasses.replace(
            self.policy,
            worker_profiles={**self.policy.worker_profiles, "good": forged_profile},
        )
        with self.assertRaises(ProtocolRefusal) as bad_policy:
            AdmissionBinder.bind(
                self.ledger, self.run_id, self.plan, forged_policy, now=NOW
            )
        self.assertEqual("policy_integrity_invalid", bad_policy.exception.code)

    def test_replay_rejects_binding_digest_or_table_tampering(self) -> None:
        """Catches hostile persisted admission tables or digests being trusted on replay."""
        bound = AdmissionBinder.bind(
            self.ledger, self.run_id, self.plan, self.policy, now=NOW
        )
        records = self.ledger.records()
        index = next(i for i, row in enumerate(records) if row["id"] == bound["id"])
        path = self.root.resolve_relative(self.ledger.relative_path)

        changed_digest = list(records)
        changed_digest[index] = dict(bound, admission_digest="e" * 64)
        path.write_bytes(b"".join(encode_frame(row) for row in changed_digest))
        with self.assertRaises(IntegrityFailure) as digest_failure:
            self.ledger.project()
        self.assertEqual("run_admission_digest_invalid", digest_failure.exception.code)

        changed_table = list(records)
        changed_items = [dict(row) for row in bound["items"]]
        changed_items[0]["workspace_key"] = "workspace-forged"
        changed_table[index] = dict(bound, items=changed_items)
        path.write_bytes(b"".join(encode_frame(row) for row in changed_table))
        with self.assertRaises(IntegrityFailure) as table_failure:
            self.ledger.project()
        self.assertEqual("run_admission_digest_invalid", table_failure.exception.code)

    def test_legacy_run_projects_explicit_unavailable_binding(self) -> None:
        """Catches legacy runs being upgraded to admission proof they never persisted."""
        root = FloatiRoot.open_direct_home(
            self.plan_path.parent / "legacy-projection" / "alpha", create=True
        )
        ledger = RunLedger(root)
        run_id = "run-" + uuid7_hex()
        ledger.append({
            "schema_version": 0,
            "id": "run-created-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-09T12:00:00.000Z",
            "kind": "run_created",
            "run_id": run_id,
            "plan_digest": "a" * 64,
            "item_ids": [ITEM_A],
            "dependency_edges": [],
        })
        projected = ledger.project().run(run_id)
        self.assertEqual(
            {
                "status": "unavailable",
                "reason_code": "run_admission_binding_unavailable",
            },
            projected["admission_binding"],
        )

    def test_later_policy_and_pool_bindings_must_equal_prior_admission(self) -> None:
        """Catches a valid early admission binding drifting when later run tables arrive."""
        root = FloatiRoot.open_direct_home(
            self.plan_path.parent / "early-binding" / "alpha", create=True
        )
        ledger = RunLedger(root)
        run_id = "run-" + uuid7_hex()

        def append(kind, prefix, **fields):
            return ledger.append({
                "schema_version": 0,
                "id": prefix + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-09T12:00:00.000Z",
                "kind": kind,
                "run_id": run_id,
                **fields,
            })

        append(
            "run_created", "run-created-", plan_digest=self.plan.digest,
            item_ids=[ITEM_A, ITEM_B], dependency_edges=[],
        )
        AdmissionBinder.bind(ledger, run_id, self.plan, self.policy, now=NOW)
        with self.assertRaises(ProtocolRefusal) as wrong_policy:
            append("run_policy_bound", "run-policy-bound-", policy_digest="e" * 64)
        self.assertEqual("run_admission_policy_mismatch", wrong_policy.exception.code)
        append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
        with self.assertRaises(ProtocolRefusal) as wrong_pool:
            append("worker_pool_bound", "run-worker-pool-bound-", worker_ids=["node-a"])
        self.assertEqual("run_admission_workers_mismatch", wrong_pool.exception.code)

    def test_plan_amendment_cannot_drift_after_admission_binding(self) -> None:
        """Catches a post-admission contract amendment invalidating the bound plan digest."""
        AdmissionBinder.bind(self.ledger, self.run_id, self.plan, self.policy, now=NOW)
        current = self.ledger.project().run(self.run_id)["contracts"][ITEM_A]
        replacement = current["contract"].replaced(objective="drift after binding")
        with self.assertRaises(ProtocolRefusal) as caught:
            self._append(
                "plan_amendment", "plan-amendment-", item_id=ITEM_A,
                task_contract_id=current["task_contract_id"],
                previous_digest=current["contract_digest"],
                replacement_fields={"objective": "drift after binding"},
                contract_digest=contract_digest(replacement),
            )
        self.assertEqual("task_contract_frozen", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

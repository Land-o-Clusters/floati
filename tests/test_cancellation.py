"""Durable cancellation and late-result fencing contracts."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.approvals import CapabilityLedger
from floati.errors import ProtocolRefusal
from floati.contracts import TaskContract, contract_digest
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.scheduler import RetryPolicy, RunScheduler

try:
    from floati.cancellation import CancelMode, CancellationCoordinator, FloatiSupervisor
except ModuleNotFoundError:
    CancelMode = CancellationCoordinator = FloatiSupervisor = None


NOW = "2026-08-08T12:00:00.000Z"
AUTH_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
DIGEST = "a" * 64


class _Adapter:
    def __init__(self, mode: object, ledger: RunLedger) -> None:
        self.cancel_mode = mode
        self.ledger = ledger
        self.actions: list[str] = []

    def cancel(self) -> None:
        self.actions.append("cancel")
        self._assert_scope_is_durable()

    def cancel_local_process(self) -> None:
        self.actions.append("cancel_local_process")
        self._assert_scope_is_durable()

    def _assert_scope_is_durable(self) -> None:
        self.assertion = [row["kind"] for row in self.ledger.records()]
        if "cancel_scope_resolved" not in self.assertion:
            raise AssertionError("adapter action ran before durable scope resolution")


class _RaisingAdapter(_Adapter):
    def cancel(self) -> None:
        super().cancel()
        raise RuntimeError("native cancellation failed")

    def cancel_local_process(self) -> None:
        super().cancel_local_process()
        raise RuntimeError("local process cancellation failed")


class CancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.ledger = RunLedger(self.root)
        self.scheduler = RunScheduler(self.ledger)
        self.run_id = "run-" + uuid7_hex()
        self.first, self.second = sorted(("work-" + uuid7_hex(), "work-" + uuid7_hex()))
        self.ledger.append(self.record("run_created", run_id=self.run_id,
            plan_digest=DIGEST, item_ids=[self.first, self.second],
            dependency_edges=[{"source": self.first, "target": self.second}]))
        for item_id in (self.first, self.second):
            self.bind_contract(item_id)
        self.ledger.append(self.record("run_policy_bound", run_id=self.run_id, policy_digest=DIGEST))
        self.ledger.append(self.record("worker_pool_bound", run_id=self.run_id, worker_ids=["worker-a"]))

    def record(self, kind: str, **fields: object) -> dict:
        prefixes = {
            "run_created": "run-created-", "run_policy_bound": "run-policy-bound-",
            "worker_pool_bound": "run-worker-pool-bound-", "dispatch_decision": "run-dispatch-decision-",
            "result_produced": "run-result-produced-",
        }
        return {"schema_version": 0, "id": prefixes[kind] + uuid7_hex(),
                "tenant_id": "alpha", "timestamp": NOW, "kind": kind, **fields}

    def bind_contract(self, item_id: str) -> dict:
        contract = TaskContract.create(
            objective="govern cancellation attempt", non_goals=["no post-attempt amendment"],
            areas_to_avoid=[{"path": "slip/graph.py", "region": "all"}],
            input_hashes={"brief": DIGEST}, acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"}, risk_class="high",
            retry_policy={"max_attempts": 2, "backoff": {"base_delay_ms": 10, "cap_delay_ms": 10, "strategy": "exponential"}}, dependencies=[],
        )
        return self.ledger.append({"schema_version": 0, "id": "task-contract-" + uuid7_hex(), "tenant_id": "alpha", "timestamp": NOW,
            "kind": "task_contract", "run_id": self.run_id, "item_id": item_id,
            **contract.canonical(), "contract_digest": contract_digest(contract)})

    def started_attempt(self, item_id: str) -> dict:
        opened = self.scheduler.open_attempt(
            self.run_id, item_id, RetryPolicy(2, 10, 10), 1, now=NOW,
        )
        dispatch = self.ledger.append(self.record("dispatch_decision", run_id=self.run_id,
            item_id=item_id, attempt_id=opened["attempt_id"], eligible_workers=["worker-a"],
            chosen_worker="worker-a", capability_digest=DIGEST, reason_code="policy.route",
            policy_digest=DIGEST, routing_rank=0, scheduler_epoch=1))
        self.scheduler.start_attempt(self.run_id, item_id, opened["attempt_id"], dispatch["id"], now=NOW)
        return opened

    def coordinator(self):
        self.assertIsNotNone(CancellationCoordinator, "floati.cancellation must provide CancellationCoordinator")
        return CancellationCoordinator(self.ledger)

    def authorize_operator(self) -> dict:
        Registry(self.root).register("operator-a", "Operator")
        grant = AuthorityGrantStore(self.root).claim("stale-adoption", "operator-a", 120, 120, AUTH_NOW)
        capability = CapabilityLedger(self.root).declare(
            "operator-a", "stale_evidence.adopt", "read_write", "run", 60, now=AUTH_NOW,
        )
        return {"authority_subject": "stale-adoption", "authority_epoch": grant["epoch"],
                "capability_record_id": capability["id"]}

    def authorize_non_operator(self) -> dict:
        Registry(self.root).register("worker-b", "Worker")
        grant = AuthorityGrantStore(self.root).claim("worker-adoption", "worker-b", 120, 120, AUTH_NOW)
        capability = CapabilityLedger(self.root).declare(
            "worker-b", "stale_evidence.adopt", "read_write", "run", 60, now=AUTH_NOW,
        )
        return {"authority_subject": "worker-adoption", "authority_epoch": grant["epoch"],
                "capability_record_id": capability["id"]}

    def authorize_floati(self) -> dict:
        Registry(self.root).register("floati-supervisor", "FloatiSupervisor")
        grant = AuthorityGrantStore(self.root).claim("orphaning", "floati-supervisor", 120, 120, AUTH_NOW)
        capability = CapabilityLedger(self.root).declare(
            "floati-supervisor", "orphan.emit", "read_write", "run", 60, now=AUTH_NOW,
        )
        return {"authority_subject": "orphaning", "authority_epoch": grant["epoch"],
                "capability_record_id": capability["id"]}

    def test_scope_closure_is_durable_before_each_cancel_action_for_every_adapter_mode(self) -> None:
        """Catches an adapter or local-process signal that precedes durable graph closure."""
        self.assertIsNotNone(CancelMode, "floati.cancellation must provide CancelMode")
        for mode, expected_kinds, expected_actions in (
            (CancelMode.native, ["cancel_observed", "cancel_signal_sent", "cancel_terminal"], 1),
            (CancelMode.local_process_only, ["cancel_observed", "cancel_signal_sent", "cancel_terminal"], 1),
            (CancelMode.unavailable, ["cancel_observed", "cancel_unconfirmed"], 0),
        ):
            with self.subTest(mode=mode):
                self.setUp()
                self.addCleanup(self.temp.cleanup)
                active = self.started_attempt(self.first)
                adapter = _Adapter(mode, self.ledger)
                resolved = self.coordinator().request(
                    self.run_id, {"worker-a": adapter}, item_id=self.first, now=NOW,
                )

                records = self.ledger.records()
                kinds = [row["kind"] for row in records]
                scope_index = kinds.index("cancel_scope_resolved")
                first_action_index = min(kinds.index(kind) for kind in expected_kinds)
                self.assertLess(scope_index, first_action_index)
                self.assertEqual([self.first, self.second], resolved["item_ids"])
                self.assertEqual([active["attempt_id"]], resolved["attempt_ids"])
                self.assertEqual(expected_actions, len(adapter.actions))
                self.assertTrue(all(kind in kinds for kind in expected_kinds))

    def test_raising_available_cancel_is_durably_unconfirmed_and_propagates_the_error(self) -> None:
        """Catches a native/local cancellation exception that leaves only an observed record."""
        for mode in (CancelMode.native, CancelMode.local_process_only):
            with self.subTest(mode=mode):
                self.setUp()
                self.addCleanup(self.temp.cleanup)
                self.started_attempt(self.first)
                with self.assertRaisesRegex(RuntimeError, "cancellation failed"):
                    self.coordinator().request(self.run_id, {"worker-a": _RaisingAdapter(mode, self.ledger)},
                        item_id=self.first, now=NOW)
                kinds = [row["kind"] for row in self.ledger.records()]
                self.assertIn("cancel_observed", kinds)
                self.assertIn("cancel_unconfirmed", kinds)
                self.assertNotIn("cancel_signal_sent", kinds)
                self.assertNotIn("cancel_terminal", kinds)

    def test_superseded_receipt_is_retained_as_stale_and_requires_explicit_operator_adoption(self) -> None:
        """Catches a late result that advances canonical state or an unapproved stale-evidence admission."""
        first = self.started_attempt(self.first)
        first_dispatch = self.ledger.project().run(self.run_id)["dispatches"][first["attempt_id"]]
        self.scheduler.terminal_attempt(self.run_id, self.first, first["attempt_id"], "failed",
            "transient", "transient_failure", "idempotent", now=NOW)
        second = self.scheduler.open_attempt(self.run_id, self.first, RetryPolicy(2, 10, 10), 1, now=NOW)
        receipt = self.raw_receipt()

        with self.assertRaises(ProtocolRefusal):
            self.ledger.append(self.record("result_produced", run_id=self.run_id, item_id=self.first,
                attempt_id=first["attempt_id"], dispatch_decision_id=first_dispatch["id"],
                worker_receipt_ids=[receipt]))

        evidence = self.coordinator().retain_late_receipt(
            self.run_id, self.first, first["attempt_id"], [receipt], first["fence_token"], now=NOW,
        )
        projection = self.ledger.project().run(self.run_id)
        self.assertEqual({}, projection["accepted"])
        self.assertEqual(second["attempt_id"], evidence["current_attempt_id"])
        self.assertEqual(second["fence_token"], evidence["current_fence_token"])
        self.assertEqual([receipt], evidence["worker_receipt_ids"])

        with self.assertRaises(ProtocolRefusal) as direct:
            self.ledger.append({**evidence, "id": "stale-evidence-adopted-" + uuid7_hex(),
                "kind": "stale_evidence_adopted"})
        self.assertEqual("cancellation_only", direct.exception.code)

        with self.assertRaises(ProtocolRefusal) as untrusted:
            self.coordinator().adopt_stale_evidence(self.run_id, evidence["id"], operator_id="operator-a", now=NOW)
        self.assertIn(untrusted.exception.code, {"operator_authority_required", "unknown_node", "operator_role_invalid"})

        with self.assertRaises(ProtocolRefusal) as non_operator:
            self.coordinator().adopt_stale_evidence(
                self.run_id, evidence["id"], operator_id="worker-b", now=AUTH_NOW,
                **self.authorize_non_operator(),
            )
        self.assertEqual("operator_role_invalid", non_operator.exception.code)

        adopted = self.coordinator().adopt_stale_evidence(
            self.run_id, evidence["id"], operator_id="operator-a", now=AUTH_NOW,
            **self.authorize_operator(),
        )
        self.assertEqual(evidence["id"], adopted["stale_evidence_id"])
        self.assertEqual(second["fence_token"], adopted["current_fence_token"])
        self.assertEqual("operator-a", adopted["operator_id"])
        self.assertEqual("stale-adoption", adopted["authority_subject"])

    def test_attempt_harness_binding_keeps_stable_joins_and_floati_orphaning_typed(self) -> None:
        """Catches inferred multi-session joins or a receipt not emitted by the Floati lifecycle."""
        attempt = self.started_attempt(self.first)
        claim_id = "claim-" + uuid7_hex()
        lease_id = "lease-" + uuid7_hex()
        worker_session_id = "worker-" + uuid7_hex()
        coordinator = self.coordinator()
        bound = coordinator.bind_harness_session(self.run_id, self.first, attempt["attempt_id"],
            claim_id=claim_id, lease_id=lease_id, worker_session_id=worker_session_id,
            harness_segments=[{"ordinal": 1, "harness_session_id": worker_session_id}], now=NOW)
        self.assertIsNotNone(FloatiSupervisor, "floati.cancellation must provide FloatiSupervisor")
        supervisor = FloatiSupervisor(self.ledger)
        authorization = self.authorize_floati()
        orphaned = []
        for orphan_class, emitter in (
            ("owner_loss", supervisor.owner_loss),
            ("unregister", supervisor.unregister),
            ("lease_abandonment", supervisor.lease_abandonment),
        ):
            orphaned.append(emitter(self.run_id, self.first, attempt["attempt_id"],
                claim_id=claim_id, lease_id=lease_id, worker_session_id=worker_session_id,
                now=AUTH_NOW, **authorization))

        projection = self.ledger.project().run(self.run_id)
        self.assertEqual([bound], projection["harness_sessions"][attempt["attempt_id"]])
        self.assertEqual({"owner_loss", "unregister", "lease_abandonment"},
            {row["orphan_class"] for row in orphaned})
        with self.assertRaises(ProtocolRefusal) as duplicate:
            supervisor.owner_loss(self.run_id, self.first, attempt["attempt_id"],
                claim_id=claim_id, lease_id=lease_id, worker_session_id=worker_session_id,
                now=AUTH_NOW, **authorization)
        self.assertEqual("orphaning_duplicate", duplicate.exception.code)
        with self.assertRaises(ProtocolRefusal) as ordinary:
            self.ledger.append(orphaned[0])
        self.assertEqual("supervisor_only", ordinary.exception.code)

    def raw_receipt(self) -> str:
        from floati.jsonl import append_record

        row = {"schema_version": 0, "id": "worker-receipt-" + uuid7_hex(), "tenant_id": "alpha",
            "timestamp": NOW, "kind": "worker_receipt", "session_id": "worker-" + uuid7_hex(),
            "work_item_id": self.first, "node_id": "worker-a", "adapter": "fixture",
            "transition": "claim", "outcome_code": None, "authority_subject": "authority",
            "authority_epoch": 1, "artifact_bindings": []}
        append_record(self.root, "receipts/workers.jsonl", row, allowed_kinds={"worker_receipt"})
        return row["id"]


if __name__ == "__main__":
    unittest.main()

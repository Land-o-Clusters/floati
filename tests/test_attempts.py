"""Behavior tests for scheduler-owned, durable attempt transitions."""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.contracts import TaskContract, contract_digest
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from floati.runtruth import RunLedger


NOW = "2026-08-02T12:00:00.000Z"
DIGEST = "a" * 64
RUN_ID = "run-018f7e9b3c117abc8def0123456789ab"
ITEM_ID = "work-018f7e9b3c117abc8def0123456789ab"


def append_task_contract(ledger: RunLedger, run_id: str, item_id: str, policy: object) -> dict:
    contract = TaskContract.create(
        objective="govern scheduler retries", non_goals=["no post-attempt amendment"],
        areas_to_avoid=[{"path": "floati/graph.py", "region": "all"}],
        input_hashes={"brief": DIGEST}, acceptance_checks={"tests.unit": "python3 -m unittest"},
        constraints={"network": "dark"}, risk_class="high",
        retry_policy={"max_attempts": policy.max_attempts, "backoff": {
            "base_delay_ms": policy.base_delay_ms, "cap_delay_ms": policy.cap_delay_ms,
            "strategy": policy.strategy,
        }}, dependencies=[],
    )
    return ledger.append({"schema_version": 0, "id": "task-contract-" + uuid7_hex(),
        "tenant_id": "alpha", "timestamp": NOW, "kind": "task_contract", "run_id": run_id,
        "item_id": item_id, **contract.canonical(), "contract_digest": contract_digest(contract)})


class AttemptLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.ledger = RunLedger(self.root)
        self.run_id = RUN_ID
        self.item_ids = sorted([ITEM_ID] + ["work-" + uuid7_hex() for _unused in range(6)])
        self.ledger.append(self.record("run_created", run_id=self.run_id,
            plan_digest=DIGEST, item_ids=self.item_ids, dependency_edges=[]))
        for item_id in self.item_ids:
            append_task_contract(self.ledger, self.run_id, item_id, self.policy())
        self.ledger.append(self.record("run_policy_bound", run_id=self.run_id, policy_digest=DIGEST))
        self.ledger.append(self.record("worker_pool_bound", run_id=self.run_id, worker_ids=["worker-a"]))

    def record(self, kind: str, **fields: object) -> dict:
        prefixes = {
            "run_created": "run-created-", "run_policy_bound": "run-policy-bound-",
            "worker_pool_bound": "run-worker-pool-bound-", "dispatch_decision": "run-dispatch-decision-",
            "attempt_opened": "attempt-opened-",
        }
        return {"schema_version": 0, "id": prefixes[kind] + uuid7_hex(),
                "tenant_id": "alpha", "timestamp": NOW, "kind": kind, **fields}

    def scheduler(self):
        from floati.scheduler import RunScheduler
        return RunScheduler(self.ledger)

    def policy(self):
        from floati.scheduler import RetryPolicy
        return RetryPolicy(max_attempts=3, base_delay_ms=100, cap_delay_ms=1000)

    def dispatch(self, item_id: str, attempt: dict) -> dict:
        return self.ledger.append(self.record("dispatch_decision", run_id=self.run_id,
            item_id=item_id, attempt_id=attempt["attempt_id"], eligible_workers=["worker-a"],
            chosen_worker="worker-a", capability_digest=DIGEST, reason_code="policy.route",
            policy_digest=DIGEST, routing_rank=0, scheduler_epoch=attempt["scheduler_epoch"]))

    def test_scheduler_persists_open_start_terminal_and_reserved_retry_in_order(self) -> None:
        """Catches a scheduler that omits an attempt frame or appends retry before terminal."""
        scheduler = self.scheduler()
        opened = scheduler.open_attempt(self.run_id, ITEM_ID, self.policy(), 7, now=NOW)
        dispatch = self.dispatch(ITEM_ID, opened)
        started = scheduler.start_attempt(self.run_id, ITEM_ID, opened["attempt_id"], dispatch["id"], now=NOW)
        terminal = scheduler.terminal_attempt(self.run_id, ITEM_ID, opened["attempt_id"], "failed",
            "transient", "transient_failure", "idempotent", now=NOW)

        records = self.ledger.records()
        self.assertEqual(
            ["run_created"] + ["task_contract"] * len(self.item_ids) + ["run_policy_bound", "worker_pool_bound", "attempt_opened",
             "dispatch_decision", "attempt_started", "attempt_terminal", "retry_scheduled"],
            [record["kind"] for record in records],
        )
        self.assertEqual(1, opened["ordinal"])
        self.assertEqual(3, opened["max_attempts"])
        self.assertEqual({"strategy": "exponential", "base_delay_ms": 100,
                          "cap_delay_ms": 1000, "jitter": "sha256_25pct"}, opened["backoff"])
        # See tests/test_retired_name_pins.py: this domain is a salt whose bytes
        # are a ledger contract, so it is built from the governed token here.
        expected_fence = hashlib.sha256(
            (RETIRED_PRODUCT_NAME + "-attempt-fence-v0").encode("ascii")
            + b"\0run-018f7e9b3c117abc8def0123456789ab\0"
            b"work-018f7e9b3c117abc8def0123456789ab\0" b"1\0" b"7"
        ).hexdigest()
        self.assertEqual(expected_fence, opened["fence_token"])
        self.assertEqual(opened["id"], started["attempt_opened_id"])
        self.assertEqual("scheduled", terminal["retry_disposition"])
        self.assertEqual(2, terminal["next_ordinal"])
        self.assertEqual(118, terminal["retry_delay_ms"])
        self.assertEqual(terminal["retry_record_id"], records[-1]["id"])

    def test_retry_open_consumes_reserved_schedule_and_monotonically_increases_item_ordinal(self) -> None:
        """Catches a retry that manufactures an ordinal or a new ID instead of consuming its reservation."""
        scheduler = self.scheduler()
        first = scheduler.open_attempt(self.run_id, ITEM_ID, self.policy(), 7, now=NOW)
        decision = self.dispatch(ITEM_ID, first)
        scheduler.start_attempt(self.run_id, ITEM_ID, first["attempt_id"], decision["id"], now=NOW)
        terminal = scheduler.terminal_attempt(self.run_id, ITEM_ID, first["attempt_id"], "failed",
            "transient", "transient_failure", "idempotent", now=NOW)
        second = scheduler.open_attempt(self.run_id, ITEM_ID, self.policy(), 7, now=NOW)

        self.assertEqual(2, second["ordinal"])
        self.assertEqual(terminal["next_attempt_id"], second["attempt_id"])
        self.assertEqual(terminal["next_fence_token"], second["fence_token"])
        self.assertEqual(terminal["next_scheduler_epoch"], second["scheduler_epoch"])
        self.assertEqual(second, scheduler.open_attempt(self.run_id, ITEM_ID, self.policy(), 7, now=NOW))
        with self.assertRaises(ProtocolRefusal) as caught:
            scheduler.open_attempt(self.run_id, ITEM_ID, self.policy(), 8, now=NOW)
        self.assertEqual("attempt_open_input_divergent", caught.exception.code)

    def test_retry_delay_is_restart_stable_and_has_hand_checked_governed_values(self) -> None:
        """Catches clock/randomness-dependent jitter or an off-by-one exponential ceiling."""
        from floati.scheduler import RetryPolicy, retry_delay_ms
        policy = RetryPolicy(max_attempts=5, base_delay_ms=100, cap_delay_ms=1000)
        self.assertEqual(106, retry_delay_ms(RUN_ID, ITEM_ID, 1, policy))
        self.assertEqual(118, retry_delay_ms(RUN_ID, ITEM_ID, 2, policy))
        self.assertEqual(201, retry_delay_ms(RUN_ID, ITEM_ID, 3, policy))
        self.assertEqual(450, retry_delay_ms(RUN_ID, ITEM_ID, 4, policy))
        self.assertEqual(961, retry_delay_ms(RUN_ID, ITEM_ID, 5, policy))
        self.assertEqual(0, retry_delay_ms(RUN_ID, ITEM_ID, 2,
            RetryPolicy(max_attempts=2, base_delay_ms=0, cap_delay_ms=0, strategy="fixed")))

    def test_scheduler_refuses_policy_that_differs_from_the_frozen_task_contract(self) -> None:
        """Catches a scheduler that lets caller retry policy override a frozen task contract."""
        from floati.scheduler import RetryPolicy
        with self.assertRaises(ProtocolRefusal) as caught:
            self.scheduler().open_attempt(self.run_id, ITEM_ID, RetryPolicy(2, 100, 1000), 7, now=NOW)
        self.assertEqual("task_contract_policy_mismatch", caught.exception.code)

    def test_retry_policy_unhashable_strategy_is_a_typed_refusal(self) -> None:
        """Catches an unhashable retry strategy escaping validation as a raw TypeError."""
        from floati.scheduler import RetryPolicy
        with self.assertRaises(ProtocolRefusal) as caught:
            RetryPolicy(2, 10, 10, strategy=[])
        self.assertEqual("retry_policy_invalid", caught.exception.code)

    def test_scheduler_refuses_non_retry_policy_before_any_run_append(self) -> None:
        """Catches a public scheduler boundary that dereferences arbitrary policy objects or appends first."""
        scheduler = self.scheduler()
        before = self.ledger.records()
        for candidate in ({}, object(), []):
            with self.subTest(candidate=type(candidate).__name__), self.assertRaises(ProtocolRefusal) as caught:
                scheduler.open_attempt(self.run_id, ITEM_ID, candidate, 7, now=NOW)
            self.assertEqual("retry_policy_required", caught.exception.code)
            self.assertEqual(before, self.ledger.records())

    def test_each_noncompleted_policy_class_is_durable_and_exact(self) -> None:
        """Catches acceptance of an undeclared policy class or loss of a declared class."""
        scheduler = self.scheduler()
        cases = (
            (ITEM_ID, "failed", "transient", "transient_failure", "idempotent"),
            (self.item_ids[1], "failed", "permanent", "malformed_evidence", "idempotent"),
            (self.item_ids[2], "failed", "operator_required", "approval_denial", "idempotent"),
            (self.item_ids[3], "failed", "policy_refusal", "capability_violation", "idempotent"),
            (self.item_ids[4], "cancelled", "cancelled", "operator_cancellation", "idempotent"),
            (self.item_ids[5], "uncertain", "unknown_effect", "unknown_effect", "unknown_effect"),
        )
        for item_id, state, policy_class, reason, safety in cases:
            with self.subTest(policy_class=policy_class):
                opened = scheduler.open_attempt(self.run_id, item_id, self.policy(), 7, now=NOW)
                decision = self.dispatch(item_id, opened)
                scheduler.start_attempt(self.run_id, item_id, opened["attempt_id"], decision["id"], now=NOW)
                terminal = scheduler.terminal_attempt(self.run_id, item_id, opened["attempt_id"], state,
                    policy_class, reason, safety, now=NOW)
                self.assertEqual(policy_class, terminal["policy_class"])


class RetryRefusalTests(unittest.TestCase):
    def test_forbidden_retry_table_appends_no_retry_record(self) -> None:
        """Catches a fail-open retry branch for unsafe failures or cancellations."""
        cases = (
            ("unknown_effect", "unknown_effect", "unknown_effect"),
            ("malformed_evidence", "permanent", "idempotent"),
            ("approval_denial", "policy_refusal", "idempotent"),
            ("capability_violation", "policy_refusal", "idempotent"),
            ("stale_authority", "policy_refusal", "idempotent"),
            ("non_idempotent_effect", "transient", "non_idempotent"),
            ("operator_cancellation", "cancelled", "idempotent"),
        )
        for reason, policy_class, safety in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                from floati.scheduler import RetryPolicy, RunScheduler
                root = FloatiRoot.open(Path(directory), "alpha")
                ledger = RunLedger(root)
                run_id = "run-" + uuid7_hex(); item_id = "work-" + uuid7_hex()
                def record(kind: str, prefix: str, **fields: object) -> dict:
                    return {"schema_version": 0, "id": prefix + uuid7_hex(), "tenant_id": "alpha",
                            "timestamp": NOW, "kind": kind, **fields}
                ledger.append(record("run_created", "run-created-", run_id=run_id, plan_digest=DIGEST,
                    item_ids=[item_id], dependency_edges=[]))
                ledger.append(record("run_policy_bound", "run-policy-bound-", run_id=run_id, policy_digest=DIGEST))
                ledger.append(record("worker_pool_bound", "run-worker-pool-bound-", run_id=run_id, worker_ids=["worker-a"]))
                policy = RetryPolicy(2, 10, 10)
                append_task_contract(ledger, run_id, item_id, policy)
                scheduler = RunScheduler(ledger)
                opened = scheduler.open_attempt(run_id, item_id, policy, 1, now=NOW)
                decision = ledger.append(record("dispatch_decision", "run-dispatch-decision-", run_id=run_id,
                    item_id=item_id, attempt_id=opened["attempt_id"], eligible_workers=["worker-a"], chosen_worker="worker-a",
                    capability_digest=DIGEST, reason_code="policy.route", policy_digest=DIGEST, routing_rank=0, scheduler_epoch=1))
                scheduler.start_attempt(run_id, item_id, opened["attempt_id"], decision["id"], now=NOW)
                state = "cancelled" if policy_class == "cancelled" else "failed"
                scheduler.terminal_attempt(run_id, item_id, opened["attempt_id"], state, policy_class, reason, safety, now=NOW)
                self.assertEqual([], [row for row in ledger.records() if row["kind"].startswith("retry_")])

    def test_run_ledger_refuses_direct_attempt_candidates(self) -> None:
        """Catches a public ledger append path that bypasses scheduler-owned retry authority."""
        with tempfile.TemporaryDirectory() as directory:
            root = FloatiRoot.open(Path(directory), "alpha")
            ledger = RunLedger(root)
            record = {"schema_version": 0, "id": "attempt-opened-" + uuid7_hex(), "tenant_id": "alpha", "timestamp": NOW,
                "kind": "attempt_opened", "run_id": RUN_ID, "item_id": ITEM_ID, "attempt_id": "attempt-" + uuid7_hex(),
                "ordinal": 1, "scheduler_epoch": 1, "fence_token": "a" * 64, "max_attempts": 2,
                "backoff": {"strategy": "fixed", "base_delay_ms": 1, "cap_delay_ms": 1, "jitter": "sha256_25pct"}}
            with self.assertRaises(ProtocolRefusal) as caught:
                ledger.append(record)
            self.assertEqual("scheduler_only", caught.exception.code)

    def test_internal_attempt_append_refuses_missing_and_forged_scheduler_capabilities(self) -> None:
        """Catches a callable internal append path that accepts ordinary callers as scheduler authority."""
        from floati.runtruth import attempt_fence_token

        for capability in ("missing", None, object()):
            with self.subTest(capability=type(capability).__name__), tempfile.TemporaryDirectory() as directory:
                root = FloatiRoot.open(Path(directory), "alpha")
                ledger = RunLedger(root)
                ledger.append({"schema_version": 0, "id": "run-created-" + uuid7_hex(), "tenant_id": "alpha",
                    "timestamp": NOW, "kind": "run_created", "run_id": RUN_ID, "plan_digest": DIGEST,
                    "item_ids": [ITEM_ID], "dependency_edges": []})
                candidate = {"schema_version": 0, "id": "attempt-opened-" + uuid7_hex(), "tenant_id": "alpha", "timestamp": NOW,
                    "kind": "attempt_opened", "run_id": RUN_ID, "item_id": ITEM_ID, "attempt_id": "attempt-" + uuid7_hex(),
                    "ordinal": 1, "scheduler_epoch": 1, "fence_token": attempt_fence_token(RUN_ID, ITEM_ID, 1, 1), "max_attempts": 2,
                    "backoff": {"strategy": "fixed", "base_delay_ms": 1, "cap_delay_ms": 1, "jitter": "sha256_25pct"}}
                with self.assertRaises(ProtocolRefusal) as caught:
                    if capability == "missing":
                        ledger._append_scheduler(candidate)
                    else:
                        ledger._append_scheduler(candidate, capability)
                self.assertEqual("scheduler_only", caught.exception.code)
                self.assertEqual(["run_created"], [row["kind"] for row in ledger.records()])


if __name__ == "__main__":
    unittest.main()

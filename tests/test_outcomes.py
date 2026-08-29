"""Behavior tests for logical outcomes projected from canonical run frames."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Optional

from floati.errors import ProtocolRefusal
from floati.contracts import TaskContract, contract_digest
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from floati.runtruth import DependencyEdge, RunLedger, RunProjection
from floati.scheduler import RetryPolicy, RunScheduler


NOW = "2026-08-08T12:00:00.000Z"
DIGEST = "a" * 64


class LogicalOutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.ledger = RunLedger(self.root)
        self.scheduler = RunScheduler(self.ledger)
        self.run_id = "run-" + uuid7_hex()
        self.items = {
            name: "work-" + uuid7_hex()
            for name in ("succeeded", "failed", "cancelled", "needs_operator", "uncertain", "skipped")
        }
        if self.items["failed"] > self.items["skipped"]:
            self.items["failed"], self.items["skipped"] = self.items["skipped"], self.items["failed"]
        self.ledger.append(self.record("run_created", run_id=self.run_id, plan_digest=DIGEST,
            item_ids=sorted(self.items.values()), dependency_edges=[{
                "source": self.items["failed"], "target": self.items["skipped"],
                "requires": "accepted", "failure_policy": "skip_dependent",
            }]))
        for item_id in self.items.values():
            self.bind_contract(self.run_id, item_id, RetryPolicy(1, 0, 0))
        self.ledger.append(self.record("run_policy_bound", run_id=self.run_id, policy_digest=DIGEST))
        self.ledger.append(self.record("worker_pool_bound", run_id=self.run_id, worker_ids=["worker-a"]))

    def record(self, kind: str, **fields: object) -> dict:
        prefixes = {
            "run_created": "run-created-", "run_policy_bound": "run-policy-bound-",
            "worker_pool_bound": "run-worker-pool-bound-", "dispatch_decision": "run-dispatch-decision-",
            "result_produced": "run-result-produced-", "result_accepted": "run-result-accepted-",
            "run_terminal": "run-terminal-",
        }
        return {"schema_version": 0, "id": prefixes[kind] + uuid7_hex(), "tenant_id": "alpha",
                "timestamp": NOW, "kind": kind, **fields}

    def bind_contract(self, run_id: str, item_id: str, policy: RetryPolicy) -> dict:
        contract = TaskContract.create(
            objective="govern logical outcome attempt", non_goals=["no post-attempt amendment"],
            areas_to_avoid=[{"path": "slip/graph.py", "region": "all"}],
            input_hashes={"brief": DIGEST}, acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"}, risk_class="high",
            retry_policy={"max_attempts": policy.max_attempts, "backoff": {"base_delay_ms": policy.base_delay_ms, "cap_delay_ms": policy.cap_delay_ms, "strategy": policy.strategy}}, dependencies=[],
        )
        return self.ledger.append({"schema_version": 0, "id": "task-contract-" + uuid7_hex(), "tenant_id": "alpha", "timestamp": NOW,
            "kind": "task_contract", "run_id": run_id, "item_id": item_id, **contract.canonical(), "contract_digest": contract_digest(contract)})

    def started(self, item_id: str, *, run_id: Optional[str] = None) -> dict:
        run_id = self.run_id if run_id is None else run_id
        opened = self.scheduler.open_attempt(run_id, item_id, RetryPolicy(1, 0, 0), 1, now=NOW)
        dispatch = self.ledger.append(self.record("dispatch_decision", run_id=run_id, item_id=item_id,
            attempt_id=opened["attempt_id"], eligible_workers=["worker-a"], chosen_worker="worker-a",
            capability_digest=DIGEST, reason_code="policy.route", policy_digest=DIGEST,
            routing_rank=0, scheduler_epoch=1))
        self.scheduler.start_attempt(run_id, item_id, opened["attempt_id"], dispatch["id"], now=NOW)
        return {"opened": opened, "dispatch": dispatch}

    def receipt(self, item_id: str) -> dict:
        from floati.jsonl import append_record

        row = {"schema_version": 0, "id": "worker-receipt-" + uuid7_hex(), "tenant_id": "alpha",
            "timestamp": NOW, "kind": "worker_receipt", "session_id": "worker-" + uuid7_hex(),
            "work_item_id": item_id, "node_id": "worker-a", "adapter": "fixture",
            "transition": "claim", "outcome_code": None, "authority_subject": "authority",
            "authority_epoch": 1, "artifact_bindings": []}
        append_record(self.root, "receipts/workers.jsonl", row, allowed_kinds={"worker_receipt"})
        return row

    def succeed(self, item_id: str, *, run_id: Optional[str] = None) -> None:
        run_id = self.run_id if run_id is None else run_id
        state = self.started(item_id, run_id=run_id)
        receipt = self.receipt(item_id)
        produced = self.ledger.append(self.record("result_produced", run_id=run_id, item_id=item_id,
            attempt_id=state["opened"]["attempt_id"], dispatch_decision_id=state["dispatch"]["id"],
            worker_receipt_ids=[receipt["id"]]))
        self.ledger.append(self.record("result_accepted", run_id=run_id, item_id=item_id,
            attempt_id=state["opened"]["attempt_id"], predecessor_result_id=produced["id"],
            acceptance_mode="accepted_unverified", acceptance_receipt_id=None,
            worker_receipt_ids=[receipt["id"]]))
        self.scheduler.terminal_attempt(run_id, item_id, state["opened"]["attempt_id"], "completed",
            None, "completed", "idempotent", now=NOW)

    def terminal(self, item_id: str, state: str, policy: str, reason: str, safety: str, *, run_id: Optional[str] = None) -> None:
        run_id = self.run_id if run_id is None else run_id
        started = self.started(item_id, run_id=run_id)
        self.scheduler.terminal_attempt(run_id, item_id, started["opened"]["attempt_id"], state,
            policy, reason, safety, now=NOW)

    def test_terminal_attempt_truth_table_projects_every_item_outcome(self) -> None:
        """Catches a projector that infers logical state from workers or collapses a terminal class."""
        self.succeed(self.items["succeeded"])
        self.terminal(self.items["failed"], "failed", "permanent", "malformed_evidence", "idempotent")
        self.terminal(self.items["cancelled"], "cancelled", "cancelled", "operator_cancellation", "idempotent")
        self.terminal(self.items["needs_operator"], "failed", "operator_required", "approval_denial", "idempotent")
        self.terminal(self.items["uncertain"], "uncertain", "unknown_effect", "unknown_effect", "unknown_effect")

        self.assertEqual({
            self.items["succeeded"]: "succeeded", self.items["failed"]: "failed",
            self.items["cancelled"]: "cancelled", self.items["needs_operator"]: "needs_operator",
            self.items["uncertain"]: "uncertain", self.items["skipped"]: "skipped",
        }, self.ledger.project().item_outcomes(self.run_id))
        self.assertEqual("uncertain", self.ledger.project().run_outcome(self.run_id))

    def test_unresolved_item_and_unknown_effect_never_collapse_to_failed_or_succeeded(self) -> None:
        """Catches a run reducer that treats absent terminal evidence or unknown effect as a decisive result."""
        self.succeed(self.items["succeeded"])
        self.assertEqual("uncertain", self.ledger.project().run_outcome(self.run_id))
        self.terminal(self.items["uncertain"], "uncertain", "unknown_effect", "unknown_effect", "unknown_effect")
        self.assertEqual("uncertain", self.ledger.project().run_outcome(self.run_id))

    def test_scheduled_retry_remains_uncertain_until_its_reserved_attempt_opens(self) -> None:
        """Catches a projector that treats a failed frame with a durable retry reservation as final."""
        run_id, item_id = "run-" + uuid7_hex(), "work-" + uuid7_hex()
        self.ledger.append(self.record("run_created", run_id=run_id, plan_digest=DIGEST,
            item_ids=[item_id], dependency_edges=[]))
        self.ledger.append(self.record("run_policy_bound", run_id=run_id, policy_digest=DIGEST))
        self.ledger.append(self.record("worker_pool_bound", run_id=run_id, worker_ids=["worker-a"]))
        self.bind_contract(run_id, item_id, RetryPolicy(2, 0, 0))
        opened = self.scheduler.open_attempt(run_id, item_id, RetryPolicy(2, 0, 0), 1, now=NOW)
        dispatch = self.ledger.append(self.record("dispatch_decision", run_id=run_id, item_id=item_id,
            attempt_id=opened["attempt_id"], eligible_workers=["worker-a"], chosen_worker="worker-a",
            capability_digest=DIGEST, reason_code="policy.route", policy_digest=DIGEST,
            routing_rank=0, scheduler_epoch=1))
        self.scheduler.start_attempt(run_id, item_id, opened["attempt_id"], dispatch["id"], now=NOW)
        self.scheduler.terminal_attempt(run_id, item_id, opened["attempt_id"], "failed", "transient",
            "transient_failure", "idempotent", now=NOW)

        self.assertEqual({item_id: "uncertain"}, self.ledger.project().item_outcomes(run_id))
        self.assertEqual("uncertain", self.ledger.project().run_outcome(run_id))

    def test_cancelled_source_with_skipped_dependent_remains_cancelled_not_partial(self) -> None:
        """Catches a no-success terminal set that falls through to partially_succeeded."""
        run_id = "run-" + uuid7_hex()
        source, target = sorted("work-" + uuid7_hex() for _unused in range(2))
        self.ledger.append(self.record("run_created", run_id=run_id, plan_digest=DIGEST,
            item_ids=[source, target], dependency_edges=[{
                "source": source, "target": target, "requires": "accepted",
                "failure_policy": "skip_dependent",
            }]))
        self.ledger.append(self.record("run_policy_bound", run_id=run_id, policy_digest=DIGEST))
        self.ledger.append(self.record("worker_pool_bound", run_id=run_id, worker_ids=["worker-a"]))
        self.bind_contract(run_id, source, RetryPolicy(1, 0, 0))
        self.terminal(source, "cancelled", "cancelled", "operator_cancellation", "idempotent", run_id=run_id)

        self.assertEqual({source: "cancelled", target: "skipped"}, self.ledger.project().item_outcomes(run_id))
        self.assertEqual("cancelled", self.ledger.project().run_outcome(run_id))

    def test_failure_policies_are_closed_default_fail_run_and_change_logical_run_result(self) -> None:
        """Catches permissive edge policy validation or a default that silently continues after a dependency fails."""
        first, second = self.items["failed"], self.items["skipped"]
        self.assertEqual("fail_run", DependencyEdge(first, second).failure_policy)
        for policy in ("fail_run", "skip_dependent", "continue"):
            self.assertEqual(policy, DependencyEdge(first, second, failure_policy=policy).failure_policy)
        with self.assertRaisesRegex(ProtocolRefusal, "failure_policy_invalid"):
            DependencyEdge(first, second, failure_policy="ignore")

        def run_outcome(policy: str) -> tuple[str, str]:
            run_id = "run-" + uuid7_hex()
            source, target, independent = sorted("work-" + uuid7_hex() for _unused in range(3))
            self.ledger.append(self.record("run_created", run_id=run_id, plan_digest=DIGEST,
                item_ids=[source, target, independent], dependency_edges=[{
                    "source": source, "target": target, "requires": "accepted", "failure_policy": policy,
                }]))
            self.ledger.append(self.record("run_policy_bound", run_id=run_id, policy_digest=DIGEST))
            self.ledger.append(self.record("worker_pool_bound", run_id=run_id, worker_ids=["worker-a"]))
            for item_id in (source, target, independent):
                self.bind_contract(run_id, item_id, RetryPolicy(1, 0, 0))
            self.terminal(source, "failed", "permanent", "malformed_evidence", "idempotent", run_id=run_id)
            self.succeed(independent, run_id=run_id)
            if policy == "continue":
                self.succeed(target, run_id=run_id)
            return self.ledger.project().run_outcome(run_id), run_id

        self.assertEqual("failed", run_outcome("fail_run")[0])
        partial, partial_run_id = run_outcome("skip_dependent")
        self.assertEqual("partially_succeeded", partial)
        terminal = self.ledger.append(self.record("run_terminal", run_id=partial_run_id, outcome=partial))
        self.assertEqual("partially_succeeded", terminal["outcome"])
        self.assertEqual("partially_succeeded", run_outcome("continue")[0])


class EffectOutcomeTests(unittest.TestCase):
    def test_unknown_effect_ranks_ahead_of_ordinary_failure_in_outcome_projection(self) -> None:
        """Catches unknown external state collapsing into an ordinary failed run."""
        from datetime import timedelta
        from tests.test_admission import ITEM_A, ITEM_B, ITEM_C
        from tests.test_effect_controller import _EffectCase
        from tests.test_run_limits import NOW

        case = _EffectCase(self)
        intent = case.controller.intent(**case.intent_args())
        case.controller.dispatched(
            intent["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64, now=NOW + timedelta(seconds=23),
        )
        case.controller.unknown(
            intent["operation_id"], reason_code="confirmation_absent",
            evidence_digest="e" * 64, spend_status="unknown",
            measured_spend=None, now=NOW + timedelta(seconds=24),
        )
        case.run.scheduler.terminal_attempt(
            case.run.run_id, ITEM_A, case.opened["attempt_id"],
            "failed", "permanent", "permanent_failure", "idempotent",
            now=NOW + timedelta(seconds=25),
        )
        for offset, item_id in enumerate((ITEM_B, ITEM_C), start=26):
            dispatch = case.run.dispatch(item_id, "node-b")
            opened = case.run.opened[item_id]
            case.run.scheduler.start_attempt(
                case.run.run_id, item_id, opened["attempt_id"], dispatch["id"],
                now=NOW + timedelta(seconds=offset),
            )
            case.run.scheduler.terminal_attempt(
                case.run.run_id, item_id, opened["attempt_id"],
                "failed", "permanent", "permanent_failure", "idempotent",
                now=NOW + timedelta(seconds=offset + 1),
            )
        projection = case.run_ledger.project()
        self.assertEqual("needs_operator", projection.item_outcomes(case.run.run_id)[ITEM_A])
        self.assertEqual("needs_operator", projection.run_outcome(case.run.run_id))

        reducer = RunProjection.empty()
        reducer._item_outcomes = lambda run, edges: {  # type: ignore[method-assign]
            ITEM_A: "needs_operator", ITEM_B: "failed",
        }
        self.assertEqual("needs_operator", reducer._run_outcome(
            {}, [DependencyEdge(ITEM_B, ITEM_A, failure_policy="fail_run")],
        ))

    def _failed_effect_outcome(self, spend_status):
        from datetime import timedelta
        from tests.test_admission import ITEM_A
        from tests.test_effect_controller import _EffectCase
        from tests.test_run_limits import NOW

        case = _EffectCase(self)
        intent = case.controller.intent(**case.intent_args())
        case.controller.dispatched(
            intent["operation_id"], dispatch_adapter="git_local",
            dispatch_evidence_digest="d" * 64,
            now=NOW + timedelta(seconds=23),
        )
        case.controller.failed(
            intent["operation_id"], reason_code="effect_not_applied",
            evidence_digest="e" * 64, spend_status=spend_status,
            measured_spend=[{"budget_id": "build", "amount": 0}],
            now=NOW + timedelta(seconds=24),
        )
        case.run.scheduler.terminal_attempt(
            case.run.run_id, ITEM_A, case.opened["attempt_id"],
            "failed", "permanent", "permanent_failure", "idempotent",
            now=NOW + timedelta(seconds=25),
        )
        return case, intent

    def test_failed_partial_spend_ranks_needs_operator(self) -> None:
        """Catches incomplete failed spend being collapsed into known failure."""
        from tests.test_admission import ITEM_A
        case, intent = self._failed_effect_outcome("partial")
        evidence = case.effect_ledger.project().acceptance_evidence(
            case.run.run_id, case.opened["attempt_id"],
        )
        self.assertEqual(
            (intent["operation_id"],), evidence.incomplete_spend_operation_ids,
        )
        self.assertEqual(
            "needs_operator",
            case.run_ledger.project().item_outcomes(case.run.run_id)[ITEM_A],
        )

    def test_failed_complete_spend_remains_ordinary_failure(self) -> None:
        """Lawful control: complete known failure remains an ordinary failure."""
        from tests.test_admission import ITEM_A

        case, _intent = self._failed_effect_outcome("complete")
        self.assertEqual(
            "failed",
            case.run_ledger.project().item_outcomes(case.run.run_id)[ITEM_A],
        )

    def _reconciled_failed_effect_outcome(self, spend_status):
        from floati.effects import EffectLedger
        from floati.framing import encode_frame
        from floati.records import EFFECT_BINDING_FIELDS

        case, intent = self._failed_effect_outcome("complete")
        operation = case.effect_ledger.project().operation(intent["operation_id"])
        reconciled = {
            "schema_version": 1,
            "id": "effect-reconciled-" + uuid7_hex(),
            "tenant_id": case.root.tenant_id,
            "timestamp": "2026-08-09T14:00:26.000Z",
            "kind": "effect_reconciled",
            **{field: intent[field] for field in EFFECT_BINDING_FIELDS},
            "effect_intent_id": intent["id"],
            "prior_effect_evidence_id": operation["current_evidence_id"],
            "reconciled_outcome": "failed",
            "reconciliation_evidence_digest": "f" * 64,
            "confirmation": None,
            "spend_status": spend_status,
            "measured_spend": (
                None if spend_status == "unknown"
                else [{"budget_id": "build", "amount": 0}]
            ),
            "reconciled_at_testimony": "2026-08-09T14:00:26.000Z",
        }
        path = case.root.resolve_relative(EffectLedger.relative_path)
        path.write_bytes(path.read_bytes() + encode_frame(reconciled))
        return case, intent

    def test_reconciled_failed_partial_or_unknown_spend_ranks_needs_operator(self) -> None:
        """Catches reconciled failure erasing incomplete spend uncertainty."""
        from tests.test_admission import ITEM_A

        for spend_status in ("partial", "unknown"):
            with self.subTest(spend_status=spend_status):
                case, intent = self._reconciled_failed_effect_outcome(spend_status)
                evidence = case.effect_ledger.project().acceptance_evidence(
                    case.run.run_id, case.opened["attempt_id"],
                )
                self.assertEqual(
                    (intent["operation_id"],),
                    evidence.incomplete_spend_operation_ids,
                )
                self.assertEqual(
                    "needs_operator",
                    case.run_ledger.project().item_outcomes(case.run.run_id)[ITEM_A],
                )

    def test_reconciled_failed_complete_spend_remains_ordinary_failure(self) -> None:
        """Lawful control: reconciled known failure with complete spend stays failed."""
        from tests.test_admission import ITEM_A

        case, _intent = self._reconciled_failed_effect_outcome("complete")
        self.assertEqual(
            "failed",
            case.run_ledger.project().item_outcomes(case.run.run_id)[ITEM_A],
        )


if __name__ == "__main__":
    unittest.main()

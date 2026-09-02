from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from floati.contracts import TaskContract, contract_digest
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.root import FloatiRoot
from floati.run_manifest import RunManifestStore
from floati.runtruth import RunLedger
from floati.scheduler import RetryPolicy, RunScheduler
from tests.schema_validation import validate_json_schema


NOW = "2026-09-01T21:00:00.000Z"
DIGEST = "a" * 64


def _record(kind: str, prefix: str, **fields: object) -> dict[str, object]:
    return {
        "schema_version": 0,
        "id": prefix + uuid7_hex(),
        "tenant_id": "alpha",
        "timestamp": NOW,
        "kind": kind,
        **fields,
    }


class RunManifestWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="floati-run-manifest-wiring-")
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open(Path(temporary.name), "alpha")

    def _started_legacy_attempt(self) -> tuple[RunScheduler, str, str, str]:
        ledger = RunLedger(self.root)
        scheduler = RunScheduler(ledger)
        run_id = "run-" + uuid7_hex()
        item_id = "work-" + uuid7_hex()
        contract = TaskContract.create(
            objective="wire manifest",
            non_goals=["no comparison"],
            areas_to_avoid=[{"path": "floati/tui.py", "region": "all"}],
            input_hashes={"brief": DIGEST},
            acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"},
            risk_class="high",
            retry_policy={
                "max_attempts": 1,
                "backoff": {
                    "base_delay_ms": 0,
                    "cap_delay_ms": 0,
                    "strategy": "fixed",
                },
            },
            dependencies=[],
        )
        ledger.append(_record(
            "run_created", "run-created-", run_id=run_id,
            plan_digest=DIGEST, item_ids=[item_id], dependency_edges=[],
        ))
        ledger.append(_record(
            "task_contract", "task-contract-", run_id=run_id,
            item_id=item_id, **contract.canonical(),
            contract_digest=contract_digest(contract),
        ))
        ledger.append(_record(
            "run_policy_bound", "run-policy-bound-", run_id=run_id,
            policy_digest=DIGEST,
        ))
        ledger.append(_record(
            "worker_pool_bound", "run-worker-pool-bound-", run_id=run_id,
            worker_ids=["worker-a"],
        ))
        opened = scheduler.open_attempt(
            run_id, item_id, RetryPolicy(1, 0, 0, strategy="fixed"), 1,
            now=NOW,
        )
        dispatch = ledger.append(_record(
            "dispatch_decision", "run-dispatch-decision-", run_id=run_id,
            item_id=item_id, attempt_id=opened["attempt_id"],
            eligible_workers=["worker-a"], chosen_worker="worker-a",
            capability_digest=DIGEST, reason_code="policy.route",
            policy_digest=DIGEST, routing_rank=0, scheduler_epoch=1,
        ))
        scheduler.start_attempt(
            run_id, item_id, str(opened["attempt_id"]), str(dispatch["id"]), now=NOW,
        )
        return scheduler, run_id, item_id, str(opened["attempt_id"])

    def test_scheduler_terminal_attempt_calls_manifest_close_product_path(self) -> None:
        scheduler, run_id, item_id, attempt_id = self._started_legacy_attempt()
        with mock.patch.object(
            RunManifestStore, "close_attempt", return_value=None,
        ) as close:
            scheduler.terminal_attempt(
                run_id, item_id, attempt_id, "cancelled", "cancelled",
                "operator_cancellation", "idempotent", now=NOW,
            )
        close.assert_called_once_with(attempt_id)

    def test_worker_runner_names_observation_writer_on_exact_attempt_path(self) -> None:
        from floati.workers import WorkerRunner

        self.assertIn(
            "observe_worker_environment",
            WorkerRunner.run.__code__.co_names,
            "WorkerRunner.run is the product caller for run_environment_observed",
        )

    def test_observation_store_is_idempotent_and_reader_lists_it(self) -> None:
        store = RunManifestStore(self.root)
        attempt_id = "attempt-" + uuid7_hex()
        fields = {
            "run_id": "run-" + uuid7_hex(),
            "item_id": "work-" + uuid7_hex(),
            "attempt_id": attempt_id,
            "adapter": "codex",
            "harness_version": None,
            "model_observed": None,
            "provider_observed": None,
            "workspace_base_commit": None,
            "toolchain_fingerprint": "b" * 64,
            "now": datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc),
        }
        first = store.observe_environment(**fields)
        second = store.observe_environment(**fields)

        self.assertEqual(first, second)
        self.assertEqual([first], store.observations())
        self.assertEqual(
            ["harness_version", "model_observed", "provider_observed", "workspace_base_commit"],
            first["unknown_fields"],
        )
        validate_json_schema(
            first, Path("schemas/v0/run-environment-observed-record.schema.json")
        )

    def test_manifest_reader_starts_empty_without_fabricating_a_fact(self) -> None:
        self.assertEqual([], RunManifestStore(self.root).records())

    def test_projector_is_byte_stable_and_source_perturbation_moves_named_field(self) -> None:
        run_id = "run-" + uuid7_hex()
        item_id = "work-" + uuid7_hex()
        attempt_id = "attempt-" + uuid7_hex()
        capability_id = "capability-set-bound-" + uuid7_hex()
        contract_id = "task-contract-" + uuid7_hex()
        observation_store = RunManifestStore(self.root)
        observation_store.observe_environment(
            run_id=run_id, item_id=item_id, attempt_id=attempt_id,
            adapter="codex", harness_version=None, model_observed=None,
            provider_observed=None, workspace_base_commit=None,
            toolchain_fingerprint="b" * 64,
            now=datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc),
        )
        terminal = {
            "timestamp": NOW,
            "terminal_state": "completed",
        }
        run = {
            "run_id": run_id,
            "attempts": {attempt_id: {
                "opened": {"item_id": item_id},
                "terminal": terminal,
                "approval_consumption": None,
            }},
            "capability_sets": {attempt_id: {
                "id": capability_id,
                "effective_grants": [{"capability_name": "review"}],
            }},
            "dispatches": {attempt_id: {
                "adapter": "codex", "policy_digest": DIGEST,
            }},
            "contracts": {item_id: {
                "task_contract_id": contract_id,
                "contract_digest": "c" * 64,
            }},
            "spawn_groups": {},
        }
        store = RunManifestStore(
            self.root,
            projection_loader=lambda: SimpleNamespace(_runs={run_id: run}),
        )

        first = store.derive_attempt(attempt_id)
        second = store.derive_attempt(attempt_id)
        self.assertEqual(first, second)
        self.assertEqual("succeeded", first["terminal_outcome"])
        self.assertIsNone(first["tool_set"])
        self.assertIsNone(first["budget_allocation"])
        self.assertIsNone(first["verification_commands"])
        self.assertIsNone(first["operator_interventions"])
        self.assertEqual(8, len(first["unknown_fields"]))
        self.assertEqual(
            {
                "budget_allocation": "attempt_budget_allocation_source_absent",
                "operator_interventions": "attempt_operator_intervention_source_absent",
                "tool_set": "attempt_tool_set_source_absent",
                "verification_commands": "attempt_verification_command_source_absent",
            },
            {
                row["field"]: row["reason"]
                for row in first["unknown_sources"]
                if row["field"] in {
                    "budget_allocation", "operator_interventions", "tool_set",
                    "verification_commands",
                }
            },
        )
        self.assertEqual(first, store.close_attempt(attempt_id))
        self.assertEqual([first], store.records())

        terminal["terminal_state"] = "failed"
        moved = store.derive_attempt(attempt_id)
        self.assertEqual("failed", moved["terminal_outcome"])
        with self.assertRaises(ProtocolRefusal) as refusal:
            store.close_attempt(attempt_id)
        self.assertEqual("run_manifest_source_drift", refusal.exception.code)

    def test_terminal_attempt_without_adapter_observation_gets_nine_typed_unknowns(self) -> None:
        run_id = "run-" + uuid7_hex()
        item_id = "work-" + uuid7_hex()
        attempt_id = "attempt-" + uuid7_hex()
        run = {
            "run_id": run_id,
            "attempts": {attempt_id: {
                "opened": {"item_id": item_id},
                "terminal": {"timestamp": NOW, "terminal_state": "cancelled"},
                "approval_consumption": None,
            }},
            "capability_sets": {attempt_id: {
                "id": "capability-set-bound-" + uuid7_hex(),
                "effective_grants": [],
            }},
            "dispatches": {attempt_id: {
                "adapter": "codex", "policy_digest": DIGEST,
            }},
            "contracts": {item_id: {
                "task_contract_id": "task-contract-" + uuid7_hex(),
                "contract_digest": "c" * 64,
            }},
            "spawn_groups": {},
        }
        store = RunManifestStore(
            self.root,
            projection_loader=lambda: SimpleNamespace(_runs={run_id: run}),
        )

        fact = store.close_attempt(attempt_id)

        self.assertEqual("cancelled", fact["terminal_outcome"])
        self.assertEqual(9, len(fact["unknown_fields"]))
        self.assertEqual(
            "run_environment_observation_record_absent",
            next(
                row["reason"] for row in fact["unknown_sources"]
                if row["field"] == "model_observed"
            ),
        )
        self.assertEqual([fact], store.records())


if __name__ == "__main__":
    unittest.main()

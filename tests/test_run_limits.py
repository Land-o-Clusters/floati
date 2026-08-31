from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.approvals import ApprovalLedger
from floati.capabilities import CapabilityGrantLedger
from floati.capability_binding import CapabilityBinder
from floati.contracts import contract_digest
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.policy import RepositoryPolicy
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.runtruth import RunLedger
from floati.scheduler import RetryPolicy, RunScheduler
from tests.test_admission import ITEM_A, ITEM_B, ITEM_C, VALID_POLICY

try:
    from floati.admission import AdmissionBinder, AdmissionPlan
    from floati.run_limits import RunLimitGate
except (ImportError, ModuleNotFoundError):
    AdmissionBinder = AdmissionPlan = RunLimitGate = None


NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


def _contract(dependencies=()):
    return {
        "objective": "enforce physical dispatch limits",
        "non_goals": ["no hidden scheduler cache"],
        "areas_to_avoid": [{"path": "bundle/c7.2", "region": "all"}],
        "input_hashes": {"brief": "b" * 64},
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


class _RunLimitCase:
    def __init__(
        self,
        testcase,
        *,
        max_active=3,
        worker_max=2,
        workspace_keys=("workspace-a", "workspace-b", "workspace-c"),
        concurrency_keys=("concurrency-a", "concurrency-b", "concurrency-c"),
        edges=(),
    ):
        testcase.assertIsNotNone(AdmissionBinder, "floati.admission must provide AdmissionBinder")
        testcase.assertIsNotNone(RunLimitGate, "floati.run_limits must provide RunLimitGate")
        self.temp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        self.base = base
        policy_text = VALID_POLICY.replace(
            "max_active_attempts = 2", f"max_active_attempts = {max_active}"
        ).replace("max_concurrency = 2", f"max_concurrency = {worker_max}")
        self.policy_path = base / "FLOATI.toml"
        self.policy_path.write_text(policy_text, encoding="utf-8")
        self.policy = RepositoryPolicy.load(self.policy_path)
        self.item_ids = (ITEM_A, ITEM_B, ITEM_C)
        dependencies = {item_id: [] for item_id in self.item_ids}
        for edge in edges:
            dependencies[edge["target"]].append(edge["source"])
        items = []
        for index, item_id in enumerate(self.item_ids):
            items.append(
                {
                    "item_id": item_id,
                    "contract": _contract(sorted(dependencies[item_id])),
                    "capability_selector": "review_write",
                    "requires_cancellation": True,
                    "requires_callback": True,
                    "workspace_key": workspace_keys[index],
                    "concurrency_key": concurrency_keys[index],
                    "retry_class": "transient",
                    "effect_safety": "idempotent",
                    "merge_gate": None,
                }
            )
        plan_value = {
            "schema_version": 0,
            "workers": [
                {"node_id": "node-a", "worker_profile": "good"},
                {"node_id": "node-b", "worker_profile": "good"},
            ],
            "max_active_attempts": max_active,
            "budget_reservations": [{"budget_id": "build", "amount": 1}],
            "items": items,
            "dependency_edges": list(edges),
        }
        plan_path = base / "admission-plan.json"
        plan_path.write_text(json.dumps(plan_value, separators=(",", ":")), encoding="utf-8")
        self.plan = AdmissionPlan.load(plan_path)
        self.root = FloatiRoot.open_direct_home(base / "alpha", create=True)
        self.ledger = RunLedger(self.root)
        registry = Registry(self.root)
        for worker in ("node-a", "node-b"):
            registry.register(worker, "Codex")
        registry.register(public_ids.reviewer(), "Claude")
        authority = AuthorityGrantStore(self.root).claim(
            "approve-build", public_ids.reviewer(), 300, 300, NOW
        )
        approvals = ApprovalLedger(self.root)
        self.grants = CapabilityGrantLedger(self.root)
        offset = 0
        for worker in ("node-a", "node-b"):
            for capability in ("review", "workspace_write"):
                offset += 1
                request = approvals.request(
                    worker, capability, "worker:" + worker, 120,
                    "approve-build", authority["epoch"],
                    now=NOW + timedelta(seconds=offset),
                )
                decision = approvals.decide(
                    request["id"], public_ids.reviewer(), "approved", None,
                    granted_scope="worker:" + worker, granted_ttl_seconds=90,
                    now=NOW + timedelta(seconds=offset, milliseconds=100),
                )
                self.grants.grant(
                    worker, capability, self.policy, request["id"], decision["id"],
                    now=NOW + timedelta(seconds=offset, milliseconds=200),
                )
        self.run_id = "run-" + uuid7_hex()
        self._append(
            "run_created", "run-created-", plan_digest=self.plan.digest,
            policy_digest=self.policy.digest, item_ids=list(self.item_ids),
            dependency_edges=list(edges),
        )
        for item in self.plan.items:
            self._append(
                "task_contract", "task-contract-", item_id=item.item_id,
                **item.contract.canonical(),
                contract_digest=contract_digest(item.contract),
            )
        self._append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
        self._append(
            "worker_pool_bound", "run-worker-pool-bound-",
            worker_ids=["node-a", "node-b"],
        )
        AdmissionBinder.bind(
            self.ledger, self.run_id, self.plan, self.policy, now=NOW
        )
        self.scheduler = RunScheduler(self.ledger)
        self.opened = {
            item_id: self.scheduler.open_attempt(
                self.run_id, item_id, RetryPolicy(1, 0, 0, strategy="fixed"),
                1, now=NOW,
            )
            for item_id in self.item_ids
        }
        self.binder = CapabilityBinder(self.ledger, self.grants)
        self.snapshots = {}

    def _append(self, kind, prefix, **fields):
        return self.ledger.append(
            {
                "schema_version": 0,
                "id": prefix + uuid7_hex(),
                "tenant_id": "alpha",
                "timestamp": "2026-08-09T14:00:00.000Z",
                "kind": kind,
                "run_id": self.run_id,
                **fields,
            }
        )

    def dispatch(self, item_id, worker="node-a"):
        snapshot = self.snapshot(item_id, worker)
        return self.binder.dispatch(
            snapshot["id"], [worker], "policy.route", self.policy,
            now=NOW + timedelta(seconds=20),
        )

    def snapshot(self, item_id, worker="node-a"):
        key = (item_id, worker)
        if key not in self.snapshots:
            opened = self.opened[item_id]
            self.snapshots[key] = self.binder.bind(
                self.run_id, item_id, opened["attempt_id"], worker, "good",
                self.policy, 0, now=NOW + timedelta(seconds=10),
            )
        return self.snapshots[key]

    def add_run(self, workspace_key, concurrency_key, worker="node-a"):
        run_id = "run-" + uuid7_hex()
        item_id = "work-" + uuid7_hex()
        contract = _contract()
        plan_value = {
            "schema_version": 0,
            "workers": [
                {"node_id": "node-a", "worker_profile": "good"},
                {"node_id": "node-b", "worker_profile": "good"},
            ],
            "max_active_attempts": 1,
            "budget_reservations": [{"budget_id": "build", "amount": 1}],
            "items": [{
                "item_id": item_id,
                "contract": contract,
                "capability_selector": "review_write",
                "requires_cancellation": True,
                "requires_callback": True,
                "workspace_key": workspace_key,
                "concurrency_key": concurrency_key,
                "retry_class": "transient",
                "effect_safety": "idempotent",
                "merge_gate": None,
            }],
            "dependency_edges": [],
        }
        path = self.base / (run_id + "-admission-plan.json")
        path.write_text(json.dumps(plan_value, separators=(",", ":")), encoding="utf-8")
        plan = AdmissionPlan.load(path)

        def append(kind, prefix, **fields):
            return self.ledger.append({
                "schema_version": 0, "id": prefix + uuid7_hex(),
                "tenant_id": "alpha", "timestamp": "2026-08-09T14:00:00.000Z",
                "kind": kind, "run_id": run_id, **fields,
            })

        append(
            "run_created", "run-created-", plan_digest=plan.digest,
            policy_digest=self.policy.digest, item_ids=[item_id], dependency_edges=[],
        )
        append(
            "task_contract", "task-contract-", item_id=item_id,
            **plan.items[0].contract.canonical(),
            contract_digest=contract_digest(plan.items[0].contract),
        )
        append("run_policy_bound", "run-policy-bound-", policy_digest=self.policy.digest)
        append(
            "worker_pool_bound", "run-worker-pool-bound-",
            worker_ids=["node-a", "node-b"],
        )
        AdmissionBinder.bind(self.ledger, run_id, plan, self.policy, now=NOW)
        opened = RunScheduler(self.ledger).open_attempt(
            run_id, item_id, RetryPolicy(1, 0, 0, strategy="fixed"), 1, now=NOW
        )
        snapshot = self.binder.bind(
            run_id, item_id, opened["attempt_id"], worker, "good",
            self.policy, 0, now=NOW + timedelta(seconds=10),
        )
        return run_id, item_id, opened, snapshot


class RunLimitTests(unittest.TestCase):
    def test_cross_run_keys_and_worker_capacity_are_global_with_independent_positive_control(self) -> None:
        """Catches per-run resource accounting or a vacuous global deny-all implementation."""
        cases = (
            (
                "workspace_key_busy", 3, 2,
                "workspace-a", "other-concurrency", "node-b",
            ),
            (
                "concurrency_key_busy", 3, 2,
                "other-workspace", "concurrency-a", "node-b",
            ),
            (
                "worker_concurrency_exhausted", 2, 1,
                "other-workspace", "other-concurrency", "node-a",
            ),
        )
        for code, max_active, worker_max, workspace_key, concurrency_key, worker in cases:
            with self.subTest(code=code):
                case = _RunLimitCase(
                    self, max_active=max_active, worker_max=worker_max
                )
                case.dispatch(ITEM_A, "node-a")
                _run_id, _item_id, _opened, snapshot = case.add_run(
                    workspace_key, concurrency_key, worker
                )
                before = case.ledger.records()
                with self.assertRaises(ProtocolRefusal) as caught:
                    case.binder.dispatch(
                        snapshot["id"], [worker], "policy.route", case.policy,
                        now=NOW + timedelta(seconds=20),
                    )
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(before, case.ledger.records())
                consumers = case.ledger.project().run(snapshot["run_id"])[
                    "capability_set_consumers"
                ]
                self.assertNotIn(snapshot["id"], consumers)

        positive = _RunLimitCase(self, max_active=2, worker_max=1)
        positive.dispatch(ITEM_A, "node-a")
        _run_id, _item_id, _opened, snapshot = positive.add_run(
            "independent-workspace", "independent-concurrency", "node-b"
        )
        dispatched = positive.binder.dispatch(
            snapshot["id"], ["node-b"], "policy.route", positive.policy,
            now=NOW + timedelta(seconds=20),
        )
        self.assertEqual("dispatch_decision", dispatched["kind"])
        consumers = positive.ledger.project().run(snapshot["run_id"])[
            "capability_set_consumers"
        ]
        self.assertEqual(dispatched["id"], consumers[snapshot["id"]])

    def test_active_run_ceiling_uses_dispatched_nonterminal_reservations(self) -> None:
        """Catches opened-only counts or a stale count that permits a third active dispatch."""
        case = _RunLimitCase(self, max_active=2, worker_max=8)
        case.dispatch(ITEM_A, "node-a")
        case.dispatch(ITEM_B, "node-b")
        with self.assertRaises(ProtocolRefusal) as caught:
            RunLimitGate.check_dispatch(
                case.ledger.project(), case.snapshot(ITEM_C), case.policy
            )
        self.assertEqual("run_concurrency_exhausted", caught.exception.code)

    def test_workspace_and_concurrency_keys_block_distinct_active_items(self) -> None:
        """Catches loose dispatch fields overriding either integrity-bound exclusion key."""
        edge = {
            "source": ITEM_A,
            "target": ITEM_B,
            "requires": "accepted",
            "failure_policy": "fail_run",
        }
        cases = (
            (
                "workspace_key_busy",
                dict(
                    workspace_keys=("workspace-shared", "workspace-shared", "workspace-c"),
                    concurrency_keys=("concurrency-a", "concurrency-b", "concurrency-c"),
                ),
            ),
            (
                "concurrency_key_busy",
                dict(
                    workspace_keys=("workspace-a", "workspace-b", "workspace-c"),
                    concurrency_keys=("concurrency-shared", "concurrency-shared", "concurrency-c"),
                ),
            ),
        )
        for code, options in cases:
            with self.subTest(code=code):
                case = _RunLimitCase(self, edges=(edge,), **options)
                case.dispatch(ITEM_A)
                with self.assertRaises(ProtocolRefusal) as caught:
                    RunLimitGate.check_dispatch(
                        case.ledger.project(), case.snapshot(ITEM_B), case.policy
                    )
                self.assertEqual(code, caught.exception.code)

    def test_worker_profile_ceiling_counts_physical_active_dispatches(self) -> None:
        """Catches per-worker capacity being confused with run-wide capacity."""
        case = _RunLimitCase(self, max_active=3, worker_max=2)
        case.dispatch(ITEM_A)
        case.dispatch(ITEM_B)
        with self.assertRaises(ProtocolRefusal) as caught:
            RunLimitGate.check_dispatch(
                case.ledger.project(), case.snapshot(ITEM_C), case.policy
            )
        self.assertEqual("worker_concurrency_exhausted", caught.exception.code)

    def test_terminal_attempt_releases_every_runtime_reservation(self) -> None:
        """Catches terminal dispatches permanently occupying run, key, or worker capacity."""
        edge = {
            "source": ITEM_A,
            "target": ITEM_B,
            "requires": "accepted",
            "failure_policy": "fail_run",
        }
        case = _RunLimitCase(
            self,
            max_active=2,
            workspace_keys=("workspace-shared", "workspace-shared", "workspace-c"),
            concurrency_keys=("concurrency-shared", "concurrency-shared", "concurrency-c"),
            edges=(edge,),
        )
        dispatch = case.dispatch(ITEM_A)
        opened = case.opened[ITEM_A]
        case.scheduler.start_attempt(
            case.run_id, ITEM_A, opened["attempt_id"], dispatch["id"], now=NOW
        )
        case.scheduler.terminal_attempt(
            case.run_id, ITEM_A, opened["attempt_id"], "failed", "permanent",
            "permanent_failure", "idempotent", now=NOW + timedelta(seconds=1),
        )
        self.assertIsNone(
            RunLimitGate.check_dispatch(
                case.ledger.project(), case.snapshot(ITEM_B), case.policy
            )
        )

    def test_current_policy_and_bound_plan_evidence_must_still_match(self) -> None:
        """Catches policy drift being treated as capacity testimony for an older admission."""
        case = _RunLimitCase(self)
        changed_path = case.policy_path.parent / "changed-policy" / "FLOATI.toml"
        changed_path.parent.mkdir()
        changed_path.write_text(
            case.policy_path.read_text(encoding="utf-8").replace(
                "max_concurrency = 2", "max_concurrency = 1"
            ),
            encoding="utf-8",
        )
        changed = RepositoryPolicy.load(changed_path)
        with self.assertRaises(ProtocolRefusal) as caught:
            RunLimitGate.check_dispatch(
                case.ledger.project(), case.snapshot(ITEM_A), changed
            )
        self.assertEqual("run_admission_policy_mismatch", caught.exception.code)


class EffectBudgetTests(unittest.TestCase):
    def test_measured_spend_cannot_exceed_intent_attempt_or_run_bounds(self) -> None:
        """Catches acceptance counting claims as spend or permitting measured overrun."""
        from floati.effects import EffectAcceptanceEvidence

        case = _RunLimitCase(self)
        self.assertTrue(
            callable(getattr(RunLimitGate, "check_effect_spend", None)),
            "RunLimitGate must validate measured Effect spend",
        )
        attempt_id = case.opened[ITEM_A]["attempt_id"]
        lawful = EffectAcceptanceEvidence(
            operation_ids=("effect-op-018f0000000070008000000000000001",),
            high_watermark=4,
            evidence_digest="a" * 64,
            measured_spend=(("build", 1),), blockers=(),
        )
        self.assertIsNone(RunLimitGate.check_effect_spend(
            case.ledger.project(), case.run_id, ITEM_A, attempt_id, lawful,
        ))
        overrun = EffectAcceptanceEvidence(
            operation_ids=lawful.operation_ids,
            high_watermark=lawful.high_watermark,
            evidence_digest=lawful.evidence_digest,
            measured_spend=(("build", 2),), blockers=(),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            RunLimitGate.check_effect_spend(
                case.ledger.project(), case.run_id, ITEM_A, attempt_id, overrun,
            )
        self.assertEqual("effect_budget_exceeded", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

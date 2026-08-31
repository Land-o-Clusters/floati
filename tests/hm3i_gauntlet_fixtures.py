"""Test-only HM-3I durable-truth traces and inventory assertions.

Nothing in this module is deployable.  It deliberately builds valid prefixes
through the public owners before a gauntlet mutates persisted bytes.
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Mapping, Sequence, Tuple

from floati.approvals import CapabilityLedger
from floati.cancellation import CancellationCoordinator, FloatiSupervisor
from floati.contracts import TaskContract, contract_digest
from floati.decisions import DecisionRegister, decision_digest
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import AuthorityGrantStore
from floati.policy import PolicyDeploymentChecker, RepositoryPolicy
from floati.admission import AdmissionEvaluator, AdmissionPlan
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.runtruth import ITEM_OUTCOMES, LEGACY_RUN_KINDS, RUN_OUTCOMES, RunLedger, RunProjection
from floati.scheduler import RetryPolicy, RunScheduler
from floati.workers import WorkerReceipts


NOW = "2026-08-08T12:00:00.000Z"
AUTH_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
DIGEST = "a" * 64

CANONICAL_RUN_KINDS = frozenset(
    {
        "run_created",
        "task_contract",
        "plan_amendment",
        "run_policy_bound",
        "worker_pool_bound",
        "dispatch_decision",
        "result_produced",
        "result_verified",
        "acceptance_receipt",
        "result_accepted",
        "run_terminal",
        "attempt_opened",
        "attempt_started",
        "attempt_terminal",
        "retry_scheduled",
        "retry_exhausted",
        "cancel_requested",
        "cancel_scope_resolved",
        "cancel_observed",
        "cancel_signal_sent",
        "cancel_terminal",
        "cancel_unconfirmed",
        "stale_attempt_evidence",
        "stale_evidence_adopted",
        "attempt_harness_session_bound",
        "supervisor_orphaned",
    }
)
AUXILIARY_FAMILIES = frozenset(
    {"policy", "admission", "decision_record", "handoff_capsule"}
)
GAUNTLET_AXES = ("crash", "fuzz", "time", "recovery", "contention")
ITEM5_OUTCOMES = frozenset(
    {"succeeded", "failed", "cancelled", "skipped", "needs_operator", "uncertain"}
)
ITEM5_RUN_OUTCOMES = ITEM5_OUTCOMES | frozenset({"partially_succeeded"})

@dataclass(frozen=True)
class Trace:
    root: FloatiRoot
    ledger: RunLedger
    run_id: str
    item_ids: Tuple[str, ...]
    records_by_kind: Mapping[str, Tuple[Mapping[str, object], ...]]
    attempt_ids: Tuple[str, ...]
    worker_receipt_ids: Tuple[str, ...]
    claim_id: str | None = None
    lease_id: str | None = None
    worker_session_id: str | None = None

    @property
    def records(self) -> Tuple[Mapping[str, object], ...]:
        return tuple(self.ledger.records())


def axis_coverage_from_traces(
    axis: str, traces: Sequence[Trace]
) -> Mapping[str, bool]:
    """Derive one gauntlet axis's literal run-kind coverage from its real traces."""

    if axis not in GAUNTLET_AXES:
        raise ValueError(f"unknown gauntlet axis: {axis}")
    observed = frozenset(
        str(record["kind"])
        for trace in traces
        for record in trace.records
    )
    coverage = {
        kind: kind in observed
        for kind in sorted(CANONICAL_RUN_KINDS)
    }
    missing = sorted(kind for kind, covered in coverage.items() if not covered)
    unexpected = sorted(observed - CANONICAL_RUN_KINDS)
    if missing or unexpected:
        raise AssertionError(
            f"HM-3I {axis} trace coverage mismatch: "
            f"missing={missing!r}, unexpected={unexpected!r}"
        )
    return coverage


@dataclass(frozen=True)
class CanonicalObservation:
    item_outcomes: Tuple[Tuple[str, str], ...]
    run_outcome: str
    contract_history: Tuple[Tuple[str, Tuple[str, ...], str], ...]
    current_attempts: Tuple[Tuple[str, str, str], ...]
    cancellation_ids: Tuple[str, ...]
    stale_evidence_ids: Tuple[str, ...]
    stale_adoption_ids: Tuple[str, ...]
    harness_sessions: Tuple[Tuple[str, Tuple[str, ...]], ...]
    orphan_receipt_ids: Tuple[str, ...]


@dataclass(frozen=True)
class PolicyCase:
    path: Path
    policy: RepositoryPolicy
    digest: str
    status: str


@dataclass(frozen=True)
class AdmissionCase:
    plan_path: Path
    policy_path: Path
    plan: AdmissionPlan
    policy: RepositoryPolicy
    outcome: str
    machine: Mapping[str, object]


@dataclass(frozen=True)
class DecisionCase:
    register: DecisionRegister
    proposal: Mapping[str, object]
    record: Mapping[str, object]
    capsule: Mapping[str, object]


class _CancellationAdapter:
    def __init__(self, mode: str) -> None:
        self.cancel_mode = mode
        self.actions: list[str] = []

    def cancel(self) -> None:
        self.actions.append("cancel")

    def cancel_local_process(self) -> None:
        self.actions.append("cancel_local_process")


def assert_inventory_and_coverage() -> None:
    """Fail closed when the frozen durable vocabulary or Item 5 outcomes drift."""

    if LEGACY_RUN_KINDS != CANONICAL_RUN_KINDS:
        raise AssertionError(
            "HM-3I run-kind inventory drift: "
            + repr((sorted(CANONICAL_RUN_KINDS), sorted(LEGACY_RUN_KINDS)))
        )
    if ITEM_OUTCOMES != ITEM5_OUTCOMES or RUN_OUTCOMES != ITEM5_RUN_OUTCOMES:
        raise AssertionError("HM-3I Item 5 outcome vocabulary drift")


def _record(kind: str, **fields: object) -> Dict[str, object]:
    prefixes = {
        "run_created": "run-created-",
        "task_contract": "task-contract-",
        "plan_amendment": "plan-amendment-",
        "run_policy_bound": "run-policy-bound-",
        "worker_pool_bound": "run-worker-pool-bound-",
        "dispatch_decision": "run-dispatch-decision-",
        "result_produced": "run-result-produced-",
        "result_verified": "run-result-verified-",
        "acceptance_receipt": "acceptance-receipt-",
        "result_accepted": "run-result-accepted-",
        "run_terminal": "run-terminal-",
    }
    return {
        "schema_version": 0,
        "id": prefixes[kind] + uuid7_hex(),
        "tenant_id": "alpha",
        "timestamp": NOW,
        "kind": kind,
        **fields,
    }


def _append_owned(ledger: RunLedger, kind: str, **fields: object) -> Mapping[str, object]:
    """Build an ordinary-owner record for the selected tenant, then append it."""

    record = _record(kind, **fields)
    record["tenant_id"] = ledger.root.tenant_id
    return ledger.append(record)


def _contract(policy: RetryPolicy, *, objective: str) -> TaskContract:
    return TaskContract.create(
        objective=objective,
        non_goals=["no unbounded authority"],
        areas_to_avoid=[{"path": "slip/graph.py", "region": "all"}],
        input_hashes={"brief": DIGEST},
        acceptance_checks={"tests.unit": "python3 -m unittest"},
        constraints={"network": "dark"},
        risk_class="high",
        retry_policy={
            "max_attempts": policy.max_attempts,
            "backoff": {
                "base_delay_ms": policy.base_delay_ms,
                "cap_delay_ms": policy.cap_delay_ms,
                "strategy": policy.strategy,
            },
        },
        dependencies=[],
    )


def _worker_receipt(root: FloatiRoot, item_id: str) -> Mapping[str, object]:
    receipt: Dict[str, object] = {
        "schema_version": 0,
        "id": "worker-receipt-" + uuid7_hex(),
        "tenant_id": root.tenant_id,
        "timestamp": NOW,
        "kind": "worker_receipt",
        "session_id": "worker-" + uuid7_hex(),
        "work_item_id": item_id,
        "node_id": "worker-a",
        "adapter": "fixture",
        "transition": "claim",
        "outcome_code": None,
        "authority_subject": "authority",
        "authority_epoch": 1,
        "artifact_bindings": [],
    }
    append_record(
        root, "receipts/workers.jsonl", receipt, allowed_kinds={"worker_receipt"}
    )
    return receipt


def _append_base(
    root: FloatiRoot, *, policy: RetryPolicy, amend: bool = True
) -> tuple[RunLedger, str, str, TaskContract]:
    ledger = RunLedger(root)
    run_id = "run-" + uuid7_hex()
    item_id = "work-" + uuid7_hex()
    initial = _contract(policy, objective="initial governed work")
    _append_owned(
        ledger,
        "run_created",
            run_id=run_id,
            plan_digest=DIGEST,
            item_ids=[item_id],
            dependency_edges=[],
    )
    contract_record = _append_owned(
        ledger,
        "task_contract",
            run_id=run_id,
            item_id=item_id,
            **initial.canonical(),
            contract_digest=contract_digest(initial),
    )
    current = initial
    if amend:
        current = initial.replaced(objective="amended governed work")
        _append_owned(
            ledger,
            "plan_amendment",
                run_id=run_id,
                item_id=item_id,
                task_contract_id=contract_record["id"],
                previous_digest=contract_record["contract_digest"],
                replacement_fields={"objective": "amended governed work"},
                contract_digest=contract_digest(current),
        )
    _append_owned(ledger, "run_policy_bound", run_id=run_id, policy_digest=DIGEST)
    _append_owned(
        ledger, "worker_pool_bound", run_id=run_id, worker_ids=["worker-a"]
    )
    return ledger, run_id, item_id, current


def _dispatch(ledger: RunLedger, run_id: str, item_id: str, opened: Mapping[str, object]) -> Mapping[str, object]:
    return _append_owned(
        ledger,
        "dispatch_decision",
            run_id=run_id,
            item_id=item_id,
            attempt_id=opened["attempt_id"],
            eligible_workers=["worker-a"],
            chosen_worker="worker-a",
            capability_digest=DIGEST,
            reason_code="policy.route",
            policy_digest=DIGEST,
            routing_rank=0,
            scheduler_epoch=opened["scheduler_epoch"],
    )


def _trace(
    root: FloatiRoot,
    ledger: RunLedger,
    run_id: str,
    item_ids: Sequence[str],
    *,
    worker_receipt_ids: Sequence[str] = (),
    claim_id: str | None = None,
    lease_id: str | None = None,
    worker_session_id: str | None = None,
) -> Trace:
    grouped: Dict[str, list[Mapping[str, object]]] = {}
    for record in ledger.records():
        grouped.setdefault(str(record["kind"]), []).append(record)
    return Trace(
        root=root,
        ledger=ledger,
        run_id=run_id,
        item_ids=tuple(item_ids),
        records_by_kind={key: tuple(value) for key, value in grouped.items()},
        attempt_ids=tuple(
            str(record["attempt_id"])
            for record in grouped.get("attempt_opened", ())
        ),
        worker_receipt_ids=tuple(worker_receipt_ids),
        claim_id=claim_id,
        lease_id=lease_id,
        worker_session_id=worker_session_id,
    )


def build_success_trace(root: FloatiRoot) -> Trace:
    """Build the accepted complete lifecycle through ordinary and scheduler owners."""

    policy = RetryPolicy(1, 10, 10, "fixed")
    ledger, run_id, item_id, current_contract = _append_base(root, policy=policy)
    scheduler = RunScheduler(ledger)
    opened = scheduler.open_attempt(run_id, item_id, policy, 1, now=NOW)
    dispatch = _dispatch(ledger, run_id, item_id, opened)
    scheduler.start_attempt(run_id, item_id, str(opened["attempt_id"]), str(dispatch["id"]), now=NOW)
    receipt = _worker_receipt(root, item_id)
    produced = _append_owned(
        ledger,
        "result_produced",
            run_id=run_id,
            item_id=item_id,
            attempt_id=opened["attempt_id"],
            dispatch_decision_id=dispatch["id"],
            worker_receipt_ids=[receipt["id"]],
    )
    verified = _append_owned(
        ledger,
        "result_verified",
            run_id=run_id,
            item_id=item_id,
            attempt_id=opened["attempt_id"],
            result_produced_id=produced["id"],
            worker_receipt_ids=[receipt["id"]],
    )
    acceptance = _append_owned(
        ledger,
        "acceptance_receipt",
            run_id=run_id,
            item_id=item_id,
            attempt_id=opened["attempt_id"],
            contract_digest=contract_digest(current_contract),
            check_ids=["tests.unit"],
            reviewer="reviewer-a",
            evidence_bindings=[receipt["id"]],
            deviations=[],
            result="accepted",
    )
    _append_owned(
        ledger,
        "result_accepted",
            run_id=run_id,
            item_id=item_id,
            attempt_id=opened["attempt_id"],
            predecessor_result_id=verified["id"],
            acceptance_mode="verified",
            acceptance_receipt_id=acceptance["id"],
            worker_receipt_ids=[receipt["id"]],
    )
    scheduler.terminal_attempt(
        run_id,
        item_id,
        str(opened["attempt_id"]),
        "completed",
        None,
        "completed",
        "idempotent",
        now=NOW,
    )
    _append_owned(
        ledger, "run_terminal", run_id=run_id, outcome=ledger.project().run_outcome(run_id)
    )
    return _trace(root, ledger, run_id, [item_id], worker_receipt_ids=[str(receipt["id"])])


def _authorize(root: FloatiRoot, *, actor: str, role: str, capability_name: str, subject: str) -> Mapping[str, object]:
    Registry(root).register(actor, role)
    grant = AuthorityGrantStore(root).claim(subject, actor, 120, 120, AUTH_NOW)
    capability = CapabilityLedger(root).declare(
        actor, capability_name, "read_write", "run", 60, now=AUTH_NOW
    )
    return {
        "authority_subject": subject,
        "authority_epoch": grant["epoch"],
        "capability_record_id": capability["id"],
    }


def build_retry_stale_trace(root: FloatiRoot) -> Trace:
    """Build retry reservation/exhaustion plus explicit stale retention/adoption."""

    policy = RetryPolicy(2, 10, 10, "fixed")
    ledger, run_id, item_id, _contract_value = _append_base(root, policy=policy)
    scheduler = RunScheduler(ledger)
    coordinator = CancellationCoordinator(ledger)
    first = scheduler.open_attempt(run_id, item_id, policy, 1, now=NOW)
    first_dispatch = _dispatch(ledger, run_id, item_id, first)
    scheduler.start_attempt(run_id, item_id, str(first["attempt_id"]), str(first_dispatch["id"]), now=NOW)
    scheduler.terminal_attempt(
        run_id, item_id, str(first["attempt_id"]), "failed", "transient",
        "transient_failure", "idempotent", now=NOW,
    )
    second = scheduler.open_attempt(run_id, item_id, policy, 1, now=NOW)
    receipt = _worker_receipt(root, item_id)
    stale = coordinator.retain_late_receipt(
        run_id,
        item_id,
        str(first["attempt_id"]),
        [str(receipt["id"])],
        str(first["fence_token"]),
        now=NOW,
    )
    adoption = coordinator.adopt_stale_evidence(
        run_id,
        str(stale["id"]),
        operator_id="operator-a",
        now=AUTH_NOW,
        **_authorize(
            root,
            actor="operator-a",
            role="Operator",
            capability_name="stale_evidence.adopt",
            subject="stale-adoption",
        ),
    )
    second_dispatch = _dispatch(ledger, run_id, item_id, second)
    scheduler.start_attempt(run_id, item_id, str(second["attempt_id"]), str(second_dispatch["id"]), now=NOW)
    scheduler.terminal_attempt(
        run_id, item_id, str(second["attempt_id"]), "failed", "transient",
        "transient_failure", "idempotent", now=NOW,
    )
    return _trace(root, ledger, run_id, [item_id], worker_receipt_ids=[str(receipt["id"])])


def build_cancellation_trace(root: FloatiRoot, cancel_mode: str) -> Trace:
    """Build one exactly ordered cancellation mode through its coordinator."""

    if cancel_mode not in {"native", "local_process_only", "unavailable"}:
        raise ValueError("cancel_mode must be a governed cancellation mode")
    policy = RetryPolicy(1, 10, 10, "fixed")
    ledger, run_id, item_id, _contract_value = _append_base(root, policy=policy)
    scheduler = RunScheduler(ledger)
    opened = scheduler.open_attempt(run_id, item_id, policy, 1, now=NOW)
    dispatch = _dispatch(ledger, run_id, item_id, opened)
    scheduler.start_attempt(run_id, item_id, str(opened["attempt_id"]), str(dispatch["id"]), now=NOW)
    CancellationCoordinator(ledger).request(
        run_id,
        {"worker-a": _CancellationAdapter(cancel_mode)},
        item_id=item_id,
        now=NOW,
    )
    return _trace(root, ledger, run_id, [item_id])


def build_foc_orphan_trace(root: FloatiRoot) -> Trace:
    """Build FOC's stable joins, two ordered harness sessions, and all orphan classes."""

    policy = RetryPolicy(1, 10, 10, "fixed")
    ledger, run_id, item_id, _contract_value = _append_base(root, policy=policy)
    scheduler = RunScheduler(ledger)
    opened = scheduler.open_attempt(run_id, item_id, policy, 1, now=NOW)
    dispatch = _dispatch(ledger, run_id, item_id, opened)
    scheduler.start_attempt(run_id, item_id, str(opened["attempt_id"]), str(dispatch["id"]), now=NOW)
    claim_id = "claim-" + uuid7_hex()
    lease_id = "lease-" + uuid7_hex()
    worker_session_id = "worker-" + uuid7_hex()
    second_session = "worker-" + uuid7_hex()
    coordinator = CancellationCoordinator(ledger)
    coordinator.bind_harness_session(
        run_id,
        item_id,
        str(opened["attempt_id"]),
        claim_id=claim_id,
        lease_id=lease_id,
        worker_session_id=worker_session_id,
        harness_segments=[
            {"ordinal": 1, "harness_session_id": worker_session_id},
            {"ordinal": 2, "harness_session_id": second_session},
        ],
        now=NOW,
    )
    authorization = _authorize(
        root,
        actor="floati-supervisor",
        role="FloatiSupervisor",
        capability_name="orphan.emit",
        subject="orphaning",
    )
    supervisor = FloatiSupervisor(ledger)
    for emitter in (
        supervisor.owner_loss,
        supervisor.unregister,
        supervisor.lease_abandonment,
    ):
        emitter(
            run_id,
            item_id,
            str(opened["attempt_id"]),
            claim_id=claim_id,
            lease_id=lease_id,
            worker_session_id=worker_session_id,
            now=AUTH_NOW,
            **authorization,
        )
    return _trace(
        root,
        ledger,
        run_id,
        [item_id],
        claim_id=claim_id,
        lease_id=lease_id,
        worker_session_id=worker_session_id,
    )


def build_full_run_trace_set(base: Path) -> Tuple[Trace, ...]:
    """Build every literal run family through its owner APIs for one gauntlet axis."""

    return (
        build_success_trace(
            FloatiRoot.open_direct_home(base / "success", create=True)
        ),
        build_retry_stale_trace(
            FloatiRoot.open_direct_home(base / "retry", create=True)
        ),
        build_cancellation_trace(
            FloatiRoot.open_direct_home(base / "cancellation-native", create=True),
            "native",
        ),
        build_cancellation_trace(
            FloatiRoot.open_direct_home(
                base / "cancellation-local-process-only", create=True
            ),
            "local_process_only",
        ),
        build_cancellation_trace(
            FloatiRoot.open_direct_home(base / "cancellation-unavailable", create=True),
            "unavailable",
        ),
        build_foc_orphan_trace(
            FloatiRoot.open_direct_home(base / "foc", create=True)
        ),
    )


_POLICY_TEXT = '''schema_version = 0
capability_registry = ["review", "workspace_write"]

[limits]
max_items = 8
max_depth = 4
max_fan_out = 2
max_active_attempts = 2

[budgets.build]
unit = "attempts"
limit = 5

[worker_profiles.codex]
capabilities = ["review", "workspace_write"]
cancel_mode = "native"
callback_support = true
max_concurrency = 2

[capability_selectors.review_write]
all_of = ["review", "workspace_write"]

[routing.review_write_codex]
worker_profile = "codex"
capability_selector = "review_write"
rank = 0

[retry_classes.transient]
automatic = true
[retry_classes.permanent]
automatic = false
[retry_classes.operator_required]
automatic = false
[retry_classes.policy_refusal]
automatic = false
[retry_classes.cancelled]
automatic = false
[retry_classes.unknown_effect]
automatic = false

[approval_requirements.low]
required = false
[approval_requirements.medium]
required = false
[approval_requirements.high]
required = true
[approval_requirements.critical]
required = true

[verification.unit]
argv = ["python3", "-m", "unittest", "tests.test_policy"]

[merge_gates.local]
verification_ids = ["unit"]
'''


def _fixture_inputs(root: FloatiRoot) -> Path:
    directory = root.path / "hm3i-gauntlet-inputs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def build_policy_case(root: FloatiRoot) -> PolicyCase:
    inputs = _fixture_inputs(root)
    path = inputs / "FLOATI.toml"
    path.write_text(_POLICY_TEXT, encoding="utf-8")
    policy = RepositoryPolicy.load(path)
    checked = PolicyDeploymentChecker.check(path, policy.digest)
    return PolicyCase(path=path, policy=policy, digest=policy.digest, status=checked.status.value)


def build_admission_case(root: FloatiRoot) -> AdmissionCase:
    inputs = _fixture_inputs(root)
    policy_path = inputs / "FLOATI.toml"
    policy_path.write_text(_POLICY_TEXT, encoding="utf-8")
    item_id = "work-" + uuid7_hex()
    plan_path = inputs / "admission-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 0,
                "workers": [{"node_id": "node-a", "worker_profile": "codex"}],
                "max_active_attempts": 1,
                "budget_reservations": [{"budget_id": "build", "amount": 1}],
                "items": [
                    {
                        "item_id": item_id,
                        "contract": {
                            "objective": "admit finite work",
                            "non_goals": ["no model authority"],
                            "areas_to_avoid": [{"path": "slip/graph.py", "region": "all"}],
                            "input_hashes": {"brief": DIGEST},
                            "acceptance_checks": {"tests.unit": "python3 -m unittest"},
                            "constraints": {"network": "dark"},
                            "risk_class": "low",
                            "retry_policy": {"max_attempts": 1, "backoff": {"base_delay_ms": 0, "cap_delay_ms": 1, "strategy": "fixed"}},
                            "dependencies": [],
                        },
                        "capability_selector": "review_write",
                        "requires_cancellation": True,
                        "requires_callback": True,
                        "workspace_key": "workspace-a",
                        "concurrency_key": "concurrency-a",
                        "retry_class": "transient",
                        "effect_safety": "idempotent",
                        "merge_gate": None,
                    }
                ],
                "dependency_edges": [],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    plan = AdmissionPlan.load(plan_path)
    policy = RepositoryPolicy.load(policy_path)
    artifact = AdmissionEvaluator.evaluate(plan, policy)
    return AdmissionCase(
        plan_path=plan_path,
        policy_path=policy_path,
        plan=plan,
        policy=policy,
        outcome=artifact.outcome,
        machine=artifact.machine(),
    )


def build_decision_case(root: FloatiRoot) -> DecisionCase:
    """Build one source-proven proposal and accepted capsule through the ruled public boundary."""

    trace = build_success_trace(root)
    register = DecisionRegister(root, "owner/repo")
    proposal = register.propose(
        timestamp=NOW,
        scope={"kind": "repository"},
        statement="Physical append order remains authoritative.",
        decided_by=public_ids.reviewer(),
        author_authority="worker",
        source_artifact_ids=["run:" + trace.run_id],
    )
    record = dict(proposal)
    record.update(
        {
            "id": "decision-record-" + uuid7_hex(),
            "timestamp": NOW,
            "status": "accepted",
            "author_authority": "architect",
            "decided_by": "architect",
        }
    )
    record["decision_digest"] = decision_digest(record)
    accepted = register.append(record)
    return DecisionCase(
        register=register,
        proposal=proposal,
        record=accepted,
        capsule=register.capsule(),
    )


def _observation(trace: Trace, projection: RunProjection) -> CanonicalObservation:
    run = projection.run(trace.run_id)
    contracts = tuple(
        sorted(
            (
                item_id,
                tuple(value["history_ids"]),
                str(value["contract_digest"]),
            )
            for item_id, value in run["contracts"].items()
        )
    )
    attempts = tuple(
        sorted(
            (
                item_id,
                str(run["attempts"][attempt_ids[-1]]["opened"]["attempt_id"]),
                str(run["attempts"][attempt_ids[-1]]["opened"]["fence_token"]),
            )
            for item_id, attempt_ids in run["item_attempt_ids"].items()
            if attempt_ids
        )
    )
    harness = tuple(
        sorted(
            (
                attempt_id,
                tuple(
                    str(segment["harness_session_id"])
                    for record in records
                    for segment in record["harness_segments"]
                ),
            )
            for attempt_id, records in run["harness_sessions"].items()
        )
    )
    return CanonicalObservation(
        item_outcomes=tuple(sorted(projection.item_outcomes(trace.run_id).items())),
        run_outcome=projection.run_outcome(trace.run_id),
        contract_history=contracts,
        current_attempts=attempts,
        cancellation_ids=tuple(sorted(run["cancellations"])),
        stale_evidence_ids=tuple(sorted(run["stale_evidence"])),
        stale_adoption_ids=tuple(sorted(run["stale_adoptions"])),
        harness_sessions=harness,
        orphan_receipt_ids=tuple(sorted(run["orphaned"])),
    )


def canonical_observation_from_records(
    trace: Trace, records: Sequence[Dict[str, object]]
) -> CanonicalObservation:
    """Project supplied physical frames with the trace's immutable raw receipt evidence."""

    projection = RunProjection.from_records(
        records, WorkerReceipts(trace.root).records(), integrity=True
    )
    return _observation(trace, projection)


def assert_physical_projection(trace: Trace) -> CanonicalObservation:
    """Return a timestamp-free, physically ordered run observation for equality tests."""

    return _observation(trace, trace.ledger.project())

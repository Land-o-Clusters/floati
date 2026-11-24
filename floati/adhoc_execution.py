"""Construct real admitted native execution from an existing human launch grant."""
from __future__ import annotations

from .adhoc_journal import AdhocJournal
from .adhoc_launch import BUDGET, CAPABILITY, ROUTING_RANK, WORKER_PROFILE, require_adhoc_launch
from .adhoc_profiles import current_time, timestamp
from .adhoc_records import refuse
from .admission import AdmissionBinder, AdmissionPlan
from .approvals import ApprovalLedger
from .bus_epoch import epoch_guard
from .capabilities import CapabilityGrantLedger
from .capability_binding import CapabilityBinder
from .contracts import TaskContract, contract_digest
from .effects import EffectController, EffectLedger
from .ids import uuid7_hex
from .runtruth import RunLedger
from .scheduler import RetryPolicy, RunScheduler
from .spawn_groups import SpawnGroupController
from .worker_bootstrap_protocol import BuiltInAdapterSpec
from .workers import WorkerRunner


def _contract(profile, admission):
    return TaskContract.create(objective="Execute the exact authenticated native dispatch",
        non_goals=["No authority beyond the authenticated dispatch"],
        areas_to_avoid=[{"path": ".git", "region": "all"}],
        input_hashes={"dispatch": admission["dispatch_digest"], "profile": profile["digest"]},
        acceptance_checks={"parent_result": "Exact parent result disposition"},
        constraints={"workspace": profile["workspace"], "subagents": "disabled"}, risk_class="low",
        retry_policy={"max_attempts": 1, "backoff": {"base_delay_ms": 0, "cap_delay_ms": 0, "strategy": "fixed"}}, dependencies=[])


def _plan(profile, admission, contract):
    return AdmissionPlan.from_canonical({"schema_version": 0,
        "workers": [{"node_id": profile["node_id"], "worker_profile": WORKER_PROFILE}],
        "max_active_attempts": 1, "budget_reservations": [{"budget_id": BUDGET, "amount": 1}],
        "items": [{"item_id": admission["dispatch"]["work_item_id"], "contract": contract.canonical(),
            "capability_selector": CAPABILITY, "requires_cancellation": True, "requires_callback": True,
            "workspace_key": profile["id"], "concurrency_key": profile["id"], "retry_class": "operator_required",
            "effect_safety": "idempotent", "merge_gate": None}], "dependency_edges": []})


def _ordinary_once(ledger, run_id, current, kind, prefix, **fields):
    prior = [row for row in ledger.records() if row["run_id"] == run_id and row["kind"] == kind]
    if prior:
        if len(prior) != 1 or any(prior[0].get(key) != value for key, value in fields.items()):
            refuse("effect_launch_binding_invalid", "saved native run differs from its authenticated dispatch")
        return prior[0]
    return ledger.append({"schema_version": 0, "id": prefix + uuid7_hex(),
        "tenant_id": ledger.root.tenant_id, "timestamp": timestamp(current), "kind": kind,
        "run_id": run_id, **fields})


def _prepare_binding(root, profile, admission, policy, grant, current):
    ledger = RunLedger(root)
    contract = _contract(profile, admission)
    plan = _plan(profile, admission, contract)
    item_id = admission["dispatch"]["work_item_id"]
    run_id = "run-" + admission["dispatch_id"].removeprefix("adhoc-dispatch-")
    _ordinary_once(ledger, run_id, current, "run_created", "run-created-", plan_digest=plan.digest,
        policy_digest=policy.digest, item_ids=[item_id], dependency_edges=[])
    _ordinary_once(ledger, run_id, current, "task_contract", "task-contract-", item_id=item_id,
        **contract.canonical(), contract_digest=contract_digest(contract))
    _ordinary_once(ledger, run_id, current, "run_policy_bound", "run-policy-bound-", policy_digest=policy.digest)
    _ordinary_once(ledger, run_id, current, "worker_pool_bound", "run-worker-pool-bound-", worker_ids=[profile["node_id"]])
    run = ledger.project().run(run_id)
    if run["admission_binding"]["status"] != "bound":
        AdmissionBinder.bind(ledger, run_id, plan, policy, now=current)
    scheduler = RunScheduler(ledger)
    opened = scheduler.open_attempt(run_id, item_id, RetryPolicy(1, 0, 0, strategy="fixed"), 1, now=current)
    attempt_id = opened["attempt_id"]
    binder = CapabilityBinder(ledger, CapabilityGrantLedger(root))
    run = ledger.project().run(run_id)
    snapshot = run["capability_sets"].get(attempt_id)
    if snapshot is None:
        snapshot = binder.bind(run_id, item_id, attempt_id, profile["node_id"],
            WORKER_PROFILE, policy, ROUTING_RANK, now=current)
    if (snapshot["chosen_worker"] != profile["node_id"] or snapshot["policy_digest"] != policy.digest
        or snapshot["routing_rank"] != ROUTING_RANK or grant["id"] not in {
            row["grant_id"] for row in snapshot["effective_grants"] if row["capability_name"] == CAPABILITY}):
        refuse("effect_launch_binding_invalid", "saved native attempt does not bind its exact human launch grant")
    spawn_policy = ledger.project().run(run_id)["attempt_spawn_policy"].get(attempt_id)
    expected_spawn = {"adapter": profile["adapter"], "subagents_mode": "disabled", "max_children": 0,
        "max_depth": 0, "child_capability_ceiling": [], "spawn_budget_ceiling": [], "workspace_policies": []}
    if spawn_policy is None:
        SpawnGroupController(ledger, policy).bind_attempt_policy(run_id, item_id, attempt_id, snapshot["id"],
            **expected_spawn, now=current)
    elif any(spawn_policy.get(key) != value for key, value in expected_spawn.items()):
        refuse("effect_launch_binding_invalid", "saved native spawn policy differs from its disabled child boundary")
    run = ledger.project().run(run_id)
    dispatch = run["dispatches"].get(attempt_id)
    if dispatch is None:
        dispatch = binder.dispatch(snapshot["id"], [profile["node_id"]], "policy.route", policy, now=current)
    if (dispatch["chosen_worker"] != profile["node_id"] or dispatch.get("adapter") != profile["adapter"]
        or dispatch["capability_set_bound_id"] != snapshot["id"]):
        refuse("effect_launch_binding_invalid", "saved native dispatch differs from its profile and capability snapshot")
    scheduler.start_attempt(run_id, item_id, attempt_id, dispatch["id"], now=current)
    ledger.project().effect_intent_context(run_id, item_id, attempt_id, opened["fence_token"])
    return ledger, {"run_id": run_id, "item_id": item_id, "attempt_id": attempt_id}


def authorize_adhoc_capability(root, run, item_id, chosen_worker, worker_profile, routing_rank, policy, current):
    """The shared capability owner admits only this exact human-approved action."""
    from .adhoc_profiles import AdhocProfiles
    from .errors import ProtocolRefusal
    try:
        dispatch_id = "adhoc-dispatch-" + run["run_id"].removeprefix("run-")
        admission = AdhocJournal(root).show(dispatch_id)
        profile = AdhocProfiles(root).get(admission["profile_id"])
        contract = _contract(profile, admission)
        if (chosen_worker != profile["node_id"] or worker_profile != WORKER_PROFILE
            or routing_rank != ROUTING_RANK or item_id != admission["dispatch"]["work_item_id"]
            or run["contracts"][item_id]["contract"].canonical() != contract.canonical()
            or run["plan_digest"] != _plan(profile, admission, contract).digest):
            raise ProtocolRefusal("effect_launch_binding_invalid", "native launch contract differs from its admitted profile")
        launch = require_adhoc_launch(root, profile, admission, now=current)
        if launch["policy"].digest != policy.digest:
            raise ProtocolRefusal("effect_launch_binding_invalid", "native launch policy differs from its human approval")
        return launch["launch_grant"]["id"]
    except ProtocolRefusal as exc:
        raise ProtocolRefusal("capability_selector_unsatisfied",
            "native launch requires the exact admitted human-approved profile action",
            remedy="use the authenticated worker adhoc run path for the approved profile") from exc


def prepare_adhoc_execution(root, profile, admission, clock):
    """Prepare or replay one actual attempt; never manufacture human approval."""
    current = current_time(clock())
    with epoch_guard(root, exclusive=True):
        launch = require_adhoc_launch(root, profile, admission, now=current)
        if AdhocJournal(root).show(admission["dispatch_id"]) != admission:
            refuse("adhoc_dispatch_authentication_failed", "native preparation requires its exact durable admission")
        if admission["profile_id"] != profile["id"] or admission["profile_digest"] != profile["digest"]:
            refuse("adhoc_dispatch_authentication_failed", "native preparation profile differs from its exact admission")
        policy = launch["policy"]
        ledger, binding = _prepare_binding(root, profile, admission, policy, launch["launch_grant"], current)
        effects = EffectController(EffectLedger(root), ledger, policy, ApprovalLedger(root))
    from .adapters.codex_live import _DEFAULT_COMMAND as codex
    from .adapters.claude import _DEFAULT_COMMAND as claude
    from .adapters.pi import _DEFAULT_COMMAND as pi
    command = {"codex": codex, "claude": claude, "pi": pi}[profile["adapter"]]
    return WorkerRunner(root, {}, clock=clock, effect_controller=effects,
        effect_adapter_specs={profile["adapter"]: BuiltInAdapterSpec(profile["adapter"], tuple(command), "borrowed")}), binding

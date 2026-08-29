"""Canonical, append-only run truth for bounded local runs."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .contracts import TaskContract, contract_digest
from .records import SPAWN_GROUP_KINDS, TASK3_CANCELLATION_KINDS, validate_record
from .root import FloatiRoot
from .run_segments import RunStoreSnapshot, SegmentConfig, SegmentedRunStore
from .workers import WorkerReceipts


ATTEMPT_KINDS = frozenset({
    "attempt_opened", "attempt_started", "attempt_terminal", "retry_scheduled", "retry_exhausted",
})
CANCELLATION_KINDS = frozenset({
    "cancel_requested", "cancel_scope_resolved", "cancel_observed", "cancel_signal_sent",
    "cancel_terminal", "cancel_unconfirmed", "stale_attempt_evidence",
    "stale_evidence_adopted", "attempt_harness_session_bound",
}) | TASK3_CANCELLATION_KINDS
SUPERVISOR_KINDS = frozenset({"supervisor_orphaned"})
SUSPENSION_KINDS = frozenset({
    "attempt_suspended_for_approval",
    "approval_consumed_for_resume",
})
CAPABILITY_BINDING_KINDS = frozenset({"capability_set_bound"})
ADMISSION_BINDING_KINDS = frozenset({"run_admission_bound"})
SPAWN_RUN_KINDS = SPAWN_GROUP_KINDS | frozenset({"plan_amendment"})
LEGACY_RUN_KINDS = frozenset({
    "run_created", "task_contract", "plan_amendment", "run_policy_bound", "worker_pool_bound", "dispatch_decision",
    "result_produced", "result_verified", "acceptance_receipt", "result_accepted", "run_terminal",
}) | ATTEMPT_KINDS | CANCELLATION_KINDS | SUPERVISOR_KINDS | CAPABILITY_BINDING_KINDS | ADMISSION_BINDING_KINDS
POST_V1_RUN_KINDS = CAPABILITY_BINDING_KINDS | ADMISSION_BINDING_KINDS | SUSPENSION_KINDS
LEGACY_RUN_KINDS = LEGACY_RUN_KINDS - POST_V1_RUN_KINDS
RUN_KINDS = LEGACY_RUN_KINDS | POST_V1_RUN_KINDS | SPAWN_GROUP_KINDS
ITEM_OUTCOMES = frozenset({"succeeded", "failed", "cancelled", "skipped", "needs_operator", "uncertain"})
RUN_OUTCOMES = ITEM_OUTCOMES | frozenset({"partially_succeeded"})
FAILURE_POLICIES = frozenset({"fail_run", "skip_dependent", "continue"})
_EFFECT_ACCEPTANCE_FENCE_KINDS = frozenset({
    "result_accepted", "attempt_terminal", "attempt_suspended_for_approval",
    "run_terminal",
})


def _detach_run_record(raw: object) -> Dict[str, object]:
    """Detach one candidate without invoking caller container/copy hooks."""

    if type(raw) is not dict:
        raise ProtocolRefusal(
            "record_not_object", "each durable record must be an exact object"
        )

    def detach(value: object) -> object:
        if value is None or type(value) in {str, int, float, bool}:
            return value
        if type(value) is list or type(value) is tuple:
            return [detach(member) for member in value]
        if type(value) is dict:
            detached: Dict[str, object] = {}
            for key, member in value.items():
                if type(key) is not str:
                    raise ProtocolRefusal(
                        "record_value_invalid",
                        "durable record object keys must be exact strings",
                    )
                detached[key] = detach(member)
            return detached
        raise ProtocolRefusal(
            "record_value_invalid",
            "durable record values must use exact JSON containers and scalars",
        )

    detached_record: Dict[str, object] = {}
    for key, value in raw.items():
        if type(key) is not str:
            raise ProtocolRefusal(
                "record_value_invalid",
                "durable record object keys must be exact strings",
            )
        detached_record[key] = detach(value)
    return detached_record


@contextmanager
def effect_acceptance_guard(root: FloatiRoot, *, exclusive: bool = True):
    """Serialize effect intent against Run transitions without storing truth."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal(
            "root_required", "effect acceptance coordination requires FloatiRoot"
        )
    from .jsonl import _locked_path

    lock_path = root.resolve_relative(Path("effects/acceptance.lock"))
    # Complete governed traces briefly serialize several cross-ledger fences.
    # Keep this coordination wait bounded, but do not subject lawful queued
    # fences to the shorter generic single-record ledger lock budget.
    with _locked_path(lock_path, exclusive=exclusive, timeout_seconds=5.0):
        yield


def attempt_fence_token(run_id: str, item_id: str, ordinal: int, scheduler_epoch: int) -> str:
    payload = "\0".join(("slipway-attempt-fence-v0", run_id, item_id, str(ordinal), str(scheduler_epoch)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def retry_delay_from_backoff(run_id: str, item_id: str, ordinal: int, backoff: Dict[str, object]) -> int:
    base, cap = int(backoff["base_delay_ms"]), int(backoff["cap_delay_ms"])
    ceiling = base if backoff["strategy"] == "fixed" else min(cap, base * (2 ** max(0, ordinal - 2)))
    if ceiling == 0:
        return 0
    payload = "\0".join(("slipway-retry-jitter-v0", run_id, item_id, str(ordinal)))
    jitter = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big") % (ceiling // 4 + 1)
    return min(cap, ceiling + jitter)


def _task_contract_from_record(record: Dict[str, object]) -> TaskContract:
    return TaskContract.create(
        objective=record["objective"], non_goals=record["non_goals"], areas_to_avoid=record["areas_to_avoid"],
        input_hashes=record["input_hashes"], acceptance_checks=record["acceptance_checks"],
        constraints=record["constraints"], risk_class=record["risk_class"],
        retry_policy=record["retry_policy"], dependencies=record["dependencies"],
    )


def _contract_retry_matches(contract: TaskContract, max_attempts: object, backoff: object) -> bool:
    retry = contract.canonical()["retry_policy"]
    governed = retry["backoff"]
    return (
        max_attempts == retry["max_attempts"] and isinstance(backoff, dict)
        and backoff.get("base_delay_ms") == governed["base_delay_ms"]
        and backoff.get("cap_delay_ms") == governed["cap_delay_ms"]
        and backoff.get("strategy") == governed["strategy"]
        and backoff.get("jitter") == "sha256_25pct"
    )


def _spawn_admission_digest(previous: str, plan: object) -> str:
    canonical = plan.canonical()
    payload = {
        "previous_admission_digest": previous,
        "workers": canonical["workers"],
        "max_active_attempts": canonical["max_active_attempts"],
        "budget_reservations": canonical["budget_reservations"],
        "items": canonical["items"],
    }
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _spawn_child_admission_item(child: Dict[str, object]) -> Dict[str, object]:
    return {
        "item_id": child["item_id"],
        "contract": child["task_contract"],
        "capability_selector": child["capability_selector"],
        "requires_cancellation": child["requires_cancellation"],
        "requires_callback": child["requires_callback"],
        "workspace_key": child["workspace_key"],
        "concurrency_key": child["concurrency_key"],
        "retry_class": child["retry_class"],
        "effect_safety": child["effect_safety"],
        "merge_gate": child["merge_gate"],
    }


def _spawn_parent_cancel_requested(
    run: Dict[str, object], parent_item_id: object,
    dependency_edges: Optional[Sequence["DependencyEdge"]] = None,
) -> bool:
    """Whether a physically prior cancellation request covers this parent."""

    for cancellation in run["cancellations"].values():
        request = cancellation["requested"]
        if request["scope"] == "exact_items":
            if parent_item_id in request["item_ids"]:
                return True
            continue
        if request["scope"] == "run" or request["item_id"] == parent_item_id:
            return True
        if dependency_edges is not None and parent_item_id in _cancel_closure(
            run["item_ids"], dependency_edges, request["item_id"]
        ):
            return True
        resolved = cancellation["resolved"]
        if resolved is not None and parent_item_id in resolved["item_ids"]:
            return True
    return False


def _cancel_request_covers_item(
    run: Dict[str, object], item_id: str,
    dependency_edges: Sequence["DependencyEdge"],
) -> bool:
    """The durable request itself fences every later launch transition."""

    for cancellation in run["cancellations"].values():
        request = cancellation["requested"]
        if request["scope"] == "exact_items":
            if item_id in request["item_ids"]:
                return True
        elif item_id in _cancel_scope_items(run, dependency_edges, request["item_id"]):
            return True
    return False


def _spawn_join_decision(
    run: Dict[str, object], group: Dict[str, object],
) -> Optional[str]:
    """Return the first irreversible member-join class in physical order."""
    created = group["created"]
    members = list(group["member_item_ids"])
    positions = {
        record["id"]: index
        for index, record in enumerate(run["records"], start=1)
    }
    events: list[tuple[int, str, str]] = []
    for item_id in members:
        accepted = run["accepted"].get(item_id)
        if accepted is not None:
            events.append((positions[accepted["id"]], item_id, "accepted"))
            continue
        rejected = group["rejections"].get(item_id)
        if rejected is not None:
            state = (
                "failed" if created["on_child_failure"] == "fail_group"
                else "skipped"
            )
            events.append((positions[rejected["id"]], item_id, state))
            continue
        projected_outcome = run["spawn_item_outcomes"].get(item_id)
        cancellation_record = run.get("spawn_item_outcome_records", {}).get(item_id)
        if projected_outcome == "cancelled" and cancellation_record is not None:
            events.append((positions[cancellation_record["id"]], item_id, "cancelled"))
            continue
        attempt_ids = run["item_attempt_ids"].get(item_id, [])
        if not attempt_ids:
            continue
        terminal = run["attempts"][attempt_ids[-1]]["terminal"]
        if terminal is None or terminal["retry_disposition"] == "scheduled":
            continue
        if (
            terminal["terminal_state"] == "uncertain"
            or terminal["effect_safety"] == "unknown_effect"
            or terminal["policy_class"] in {"operator_required", "unknown_effect"}
        ):
            state = "needs_operator"
        elif terminal["terminal_state"] == "cancelled":
            state = "cancelled"
        elif terminal["policy_class"] == "policy_refusal":
            state = "skipped"
        else:
            state = "failed"
        events.append((positions[terminal["id"]], item_id, state))

    live = set(members)
    accepted_count = 0
    mode = created["join_mode"]
    required = int(created["required_count"] or len(members))
    fail_fast = created["on_child_failure"] == "fail_group"
    for _position, item_id, state in sorted(events):
        live.remove(item_id)
        if state == "accepted":
            accepted_count += 1
        elif state == "needs_operator":
            return "needs_operator"
        elif fail_fast:
            return "failed"

        if mode == "all_accepted" and accepted_count == len(members):
            return "satisfied"
        if mode == "all_terminal" and (
            (fail_fast and accepted_count == len(members))
            or (not fail_fast and not live)
        ):
            return "satisfied"
        if mode == "quorum" and accepted_count >= required:
            return "satisfied"
        if mode == "first_accepted" and accepted_count:
            return "satisfied"

        if fail_fast:
            continue
        if mode == "all_accepted" and state != "accepted":
            return "failed"
        if mode == "quorum" and accepted_count + len(live) < required:
            return "failed"
        if mode == "first_accepted" and not live:
            return "failed"
    return None


def _spawn_remaining_at_record(
    run: Dict[str, object], group: Dict[str, object], record_id: str,
) -> list[str]:
    """Derive the immutable live member set immediately before one record."""

    positions = {
        record["id"]: index
        for index, record in enumerate(run["records"])
    }
    boundary = positions.get(record_id)
    if boundary is None:
        return []
    remaining: list[str] = []
    for item_id in group["member_item_ids"]:
        final_ids: list[str] = []
        accepted = run["accepted"].get(item_id)
        rejected = group["rejections"].get(item_id)
        cancellation = run.get("spawn_item_outcome_records", {}).get(item_id)
        if accepted is not None:
            final_ids.append(accepted["id"])
        if rejected is not None:
            final_ids.append(rejected["id"])
        if cancellation is not None:
            final_ids.append(cancellation["id"])
        for attempt_id in run["item_attempt_ids"].get(item_id, []):
            terminal = run["attempts"][attempt_id]["terminal"]
            if terminal is not None:
                final_ids.append(terminal["id"])
        if not any(positions.get(final_id, boundary) < boundary for final_id in final_ids):
            remaining.append(item_id)
    return sorted(remaining)


@dataclass(frozen=True)
class DependencyEdge:
    source: str
    target: str
    requires: str = "accepted"
    failure_policy: str = "fail_run"

    def __post_init__(self) -> None:
        if self.requires not in {"produced", "verified", "accepted"}:
            raise ProtocolRefusal("requires_invalid", "dependency requires is produced, verified, or accepted")
        if self.failure_policy not in FAILURE_POLICIES:
            raise ProtocolRefusal("failure_policy_invalid", "dependency failure policy is fail_run, skip_dependent, or continue")


class RunProjection:
    def __init__(self, runs: Dict[str, Dict[str, object]], edges: Dict[str, list[DependencyEdge]], *, worker_receipts: Sequence[Dict[str, object]] = (), effect_projection: object = None, track_record_ids: bool = True) -> None:
        self._runs = runs
        self._edges = edges
        self._receipts = {str(row["id"]): row for row in worker_receipts}
        self._effect_projection = effect_projection
        self._harness_segment_positions: Dict[str, Dict[str, Dict[str, tuple[int, int]]]] = {}
        self._harness_segment_attempts: Dict[str, Dict[str, set[str]]] = {}
        self._workspace_reservations: Dict[str, tuple[str, str, str]] = {}
        self._seen_ids: set[str] = set()
        self._track_record_ids = track_record_ids
        self._last_position = 0

    @classmethod
    def empty(cls, worker_receipts: Sequence[Dict[str, object]] = (), *, effect_projection: object = None, track_record_ids: bool = True) -> "RunProjection":
        return cls({}, {}, worker_receipts=worker_receipts, effect_projection=effect_projection, track_record_ids=track_record_ids)

    @classmethod
    def from_records(cls, records: Sequence[Dict[str, object]], worker_receipts: Sequence[Dict[str, object]] = (), *, effect_projection: object = None, integrity: bool = True) -> "RunProjection":
        projection = cls.empty(worker_receipts, effect_projection=effect_projection)
        for physical_position, raw in enumerate(records, start=1):
            projection.apply(raw, physical_position=physical_position, integrity=integrity)
        return projection

    def _effect_evidence(
        self, run_id: str, attempt_id: str, *, high_watermark: object = None,
    ) -> object:
        from .effects import EffectProjection

        effects = self._effect_projection
        if effects is None:
            return None
        if not isinstance(effects, EffectProjection):
            raise ProtocolRefusal(
                "effect_evidence_invalid",
                "run projection requires one immutable Effect snapshot",
            )
        if high_watermark is None:
            selected = effects
        else:
            if (
                not isinstance(high_watermark, int)
                or isinstance(high_watermark, bool)
                or not 1 <= high_watermark <= len(effects._records)
            ):
                raise ProtocolRefusal(
                    "effect_evidence_invalid",
                    "accepted effect high watermark is outside durable truth",
                )
            selected = EffectProjection.from_records(
                effects._records[:high_watermark], integrity=True
            )
        return selected.acceptance_evidence(run_id, attempt_id)

    def _canonical_acceptance_retry(
        self, record: Dict[str, object],
    ) -> Optional[Dict[str, object]]:
        run = self.run(str(record["run_id"]))
        prior = run["accepted"].get(record["item_id"])
        if (
            prior is None
            or prior["attempt_id"] != record["attempt_id"]
            or {
                key: value for key, value in prior.items()
                if key not in {"id", "timestamp"}
            }
            != {
                key: value for key, value in record.items()
                if key not in {"id", "timestamp"}
            }
        ):
            return None
        if prior["schema_version"] == 1:
            effects = self._effect_evidence(
                str(record["run_id"]), str(record["attempt_id"]),
                high_watermark=prior["effect_ledger_high_watermark"],
            )
            if (
                effects is None
                or effects.blockers
                or list(effects.operation_ids) != prior["effect_operation_ids"]
                or effects.evidence_digest != prior["effect_evidence_digest"]
            ):
                raise ProtocolRefusal(
                    "effect_evidence_overtaken",
                    "effect-bound acceptance retry requires its exact accepted prefix",
                )
            self._require_no_post_acceptance_intents(
                str(record["run_id"]), prior, self._raise_protocol,
            )
        return deepcopy(prior)

    @staticmethod
    def _raise_protocol(code: str, detail: str) -> None:
        raise ProtocolRefusal(code, detail)

    def _require_no_post_acceptance_intents(
        self, run_id: str, acceptance: Dict[str, object],
        refuse: Callable[[str, str], None],
    ) -> None:
        effects = self._effect_projection
        if effects is None:
            refuse(
                "effect_evidence_invalid",
                "accepted effect prefix requires one immutable Effect snapshot",
            )
        watermark = (
            0 if acceptance["schema_version"] == 0
            else acceptance["effect_ledger_high_watermark"]
        )
        later = effects.post_watermark_intent_ids(
            run_id, str(acceptance["attempt_id"]), watermark,
        )
        if later:
            refuse(
                "effect_evidence_overtaken",
                "accepted attempt has a later physical effect intent",
            )

    def _require_current_acceptance_effects(
        self, run_id: str, acceptance: Dict[str, object], refuse: Callable[[str, str], None],
    ) -> None:
        try:
            effects = self._effect_evidence(
                run_id, str(acceptance["attempt_id"]),
                high_watermark=(
                    None
                    if acceptance["schema_version"] == 0
                    else acceptance["effect_ledger_high_watermark"]
                ),
            )
        except (ProtocolRefusal, IntegrityFailure) as exc:
            refuse(exc.code, exc.detail)
        if effects is None:
            refuse(
                "effect_evidence_invalid",
                "successful terminal requires one current Effect snapshot",
            )
        if acceptance["schema_version"] == 0:
            if effects.operation_ids:
                refuse(
                    "effect_evidence_overtaken",
                    "legacy acceptance no longer matches current Effect truth",
                )
            return
        if (
            effects.blockers
            or list(effects.operation_ids) != acceptance["effect_operation_ids"]
            or effects.evidence_digest != acceptance["effect_evidence_digest"]
        ):
            refuse(
                "effect_evidence_overtaken",
                "successful terminal requires exact current accepted Effect evidence",
            )
        self._require_no_post_acceptance_intents(run_id, acceptance, refuse)

    def apply(self, raw: Dict[str, object], *, physical_position: int, integrity: bool, retain_record: bool = True, require_current_effect_binding: bool = False) -> None:
        error = IntegrityFailure if integrity else ProtocolRefusal

        def refuse(code: str, detail: str) -> None:
            raise error(code, detail)

        if not isinstance(physical_position, int) or isinstance(physical_position, bool) or physical_position != self._last_position + 1:
            refuse("projection_position_invalid", "physical position must be the next positive contiguous position")
        record_position = physical_position
        receipts = self._receipts
        runs = self._runs
        edges = self._edges
        harness_segment_positions = self._harness_segment_positions
        harness_segment_attempts = self._harness_segment_attempts
        record = validate_record(raw, str(raw.get("tenant_id")), RUN_KINDS, integrity=integrity)
        if self._track_record_ids:
            if record["id"] in self._seen_ids:
                refuse("duplicate_record_id", "run ledger repeats record id")
            self._seen_ids.add(record["id"])
        kind, run_id = str(record["kind"]), str(record["run_id"])
        run = runs.get(run_id)
        if kind == "run_created":
            if run is not None:
                refuse("run_duplicate", "run_created already exists")
            canonical_edges = [DependencyEdge(
                str(edge["source"]), str(edge["target"]), str(edge.get("requires", "accepted")),
                str(edge.get("failure_policy", "fail_run")),
            ) for edge in record["dependency_edges"]]
            admitted_pair_proof = {
                "status": "unavailable",
                "reason_code": "admitted_pair_proof_unavailable",
            }
            admission_binding = {
                "status": "unavailable",
                "reason_code": "run_admission_binding_unavailable",
            }
            if "policy_digest" in record:
                admitted_pair_proof = {
                    "status": "pending",
                    "plan_digest": record["plan_digest"],
                    "policy_digest": record["policy_digest"],
                }
                admission_binding = {
                    "status": "pending",
                    "plan_digest": record["plan_digest"],
                    "policy_digest": record["policy_digest"],
                }
            run = {
                "run_id": run_id, "item_ids": list(record["item_ids"]), "plan_digest": record["plan_digest"],
                "admitted_pair_proof": admitted_pair_proof,
                "admission_binding": admission_binding,
                "policy": None, "pool": None, "capability_sets": {}, "capability_set_consumers": {},
                "dispatches": {}, "produced": {}, "verified": {},
                "accepted": {}, "acceptance_receipts": {}, "contracts": {}, "attempts": {}, "item_attempt_ids": {}, "cancellations": {},
                "stale_evidence": {}, "stale_adoptions": {}, "harness_sessions": {},
                "orphaned": {}, "orphaned_by_attempt": {},
                "spawn_admission": None, "attempt_spawn_policy": {},
                "spawn_groups": {}, "spawn_group_by_parent_key": {},
                "spawn_child_group": {}, "spawn_item_depth": {item_id: 0 for item_id in record["item_ids"]},
                "spawn_item_outcomes": {}, "spawn_item_outcome_records": {},
                "untracked_descendants": {},
                "descendant_observation_close": {}, "late_result_dispositions": {},
                "terminal": None, "records": [],
            }
            runs[run_id] = run
            edges[run_id] = canonical_edges
            harness_segment_positions[run_id] = {}
            harness_segment_attempts[run_id] = {}
        elif run is None:
            refuse("run_missing", "a run record must follow run_created")
        if run["terminal"] is not None and kind != "run_created":
            refuse("run_terminal_closed", "no records follow run_terminal")
        if kind == "run_created":
            pass
        elif kind == "task_contract":
            if record["item_id"] not in run["item_ids"] or record["item_id"] in run["contracts"]:
                refuse("task_contract_invalid", "a task contract must bind one unbound run item")
            contract = _task_contract_from_record(record)
            if record["contract_digest"] != contract_digest(contract):
                refuse("task_contract_digest_invalid", "task contract digest must match its canonical governed fields")
            contract_state = {
                "task_contract_id": record["id"], "contract": contract, "contract_digest": record["contract_digest"],
                "history_ids": [record["id"]],
            }
            if "repository" in record:
                contract_state["repository"] = record["repository"]
            run["contracts"][record["item_id"]] = contract_state
        elif kind == "plan_amendment":
            if record["schema_version"] == 0:
                if run["admission_binding"]["status"] == "bound":
                    refuse("task_contract_frozen", "task contracts freeze at durable admission binding")
                current = run["contracts"].get(record["item_id"])
                if current is None or record["task_contract_id"] != current["task_contract_id"] or record["previous_digest"] != current["contract_digest"]:
                    refuse("plan_amendment_invalid", "amendment must name this item's current durable contract and digest")
                if run["item_attempt_ids"].get(record["item_id"]):
                    refuse("task_contract_frozen", "a task contract freezes before its first attempt opens")
                canonical = current["contract"].canonical()
                canonical.update(record["replacement_fields"])
                replacement = TaskContract.create(**canonical)
                if record["contract_digest"] != contract_digest(replacement):
                    refuse("plan_amendment_digest_invalid", "amendment digest must match its exact replacement fields")
                current["contract"] = replacement
                current["contract_digest"] = record["contract_digest"]
                current["history_ids"].append(record["id"])
            else:
                group = run["spawn_groups"].get(record["spawn_group_id"])
                if group is None or group["state"] != "pending":
                    refuse("spawn_membership_immutable", "spawn amendment requires one pending group")
                if _spawn_parent_cancel_requested(
                    run, record["parent_item_id"], edges[run_id]
                ):
                    refuse(
                        "spawn_parent_cancel_requested",
                        "a prior covering cancellation request fences group activation",
                    )
                if any(record[field] != group["created"][field] for field in (
                    "parent_item_id", "parent_attempt_id", "parent_spawn_policy_id"
                )):
                    refuse("spawn_amendment_binding_invalid", "activation must repeat its pending group binding")
                enabled = run["spawn_admission"]
                if enabled is None:
                    refuse("spawn_admission_disabled", "run has no complete spawn admission preimage")
                if (
                    record["previous_plan_digest"] != run["plan_digest"]
                    or record["previous_admission_digest"] != run["admission_binding"].get("admission_digest")
                    or record["policy_digest"] != run["admission_binding"].get("policy_digest")
                ):
                    refuse("spawn_admission_chain_invalid", "spawn activation must extend the current digest chain")
                children = record["children"]
                child_ids = [child["item_id"] for child in children]
                existing_spawn_children = sum(
                    len(prior["member_item_ids"])
                    for prior in run["spawn_groups"].values()
                    if prior["created"]["parent_attempt_id"] == record["parent_attempt_id"]
                )
                parent_policy = run["attempt_spawn_policy"].get(record["parent_attempt_id"])
                if (
                    parent_policy is None
                    or len(children) > group["created"]["max_children"]
                    or existing_spawn_children + len(children) > parent_policy["max_children"]
                    or len(run["item_ids"]) + len(children) > 64
                ):
                    refuse("spawn_item_limit", "spawn amendment exceeds fixed item or fan-out bounds")
                if set(child_ids) & set(run["item_ids"]):
                    refuse("spawn_membership_immutable", "spawn children must be new run items")
                contract_ids = [child["task_contract_id"] for child in children]
                existing_contract_ids = {
                    contract_id
                    for contract in run["contracts"].values()
                    for contract_id in contract["history_ids"]
                }
                if (
                    len(contract_ids) != len(set(contract_ids))
                    or set(contract_ids) & existing_contract_ids
                ):
                    refuse("spawn_contract_id_duplicate", "spawn child contract IDs must be independent")
                workspace_rank = {"patch_only": 0, "isolated_worktree": 1}
                for child in children:
                    expected_depth = run["spawn_item_depth"].get(record["parent_item_id"], 0) + 1
                    if child["depth"] != expected_depth or child["depth"] > min(16, group["created"]["max_depth"]):
                        refuse("spawn_depth_limit", "child depth exceeds its immutable group bound")
                    if not set(child["capability_ceiling"]) <= set(group["created"]["child_capability_ceiling"]):
                        refuse("spawn_capability_widening", "child capabilities widen the group ceiling")
                    if (
                        child["workspace_policy"] not in parent_policy["workspace_policies"]
                        or workspace_rank[child["workspace_policy"]]
                        > workspace_rank[group["created"]["workspace_policy"]]
                    ):
                        refuse("spawn_workspace_widening", "child workspace policy widens its group or parent")
                group_budget = {row["budget_id"]: row["amount"] for row in group["created"]["aggregate_budget"]}
                allocated: Dict[str, int] = {}
                for child in children:
                    for row in child["budget_allocation"]:
                        allocated[row["budget_id"]] = allocated.get(row["budget_id"], 0) + row["amount"]
                if any(amount > group_budget.get(budget_id, 0) for budget_id, amount in allocated.items()):
                    refuse("spawn_budget_widening", "child allocations exceed aggregate group budget")
                from .admission import AdmissionPlan

                current_plan = deepcopy(enabled["current_plan"])
                current_plan["items"] = sorted(
                    [*current_plan["items"], *[_spawn_child_admission_item(child) for child in children]],
                    key=lambda item: item["item_id"],
                )
                current_plan["dependency_edges"] = sorted(
                    [*current_plan["dependency_edges"], *record["dependency_edges"]],
                    key=lambda edge: (edge["source"], edge["target"], edge["requires"], edge["failure_policy"]),
                )
                try:
                    amended = AdmissionPlan.from_canonical(current_plan)
                except ProtocolRefusal as exc:
                    refuse(exc.code, exc.detail)
                if amended.digest != record["plan_digest"]:
                    refuse("spawn_plan_digest_invalid", "plan digest must cover the complete amended AdmissionPlan")
                expected_admission = _spawn_admission_digest(record["previous_admission_digest"], amended)
                if expected_admission != record["admission_digest"]:
                    refuse("spawn_admission_digest_invalid", "admission digest must cover the chained complete item table")
                all_ids = {item["item_id"] for item in current_plan["items"]}
                if any(edge["source"] not in all_ids or edge["target"] not in all_ids for edge in current_plan["dependency_edges"]):
                    refuse("dependency_edges_invalid", "spawn edge endpoints must exist in the amended graph")
                outgoing: Dict[str, list[str]] = {}
                for edge in current_plan["dependency_edges"]:
                    outgoing.setdefault(str(edge["source"]), []).append(str(edge["target"]))
                visiting: set[str] = set(); visited: set[str] = set()
                def visit(item_id: str) -> None:
                    if item_id in visiting:
                        refuse("dependency_cycle", "spawn amendment must remain acyclic")
                    if item_id in visited:
                        return
                    visiting.add(item_id)
                    for target in outgoing.get(item_id, []):
                        visit(target)
                    visiting.remove(item_id); visited.add(item_id)
                for item_id in sorted(all_ids):
                    visit(item_id)
                for child in children:
                    typed = TaskContract.create(**child["task_contract"])
                    run["contracts"][child["item_id"]] = {
                        "task_contract_id": child["task_contract_id"],
                        "contract": typed,
                        "contract_digest": child["task_contract_digest"],
                        "history_ids": [child["task_contract_id"]],
                    }
                run["item_ids"] = sorted(all_ids)
                run["plan_digest"] = record["plan_digest"]
                run["admission_binding"]["plan_digest"] = record["plan_digest"]
                run["admission_binding"]["admission_digest"] = record["admission_digest"]
                run["admission_binding"]["items"] = [
                    {key: item[key] for key in ("item_id", "workspace_key", "concurrency_key", "capability_selector")}
                    for item in current_plan["items"]
                ]
                run["admitted_pair_proof"]["plan_digest"] = record["plan_digest"]
                enabled["current_plan"] = current_plan
                edges[run_id] = [DependencyEdge(
                    edge["source"], edge["target"], edge["requires"], edge["failure_policy"]
                ) for edge in current_plan["dependency_edges"]]
                group.update({
                    "state": "activated", "amendment": record,
                    "member_item_ids": child_ids, "admissions": {}, "rejections": {},
                    "closed": None, "late_result_ids": [],
                })
                for child_id in child_ids:
                    run["spawn_child_group"][child_id] = record["spawn_group_id"]
                    run["spawn_item_depth"][child_id] = next(
                        child["depth"] for child in children if child["item_id"] == child_id
                    )
        elif kind == "run_policy_bound":
            if run["policy"] is not None:
                refuse("run_policy_duplicate", "policy can bind once")
            if (
                run["admission_binding"]["status"] == "bound"
                and record["policy_digest"] != run["admission_binding"]["policy_digest"]
            ):
                refuse("run_admission_policy_mismatch", "run policy must equal prior admission binding")
            admitted_pair_proof = run["admitted_pair_proof"]
            if admitted_pair_proof["status"] != "unavailable":
                if record["policy_digest"] != admitted_pair_proof["policy_digest"]:
                    refuse("admitted_pair_policy_mismatch", "run policy binding must equal the durable admitted pair")
                run["admitted_pair_proof"] = {
                    "status": "bound",
                    "plan_digest": admitted_pair_proof["plan_digest"],
                    "policy_digest": admitted_pair_proof["policy_digest"],
                }
            run["policy"] = record
        elif kind == "worker_pool_bound":
            if run["pool"] is not None:
                refuse("worker_pool_duplicate", "pool can bind once")
            if (
                run["admission_binding"]["status"] == "bound"
                and record["worker_ids"]
                != [row["node_id"] for row in run["admission_binding"]["workers"]]
            ):
                refuse("run_admission_workers_mismatch", "worker pool must equal prior admission binding")
            run["pool"] = record
        elif kind == "run_admission_bound":
            if run["admission_binding"]["status"] == "bound":
                refuse("run_admission_duplicate", "run admission can bind once")
            if run["attempts"]:
                refuse("run_admission_late", "run admission must physically precede every attempt")
            if record["plan_digest"] != run["plan_digest"]:
                refuse("run_admission_plan_mismatch", "admission plan digest must equal run creation")
            durable_policy_digest = None
            if run["policy"] is not None:
                durable_policy_digest = run["policy"]["policy_digest"]
            elif run["admitted_pair_proof"]["status"] != "unavailable":
                durable_policy_digest = run["admitted_pair_proof"]["policy_digest"]
            if durable_policy_digest is not None and record["policy_digest"] != durable_policy_digest:
                refuse("run_admission_policy_mismatch", "admission policy digest must equal durable run evidence")
            if [row["item_id"] for row in record["items"]] != run["item_ids"]:
                refuse("run_admission_items_mismatch", "admission items must exactly equal run creation")
            if run["pool"] is not None and [row["node_id"] for row in record["workers"]] != run["pool"]["worker_ids"]:
                refuse("run_admission_workers_mismatch", "admission workers must exactly equal the bound pool")
            run["admission_binding"] = {"status": "bound", **record}
        elif kind == "run_spawn_admission_enabled":
            if run["spawn_admission"] is not None or run["attempts"]:
                refuse("spawn_admission_enablement_late", "spawn enablement binds once before every attempt")
            binding = run["admission_binding"]
            if (
                binding["status"] != "bound"
                or record["run_admission_binding_id"] != binding["id"]
                or record["admission_digest"] != binding["admission_digest"]
                or record["policy_digest"] != binding["policy_digest"]
            ):
                refuse("spawn_admission_binding_invalid", "spawn enablement must repeat the current admission binding")
            from .admission import AdmissionPlan

            try:
                plan = AdmissionPlan.from_canonical(record["base_plan"])
            except ProtocolRefusal as exc:
                refuse(exc.code, exc.detail)
            if plan.digest != record["base_plan_digest"] or plan.digest != run["plan_digest"]:
                refuse("spawn_base_plan_digest_invalid", "spawn base plan must exactly equal the run plan digest")
            canonical = plan.canonical()
            if (
                canonical["workers"] != binding["workers"]
                or canonical["max_active_attempts"] != binding["max_active_attempts"]
                or canonical["budget_reservations"] != binding["budget_reservations"]
                or [
                    {key: item[key] for key in ("item_id", "workspace_key", "concurrency_key", "capability_selector")}
                    for item in canonical["items"]
                ] != binding["items"]
                or [item["item_id"] for item in canonical["items"]] != run["item_ids"]
            ):
                refuse("spawn_base_plan_binding_invalid", "base plan must reproduce every current admission table")
            for item in canonical["items"]:
                current = run["contracts"].get(item["item_id"])
                if current is None or current["contract"].canonical() != item["contract"] or current["contract_digest"] != contract_digest(item["contract"]):
                    refuse("spawn_contract_mismatch", "base plan contracts must equal current projected contracts")
            physical_edges = [
                {
                    "source": edge.source,
                    "target": edge.target,
                    "requires": edge.requires,
                    "failure_policy": edge.failure_policy,
                }
                for edge in edges[run_id]
            ]
            if canonical["dependency_edges"] != physical_edges:
                refuse(
                    "spawn_base_plan_edges_mismatch",
                    "spawn enablement dependency edges must equal physical run truth",
                )
            run["spawn_admission"] = {**record, "current_plan": canonical}
        elif kind == "attempt_opened":
            if run["admission_binding"]["status"] == "pending":
                refuse("run_admission_missing", "binding-required run must persist admission before any attempt")
            item_id, attempt_id = record["item_id"], record["attempt_id"]
            if _cancel_request_covers_item(run, item_id, edges[run_id]):
                refuse("cancel_request_fence", "a durable cancellation request fences new attempts")
            if item_id not in run["item_ids"] or attempt_id in run["attempts"]:
                refuse("attempt_open_invalid", "opened attempt must be unique and name a run item")
            if item_id not in run["contracts"]:
                refuse("task_contract_missing", "a task contract must bind before an item attempt opens")
            group_id = run["spawn_child_group"].get(item_id)
            if group_id is not None and item_id not in run["spawn_groups"][group_id]["admissions"]:
                refuse("spawn_child_admission_missing", "group child must be durably admitted before attempt open")
            if not _contract_retry_matches(run["contracts"][item_id]["contract"], record["max_attempts"], record["backoff"]):
                refuse("task_contract_policy_mismatch", "attempt retry policy must equal the frozen task contract")
            if record["fence_token"] != attempt_fence_token(run_id, item_id, record["ordinal"], record["scheduler_epoch"]):
                refuse("attempt_fence_invalid", "opened attempt fence does not match its governed domain")
            prior_ids = run["item_attempt_ids"].get(item_id, [])
            if not prior_ids:
                if record["ordinal"] != 1:
                    refuse("attempt_ordinal_invalid", "first item attempt must use ordinal one")
            else:
                prior = run["attempts"][prior_ids[-1]]
                schedule = prior.get("schedule")
                if schedule is None or record["attempt_id"] != schedule["next_attempt_id"] or record["ordinal"] != schedule["next_ordinal"] or record["scheduler_epoch"] != schedule["scheduler_epoch"] or record["fence_token"] != schedule["next_fence_token"]:
                    refuse("attempt_open_invalid", "later attempt must consume its prior retry reservation")
                if record["max_attempts"] != prior["opened"]["max_attempts"] or record["backoff"] != prior["opened"]["backoff"]:
                    refuse("attempt_policy_invalid", "later attempt must preserve persisted retry policy")
            state = {
                "opened": record,
                "started": None,
                "terminal": None,
                "schedule": None,
                "exhaustion": None,
                "suspension": None,
                "approval_consumption": None,
                "state": "running",
            }
            run["attempts"][attempt_id] = state
            run["item_attempt_ids"].setdefault(item_id, []).append(attempt_id)
        elif kind == "capability_set_bound":
            if run["policy"] is None or run["pool"] is None:
                refuse("run_binding_missing", "capability binding requires policy and worker pool")
            state = run["attempts"].get(record["attempt_id"])
            if state is None:
                refuse("attempt_missing", "capability binding requires a prior opened attempt")
            opened = state["opened"]
            if _cancel_request_covers_item(run, opened["item_id"], edges[run_id]):
                refuse("cancel_request_fence", "a durable cancellation request fences capability binding")
            if (
                record["item_id"] != opened["item_id"]
                or record["fence_token"] != opened["fence_token"]
                or record["policy_digest"] != run["policy"]["policy_digest"]
                or record["chosen_worker"] not in run["pool"]["worker_ids"]
            ):
                refuse("capability_snapshot_binding_invalid", "capability snapshot must match the open attempt, fence, policy, and pool")
            if record["attempt_id"] in run["capability_sets"]:
                refuse("capability_snapshot_duplicate", "an attempt can bind one capability snapshot")
            if (
                record["attempt_id"] in run["dispatches"]
                or state["started"] is not None
                or state["terminal"] is not None
            ):
                refuse("capability_snapshot_late", "capability snapshot must physically precede dispatch and attempt start")
            run["capability_sets"][record["attempt_id"]] = record
        elif kind == "attempt_spawn_policy_bound":
            state = run["attempts"].get(record["parent_attempt_id"])
            capability = run["capability_sets"].get(record["parent_attempt_id"])
            if (
                state is None or capability is None
                or record["parent_item_id"] != state["opened"]["item_id"]
                or record["parent_fence_token"] != state["opened"]["fence_token"]
                or record["parent_capability_set_bound_id"] != capability["id"]
                or record["parent_attempt_id"] in run["dispatches"]
                or state["started"] is not None
            ):
                refuse("spawn_policy_binding_invalid", "spawn policy must follow the exact capability snapshot before dispatch")
            if record["parent_attempt_id"] in run["attempt_spawn_policy"]:
                refuse("spawn_policy_duplicate", "attempt can bind one spawn policy")
            if record["subagents_mode"] != "disabled" and run["spawn_admission"] is None:
                refuse("spawn_admission_disabled", "observed or managed mode requires spawn admission enablement")
            effective = {row["capability_name"] for row in capability["effective_grants"]}
            if not set(record["child_capability_ceiling"]) <= effective:
                refuse("spawn_capability_widening", "spawn policy cannot widen effective capabilities")
            admitted_budget = {
                row["budget_id"]: row["amount"]
                for row in run["admission_binding"].get("budget_reservations", [])
            }
            allocated_budget: Dict[str, int] = {}
            for attempt_id, prior in run["attempt_spawn_policy"].items():
                prior_state = run["attempts"].get(attempt_id)
                if (
                    prior.get("subagents_mode") != "managed"
                    or prior_state is None
                    or prior_state["terminal"] is not None
                ):
                    continue
                for row in prior["spawn_budget_ceiling"]:
                    allocated_budget[row["budget_id"]] = (
                        allocated_budget.get(row["budget_id"], 0) + row["amount"]
                    )
            for row in record["spawn_budget_ceiling"]:
                allocated_budget[row["budget_id"]] = (
                    allocated_budget.get(row["budget_id"], 0) + row["amount"]
                )
            if any(
                amount > admitted_budget.get(budget_id, 0)
                for budget_id, amount in allocated_budget.items()
            ):
                refuse(
                    "spawn_budget_widening",
                    "live attempt spawn ceilings exceed admitted reservations",
                )
            run["attempt_spawn_policy"][record["parent_attempt_id"]] = record
        elif kind == "dispatch_decision":
            if run["policy"] is None or run["pool"] is None:
                refuse("run_binding_missing", "dispatch requires policy and worker pool")
            if (
                run["admission_binding"]["status"] == "bound"
                and record["schema_version"] != 1
            ):
                refuse("dispatch_version_required", "admission-bound runs require binder-owned v1 dispatch")
            if record["item_id"] not in run["item_ids"] or record["policy_digest"] != run["policy"]["policy_digest"]:
                refuse("dispatch_invalid", "dispatch must name a bound item and policy")
            if record["item_id"] not in run["contracts"]:
                refuse("task_contract_missing", "a task contract must bind before dispatch")
            attempt_state = run["attempts"].get(record["attempt_id"])
            if attempt_state is None or attempt_state["opened"]["item_id"] != record["item_id"] or attempt_state["opened"]["scheduler_epoch"] != record["scheduler_epoch"]:
                refuse("attempt_missing", "dispatch requires matching prior opened attempt and epoch")
            if _cancel_request_covers_item(run, record["item_id"], edges[run_id]):
                refuse("cancel_request_fence", "a durable cancellation request fences dispatch")
            pool = run["pool"]["worker_ids"]
            if record["chosen_worker"] not in record["eligible_workers"] or record["chosen_worker"] not in pool or any(worker not in pool for worker in record["eligible_workers"]):
                refuse("worker_pool_mismatch", "dispatch workers must be an eligible subset of bound pool")
            if record["attempt_id"] in run["dispatches"]:
                refuse("attempt_duplicate", "attempt can dispatch once")
            group_id = run["spawn_child_group"].get(record["item_id"])
            if group_id is not None and record["item_id"] not in run["spawn_groups"][group_id]["admissions"]:
                refuse("spawn_child_admission_missing", "group child must be durably admitted before dispatch")
            projected_dispatch = dict(record)
            if record["schema_version"] == 1:
                snapshot = run["capability_sets"].get(record["attempt_id"])
                if snapshot is None or record["capability_set_bound_id"] != snapshot["id"]:
                    refuse("capability_snapshot_missing", "v1 dispatch requires its preceding attempt snapshot")
                if snapshot["id"] in run["capability_set_consumers"]:
                    refuse("capability_snapshot_consumed", "capability snapshot can authorize one dispatch")
                if any(record[field] != snapshot[field] for field in (
                    "item_id", "attempt_id", "chosen_worker", "policy_digest", "routing_rank", "capability_digest",
                )):
                    refuse("capability_dispatch_mismatch", "dispatch must exactly repeat its bound capability snapshot")
                run["capability_set_consumers"][snapshot["id"]] = record["id"]
                projected_dispatch["capability_enforcement"] = "enforced_v1"
            else:
                projected_dispatch["capability_enforcement"] = "legacy_unenforced"
            spawn_policy = run["attempt_spawn_policy"].get(record["attempt_id"])
            if "adapter" in record:
                if spawn_policy is None:
                    refuse("spawn_policy_missing", "spawn-aware dispatch requires its prior policy")
                if (
                    record["attempt_spawn_policy_id"] != spawn_policy["id"]
                    or record["adapter"] != spawn_policy["adapter"]
                ):
                    refuse("spawn_dispatch_mismatch", "spawn-aware dispatch must repeat exact policy and adapter")
            elif spawn_policy is not None and spawn_policy.get("id") is not None:
                refuse(
                    "spawn_policy_missing",
                    "every explicit spawn policy requires spawn-aware dispatch fields",
                )
            if spawn_policy is None:
                spawn_policy = {
                    "id": None, "subagents_mode": "disabled", "adapter": None,
                    "legacy_default": True,
                }
                run["attempt_spawn_policy"][record["attempt_id"]] = spawn_policy
            run["dispatches"][record["attempt_id"]] = projected_dispatch
        elif kind == "attempt_started":
            state = run["attempts"].get(record["attempt_id"])
            dispatch = run["dispatches"].get(record["attempt_id"])
            if state is None or dispatch is None or state["started"] is not None or state["terminal"] is not None:
                refuse("attempt_start_invalid", "attempt can start once after its dispatch")
            if record["item_id"] not in run["contracts"]:
                refuse("task_contract_missing", "a task contract must bind before an attempt starts")
            opened = state["opened"]
            if any(record[field] != opened[field] for field in ("item_id", "ordinal", "fence_token")) or record["attempt_opened_id"] != opened["id"] or record["dispatch_decision_id"] != dispatch["id"]:
                refuse("attempt_start_invalid", "started attempt must exactly name its open and dispatch")
            state["started"] = record
        elif kind == "spawn_group_created":
            state = run["attempts"].get(record["parent_attempt_id"])
            policy = run["attempt_spawn_policy"].get(record["parent_attempt_id"])
            if _spawn_parent_cancel_requested(
                run, record["parent_item_id"], edges[run_id]
            ):
                refuse(
                    "spawn_parent_cancel_requested",
                    "a prior covering cancellation request fences group creation",
                )
            if (
                state is None or state["started"] is None or state["terminal"] is not None
                or record["parent_item_id"] != state["opened"]["item_id"]
                or record["parent_fence_token"] != state["opened"]["fence_token"]
                or policy is None or record["parent_spawn_policy_id"] != policy.get("id")
                or policy.get("subagents_mode") != "managed"
            ):
                refuse("spawn_parent_fence_invalid", "group creation requires current started managed parent fence")
            if run["spawn_admission"] is None:
                refuse("spawn_admission_disabled", "run is not enabled for spawn admission")
            semantic = (record["parent_attempt_id"], record["group_key"])
            if semantic in run["spawn_group_by_parent_key"]:
                refuse("spawn_group_key_duplicate", "group key is unique within the parent attempt")
            if (
                record["max_children"] > policy["max_children"]
                or record["max_depth"] > policy["max_depth"]
                or not set(record["child_capability_ceiling"]) <= set(policy["child_capability_ceiling"])
                or record["workspace_policy"] not in policy["workspace_policies"]
            ):
                refuse("spawn_group_widening", "group cannot widen the parent spawn policy")
            policy_budget = {row["budget_id"]: row["amount"] for row in policy["spawn_budget_ceiling"]}
            allocated_budget: Dict[str, int] = {}
            for prior in run["spawn_groups"].values():
                if prior["created"]["parent_attempt_id"] == record["parent_attempt_id"] and prior["state"] != "aborted":
                    for row in prior["created"]["aggregate_budget"]:
                        allocated_budget[row["budget_id"]] = allocated_budget.get(row["budget_id"], 0) + row["amount"]
            for row in record["aggregate_budget"]:
                allocated_budget[row["budget_id"]] = allocated_budget.get(row["budget_id"], 0) + row["amount"]
            if any(amount > policy_budget.get(budget_id, 0) for budget_id, amount in allocated_budget.items()):
                refuse("spawn_budget_widening", "group budget cannot widen the parent spawn ceiling")
            run["spawn_groups"][record["id"]] = {
                "state": "pending", "created": record, "amendment": None,
                "member_item_ids": [], "admissions": {}, "rejections": {},
                "closed": None, "late_result_ids": [],
            }
            run["spawn_group_by_parent_key"][semantic] = record["id"]
        elif kind == "spawn_group_aborted":
            group = run["spawn_groups"].get(record["spawn_group_id"])
            if group is None or group["state"] != "pending":
                refuse("spawn_group_final", "only a pending group can abort")
            created = group["created"]
            if any(record[field] != created[field] for field in (
                "parent_attempt_id", "parent_fence_token"
            )):
                refuse("spawn_abort_binding_invalid", "abort must repeat the pending parent fence")
            group["state"] = "aborted"
            group["aborted"] = record
        elif kind == "child_admitted":
            group = run["spawn_groups"].get(record["spawn_group_id"])
            if group is None or group["state"] != "activated" or group["closed"] is not None:
                refuse("spawn_group_inactive", "child admission requires one open activated group")
            if record["plan_amendment_id"] != group["amendment"]["id"] or record["parent_attempt_id"] != group["created"]["parent_attempt_id"]:
                refuse("spawn_child_binding_invalid", "child admission must repeat group activation")
            child = next((row for row in group["amendment"]["children"] if row["item_id"] == record["child_item_id"]), None)
            if child is None:
                refuse("spawn_child_missing", "admission must name an immutable member")
            if record["child_item_id"] in group["admissions"] or record["child_item_id"] in group["rejections"]:
                refuse("spawn_child_outcome_duplicate", "child can have one admission outcome")
            repeated = {
                "child_depth": "depth", "task_contract_id": "task_contract_id",
                "task_contract_digest": "task_contract_digest", "capability_ceiling": "capability_ceiling",
                "budget_allocation": "budget_allocation", "workspace_policy": "workspace_policy",
            }
            if record["admission_digest"] != group["amendment"]["admission_digest"] or any(record[left] != child[right] for left, right in repeated.items()):
                refuse("spawn_child_binding_invalid", "admission must repeat its exact amended child")
            if record["workspace"] != f"/private/tmp/floati-work/{record['child_item_id']}":
                refuse("workspace_invalid", "child workspace must derive from its item")
            group["admissions"][record["child_item_id"]] = record
        elif kind == "child_rejected":
            group = run["spawn_groups"].get(record["spawn_group_id"])
            if group is None or group["state"] != "activated" or group["closed"] is not None:
                refuse("spawn_group_inactive", "child rejection requires one open activated group")
            item_id = record["child_item_id"]
            if (
                record["plan_amendment_id"] != group["amendment"]["id"]
                or item_id not in group["member_item_ids"]
                or item_id in group["admissions"] or item_id in group["rejections"]
            ):
                refuse("spawn_child_outcome_duplicate", "child can have one immutable admission outcome")
            group["rejections"][item_id] = record
            run["spawn_item_outcomes"][item_id] = (
                "failed" if group["created"]["on_child_failure"] == "fail_group" else "skipped"
            )
        elif kind == "spawn_group_closed":
            group = run["spawn_groups"].get(record["spawn_group_id"])
            if group is None or group["state"] != "activated" or group["closed"] is not None:
                refuse("spawn_group_final", "group closure requires one open activated group")
            created = group["created"]
            descendant_rows = [
                row for key, row in run["untracked_descendants"].items()
                if key[0] == created["parent_attempt_id"]
            ]
            if any(row["state"] == "observed" for row in descendant_rows):
                refuse("untracked_descendant_unresolved", "group cannot close with unresolved descendants")
            if (
                record["plan_amendment_id"] != group["amendment"]["id"]
                or record["parent_attempt_id"] != created["parent_attempt_id"]
                or record["member_item_ids"] != group["member_item_ids"]
                or record["join_mode"] != created["join_mode"]
                or record["required_count"] != created["required_count"]
            ):
                refuse("spawn_group_close_invalid", "closure must repeat immutable group truth")
            accepted_items = sorted(item for item in group["member_item_ids"] if item in run["accepted"])
            terminal_items = sorted(
                item for item in group["member_item_ids"]
                if run["spawn_item_outcomes"].get(item) == "cancelled"
                or (
                    run["item_attempt_ids"].get(item)
                    and run["attempts"][run["item_attempt_ids"][item][-1]]["terminal"] is not None
                )
            )
            rejected_items = sorted(group["rejections"])
            if (
                record["accepted_item_ids"] != accepted_items
                or record["terminal_item_ids"] != terminal_items
                or record["rejected_item_ids"] != rejected_items
            ):
                refuse("spawn_group_close_sets_invalid", "closure sets must derive from physical member truth")
            unknown_descendant = any(row["state"] == "unknown" for row in descendant_rows)
            decision = "needs_operator" if unknown_descendant else _spawn_join_decision(run, group)
            expected_reason = None
            if decision == "satisfied":
                expected_reason = {
                    "all_accepted": "all_members_accepted",
                    "all_terminal": "all_members_terminal",
                    "quorum": "quorum_reached",
                    "first_accepted": "first_accepted",
                }[created["join_mode"]]
            elif decision == "failed":
                expected_reason = (
                    "child_failure" if created["on_child_failure"] == "fail_group"
                    else "join_impossible"
                )
            elif decision == "needs_operator":
                expected_reason = (
                    "untracked_descendant_unknown" if unknown_descendant
                    else "member_needs_operator"
                )

            cancel_scope_id = record["cancel_scope_resolved_id"]
            resolved = next((
                cancellation["resolved"]
                for cancellation in run["cancellations"].values()
                if cancellation["resolved"] is not None
                and cancellation["resolved"]["id"] == cancel_scope_id
            ), None)
            if record["outcome"] == "cancelled":
                required_items = {created["parent_item_id"], *group["member_item_ids"]}
                complete_members = set(accepted_items) | set(terminal_items) | set(rejected_items)
                if (
                    record["close_reason"] != "parent_cancelled"
                    or resolved is None
                    or not required_items <= set(resolved["item_ids"])
                    or complete_members != set(group["member_item_ids"])
                ):
                    refuse("spawn_group_close_invalid", "cancelled close requires whole-group parent scope")
            elif record["outcome"] == "deadline":
                closed_at = datetime.fromisoformat(
                    record["closed_at_testimony"].replace("Z", "+00:00")
                )
                deadline = datetime.fromisoformat(created["deadline"].replace("Z", "+00:00"))
                if (
                    record["close_reason"] != "deadline_expired"
                    or closed_at < deadline
                    or decision is not None
                ):
                    refuse("spawn_group_close_invalid", "deadline close requires an expired still-live join")
            elif (
                record["outcome"] != decision
                or record["close_reason"] != expected_reason
            ):
                if unknown_descendant:
                    refuse("untracked_descendant_unknown", "unknown descendant forces needs-operator closure")
                refuse("spawn_group_close_invalid", "closure must equal the authoritative physical join")

            if record["outcome"] == "satisfied" and cancel_scope_id is not None:
                cancellation = next((
                    row for row in run["cancellations"].values()
                    if row["resolved"] is not None
                    and row["resolved"]["id"] == cancel_scope_id
                ), None)
                request = None if cancellation is None else cancellation["requested"]
                remaining = (
                    [] if request is None
                    else _spawn_remaining_at_record(run, group, request["id"])
                )
                if (
                    resolved is None
                    or not created["cancel_remaining_after_success"]
                    or request is None
                    or request.get("scope") != "exact_items"
                    or request.get("spawn_group_id") != record["spawn_group_id"]
                    or request.get("requested_by") != "spawn_join"
                    or created["parent_item_id"] in resolved["item_ids"]
                    or request.get("item_ids") != remaining
                    or resolved["item_ids"] != remaining
                ):
                    refuse("spawn_group_close_invalid", "satisfied cancellation must equal the physical remaining member set")
            group["closed"] = record
            group["state"] = "closed"
        elif kind == "untracked_descendant":
            state = run["attempts"].get(record["parent_attempt_id"])
            policy = run["attempt_spawn_policy"].get(record["parent_attempt_id"])
            if state is None or policy is None or policy.get("subagents_mode") == "disabled" or record["adapter"] != policy.get("adapter") or record["parent_item_id"] != state["opened"]["item_id"]:
                refuse("descendant_observation_invalid", "descendant testimony requires exact observed or managed launch")
            if record["parent_attempt_id"] in run["descendant_observation_close"]:
                refuse("descendant_observation_closed", "descendant testimony cannot follow observation closure")
            key = (record["parent_attempt_id"], record["adapter"], record["provider_descendant_id"])
            prior = run["untracked_descendants"].get(key)
            if prior is None and record["state"] != "observed":
                refuse("descendant_observation_missing", "descendant resolution requires prior observation")
            if prior is not None and prior["state"] != "observed":
                refuse("descendant_resolution_final", "descendant resolution is final")
            if record["state"] == "adopted":
                adopted_group_id = run["spawn_child_group"].get(record["adopted_item_id"])
                if (
                    adopted_group_id is None
                    or record["adopted_item_id"] not in run["spawn_groups"][adopted_group_id]["admissions"]
                ):
                    refuse("descendant_adoption_invalid", "adoption must name one admitted managed child")
            run["untracked_descendants"][key] = record
        elif kind == "descendant_observation_closed":
            state = run["attempts"].get(record["parent_attempt_id"])
            policy = run["attempt_spawn_policy"].get(record["parent_attempt_id"])
            if (
                state is None or policy is None or policy.get("subagents_mode") == "disabled"
                or record["attempt_spawn_policy_id"] != policy.get("id")
                or record["adapter"] != policy.get("adapter")
                or record["parent_item_id"] != state["opened"]["item_id"]
                or record["parent_fence_token"] != state["opened"]["fence_token"]
                or record["parent_attempt_id"] in run["descendant_observation_close"]
            ):
                refuse("descendant_observation_close_invalid", "observation closure must repeat exact launch policy")
            rows = [row for (attempt_id, _adapter, _provider), row in run["untracked_descendants"].items() if attempt_id == record["parent_attempt_id"]]
            if any(row["state"] == "observed" for row in rows):
                refuse("untracked_descendant_unresolved", "all observed descendants must resolve before closure")
            if any(row["state"] == "unknown" for row in rows):
                refuse("untracked_descendant_unknown", "unknown descendants cannot close observation")
            if record["observed_descendant_ids"] != sorted(row["provider_descendant_id"] for row in rows):
                refuse("descendant_observation_set_invalid", "closure must name the complete observed set")
            run["descendant_observation_close"][record["parent_attempt_id"]] = record
        elif kind == "spawn_late_result_disposition":
            group = run["spawn_groups"].get(record["spawn_group_id"])
            if group is None or group["closed"] is None or record["child_item_id"] not in group["member_item_ids"]:
                refuse("late_result_invalid", "late disposition requires a closed member group")
            if record["result_record_id"] not in group["late_result_ids"]:
                refuse("late_result_missing", "disposition must name a physically post-close result")
            key = (record["spawn_group_id"], record["child_item_id"], record["result_record_id"])
            if key in run["late_result_dispositions"]:
                refuse("late_result_disposition_duplicate", "late result can be disposed once")
            run["late_result_dispositions"][key] = record
        elif kind == "attempt_suspended_for_approval":
            state = run["attempts"].get(record["attempt_id"])
            if (
                state is None
                or state["started"] is None
                or state["terminal"] is not None
                or record["item_id"] != state["opened"]["item_id"]
                or record["attempt_started_id"] != state["started"]["id"]
                or record["fence_token"] != state["opened"]["fence_token"]
            ):
                refuse(
                    "attempt_suspension_invalid",
                    "suspension requires the exact started nonterminal attempt fence",
                )
            if state["suspension"] is not None:
                refuse("attempt_suspension_duplicate", "an attempt can suspend once")
            expected_workspace = f"/private/tmp/floati-work/{record['item_id']}"
            if record["workspace"] != expected_workspace:
                refuse(
                    "workspace_invalid",
                    "suspension workspace must derive from its exact run item",
                )
            reservation = self._workspace_reservations.get(str(record["workspace"]))
            if reservation is not None:
                refuse(
                    "workspace_reserved",
                    "one live suspension already owns this workspace",
                )
            state["suspension"] = record
            state["state"] = "suspended"
            self._workspace_reservations[str(record["workspace"])] = (
                run_id,
                str(record["attempt_id"]),
                str(record["id"]),
            )
        elif kind == "approval_consumed_for_resume":
            state = run["attempts"].get(record["attempt_id"])
            if state is None or state["suspension"] is None:
                refuse(
                    "approval_suspension_missing",
                    "approval consumption requires its prior physical suspension",
                )
            if state["terminal"] is not None:
                refuse(
                    "approval_consumption_terminal",
                    "terminal attempts cannot consume resume approval",
                )
            if state["approval_consumption"] is not None:
                refuse(
                    "approval_consumption_duplicate",
                    "one suspension can be consumed once",
                )
            suspension = state["suspension"]
            exact_repeats = (
                ("approval_request_id", "approval_request_id"),
                ("exact_action_digest", "exact_action_digest"),
                ("requested_scope", "requested_scope"),
                ("resume_mode", "resume_mode"),
                ("provider_session_or_thread_id", "provider_session_or_thread_id"),
                ("workspace", "workspace"),
                ("workspace_checkpoint", "workspace_checkpoint"),
            )
            if (
                record["item_id"] != state["opened"]["item_id"]
                or record["fence_token"] != state["opened"]["fence_token"]
                or record["attempt_suspended_id"] != suspension["id"]
                or any(record[left] != suspension[right] for left, right in exact_repeats)
                or record["resume_authority_subject"]
                != suspension["execution_authority_subject"]
                or record["resume_authority_epoch"]
                <= suspension["authority_epoch_at_request"]
            ):
                refuse(
                    "approval_consumption_invalid",
                    "consumption must repeat its suspension under a newer same-subject authority",
                )
            state["approval_consumption"] = record
            state["state"] = "resumed"
        elif kind == "acceptance_receipt":
            state = run["attempts"].get(record["attempt_id"])
            attempt = run["dispatches"].get(record["attempt_id"])
            contract = run["contracts"].get(record["item_id"])
            if state is not None and state["state"] == "suspended":
                refuse("attempt_suspended", "suspended attempts cannot append result evidence")
            if state is None or attempt is None or state["started"] is None or state["terminal"] is not None or record["item_id"] != state["opened"]["item_id"]:
                refuse("acceptance_receipt_invalid", "receipt requires a matching started nonterminal attempt")
            if contract is None or record["contract_digest"] != contract["contract_digest"] or record["result"] != "accepted":
                refuse("acceptance_receipt_invalid", "receipt must accept the item's current contract digest")
            if not set(record["check_ids"]) <= set(contract["contract"].canonical()["acceptance_checks"]):
                refuse("acceptance_receipt_invalid", "receipt checks must be declared by the current task contract")
            for receipt_id in record["evidence_bindings"]:
                evidence = receipts.get(str(receipt_id))
                consumption = state["approval_consumption"]
                if (
                    evidence is None
                    or evidence.get("work_item_id") != record["item_id"]
                    or evidence.get("node_id") != attempt["chosen_worker"]
                    or (
                        consumption is not None
                        and (
                            evidence.get("authority_subject")
                            != consumption["resume_authority_subject"]
                            or evidence.get("authority_epoch")
                            != consumption["resume_authority_epoch"]
                        )
                    )
                ):
                    refuse("acceptance_receipt_invalid", "receipt evidence must be a matching raw worker receipt")
            if record["id"] in run["acceptance_receipts"]:
                refuse("acceptance_receipt_duplicate", "acceptance receipt id may append once")
            run["acceptance_receipts"][record["id"]] = record
        elif kind in {"result_produced", "result_verified", "result_accepted"}:
            state = run["attempts"].get(record["attempt_id"])
            attempt = run["dispatches"].get(record["attempt_id"])
            parent_groups = [
                group for group in run["spawn_groups"].values()
                if group["created"]["parent_attempt_id"] == record["attempt_id"]
            ]
            if any(group["state"] == "pending" for group in parent_groups):
                refuse("spawn_group_pending", "pending group fences parent result transitions")
            if state is not None and state["state"] == "suspended":
                refuse("attempt_suspended", "suspended attempts cannot append result evidence")
            if state is None or state["started"] is None or state["terminal"] is not None or state["schedule"] is not None or state["exhaustion"] is not None or attempt is None or record["item_id"] != attempt["item_id"]:
                refuse("attempt_missing", "result requires matching started nonterminal attempt")
            if record["item_id"] not in run["contracts"]:
                refuse("task_contract_missing", "a task contract must bind before a result transition")
            for receipt_id in record["worker_receipt_ids"]:
                receipt = receipts.get(str(receipt_id))
                consumption = state["approval_consumption"]
                if (
                    receipt is None
                    or receipt.get("work_item_id") != record["item_id"]
                    or receipt.get("node_id") != attempt["chosen_worker"]
                    or (
                        consumption is not None
                        and (
                            receipt.get("authority_subject")
                            != consumption["resume_authority_subject"]
                            or receipt.get("authority_epoch")
                            != consumption["resume_authority_epoch"]
                        )
                    )
                ):
                    refuse("worker_receipt_invalid", "result requires existing matching raw worker receipt")
            if kind == "result_produced":
                if record["dispatch_decision_id"] != attempt["id"] or record["attempt_id"] in run["produced"]:
                    refuse("result_produced_invalid", "produced result must name its dispatch once")
                run["produced"][record["attempt_id"]] = record
            elif kind == "result_verified":
                produced = run["produced"].get(record["attempt_id"])
                if produced is None or record["result_produced_id"] != produced["id"] or record["attempt_id"] in run["verified"]:
                    refuse("result_verified_invalid", "verified result must name matching produced once")
                run["verified"][record["attempt_id"]] = record
            else:
                policy = run["attempt_spawn_policy"].get(record["attempt_id"])
                if any(group["state"] != "closed" or group["closed"]["outcome"] != "satisfied" for group in parent_groups):
                    refuse("spawn_join_unsatisfied", "parent acceptance requires every group closed satisfied")
                if any(
                    key[0] == record["attempt_id"] and row["state"] == "observed"
                    for key, row in run["untracked_descendants"].items()
                ):
                    refuse("untracked_descendant_unresolved", "parent acceptance requires every descendant resolved")
                if any(
                    key[0] == record["attempt_id"] and row["state"] == "unknown"
                    for key, row in run["untracked_descendants"].items()
                ):
                    refuse("untracked_descendant_unknown", "unknown descendant forces parent needs-operator")
                if policy is not None and policy.get("subagents_mode") in {"observed_only", "managed"} and record["attempt_id"] not in run["descendant_observation_close"]:
                    refuse("descendant_observation_close_missing", "non-disabled launch requires observation closure before acceptance")
                if record["item_id"] in run["accepted"]:
                    refuse("result_accepted_duplicate", "item can be accepted once")
                predecessor = run["verified"].get(record["attempt_id"]) if record["acceptance_mode"] == "verified" else run["produced"].get(record["attempt_id"])
                if predecessor is None or record["predecessor_result_id"] != predecessor["id"]:
                    refuse("result_accepted_invalid", "acceptance requires matching prior result")
                if record["acceptance_mode"] == "accepted_unverified" and record["attempt_id"] in run["verified"]:
                    refuse("result_accepted_invalid", "unverified acceptance forbids verification")
                if record["acceptance_mode"] == "verified":
                    receipt = run["acceptance_receipts"].get(record["acceptance_receipt_id"])
                    if receipt is None:
                        refuse("acceptance_receipt_missing", "verified acceptance requires its prior durable receipt")
                    contract = run["contracts"].get(record["item_id"])
                    if any(receipt[field] != record[field] for field in ("run_id", "item_id", "attempt_id")) or contract is None or receipt["contract_digest"] != contract["contract_digest"] or receipt["result"] != "accepted":
                        refuse("acceptance_receipt_invalid", "receipt must bind the accepted run item attempt and contract")
                current_effects = self._effect_evidence(
                    run_id, str(record["attempt_id"])
                )
                if record["schema_version"] == 0:
                    if (
                        current_effects is not None
                        and current_effects.operation_ids
                    ):
                        refuse(
                            "effect_binding_required",
                            "effectful attempts require schema-v1 result acceptance",
                        )
                else:
                    if current_effects is None:
                        refuse(
                            "effect_evidence_invalid",
                            "schema-v1 acceptance requires an Effect snapshot",
                        )
                    if (
                        require_current_effect_binding
                        and record["effect_ledger_high_watermark"]
                        != current_effects.high_watermark
                    ):
                        refuse(
                            "effect_evidence_invalid",
                            "acceptance must bind the current Effect prefix",
                        )
                    try:
                        effects = self._effect_evidence(
                            run_id, str(record["attempt_id"]),
                            high_watermark=record["effect_ledger_high_watermark"],
                        )
                    except (ProtocolRefusal, IntegrityFailure) as exc:
                        refuse(exc.code, exc.detail)
                    if effects.blockers:
                        refuse(
                            "effect_unknown_blocks_acceptance",
                            str(effects.blockers[0]),
                        )
                    if (
                        list(effects.operation_ids)
                        != record["effect_operation_ids"]
                        or effects.high_watermark
                        != record["effect_ledger_high_watermark"]
                        or effects.evidence_digest
                        != record["effect_evidence_digest"]
                    ):
                        refuse(
                            "effect_evidence_invalid",
                            "result acceptance must bind exact current effect evidence",
                        )
                    self._require_no_post_acceptance_intents(
                        run_id, record, refuse,
                    )
                    from .run_limits import RunLimitGate

                    try:
                        RunLimitGate.check_effect_spend(
                            self, run_id, record["item_id"],
                            record["attempt_id"], effects,
                        )
                    except ProtocolRefusal as exc:
                        refuse(exc.code, exc.detail)
                run["accepted"][record["item_id"]] = record
            group_id = run["spawn_child_group"].get(record["item_id"])
            if group_id is not None and run["spawn_groups"][group_id]["closed"] is not None:
                run["spawn_groups"][group_id]["late_result_ids"].append(record["id"])
        elif kind == "attempt_terminal":
            state = run["attempts"].get(record["attempt_id"])
            parent_groups = [
                group for group in run["spawn_groups"].values()
                if group["created"]["parent_attempt_id"] == record["attempt_id"]
            ]
            if any(group["state"] == "pending" for group in parent_groups):
                refuse("spawn_group_pending", "pending group fences parent attempt terminal")
            if any(group["state"] == "activated" for group in parent_groups):
                refuse("spawn_group_open", "activated group must close before parent attempt terminal")
            if state is None or state["started"] is None or state["terminal"] is not None:
                refuse("attempt_terminal_invalid", "terminal requires one prior started attempt")
            if record["terminal_state"] == "completed":
                terminal_class = "satisfied"
            elif record["terminal_state"] == "cancelled":
                terminal_class = "cancelled"
            elif (
                record["terminal_state"] == "uncertain"
                or record["effect_safety"] == "unknown_effect"
                or record["policy_class"] in {"operator_required", "unknown_effect"}
            ):
                terminal_class = "needs_operator"
            else:
                terminal_class = "failed"
            if any(
                key[0] == record["attempt_id"] and row["state"] == "unknown"
                for key, row in run["untracked_descendants"].items()
            ) and terminal_class != "needs_operator":
                refuse("untracked_descendant_unknown", "unknown descendant forces needs-operator parent terminal")
            expected_terminal = {
                "satisfied": "satisfied",
                "failed": "failed",
                "cancelled": "cancelled",
                "deadline": "needs_operator",
                "needs_operator": "needs_operator",
            }
            if any(
                group["state"] == "closed"
                and terminal_class != expected_terminal[group["closed"]["outcome"]]
                for group in parent_groups
            ) or any(
                group["state"] == "aborted" and terminal_class == "satisfied"
                for group in parent_groups
            ):
                refuse("spawn_group_terminal_mismatch", "parent terminal class must match every final group")
            denial_terminal = (
                record["terminal_state"],
                record["policy_class"],
                record["reason_code"],
                record["effect_safety"],
            ) == ("failed", "operator_required", "approval_denial", "idempotent")
            cancellation_terminal = (
                record["terminal_state"],
                record["policy_class"],
                record["reason_code"],
                record["effect_safety"],
            ) == ("cancelled", "cancelled", "operator_cancellation", "idempotent") and any(
                isinstance(cancellation_attempt, dict)
                and cancellation_attempt.get("terminal") is not None
                for cancellation in run["cancellations"].values()
                for cancellation_attempt in (
                    cancellation["attempts"].get(record["attempt_id"]),
                )
            )
            if (
                state["state"] == "suspended"
                and state["approval_consumption"] is None
                and not (denial_terminal or cancellation_terminal)
            ):
                refuse(
                    "attempt_suspended",
                    "an unconsumed suspension closes only through approval denial or completed cancellation",
                )
            opened = state["opened"]
            if any(record[field] != opened[field] for field in ("item_id", "ordinal", "fence_token")) or record["attempt_started_id"] != state["started"]["id"]:
                refuse("attempt_terminal_invalid", "terminal must exactly name its started attempt")
            accepted = run["accepted"].get(record["item_id"])
            if record["terminal_state"] == "completed":
                if accepted is None or accepted["attempt_id"] != record["attempt_id"]:
                    refuse("attempt_terminal_invalid", "completed terminal requires matching accepted result")
                if require_current_effect_binding:
                    self._require_current_acceptance_effects(
                        run_id, accepted, refuse,
                    )
            elif accepted is not None:
                refuse("attempt_terminal_invalid", "noncompleted terminal forbids an accepted item result")
            eligible = (record["terminal_state"], record["policy_class"], record["reason_code"], record["effect_safety"]) == ("failed", "transient", "transient_failure", "idempotent")
            if eligible and opened["ordinal"] < opened["max_attempts"]:
                expected = ("scheduled", "retry-scheduled-", opened["ordinal"] + 1)
                if record["retry_disposition"] != expected[0] or not str(record["retry_record_id"]).startswith(expected[1]) or record["next_ordinal"] != expected[2] or record["next_scheduler_epoch"] != opened["scheduler_epoch"] or record["next_fence_token"] != attempt_fence_token(run_id, record["item_id"], expected[2], opened["scheduler_epoch"]) or record["retry_delay_ms"] != retry_delay_from_backoff(run_id, record["item_id"], expected[2], opened["backoff"]):
                    refuse("retry_closure_invalid", "eligible terminal must reserve its exact governed retry")
            elif eligible and opened["ordinal"] == opened["max_attempts"]:
                if record["retry_disposition"] != "exhausted" or record["retry_record_id"] is None:
                    refuse("retry_closure_invalid", "last eligible attempt must reserve exhaustion")
            elif record["retry_disposition"] != "none":
                refuse("retry_forbidden", "only the governed eligible tuple can reserve retry closure")
            state["terminal"] = record
            state["state"] = "terminal"
            if state["suspension"] is not None:
                self._workspace_reservations.pop(
                    str(state["suspension"]["workspace"]), None
                )
        elif kind == "retry_scheduled":
            if _cancel_request_covers_item(run, record["item_id"], edges[run_id]):
                refuse("cancel_request_fence", "a durable cancellation request fences retry scheduling")
            if any(group["state"] == "pending" and group["created"]["parent_attempt_id"] == record["previous_attempt_id"] for group in run["spawn_groups"].values()):
                refuse("spawn_group_pending", "pending group fences parent retry")
            state = run["attempts"].get(record["previous_attempt_id"])
            if state is None or state["terminal"] is None or state["schedule"] is not None:
                refuse("retry_schedule_invalid", "retry schedule requires one prior terminal")
            terminal = state["terminal"]
            if terminal["retry_disposition"] != "scheduled" or any(record[left] != terminal[right] for left, right in (("id", "retry_record_id"), ("attempt_terminal_id", "id"), ("next_attempt_id", "next_attempt_id"), ("next_ordinal", "next_ordinal"), ("delay_ms", "retry_delay_ms"), ("scheduler_epoch", "next_scheduler_epoch"), ("next_fence_token", "next_fence_token"))) or record["item_id"] != terminal["item_id"]:
                refuse("retry_schedule_invalid", "retry schedule must consume terminal reservation exactly")
            state["schedule"] = record
        elif kind == "retry_exhausted":
            if any(group["state"] == "pending" and group["created"]["parent_attempt_id"] == record["attempt_id"] for group in run["spawn_groups"].values()):
                refuse("spawn_group_pending", "pending group fences parent exhaustion")
            state = run["attempts"].get(record["attempt_id"])
            if state is None or state["terminal"] is None or state["exhaustion"] is not None:
                refuse("retry_exhaustion_invalid", "retry exhaustion requires one prior terminal")
            terminal = state["terminal"]
            if terminal["retry_disposition"] != "exhausted" or record["id"] != terminal["retry_record_id"] or record["attempt_terminal_id"] != terminal["id"] or record["item_id"] != terminal["item_id"] or record["ordinal"] != state["opened"]["ordinal"] or record["max_attempts"] != state["opened"]["max_attempts"]:
                refuse("retry_exhaustion_invalid", "retry exhaustion must consume terminal reservation exactly")
            state["exhaustion"] = record
        elif kind == "cancel_requested":
            if record["scope"] == "item" and record["item_id"] not in run["item_ids"]:
                refuse("cancel_scope_invalid", "item cancellation must name a run item")
            if record["scope"] == "exact_items":
                group = run["spawn_groups"].get(record["spawn_group_id"])
                if (
                    group is None
                    or group["state"] != "activated"
                    or not set(record["item_ids"]) <= set(group["member_item_ids"])
                ):
                    refuse("cancel_scope_invalid", "exact items must be immutable members of the named group")
            run["cancellations"][record["id"]] = {"requested": record, "resolved": None, "attempts": {}}
        elif kind == "cancel_scope_resolved":
            cancellation = run["cancellations"].get(record["cancel_request_id"])
            if cancellation is None or cancellation["resolved"] is not None:
                refuse("cancel_scope_invalid", "scope resolution requires exactly one prior request")
            request = cancellation["requested"]
            if record["scope"] != request["scope"] or record["item_id"] != request["item_id"]:
                refuse("cancel_scope_invalid", "scope resolution must preserve its request scope")
            expected_items = (
                list(request["item_ids"])
                if request["scope"] == "exact_items"
                else _cancel_scope_items(run, edges[run_id], request["item_id"])
            )
            if record["item_ids"] != expected_items:
                refuse("cancel_scope_invalid", "scope resolution must persist the exact sorted graph closure")
            expected_attempts = sorted(
                attempt_id for attempt_id, state in run["attempts"].items()
                if state["opened"]["item_id"] in expected_items and state["terminal"] is None
            )
            if record["attempt_ids"] != expected_attempts:
                refuse("cancel_scope_invalid", "scope resolution must persist the active attempt closure")
            cancellation["resolved"] = record
        elif kind == "attempt_cancelled_before_start":
            cancellation = next((
                row for row in run["cancellations"].values()
                if row["resolved"] is not None
                and row["resolved"]["id"] == record["cancel_scope_resolved_id"]
            ), None)
            state = run["attempts"].get(record["attempt_id"])
            if record["retry_scheduled_id"] is None:
                capability = run["capability_sets"].get(record["attempt_id"])
                dispatch = run["dispatches"].get(record["attempt_id"])
                if (
                    cancellation is None or state is None or state["started"] is not None
                    or state["terminal"] is not None
                    or record["attempt_id"] not in cancellation["resolved"]["attempt_ids"]
                    or record["item_id"] != state["opened"]["item_id"]
                    or record["attempt_opened_id"] != state["opened"]["id"]
                    or record["fence_token"] != state["opened"]["fence_token"]
                    or record["capability_set_bound_id"] != (None if capability is None else capability["id"])
                    or record["dispatch_decision_id"] != (None if dispatch is None else dispatch["id"])
                ):
                    refuse("cancel_transition_invalid", "pre-start cancellation must repeat its exact resolved attempt prefix")
            else:
                prior_ids = run["item_attempt_ids"].get(record["item_id"], [])
                prior = None if not prior_ids else run["attempts"][prior_ids[-1]]
                schedule = None if prior is None else prior.get("schedule")
                terminal = None if prior is None else prior.get("terminal")
                reservation = (
                    schedule
                    if schedule is not None
                    else None if terminal is None else {
                        "id": terminal.get("retry_record_id"),
                        "next_attempt_id": terminal.get("next_attempt_id"),
                        "next_ordinal": terminal.get("next_ordinal"),
                        "scheduler_epoch": terminal.get("next_scheduler_epoch"),
                        "next_fence_token": terminal.get("next_fence_token"),
                    }
                )
                if (
                    cancellation is None
                    or record["item_id"] not in cancellation["resolved"]["item_ids"]
                    or state is not None or prior is None or reservation is None
                    or terminal is None
                    or terminal.get("retry_disposition") != "scheduled"
                    or record["retry_scheduled_id"] != reservation["id"]
                    or record["attempt_id"] != reservation["next_attempt_id"]
                    or record["fence_token"] != reservation["next_fence_token"]
                    or record["attempt_opened_id"] is not None
                    or record["capability_set_bound_id"] is not None
                    or record["dispatch_decision_id"] is not None
                ):
                    refuse("cancel_transition_invalid", "scheduled retry cancellation must consume its exact fenced reservation")
                opened = {
                    "schema_version": 1,
                    "id": None,
                    "tenant_id": record["tenant_id"],
                    "timestamp": record["timestamp"],
                    "kind": "attempt_opened",
                    "run_id": record["run_id"],
                    "item_id": record["item_id"],
                    "attempt_id": record["attempt_id"],
                    "ordinal": reservation["next_ordinal"],
                    "scheduler_epoch": reservation["scheduler_epoch"],
                    "fence_token": reservation["next_fence_token"],
                    "max_attempts": prior["opened"]["max_attempts"],
                    "backoff": deepcopy(prior["opened"]["backoff"]),
                }
                state = {
                    "opened": opened, "started": None, "terminal": None,
                    "schedule": None, "exhaustion": None,
                    "suspension": None, "approval_consumption": None,
                    "state": "running",
                }
                run["attempts"][record["attempt_id"]] = state
                run["item_attempt_ids"].setdefault(record["item_id"], []).append(
                    record["attempt_id"]
                )
            state["terminal"] = record
            state["state"] = "terminal"
            run["spawn_item_outcomes"][record["item_id"]] = "cancelled"
            run["spawn_item_outcome_records"][record["item_id"]] = record
            cancellation["attempts"][record["attempt_id"]] = {"prestart": record}
        elif kind == "spawn_child_cancelled_without_attempt":
            cancellation = next((
                row for row in run["cancellations"].values()
                if row["resolved"] is not None
                and row["resolved"]["id"] == record["cancel_scope_resolved_id"]
            ), None)
            group = run["spawn_groups"].get(record["spawn_group_id"])
            item_id = record["child_item_id"]
            admission = None if group is None else group["admissions"].get(item_id)
            if (
                cancellation is None or item_id not in cancellation["resolved"]["item_ids"]
                or group is None or group["state"] != "activated"
                or record["plan_amendment_id"] != group["amendment"]["id"]
                or item_id not in group["member_item_ids"]
                or item_id in group["rejections"] or run["item_attempt_ids"].get(item_id)
                or record["child_admitted_id"] != (None if admission is None else admission["id"])
                or item_id in run["spawn_item_outcomes"]
            ):
                refuse("cancel_transition_invalid", "zero-attempt cancellation must repeat one live immutable child")
            run["spawn_item_outcomes"][item_id] = "cancelled"
            run["spawn_item_outcome_records"][item_id] = record
        elif kind in {"cancel_observed", "cancel_signal_sent", "cancel_terminal", "cancel_unconfirmed"}:
            cancellation = next((row for row in run["cancellations"].values() if row["resolved"] is not None and row["resolved"]["id"] == record["cancel_scope_resolved_id"]), None)
            if cancellation is None:
                refuse("cancel_transition_invalid", "cancellation transition requires a resolved scope")
            resolved = cancellation["resolved"]
            state = run["attempts"].get(record["attempt_id"])
            if state is None or record["item_id"] != state["opened"]["item_id"] or record["attempt_id"] not in resolved["attempt_ids"] or record["fence_token"] != state["opened"]["fence_token"]:
                refuse("cancel_transition_invalid", "cancellation transition must name a resolved current attempt fence")
            prior = cancellation["attempts"].get(record["attempt_id"])
            if kind == "cancel_observed":
                if prior is not None:
                    refuse("cancel_transition_invalid", "cancellation observation may append once")
                cancellation["attempts"][record["attempt_id"]] = record
            else:
                if prior is None or prior["cancel_mode"] != record["cancel_mode"] or prior["adapter"] != record["adapter"]:
                    refuse("cancel_transition_invalid", "cancellation transition must preserve observed adapter mode")
                if kind == "cancel_signal_sent":
                    if record["cancel_mode"] == "unavailable" or prior.get("signal") is not None:
                        refuse("cancel_transition_invalid", "only one available cancellation signal may follow observation")
                    prior = dict(prior); prior["signal"] = record; cancellation["attempts"][record["attempt_id"]] = prior
                elif kind == "cancel_terminal":
                    if record["cancel_mode"] == "unavailable" or prior.get("signal") is None or prior.get("terminal") is not None:
                        refuse("cancel_transition_invalid", "terminal cancellation requires exactly one signal")
                    prior = dict(prior); prior["terminal"] = record; cancellation["attempts"][record["attempt_id"]] = prior
                else:
                    if prior.get("unconfirmed") is not None or prior.get("terminal") is not None:
                        refuse("cancel_transition_invalid", "cancellation can become unconfirmed only once before terminal confirmation")
                    prior = dict(prior); prior["unconfirmed"] = record; cancellation["attempts"][record["attempt_id"]] = prior
        elif kind == "stale_attempt_evidence":
            state = run["attempts"].get(record["attempt_id"])
            current_ids = run["item_attempt_ids"].get(record["item_id"], [])
            if state is None or state["opened"]["item_id"] != record["item_id"] or not current_ids:
                refuse("stale_evidence_invalid", "stale evidence must name an existing item attempt")
            current_id = current_ids[-1]
            current = run["attempts"][current_id]["opened"]
            if current_id == record["attempt_id"] or record["presented_fence_token"] != state["opened"]["fence_token"] or record["current_attempt_id"] != current_id or record["current_fence_token"] != current["fence_token"]:
                refuse("stale_evidence_invalid", "stale evidence must name a superseded attempt and current fence")
            for receipt_id in record["worker_receipt_ids"]:
                receipt = receipts.get(str(receipt_id))
                if receipt is None or receipt.get("work_item_id") != record["item_id"]:
                    refuse("worker_receipt_invalid", "stale evidence requires matching raw worker receipts")
            run["stale_evidence"][record["id"]] = record
        elif kind == "stale_evidence_adopted":
            evidence = run["stale_evidence"].get(record["stale_evidence_id"])
            current_ids = run["item_attempt_ids"].get(record["item_id"], [])
            if evidence is None or evidence["item_id"] != record["item_id"] or not current_ids:
                refuse("stale_adoption_invalid", "stale adoption requires retained evidence for its run item")
            current_id = current_ids[-1]
            current = run["attempts"][current_id]["opened"]
            if record["current_attempt_id"] != current_id or record["current_fence_token"] != current["fence_token"] or record["stale_evidence_id"] in run["stale_adoptions"]:
                refuse("stale_adoption_invalid", "stale adoption must name its still-current fence exactly once")
            run["stale_adoptions"][record["stale_evidence_id"]] = record
        elif kind == "attempt_harness_session_bound":
            state = run["attempts"].get(record["attempt_id"])
            if state is None or state["opened"]["item_id"] != record["item_id"] or state["opened"]["fence_token"] != record["fence_token"]:
                refuse("harness_binding_invalid", "harness session binding must name its exact attempt fence")
            if record["schema_version"] == 1:
                attempt_id = str(record["attempt_id"])
                positions = harness_segment_positions[run_id].setdefault(attempt_id, {})
                attempts_by_segment = harness_segment_attempts[run_id]
                current_segment_ids = {
                    str(segment["segment_id"])
                    for segment in record["harness_segments"]
                }
                for segment in record["harness_segments"]:
                    segment_id = str(segment["segment_id"])
                    if segment_id in positions:
                        refuse("harness_segment_id_duplicate", "segment_id must be unique within one attempt lineage")
                    if segment["segment_kind"] != "initial":
                        predecessor_segment_id = str(segment["predecessor_segment_id"])
                        predecessor_position = positions.get(predecessor_segment_id)
                        if predecessor_position is None:
                            predecessor_attempts = attempts_by_segment.get(predecessor_segment_id, set())
                            if predecessor_attempts and attempt_id not in predecessor_attempts:
                                refuse("harness_predecessor_attempt_mismatch", "harness predecessor must belong to the same attempt")
                            if predecessor_segment_id in current_segment_ids:
                                refuse("harness_predecessor_not_prior", "harness predecessor must be at an earlier physical position")
                            refuse("harness_predecessor_missing", "harness predecessor must exist in the same attempt lineage")
                        if predecessor_position >= (record_position, int(segment["ordinal"])):
                            refuse("harness_predecessor_not_prior", "harness predecessor must be at an earlier physical position")
                    positions[segment_id] = (record_position, int(segment["ordinal"]))
                    attempts_by_segment.setdefault(segment_id, set()).add(attempt_id)
            run["harness_sessions"].setdefault(record["attempt_id"], []).append(record)
        elif kind == "supervisor_orphaned":
            bindings = run["harness_sessions"].get(record["attempt_id"], [])
            if not any(binding["item_id"] == record["item_id"] and binding["claim_id"] == record["claim_id"] and binding["lease_id"] == record["lease_id"] and binding["worker_session_id"] == record["worker_session_id"] for binding in bindings):
                refuse("orphaning_invalid", "Floati orphaning requires a matching harness session binding")
            orphaned_classes = run["orphaned_by_attempt"].setdefault(record["attempt_id"], set())
            if record["orphan_class"] in orphaned_classes:
                refuse("orphaning_duplicate", "Floati can emit each typed orphaning class once per attempt")
            orphaned_classes.add(record["orphan_class"])
            run["orphaned"][record["id"]] = record
        elif kind == "run_terminal":
            if any(group["state"] == "pending" for group in run["spawn_groups"].values()):
                refuse("spawn_group_pending", "pending group fences run terminal")
            if any(group["state"] == "activated" for group in run["spawn_groups"].values()):
                refuse("spawn_group_open", "run terminal requires every group closed or aborted")
            for group_id, group in run["spawn_groups"].items():
                if group["created"]["on_late_result"] == "operator_decision":
                    for result_id in group["late_result_ids"]:
                        item_id = next((item for item in group["member_item_ids"] if any(
                            row.get("id") == result_id and row.get("item_id") == item
                            for row in run["records"]
                        )), None)
                        if item_id is not None and (group_id, item_id, result_id) not in run["late_result_dispositions"]:
                            refuse("late_result_disposition_missing", "operator-decision late result blocks run terminal")
            outcome = self._run_outcome(run, edges[run_id])
            if run["terminal"] is not None or record["outcome"] != outcome:
                refuse("run_terminal_invalid", "run terminal outcome must equal its canonical logical projection")
            if record["outcome"] == "succeeded" and require_current_effect_binding:
                for item_id in run["item_ids"]:
                    acceptance = run["accepted"].get(item_id)
                    if acceptance is None:
                        refuse(
                            "run_terminal_invalid",
                            "successful run terminal requires every accepted item",
                        )
                    self._require_current_acceptance_effects(
                        run_id, acceptance, refuse,
                    )
            run["terminal"] = record
        if retain_record:
            run["records"].append(record)
        self._last_position = physical_position


    def run(self, run_id: str) -> Dict[str, object]:
        if run_id not in self._runs:
            raise ProtocolRefusal("run_missing", "run is not present")
        return deepcopy(self._runs[run_id])

    def effect_intent_context(
        self, run_id: object, item_id: object, attempt_id: object, fence_token: object
    ) -> Dict[str, object]:
        """Freeze the exact current attempt coordinates eligible for new intent."""

        if not isinstance(run_id, str) or run_id not in self._runs:
            raise ProtocolRefusal("run_missing", "effect intent run is not present")
        run = self._runs[run_id]
        if item_id in run["accepted"]:
            raise ProtocolRefusal(
                "effect_attempt_accepted", "accepted results fence new effect intent"
            )
        if run["terminal"] is not None:
            raise ProtocolRefusal(
                "effect_run_terminal", "terminal runs fence new effect intent"
            )
        state = run["attempts"].get(attempt_id)
        if state is None or state["opened"].get("item_id") != item_id:
            raise ProtocolRefusal(
                "effect_attempt_missing", "effect intent requires its exact run attempt"
            )
        current_ids = run["item_attempt_ids"].get(item_id, [])
        if not current_ids or current_ids[-1] != attempt_id:
            raise ProtocolRefusal(
                "effect_attempt_stale", "effect intent requires the current item attempt"
            )
        if state["started"] is None:
            raise ProtocolRefusal(
                "effect_attempt_unstarted", "effect intent requires a started attempt"
            )
        if state["terminal"] is not None:
            raise ProtocolRefusal(
                "effect_attempt_terminal", "terminal attempts fence new effect intent"
            )
        if state["state"] == "suspended":
            raise ProtocolRefusal(
                "effect_attempt_suspended", "suspended attempts fence new effect intent"
            )
        if state["opened"]["fence_token"] != fence_token:
            raise ProtocolRefusal(
                "effect_fence_stale", "effect intent fence is not the current attempt fence"
            )
        policy = run.get("policy")
        admission = run.get("admission_binding")
        if (
            not isinstance(policy, dict)
            or not isinstance(admission, dict)
            or admission.get("status") != "bound"
        ):
            raise ProtocolRefusal(
                "effect_run_admission_missing",
                "effect intent requires current durable policy and admission bindings",
            )
        consumption = state.get("approval_consumption")
        return {
            "attempt_started_id": state["started"]["id"],
            "fence_token": state["opened"]["fence_token"],
            "policy_digest": policy["policy_digest"],
            "budget_reservations": deepcopy(admission["budget_reservations"]),
            "approval_consumption_id": (
                None if consumption is None else consumption["id"]
            ),
            "approval_consumption_request_id": (
                None if consumption is None else consumption["approval_request_id"]
            ),
            "approval_consumption_decision_id": (
                None if consumption is None else consumption["approval_decision_id"]
            ),
            "approval_consumption_action_digest": (
                None if consumption is None else consumption["exact_action_digest"]
            ),
        }

    def semantic_digest(self) -> str:
        """Hash the replay state that affects lifecycle meaning, not retained history."""
        semantic_runs = {
            run_id: {key: value for key, value in run.items() if key != "records"}
            for run_id, run in self._runs.items()
        }
        payload = self._semantic_json({"runs": semantic_runs, "edges": self._edges})
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _semantic_json(value: object) -> object:
        if isinstance(value, TaskContract):
            return RunProjection._semantic_json(value.canonical())
        if isinstance(value, DependencyEdge):
            return {
                "source": value.source,
                "target": value.target,
                "requires": value.requires,
                "failure_policy": value.failure_policy,
            }
        if isinstance(value, dict):
            return {
                str(key): RunProjection._semantic_json(item)
                for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            }
        if isinstance(value, set):
            return sorted(RunProjection._semantic_json(item) for item in value)
        if isinstance(value, tuple):
            return [RunProjection._semantic_json(item) for item in value]
        if isinstance(value, list):
            return [RunProjection._semantic_json(item) for item in value]
        return value

    def edges(self, run_id: str) -> list[DependencyEdge]:
        self.run(run_id)
        return list(self._edges[run_id])

    def task_contract(self, run_id: str, item_id: str) -> Dict[str, object]:
        run = self.run(run_id)
        contract = run["contracts"].get(item_id)
        if contract is None:
            raise ProtocolRefusal("task_contract_missing", "run item has no durable task contract")
        projected = {
            "task_contract_id": contract["task_contract_id"], "contract_digest": contract["contract_digest"],
            "contract": contract["contract"].canonical(), "history_ids": list(contract["history_ids"]),
        }
        if "repository" in contract:
            projected["repository"] = contract["repository"]
        return projected

    def _item_outcomes(self, run: Dict[str, object], edges: Sequence[DependencyEdge]) -> Dict[str, str]:
        outcomes: Dict[str, str] = {}
        for item_id in run["item_ids"]:
            if item_id in run.get("spawn_item_outcomes", {}):
                outcomes[item_id] = run["spawn_item_outcomes"][item_id]
                continue
            attempt_ids = run["item_attempt_ids"].get(item_id, [])
            if not attempt_ids:
                outcomes[item_id] = "uncertain"
                continue
            terminal = run["attempts"][attempt_ids[-1]]["terminal"]
            if terminal is None or terminal["retry_disposition"] == "scheduled":
                outcomes[item_id] = "uncertain"
            elif terminal["terminal_state"] == "completed":
                outcomes[item_id] = "succeeded"
            elif terminal["terminal_state"] == "uncertain" or terminal["effect_safety"] == "unknown_effect" or terminal["policy_class"] == "unknown_effect":
                outcomes[item_id] = "uncertain"
            elif terminal["terminal_state"] == "cancelled":
                outcomes[item_id] = "cancelled"
            elif terminal["policy_class"] == "operator_required":
                outcomes[item_id] = "needs_operator"
            elif terminal["policy_class"] == "policy_refusal":
                outcomes[item_id] = "skipped"
            else:
                outcomes[item_id] = "failed"
            acceptance = run["accepted"].get(item_id)
            accepted_watermark = None
            if (
                outcomes[item_id] == "succeeded"
                and acceptance is not None
                and acceptance["attempt_id"] == attempt_ids[-1]
                and acceptance["schema_version"] == 1
            ):
                accepted_watermark = acceptance["effect_ledger_high_watermark"]
            effects = self._effect_evidence(
                run["run_id"], attempt_ids[-1],
                high_watermark=accepted_watermark,
            )
            if effects is not None and effects.blockers:
                blocker_states = {
                    blocker.rsplit(":", 1)[-1]
                    for blocker in effects.blockers
                }
                if (
                    effects.incomplete_spend_operation_ids
                    or blocker_states - {"failed", "reconciled_failed"}
                ):
                    outcomes[item_id] = "needs_operator"
        changed = True
        while changed:
            changed = False
            for edge in edges:
                if outcomes[edge.target] != "uncertain" or outcomes[edge.source] in {"succeeded", "uncertain"}:
                    continue
                if edge.failure_policy in {"fail_run", "skip_dependent"}:
                    outcomes[edge.target] = "skipped"
                    changed = True
        return outcomes

    @staticmethod
    def _whole_parent_spawn_cancellation(
        run: Dict[str, object], outcomes: Dict[str, str],
    ) -> bool:
        """Recognize only complete spawn-family cancellation provenance."""

        if (
            "cancelled" not in outcomes.values()
            or set(outcomes.values()) - {"cancelled", "skipped"}
        ):
            return False
        for cancellation in run["cancellations"].values():
            request = cancellation["requested"]
            resolved = cancellation["resolved"]
            if resolved is None:
                continue
            scope = request.get("scope")
            covered = set(resolved["item_ids"])
            if scope == "item" and request.get("item_id") is not None:
                parent_item_id = request["item_id"]
                if parent_item_id not in covered:
                    continue
            elif scope == "run" and request.get("item_id") is None:
                parent_item_id = None
                if covered != set(run["item_ids"]):
                    continue
            else:
                continue
            if not set(outcomes) <= covered:
                continue
            groups = [
                group for group in run["spawn_groups"].values()
                if group["created"]["parent_item_id"] in covered
                and group["state"] != "aborted"
            ]
            if (
                not groups
                or (
                    parent_item_id is not None
                    and not any(
                        group["created"]["parent_item_id"] == parent_item_id
                        for group in groups
                    )
                )
                or any(
                    group["state"] != "closed"
                    or group["closed"]["outcome"] != "cancelled"
                    or group["closed"]["cancel_scope_resolved_id"] != resolved["id"]
                    or not set(group["member_item_ids"]) <= covered
                    for group in groups
                )
            ):
                continue

            cancellation_consistent = True
            for item_id, outcome in outcomes.items():
                if outcome != "cancelled":
                    continue
                outcome_record = run["spawn_item_outcome_records"].get(item_id)
                if (
                    outcome_record is not None
                    and outcome_record.get("cancel_scope_resolved_id") == resolved["id"]
                ):
                    continue
                attempt_ids = run["item_attempt_ids"].get(item_id, [])
                if not attempt_ids:
                    cancellation_consistent = False
                    break
                attempt_id = attempt_ids[-1]
                state = run["attempts"][attempt_id]
                transition = cancellation["attempts"].get(attempt_id)
                if (
                    state["terminal"] is None
                    or state["terminal"].get("terminal_state") != "cancelled"
                    or not isinstance(transition, dict)
                    or (
                        transition.get("terminal") is None
                        and transition.get("prestart") is None
                    )
                ):
                    cancellation_consistent = False
                    break
            if cancellation_consistent:
                return True
        return False

    def _run_outcome(self, run: Dict[str, object], edges: Sequence[DependencyEdge]) -> str:
        outcomes = self._item_outcomes(run, edges)
        values = set(outcomes.values())
        if "uncertain" in values:
            return "uncertain"
        if "needs_operator" in values:
            return "needs_operator"
        if any(edge.failure_policy == "fail_run" and outcomes[edge.source] != "succeeded" for edge in edges):
            if self._whole_parent_spawn_cancellation(run, outcomes):
                return "cancelled"
            return "failed"
        if values == {"succeeded"}:
            return "succeeded"
        if "succeeded" in values and len(values) > 1:
            return "partially_succeeded"
        if "failed" in values:
            return "failed"
        if "cancelled" in values and values <= {"cancelled", "skipped"}:
            return "cancelled"
        if values == {"skipped"}:
            return "skipped"
        return "partially_succeeded"

    def item_outcomes(self, run_id: str) -> Dict[str, str]:
        run = self.run(run_id)
        return self._item_outcomes(run, self._edges[run_id])

    def run_outcome(self, run_id: str) -> str:
        run = self.run(run_id)
        return self._run_outcome(run, self._edges[run_id])


class RunLedger:
    relative_path = Path("runs/events.jsonl")

    def __init__(
        self,
        root: FloatiRoot,
        *,
        sequencer_client: object = None,
        segment_config: SegmentConfig = SegmentConfig(),
    ) -> None:
        self.root = root
        self._store = SegmentedRunStore(root, RUN_KINDS, segment_config)
        if sequencer_client is not None and not callable(
            getattr(sequencer_client, "append", None)
        ):
            raise ProtocolRefusal(
                "sequencer_client_invalid", "run ledger requires a sequencer client"
            )
        self._sequencer_client = sequencer_client
        self.__scheduler_capability = None
        self.__cancellation_capability = None
        self.__supervisor_capability = None
        self.__capability_binding_capability = None
        self.__admission_binding_capability = None
        self.__suspension_capability = None
        self.__spawn_group_capability = None
        self._managed_projection_cache: Optional[
            Tuple[int, str, str, RunProjection]
        ] = None

    def records(self) -> list[Dict[str, object]]:
        return self._store.records()

    @staticmethod
    def _canonical_client_response(response: object) -> Dict[str, object]:
        canonical = response.get("record") if isinstance(response, dict) else None
        if not isinstance(canonical, dict):
            raise ProtocolRefusal(
                "sequencer_response_invalid",
                "sequencer did not confirm a canonical run record",
            )
        return canonical

    def _with_cross_ledger_snapshot(
        self, operation: Callable[[object], object], *, already_guarded: bool,
        exclusive: bool,
    ) -> object:
        from .effects import EffectLedger

        guard = (
            nullcontext()
            if already_guarded
            else (
                effect_acceptance_guard(self.root)
                if exclusive
                else effect_acceptance_guard(self.root, exclusive=False)
            )
        )
        with guard:
            effects = EffectLedger(self.root).project()
            return operation(effects)

    def _project_cross_ledger(self, *, already_guarded: bool) -> RunProjection:
        def capture_run_snapshot(effects: object) -> object:
            def decide(snapshot: RunStoreSnapshot):
                # Hold the run writer lock while sampling receipt testimony and
                # the physical run prefix.  The outer coordination guard keeps
                # this Run snapshot paired with its already-released Effect
                # snapshot without ever nesting the two ledger locks.
                return (
                    effects,
                    snapshot,
                    WorkerReceipts(self.root).records(),
                ), None

            return self._store.transact(decide)

        captured = self._with_cross_ledger_snapshot(
            capture_run_snapshot, already_guarded=already_guarded,
            exclusive=False,
        )
        if (
            not isinstance(captured, tuple)
            or len(captured) != 3
            or not isinstance(captured[1], RunStoreSnapshot)
        ):
            raise IntegrityFailure(
                "effect_evidence_invalid", "cross-ledger projection returned invalid state"
            )
        effects, snapshot, receipts = captured
        projection = RunProjection.empty(
            receipts, effect_projection=effects,
        )
        # Replay the immutable paired snapshots after releasing every lock so
        # read work cannot starve an exclusive intent/acceptance fence.
        for physical_position, record in enumerate(snapshot.iter_records(), start=1):
            projection.apply(
                record, physical_position=physical_position, integrity=True,
            )
        return projection

    def _project_under_effect_acceptance_guard(self) -> RunProjection:
        """Project while the caller already holds the non-reentrant Effect fence."""

        return self._project_cross_ledger(already_guarded=True)

    def project(self) -> RunProjection:
        return self._project_cross_ledger(already_guarded=False)

    def append(self, record: Dict[str, object]) -> Dict[str, object]:
        self._authorize_public_append(record)
        return self._append(record, scheduler=False)

    @staticmethod
    def _authorize_public_append(record: Dict[str, object]) -> None:
        if record.get("kind") in SPAWN_GROUP_KINDS or (
            record.get("kind") == "plan_amendment" and record.get("schema_version") == 1
        ):
            raise ProtocolRefusal(
                "spawn_group_controller_only",
                "spawn records and schema-v1 activation require controller-owned authority",
            )
        if record.get("kind") in ATTEMPT_KINDS:
            raise ProtocolRefusal("scheduler_only", "attempt and retry records are scheduler-owned")
        if record.get("kind") in CANCELLATION_KINDS:
            raise ProtocolRefusal("cancellation_only", "cancellation records are coordinator-owned")
        if record.get("kind") in SUPERVISOR_KINDS:
            raise ProtocolRefusal("supervisor_only", "Floati orphaning records are supervisor-owned")
        if record.get("kind") in SUSPENSION_KINDS:
            raise ProtocolRefusal(
                "suspension_controller_only",
                "approval suspension records require controller-owned authority",
            )
        if record.get("kind") in CAPABILITY_BINDING_KINDS:
            raise ProtocolRefusal("capability_binder_only", "capability snapshots are binder-owned")
        if record.get("kind") in ADMISSION_BINDING_KINDS:
            raise ProtocolRefusal("admission_binder_only", "run admission bindings are binder-owned")
        if record.get("kind") == "dispatch_decision" and record.get("schema_version") == 1:
            raise ProtocolRefusal("capability_binder_only", "v1 dispatch is capability-binder-owned")

    def _append_managed(
        self,
        record: Dict[str, object],
        epoch: int,
        capability: object = None,
    ) -> Dict[str, object]:
        from .sequencer_epoch import _managed_append_scope

        self._authorize_public_append(record)
        with _managed_append_scope(capability, self.root, epoch):
            return self._append_governed(record, scheduler=False)

    def _append_managed_owned(
        self,
        owner: str,
        record: Dict[str, object],
        epoch: int,
        managed_capability: object,
        owner_capability: object,
        *,
        dispatch_policy: object = None,
    ) -> Dict[str, object]:
        """Validate service-owned domain authority inside the live managed lease."""
        if owner == "scheduler":
            valid = (
                owner_capability is self.__scheduler_capability
                and record.get("kind") in ATTEMPT_KINDS
            )
            code, detail, scheduler = (
                "scheduler_only",
                "scheduler capability only appends attempt and retry records",
                True,
            )
        elif owner == "cancellation":
            valid = (
                owner_capability is self.__cancellation_capability
                and record.get("kind") in CANCELLATION_KINDS
            )
            code, detail, scheduler = (
                "cancellation_only",
                "cancellation capability only appends cancellation records",
                False,
            )
        elif owner == "supervisor":
            valid = (
                owner_capability is self.__supervisor_capability
                and record.get("kind") in SUPERVISOR_KINDS
            )
            code, detail, scheduler = (
                "supervisor_only",
                "supervisor capability only appends Floati orphaning records",
                False,
            )
        elif owner == "admission_binding":
            valid = (
                owner_capability is self.__admission_binding_capability
                and record.get("kind") in ADMISSION_BINDING_KINDS
            )
            code, detail, scheduler = (
                "admission_binder_only",
                "admission capability only appends run admission bindings",
                False,
            )
        elif owner == "capability_binding":
            valid = (
                owner_capability is self.__capability_binding_capability
                and record.get("kind") in CAPABILITY_BINDING_KINDS
            )
            code, detail, scheduler = (
                "capability_binder_only",
                "binder capability only appends capability snapshots",
                False,
            )
        elif owner == "capability_dispatch":
            valid = (
                owner_capability is self.__capability_binding_capability
                and record.get("kind") == "dispatch_decision"
                and record.get("schema_version") == 1
                and dispatch_policy is not None
            )
            code, detail, scheduler = (
                "capability_binder_only",
                "binder capability only appends governed v1 dispatches",
                False,
            )
        else:
            raise ProtocolRefusal("intent_owner_invalid", "typed intent owner is invalid")
        if owner_capability is None or not valid:
            raise ProtocolRefusal(code, detail)
        from .sequencer_epoch import _managed_append_scope

        with _managed_append_scope(managed_capability, self.root, epoch):
            return self._append_governed(
                record,
                scheduler=scheduler,
                dispatch_policy=dispatch_policy,
            )

    def _append_managed_scheduler(
        self,
        record: Dict[str, object],
        capability: object,
        epoch: int,
        managed_capability: object,
        service_capability: object,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        """Append scheduler truth reconstructed inside one live evaluation."""

        if (
            capability is None
            or capability is not self.__scheduler_capability
            or record.get("kind") not in ATTEMPT_KINDS
            or not self._has_evaluated_service_capability(service_capability)
        ):
            raise ProtocolRefusal(
                "scheduler_only",
                "managed scheduling requires method-local scheduler authority",
            )
        from .sequencer_epoch import _managed_append_scope

        with _managed_append_scope(managed_capability, self.root, epoch):
            return self._append_governed(
                record,
                scheduler=True,
                resolve_existing=resolve_existing,
            )

    def _append_managed_cancellation(
        self,
        record: Dict[str, object],
        capability: object,
        epoch: int,
        managed_capability: object,
        service_capability: object,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        """Append cancellation truth reconstructed inside one live evaluation."""

        if (
            capability is None
            or capability is not self.__cancellation_capability
            or record.get("kind") not in CANCELLATION_KINDS
            or not self._has_evaluated_service_capability(service_capability)
        ):
            raise ProtocolRefusal(
                "cancellation_only",
                "managed cancellation requires method-local coordinator authority",
            )
        from .sequencer_epoch import _managed_append_scope

        with _managed_append_scope(managed_capability, self.root, epoch):
            return self._append_governed(
                record,
                scheduler=False,
                resolve_existing=resolve_existing,
            )

    def _append_managed_batch(
        self,
        records: Sequence[Dict[str, object]],
        epoch: int,
        capability: object,
    ) -> list[object]:
        from .sequencer_epoch import _managed_append_scope

        if not isinstance(records, (list, tuple)) or not records:
            raise ProtocolRefusal("batch_invalid", "managed batch must be nonempty")
        for record in records:
            self._authorize_public_append(record)
        with _managed_append_scope(capability, self.root, epoch):
            return self._append_governed_batch(records)

    def _admission_binding_capability_for(self, binder: object) -> object:
        from .admission import AdmissionBinder

        if not isinstance(binder, AdmissionBinder):
            raise ProtocolRefusal("admission_binder_only", "binding capability requires AdmissionBinder authority")
        if self.__admission_binding_capability is None:
            self.__admission_binding_capability = object()
        return self.__admission_binding_capability

    def _suspension_capability_for(self, controller: object) -> object:
        # The token is per-ledger, opaque, and returned only to the direct
        # controller. Managed semantic evaluation is added by the sequencer task.
        from .suspension import ApprovalSuspensionController

        if not isinstance(controller, ApprovalSuspensionController):
            raise ProtocolRefusal(
                "suspension_controller_only",
                "suspension capability requires ApprovalSuspensionController authority",
            )
        if self.__suspension_capability is None:
            self.__suspension_capability = object()
        return self.__suspension_capability

    def _spawn_group_capability_for(self, controller: object) -> object:
        """Return the per-ledger opaque append token only to the real controller."""

        from .spawn_groups import SpawnGroupController

        if not isinstance(controller, SpawnGroupController):
            raise ProtocolRefusal(
                "spawn_group_controller_only",
                "spawn capability requires SpawnGroupController authority",
            )
        if self.__spawn_group_capability is None:
            self.__spawn_group_capability = object()
        return self.__spawn_group_capability

    def _append_spawn_group(
        self,
        record: Dict[str, object],
        capability: object = None,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        if (
            capability is None
            or capability is not self.__spawn_group_capability
            or (
                record.get("kind") not in SPAWN_GROUP_KINDS
                and not (
                    record.get("kind") == "plan_amendment"
                    and record.get("schema_version") == 1
                )
            )
        ):
            raise ProtocolRefusal(
                "spawn_group_controller_only",
                "spawn capability only appends private controller records",
            )
        if self._sequencer_client is not None:
            raise ProtocolRefusal(
                "spawn_managed_evaluation_required",
                "managed spawn requires service-side semantic evaluation",
            )
        return self._append(
            record,
            scheduler=False,
            resolve_existing=resolve_existing,
        )

    def _append_managed_spawn_group(
        self,
        record: Dict[str, object],
        capability: object,
        epoch: int,
        managed_capability: object,
        service_capability: object,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        """Append one service-reconstructed spawn record under method-local authority."""

        if (
            capability is None
            or capability is not self.__spawn_group_capability
            or (
                record.get("kind") not in SPAWN_GROUP_KINDS
                and not (
                    record.get("kind") == "plan_amendment"
                    and record.get("schema_version") == 1
                )
            )
            or not self._has_evaluated_service_capability(service_capability)
        ):
            raise ProtocolRefusal(
                "spawn_group_controller_only",
                "managed spawn requires method-local controller authority",
            )
        from .sequencer_epoch import _managed_append_scope

        with _managed_append_scope(managed_capability, self.root, epoch):
            return self._append_governed(
                record,
                scheduler=False,
                resolve_existing=resolve_existing,
            )

    def _append_spawn_admission(
        self,
        record: Dict[str, object],
        capability: object = None,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        if (
            capability is None
            or capability is not self.__admission_binding_capability
            or record.get("kind") != "run_spawn_admission_enabled"
        ):
            raise ProtocolRefusal(
                "admission_binder_only",
                "spawn admission enablement requires AdmissionBinder authority",
            )
        if self._sequencer_client is not None:
            raise ProtocolRefusal(
                "spawn_managed_evaluation_required",
                "managed spawn admission requires service-side evaluation",
            )
        return self._append(
            record,
            scheduler=False,
            resolve_existing=resolve_existing,
        )

    def _append_managed_spawn_admission(
        self,
        record: Dict[str, object],
        capability: object,
        epoch: int,
        managed_capability: object,
        service_capability: object,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        if (
            capability is None
            or capability is not self.__admission_binding_capability
            or record.get("kind") != "run_spawn_admission_enabled"
            or not self._has_evaluated_service_capability(service_capability)
        ):
            raise ProtocolRefusal(
                "admission_binder_only",
                "managed spawn enablement requires method-local binder authority",
            )
        from .sequencer_epoch import _managed_append_scope

        with _managed_append_scope(managed_capability, self.root, epoch):
            return self._append_governed(
                record,
                scheduler=False,
                resolve_existing=resolve_existing,
            )

    def _append_suspension(
        self,
        record: Dict[str, object],
        capability: object = None,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        if (
            capability is None
            or capability is not self.__suspension_capability
            or record.get("kind") not in SUSPENSION_KINDS
        ):
            raise ProtocolRefusal(
                "suspension_controller_only",
                "suspension capability only appends suspension records",
            )
        if self._sequencer_client is not None:
            raise ProtocolRefusal(
                "suspension_managed_evaluation_required",
                "managed suspension requires service-side semantic evaluation",
            )
        return self._append(
            record,
            scheduler=False,
            resolve_existing=resolve_existing,
        )

    def _append_managed_suspension(
        self,
        record: Dict[str, object],
        capability: object,
        epoch: int,
        managed_capability: object,
        service_capability: object,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        """Append one controller-built record inside a validated service evaluation."""

        if (
            capability is None
            or capability is not self.__suspension_capability
            or record.get("kind") not in SUSPENSION_KINDS
            or not self._has_evaluated_service_capability(service_capability)
        ):
            raise ProtocolRefusal(
                "suspension_controller_only",
                "managed suspension requires method-local controller authority",
            )
        from .sequencer_epoch import _managed_append_scope

        with _managed_append_scope(managed_capability, self.root, epoch):
            return self._append_governed(
                record,
                scheduler=False,
                resolve_existing=resolve_existing,
            )

    def _has_evaluated_service_capability(self, capability: object) -> bool:
        return False

    def _evaluate_suspension_intent(
        self, operation: str, intent: Dict[str, object]
    ) -> Dict[str, object]:
        method = getattr(self._sequencer_client, "append_intent", None)
        if not callable(method):
            raise ProtocolRefusal(
                "sequencer_client_invalid",
                "managed suspension requires semantic intent support",
            )
        response = method(operation, intent)
        return self._canonical_client_response(response)

    def _request_spawn_intent(
        self, operation: str, intent: Dict[str, object]
    ) -> Dict[str, object]:
        method = getattr(self._sequencer_client, "append_intent", None)
        if not callable(method):
            raise ProtocolRefusal(
                "sequencer_client_invalid",
                "managed spawn requires semantic intent support",
            )
        response = method(operation, intent)
        if not isinstance(response, dict) or response.get("status") != "ok":
            raise ProtocolRefusal(
                "sequencer_response_invalid",
                "sequencer did not confirm a canonical spawn operation",
            )
        return deepcopy(response)

    def _evaluate_spawn_intent(
        self, operation: str, intent: Dict[str, object]
    ) -> Dict[str, object]:
        return self._canonical_client_response(
            self._request_spawn_intent(operation, intent)
        )

    def _append_admission_binding(self, record: Dict[str, object], capability: object = None) -> Dict[str, object]:
        if (
            capability is None
            or capability is not self.__admission_binding_capability
            or record.get("kind") not in ADMISSION_BINDING_KINDS
        ):
            raise ProtocolRefusal("admission_binder_only", "admission capability only appends run admission bindings")
        if self._sequencer_client is not None:
            return self._append_client_intent(
                "admission_binding", record, owner_capability=capability
            )
        return self._append(record, scheduler=False)

    def _scheduler_capability_for(self, scheduler: object) -> object:
        # The token is per-ledger, opaque, and returned only to a real scheduler.
        # It is intentionally neither a public constant nor a caller-supplied flag.
        from .scheduler import RunScheduler

        if not isinstance(scheduler, RunScheduler):
            raise ProtocolRefusal("scheduler_only", "scheduler capability requires RunScheduler authority")
        if self.__scheduler_capability is None:
            self.__scheduler_capability = object()
        return self.__scheduler_capability

    def _append_scheduler(self, record: Dict[str, object], capability: object = None) -> Dict[str, object]:
        if capability is None or capability is not self.__scheduler_capability or record.get("kind") not in ATTEMPT_KINDS:
            raise ProtocolRefusal("scheduler_only", "scheduler capability only appends attempt and retry records")
        if self._sequencer_client is not None:
            return self._append_client_intent(
                "scheduler", record, owner_capability=capability
            )
        return self._append(record, scheduler=True)

    def _cancellation_capability_for(self, coordinator: object) -> object:
        from .cancellation import CancellationCoordinator

        if not isinstance(coordinator, CancellationCoordinator):
            raise ProtocolRefusal("cancellation_only", "cancellation capability requires CancellationCoordinator authority")
        if self.__cancellation_capability is None:
            self.__cancellation_capability = object()
        return self.__cancellation_capability

    def _append_cancellation(
        self,
        record: Dict[str, object],
        capability: object = None,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        if capability is None or capability is not self.__cancellation_capability or record.get("kind") not in CANCELLATION_KINDS:
            raise ProtocolRefusal("cancellation_only", "cancellation capability only appends cancellation records")
        if self._sequencer_client is not None:
            return self._append_client_intent(
                "cancellation", record, owner_capability=capability
            )
        return self._append(
            record,
            scheduler=False,
            resolve_existing=resolve_existing,
        )

    def _supervisor_capability_for(self, supervisor: object) -> object:
        from .cancellation import FloatiSupervisor

        if not isinstance(supervisor, FloatiSupervisor):
            raise ProtocolRefusal("supervisor_only", "supervisor capability requires FloatiSupervisor authority")
        if self.__supervisor_capability is None:
            self.__supervisor_capability = object()
        return self.__supervisor_capability

    def _append_supervisor(self, record: Dict[str, object], capability: object = None) -> Dict[str, object]:
        if capability is None or capability is not self.__supervisor_capability or record.get("kind") not in SUPERVISOR_KINDS:
            raise ProtocolRefusal("supervisor_only", "supervisor capability only appends Floati orphaning records")
        if self._sequencer_client is not None:
            return self._append_client_intent(
                "supervisor", record, owner_capability=capability
            )
        return self._append(record, scheduler=False)

    def _capability_binding_capability_for(self, binder: object) -> object:
        from .capability_binding import CapabilityBinder

        if not isinstance(binder, CapabilityBinder):
            raise ProtocolRefusal("capability_binder_only", "binding capability requires CapabilityBinder authority")
        if self.__capability_binding_capability is None:
            self.__capability_binding_capability = object()
        return self.__capability_binding_capability

    def _append_capability_set(self, record: Dict[str, object], capability: object = None) -> Dict[str, object]:
        if (
            capability is None
            or capability is not self.__capability_binding_capability
            or record.get("kind") not in CAPABILITY_BINDING_KINDS
        ):
            raise ProtocolRefusal("capability_binder_only", "binder capability only appends capability snapshots")
        if self._sequencer_client is not None:
            return self._append_client_intent(
                "capability_binding", record, owner_capability=capability
            )
        return self._append(record, scheduler=False)

    def _append_capability_dispatch(
        self,
        record: Dict[str, object],
        policy: object,
        capability: object = None,
    ) -> Dict[str, object]:
        if (
            capability is None
            or capability is not self.__capability_binding_capability
            or record.get("kind") != "dispatch_decision"
            or record.get("schema_version") != 1
        ):
            raise ProtocolRefusal("capability_binder_only", "binder capability only appends v1 dispatches")
        if self._sequencer_client is not None:
            return self._append_client_intent(
                "capability_dispatch",
                record,
                policy=policy,
                owner_capability=capability,
            )
        return self._append(record, scheduler=False, dispatch_policy=policy)

    def _append_client_intent(
        self,
        owner: str,
        record: Dict[str, object],
        *,
        policy: object = None,
        owner_capability: object = None,
    ) -> Dict[str, object]:
        authorized = getattr(
            self._sequencer_client, "_append_authorized_intent", None
        )
        if callable(authorized):
            response = authorized(owner, record, owner_capability, policy)
            canonical = response.get("record") if isinstance(response, dict) else None
            if not isinstance(canonical, dict):
                raise ProtocolRefusal(
                    "sequencer_response_invalid",
                    "sequencer did not confirm a canonical run record",
                )
            return canonical
        method = getattr(self._sequencer_client, "append_intent", None)
        if not callable(method):
            raise ProtocolRefusal(
                "sequencer_client_invalid", "sequencer client cannot send typed intents"
            )
        response = method(owner, record, policy)
        canonical = response.get("record") if isinstance(response, dict) else None
        if not isinstance(canonical, dict):
            raise ProtocolRefusal(
                "sequencer_response_invalid",
                "sequencer did not confirm a canonical run record",
            )
        return canonical

    def _append(
        self,
        record: Dict[str, object],
        *,
        scheduler: bool,
        dispatch_policy: object = None,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        if self._sequencer_client is not None:
            response = self._sequencer_client.append(record)
            canonical = response.get("record") if isinstance(response, dict) else None
            if not isinstance(canonical, dict):
                raise ProtocolRefusal(
                    "sequencer_response_invalid",
                    "sequencer did not confirm a canonical run record",
                )
            return canonical
        from .sequencer_epoch import DirectWriterLease

        with DirectWriterLease(self.root):
            return self._append_governed(
                record,
                scheduler=scheduler,
                dispatch_policy=dispatch_policy,
                resolve_existing=resolve_existing,
            )

    def _append_governed(
        self,
        record: Dict[str, object],
        *,
        scheduler: bool,
        dispatch_policy: object = None,
        resolve_existing: Optional[
            Callable[[RunProjection, Dict[str, object]], Optional[Dict[str, object]]]
        ] = None,
    ) -> Dict[str, object]:
        record = _detach_run_record(record)
        if record.get("kind") == "run_created" and isinstance(record.get("dependency_edges"), list):
            record = dict(record)
            record["dependency_edges"] = [
                dict(edge, requires=edge.get("requires", "accepted"), failure_policy=edge.get("failure_policy", "fail_run"))
                if isinstance(edge, dict) else edge for edge in record["dependency_edges"]
            ]
        record = validate_record(
            record, self.root.tenant_id, RUN_KINDS, integrity=False
        )
        effect_snapshots: list[object] = []

        def decide(snapshot: RunStoreSnapshot):
            durable = snapshot.lookup(str(record["id"]))
            if durable is not None:
                if durable.record == record and record["kind"] != "result_accepted":
                    return durable.record, None
                if resolve_existing is None and record["kind"] != "result_accepted":
                    raise ProtocolRefusal(
                        "duplicate_record_id", "record id cannot change its semantic payload"
                    )
            # Receipt proof must be sampled under this ledger's append lock:
            # another process can otherwise append a receipt-backed result
            # between a pre-lock snapshot and this physical-order replay.
            receipts = WorkerReceipts(self.root).records()
            projection = RunProjection.empty(
                receipts, effect_projection=effect_snapshots[0]
            )
            for physical_position, prior in enumerate(snapshot.iter_records(), start=1):
                projection.apply(prior, physical_position=physical_position, integrity=False)
            if record["kind"] == "result_accepted":
                prior_acceptance = projection._canonical_acceptance_retry(record)
                if prior_acceptance is not None:
                    return prior_acceptance, None
            if resolve_existing is not None:
                existing = resolve_existing(projection, record)
                if existing is not None:
                    if durable is not None and existing != durable.record:
                        raise ProtocolRefusal(
                            "duplicate_record_id",
                            "record id cannot change its semantic payload",
                        )
                    return existing, None
            if durable is not None:
                raise ProtocolRefusal(
                    "duplicate_record_id", "record id cannot change its semantic payload"
                )
            if dispatch_policy is not None:
                from .run_limits import RunLimitGate

                run = projection.run(str(record["run_id"]))
                bound_snapshot = run["capability_sets"].get(record["attempt_id"])
                if bound_snapshot is None or bound_snapshot["id"] != record.get("capability_set_bound_id"):
                    raise ProtocolRefusal(
                        "capability_snapshot_missing",
                        "v1 dispatch requires its exact prior capability snapshot",
                    )
                if bound_snapshot["id"] in run["capability_set_consumers"]:
                    raise ProtocolRefusal(
                        "capability_snapshot_consumed",
                        "capability snapshot can authorize one dispatch",
                    )
                RunLimitGate.check_dispatch(projection, bound_snapshot, dispatch_policy)
            projection.apply(
                record, physical_position=snapshot.total_records + 1,
                integrity=False, require_current_effect_binding=True,
            )
            return record, record

        def append_effect_snapshot(effects: object) -> Dict[str, object]:
            effect_snapshots.append(effects)
            return deepcopy(self._store.transact(decide))

        appended = self._with_cross_ledger_snapshot(
            append_effect_snapshot, already_guarded=False,
            exclusive=record["kind"] in _EFFECT_ACCEPTANCE_FENCE_KINDS,
        )
        if not isinstance(appended, dict):
            raise IntegrityFailure(
                "effect_evidence_invalid", "cross-ledger append returned invalid state"
            )
        return appended

    def _append_governed_batch(
        self,
        raw_records: Sequence[Dict[str, object]],
    ) -> list[object]:
        if type(raw_records) is not list and type(raw_records) is not tuple:
            raise ProtocolRefusal(
                "batch_invalid", "governed batch must be an exact sequence"
            )
        records: list[Dict[str, object]] = []
        for raw in raw_records:
            record = _detach_run_record(raw)
            if record.get("kind") == "run_created" and isinstance(
                record.get("dependency_edges"), list
            ):
                record = dict(record)
                record["dependency_edges"] = [
                    dict(
                        edge,
                        requires=edge.get("requires", "accepted"),
                        failure_policy=edge.get("failure_policy", "fail_run"),
                    )
                    if isinstance(edge, dict)
                    else edge
                    for edge in record["dependency_edges"]
                ]
            record = validate_record(
                record, self.root.tenant_id, RUN_KINDS, integrity=False
            )
            records.append(record)
        outcomes: list[object] = [None] * len(records)
        cached = self._managed_projection_cache
        self._managed_projection_cache = None
        completed_projection: list[RunProjection] = []
        effect_snapshots: list[object] = []

        def decide(snapshot: RunStoreSnapshot):
            receipts = WorkerReceipts(self.root).records()
            receipt_digest = hashlib.sha256(
                json.dumps(
                    receipts,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                cached is not None
                and not effect_snapshots[0]._records
                and cached[0] == snapshot.total_records
                and cached[1] == snapshot.prefix_digest
                and cached[2] == receipt_digest
            ):
                projection = cached[3]
            else:
                projection = RunProjection.empty(
                    receipts, effect_projection=effect_snapshots[0]
                )
                for position, prior in enumerate(snapshot.iter_records(), start=1):
                    projection.apply(
                        prior,
                        physical_position=position,
                        integrity=False,
                        retain_record=False,
                    )
            accepted: list[Dict[str, object]] = []
            for index, record in enumerate(records):
                if record["id"] in projection._seen_ids:
                    outcomes[index] = ProtocolRefusal(
                        "duplicate_record_id", "managed batch requires new operation ids"
                    )
                    continue
                if record["kind"] == "result_accepted":
                    prior_acceptance = projection._canonical_acceptance_retry(record)
                    if prior_acceptance is not None:
                        outcomes[index] = prior_acceptance
                        continue
                try:
                    projection.apply(
                        record,
                        physical_position=snapshot.total_records + len(accepted) + 1,
                        integrity=False,
                        retain_record=False,
                        require_current_effect_binding=True,
                    )
                except (ProtocolRefusal, IntegrityFailure) as exc:
                    outcomes[index] = exc
                    projection = RunProjection.empty(
                        receipts, effect_projection=effect_snapshots[0]
                    )
                    for position, prior in enumerate(snapshot.iter_records(), start=1):
                        projection.apply(
                            prior,
                            physical_position=position,
                            integrity=False,
                            retain_record=False,
                        )
                    for offset, prior in enumerate(accepted, start=1):
                        projection.apply(
                            prior,
                            physical_position=snapshot.total_records + offset,
                            integrity=False,
                            retain_record=False,
                        )
                    continue
                accepted.append(record)
                outcomes[index] = record
            completed_projection[:] = [projection]
            completed_receipt_digest[:] = [receipt_digest]
            return list(accepted), list(accepted)

        completed_receipt_digest: list[str] = []
        def append_effect_snapshot(effects: object) -> list[object]:
            effect_snapshots.append(effects)
            _appended, (prefix_count, prefix_digest) = (
                self._store._transact_batch_identity(decide)
            )
            if (
                completed_projection
                and completed_receipt_digest
                and not effect_snapshots[0]._records
            ):
                if prefix_count == completed_projection[0]._last_position:
                    self._managed_projection_cache = (
                        prefix_count,
                        prefix_digest,
                        completed_receipt_digest[0],
                        completed_projection[0],
                    )
            return [
                deepcopy(outcome) if isinstance(outcome, dict) else outcome
                for outcome in outcomes
            ]

        appended = self._with_cross_ledger_snapshot(
            append_effect_snapshot, already_guarded=False,
            exclusive=any(
                record["kind"] in _EFFECT_ACCEPTANCE_FENCE_KINDS
                for record in records
            ),
        )
        if not isinstance(appended, list):
            raise IntegrityFailure(
                "effect_evidence_invalid", "cross-ledger batch returned invalid state"
            )
        return appended


def _cancel_closure(item_ids: Sequence[str], edges: Sequence[DependencyEdge], item_id: object) -> list[str]:
    if item_id is None:
        return sorted(item_ids)
    closure = {str(item_id)}
    pending = [str(item_id)]
    outgoing: Dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge.source, []).append(edge.target)
    while pending:
        source = pending.pop()
        for target in outgoing.get(source, []):
            if target not in closure:
                closure.add(target)
                pending.append(target)
    return sorted(closure)


def _cancel_scope_items(
    run: Dict[str, object],
    edges: Sequence[DependencyEdge],
    item_id: object,
) -> list[str]:
    """Union immutable activated membership for every covered group parent."""

    covered = set(_cancel_closure(run["item_ids"], edges, item_id))
    changed = True
    while changed:
        changed = False
        for group in run["spawn_groups"].values():
            if (
                group["state"] not in {"activated", "closed"}
                or group["created"]["parent_item_id"] not in covered
            ):
                continue
            widened = covered | set(group["member_item_ids"])
            if widened != covered:
                covered = widened
                changed = True
    return sorted(covered)

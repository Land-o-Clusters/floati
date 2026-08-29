"""Replay-derived runtime dispatch ceilings for integrity-bound runs."""

from __future__ import annotations

from typing import Dict, List

from .errors import ProtocolRefusal
from .policy import RepositoryPolicy, validate_repository_policy_integrity


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


class RunLimitGate:
    """Check one candidate snapshot against physical active reservations."""

    @staticmethod
    def check_dispatch(
        projection: object,
        snapshot: Dict[str, object],
        policy: RepositoryPolicy,
    ) -> None:
        from .runtruth import RunProjection

        if not isinstance(projection, RunProjection):
            _refuse("run_projection_required", "dispatch limits require a canonical run projection")
        if not isinstance(snapshot, dict):
            _refuse("capability_snapshot_required", "dispatch limits require one bound snapshot")
        policy = validate_repository_policy_integrity(policy)
        try:
            run = projection.run(str(snapshot["run_id"]))
            binding = run["admission_binding"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolRefusal(
                "run_admission_policy_mismatch",
                "dispatch evidence does not name one admission-bound run",
            ) from exc
        if binding.get("status") != "bound":
            _refuse(
                "run_admission_policy_mismatch",
                "v1 dispatch requires durable admission semantics",
            )
        run_policy = run.get("policy")
        if (
            binding.get("plan_digest") != run.get("plan_digest")
            or binding.get("policy_digest") != policy.digest
            or not isinstance(run_policy, dict)
            or run_policy.get("policy_digest") != policy.digest
            or snapshot.get("policy_digest") != policy.digest
        ):
            _refuse(
                "run_admission_policy_mismatch",
                "bound plan and policy evidence must equal current dispatch evidence",
            )

        workers = {
            row["node_id"]: row["worker_profile"]
            for row in binding["workers"]
        }
        items = {row["item_id"]: row for row in binding["items"]}
        chosen_worker = snapshot.get("chosen_worker")
        item_id = snapshot.get("item_id")
        worker_profile = workers.get(chosen_worker)
        item = items.get(item_id)
        profile = policy.worker_profiles.get(worker_profile)
        routes = [
            route
            for route in policy.routes
            if route.rank == snapshot.get("routing_rank")
            and route.worker_profile == worker_profile
            and item is not None
            and route.capability_selector == item["capability_selector"]
        ]
        if item is None or profile is None or len(routes) != 1:
            _refuse(
                "run_admission_policy_mismatch",
                "bound worker profile and selector must resolve in current policy",
            )

        candidate_run_id = str(snapshot["run_id"])
        active: List[Dict[str, object]] = []
        reservations: List[Dict[str, object]] = []
        for source_run_id, source_run in projection._runs.items():
            source_binding = source_run["admission_binding"]
            source_items = {
                row["item_id"]: row
                for row in source_binding.get("items", [])
            } if source_binding.get("status") == "bound" else {}
            for attempt_id, dispatch in source_run["dispatches"].items():
                state = source_run["attempts"].get(attempt_id)
                if state is None or state["terminal"] is not None:
                    continue
                if source_run_id == candidate_run_id:
                    active.append(dispatch)
                reservations.append({
                    "run_id": source_run_id,
                    "binding_status": source_binding.get("status"),
                    "dispatch": dispatch,
                    "item": source_items.get(dispatch["item_id"]),
                })
        effective_run_limit = min(
            binding["max_active_attempts"], policy.limits["max_active_attempts"]
        )
        if len(active) >= effective_run_limit:
            _refuse(
                "run_concurrency_exhausted",
                "active dispatched attempts reached the bound run limit",
            )

        workspace_key = item["workspace_key"]
        concurrency_key = item["concurrency_key"]
        for reservation in reservations:
            dispatch = reservation["dispatch"]
            active_item_id = dispatch["item_id"]
            if (
                reservation["run_id"] == candidate_run_id
                and active_item_id == item_id
            ):
                continue
            active_item = reservation["item"]
            if active_item is None:
                if reservation["binding_status"] == "bound":
                    _refuse(
                        "run_admission_policy_mismatch",
                        "active dispatch item is absent from admission semantics",
                    )
                continue
            if workspace_key and active_item["workspace_key"] == workspace_key:
                _refuse(
                    "workspace_key_busy",
                    "another active item owns the integrity-bound workspace key",
                )
            if concurrency_key and active_item["concurrency_key"] == concurrency_key:
                _refuse(
                    "concurrency_key_busy",
                    "another active item owns the integrity-bound concurrency key",
                )

        worker_active = sum(
            reservation["dispatch"]["chosen_worker"] == chosen_worker
            for reservation in reservations
        )
        if worker_active >= profile.max_concurrency:
            _refuse(
                "worker_concurrency_exhausted",
                "chosen worker reached its current profile concurrency limit",
            )

    @staticmethod
    def check_effect_spend(
        projection: object,
        run_id: object,
        item_id: object,
        attempt_id: object,
        evidence: object,
    ) -> None:
        """Require complete measured spend to fit intent, attempt, and run bounds."""

        from .effects import EffectAcceptanceEvidence
        from .runtruth import RunProjection

        if not isinstance(projection, RunProjection):
            _refuse(
                "run_projection_required",
                "effect spend requires a canonical run projection",
            )
        if not isinstance(evidence, EffectAcceptanceEvidence):
            _refuse(
                "effect_evidence_invalid",
                "effect spend requires immutable acceptance evidence",
            )
        try:
            run = projection.run(str(run_id))
            state = run["attempts"][attempt_id]
        except (KeyError, TypeError) as exc:
            raise ProtocolRefusal(
                "effect_attempt_missing",
                "effect spend must name one current durable attempt",
            ) from exc
        if state["opened"]["item_id"] != item_id:
            _refuse(
                "effect_attempt_missing",
                "effect spend item must equal the durable attempt item",
            )
        admission = run.get("admission_binding")
        if not isinstance(admission, dict) or admission.get("status") != "bound":
            _refuse(
                "effect_run_admission_missing",
                "effect spend requires durable run budget reservations",
            )
        run_bound = {
            str(row["budget_id"]): int(row["amount"])
            for row in admission.get("budget_reservations", [])
        }
        attempt_bound = dict(run_bound)
        for group in run.get("spawn_groups", {}).values():
            admitted = group.get("admissions", {}).get(item_id)
            if admitted is None:
                continue
            attempt_bound = {
                str(row["budget_id"]): int(row["amount"])
                for row in admitted["budget_allocation"]
            }
            break
        for budget_id, amount in evidence.measured_spend:
            if (
                amount > attempt_bound.get(budget_id, -1)
                or amount > run_bound.get(budget_id, -1)
            ):
                _refuse(
                    "effect_budget_exceeded",
                    "measured effect spend exceeds attempt or run bounds",
                )


__all__ = ["RunLimitGate"]

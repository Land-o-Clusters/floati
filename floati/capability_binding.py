"""Attempt/fence-bound capability snapshots and dispatch authorization."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .capabilities import CapabilityGrantLedger
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import transact
from .policy import RepositoryPolicy, validate_repository_policy_integrity
from .records import capability_set_digest
from .runtruth import RunLedger


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CapabilityBinder:
    """The sole grant-then-run two-ledger coordinator."""

    def __init__(self, run_ledger: RunLedger, grant_ledger: CapabilityGrantLedger) -> None:
        if not isinstance(run_ledger, RunLedger) or not isinstance(grant_ledger, CapabilityGrantLedger):
            raise ProtocolRefusal("capability_binder_invalid", "binder requires canonical run and grant ledgers")
        if run_ledger.root != grant_ledger.root:
            raise ProtocolRefusal("capability_root_mismatch", "binder ledgers must share one exact root")
        self.run_ledger = run_ledger
        self.grant_ledger = grant_ledger
        self.__binding_capability = run_ledger._capability_binding_capability_for(self)

    def bind(
        self,
        run_id: str,
        item_id: str,
        attempt_id: str,
        chosen_worker: str,
        worker_profile: str,
        policy: RepositoryPolicy,
        routing_rank: int,
        *,
        now: Optional[datetime] = None,
        _record_id: Optional[str] = None,
        _service_capability: object = None,
    ) -> Dict[str, object]:
        policy = validate_repository_policy_integrity(policy)
        current = _now(now)
        if _record_id is not None:
            from .sequencer import (
                _known_service_record_id,
                _policy_evidence,
            )

            intent = {
                "run_id": run_id,
                "item_id": item_id,
                "attempt_id": attempt_id,
                "chosen_worker": chosen_worker,
                "worker_profile": worker_profile,
                "policy": _policy_evidence(policy),
                "routing_rank": routing_rank,
            }
            if (
                _known_service_record_id(
                    self.run_ledger.root,
                    "capability_binding_evaluation",
                    intent,
                    _record_id,
                )
                and not self.run_ledger._has_evaluated_service_capability(
                    _service_capability
                )
            ):
                raise ProtocolRefusal(
                    "evaluated_service_only",
                    "service-derived capability identity requires live service authority",
                )
        remote = getattr(self.run_ledger._sequencer_client, "bind_capability", None)
        if callable(remote):
            return self.run_ledger._canonical_client_response(
                remote(
                    run_id,
                    item_id,
                    attempt_id,
                    chosen_worker,
                    worker_profile,
                    policy,
                    routing_rank,
                    _timestamp(current),
                )
            )
        run = self.run_ledger.project().run(run_id)
        if run["policy"] is None or run["policy"]["policy_digest"] != policy.digest:
            raise ProtocolRefusal("capability_policy_mismatch", "binder policy must equal run_policy_bound")
        state = run["attempts"].get(attempt_id)
        if state is None or state["opened"]["item_id"] != item_id:
            raise ProtocolRefusal("attempt_missing", "capability binding requires the exact open item attempt")
        if state["started"] is not None or state["terminal"] is not None:
            raise ProtocolRefusal("capability_attempt_closed", "capability binding must precede attempt start")
        routes = [
            route for route in policy.routes
            if route.rank == routing_rank and route.worker_profile == worker_profile
        ]
        if len(routes) != 1:
            raise ProtocolRefusal("capability_route_unresolved", "policy must resolve one route for worker profile and rank")
        route = routes[0]
        required = set(policy.capability_selectors[route.capability_selector].all_of)

        def bind_under_grant_lock(records: List[Dict[str, object]]):
            effective = self.grant_ledger._project_records(
                records, chosen_worker, policy.digest, current, integrity=True
            )
            observed_names = {name for name, _, _ in effective.grant_triples}
            if not required <= observed_names:
                raise ProtocolRefusal(
                    "capability_selector_unsatisfied",
                    "effective grants do not cover the complete policy selector",
                )
            grants = [
                {
                    "capability_name": name,
                    "grant_id": grant_id,
                    "physical_position": position,
                }
                for name, grant_id, position in effective.grant_triples
            ]
            record: Dict[str, object] = {
                "schema_version": 1,
                "id": _record_id or "capability-set-bound-" + uuid7_hex(),
                "tenant_id": self.run_ledger.root.tenant_id,
                "timestamp": _timestamp(current),
                "kind": "capability_set_bound",
                "run_id": run_id,
                "item_id": item_id,
                "attempt_id": attempt_id,
                "fence_token": state["opened"]["fence_token"],
                "chosen_worker": chosen_worker,
                "policy_digest": policy.digest,
                "routing_rank": routing_rank,
                "evaluated_at_testimony": effective.evaluated_at_testimony,
                "grant_ledger_high_watermark": effective.high_watermark,
                "effective_grants": grants,
                "capability_digest": capability_set_digest(grants),
            }
            appended = self.run_ledger._append_capability_set(record, self.__binding_capability)
            return appended, None

        return transact(
            self.grant_ledger.root,
            self.grant_ledger.relative_path,
            bind_under_grant_lock,
            allowed_kinds={"capability_grant", "capability_revoked"},
        )

    def dispatch(
        self,
        capability_set_bound_id: str,
        eligible_workers: Sequence[str],
        reason_code: str,
        policy: RepositoryPolicy,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        policy = validate_repository_policy_integrity(policy)
        current = _now(now)
        projection = self.run_ledger.project()
        found = None
        found_run = None
        for run_id, run in projection._runs.items():
            for snapshot in run["capability_sets"].values():
                if snapshot["id"] == capability_set_bound_id:
                    found, found_run = snapshot, run
                    break
            if found is not None:
                break
        if found is None or found_run is None:
            raise ProtocolRefusal("capability_snapshot_unknown", "dispatch requires one exact bound snapshot")
        if capability_set_bound_id in found_run["capability_set_consumers"]:
            raise ProtocolRefusal("capability_snapshot_consumed", "capability snapshot can authorize one dispatch")
        opened = found_run["attempts"][found["attempt_id"]]["opened"]
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "run-dispatch-decision-" + uuid7_hex(),
            "tenant_id": self.run_ledger.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "dispatch_decision",
            "run_id": found["run_id"],
            "item_id": found["item_id"],
            "attempt_id": found["attempt_id"],
            "eligible_workers": list(eligible_workers),
            "chosen_worker": found["chosen_worker"],
            "capability_digest": found["capability_digest"],
            "reason_code": reason_code,
            "policy_digest": found["policy_digest"],
            "routing_rank": found["routing_rank"],
            "scheduler_epoch": opened["scheduler_epoch"],
            "capability_set_bound_id": capability_set_bound_id,
        }
        spawn_policy = found_run["attempt_spawn_policy"].get(found["attempt_id"])
        if spawn_policy is not None and spawn_policy.get("id") is not None:
            record["adapter"] = spawn_policy["adapter"]
            record["attempt_spawn_policy_id"] = spawn_policy["id"]
        return self.run_ledger._append_capability_dispatch(
            record, policy, self.__binding_capability
        )


__all__ = ["CapabilityBinder", "capability_set_digest"]

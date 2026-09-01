"""Private, recoverable controller for bounded schema-v1 spawn groups."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, Optional, Sequence

from .admission import AdmissionEvaluator, AdmissionPlan
from .errors import ProtocolRefusal
from .host_paths import worker_workspace_root
from .ids import uuid7_hex
from .policy import RepositoryPolicy, validate_repository_policy_integrity
from .records import validate_record
from .runtruth import (
    RUN_KINDS,
    RunLedger,
    _cancel_request_covers_item,
    _spawn_join_decision,
    _spawn_remaining_at_record,
)


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        _refuse("time_invalid", "spawn evaluation requires an aware UTC time")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal(
            "timestamp_invalid", "spawn deadline must be canonical UTC testimony"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _refuse("timestamp_invalid", "spawn deadline must include UTC authority")
    return parsed.astimezone(timezone.utc)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "spawn_input_invalid", "spawn semantics must form canonical I-JSON"
        ) from exc


def _normalize_spawn_group_numbers(
    request: Dict[str, object],
) -> Dict[str, object]:
    """Normalize only integral JSON numbers accepted by spawn record semantics."""

    normalized = deepcopy(request)

    def integral(value: object) -> object:
        if (
            isinstance(value, float)
            and math.isfinite(value)
            and value.is_integer()
        ):
            return int(value)
        return value

    for field in ("max_children", "max_depth", "required_count"):
        if field in normalized and normalized[field] is not None:
            normalized[field] = integral(normalized[field])
    budgets = normalized.get("aggregate_budget")
    if isinstance(budgets, list):
        for row in budgets:
            if isinstance(row, dict) and "amount" in row:
                row["amount"] = integral(row["amount"])
    children = normalized.get("children")
    if isinstance(children, list):
        for child in children:
            if not isinstance(child, dict):
                continue
            if "depth" in child:
                child["depth"] = integral(child["depth"])
            allocations = child.get("budget_allocation")
            if isinstance(allocations, list):
                for row in allocations:
                    if isinstance(row, dict) and "amount" in row:
                        row["amount"] = integral(row["amount"])
    return normalized


def _semantic_uuid(domain: str, value: object) -> str:
    digest = bytearray(hashlib.sha256(domain.encode("ascii") + b"\0" + _canonical(value)).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x70
    digest[8] = (digest[8] & 0x3F) | 0x80
    return digest.hex()


def _spawn_admission_digest(previous: str, plan: AdmissionPlan) -> str:
    canonical = plan.canonical()
    return hashlib.sha256(_canonical({
        "previous_admission_digest": previous,
        "workers": canonical["workers"],
        "max_active_attempts": canonical["max_active_attempts"],
        "budget_reservations": canonical["budget_reservations"],
        "items": canonical["items"],
    })).hexdigest()


def _child_item(child: Dict[str, object]) -> Dict[str, object]:
    return {
        "item_id": child["item_id"],
        "contract": deepcopy(child["task_contract"]),
        "capability_selector": child["capability_selector"],
        "requires_cancellation": child["requires_cancellation"],
        "requires_callback": child["requires_callback"],
        "workspace_key": child["workspace_key"],
        "concurrency_key": child["concurrency_key"],
        "retry_class": child["retry_class"],
        "effect_safety": child["effect_safety"],
        "merge_gate": child["merge_gate"],
    }


def _without_common(record: Dict[str, object]) -> Dict[str, object]:
    return {
        key: deepcopy(value)
        for key, value in record.items()
        if key not in {"id", "tenant_id", "timestamp"}
    }


def _admission_digest_is_ancestor(
    run: Dict[str, object], candidate: object,
) -> bool:
    """Prove one immutable amendment digest remains on the canonical chain."""

    current = run["admission_binding"].get("admission_digest")
    if candidate == current:
        return True
    predecessors = {
        record["admission_digest"]: record["previous_admission_digest"]
        for record in run["records"]
        if record.get("kind") == "plan_amendment"
        and record.get("schema_version") == 1
    }
    seen: set[object] = set()
    while current in predecessors and current not in seen:
        seen.add(current)
        current = predecessors[current]
        if current == candidate:
            return True
    return False


class SpawnGroupController:
    """Own private spawn records and evaluate each append from durable truth."""

    def __init__(self, ledger: RunLedger, policy: RepositoryPolicy) -> None:
        if not isinstance(ledger, RunLedger):
            _refuse("run_ledger_required", "spawn controller requires RunLedger")
        self.ledger = ledger
        self.policy = validate_repository_policy_integrity(policy)
        self.__capability = ledger._spawn_group_capability_for(self)
        ledger._spawn_group_controller = self

    def _managed_intent(
        self,
        operation: str,
        fields: Dict[str, object],
        *,
        multiple: bool = False,
    ):
        """Send closed semantics; the service reconstructs every private record."""

        if self.ledger._sequencer_client is None:
            return None
        from .sequencer import _policy_evidence

        validate_repository_policy_integrity(self.policy)
        intent = {**deepcopy(fields), "policy": _policy_evidence(self.policy)}
        if not multiple:
            return self.ledger._evaluate_spawn_intent(operation, intent)
        amendment = self.ledger._evaluate_spawn_intent(operation, intent)
        group_id = amendment.get("spawn_group_id")
        if not isinstance(group_id, str):
            _refuse(
                "sequencer_response_invalid",
                "group creation requires one canonical activation record",
            )
        group = self.ledger.project().run(str(intent["run_id"]))["spawn_groups"].get(
            group_id
        )
        if group is None or group.get("amendment", {}).get("id") != amendment["id"]:
            _refuse(
                "sequencer_response_invalid",
                "group creation response must match same-root durable truth",
            )
        return deepcopy(group["created"]), deepcopy(amendment)

    def _begin_worker_launch(
        self, run_id: str, parent_attempt_id: str, adapter: str,
    ) -> object:
        """Refuse the former durable-state-only mint seam."""

        _refuse(
            "descendant_launch_capability_required",
            "launch authority is created only around a started WorkerRunner process and pipe",
        )

    def _end_worker_launch(self, capability: object) -> None:
        _refuse(
            "descendant_launch_capability_required",
            "launch authority lifecycle is owned only by WorkerRunner",
        )

    def _require_worker_launch(
        self, capability: object, run_id: str, parent_attempt_id: str,
        adapter: str, authorizer: object,
    ) -> tuple[object, object, int, object]:
        from multiprocessing.connection import Connection
        from multiprocessing.process import BaseProcess
        from types import FrameType

        from .worker_bootstrap_protocol import BootstrapChannel
        from .worker_exec import SpawnedWorkerProcess
        from .workers import WorkerRunner

        if not callable(authorizer):
            _refuse(
                "descendant_launch_capability_required",
                "descendant testimony requires the live WorkerRunner launch capability",
            )
        try:
            launch = authorizer(
                capability, self, run_id, parent_attempt_id, adapter,
            )
        except Exception:
            launch = None
        if not isinstance(launch, tuple) or len(launch) != 4:
            _refuse(
                "descendant_launch_capability_required",
                "descendant testimony requires the live WorkerRunner launch capability",
            )
        process, connection, owner_pid, launch_frame = launch
        active_stack = False
        current_frame = sys._getframe()
        while current_frame is not None:
            if current_frame is launch_frame:
                active_stack = True
                break
            current_frame = current_frame.f_back
        frame_locals = (
            launch_frame.f_locals
            if isinstance(launch_frame, FrameType)
            else {}
        )
        runner = frame_locals.get("self")
        spawn_context = frame_locals.get("spawn_context")
        legacy_transport = (
            isinstance(process, BaseProcess)
            and getattr(process, "_parent_pid", None) == owner_pid
            and process.pid is not None
            and getattr(process, "_popen", None) is not None
            and process.is_alive()
            and isinstance(connection, Connection)
            and not connection.closed
        )
        exec_transport = (
            type(process) is SpawnedWorkerProcess
            and getattr(process, "_parent_pid", None) == owner_pid
            and type(process.pid) is int
            and process.is_alive()
            and type(connection) is BootstrapChannel
            and connection._socket.fileno() >= 0
        )
        if (
            type(owner_pid) is not int
            or owner_pid != os.getpid()
            or not isinstance(launch_frame, FrameType)
            or launch_frame.f_code is not WorkerRunner.run.__code__
            or not active_stack
            or not isinstance(runner, WorkerRunner)
            or runner.spawn_controller is not self
            or frame_locals.get("process") is not process
            or frame_locals.get("parent") is not connection
            or frame_locals.get("process_started") is not True
            or frame_locals.get("launch_identity") is not capability
            or not isinstance(spawn_context, dict)
            or (
                spawn_context.get("run_id"),
                spawn_context.get("attempt_id"),
                spawn_context.get("adapter"),
            ) != (run_id, parent_attempt_id, adapter)
            or not (legacy_transport or exec_transport)
        ):
            _refuse(
                "descendant_launch_capability_required",
                "descendant testimony requires the exact live WorkerRunner process and pipe",
            )
        run = self.ledger.project().run(run_id)
        attempt = run["attempts"].get(parent_attempt_id)
        policy = run["attempt_spawn_policy"].get(parent_attempt_id)
        dispatch = run["dispatches"].get(parent_attempt_id)
        if (
            attempt is None or attempt["started"] is None
            or attempt["terminal"] is not None or policy is None or dispatch is None
            or policy.get("adapter") != adapter
            or dispatch.get("adapter") != adapter
            or dispatch.get("attempt_spawn_policy_id") != policy.get("id")
        ):
            _refuse(
                "descendant_launch_invalid",
                "descendant testimony requires the still-live durable launch",
            )
        return process, connection, owner_pid, launch_frame

    def _require_worker_pipe_receive(
        self, capability: object, run_id: str, parent_attempt_id: str,
        adapter: str, authorizer: object, expected_status: str,
        expected_descendant: Optional[tuple[object, object, object]] = None,
    ) -> object:
        """Require the exact live WorkerRunner receive that consumed testimony."""

        from .workers import WorkerRunner

        _process, connection, _owner_pid, launch_frame = self._require_worker_launch(
            capability, run_id, parent_attempt_id, adapter, authorizer,
        )
        runner = launch_frame.f_locals.get("self")
        try:
            direct_receive_frame = sys._getframe(2)
        except ValueError:
            direct_receive_frame = None
        current_frame = sys._getframe()
        while current_frame is not None:
            if current_frame.f_code is WorkerRunner._receive.__code__:
                if (
                    current_frame.f_locals.get("self") is runner
                    and current_frame.f_locals.get("connection") is connection
                ):
                    if expected_status == "descendant":
                        snapshot = current_frame.f_locals.get(
                            "descendant_snapshot"
                        )
                        if (
                            isinstance(snapshot, tuple)
                            and len(snapshot) == 3
                            and snapshot == expected_descendant
                            and current_frame is direct_receive_frame
                        ):
                            return snapshot
                    else:
                        received = current_frame.f_locals.get("result")
                        if (
                            isinstance(received, tuple)
                            and len(received) == 2
                            and received[0] == expected_status
                        ):
                            return received[1]
            current_frame = current_frame.f_back
        _refuse(
            "descendant_pipe_receive_required",
            "descendant testimony requires the exact just-consumed worker pipe message",
        )

    def bind_attempt_policy(
        self,
        run_id: str,
        parent_item_id: str,
        parent_attempt_id: str,
        parent_capability_set_bound_id: str,
        *,
        adapter: str,
        subagents_mode: str,
        max_children: int,
        max_depth: int,
        child_capability_ceiling: Sequence[str],
        spawn_budget_ceiling: Sequence[Dict[str, object]],
        workspace_policies: Sequence[str],
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        policy_object = validate_repository_policy_integrity(self.policy)
        managed = self._managed_intent(
            "spawn_policy_bind_evaluation",
            {
                "run_id": run_id,
                "parent_item_id": parent_item_id,
                "parent_attempt_id": parent_attempt_id,
                "parent_capability_set_bound_id": parent_capability_set_bound_id,
                "adapter": adapter,
                "subagents_mode": subagents_mode,
                "max_children": max_children,
                "max_depth": max_depth,
                "child_capability_ceiling": list(child_capability_ceiling),
                "spawn_budget_ceiling": [dict(row) for row in spawn_budget_ceiling],
                "workspace_policies": list(workspace_policies),
            },
        )
        if managed is not None:
            return managed
        current = _now(now)
        run = self.ledger.project().run(run_id)
        state = run["attempts"].get(parent_attempt_id)
        snapshot = run["capability_sets"].get(parent_attempt_id)
        if (
            state is None
            or state["opened"]["item_id"] != parent_item_id
            or snapshot is None
            or snapshot["id"] != parent_capability_set_bound_id
        ):
            _refuse(
                "capability_snapshot_missing",
                "spawn policy requires the exact prior capability snapshot",
            )
        if run["policy"] is None or run["policy"]["policy_digest"] != policy_object.digest:
            _refuse("spawn_policy_digest_mismatch", "spawn policy must equal run policy")

        ceilings = list(child_capability_ceiling)
        budgets = [dict(row) for row in spawn_budget_ceiling]
        workspaces = list(workspace_policies)
        effective = {row["capability_name"] for row in snapshot["effective_grants"]}
        admitted_budget = {
            row["budget_id"]: row["amount"]
            for row in run["admission_binding"].get("budget_reservations", [])
        }
        if not set(ceilings) <= effective:
            _refuse("spawn_capability_widening", "spawn ceiling exceeds bound grants")
        if any(row["amount"] > admitted_budget.get(row["budget_id"], 0) for row in budgets):
            _refuse("spawn_budget_widening", "spawn ceiling exceeds admitted reservations")
        if subagents_mode == "managed" and (
            max_children > min(8, policy_object.limits["max_fan_out"])
            or max_depth > min(16, policy_object.limits["max_depth"])
        ):
            _refuse(
                "spawn_policy_limit_widening",
                "spawn policy exceeds the current repository graph limits",
            )

        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "attempt-spawn-policy-bound-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "attempt_spawn_policy_bound",
            "run_id": run_id,
            "parent_item_id": parent_item_id,
            "parent_attempt_id": parent_attempt_id,
            "parent_fence_token": state["opened"]["fence_token"],
            "parent_capability_set_bound_id": parent_capability_set_bound_id,
            "adapter": adapter,
            "subagents_mode": subagents_mode,
            "max_children": max_children,
            "max_depth": max_depth,
            "child_capability_ceiling": ceilings,
            "spawn_budget_ceiling": budgets,
            "workspace_policies": workspaces,
            "bound_at_testimony": _timestamp(current),
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            existing = projection.run(run_id)["attempt_spawn_policy"].get(
                parent_attempt_id
            )
            if existing is None:
                return None
            fields = set(candidate) - {"id", "timestamp", "bound_at_testimony"}
            if any(existing[field] != candidate[field] for field in fields):
                _refuse(
                    "spawn_policy_input_divergent",
                    "spawn policy retry changed its bounded launch contract",
                )
            return deepcopy(existing)

        return self.ledger._append_spawn_group(
            record, self.__capability, resolve_existing
        )

    def create_group(
        self,
        *,
        run_id: str,
        parent_item_id: str,
        parent_attempt_id: str,
        parent_fence_token: str,
        group_key: str,
        children: Sequence[Dict[str, object]],
        dependency_edges: Sequence[Dict[str, object]],
        max_children: int,
        max_depth: int,
        child_capability_ceiling: Sequence[str],
        aggregate_budget: Sequence[Dict[str, object]],
        workspace_policy: str,
        deadline: str,
        join_mode: str,
        required_count: Optional[int],
        on_late_result: str,
        on_child_failure: str,
        cancel_remaining_after_success: bool,
        now: Optional[datetime] = None,
    ) -> tuple[Dict[str, object], Dict[str, object]]:
        validate_repository_policy_integrity(self.policy)
        request_semantics = _normalize_spawn_group_numbers({
            "run_id": run_id,
            "parent_item_id": parent_item_id,
            "parent_attempt_id": parent_attempt_id,
            "parent_fence_token": parent_fence_token,
            "group_key": group_key,
            "children": [deepcopy(row) for row in children],
            "dependency_edges": [deepcopy(row) for row in dependency_edges],
            "max_children": max_children,
            "max_depth": max_depth,
            "child_capability_ceiling": list(child_capability_ceiling),
            "aggregate_budget": [dict(row) for row in aggregate_budget],
            "workspace_policy": workspace_policy,
            "deadline": deadline,
            "join_mode": join_mode,
            "required_count": required_count,
            "on_late_result": on_late_result,
            "on_child_failure": on_child_failure,
            "cancel_remaining_after_success": cancel_remaining_after_success,
        })
        managed = self._managed_intent(
            "spawn_group_create_evaluation",
            request_semantics,
            multiple=True,
        )
        if managed is not None:
            return managed
        current = _now(now)
        child_rows = deepcopy(request_semantics["children"])
        edge_rows = deepcopy(request_semantics["dependency_edges"])
        max_children = request_semantics["max_children"]
        max_depth = request_semantics["max_depth"]
        child_capability_ceiling = request_semantics["child_capability_ceiling"]
        aggregate_budget = request_semantics["aggregate_budget"]
        required_count = request_semantics["required_count"]
        group_id = "spawn-group-created-" + _semantic_uuid(
            "slipway-spawn-group-v1", request_semantics
        )
        projection = self.ledger.project()
        run = projection.run(run_id)
        existing_id = run["spawn_group_by_parent_key"].get(
            (parent_attempt_id, group_key)
        )
        if existing_id is not None:
            group = run["spawn_groups"][existing_id]
            if existing_id != group_id:
                _refuse(
                    "spawn_group_input_divergent",
                    "group retry changed pending or activated semantics",
                )
            if group["state"] == "aborted":
                _refuse("spawn_group_aborted", "aborted group cannot activate")
            if group["state"] in {"activated", "closed"}:
                amendment = group["amendment"]
                if (
                    amendment["children"] != child_rows
                    or amendment["dependency_edges"] != edge_rows
                ):
                    _refuse(
                        "spawn_group_input_divergent",
                        "activated group retry changed immutable membership",
                    )
                return deepcopy(group["created"]), deepcopy(amendment)
            created = deepcopy(group["created"])
        else:
            if _cancel_request_covers_item(
                run, parent_item_id, projection.edges(run_id),
            ):
                _refuse(
                    "spawn_parent_cancel_requested",
                    "a durable parent cancellation request fences group creation",
                )
            if _parse_time(deadline) <= current:
                _refuse("spawn_deadline_expired", "new group deadline has expired")
            policy = run["attempt_spawn_policy"].get(parent_attempt_id)
            if policy is None:
                _refuse("spawn_policy_missing", "group requires a bound spawn policy")
            created = {
                "schema_version": 1,
                "id": group_id,
                "tenant_id": self.ledger.root.tenant_id,
                "timestamp": _timestamp(current),
                "kind": "spawn_group_created",
                "run_id": run_id,
                "parent_item_id": parent_item_id,
                "parent_attempt_id": parent_attempt_id,
                "parent_fence_token": parent_fence_token,
                "parent_spawn_policy_id": policy["id"],
                "group_key": group_key,
                "max_children": max_children,
                "max_depth": max_depth,
                "child_capability_ceiling": list(child_capability_ceiling),
                "aggregate_budget": [dict(row) for row in aggregate_budget],
                "workspace_policy": workspace_policy,
                "deadline": deadline,
                "join_mode": join_mode,
                "required_count": required_count,
                "on_late_result": on_late_result,
                "on_child_failure": on_child_failure,
                "cancel_remaining_after_success": cancel_remaining_after_success,
            }
            validate_record(
                created, self.ledger.root.tenant_id, RUN_KINDS, integrity=False
            )
            # Validate the complete plan before creating an inert lifecycle fence.
            try:
                self._validate_creation_bounds(run, created, child_rows)
                self._activation_record(run, created, child_rows, edge_rows)
            except ProtocolRefusal:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise ProtocolRefusal(
                    "spawn_input_invalid",
                    "children and edges must use complete closed semantics",
                ) from exc

            def resolve_created(projection: object, candidate: Dict[str, object]):
                projected = projection.run(run_id)
                found_id = projected["spawn_group_by_parent_key"].get(
                    (parent_attempt_id, group_key)
                )
                if found_id is None:
                    return None
                found = projected["spawn_groups"][found_id]["created"]
                if found_id != group_id or _without_common(found) != _without_common(candidate):
                    _refuse(
                        "spawn_group_input_divergent",
                        "group retry changed its durable semantic commitment",
                    )
                return deepcopy(found)

            created = self.ledger._append_spawn_group(
                created, self.__capability, resolve_created
            )

        run = self.ledger.project().run(run_id)
        group = run["spawn_groups"][created["id"]]
        if group["state"] == "aborted":
            _refuse("spawn_group_aborted", "abort won before activation")
        if group["state"] in {"activated", "closed"}:
            return deepcopy(group["created"]), deepcopy(group["amendment"])
        amendment = self._activation_record(run, created, child_rows, edge_rows)

        def resolve_activation(projection: object, candidate: Dict[str, object]):
            projected_group = projection.run(run_id)["spawn_groups"].get(
                created["id"]
            )
            if projected_group is None:
                return None
            if projected_group["state"] == "aborted":
                _refuse("spawn_group_aborted", "abort won before activation")
            existing = projected_group.get("amendment")
            if existing is None:
                return None
            if _without_common(existing) != _without_common(candidate):
                _refuse(
                    "spawn_group_input_divergent",
                    "activation retry changed immutable graph semantics",
                )
            return deepcopy(existing)

        amendment = self.ledger._append_spawn_group(
            amendment, self.__capability, resolve_activation
        )
        return deepcopy(created), deepcopy(amendment)

    @staticmethod
    def _validate_creation_bounds(
        run: Dict[str, object],
        created: Dict[str, object],
        children: Sequence[Dict[str, object]],
    ) -> None:
        """Refuse all request-known widening before the pending fsync."""

        attempt_id = created["parent_attempt_id"]
        state = run["attempts"].get(attempt_id)
        policy = run["attempt_spawn_policy"].get(attempt_id)
        if (
            state is None
            or state["started"] is None
            or state["terminal"] is not None
            or state["opened"]["item_id"] != created["parent_item_id"]
            or state["opened"]["fence_token"] != created["parent_fence_token"]
            or policy is None
            or policy.get("id") != created["parent_spawn_policy_id"]
            or policy.get("subagents_mode") != "managed"
        ):
            _refuse(
                "spawn_parent_fence_invalid",
                "group creation requires the current started managed parent",
            )
        if (
            created["max_children"] > policy["max_children"]
            or created["max_depth"] > policy["max_depth"]
            or not set(created["child_capability_ceiling"])
            <= set(policy["child_capability_ceiling"])
            or created["workspace_policy"] not in policy["workspace_policies"]
        ):
            _refuse("spawn_group_widening", "group widens parent spawn policy")
        existing_members = sum(
            len(group["member_item_ids"])
            for group in run["spawn_groups"].values()
            if group["created"]["parent_attempt_id"] == attempt_id
        )
        if (
            not 1 <= len(children) <= created["max_children"]
            or existing_members + len(children) > policy["max_children"]
            or len(run["item_ids"]) + len(children) > 64
        ):
            _refuse("spawn_item_limit", "children exceed fixed or parent bounds")
        child_ids = [row["item_id"] for row in children]
        contract_ids = [row["task_contract_id"] for row in children]
        existing_contract_ids = {
            contract_id
            for contract in run["contracts"].values()
            for contract_id in contract["history_ids"]
        }
        if (
            len(child_ids) != len(set(child_ids))
            or set(child_ids) & set(run["item_ids"])
            or len(contract_ids) != len(set(contract_ids))
            or set(contract_ids) & existing_contract_ids
        ):
            _refuse(
                "spawn_membership_immutable",
                "child and contract identities must be new and unique",
            )
        workspace_rank = {"patch_only": 0, "isolated_worktree": 1}
        expected_depth = run["spawn_item_depth"].get(created["parent_item_id"], 0) + 1
        for child in children:
            if (
                child["depth"] != expected_depth
                or child["depth"] > min(16, created["max_depth"])
            ):
                _refuse("spawn_depth_limit", "child depth exceeds group bound")
            if not set(child["capability_ceiling"]) <= set(
                created["child_capability_ceiling"]
            ):
                _refuse("spawn_capability_widening", "child widens group capability")
            if (
                child["workspace_policy"] not in policy["workspace_policies"]
                or workspace_rank[child["workspace_policy"]]
                > workspace_rank[created["workspace_policy"]]
            ):
                _refuse("spawn_workspace_widening", "child widens workspace policy")
        group_budget = {
            row["budget_id"]: row["amount"]
            for row in created["aggregate_budget"]
        }
        child_budget: Dict[str, int] = {}
        for child in children:
            for row in child["budget_allocation"]:
                child_budget[row["budget_id"]] = (
                    child_budget.get(row["budget_id"], 0) + row["amount"]
                )
        if any(
            amount > group_budget.get(budget_id, 0)
            for budget_id, amount in child_budget.items()
        ):
            _refuse("spawn_budget_widening", "child allocations exceed group budget")
        parent_budget = {
            row["budget_id"]: row["amount"]
            for row in policy["spawn_budget_ceiling"]
        }
        allocated: Dict[str, int] = {}
        for group in run["spawn_groups"].values():
            if (
                group["created"]["parent_attempt_id"] == attempt_id
                and group["state"] != "aborted"
            ):
                for row in group["created"]["aggregate_budget"]:
                    allocated[row["budget_id"]] = (
                        allocated.get(row["budget_id"], 0) + row["amount"]
                    )
        for row in created["aggregate_budget"]:
            allocated[row["budget_id"]] = (
                allocated.get(row["budget_id"], 0) + row["amount"]
            )
        if any(
            amount > parent_budget.get(budget_id, 0)
            for budget_id, amount in allocated.items()
        ):
            _refuse("spawn_budget_widening", "group budgets exceed parent ceiling")

    def _activation_record(
        self,
        run: Dict[str, object],
        created: Dict[str, object],
        children: Sequence[Dict[str, object]],
        dependency_edges: Sequence[Dict[str, object]],
    ) -> Dict[str, object]:
        enabled = run.get("spawn_admission")
        if enabled is None:
            _refuse("spawn_admission_disabled", "run lacks complete spawn preimage")
        if run["policy"] is None or run["policy"]["policy_digest"] != self.policy.digest:
            _refuse("spawn_policy_digest_mismatch", "controller policy is not durable")
        current = deepcopy(enabled["current_plan"])
        current["items"] = sorted(
            [*current["items"], *[_child_item(row) for row in children]],
            key=lambda row: row["item_id"],
        )
        current["dependency_edges"] = sorted(
            [*current["dependency_edges"], *deepcopy(list(dependency_edges))],
            key=lambda edge: (
                edge["source"], edge["target"], edge["requires"],
                edge["failure_policy"],
            ),
        )
        amended = AdmissionPlan.from_canonical(current)
        artifact = AdmissionEvaluator.evaluate(amended, self.policy)
        if artifact.outcome != "admitted":
            _refuse(
                "spawn_admission_not_admitted",
                "complete amended plan fails current AdmissionEvaluator semantics",
            )
        previous_admission = run["admission_binding"]["admission_digest"]
        semantic = {
            "run_id": created["run_id"],
            "spawn_group_id": created["id"],
            "parent_item_id": created["parent_item_id"],
            "parent_attempt_id": created["parent_attempt_id"],
            "parent_spawn_policy_id": created["parent_spawn_policy_id"],
            "previous_plan_digest": run["plan_digest"],
            "previous_admission_digest": previous_admission,
            "policy_digest": self.policy.digest,
            "children": deepcopy(list(children)),
            "dependency_edges": deepcopy(list(dependency_edges)),
            "plan_digest": amended.digest,
            "admission_digest": _spawn_admission_digest(
                previous_admission, amended
            ),
        }
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "plan-amendment-" + _semantic_uuid(
                "slipway-spawn-activation-v1", semantic
            ),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": created["timestamp"],
            "kind": "plan_amendment",
            **semantic,
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)
        return record

    def abort_group(
        self,
        run_id: str,
        spawn_group_id: str,
        *,
        reason_code: str,
        cancel_scope_resolved_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        authority_subject: Optional[str] = None,
        authority_epoch: Optional[int] = None,
        capability_record_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        validate_repository_policy_integrity(self.policy)
        managed = self._managed_intent(
            "spawn_group_abort_evaluation",
            {
                "run_id": run_id,
                "spawn_group_id": spawn_group_id,
                "reason_code": reason_code,
                "cancel_scope_resolved_id": cancel_scope_resolved_id,
                "operator_id": operator_id,
                "authority_subject": authority_subject,
                "authority_epoch": authority_epoch,
                "capability_record_id": capability_record_id,
            },
        )
        if managed is not None:
            return managed
        operator_fields = (
            operator_id,
            authority_subject,
            authority_epoch,
            capability_record_id,
        )
        if (
            reason_code == "cancellation"
            and any(value is not None for value in operator_fields)
        ) or (
            reason_code == "operator_abandonment"
            and cancel_scope_resolved_id is not None
        ):
            _refuse(
                "spawn_abort_authority_conflict",
                "abort reason must use exactly one closed authority variant",
            )
        current = _now(now)
        run = self.ledger.project().run(run_id)
        group = run["spawn_groups"].get(spawn_group_id)
        if group is None:
            _refuse("spawn_group_missing", "abort requires a durable pending group")
        if group["state"] == "aborted":
            existing = group["aborted"]
            repeated = {
                "reason_code": reason_code,
                "cancel_scope_resolved_id": cancel_scope_resolved_id,
                "operator_id": operator_id,
                "authority_subject": authority_subject,
                "authority_epoch": authority_epoch,
                "capability_record_id": capability_record_id,
            }
            if any(existing[key] != value for key, value in repeated.items()):
                _refuse("spawn_abort_input_divergent", "abort retry changed authority")
            return deepcopy(existing)
        if group["state"] != "pending":
            _refuse("spawn_group_activated", "activation won before abort")
        created = group["created"]
        if reason_code == "cancellation":
            resolved = next((
                row["resolved"]
                for row in run["cancellations"].values()
                if row["resolved"] is not None
                and row["resolved"]["id"] == cancel_scope_resolved_id
            ), None)
            if resolved is None or created["parent_item_id"] not in resolved["item_ids"]:
                _refuse(
                    "spawn_abort_cancellation_invalid",
                    "cancellation abort requires a resolved scope covering the parent",
                )
        elif reason_code == "operator_abandonment":
            from .cancellation import _authorize_actor

            capability = _authorize_actor(
                self.ledger,
                operator_id,
                "operator",
                "spawn.group.abort",
                authority_subject,
                authority_epoch,
                capability_record_id,
                current,
            )
            capability_record_id = capability["id"]
        else:
            _refuse("spawn_abort_reason_invalid", "abort reason is outside the closed set")
        semantic = {
            "run_id": run_id,
            "spawn_group_id": spawn_group_id,
            "parent_attempt_id": created["parent_attempt_id"],
            "parent_fence_token": created["parent_fence_token"],
            "reason_code": reason_code,
            "cancel_scope_resolved_id": cancel_scope_resolved_id,
            "operator_id": operator_id,
            "authority_subject": authority_subject,
            "authority_epoch": authority_epoch,
            "capability_record_id": capability_record_id,
        }
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "spawn-group-aborted-" + _semantic_uuid(
                "slipway-spawn-abort-v1", semantic
            ),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "spawn_group_aborted",
            **semantic,
            "aborted_at_testimony": _timestamp(current),
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            projected = projection.run(run_id)["spawn_groups"].get(spawn_group_id)
            if projected is None:
                return None
            if projected["state"] in {"activated", "closed"}:
                _refuse("spawn_group_activated", "activation won before abort")
            existing = projected.get("aborted")
            if existing is None:
                return None
            fields = set(candidate) - {"id", "timestamp", "aborted_at_testimony"}
            if any(existing[field] != candidate[field] for field in fields):
                _refuse("spawn_abort_input_divergent", "abort retry changed authority")
            return deepcopy(existing)

        return self.ledger._append_spawn_group(
            record, self.__capability, resolve_existing
        )

    def admit_child(
        self, run_id: str, spawn_group_id: str, child_item_id: str, *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """Bind one immutable child to current durable admission evidence."""

        validate_repository_policy_integrity(self.policy)
        managed = self._managed_intent(
            "spawn_child_admission_evaluation",
            {
                "run_id": run_id,
                "spawn_group_id": spawn_group_id,
                "child_item_id": child_item_id,
                "outcome": "admit",
                "reason_code": None,
            },
        )
        if managed is not None:
            return managed
        current = _now(now)
        run = self.ledger.project().run(run_id)
        group = run["spawn_groups"].get(spawn_group_id)
        if group is None or group["state"] not in {"activated", "closed"}:
            _refuse("spawn_group_inactive", "child admission requires an activated group")
        existing = group["admissions"].get(child_item_id)
        if existing is not None:
            return deepcopy(existing)
        if child_item_id in group["rejections"] or group["closed"] is not None:
            _refuse("spawn_child_outcome_final", "child already has a final group outcome")
        if current > _parse_time(group["created"]["deadline"]):
            _refuse("spawn_deadline_expired", "child admission cannot follow the group deadline")
        child = next((
            row for row in group["amendment"]["children"]
            if row["item_id"] == child_item_id
        ), None)
        if child is None:
            _refuse("spawn_child_missing", "admission must name an immutable member")
        parent_policy = run["attempt_spawn_policy"].get(
            group["created"]["parent_attempt_id"]
        )
        if (
            parent_policy is None
            or not _admission_digest_is_ancestor(
                run, group["amendment"]["admission_digest"]
            )
            or not set(child["capability_ceiling"])
            <= set(group["created"]["child_capability_ceiling"])
            <= set(parent_policy["child_capability_ceiling"])
        ):
            _refuse("spawn_child_binding_invalid", "child no longer matches durable admission ceilings")
        workspace = str(worker_workspace_root() / child_item_id)
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "child-admitted-" + _semantic_uuid(
                "slipway-child-admitted-v1", {
                    "spawn_group_id": spawn_group_id,
                    "child_item_id": child_item_id,
                    "plan_amendment_id": group["amendment"]["id"],
                },
            ),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "child_admitted",
            "run_id": run_id,
            "spawn_group_id": spawn_group_id,
            "plan_amendment_id": group["amendment"]["id"],
            "parent_attempt_id": group["created"]["parent_attempt_id"],
            "child_item_id": child_item_id,
            "child_depth": child["depth"],
            "task_contract_id": child["task_contract_id"],
            "task_contract_digest": child["task_contract_digest"],
            "admission_digest": group["amendment"]["admission_digest"],
            "capability_ceiling": deepcopy(child["capability_ceiling"]),
            "budget_allocation": deepcopy(child["budget_allocation"]),
            "workspace_policy": child["workspace_policy"],
            "workspace": workspace,
            "admitted_at_testimony": _timestamp(current),
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            projected = projection.run(run_id)["spawn_groups"].get(spawn_group_id)
            if projected is None:
                return None
            found = projected["admissions"].get(child_item_id)
            if found is None:
                if child_item_id in projected["rejections"]:
                    _refuse("spawn_child_outcome_final", "rejection won child admission")
                return None
            fields = set(candidate) - {"timestamp", "admitted_at_testimony"}
            if any(found[field] != candidate[field] for field in fields):
                _refuse("spawn_child_input_divergent", "child admission retry changed immutable evidence")
            return deepcopy(found)

        return self.ledger._append_spawn_group(record, self.__capability, resolve_existing)

    def reject_child(
        self, run_id: str, spawn_group_id: str, child_item_id: str, *,
        reason_code: Optional[str] = None, now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """Persist the sole non-launch admission outcome for one member."""

        validate_repository_policy_integrity(self.policy)
        managed = self._managed_intent(
            "spawn_child_admission_evaluation",
            {
                "run_id": run_id,
                "spawn_group_id": spawn_group_id,
                "child_item_id": child_item_id,
                "outcome": "reject",
                "reason_code": reason_code,
            },
        )
        if managed is not None:
            return managed
        current = _now(now)
        run = self.ledger.project().run(run_id)
        group = run["spawn_groups"].get(spawn_group_id)
        if group is None or group["state"] not in {"activated", "closed"}:
            _refuse("spawn_group_inactive", "child rejection requires an activated group")
        existing = group["rejections"].get(child_item_id)
        if existing is not None:
            if reason_code is not None and existing["reason_code"] != reason_code:
                _refuse("spawn_child_input_divergent", "rejection retry changed reason")
            return deepcopy(existing)
        if child_item_id in group["admissions"] or group["closed"] is not None:
            _refuse("spawn_child_outcome_final", "child already has a final group outcome")
        child = next((
            row for row in group["amendment"]["children"]
            if row["item_id"] == child_item_id
        ), None)
        if child is None:
            _refuse("spawn_child_missing", "rejection must name an immutable member")
        derived_reason = self._child_rejection_reason(run, group, child, current)
        if derived_reason is None:
            _refuse(
                "spawn_child_rejection_unfounded",
                "lawful child admission evidence cannot be caller-nominated for rejection",
            )
        if reason_code is not None and reason_code != derived_reason:
            _refuse(
                "spawn_child_rejection_evidence_mismatch",
                "rejection reason must equal the canonical evidence-derived outcome",
            )
        reason_code = derived_reason
        semantic = {
            "run_id": run_id, "spawn_group_id": spawn_group_id,
            "plan_amendment_id": group["amendment"]["id"],
            "parent_attempt_id": group["created"]["parent_attempt_id"],
            "child_item_id": child_item_id, "reason_code": reason_code,
        }
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "child-rejected-" + _semantic_uuid("slipway-child-rejected-v1", semantic),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": _timestamp(current), "kind": "child_rejected",
            **semantic, "evaluated_at_testimony": _timestamp(current),
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            projected = projection.run(run_id)["spawn_groups"].get(spawn_group_id)
            if projected is None:
                return None
            found = projected["rejections"].get(child_item_id)
            if found is None:
                if child_item_id in projected["admissions"]:
                    _refuse("spawn_child_outcome_final", "admission won child rejection")
                return None
            if found["reason_code"] != reason_code:
                _refuse("spawn_child_input_divergent", "rejection retry changed reason")
            return deepcopy(found)

        return self.ledger._append_spawn_group(record, self.__capability, resolve_existing)

    def _child_rejection_reason(
        self, run: Dict[str, object], group: Dict[str, object],
        child: Dict[str, object], current: datetime,
    ) -> Optional[str]:
        """Return the first closed refusal class proved by durable evidence."""

        validate_repository_policy_integrity(self.policy)
        created = group["created"]
        parent_policy = run["attempt_spawn_policy"].get(
            created["parent_attempt_id"]
        )
        if current > _parse_time(created["deadline"]):
            return "deadline_expired"
        if parent_policy is None or not _admission_digest_is_ancestor(
            run, group["amendment"]["admission_digest"]
        ):
            return "admission_binding_refusal"
        if len(run["item_ids"]) > 64:
            return "item_limit"
        if len(group["member_item_ids"]) > int(created["max_children"]):
            return "fanout_limit"
        if int(child["depth"]) > min(
            int(created["max_depth"]), int(parent_policy["max_depth"]), 16,
        ):
            return "depth_limit"
        child_budget = {
            row["budget_id"]: row["amount"] for row in child["budget_allocation"]
        }
        group_budget = {
            row["budget_id"]: row["amount"] for row in created["aggregate_budget"]
        }
        parent_budget = {
            row["budget_id"]: row["amount"]
            for row in parent_policy["spawn_budget_ceiling"]
        }
        if any(
            amount > group_budget.get(budget_id, -1)
            or amount > parent_budget.get(budget_id, -1)
            for budget_id, amount in child_budget.items()
        ):
            return "budget_refusal"
        if not set(child["capability_ceiling"]) <= set(
            created["child_capability_ceiling"]
        ) <= set(parent_policy["child_capability_ceiling"]):
            return "capability_refusal"
        if (
            child["workspace_policy"] != created["workspace_policy"]
            or child["workspace_policy"] not in parent_policy["workspace_policies"]
        ):
            return "workspace_refusal"
        if group["amendment"].get("policy_digest") != self.policy.digest:
            return "policy_refusal"
        return None

    def close_group(
        self, run_id: str, spawn_group_id: str, *,
        adapters: Optional[Dict[str, object]] = None,
        cancel_scope_resolved_id: Optional[str] = None,
        outcome: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """Close one group from the physical prefix; callers never nominate sets."""

        if self.ledger._sequencer_client is not None:
            if adapters:
                _refuse(
                    "spawn_managed_adapters_invalid",
                    "managed close cannot accept caller-owned adapter objects",
                )
            managed = self._managed_intent(
                "spawn_group_close_evaluation",
                {
                    "run_id": run_id,
                    "spawn_group_id": spawn_group_id,
                    "cancel_scope_resolved_id": cancel_scope_resolved_id,
                    "outcome": outcome,
                },
            )
            assert managed is not None
            return managed
        current = _now(now)
        run = self.ledger.project().run(run_id)
        group = run["spawn_groups"].get(spawn_group_id)
        if group is None:
            _refuse("spawn_group_missing", "close requires an activated group")
        if group["closed"] is not None:
            existing = group["closed"]
            if outcome is not None and existing["outcome"] != outcome:
                _refuse("spawn_group_close_divergent", "close retry changed outcome")
            if cancel_scope_resolved_id is not None and existing["cancel_scope_resolved_id"] != cancel_scope_resolved_id:
                _refuse("spawn_group_close_divergent", "close retry changed cancellation")
            return deepcopy(existing)
        if group["state"] != "activated":
            _refuse("spawn_group_inactive", "close requires an activated group")
        created = group["created"]
        descendants = [
            row for (attempt_id, _adapter, _provider), row
            in run["untracked_descendants"].items()
            if attempt_id == created["parent_attempt_id"]
        ]
        if any(row["state"] == "observed" for row in descendants):
            _refuse("untracked_descendant_unresolved", "group cannot close with unresolved descendants")
        unknown = any(row["state"] == "unknown" for row in descendants)
        decision = "needs_operator" if unknown else _spawn_join_decision(run, group)
        close_reason: Optional[str] = None
        chosen = outcome
        if chosen == "cancelled":
            unfinished = [
                item for item in group["member_item_ids"]
                if item not in run["spawn_item_outcomes"]
                and item not in run["accepted"]
                and not (
                    run["item_attempt_ids"].get(item)
                    and run["attempts"][run["item_attempt_ids"][item][-1]]["terminal"] is not None
                )
            ]
            if unfinished:
                _refuse("spawn_cancellation_incomplete", "cancelled close requires every member terminal")
            close_reason = "parent_cancelled"
        elif decision is not None:
            chosen = decision
            close_reason = {
                "satisfied": {
                    "all_accepted": "all_members_accepted",
                    "all_terminal": "all_members_terminal",
                    "quorum": "quorum_reached",
                    "first_accepted": "first_accepted",
                }[created["join_mode"]],
                "failed": (
                    "child_failure" if created["on_child_failure"] == "fail_group"
                    else "join_impossible"
                ),
                "needs_operator": (
                    "untracked_descendant_unknown" if unknown else "member_needs_operator"
                ),
            }[decision]
        elif current >= _parse_time(created["deadline"]):
            chosen, close_reason = "deadline", "deadline_expired"
        else:
            _refuse("spawn_join_unsatisfied", "group join is not yet irreversible")

        members = list(group["member_item_ids"])
        accepted = sorted(item for item in members if item in run["accepted"])
        rejected = sorted(group["rejections"])
        terminal = sorted(
            item for item in members
            if run["spawn_item_outcomes"].get(item) == "cancelled"
            or (
                run["item_attempt_ids"].get(item)
                and run["attempts"][run["item_attempt_ids"][item][-1]]["terminal"] is not None
            )
        )
        if chosen == "satisfied" and created["cancel_remaining_after_success"]:
            remaining = sorted(set(members) - set(accepted) - set(terminal) - set(rejected))
            if remaining:
                from .cancellation import CancellationCoordinator
                resolved = CancellationCoordinator(self.ledger).request_exact_items(
                    run_id, remaining, adapters or {}, spawn_group_id=spawn_group_id,
                    now=current,
                )
                cancel_scope_resolved_id = str(resolved["id"])
                run = self.ledger.project().run(run_id)
                group = run["spawn_groups"][spawn_group_id]
                terminal = sorted(
                    item for item in members
                    if run["spawn_item_outcomes"].get(item) == "cancelled"
                    or (
                        run["item_attempt_ids"].get(item)
                        and run["attempts"][run["item_attempt_ids"][item][-1]]["terminal"] is not None
                    )
                )
        if chosen == "satisfied" and cancel_scope_resolved_id is not None:
            if not created["cancel_remaining_after_success"]:
                _refuse(
                    "spawn_satisfied_cancellation_forbidden",
                    "satisfied close cannot bind cancellation when its policy is disabled",
                )
            cancellation = next((
                row for row in run["cancellations"].values()
                if row["resolved"] is not None
                and row["resolved"]["id"] == cancel_scope_resolved_id
            ), None)
            if cancellation is None:
                _refuse(
                    "spawn_satisfied_cancellation_invalid",
                    "satisfied close requires one durable exact cancellation",
                )
            request = cancellation["requested"]
            expected_remaining = _spawn_remaining_at_record(
                run, group, request["id"],
            )
            if (
                request.get("scope") != "exact_items"
                or request.get("spawn_group_id") != spawn_group_id
                or request.get("requested_by") != "spawn_join"
                or request.get("item_ids") != expected_remaining
                or cancellation["resolved"].get("item_ids") != expected_remaining
            ):
                _refuse(
                    "spawn_satisfied_cancellation_invalid",
                    "satisfied cancellation must equal the physical remaining member set",
                )
        semantic = {
            "run_id": run_id, "spawn_group_id": spawn_group_id,
            "plan_amendment_id": group["amendment"]["id"],
            "parent_attempt_id": created["parent_attempt_id"],
            "member_item_ids": members, "accepted_item_ids": accepted,
            "terminal_item_ids": terminal, "rejected_item_ids": rejected,
            "join_mode": created["join_mode"], "required_count": created["required_count"],
            "outcome": chosen, "close_reason": close_reason,
            "cancel_scope_resolved_id": cancel_scope_resolved_id,
        }
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "spawn-group-closed-" + _semantic_uuid("slipway-spawn-close-v1", semantic),
            "tenant_id": self.ledger.root.tenant_id, "timestamp": _timestamp(current),
            "kind": "spawn_group_closed", **semantic,
            "closed_at_testimony": _timestamp(current),
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            projected = projection.run(run_id)["spawn_groups"].get(spawn_group_id)
            if projected is None or projected["closed"] is None:
                return None
            found = projected["closed"]
            fields = set(candidate) - {"timestamp", "closed_at_testimony"}
            if any(found[field] != candidate[field] for field in fields):
                _refuse("spawn_group_close_divergent", "concurrent close changed physical truth")
            return deepcopy(found)

        return self.ledger._append_spawn_group(record, self.__capability, resolve_existing)

    def record_untracked_descendant(
        self, run_id: str, parent_attempt_id: str, provider_descendant_id: str,
        state: str, *, adopted_item_id: Optional[str] = None,
        _launch_capability: object = None,
        _launch_authorizer: object = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if self.ledger._sequencer_client is not None:
            _refuse(
                "spawn_group_controller_only",
                "descendant testimony is direct WorkerRunner authority only",
            )
        current = _now(now)
        run = self.ledger.project().run(run_id)
        attempt = run["attempts"].get(parent_attempt_id)
        policy = run["attempt_spawn_policy"].get(parent_attempt_id)
        if attempt is None or policy is None or policy.get("subagents_mode") == "disabled":
            _refuse("descendant_observation_invalid", "descendant testimony requires an observed launch")
        if parent_attempt_id in run["descendant_observation_close"]:
            _refuse(
                "descendant_observation_closed",
                "descendant testimony cannot follow the durable observation close",
            )
        requested_event = (provider_descendant_id, state, adopted_item_id)
        received_event = self._require_worker_pipe_receive(
            _launch_capability, run_id, parent_attempt_id, str(policy["adapter"]),
            _launch_authorizer, "descendant", requested_event,
        )
        if (
            not isinstance(received_event, tuple)
            or len(received_event) != 3
            or requested_event != received_event
        ):
            _refuse(
                "descendant_pipe_receive_required",
                "descendant testimony must equal the exact just-consumed worker pipe message",
            )
        reason = {
            "observed": "native_descendant_observed", "terminated": "adapter_terminated",
            "adopted": "adopted_managed", "unknown": "observation_uncertain",
        }.get(state)
        if reason is None:
            _refuse("descendant_state_invalid", "descendant state is outside the closed set")
        semantic = {
            "run_id": run_id, "parent_item_id": attempt["opened"]["item_id"],
            "parent_attempt_id": parent_attempt_id, "adapter": policy["adapter"],
            "provider_descendant_id": provider_descendant_id, "state": state,
            "adopted_item_id": adopted_item_id, "reason_code": reason,
        }
        timestamp = _timestamp(current)
        observed_at_testimony = timestamp
        existing = run["untracked_descendants"].get(
            (parent_attempt_id, policy["adapter"], provider_descendant_id)
        )
        retry_fields = {
            "parent_item_id", "parent_attempt_id", "adapter",
            "provider_descendant_id", "state", "adopted_item_id", "reason_code",
        }
        if existing is not None and all(
            existing[field] == semantic[field] for field in retry_fields
        ):
            timestamp = str(existing["timestamp"])
            observed_at_testimony = str(existing["observed_at_testimony"])
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "untracked-descendant-" + _semantic_uuid("slipway-descendant-v1", semantic),
            "tenant_id": self.ledger.root.tenant_id, "timestamp": timestamp,
            "kind": "untracked_descendant", **semantic,
            "observed_at_testimony": observed_at_testimony,
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            projected = projection.run(run_id)
            found = projected["untracked_descendants"].get(
                (parent_attempt_id, policy["adapter"], provider_descendant_id)
            )
            if found is None:
                return None
            if all(found[field] == candidate[field] for field in retry_fields):
                return deepcopy(found)
            if found["state"] == "observed" and state != "observed":
                return None
            _refuse("descendant_input_divergent", "descendant retry changed final testimony")

        return self.ledger._append_spawn_group(record, self.__capability, resolve_existing)

    def close_descendant_observation(
        self, run_id: str, parent_attempt_id: str, *,
        _launch_capability: object = None,
        _launch_authorizer: object = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if self.ledger._sequencer_client is not None:
            _refuse(
                "spawn_group_controller_only",
                "observation close is direct WorkerRunner authority only",
            )
        current = _now(now)
        run = self.ledger.project().run(run_id)
        attempt = run["attempts"].get(parent_attempt_id)
        policy = run["attempt_spawn_policy"].get(parent_attempt_id)
        if attempt is None or policy is None or policy.get("subagents_mode") == "disabled":
            _refuse("descendant_observation_invalid", "observation close requires an observed launch")
        self._require_worker_pipe_receive(
            _launch_capability, run_id, parent_attempt_id, str(policy["adapter"]),
            _launch_authorizer, "result",
        )
        existing = run["descendant_observation_close"].get(parent_attempt_id)
        if existing is not None:
            return deepcopy(existing)
        rows = [row for (attempt_id, _adapter, _provider), row in run["untracked_descendants"].items() if attempt_id == parent_attempt_id]
        record: Dict[str, object] = {
            "schema_version": 1, "id": "descendant-observation-closed-" + _semantic_uuid(
                "slipway-observation-close-v1", {"run_id": run_id, "attempt_id": parent_attempt_id}
            ),
            "tenant_id": self.ledger.root.tenant_id, "timestamp": _timestamp(current),
            "kind": "descendant_observation_closed", "run_id": run_id,
            "parent_item_id": attempt["opened"]["item_id"],
            "parent_attempt_id": parent_attempt_id,
            "parent_fence_token": attempt["opened"]["fence_token"],
            "attempt_spawn_policy_id": policy["id"], "adapter": policy["adapter"],
            "observed_descendant_ids": sorted(row["provider_descendant_id"] for row in rows),
            "closed_at_testimony": _timestamp(current),
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            found = projection.run(run_id)["descendant_observation_close"].get(parent_attempt_id)
            if found is None:
                return None
            fields = set(candidate) - {"timestamp", "closed_at_testimony"}
            if any(found[field] != candidate[field] for field in fields):
                _refuse("descendant_close_divergent", "observation close retry changed the complete set")
            return deepcopy(found)

        return self.ledger._append_spawn_group(record, self.__capability, resolve_existing)

    def dispose_late_result(
        self, run_id: str, spawn_group_id: str, child_item_id: str,
        result_record_id: str, disposition: str, *, operator_id: Optional[str] = None,
        authority_subject: Optional[str] = None, authority_epoch: Optional[int] = None,
        capability_record_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        managed = self._managed_intent(
            "spawn_late_result_disposition_evaluation",
            {
                "run_id": run_id,
                "spawn_group_id": spawn_group_id,
                "child_item_id": child_item_id,
                "result_record_id": result_record_id,
                "disposition": disposition,
                "operator_id": operator_id,
                "authority_subject": authority_subject,
                "authority_epoch": authority_epoch,
                "capability_record_id": capability_record_id,
            },
        )
        if managed is not None:
            return managed
        current = _now(now)
        run = self.ledger.project().run(run_id)
        group = run["spawn_groups"].get(spawn_group_id)
        if group is None or result_record_id not in group["late_result_ids"]:
            _refuse("late_result_missing", "disposition requires one physically late member result")
        existing = run["late_result_dispositions"].get(
            (spawn_group_id, child_item_id, result_record_id)
        )
        if existing is not None:
            repeated = {
                "disposition": disposition, "operator_id": operator_id,
                "authority_subject": authority_subject, "authority_epoch": authority_epoch,
                "capability_record_id": capability_record_id,
            }
            if any(existing[field] != value for field, value in repeated.items()):
                _refuse("late_result_disposition_divergent", "late disposition retry changed authority")
            return deepcopy(existing)
        from .cancellation import _authorize_actor
        capability = _authorize_actor(
            self.ledger, operator_id, "operator", "spawn.late_result.dispose",
            authority_subject, authority_epoch, capability_record_id, current,
        )
        semantic = {
            "run_id": run_id, "spawn_group_id": spawn_group_id,
            "child_item_id": child_item_id, "result_record_id": result_record_id,
            "disposition": disposition, "operator_id": operator_id,
            "authority_subject": authority_subject, "authority_epoch": authority_epoch,
            "capability_record_id": capability["id"],
        }
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "spawn-late-result-disposition-" + _semantic_uuid("slipway-late-result-v1", semantic),
            "tenant_id": self.ledger.root.tenant_id, "timestamp": _timestamp(current),
            "kind": "spawn_late_result_disposition", **semantic,
            "decided_at_testimony": _timestamp(current),
        }
        validate_record(record, self.ledger.root.tenant_id, RUN_KINDS, integrity=False)

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            found = projection.run(run_id)["late_result_dispositions"].get(
                (spawn_group_id, child_item_id, result_record_id)
            )
            if found is None:
                return None
            fields = set(candidate) - {"timestamp", "decided_at_testimony"}
            if any(found[field] != candidate[field] for field in fields):
                _refuse("late_result_disposition_divergent", "concurrent disposition changed authority")
            return deepcopy(found)

        return self.ledger._append_spawn_group(record, self.__capability, resolve_existing)

    def group(self, run_id: str, spawn_group_id: str) -> Dict[str, object]:
        group = self.ledger.project().run(run_id)["spawn_groups"].get(
            spawn_group_id
        )
        if group is None:
            _refuse("spawn_group_missing", "group lookup requires a durable identity")
        return deepcopy(group)


__all__ = ["SpawnGroupController"]

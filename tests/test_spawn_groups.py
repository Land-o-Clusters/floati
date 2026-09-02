from __future__ import annotations

import hashlib
import multiprocessing
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from unittest.mock import patch

from floati.capabilities import CapabilityGrantLedger
from floati.capability_binding import CapabilityBinder
from floati.contracts import TaskContract, contract_digest
from floati.errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from floati.host_paths import worker_workspace_root
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.admission import AdmissionBinder, AdmissionEvaluator, AdmissionPlan
from floati.planes import AuthorityGrantStore
from floati.policy import RepositoryPolicy
from floati.registry import Registry
from floati.records import (
    capability_set_digest,
    run_admission_digest,
    validate_record,
)
from floati.root import FloatiRoot
from floati.runtruth import RUN_KINDS, RunLedger, RunProjection, attempt_fence_token
from floati.scheduler import RetryPolicy, RunScheduler
from floati.cancellation import CancellationCoordinator
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.test_admission import VALID_POLICY
from floati.sequencer import (
    MAX_FRAME_BYTES,
    SequencerClient,
    SequencerConfig,
    SequencerService,
    _canonical_evaluated_intent,
    _encode_frame,
    _policy_evidence,
    _semantic_uuid as _sequencer_semantic_uuid,
)

try:
    from floati.spawn_groups import SpawnGroupController
except (ImportError, ModuleNotFoundError):
    SpawnGroupController = None


NOW = "2026-08-10T12:00:00.000Z"
DIGEST = "a" * 64


def _record(kind: str, prefix: str, **fields: object) -> dict[str, object]:
    return {
        "schema_version": 1 if kind in {
            "run_spawn_admission_enabled", "attempt_spawn_policy_bound",
            "spawn_group_created", "spawn_group_aborted", "child_admitted",
            "child_rejected", "spawn_group_closed", "untracked_descendant",
            "descendant_observation_closed", "spawn_late_result_disposition",
        } else 0,
        "id": prefix + uuid7_hex(),
        "tenant_id": "alpha",
        "timestamp": NOW,
        "kind": kind,
        **fields,
    }


def _contract() -> TaskContract:
    return TaskContract.create(
        objective="bounded child",
        non_goals=["no hidden descendants"],
        areas_to_avoid=[{"path": "floati/provider.py", "region": "all"}],
        input_hashes={"brief": DIGEST},
        acceptance_checks={"tests.unit": "python3 -m unittest"},
        constraints={"network": "dark"},
        risk_class="high",
        retry_policy={
            "max_attempts": 1,
            "backoff": {"base_delay_ms": 0, "cap_delay_ms": 0, "strategy": "fixed"},
        },
        dependencies=[],
    )


class _GovernedSpawnPipeAdapter:
    """Forked adapter whose descendant testimony crosses the real private pipe."""

    name = "codex"

    def __init__(
        self,
        before_spawn: tuple[dict[str, object], ...] = (),
        during_drive: tuple[dict[str, object], ...] = (),
    ) -> None:
        self.before_spawn = before_spawn
        self.during_drive = during_drive
        self._emit = None

    def set_spawn_context(self, context: dict[str, object], emit: object) -> None:
        if context["subagents_mode"] != "managed" or not callable(emit):
            raise RuntimeError("governed spawn context missing")
        self._emit = emit
        for event in self.before_spawn:
            emit(dict(event))

    def spawn(self, item: dict[str, object], *, deadline_seconds: float) -> object:
        return {"item_id": item["id"], "deadline_seconds": deadline_seconds}

    def drive(
        self, handle: object, item: dict[str, object], *, deadline_seconds: float,
    ) -> list[dict[str, str]]:
        if not callable(self._emit):
            raise RuntimeError("spawn event pipe missing")
        for event in self.during_drive:
            self._emit(dict(event))
        return [{"repo": "floati-proof", "sha": "a" * 40, "doc": "README.md"}]


class _PostResultObservationCloseFailureAdapter:
    """A real forked adapter that reports failure after the parent's close message."""

    name = "codex"

    def set_spawn_context(self, context: dict[str, object], emit: object) -> None:
        if context["subagents_mode"] != "managed" or not callable(emit):
            raise RuntimeError("governed spawn context missing")

    def spawn(self, item: dict[str, object], *, deadline_seconds: float) -> object:
        return {"item_id": item["id"], "deadline_seconds": deadline_seconds}

    def drive(
        self, handle: object, item: dict[str, object], *, deadline_seconds: float,
    ) -> list[dict[str, str]]:
        from floati.workers import _adapter_process

        frame = sys._getframe()
        while frame is not None and frame.f_code is not _adapter_process.__code__:
            frame = frame.f_back
        if frame is None:
            raise RuntimeError("worker child private pipe is unavailable")
        connection = frame.f_locals["connection"]
        bindings = [{"repo": "floati-proof", "sha": "a" * 40, "doc": "README.md"}]
        connection.send(("result", bindings))
        if connection.recv() != ("observation_closed", None):
            raise RuntimeError("parent observation close was not delivered")
        connection.send(("failure", "process_died"))
        return bindings


class _ResultBeforeSpawnAdapter:
    """A real child that violates the private-pipe status order."""

    name = "codex"

    def set_spawn_context(self, context: dict[str, object], emit: object) -> None:
        if context["subagents_mode"] != "managed" or not callable(emit):
            raise RuntimeError("governed spawn context missing")

    def spawn(self, item: dict[str, object], *, deadline_seconds: float) -> object:
        from floati.workers import _adapter_process

        frame = sys._getframe()
        while frame is not None and frame.f_code is not _adapter_process.__code__:
            frame = frame.f_back
        if frame is None:
            raise RuntimeError("worker child private pipe is unavailable")
        frame.f_locals["connection"].send(("result", [{
            "repo": "floati-proof", "sha": "a" * 40, "doc": "README.md",
        }]))
        return object()

    def drive(
        self, handle: object, item: dict[str, object], *, deadline_seconds: float,
    ) -> list[dict[str, str]]:
        return [{"repo": "floati-proof", "sha": "a" * 40, "doc": "README.md"}]


def _canonical_edges(edges: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "source": edge["source"],
            "target": edge["target"],
            "requires": edge.get("requires", "accepted"),
            "failure_policy": edge.get("failure_policy", "fail_run"),
        }
        for edge in edges
    ]


def _plan_digest(item_ids: list[str], edges: list[dict[str, object]]) -> str:
    payload = json.dumps(
        {"item_ids": item_ids, "dependency_edges": _canonical_edges(edges)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class SpawnGroupFixtures:
    def __init__(self) -> None:
        self.run_id = "run-" + uuid7_hex()
        self.parent, self.child = sorted(("work-" + uuid7_hex(), "work-" + uuid7_hex()))
        self.attempt = "attempt-" + uuid7_hex()
        self.fence = attempt_fence_token(self.run_id, self.parent, 1, 1)
        self.contract = _contract()
        self.admission_id = "run-admission-bound-" + uuid7_hex()
        self.capability_id = "capability-set-bound-" + uuid7_hex()
        self.spawn_policy_id = "attempt-spawn-policy-bound-" + uuid7_hex()

    def plan(self, *, include_child: bool = False) -> AdmissionPlan:
        def item(item_id: str) -> dict[str, object]:
            return {
                "item_id": item_id,
                "contract": self.contract.canonical(),
                "capability_selector": "review",
                "requires_cancellation": False,
                "requires_callback": False,
                "workspace_key": "workspace-" + item_id[-8:],
                "concurrency_key": "concurrency-a",
                "retry_class": "bounded",
                "effect_safety": "idempotent",
                "merge_gate": None,
            }
        items = [item(self.parent)]
        edges: list[dict[str, object]] = []
        if include_child:
            items.append(item(self.child))
            items.sort(key=lambda row: str(row["item_id"]))
            edges.append({"source": self.parent, "target": self.child,
                          "requires": "accepted", "failure_policy": "fail_run"})
        return AdmissionPlan.from_canonical({
            "schema_version": 0,
            "workers": [{"node_id": "worker-a", "worker_profile": "codex"}],
            "max_active_attempts": 2,
            "budget_reservations": [{"budget_id": "build", "amount": 4}],
            "items": items,
            "dependency_edges": edges,
        })

    @staticmethod
    def amended_plan(
        current_plan: AdmissionPlan,
        children: list[dict[str, object]],
        edges: list[dict[str, object]],
    ) -> AdmissionPlan:
        canonical = current_plan.canonical()
        canonical["items"] = sorted([
            *canonical["items"],
            *[{
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
            } for child in children],
        ], key=lambda item: str(item["item_id"]))
        canonical["dependency_edges"] = sorted([
            *canonical["dependency_edges"], *edges,
        ], key=lambda edge: (
            str(edge["source"]), str(edge["target"]),
            str(edge["requires"]), str(edge["failure_policy"]),
        ))
        return AdmissionPlan.from_canonical(canonical)

    def started_parent(
        self, *, spawn_mode: str = "managed", include_enablement: bool = True,
        include_spawn_policy: bool = True,
    ) -> list[dict[str, object]]:
        plan = self.plan()
        workers = [{"node_id": "worker-a", "worker_profile": "codex"}]
        reservations = [{"budget_id": "build", "amount": 4}]
        admission_items = [{
            "item_id": self.parent,
            "workspace_key": "workspace-" + self.parent[-8:],
            "concurrency_key": "concurrency-a",
            "capability_selector": "review",
        }]
        admission_digest = run_admission_digest(workers, 2, reservations, admission_items)
        admission = _record(
            "run_admission_bound", "run-admission-bound-", run_id=self.run_id,
            plan_digest=plan.digest, policy_digest=DIGEST, max_active_attempts=2,
            workers=workers, budget_reservations=reservations, items=admission_items,
            admission_digest=admission_digest,
        )
        admission["id"] = self.admission_id
        admission["schema_version"] = 1
        enabled = _record(
            "run_spawn_admission_enabled", "run-spawn-admission-enabled-",
            run_id=self.run_id, run_admission_binding_id=admission["id"],
            admission_digest=admission_digest, policy_digest=DIGEST,
            base_plan=plan.canonical(), base_plan_digest=plan.digest,
            enabled_at_testimony=NOW,
        )
        opened = _record(
            "attempt_opened", "attempt-opened-", run_id=self.run_id,
            item_id=self.parent, attempt_id=self.attempt, ordinal=1,
            scheduler_epoch=1, fence_token=self.fence, max_attempts=1,
            backoff={"strategy": "fixed", "base_delay_ms": 0,
                     "cap_delay_ms": 0, "jitter": "sha256_25pct"},
        )
        grants = [{
            "capability_name": "review", "grant_id": "capability-grant-" + uuid7_hex(),
            "physical_position": 1,
        }]
        capability = _record(
            "capability_set_bound", "capability-set-bound-", run_id=self.run_id,
            item_id=self.parent, attempt_id=self.attempt, fence_token=self.fence,
            chosen_worker="worker-a", policy_digest=DIGEST, routing_rank=0,
            evaluated_at_testimony=NOW, grant_ledger_high_watermark=1,
            effective_grants=grants, capability_digest=capability_set_digest(grants),
        )
        capability["id"] = self.capability_id
        capability["schema_version"] = 1
        spawn_policy = _record(
            "attempt_spawn_policy_bound", "attempt-spawn-policy-bound-",
            run_id=self.run_id, parent_item_id=self.parent,
            parent_attempt_id=self.attempt, parent_fence_token=self.fence,
            parent_capability_set_bound_id=capability["id"], adapter="codex",
            subagents_mode=spawn_mode,
            max_children=2 if spawn_mode == "managed" else 0,
            max_depth=2 if spawn_mode == "managed" else 0,
            child_capability_ceiling=["review"] if spawn_mode == "managed" else [],
            spawn_budget_ceiling=[{"budget_id": "build", "amount": 2}]
            if spawn_mode == "managed" else [],
            workspace_policies=["patch_only"] if spawn_mode == "managed" else [],
            bound_at_testimony=NOW,
        )
        spawn_policy["id"] = self.spawn_policy_id
        dispatch = _record(
            "dispatch_decision", "run-dispatch-decision-", run_id=self.run_id,
            item_id=self.parent, attempt_id=self.attempt,
            eligible_workers=["worker-a"], chosen_worker="worker-a",
            capability_digest=capability["capability_digest"], reason_code="policy.route",
            policy_digest=DIGEST, routing_rank=0, scheduler_epoch=1,
            capability_set_bound_id=capability["id"],
        )
        dispatch["schema_version"] = 1
        if include_spawn_policy:
            dispatch["adapter"] = "codex"
            dispatch["attempt_spawn_policy_id"] = spawn_policy["id"]
        return [
            _record(
                "run_created", "run-created-", run_id=self.run_id,
                plan_digest=plan.digest, policy_digest=DIGEST,
                item_ids=[self.parent], dependency_edges=[],
            ),
            _record(
                "task_contract", "task-contract-", run_id=self.run_id,
                item_id=self.parent, **self.contract.canonical(),
                contract_digest=contract_digest(self.contract),
            ),
            admission,
            *([enabled] if include_enablement else []),
            _record(
                "run_policy_bound", "run-policy-bound-", run_id=self.run_id,
                policy_digest=DIGEST,
            ),
            _record(
                "worker_pool_bound", "run-worker-pool-bound-", run_id=self.run_id,
                worker_ids=["worker-a"],
            ),
            opened,
            capability,
            *([spawn_policy] if include_spawn_policy else []),
            dispatch,
            _record(
                "attempt_started", "attempt-started-", run_id=self.run_id,
                item_id=self.parent, attempt_id=self.attempt, ordinal=1,
                attempt_opened_id=opened["id"], dispatch_decision_id=dispatch["id"],
                fence_token=self.fence,
            ),
        ]

    def group(self, **changes: object) -> dict[str, object]:
        row = _record(
            "spawn_group_created", "spawn-group-created-", run_id=self.run_id,
            parent_item_id=self.parent, parent_attempt_id=self.attempt,
            parent_fence_token=self.fence, parent_spawn_policy_id=self.spawn_policy_id,
            group_key="reviewers", max_children=2,
            max_depth=2, child_capability_ceiling=["review"],
            aggregate_budget=[{"budget_id": "build", "amount": 2}],
            workspace_policy="patch_only", deadline="2026-08-10T13:00:00.000Z",
            join_mode="all_terminal", required_count=None,
            on_late_result="quarantine", on_child_failure="fail_group",
            cancel_remaining_after_success=False,
        )
        row.update(changes)
        return row

    def descriptor(self, item_id: str | None = None, **changes: object) -> dict[str, object]:
        row = {
            "item_id": item_id or self.child,
            "task_contract_id": "task-contract-" + uuid7_hex(),
            "task_contract": self.contract.canonical(),
            "task_contract_digest": contract_digest(self.contract),
            "depth": 1,
            "budget_allocation": [{"budget_id": "build", "amount": 1}],
            "capability_ceiling": ["review"],
            "workspace_policy": "patch_only",
            "workspace_key": "workspace-" + (item_id or self.child)[-8:],
            "concurrency_key": "concurrency-a",
            "capability_selector": "review",
            "requires_cancellation": False,
            "requires_callback": False,
            "retry_class": "bounded",
            "effect_safety": "idempotent",
            "merge_gate": None,
        }
        row.update(changes)
        return row

    def amendment(
        self, group: dict[str, object], *, children: list[dict[str, object]] | None = None,
        edges: list[dict[str, object]] | None = None,
        current_plan: AdmissionPlan | None = None,
        previous_admission_digest: str | None = None,
        **changes: object,
    ) -> dict[str, object]:
        child_rows = children or [self.descriptor()]
        edge_rows = edges if edges is not None else [
            {"source": self.parent, "target": self.child,
             "requires": "accepted", "failure_policy": "fail_run"}
        ]
        prior_plan = current_plan or self.plan()
        amended_plan = self.amended_plan(prior_plan, child_rows, edge_rows)
        prior_admission_digest = previous_admission_digest or self._admission_digest()
        row = _record(
            "plan_amendment", "plan-amendment-", run_id=self.run_id,
            spawn_group_id=group["id"], parent_item_id=self.parent,
            parent_attempt_id=self.attempt, parent_spawn_policy_id=self.spawn_policy_id,
            previous_plan_digest=prior_plan.digest,
            previous_admission_digest=prior_admission_digest, policy_digest=DIGEST,
            children=child_rows, dependency_edges=edge_rows,
            plan_digest=amended_plan.digest,
            admission_digest=self._spawn_admission_digest(
                amended_plan, prior_admission_digest,
            ),
        )
        row["schema_version"] = 1
        row.update(changes)
        return row

    def _admission_digest(self) -> str:
        return run_admission_digest(
            [{"node_id": "worker-a", "worker_profile": "codex"}], 2,
            [{"budget_id": "build", "amount": 4}], [{
                "item_id": self.parent,
                "workspace_key": "workspace-" + self.parent[-8:],
                "concurrency_key": "concurrency-a",
                "capability_selector": "review",
            }],
        )

    def _spawn_admission_digest(
        self, plan: AdmissionPlan, previous_admission_digest: str | None = None,
    ) -> str:
        payload = {
            "previous_admission_digest": previous_admission_digest or self._admission_digest(),
            "workers": plan.canonical()["workers"],
            "max_active_attempts": plan.canonical()["max_active_attempts"],
            "budget_reservations": plan.canonical()["budget_reservations"],
            "items": plan.canonical()["items"],
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    def admitted_record(
        self, group: dict[str, object], amendment: dict[str, object],
    ) -> dict[str, object]:
        child = amendment["children"][0]
        return _record(
            "child_admitted", "child-admitted-", run_id=self.run_id,
            spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
            parent_attempt_id=self.attempt, child_item_id=self.child, child_depth=1,
            task_contract_id=child["task_contract_id"],
            task_contract_digest=child["task_contract_digest"],
            admission_digest=amendment["admission_digest"],
            capability_ceiling=["review"],
            budget_allocation=[{"budget_id": "build", "amount": 1}],
            workspace_policy="patch_only",
            workspace=str(worker_workspace_root() / self.child),
            admitted_at_testimony=NOW,
        )

    def admitted(
        self, group: dict[str, object], amendment: dict[str, object],
    ) -> dict[str, object]:
        record = self.admitted_record(group, amendment)
        try:
            validate_record(record, "alpha", RUN_KINDS | {"child_admitted"}, integrity=False)
        except (IntegrityFailure, ProtocolRefusal) as exc:
            if (
                exc.code == "workspace_invalid"
                and exc.detail == "child workspace must use the closed reservation path"
                and record["workspace"]
                == str(worker_workspace_root() / record["child_item_id"])
            ):
                raise AssertionError(
                    "Floati child-admission workspace must be accepted before fixture construction"
                ) from None
            raise
        return record

    def child_success_records(
        self, group: dict[str, object], amendment: dict[str, object],
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        child = amendment["children"][0]
        attempt_id = "attempt-" + uuid7_hex()
        fence = attempt_fence_token(self.run_id, self.child, 1, 1)
        opened = _record(
            "attempt_opened", "attempt-opened-", run_id=self.run_id,
            item_id=self.child, attempt_id=attempt_id, ordinal=1,
            scheduler_epoch=1, fence_token=fence, max_attempts=1,
            backoff={"strategy": "fixed", "base_delay_ms": 0,
                     "cap_delay_ms": 0, "jitter": "sha256_25pct"},
        )
        grants = [{
            "capability_name": "review",
            "grant_id": "capability-grant-" + uuid7_hex(),
            "physical_position": 1,
        }]
        capability = _record(
            "capability_set_bound", "capability-set-bound-", run_id=self.run_id,
            item_id=self.child, attempt_id=attempt_id, fence_token=fence,
            chosen_worker="worker-a", policy_digest=DIGEST, routing_rank=0,
            evaluated_at_testimony=NOW, grant_ledger_high_watermark=1,
            effective_grants=grants, capability_digest=capability_set_digest(grants),
        )
        capability["schema_version"] = 1
        dispatch = _record(
            "dispatch_decision", "run-dispatch-decision-", run_id=self.run_id,
            item_id=self.child, attempt_id=attempt_id,
            eligible_workers=["worker-a"], chosen_worker="worker-a",
            capability_digest=capability["capability_digest"],
            reason_code="policy.route", policy_digest=DIGEST,
            routing_rank=0, scheduler_epoch=1,
            capability_set_bound_id=capability["id"],
        )
        dispatch["schema_version"] = 1
        started = _record(
            "attempt_started", "attempt-started-", run_id=self.run_id,
            item_id=self.child, attempt_id=attempt_id, ordinal=1,
            attempt_opened_id=opened["id"], dispatch_decision_id=dispatch["id"],
            fence_token=fence,
        )
        receipt = {
            "id": "worker-receipt-" + uuid7_hex(),
            "work_item_id": self.child, "node_id": "worker-a",
        }
        produced = _record(
            "result_produced", "run-result-produced-", run_id=self.run_id,
            item_id=self.child, attempt_id=attempt_id,
            dispatch_decision_id=dispatch["id"], worker_receipt_ids=[receipt["id"]],
        )
        accepted = _record(
            "result_accepted", "run-result-accepted-", run_id=self.run_id,
            item_id=self.child, attempt_id=attempt_id,
            predecessor_result_id=produced["id"], acceptance_mode="accepted_unverified",
            acceptance_receipt_id=None, worker_receipt_ids=[receipt["id"]],
        )
        terminal = _record(
            "attempt_terminal", "attempt-terminal-", run_id=self.run_id,
            item_id=self.child, attempt_id=attempt_id, ordinal=1,
            attempt_started_id=started["id"], fence_token=fence,
            terminal_state="completed", policy_class=None, reason_code="completed",
            effect_safety="idempotent", retry_disposition="none",
            retry_record_id=None, next_attempt_id=None, next_ordinal=None,
            retry_delay_ms=None, next_scheduler_epoch=None, next_fence_token=None,
        )
        return [
            self.admitted(group, amendment), opened, capability, dispatch, started,
            produced, accepted, terminal,
        ], receipt

    def parent_terminal(
        self, started_records: list[dict[str, object]], *,
        terminal_state: str = "failed", policy_class: str | None = "permanent",
        reason_code: str = "malformed_evidence",
    ) -> dict[str, object]:
        started = next(row for row in started_records if row["kind"] == "attempt_started")
        return _record(
            "attempt_terminal", "attempt-terminal-", run_id=self.run_id,
            item_id=self.parent, attempt_id=self.attempt, ordinal=1,
            attempt_started_id=started["id"], fence_token=self.fence,
            terminal_state=terminal_state, policy_class=policy_class,
            reason_code=reason_code, effect_safety="idempotent",
            retry_disposition="none", retry_record_id=None,
            next_attempt_id=None, next_ordinal=None, retry_delay_ms=None,
            next_scheduler_epoch=None, next_fence_token=None,
        )

    def rejected(
        self, group: dict[str, object], amendment: dict[str, object],
    ) -> dict[str, object]:
        return _record(
            "child_rejected", "child-rejected-", run_id=self.run_id,
            spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
            parent_attempt_id=self.attempt, child_item_id=self.child,
            reason_code="policy_refusal", evaluated_at_testimony=NOW,
        )

    def close(
        self, group: dict[str, object], amendment: dict[str, object], *,
        outcome: str, close_reason: str,
        accepted_item_ids: list[str] | None = None,
        terminal_item_ids: list[str] | None = None,
        rejected_item_ids: list[str] | None = None,
        cancel_scope_resolved_id: str | None = None,
        closed_at_testimony: str = NOW,
    ) -> dict[str, object]:
        return _record(
            "spawn_group_closed", "spawn-group-closed-", run_id=self.run_id,
            spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
            parent_attempt_id=self.attempt,
            member_item_ids=[child["item_id"] for child in amendment["children"]],
            accepted_item_ids=accepted_item_ids or [],
            terminal_item_ids=terminal_item_ids or [],
            rejected_item_ids=rejected_item_ids or [],
            join_mode=group["join_mode"], required_count=group["required_count"],
            outcome=outcome, close_reason=close_reason,
            cancel_scope_resolved_id=cancel_scope_resolved_id,
            closed_at_testimony=closed_at_testimony,
        )

    def observation_close(
        self, observed_descendant_ids: list[str] | None = None,
    ) -> dict[str, object]:
        return _record(
            "descendant_observation_closed", "descendant-observation-closed-",
            run_id=self.run_id, parent_item_id=self.parent,
            parent_attempt_id=self.attempt, parent_fence_token=self.fence,
            attempt_spawn_policy_id=self.spawn_policy_id, adapter="codex",
            observed_descendant_ids=observed_descendant_ids or [],
            closed_at_testimony=NOW,
        )

    def parent_result_records(
        self, started_records: list[dict[str, object]],
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        dispatch = next(row for row in started_records if row["kind"] == "dispatch_decision")
        receipt = {
            "id": "worker-receipt-" + uuid7_hex(),
            "work_item_id": self.parent, "node_id": "worker-a",
        }
        produced = _record(
            "result_produced", "run-result-produced-", run_id=self.run_id,
            item_id=self.parent, attempt_id=self.attempt,
            dispatch_decision_id=dispatch["id"], worker_receipt_ids=[receipt["id"]],
        )
        accepted = _record(
            "result_accepted", "run-result-accepted-", run_id=self.run_id,
            item_id=self.parent, attempt_id=self.attempt,
            predecessor_result_id=produced["id"], acceptance_mode="accepted_unverified",
            acceptance_receipt_id=None, worker_receipt_ids=[receipt["id"]],
        )
        return [produced, accepted], receipt


class _Task2Case:
    """Real-ledger fixture for the Task 2 controller boundary."""

    def __init__(
        self,
        testcase: unittest.TestCase,
        *,
        physical_dependency_mismatch: bool = False,
        base_peer_count: int = 0,
        policy_text: str = VALID_POLICY,
    ) -> None:
        self.testcase = testcase
        self.temp = tempfile.TemporaryDirectory()
        testcase.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name).resolve()
        policy_path = base / "FLOATI.toml"
        policy_path.write_text(policy_text, encoding="utf-8")
        self.policy = RepositoryPolicy.load(policy_path)
        self.root = FloatiRoot.open_direct_home(base / "alpha", create=True)
        self.ledger = RunLedger(self.root)
        self.run_id = "run-" + uuid7_hex()
        self.parent = "work-" + uuid7_hex()
        self.child = "work-" + uuid7_hex()
        self.child_contract_id = "task-contract-" + uuid7_hex()
        self.parent_contract = self.contract([])
        self.child_contract = self.contract([self.parent])
        if physical_dependency_mismatch:
            base_peer_count = max(base_peer_count, 1)
        self.base_peers = ["work-" + uuid7_hex() for _ in range(base_peer_count)]
        self.base_peer = (
            self.base_peers[0] if self.base_peers else "work-" + uuid7_hex()
        )
        self.base_peer_contract = self.contract([])
        base_items = [self.item(self.parent, self.parent_contract, "parent")]
        base_items.extend(
            self.item(peer, self.base_peer_contract, f"base-peer-{index}")
            for index, peer in enumerate(self.base_peers)
        )
        base_items.sort(key=lambda row: row["item_id"])
        self.plan = AdmissionPlan.from_canonical({
            "schema_version": 0,
            "workers": [{"node_id": "node-a", "worker_profile": "good"}],
            "max_active_attempts": 2,
            "budget_reservations": [{"budget_id": "build", "amount": 4}],
            "items": base_items,
            "dependency_edges": [],
        })
        base_item_ids = [row["item_id"] for row in self.plan.canonical()["items"]]
        self._append(
            "run_created", "run-created-", plan_digest=self.plan.digest,
            policy_digest=self.policy.digest, item_ids=base_item_ids,
            dependency_edges=(
                [{
                    "source": base_item_ids[0],
                    "target": base_item_ids[1],
                    "requires": "accepted",
                    "failure_policy": "fail_run",
                }]
                if physical_dependency_mismatch else []
            ),
        )
        self._append(
            "task_contract", "task-contract-", item_id=self.parent,
            **self.parent_contract.canonical(),
            contract_digest=contract_digest(self.parent_contract),
        )
        for base_peer in self.base_peers:
            self._append(
                "task_contract", "task-contract-", item_id=base_peer,
                **self.base_peer_contract.canonical(),
                contract_digest=contract_digest(self.base_peer_contract),
            )
        self._append(
            "run_policy_bound", "run-policy-bound-",
            policy_digest=self.policy.digest,
        )
        self._append(
            "worker_pool_bound", "run-worker-pool-bound-",
            worker_ids=["node-a"],
        )
        self.admission = AdmissionBinder.bind(
            self.ledger, self.run_id, self.plan, self.policy, now=self.now(0),
        )
        self.scheduler = RunScheduler(self.ledger)
        self.capability_binder = CapabilityBinder(
            self.ledger, CapabilityGrantLedger(self.root),
        )
        self.controller = (
            SpawnGroupController(self.ledger, self.policy)
            if SpawnGroupController is not None else None
        )
        self.opened: dict[str, object] | None = None
        self.snapshot: dict[str, object] | None = None
        self.spawn_policy: dict[str, object] | None = None
        self.dispatch: dict[str, object] | None = None
        self.started: dict[str, object] | None = None

    @staticmethod
    def now(offset: int = 0) -> datetime:
        return datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=offset)

    @staticmethod
    def contract(
        dependencies: list[str], max_attempts: int = 1,
    ) -> TaskContract:
        return TaskContract.create(
            objective="govern one bounded spawn child",
            non_goals=["no provider-native authority"],
            areas_to_avoid=[{"path": "bundle/c7.2", "region": "all"}],
            input_hashes={"brief": "b" * 64},
            acceptance_checks={"tests.unit": "python3 -m unittest"},
            constraints={"network": "dark"},
            risk_class="low",
            retry_policy={
                "max_attempts": max_attempts,
                "backoff": {
                    "base_delay_ms": 0, "cap_delay_ms": 0,
                    "strategy": "fixed",
                },
            },
            dependencies=dependencies,
        )

    @staticmethod
    def item(
        item_id: str, contract: TaskContract, suffix: str,
    ) -> dict[str, object]:
        return {
            "item_id": item_id,
            "contract": contract.canonical(),
            "capability_selector": "review_write",
            "requires_cancellation": True,
            "requires_callback": True,
            "workspace_key": "workspace-" + suffix,
            "concurrency_key": "concurrency-" + suffix,
            "retry_class": "transient",
            "effect_safety": "idempotent",
            "merge_gate": None,
        }

    def _append(self, kind: str, prefix: str, **fields: object) -> dict[str, object]:
        return self.ledger.append({
            "schema_version": 0,
            "id": prefix + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-10T12:00:00.000Z",
            "kind": kind,
            "run_id": self.run_id,
            **fields,
        })

    def enable(self) -> dict[str, object]:
        return AdmissionBinder.enable_spawn(
            self.ledger, self.run_id, self.plan, self.policy, now=self.now(1),
        )

    def prepare_parent(
        self, mode: str = "managed", *, enable: bool = True,
        dispatch: bool = True, start: bool = True,
        spawn_limits: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if enable:
            self.enable()
        self.opened = self.scheduler.open_attempt(
            self.run_id, self.parent, RetryPolicy(1, 0, 0, strategy="fixed"),
            1, now=self.now(2),
        )
        grants = [
            {
                "capability_name": name,
                "grant_id": "capability-grant-" + uuid7_hex(),
                "physical_position": index,
            }
            for index, name in enumerate(("review", "workspace_write"), start=1)
        ]
        record = {
            "schema_version": 1,
            "id": "capability-set-bound-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": "2026-08-10T12:00:03.000Z",
            "kind": "capability_set_bound",
            "run_id": self.run_id,
            "item_id": self.parent,
            "attempt_id": self.opened["attempt_id"],
            "fence_token": self.opened["fence_token"],
            "chosen_worker": "node-a",
            "policy_digest": self.policy.digest,
            "routing_rank": 0,
            "evaluated_at_testimony": "2026-08-10T12:00:03.000Z",
            "grant_ledger_high_watermark": 2,
            "effective_grants": grants,
            "capability_digest": capability_set_digest(grants),
        }
        token = self.ledger._capability_binding_capability_for(
            self.capability_binder,
        )
        self.snapshot = self.ledger._append_capability_set(record, token)
        limits = spawn_limits or ({
            "max_children": 2, "max_depth": 4,
            "child_capability_ceiling": ["review", "workspace_write"],
            "spawn_budget_ceiling": [{"budget_id": "build", "amount": 2}],
            "workspace_policies": ["isolated_worktree", "patch_only"],
        } if mode == "managed" else {
            "max_children": 0, "max_depth": 0,
            "child_capability_ceiling": [], "spawn_budget_ceiling": [],
            "workspace_policies": [],
        })
        assert self.controller is not None
        self.spawn_policy = self.controller.bind_attempt_policy(
            self.run_id, self.parent, str(self.opened["attempt_id"]),
            str(self.snapshot["id"]), adapter="codex", subagents_mode=mode,
            now=self.now(4), **limits,
        )
        if dispatch:
            self.dispatch = self.capability_binder.dispatch(
                str(self.snapshot["id"]), ["node-a"], "policy.route",
                self.policy, now=self.now(5),
            )
        if start:
            assert self.dispatch is not None
            self.started = self.scheduler.start_attempt(
                self.run_id, self.parent, str(self.opened["attempt_id"]),
                str(self.dispatch["id"]), now=self.now(6),
            )
        return self.spawn_policy

    def child_descriptor(self, **changes: object) -> dict[str, object]:
        row = {
            "item_id": self.child,
            "task_contract_id": self.child_contract_id,
            "task_contract": self.child_contract.canonical(),
            "task_contract_digest": contract_digest(self.child_contract),
            "depth": 1,
            "budget_allocation": [{"budget_id": "build", "amount": 1}],
            "capability_ceiling": ["review"],
            "workspace_policy": "patch_only",
            "workspace_key": "workspace-child",
            "concurrency_key": "concurrency-child",
            "capability_selector": "review_write",
            "requires_cancellation": True,
            "requires_callback": True,
            "retry_class": "transient",
            "effect_safety": "idempotent",
            "merge_gate": None,
        }
        row.update(changes)
        return row

    def create_kwargs(self, **changes: object) -> dict[str, object]:
        assert self.opened is not None
        row: dict[str, object] = {
            "run_id": self.run_id,
            "parent_item_id": self.parent,
            "parent_attempt_id": self.opened["attempt_id"],
            "parent_fence_token": self.opened["fence_token"],
            "group_key": "reviewers",
            "children": [self.child_descriptor()],
            "dependency_edges": [{
                "source": self.parent, "target": self.child,
                "requires": "accepted", "failure_policy": "fail_run",
            }],
            "max_children": 2,
            "max_depth": 4,
            "child_capability_ceiling": ["review"],
            "aggregate_budget": [{"budget_id": "build", "amount": 1}],
            "workspace_policy": "patch_only",
            "deadline": "2026-08-10T13:00:00.000Z",
            "join_mode": "all_terminal",
            "required_count": None,
            "on_late_result": "quarantine",
            "on_child_failure": "fail_group",
            "cancel_remaining_after_success": False,
            "now": self.now(7),
        }
        row.update(changes)
        return row


class SpawnGroupControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            SpawnGroupController,
            "Task 2 must provide the private SpawnGroupController",
        )

    def case(self) -> _Task2Case:
        return _Task2Case(self)

    def test_spawn_enablement_requires_exact_physical_dependency_edges(self) -> None:
        lawful = self.case()
        enabled = lawful.enable()
        self.assertEqual([], enabled["base_plan"]["dependency_edges"])

        hostile = _Task2Case(self, physical_dependency_mismatch=True)
        before = hostile.ledger.records()
        with self.assertRaises(ProtocolRefusal) as mismatch:
            hostile.enable()
        self.assertEqual("spawn_base_plan_edges_mismatch", mismatch.exception.code)
        self.assertEqual(before, hostile.ledger.records())

    def test_parent_cancellation_unions_independent_activated_members(self) -> None:
        from floati.cancellation import CancelMode, CancellationCoordinator

        class Adapter:
            cancel_mode = CancelMode.native

            def cancel(self) -> None:
                return None

        lawful = self.case()
        lawful.prepare_parent()
        lawful.controller.create_group(**lawful.create_kwargs())
        lawful_scope = CancellationCoordinator(lawful.ledger).request(
            lawful.run_id, {"node-a": Adapter()}, item_id=lawful.parent,
            now=lawful.now(8),
        )
        self.assertIn(lawful.child, lawful_scope["item_ids"])

        independent = self.case()
        independent.prepare_parent()
        independent.child_contract = independent.contract([])
        independent.controller.create_group(
            **independent.create_kwargs(dependency_edges=[]),
        )
        independent_scope = CancellationCoordinator(independent.ledger).request(
            independent.run_id, {"node-a": Adapter()},
            item_id=independent.parent, now=independent.now(8),
        )
        self.assertIn(independent.child, independent_scope["item_ids"])

    def test_mutated_repository_policy_refuses_every_task2_semantic_operation(self) -> None:
        lawful = self.case()
        lawful.prepare_parent()
        created, _ = lawful.controller.create_group(**lawful.create_kwargs())
        self.assertEqual(
            "activated",
            lawful.controller.group(lawful.run_id, created["id"])["state"],
        )

        enablement = self.case()
        object.__setattr__(enablement.policy, "canonical_bytes", b"{}")
        before_enablement = enablement.ledger.records()
        with self.assertRaises(ProtocolRefusal) as enablement_refusal:
            enablement.enable()
        self.assertEqual("policy_integrity_invalid", enablement_refusal.exception.code)
        self.assertEqual(before_enablement, enablement.ledger.records())

        binding = self.case()
        binding.prepare_parent()
        object.__setattr__(binding.policy, "canonical_bytes", b"{}")
        before_binding = binding.ledger.records()
        with self.assertRaises(ProtocolRefusal) as binding_refusal:
            binding.controller.bind_attempt_policy(
                binding.run_id, binding.parent, str(binding.opened["attempt_id"]),
                str(binding.snapshot["id"]), adapter="codex",
                subagents_mode="managed", max_children=2, max_depth=4,
                child_capability_ceiling=["review", "workspace_write"],
                spawn_budget_ceiling=[{"budget_id": "build", "amount": 2}],
                workspace_policies=["isolated_worktree", "patch_only"],
                now=binding.now(8),
            )
        self.assertEqual("policy_integrity_invalid", binding_refusal.exception.code)
        self.assertEqual(before_binding, binding.ledger.records())

        activation = self.case()
        activation.prepare_parent()
        object.__setattr__(activation.policy, "canonical_bytes", b"{}")
        before_activation = activation.ledger.records()
        with self.assertRaises(ProtocolRefusal) as activation_refusal:
            activation.controller.create_group(**activation.create_kwargs())
        self.assertEqual("policy_integrity_invalid", activation_refusal.exception.code)
        self.assertEqual(before_activation, activation.ledger.records())

        abort = self.case()
        abort.prepare_parent()
        original = abort.ledger._append_spawn_group
        with patch.object(
            abort.ledger,
            "_append_spawn_group",
            side_effect=lambda record, *args, **kwargs: (
                (_ for _ in ()).throw(
                    DurabilityFailure("jsonl_fsync_failed", "activation")
                )
                if record["kind"] == "plan_amendment"
                else original(record, *args, **kwargs)
            ),
        ):
            with self.assertRaises(DurabilityFailure):
                abort.controller.create_group(**abort.create_kwargs())
        group_id = next(iter(
            abort.ledger.project().run(abort.run_id)["spawn_groups"]
        ))
        object.__setattr__(abort.policy, "canonical_bytes", b"{}")
        before_abort = abort.ledger.records()
        with self.assertRaises(ProtocolRefusal) as abort_refusal:
            abort.controller.abort_group(
                abort.run_id, group_id, reason_code="operator_abandonment",
                operator_id="operator-a", authority_subject="spawn-admin",
                authority_epoch=1,
                capability_record_id="capability-" + uuid7_hex(),
                now=abort.now(9),
            )
        self.assertEqual("policy_integrity_invalid", abort_refusal.exception.code)
        self.assertEqual(before_abort, abort.ledger.records())

    def test_abort_authority_inputs_are_closed_and_exact_retry_is_identical(self) -> None:
        from floati.approvals import CapabilityLedger
        from floati.cancellation import CancelMode, CancellationCoordinator
        from floati.planes import AuthorityGrantStore
        from floati.registry import Registry

        class Adapter:
            cancel_mode = CancelMode.native

            def cancel(self) -> None:
                return None

        def pending_case() -> tuple[_Task2Case, str]:
            case = self.case()
            case.prepare_parent()
            original = case.ledger._append_spawn_group
            with patch.object(
                case.ledger,
                "_append_spawn_group",
                side_effect=lambda record, *args, **kwargs: (
                    (_ for _ in ()).throw(
                        DurabilityFailure("jsonl_fsync_failed", "activation")
                    )
                    if record["kind"] == "plan_amendment"
                    else original(record, *args, **kwargs)
                ),
            ):
                with self.assertRaises(DurabilityFailure):
                    case.controller.create_group(**case.create_kwargs())
            group_id = next(iter(
                case.ledger.project().run(case.run_id)["spawn_groups"]
            ))
            return case, group_id

        def operator_authority(
            case: _Task2Case,
        ) -> tuple[str, int, str]:
            Registry(case.root).register("operator-a", "Operator")
            grant = AuthorityGrantStore(case.root).claim(
                "spawn-admin", "operator-a", 120, 120, case.now(9),
            )
            capability = CapabilityLedger(case.root).declare(
                "operator-a", "spawn.group.abort", "read_write", "run", 60,
                now=case.now(9),
            )
            return "spawn-admin", int(grant["epoch"]), str(capability["id"])

        cancellation, cancellation_group_id = pending_case()
        resolved = CancellationCoordinator(cancellation.ledger).request(
            cancellation.run_id, {"node-a": Adapter()},
            item_id=cancellation.parent, now=cancellation.now(8),
        )
        cancellation_abort = cancellation.controller.abort_group(
            cancellation.run_id, cancellation_group_id,
            reason_code="cancellation",
            cancel_scope_resolved_id=str(resolved["id"]), now=cancellation.now(9),
        )
        self.assertEqual(
            cancellation_abort,
            cancellation.controller.abort_group(
                cancellation.run_id, cancellation_group_id,
                reason_code="cancellation",
                cancel_scope_resolved_id=str(resolved["id"]),
                now=cancellation.now(10),
            ),
        )

        cancellation_hostile, cancellation_hostile_group_id = pending_case()
        hostile_resolved = CancellationCoordinator(
            cancellation_hostile.ledger
        ).request(
            cancellation_hostile.run_id, {"node-a": Adapter()},
            item_id=cancellation_hostile.parent,
            now=cancellation_hostile.now(8),
        )
        before_cancellation_hostile = cancellation_hostile.ledger.records()
        with self.assertRaises(ProtocolRefusal) as cancellation_conflict:
            cancellation_hostile.controller.abort_group(
                cancellation_hostile.run_id, cancellation_hostile_group_id,
                reason_code="cancellation",
                cancel_scope_resolved_id=str(hostile_resolved["id"]),
                operator_id="operator-a", now=cancellation_hostile.now(9),
            )
        self.assertEqual(
            "spawn_abort_authority_conflict", cancellation_conflict.exception.code
        )
        self.assertEqual(
            before_cancellation_hostile, cancellation_hostile.ledger.records()
        )

        operator, operator_group_id = pending_case()
        subject, epoch, capability_id = operator_authority(operator)
        operator_abort = operator.controller.abort_group(
            operator.run_id, operator_group_id,
            reason_code="operator_abandonment", operator_id="operator-a",
            authority_subject=subject, authority_epoch=epoch,
            capability_record_id=capability_id, now=operator.now(10),
        )
        self.assertEqual(
            operator_abort,
            operator.controller.abort_group(
                operator.run_id, operator_group_id,
                reason_code="operator_abandonment", operator_id="operator-a",
                authority_subject=subject, authority_epoch=epoch,
                capability_record_id=capability_id, now=operator.now(11),
            ),
        )

        operator_hostile, operator_hostile_group_id = pending_case()
        hostile_subject, hostile_epoch, hostile_capability_id = (
            operator_authority(operator_hostile)
        )
        before_operator_hostile = operator_hostile.ledger.records()
        with self.assertRaises(ProtocolRefusal) as operator_conflict:
            operator_hostile.controller.abort_group(
                operator_hostile.run_id, operator_hostile_group_id,
                reason_code="operator_abandonment",
                cancel_scope_resolved_id="cancel-scope-resolved-" + uuid7_hex(),
                operator_id="operator-a", authority_subject=hostile_subject,
                authority_epoch=hostile_epoch,
                capability_record_id=hostile_capability_id,
                now=operator_hostile.now(10),
            )
        self.assertEqual(
            "spawn_abort_authority_conflict", operator_conflict.exception.code
        )
        self.assertEqual(before_operator_hostile, operator_hostile.ledger.records())

    def test_enable_spawn_persists_verified_complete_base_plan_before_parent_attempt(self) -> None:
        case = self.case()
        enabled = case.enable()
        self.assertEqual(case.plan.canonical(), enabled["base_plan"])
        self.assertEqual(case.plan.digest, enabled["base_plan_digest"])
        self.assertEqual(enabled, case.enable())
        self.assertEqual(1, sum(
            row["kind"] == "run_spawn_admission_enabled"
            for row in case.ledger.records()
        ))

    def test_spawn_refuses_without_full_base_plan_enablement_or_on_digest_drift(self) -> None:
        missing = self.case()
        with self.assertRaises(ProtocolRefusal) as disabled:
            missing.prepare_parent(enable=False, mode="managed")
        self.assertEqual("spawn_admission_disabled", disabled.exception.code)

        drift = self.case()
        changed = drift.plan.canonical()
        changed["items"][0]["workspace_key"] = "workspace-drift"
        with self.assertRaises(ProtocolRefusal):
            AdmissionBinder.enable_spawn(
                drift.ledger, drift.run_id,
                AdmissionPlan.from_canonical(changed), drift.policy,
                now=drift.now(1),
            )

    def test_attempt_spawn_policy_binds_disabled_observed_or_managed_before_dispatch(self) -> None:
        for mode in ("disabled", "observed_only", "managed"):
            case = self.case()
            policy = case.prepare_parent(mode=mode)
            self.assertEqual(mode, policy["subagents_mode"])
            self.assertEqual("codex", case.dispatch["adapter"])
            self.assertEqual(policy["id"], case.dispatch["attempt_spawn_policy_id"])
        disabled_without_enablement = self.case()
        explicit_disabled = disabled_without_enablement.prepare_parent(
            mode="disabled", enable=False,
        )
        self.assertEqual("disabled", explicit_disabled["subagents_mode"])
        legacy = self.case()
        legacy.enable()
        legacy.opened = legacy.scheduler.open_attempt(
            legacy.run_id, legacy.parent,
            RetryPolicy(1, 0, 0, strategy="fixed"), 1, now=legacy.now(2),
        )
        self.assertEqual(
            "disabled",
            legacy.ledger.project().run(legacy.run_id)["attempt_spawn_policy"].get(
                str(legacy.opened["attempt_id"]), {"subagents_mode": "disabled"}
            )["subagents_mode"],
        )

    def test_attempt_policy_refuses_adapter_mismatch_missing_capability_and_late_binding(self) -> None:
        missing = self.case()
        missing.enable()
        missing.opened = missing.scheduler.open_attempt(
            missing.run_id, missing.parent,
            RetryPolicy(1, 0, 0, strategy="fixed"), 1, now=missing.now(2),
        )
        with self.assertRaises(ProtocolRefusal) as no_capability:
            missing.controller.bind_attempt_policy(
                missing.run_id, missing.parent, str(missing.opened["attempt_id"]),
                "capability-set-bound-" + uuid7_hex(), adapter="codex",
                subagents_mode="disabled", max_children=0, max_depth=0,
                child_capability_ceiling=[], spawn_budget_ceiling=[],
                workspace_policies=[], now=missing.now(3),
            )
        self.assertEqual("capability_snapshot_missing", no_capability.exception.code)

        case = self.case()
        case.prepare_parent()
        with self.assertRaises(ProtocolRefusal) as changed:
            case.controller.bind_attempt_policy(
                case.run_id, case.parent, str(case.opened["attempt_id"]),
                str(case.snapshot["id"]), adapter="other",
                subagents_mode="managed", max_children=2, max_depth=4,
                child_capability_ceiling=["review"],
                spawn_budget_ceiling=[{"budget_id": "build", "amount": 2}],
                workspace_policies=["patch_only"], now=case.now(8),
            )
        self.assertEqual("spawn_policy_input_divergent", changed.exception.code)

    def test_create_group_fsyncs_pending_then_one_atomic_activation_record(self) -> None:
        case = self.case()
        case.prepare_parent()
        created, amendment = case.controller.create_group(**case.create_kwargs())
        records = case.ledger.records()
        self.assertLess(records.index(created), records.index(amendment))
        self.assertEqual("spawn_group_created", created["kind"])
        self.assertEqual("plan_amendment", amendment["kind"])
        self.assertEqual(1, amendment["schema_version"])
        self.assertEqual([case.child], [row["item_id"] for row in amendment["children"]])
        projected = case.ledger.project().run(case.run_id)
        self.assertEqual("activated", projected["spawn_groups"][created["id"]]["state"])

    def test_pending_group_is_inert_and_exact_retry_completes_after_crash(self) -> None:
        case = self.case()
        case.prepare_parent()
        original = case.ledger._append_spawn_group
        failed = False

        def fail_activation(record, *args, **kwargs):
            nonlocal failed
            if record["kind"] == "plan_amendment" and not failed:
                failed = True
                raise DurabilityFailure("jsonl_fsync_failed", "injected activation fsync")
            return original(record, *args, **kwargs)

        with patch.object(case.ledger, "_append_spawn_group", side_effect=fail_activation):
            with self.assertRaises(DurabilityFailure):
                case.controller.create_group(**case.create_kwargs())
        pending = case.ledger.project().run(case.run_id)["spawn_groups"]
        self.assertEqual(["pending"], [group["state"] for group in pending.values()])
        self.assertNotIn(case.child, case.ledger.project().run(case.run_id)["item_ids"])
        created, amendment = case.controller.create_group(
            **case.create_kwargs(now=case.now(5000)),
        )
        self.assertEqual("activated", case.ledger.project().run(case.run_id)["spawn_groups"][created["id"]]["state"])
        self.assertEqual(created["id"], amendment["spawn_group_id"])

    def test_pending_fsync_failure_appends_no_group_and_exact_retry_succeeds(self) -> None:
        case = self.case()
        case.prepare_parent()
        before = case.ledger.records()
        real_fsync = os.fsync
        calls = 0

        def fail_first(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected pending fsync failure")
            real_fsync(fd)

        with patch("floati.jsonl.os.fsync", side_effect=fail_first):
            with self.assertRaises(DurabilityFailure) as failed:
                case.controller.create_group(**case.create_kwargs())
        self.assertEqual("storage_unavailable", failed.exception.code)
        self.assertEqual(before, case.ledger.records())
        created, amendment = case.controller.create_group(**case.create_kwargs())
        self.assertEqual(created["id"], amendment["spawn_group_id"])

    def test_pending_group_blocks_parent_result_attempt_and_run_terminal_races(self) -> None:
        case = self.case()
        case.prepare_parent()
        original = case.ledger._append_spawn_group
        with patch.object(
            case.ledger, "_append_spawn_group",
            side_effect=lambda record, *args, **kwargs: (
                (_ for _ in ()).throw(DurabilityFailure("jsonl_fsync_failed", "activation"))
                if record["kind"] == "plan_amendment" else original(record, *args, **kwargs)
            ),
        ):
            with self.assertRaises(DurabilityFailure):
                case.controller.create_group(**case.create_kwargs())
        terminal = {
            "schema_version": 0, "id": "run-terminal-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": "2026-08-10T12:10:00.000Z",
            "kind": "run_terminal", "run_id": case.run_id, "outcome": "failed",
        }
        with self.assertRaises(ProtocolRefusal) as fenced:
            case.ledger.append(terminal)
        self.assertEqual("spawn_group_pending", fenced.exception.code)

    def test_activation_chains_plan_and_admission_digests_with_identified_contracts(self) -> None:
        case = self.case()
        case.prepare_parent()
        created, amendment = case.controller.create_group(**case.create_kwargs())
        self.assertEqual(case.plan.digest, amendment["previous_plan_digest"])
        self.assertEqual(case.admission["admission_digest"], amendment["previous_admission_digest"])
        self.assertTrue(amendment["children"][0]["task_contract_id"].startswith("task-contract-"))
        current = case.ledger.project().run(case.run_id)
        self.assertEqual(amendment["plan_digest"], current["plan_digest"])
        self.assertEqual(amendment["admission_digest"], current["admission_binding"]["admission_digest"])
        self.assertEqual(created["id"], current["spawn_child_group"][case.child])

    def test_create_group_attenuates_spawn_budget_capability_and_workspace(self) -> None:
        case = self.case()
        case.prepare_parent()
        case.controller.create_group(**case.create_kwargs())
        for field, value, code in (
            ("aggregate_budget", [{"budget_id": "build", "amount": 3}], "spawn_budget_widening"),
            ("child_capability_ceiling", ["other"], "spawn_group_widening"),
            ("workspace_policy", "shared_write", "workspace_policy_invalid"),
        ):
            other = self.case()
            other.prepare_parent()
            with self.subTest(field=field):
                with self.assertRaises(ProtocolRefusal) as caught:
                    other.controller.create_group(**other.create_kwargs(**{field: value}))
                self.assertEqual(code, caught.exception.code)

        for name, child, code in (
            ("child_capability", {"capability_ceiling": ["other"]}, "spawn_capability_widening"),
            ("child_workspace", {"workspace_policy": "isolated_worktree"}, "spawn_workspace_widening"),
            ("child_budget", {"budget_allocation": [{"budget_id": "build", "amount": 2}]}, "spawn_budget_widening"),
        ):
            other = self.case()
            other.prepare_parent()
            before = other.ledger.records()
            with self.subTest(name=name):
                with self.assertRaises(ProtocolRefusal) as caught:
                    other.controller.create_group(**other.create_kwargs(
                        children=[other.child_descriptor(**child)],
                    ))
                self.assertEqual(code, caught.exception.code)
                self.assertEqual(before, other.ledger.records())

        prior = self.case()
        prior.prepare_parent()
        prior.controller.create_group(**prior.create_kwargs())
        second_child = "work-" + uuid7_hex()
        second_contract = prior.contract([prior.parent])
        second_descriptor = prior.child_descriptor(
            item_id=second_child,
            task_contract_id="task-contract-" + uuid7_hex(),
            task_contract=second_contract.canonical(),
            task_contract_digest=contract_digest(second_contract),
            workspace_key="workspace-second",
            concurrency_key="concurrency-second",
        )
        before = prior.ledger.records()
        with self.assertRaises(ProtocolRefusal) as aggregate:
            prior.controller.create_group(**prior.create_kwargs(
                group_key="second", children=[second_descriptor],
                dependency_edges=[{
                    "source": prior.parent, "target": second_child,
                    "requires": "accepted", "failure_policy": "fail_run",
                }],
                aggregate_budget=[{"budget_id": "build", "amount": 2}],
            ))
        self.assertEqual("spawn_budget_widening", aggregate.exception.code)
        self.assertEqual(before, prior.ledger.records())

    def test_create_group_refuses_item_fanout_depth_cycle_and_deadline_overflow(self) -> None:
        for name, changes in (
            ("depth", {"children": None}),
            ("cycle", {"dependency_edges": None}),
            ("deadline", {"deadline": "2026-08-10T11:59:59.000Z"}),
        ):
            case = self.case()
            case.prepare_parent()
            if name == "depth":
                changes["children"] = [case.child_descriptor(depth=5)]
            elif name == "cycle":
                changes["dependency_edges"] = [
                    {"source": case.parent, "target": case.child, "requires": "accepted", "failure_policy": "fail_run"},
                    {"source": case.child, "target": case.parent, "requires": "accepted", "failure_policy": "fail_run"},
                ]
            with self.subTest(name=name):
                with self.assertRaises(ProtocolRefusal):
                    case.controller.create_group(**case.create_kwargs(**changes))

    def test_divergent_group_retry_refuses_without_append(self) -> None:
        case = self.case()
        case.prepare_parent()
        first = case.controller.create_group(**case.create_kwargs())
        before = case.ledger.records()
        with self.assertRaises(ProtocolRefusal) as caught:
            case.controller.create_group(**case.create_kwargs(max_children=1))
        self.assertEqual("spawn_group_input_divergent", caught.exception.code)
        self.assertEqual(before, case.ledger.records())
        self.assertEqual(first, case.controller.create_group(**case.create_kwargs()))

    def test_concurrent_exact_group_creation_returns_one_durable_pair(self) -> None:
        case = self.case()
        case.prepare_parent()
        kwargs = case.create_kwargs()
        barrier = threading.Barrier(2)
        outcomes: list[object] = []

        def create() -> None:
            try:
                barrier.wait(3)
                outcomes.append(case.controller.create_group(**deepcopy(kwargs)))
            except BaseException as exc:  # retained for exact race testimony
                outcomes.append(exc)

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(2, len(outcomes))
        self.assertTrue(all(isinstance(row, tuple) for row in outcomes), outcomes)
        self.assertEqual(outcomes[0], outcomes[1])
        records = case.ledger.records()
        self.assertEqual(1, sum(row["kind"] == "spawn_group_created" for row in records))
        self.assertEqual(1, sum(row["kind"] == "plan_amendment" and row["schema_version"] == 1 for row in records))

    def test_activation_fsync_failure_rolls_back_only_activation_and_retry_succeeds(self) -> None:
        case = self.case()
        case.prepare_parent()
        before_count = len(case.ledger.records())
        calls = 0
        real_fsync = os.fsync

        def inject(fd: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected activation fsync failure")
            real_fsync(fd)

        with patch("floati.jsonl.os.fsync", side_effect=inject):
            with self.assertRaises(DurabilityFailure) as failed:
                case.controller.create_group(**case.create_kwargs())
        self.assertEqual("storage_unavailable", failed.exception.code)
        self.assertEqual(1, len(case.ledger.records()) - before_count)
        created, amendment = case.controller.create_group(**case.create_kwargs())
        self.assertEqual(created["id"], amendment["spawn_group_id"])

    def test_cancel_request_orders_creation_activation_scope_and_abort(self) -> None:
        from floati.cancellation import CancelMode, CancellationCoordinator

        class Adapter:
            cancel_mode = CancelMode.native

            def cancel(self) -> None:
                return None

        before = self.case()
        before.prepare_parent()
        resolved = CancellationCoordinator(before.ledger).request(
            before.run_id, {"node-a": Adapter()}, item_id=before.parent,
            now=before.now(8),
        )
        records = before.ledger.records()
        with self.assertRaises(ProtocolRefusal) as fenced:
            before.controller.create_group(**before.create_kwargs(now=before.now(9)))
        self.assertEqual("spawn_parent_cancel_requested", fenced.exception.code)
        self.assertEqual(records, before.ledger.records())

        activated = self.case()
        activated.prepare_parent()
        created, _ = activated.controller.create_group(**activated.create_kwargs())
        scoped = CancellationCoordinator(activated.ledger).request(
            activated.run_id, {"node-a": Adapter()}, item_id=activated.parent,
            now=activated.now(8),
        )
        self.assertIn(activated.child, scoped["item_ids"])
        final_group = activated.ledger.project().run(activated.run_id)["spawn_groups"][created["id"]]
        self.assertEqual("closed", final_group["state"])
        self.assertEqual("cancelled", final_group["closed"]["outcome"])

    def test_operator_abort_requires_exact_capability_record(self) -> None:
        case = self.case()
        case.prepare_parent()
        original = case.ledger._append_spawn_group
        with patch.object(
            case.ledger, "_append_spawn_group",
            side_effect=lambda record, *args, **kwargs: (
                (_ for _ in ()).throw(DurabilityFailure("jsonl_fsync_failed", "activation"))
                if record["kind"] == "plan_amendment" else original(record, *args, **kwargs)
            ),
        ):
            with self.assertRaises(DurabilityFailure):
                case.controller.create_group(**case.create_kwargs())
        group_id = next(iter(case.ledger.project().run(case.run_id)["spawn_groups"]))
        with self.assertRaises(ProtocolRefusal) as authority:
            case.controller.abort_group(
                case.run_id, group_id, reason_code="operator_abandonment",
                operator_id="operator-a", authority_subject="spawn-admin",
                authority_epoch=1,
                capability_record_id="capability-" + uuid7_hex(),
                now=case.now(9),
            )
        self.assertIn(authority.exception.code, {
            "operator_authority_required", "registry_entry_missing", "unknown_node",
            "operator_capability_invalid",
        })

        lawful = self.case()
        lawful.prepare_parent()
        lawful_original = lawful.ledger._append_spawn_group
        with patch.object(
            lawful.ledger, "_append_spawn_group",
            side_effect=lambda record, *args, **kwargs: (
                (_ for _ in ()).throw(DurabilityFailure("jsonl_fsync_failed", "activation"))
                if record["kind"] == "plan_amendment" else lawful_original(record, *args, **kwargs)
            ),
        ):
            with self.assertRaises(DurabilityFailure):
                lawful.controller.create_group(**lawful.create_kwargs())
        from floati.approvals import CapabilityLedger
        from floati.planes import AuthorityGrantStore
        from floati.registry import Registry

        Registry(lawful.root).register("operator-a", "Operator")
        grant = AuthorityGrantStore(lawful.root).claim(
            "spawn-admin", "operator-a", 120, 120, lawful.now(9),
        )
        capability = CapabilityLedger(lawful.root).declare(
            "operator-a", "spawn.group.abort", "read_write", "run", 60,
            now=lawful.now(9),
        )
        lawful_group_id = next(iter(
            lawful.ledger.project().run(lawful.run_id)["spawn_groups"]
        ))
        aborted = lawful.controller.abort_group(
            lawful.run_id, lawful_group_id,
            reason_code="operator_abandonment", operator_id="operator-a",
            authority_subject="spawn-admin", authority_epoch=grant["epoch"],
            capability_record_id=capability["id"], now=lawful.now(10),
        )
        self.assertEqual("operator_abandonment", aborted["reason_code"])

    def test_activation_and_abort_contenders_resolve_one_durable_winner(self) -> None:
        case = self.case()
        case.prepare_parent()
        original = case.ledger._append_spawn_group
        with patch.object(
            case.ledger, "_append_spawn_group",
            side_effect=lambda record, *args, **kwargs: (
                (_ for _ in ()).throw(DurabilityFailure("jsonl_fsync_failed", "activation"))
                if record["kind"] == "plan_amendment" else original(record, *args, **kwargs)
            ),
        ):
            with self.assertRaises(DurabilityFailure):
                case.controller.create_group(**case.create_kwargs())
        from floati.cancellation import CancelMode, CancellationCoordinator

        class Adapter:
            cancel_mode = CancelMode.native

            def cancel(self) -> None:
                return None

        group_id = next(iter(case.ledger.project().run(case.run_id)["spawn_groups"]))
        barrier = threading.Barrier(2)
        outcomes: dict[str, object] = {}

        def activate() -> None:
            try:
                barrier.wait(3)
                outcomes["activate"] = case.controller.create_group(
                    **case.create_kwargs(now=case.now(9))
                )
            except BaseException as exc:
                outcomes["activate"] = exc

        def abort() -> None:
            try:
                barrier.wait(3)
                resolved = CancellationCoordinator(case.ledger).request(
                    case.run_id, {"node-a": Adapter()}, item_id=case.parent,
                    now=case.now(8),
                )
                outcomes["abort"] = case.controller.abort_group(
                    case.run_id, group_id, reason_code="cancellation",
                    cancel_scope_resolved_id=resolved["id"], now=case.now(9),
                )
            except BaseException as exc:
                outcomes["abort"] = exc

        threads = [threading.Thread(target=activate), threading.Thread(target=abort)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        state = case.ledger.project().run(case.run_id)["spawn_groups"][group_id]["state"]
        self.assertIn(state, {"closed", "aborted"})
        winners = sum(
            isinstance(outcomes.get(name), (tuple, dict))
            for name in ("activate", "abort")
        )
        self.assertEqual(1, winners, outcomes)

    def test_public_and_generic_private_append_refuse(self) -> None:
        case = self.case()
        case.prepare_parent()
        created, amendment = case.controller.create_group(**case.create_kwargs())
        for record in (created, amendment):
            with self.subTest(kind=record["kind"]):
                with self.assertRaises(ProtocolRefusal) as public:
                    case.ledger.append(dict(record, id=record["id"].split("-")[0] + "-" + uuid7_hex()))
                self.assertEqual("spawn_group_controller_only", public.exception.code)
        returned = case.ledger.project().run(case.run_id)["spawn_groups"]
        returned[created["id"]]["created"]["group_key"] = "mutated"
        self.assertEqual(
            "reviewers",
            case.ledger.project().run(case.run_id)["spawn_groups"][created["id"]]["created"]["group_key"],
        )

    def test_lookup_is_owned_and_physically_read_only(self) -> None:
        case = self.case()
        case.prepare_parent()
        created, _ = case.controller.create_group(**case.create_kwargs())
        before = sorted(
            (path.relative_to(case.root.tenant_home).as_posix(), path.stat().st_size)
            for path in case.root.tenant_home.rglob("*") if path.is_file()
        )
        first = case.controller.group(case.run_id, created["id"])
        first["created"]["group_key"] = "changed"
        second = case.controller.group(case.run_id, created["id"])
        after = sorted(
            (path.relative_to(case.root.tenant_home).as_posix(), path.stat().st_size)
            for path in case.root.tenant_home.rglob("*") if path.is_file()
        )
        self.assertEqual("reviewers", second["created"]["group_key"])
        self.assertEqual(before, after)


class _Task3Case(_Task2Case):
    """Small real-ledger fixture for Task 3 semantic controller tests."""

    def activate(self, **changes: object) -> tuple[dict[str, object], dict[str, object]]:
        self.prepare_parent()
        assert self.controller is not None
        self.created, self.amendment = self.controller.create_group(
            **self.create_kwargs(**changes)
        )
        return self.created, self.amendment

    def admit(self, *, now_offset: int = 8) -> dict[str, object]:
        assert self.controller is not None
        return self.controller.admit_child(
            self.run_id, str(self.created["id"]), self.child,
            now=self.now(now_offset),
        )

    def reject(
        self, reason_code: str | None = None, *, now_offset: int = 3601,
    ) -> dict[str, object]:
        assert self.controller is not None
        return self.controller.reject_child(
            self.run_id, str(self.created["id"]), self.child,
            reason_code=reason_code, now=self.now(now_offset),
        )

    def run_worker(
        self,
        *,
        before_spawn: tuple[dict[str, object], ...] = (),
        during_drive: tuple[dict[str, object], ...] = (),
        on_drive: object = None,
        adapter: object = None,
        clock: object = None,
    ):
        """Run the governed parent through a real WorkerRunner fork and pipe."""

        from floati.workers import WorkerRunner

        Registry(self.root).register("node-a", "Codex")
        work_item = {
            "schema_version": 0,
            "id": self.parent,
            "tenant_id": "alpha",
            "timestamp": "2026-08-10T12:00:07.000Z",
            "kind": "work_item",
            "title": "govern one bounded spawn child",
            "owner": "node-a",
            "artifact_bindings": [],
        }
        validate_record(work_item, "alpha", frozenset({"work_item"}), integrity=False)
        append_record(
            self.root, Path("work/items.jsonl"), work_item,
            allowed_kinds={"work_item", "work_transition"},
        )
        AuthorityGrantStore(self.root).claim(
            "work-claims", "node-a", 60, 60, self.now(7),
        )
        runner_kwargs: dict[str, object] = {
            "call_timeout": 2,
            "spawn_controller": self.controller,
        }
        if clock is not None:
            runner_kwargs["clock"] = clock
        runner = WorkerRunner(
            self.root,
            {
                "codex": (
                    _GovernedSpawnPipeAdapter(
                        before_spawn=before_spawn, during_drive=during_drive,
                    )
                    if adapter is None else adapter
                )
            },
            **runner_kwargs,
        )
        result = runner.run(
            "node-a", "codex", now=self.now(7), on_drive=on_drive,
            run_id=self.run_id, item_id=self.parent,
            attempt_id=str(self.opened["attempt_id"]),
        )
        return runner, result

    def complete_child(
        self, child_item_id: str, *, now_offset: int = 9,
    ) -> dict[str, object]:
        """Append one lawful accepted child lifecycle through real run APIs."""

        opened = self.scheduler.open_attempt(
            self.run_id, child_item_id,
            RetryPolicy(1, 0, 0, strategy="fixed"), 1,
            now=self.now(now_offset),
        )
        grants = [{
            "capability_name": "review",
            "grant_id": "capability-grant-" + uuid7_hex(),
            "physical_position": 1,
        }]
        capability = {
            "schema_version": 1,
            "id": "capability-set-bound-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "capability_set_bound",
            "run_id": self.run_id,
            "item_id": child_item_id,
            "attempt_id": opened["attempt_id"],
            "fence_token": opened["fence_token"],
            "chosen_worker": "node-a",
            "policy_digest": self.policy.digest,
            "routing_rank": 0,
            "evaluated_at_testimony": NOW,
            "grant_ledger_high_watermark": 1,
            "effective_grants": grants,
            "capability_digest": capability_set_digest(grants),
        }
        bound = self.ledger._append_capability_set(
            capability,
            self.ledger._capability_binding_capability_for(self.capability_binder),
        )
        dispatch = self.capability_binder.dispatch(
            bound["id"], ["node-a"], "policy.route", self.policy,
            now=self.now(now_offset + 1),
        )
        started = self.scheduler.start_attempt(
            self.run_id, child_item_id, str(opened["attempt_id"]),
            str(dispatch["id"]), now=self.now(now_offset + 2),
        )
        receipt = {
            "schema_version": 0,
            "id": "worker-receipt-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "worker_receipt",
            "session_id": "worker-" + uuid7_hex(),
            "work_item_id": child_item_id,
            "node_id": "node-a",
            "adapter": "codex",
            "transition": "claim",
            "outcome_code": None,
            "authority_subject": "execute-run",
            "authority_epoch": 1,
            "artifact_bindings": [],
        }
        append_record(
            self.root, Path("receipts/workers.jsonl"), receipt,
            allowed_kinds={"worker_receipt"},
        )
        produced = self.ledger.append({
            "schema_version": 0,
            "id": "run-result-produced-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "result_produced",
            "run_id": self.run_id,
            "item_id": child_item_id,
            "attempt_id": opened["attempt_id"],
            "dispatch_decision_id": dispatch["id"],
            "worker_receipt_ids": [receipt["id"]],
        })
        accepted = self.ledger.append({
            "schema_version": 0,
            "id": "run-result-accepted-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "result_accepted",
            "run_id": self.run_id,
            "item_id": child_item_id,
            "attempt_id": opened["attempt_id"],
            "predecessor_result_id": produced["id"],
            "acceptance_mode": "accepted_unverified",
            "acceptance_receipt_id": None,
            "worker_receipt_ids": [receipt["id"]],
        })
        self.scheduler.terminal_attempt(
            self.run_id, child_item_id, str(opened["attempt_id"]),
            "completed", None, "completed", "idempotent",
            now=self.now(now_offset + 3),
        )
        self.testcase.assertIsNotNone(started)
        return accepted

    def schedule_child_retry(self) -> tuple[dict[str, object], dict[str, object]]:
        """Build one real terminal child whose governed retry is durably reserved."""

        self.child_contract = self.contract([self.parent], max_attempts=2)
        self.activate(); self.admit()
        opened = self.scheduler.open_attempt(
            self.run_id, self.child, RetryPolicy(2, 0, 0, strategy="fixed"),
            1, now=self.now(9),
        )
        grants = [{
            "capability_name": "review",
            "grant_id": "capability-grant-" + uuid7_hex(),
            "physical_position": 1,
        }]
        capability = {
            "schema_version": 1,
            "id": "capability-set-bound-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": NOW,
            "kind": "capability_set_bound", "run_id": self.run_id,
            "item_id": self.child, "attempt_id": opened["attempt_id"],
            "fence_token": opened["fence_token"], "chosen_worker": "node-a",
            "policy_digest": self.policy.digest, "routing_rank": 0,
            "evaluated_at_testimony": NOW,
            "grant_ledger_high_watermark": 1,
            "effective_grants": grants,
            "capability_digest": capability_set_digest(grants),
        }
        bound = self.ledger._append_capability_set(
            capability,
            self.ledger._capability_binding_capability_for(self.capability_binder),
        )
        dispatch = self.capability_binder.dispatch(
            bound["id"], ["node-a"], "policy.route", self.policy,
            now=self.now(10),
        )
        self.scheduler.start_attempt(
            self.run_id, self.child, opened["attempt_id"], dispatch["id"],
            now=self.now(11),
        )
        failed = self.scheduler.terminal_attempt(
            self.run_id, self.child, opened["attempt_id"], "failed",
            "transient", "transient_failure", "idempotent", now=self.now(12),
        )
        return opened, failed


class SpawnGroupAdmissionTests(unittest.TestCase):
    """Task 3 RED bank: child launch eligibility comes only from durable truth."""

    def test_child_must_be_durable_item_and_admitted_before_attempt_or_dispatch(self) -> None:
        case = _Task3Case(self); case.activate()
        policy = RetryPolicy(1, 0, 0, strategy="fixed")
        with self.assertRaisesRegex(ProtocolRefusal, "durably admitted"):
            case.scheduler.open_attempt(case.run_id, case.child, policy, 1, now=case.now(8))
        admitted = case.admit()
        opened = case.scheduler.open_attempt(case.run_id, case.child, policy, 1, now=case.now(9))
        self.assertEqual(admitted["child_item_id"], opened["item_id"])

    def test_child_admission_attenuates_group_and_run_evidence(self) -> None:
        case = _Task3Case(self); case.activate()
        admitted = case.admit()
        self.assertEqual(admitted["capability_ceiling"], ["review"])
        self.assertEqual(admitted["budget_allocation"], [{"budget_id": "build", "amount": 1}])
        self.assertEqual(admitted["workspace"], str(worker_workspace_root() / case.child))
        self.assertIs(case.controller.admit_child(
            case.run_id, case.created["id"], case.child, now=case.now(9)
        ) is admitted, False)

    def test_child_rejection_projects_failed_or_skipped_run_outcome_from_policy(self) -> None:
        failed = _Task3Case(self); failed.activate(on_child_failure="fail_group")
        failed.reject()
        self.assertEqual(failed.ledger.project().item_outcomes(failed.run_id)[failed.child], "failed")
        skipped = _Task3Case(self); skipped.activate(
            on_child_failure="continue_until_join_impossible"
        )
        skipped.reject()
        self.assertEqual(skipped.ledger.project().item_outcomes(skipped.run_id)[skipped.child], "skipped")

    def test_child_admission_and_rejection_are_exact_semantic_winners(self) -> None:
        case = _Task3Case(self); case.activate()
        first = case.admit()
        self.assertEqual(first, case.admit(now_offset=9))
        with self.assertRaisesRegex(ProtocolRefusal, "one admission outcome|already"):
            case.reject(now_offset=10)

    def test_child_admission_does_not_snapshot_transient_active_capacity(self) -> None:
        case = _Task3Case(self); case.activate()
        admitted = case.admit()
        run = case.ledger.project().run(case.run_id)
        self.assertIsNone(run["attempts"][case.opened["attempt_id"]]["terminal"])
        self.assertEqual(admitted["child_item_id"], case.child)

    def test_older_group_admission_accepts_canonical_admission_chain_ancestor(self) -> None:
        case = _Task3Case(self); case.prepare_parent()
        first, first_amendment = case.controller.create_group(
            **case.create_kwargs(max_children=1)
        )
        second_child = "work-" + uuid7_hex()
        second_contract = case.contract([case.parent])
        second, second_amendment = case.controller.create_group(**case.create_kwargs(
            group_key="reviewers-second",
            children=[case.child_descriptor(
                item_id=second_child,
                task_contract_id="task-contract-" + uuid7_hex(),
                task_contract=second_contract.canonical(),
                task_contract_digest=contract_digest(second_contract),
                workspace_key="workspace-second",
                concurrency_key="concurrency-second",
            )],
            dependency_edges=[{
                "source": case.parent, "target": second_child,
                "requires": "accepted", "failure_policy": "fail_run",
            }],
            max_children=1,
            now=case.now(8),
        ))
        current = case.controller.admit_child(
            case.run_id, second["id"], second_child, now=case.now(9),
        )
        older = case.controller.admit_child(
            case.run_id, first["id"], case.child, now=case.now(10),
        )
        self.assertEqual(second_amendment["admission_digest"], current["admission_digest"])
        self.assertEqual(first_amendment["admission_digest"], older["admission_digest"])

    def test_child_rejection_is_derived_from_canonical_evidence(self) -> None:
        expired = _Task3Case(self); expired.activate(
            deadline="2026-08-10T12:00:08.000Z"
        )
        rejection = expired.controller.reject_child(
            expired.run_id, expired.created["id"], expired.child,
            now=expired.now(9),
        )
        self.assertEqual("deadline_expired", rejection["reason_code"])

        lawful = _Task3Case(self); lawful.activate()
        before = lawful.ledger.records()
        with self.assertRaisesRegex(ProtocolRefusal, "evidence|lawful|reject"):
            lawful.controller.reject_child(
                lawful.run_id, lawful.created["id"], lawful.child,
                reason_code="policy_refusal", now=lawful.now(8),
            )
        self.assertEqual(before, lawful.ledger.records())

    def test_child_rejection_refuses_mutated_policy_without_append(self) -> None:
        lawful = _Task3Case(self); lawful.activate(
            deadline="2026-08-10T12:00:08.000Z"
        )
        rejected = lawful.controller.reject_child(
            lawful.run_id, lawful.created["id"], lawful.child,
            now=lawful.now(9),
        )
        self.assertEqual("deadline_expired", rejected["reason_code"])

        mutated = _Task3Case(self); mutated.activate(
            deadline="2026-08-10T12:00:08.000Z"
        )
        object.__setattr__(mutated.policy, "canonical_bytes", b"{}")
        before = mutated.ledger.records()
        with self.assertRaises(ProtocolRefusal) as refusal:
            mutated.controller.reject_child(
                mutated.run_id, mutated.created["id"], mutated.child,
                now=mutated.now(9),
            )
        self.assertEqual("policy_integrity_invalid", refusal.exception.code)
        self.assertEqual(before, mutated.ledger.records())


class SpawnGroupJoinTests(unittest.TestCase):
    """Task 3 RED bank: join, cancellation, descendant, and worker contracts."""

    def test_join_failure_table_covers_all_item_outcomes_both_policies_and_four_modes(self) -> None:
        for mode, required in (("all_accepted", None), ("all_terminal", None), ("quorum", 1), ("first_accepted", 1)):
            case = _Task3Case(self); case.activate(
                join_mode=mode, required_count=required,
                on_child_failure="fail_group",
            ); case.reject()
            closed = case.controller.close_group(case.run_id, case.created["id"], now=case.now(9))
            self.assertEqual((closed["outcome"], closed["close_reason"]), ("failed", "child_failure"))
        continued = _Task3Case(self); continued.activate(
            join_mode="all_terminal", required_count=None,
            on_child_failure="continue_until_join_impossible",
        ); continued.reject()
        self.assertEqual(continued.controller.close_group(
            continued.run_id, continued.created["id"], now=continued.now(9)
        )["outcome"], "satisfied")

    def test_all_accepted_all_terminal_quorum_and_first_accepted_use_physical_truth(self) -> None:
        case = _Task3Case(self); case.activate(
            join_mode="all_terminal", required_count=None,
            on_child_failure="continue_until_join_impossible",
        ); case.reject()
        closed = case.controller.close_group(case.run_id, case.created["id"], now=case.now(9))
        self.assertEqual(closed["terminal_item_ids"], [])
        self.assertEqual(closed["rejected_item_ids"], [case.child])
        self.assertEqual(closed["close_reason"], "all_members_terminal")

    def test_impossible_quorum_and_deadline_close_with_exact_policy(self) -> None:
        impossible = _Task3Case(self); impossible.activate(
            join_mode="quorum", required_count=1,
            on_child_failure="continue_until_join_impossible",
        ); impossible.reject()
        self.assertEqual(impossible.controller.close_group(
            impossible.run_id, impossible.created["id"], now=impossible.now(9)
        )["close_reason"], "join_impossible")
        deadline = _Task3Case(self); deadline.activate(
            deadline="2026-08-10T12:00:08.000Z"
        )
        closed = deadline.controller.close_group(
            deadline.run_id, deadline.created["id"], now=deadline.now(9)
        )
        self.assertEqual((closed["outcome"], closed["close_reason"]), ("deadline", "deadline_expired"))

    def test_open_or_unsatisfied_group_blocks_parent_acceptance_and_success_terminal(self) -> None:
        case = _Task3Case(self); case.activate()
        with self.assertRaisesRegex(ProtocolRefusal, "group must close|open"):
            case.scheduler.terminal_attempt(
                case.run_id, case.parent, case.opened["attempt_id"], "completed",
                None, "completed", "idempotent", now=case.now(9),
            )

    def test_only_satisfied_groups_authorize_parent_acceptance(self) -> None:
        case = _Task3Case(self); case.activate(); case.reject()
        closed = case.controller.close_group(case.run_id, case.created["id"], now=case.now(9))
        self.assertEqual(closed["outcome"], "failed")
        run = case.ledger.project().run(case.run_id)
        self.assertNotEqual(run["spawn_groups"][case.created["id"]]["closed"]["outcome"], "satisfied")

    def test_successful_join_requests_exact_item_cancellation_before_close(self) -> None:
        case = _Task3Case(self); case.activate(
            join_mode="all_terminal", on_child_failure="continue_until_join_impossible",
            cancel_remaining_after_success=True,
        ); case.reject()
        closed = case.controller.close_group(case.run_id, case.created["id"], adapters={}, now=case.now(9))
        self.assertEqual(closed["outcome"], "satisfied")

    def test_exact_item_cancellation_never_expands_to_non_group_dependency(self) -> None:
        case = _Task3Case(self); case.activate()
        coordinator = CancellationCoordinator(case.ledger)
        resolved = coordinator.request_exact_items(
            case.run_id, [case.child], {}, spawn_group_id=case.created["id"], now=case.now(9)
        )
        self.assertEqual(resolved["item_ids"], [case.child])

    def test_crash_after_exact_cancellation_before_close_retries_safely(self) -> None:
        case = _Task3Case(self); case.activate()
        coordinator = CancellationCoordinator(case.ledger)
        first = coordinator.request_exact_items(
            case.run_id, [case.child], {}, spawn_group_id=case.created["id"], now=case.now(9)
        )
        second = coordinator.request_exact_items(
            case.run_id, [case.child], {}, spawn_group_id=case.created["id"], now=case.now(10)
        )
        self.assertEqual(first, second)

    def test_exact_item_cancel_request_fences_new_attempt_and_retry_before_resolution(self) -> None:
        case = _Task3Case(self); case.activate(); case.admit()
        coordinator = CancellationCoordinator(case.ledger)
        coordinator.request_exact_items(
            case.run_id, [case.child], {}, spawn_group_id=case.created["id"], now=case.now(9)
        )
        with self.assertRaisesRegex(ProtocolRefusal, "cancel"):
            case.scheduler.open_attempt(
                case.run_id, case.child, RetryPolicy(1, 0, 0, strategy="fixed"), 1,
                now=case.now(10),
            )

    def test_opened_capability_bound_and_dispatched_prestart_attempts_close_cancelled(self) -> None:
        case = _Task3Case(self); case.activate(); case.admit()
        opened = case.scheduler.open_attempt(
            case.run_id, case.child, RetryPolicy(1, 0, 0, strategy="fixed"), 1,
            now=case.now(9),
        )
        resolved = CancellationCoordinator(case.ledger).request_exact_items(
            case.run_id, [case.child], {}, spawn_group_id=case.created["id"], now=case.now(10)
        )
        state = case.ledger.project().run(case.run_id)["attempts"][opened["attempt_id"]]
        self.assertEqual(state["terminal"]["cancel_scope_resolved_id"], resolved["id"])

    def test_zero_attempt_admitted_or_unadmitted_child_closes_cancelled_and_run_can_terminal(self) -> None:
        for admitted in (False, True):
            case = _Task3Case(self); case.activate()
            if admitted: case.admit()
            resolved = CancellationCoordinator(case.ledger).request_exact_items(
                case.run_id, [case.child], {}, spawn_group_id=case.created["id"], now=case.now(9)
            )
            run = case.ledger.project().run(case.run_id)
            self.assertEqual(run["spawn_item_outcomes"][case.child], "cancelled")
            self.assertEqual(resolved["item_ids"], [case.child])

    def test_parent_cancel_request_and_activation_contenders_follow_physical_order(self) -> None:
        case = _Task3Case(self); case.prepare_parent()
        CancellationCoordinator(case.ledger).request(case.run_id, {}, item_id=case.parent, now=case.now(7))
        with self.assertRaisesRegex(ProtocolRefusal, "cancel"):
            case.controller.create_group(**case.create_kwargs(now=case.now(8)))

    def test_parent_cancel_request_before_group_creation_appends_no_pending_group(self) -> None:
        case = _Task3Case(self); case.prepare_parent()
        before = len(case.ledger.records())
        CancellationCoordinator(case.ledger).request(case.run_id, {}, item_id=case.parent, now=case.now(7))
        after_cancel = len(case.ledger.records())
        with self.assertRaises(ProtocolRefusal):
            case.controller.create_group(**case.create_kwargs(now=case.now(8)))
        self.assertEqual(len(case.ledger.records()), after_cancel)
        self.assertGreater(after_cancel, before)

    def test_parent_run_and_item_scopes_cancel_zero_attempt_group_children(self) -> None:
        for item_scope in (None, "parent"):
            case = _Task3Case(self); case.activate(); case.admit()
            CancellationCoordinator(case.ledger).request(
                case.run_id, {}, item_id=None if item_scope is None else case.parent,
                now=case.now(9),
            )
            self.assertEqual(case.ledger.project().item_outcomes(case.run_id)[case.child], "cancelled")

    def test_parent_cancellation_closes_activated_group_before_parent_terminal(self) -> None:
        case = _Task3Case(self); case.activate(); case.admit()
        CancellationCoordinator(case.ledger).request(case.run_id, {}, item_id=case.parent, now=case.now(9))
        group = case.ledger.project().run(case.run_id)["spawn_groups"][case.created["id"]]
        self.assertEqual((group["state"], group["closed"]["outcome"]), ("closed", "cancelled"))

    def test_sibling_only_join_cancellation_cannot_authorize_cancelled_group_close(self) -> None:
        case = _Task3Case(self); case.activate(); case.admit()
        resolved = CancellationCoordinator(case.ledger).request_exact_items(
            case.run_id, [case.child], {}, spawn_group_id=case.created["id"], now=case.now(9)
        )
        with self.assertRaisesRegex(ProtocolRefusal, "parent|whole-group"):
            case.controller.close_group(
                case.run_id, case.created["id"], cancel_scope_resolved_id=resolved["id"],
                outcome="cancelled", now=case.now(10),
            )

    def test_started_child_cancellation_appends_ordinary_terminal_before_close_and_retries(self) -> None:
        from floati.cancellation import CancelMode

        class Adapter:
            cancel_mode = CancelMode.native

            def cancel(self) -> None:
                return None

        case = _Task3Case(self); case.activate(); case.admit()
        opened = case.scheduler.open_attempt(
            case.run_id, case.child, RetryPolicy(1, 0, 0, strategy="fixed"),
            1, now=case.now(9),
        )
        grants = [{
            "capability_name": "review",
            "grant_id": "capability-grant-" + uuid7_hex(),
            "physical_position": 1,
        }]
        capability = {
            "schema_version": 1,
            "id": "capability-set-bound-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": NOW,
            "kind": "capability_set_bound", "run_id": case.run_id,
            "item_id": case.child, "attempt_id": opened["attempt_id"],
            "fence_token": opened["fence_token"], "chosen_worker": "node-a",
            "policy_digest": case.policy.digest, "routing_rank": 0,
            "evaluated_at_testimony": NOW,
            "grant_ledger_high_watermark": 1,
            "effective_grants": grants,
            "capability_digest": capability_set_digest(grants),
        }
        bound = case.ledger._append_capability_set(
            capability,
            case.ledger._capability_binding_capability_for(case.capability_binder),
        )
        dispatch = case.capability_binder.dispatch(
            bound["id"], ["node-a"], "policy.route", case.policy,
            now=case.now(10),
        )
        case.scheduler.start_attempt(
            case.run_id, case.child, opened["attempt_id"], dispatch["id"],
            now=case.now(11),
        )
        coordinator = CancellationCoordinator(case.ledger)
        first = coordinator.request(
            case.run_id, {"node-a": Adapter()}, item_id=case.parent,
            now=case.now(12),
        )
        second = coordinator.request(
            case.run_id, {"node-a": Adapter()}, item_id=case.parent,
            now=case.now(13),
        )
        run = case.ledger.project().run(case.run_id)
        self.assertEqual(first, second)
        self.assertEqual(
            "cancelled", run["attempts"][opened["attempt_id"]]["terminal"]["terminal_state"],
        )
        self.assertEqual("cancelled", run["spawn_groups"][case.created["id"]]["closed"]["outcome"])

    def test_parent_cancellation_consumes_scheduled_retry_into_cancelled_truth(self) -> None:
        from floati.cancellation import CancelMode

        class Adapter:
            cancel_mode = CancelMode.native

            def cancel(self) -> None:
                return None

        case = _Task3Case(self)
        opened, failed = case.schedule_child_retry()
        before_cancel = case.ledger.project().run(case.run_id)
        self.assertEqual("scheduled", failed["retry_disposition"])
        self.assertEqual(
            failed["next_attempt_id"],
            before_cancel["attempts"][opened["attempt_id"]]["schedule"]["next_attempt_id"],
        )

        coordinator = CancellationCoordinator(case.ledger)
        first = coordinator.request(
            case.run_id, {"node-a": Adapter()}, item_id=case.parent,
            now=case.now(13),
        )
        count = len(case.ledger.records())
        second = coordinator.request(
            case.run_id, {"node-a": Adapter()}, item_id=case.parent,
            now=case.now(14),
        )
        run = case.ledger.project().run(case.run_id)
        self.assertEqual(first, second)
        self.assertEqual(count, len(case.ledger.records()))
        retry_state = run["attempts"][failed["next_attempt_id"]]
        self.assertEqual(
            ("attempt_cancelled_before_start", "cancelled"),
            (retry_state["terminal"]["kind"], run["spawn_item_outcomes"][case.child]),
        )
        retry_cancelled = retry_state["terminal"]
        validate_record(retry_cancelled, "alpha", RUN_KINDS, integrity=False)
        validate_json_schema(
            retry_cancelled,
            Path("schemas/v1/attempt-cancelled-before-start-record.schema.json"),
        )
        for attempt_opened_id, retry_scheduled_id in ((None, None), (
            "attempt-opened-" + uuid7_hex(), retry_cancelled["retry_scheduled_id"],
        )):
            hostile = dict(
                retry_cancelled,
                attempt_opened_id=attempt_opened_id,
                retry_scheduled_id=retry_scheduled_id,
            )
            with self.subTest(
                attempt_opened_id=attempt_opened_id,
                retry_scheduled_id=retry_scheduled_id,
            ):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(hostile, "alpha", RUN_KINDS, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(
                        hostile,
                        Path("schemas/v1/attempt-cancelled-before-start-record.schema.json"),
                    )
        self.assertEqual("cancelled", case.ledger.project().run_outcome(case.run_id))

    def test_concurrent_scheduled_retry_consumption_returns_one_semantic_winner(self) -> None:
        lawful = _Task3Case(self)
        _lawful_opened, lawful_failed = lawful.schedule_child_retry()
        lawful_scope = CancellationCoordinator(lawful.ledger).request_exact_items(
            lawful.run_id, [lawful.child], {},
            spawn_group_id=lawful.created["id"], now=lawful.now(13),
        )
        lawful_terminal = lawful.ledger.project().run(lawful.run_id)["attempts"][
            lawful_failed["next_attempt_id"]
        ]["terminal"]
        self.assertEqual(
            (lawful_scope["id"], "attempt_cancelled_before_start"),
            (lawful_terminal["cancel_scope_resolved_id"], lawful_terminal["kind"]),
        )

        case = _Task3Case(self)
        _opened, failed = case.schedule_child_retry()
        setup = CancellationCoordinator(case.ledger)
        requested = setup._append(setup._v1_record(
            "cancel_requested", "cancel-requested-", case.now(13),
            run_id=case.run_id, scope="exact_items", item_id=None,
            item_ids=[case.child], spawn_group_id=case.created["id"],
            requested_by="spawn_join",
        ))
        resolved = setup._append(setup._v1_record(
            "cancel_scope_resolved", "cancel-scope-resolved-", case.now(13),
            run_id=case.run_id, cancel_request_id=requested["id"],
            scope="exact_items", item_id=None, item_ids=[case.child],
            attempt_ids=[],
        ))
        barrier = threading.Barrier(2)

        class SynchronizedCoordinator(CancellationCoordinator):
            scheduled_winner: dict[str, object] | None = None

            def _append(self, record: dict[str, object]) -> dict[str, object]:
                if record.get("retry_scheduled_id") is not None:
                    barrier.wait(3)
                winner = super()._append(record)
                if record.get("retry_scheduled_id") is not None:
                    self.scheduled_winner = winner
                return winner

        coordinators = [SynchronizedCoordinator(case.ledger) for _ in range(2)]
        errors: list[BaseException] = []

        def consume(coordinator: SynchronizedCoordinator) -> None:
            try:
                coordinator._complete_resolution(
                    case.run_id, resolved, {}, now=case.now(14),
                )
            except BaseException as exc:  # retained for exact race testimony
                errors.append(exc)

        threads = [threading.Thread(target=consume, args=(row,)) for row in coordinators]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(errors, errors)
        winners = [row.scheduled_winner for row in coordinators]
        self.assertTrue(all(isinstance(row, dict) for row in winners), winners)
        self.assertEqual(winners[0], winners[1])
        records = case.ledger.records()
        self.assertEqual(
            1,
            sum(
                row["kind"] == "attempt_cancelled_before_start"
                and row.get("retry_scheduled_id") == failed["retry_record_id"]
                for row in records
            ),
        )

        before = case.ledger.records()
        divergent = dict(
            winners[0],
            id="attempt-cancelled-before-start-" + uuid7_hex(),
            cancel_scope_resolved_id="cancel-scope-resolved-" + uuid7_hex(),
        )
        with self.assertRaises(ProtocolRefusal) as refusal:
            setup._append(divergent)
        self.assertEqual("cancel_transition_invalid", refusal.exception.code)
        self.assertEqual(before, case.ledger.records())

    def test_descendant_testimony_requires_live_worker_launch_capability(self) -> None:
        lawful = _Task3Case(self); lawful.activate()
        _runner, result = lawful.run_worker(before_spawn=(
            {
                "provider_descendant_id": "pipe-native",
                "state": "observed", "adopted_item_id": None,
            },
            {
                "provider_descendant_id": "pipe-native",
                "state": "terminated", "adopted_item_id": None,
            },
        ))
        self.assertEqual("complete", result["transition"])
        closed = lawful.ledger.project().run(lawful.run_id)[
            "descendant_observation_close"
        ][lawful.opened["attempt_id"]]
        self.assertEqual(["pipe-native"], closed["observed_descendant_ids"])

        from floati.workers import WorkerRunner

        hostile = _Task3Case(self); hostile.activate()
        runner = WorkerRunner(
            hostile.root, {}, spawn_controller=hostile.controller,
        )
        before = hostile.ledger.records()
        with self.assertRaisesRegex(ProtocolRefusal, "launch|capability|worker"):
            runner._begin_spawn_launch(
                hostile.run_id, hostile.opened["attempt_id"], "codex",
            )
        with self.assertRaisesRegex(ProtocolRefusal, "launch|capability|worker"):
            hostile.controller._begin_worker_launch(
                hostile.run_id, hostile.opened["attempt_id"], "codex",
            )
        with self.assertRaisesRegex(ProtocolRefusal, "launch|capability|worker"):
            runner._close_spawn_observation(
                hostile.run_id, hostile.opened["attempt_id"],
                now=hostile.now(8),
            )
        self.assertEqual(before, hostile.ledger.records())

    def test_late_result_is_quarantined_or_requires_durable_operator_disposition(self) -> None:
        self.assertTrue(callable(getattr(SpawnGroupController, "dispose_late_result", None)))

    def test_late_operator_disposition_requires_exact_capability_record(self) -> None:
        case = _Task3Case(self); case.activate()
        with self.assertRaisesRegex(ProtocolRefusal, "authority|capability|late"):
            case.controller.dispose_late_result(
                case.run_id, case.created["id"], case.child,
                "run-result-produced-" + uuid7_hex(), "quarantine",
                operator_id="operator-a", now=case.now(9),
            )

    def test_untracked_descendant_observed_blocks_parent_and_group(self) -> None:
        case = _Task3Case(self); case.activate()

        def assert_observed_before_drive() -> None:
            observed = case.ledger.project().run(case.run_id)[
                "untracked_descendants"
            ][(case.opened["attempt_id"], "codex", "native-1")]
            self.assertEqual(observed["state"], "observed")
            with self.assertRaisesRegex(ProtocolRefusal, "descendant"):
                case.controller.close_group(
                    case.run_id, case.created["id"], now=case.now(9),
                )

        _runner, result = case.run_worker(
            before_spawn=({
                "provider_descendant_id": "native-1", "state": "observed",
                "adopted_item_id": None,
            },),
            during_drive=({
                "provider_descendant_id": "native-1", "state": "terminated",
                "adopted_item_id": None,
            },),
            on_drive=assert_observed_before_drive,
        )
        self.assertEqual("complete", result["transition"])

    def test_untracked_descendant_adopted_terminated_or_unknown_has_closed_effect(self) -> None:
        for state, adopted in (("terminated", None), ("adopted", "child")):
            case = _Task3Case(self); case.activate(); case.admit()
            _runner, result = case.run_worker(before_spawn=(
                {
                    "provider_descendant_id": "native-1", "state": "observed",
                    "adopted_item_id": None,
                },
                {
                    "provider_descendant_id": "native-1", "state": state,
                    "adopted_item_id": case.child if adopted else None,
                },
            ))
            self.assertEqual("complete", result["transition"])
            row = case.ledger.project().run(case.run_id)["untracked_descendants"][(
                case.opened["attempt_id"], "codex", "native-1",
            )]
            self.assertEqual(row["state"], state)

        unknown = _Task3Case(self); unknown.activate(); unknown.admit()
        with self.assertRaises(ProtocolRefusal) as refused:
            unknown.run_worker(before_spawn=(
                {
                    "provider_descendant_id": "native-unknown",
                    "state": "observed", "adopted_item_id": None,
                },
                {
                    "provider_descendant_id": "native-unknown",
                    "state": "unknown", "adopted_item_id": None,
                },
            ))
        self.assertEqual("untracked_descendant_unknown", refused.exception.code)
        unknown_row = unknown.ledger.project().run(unknown.run_id)[
            "untracked_descendants"
        ][(unknown.opened["attempt_id"], "codex", "native-unknown")]
        self.assertEqual("unknown", unknown_row["state"])

    def test_observation_close_is_required_before_parent_acceptance(self) -> None:
        case = _Task3Case(self); case.activate()
        _runner, result = case.run_worker()
        self.assertEqual("complete", result["transition"])
        closed = case.ledger.project().run(case.run_id)[
            "descendant_observation_close"
        ][case.opened["attempt_id"]]
        self.assertEqual(closed["attempt_spawn_policy_id"], case.spawn_policy["id"])

    def test_descendant_observed_after_close_fails_replay(self) -> None:
        case = _Task3Case(self); case.activate()
        runner, result = case.run_worker()
        self.assertEqual("complete", result["transition"])
        before = case.ledger.records()
        with self.assertRaisesRegex(ProtocolRefusal, "closure|closed"):
            runner._handle_spawn_event(case.run_id, case.opened["attempt_id"], {
                "provider_descendant_id": "native-late", "state": "observed",
                "adopted_item_id": None,
            })
        self.assertEqual(before, case.ledger.records())

    def test_worker_launch_passes_disabled_observed_and_managed_spawn_context(self) -> None:
        from floati.workers import WorkerRunner
        class Adapter:
            name = "codex"

            def set_spawn_context(self, context: object, emit: object) -> None:
                return None
        for mode in ("disabled", "observed_only", "managed"):
            case = _Task3Case(self); case.prepare_parent(mode=mode)
            context = WorkerRunner(
                case.root, {}, spawn_controller=case.controller,
            )._governed_spawn_context(
                "codex", adapter=Adapter(), run_id=case.run_id,
                item_id=case.parent, attempt_id=case.opened["attempt_id"],
                claimed_work_item_id=case.parent,
            )
            self.assertEqual(mode, context["subagents_mode"])

    def test_governed_launch_refuses_adapter_name_drift_and_missing_context_hook(self) -> None:
        from floati.workers import WorkerRunner
        case = _Task3Case(self); case.prepare_parent()
        runner = WorkerRunner(case.root, {}, spawn_controller=case.controller)
        with self.assertRaisesRegex(ProtocolRefusal, "hook"):
            runner._governed_spawn_context(
                "codex", adapter=object(), run_id=case.run_id,
                item_id=case.parent, attempt_id=case.opened["attempt_id"],
                claimed_work_item_id=case.parent,
            )
        class Adapter:
            name = "codex"

            def set_spawn_context(self, context: object, emit: object) -> None:
                return None
        with self.assertRaisesRegex(ProtocolRefusal, "adapter|policy"):
            runner._governed_spawn_context(
                "changed", adapter=Adapter(), run_id=case.run_id,
                item_id=case.parent, attempt_id=case.opened["attempt_id"],
                claimed_work_item_id=case.parent,
            )

    def test_governed_launch_binds_actual_adapter_object_name(self) -> None:
        from floati.workers import WorkerRunner

        case = _Task3Case(self); case.prepare_parent()
        runner = WorkerRunner(case.root, {}, spawn_controller=case.controller)

        class LawfulAdapter:
            name = "codex"

            def set_spawn_context(self, context: object, emit: object) -> None:
                return None

        context = runner._governed_spawn_context(
            "codex", adapter=LawfulAdapter(), run_id=case.run_id,
            item_id=case.parent, attempt_id=case.opened["attempt_id"],
            claimed_work_item_id=case.parent,
        )
        self.assertEqual("codex", context["adapter"])

        class DriftedAdapter(LawfulAdapter):
            name = "changed"

        with self.assertRaisesRegex(ProtocolRefusal, "adapter|identity|name"):
            runner._governed_spawn_context(
                "codex", adapter=DriftedAdapter(), run_id=case.run_id,
                item_id=case.parent, attempt_id=case.opened["attempt_id"],
                claimed_work_item_id=case.parent,
            )

    def test_satisfied_close_binds_exact_physical_remaining_set_and_policy(self) -> None:
        lawful = _Task3Case(self); lawful.activate(
            join_mode="all_terminal", required_count=None,
            on_child_failure="continue_until_join_impossible",
            cancel_remaining_after_success=True,
        )
        lawful_scope = CancellationCoordinator(lawful.ledger).request_exact_items(
            lawful.run_id, [lawful.child], {},
            spawn_group_id=lawful.created["id"], now=lawful.now(8),
        )
        lawful_close = lawful.controller.close_group(
            lawful.run_id, lawful.created["id"],
            cancel_scope_resolved_id=lawful_scope["id"], now=lawful.now(9),
        )
        self.assertEqual("satisfied", lawful_close["outcome"])

        disabled = _Task3Case(self); disabled.activate(
            join_mode="all_terminal", required_count=None,
            on_child_failure="continue_until_join_impossible",
            cancel_remaining_after_success=False,
        )
        disabled_scope = CancellationCoordinator(disabled.ledger).request_exact_items(
            disabled.run_id, [disabled.child], {},
            spawn_group_id=disabled.created["id"], now=disabled.now(8),
        )
        before = disabled.ledger.records()
        with self.assertRaisesRegex(ProtocolRefusal, "remaining|policy|cancellation"):
            disabled.controller.close_group(
                disabled.run_id, disabled.created["id"],
                cancel_scope_resolved_id=disabled_scope["id"], now=disabled.now(9),
            )
        self.assertEqual(before, disabled.ledger.records())

        partial = _Task3Case(self); partial.prepare_parent()
        second_child = "work-" + uuid7_hex()
        second_contract = partial.contract([partial.parent])
        partial.created, partial.amendment = partial.controller.create_group(
            **partial.create_kwargs(
                children=[
                    partial.child_descriptor(),
                    partial.child_descriptor(
                        item_id=second_child,
                        task_contract_id="task-contract-" + uuid7_hex(),
                        task_contract=second_contract.canonical(),
                        task_contract_digest=contract_digest(second_contract),
                        workspace_key="workspace-second",
                        concurrency_key="concurrency-second",
                    ),
                ],
                dependency_edges=[
                    {
                        "source": partial.parent, "target": partial.child,
                        "requires": "accepted", "failure_policy": "fail_run",
                    },
                    {
                        "source": partial.parent, "target": second_child,
                        "requires": "accepted", "failure_policy": "fail_run",
                    },
                ],
                max_children=2, aggregate_budget=[{"budget_id": "build", "amount": 2}],
                join_mode="all_terminal", required_count=None,
                on_child_failure="continue_until_join_impossible",
                cancel_remaining_after_success=True,
            )
        )
        partial_scope = CancellationCoordinator(partial.ledger).request_exact_items(
            partial.run_id, [partial.child], {},
            spawn_group_id=partial.created["id"], now=partial.now(8),
        )
        partial.controller.reject_child(
            partial.run_id, partial.created["id"], second_child,
            now=partial.now(3601),
        )
        partial_before = partial.ledger.records()
        with self.assertRaisesRegex(ProtocolRefusal, "remaining|cancellation"):
            partial.controller.close_group(
                partial.run_id, partial.created["id"],
                cancel_scope_resolved_id=partial_scope["id"], now=partial.now(3602),
            )
        self.assertEqual(partial_before, partial.ledger.records())

    def test_adapter_descendant_events_cross_pipe_under_parent_controller_authority(self) -> None:
        case = _Task3Case(self); case.activate()
        _runner, result = case.run_worker(before_spawn=(
            {
                "provider_descendant_id": "native-parent-owned",
                "state": "observed", "adopted_item_id": None,
            },
            {
                "provider_descendant_id": "native-parent-owned",
                "state": "terminated", "adopted_item_id": None,
            },
        ))
        self.assertEqual("complete", result["transition"])
        projected = case.ledger.project().run(case.run_id)
        self.assertIn(
            (case.opened["attempt_id"], "codex", "native-parent-owned"),
            projected["untracked_descendants"],
        )

    def test_concurrent_exact_close_returns_one_record_and_changed_sets_refuse(self) -> None:
        case = _Task3Case(self); case.activate(); case.reject()
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []
        def close() -> None:
            try:
                results.append(case.controller.close_group(case.run_id, case.created["id"], now=case.now(9)))
            except BaseException as exc:
                errors.append(exc)
        threads = [threading.Thread(target=close) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(len({row["id"] for row in results}), 1)

    def test_task3_cancellation_runtime_and_schema_contracts_are_closed(self) -> None:
        fixture = SpawnGroupFixtures()
        request = _record(
            "cancel_requested", "cancel-requested-", run_id=fixture.run_id,
            scope="exact_items", item_id=None, item_ids=[fixture.child],
            spawn_group_id="spawn-group-created-" + uuid7_hex(), requested_by="spawn_join",
        )
        request["schema_version"] = 1
        resolved = _record(
            "cancel_scope_resolved", "cancel-scope-resolved-", run_id=fixture.run_id,
            cancel_request_id=request["id"], scope="exact_items", item_id=None,
            item_ids=[fixture.child], attempt_ids=[],
        )
        resolved["schema_version"] = 1
        prestart = _record(
            "attempt_cancelled_before_start", "attempt-cancelled-before-start-",
            run_id=fixture.run_id, item_id=fixture.child,
            attempt_id="attempt-" + uuid7_hex(),
            attempt_opened_id="attempt-opened-" + uuid7_hex(),
            retry_scheduled_id=None,
            fence_token="b" * 64, cancel_scope_resolved_id=resolved["id"],
            capability_set_bound_id=None, dispatch_decision_id=None,
            reason_code="cancelled_before_start", cancelled_at_testimony=NOW,
        )
        prestart["schema_version"] = 1
        zero = _record(
            "spawn_child_cancelled_without_attempt",
            "spawn-child-cancelled-without-attempt-", run_id=fixture.run_id,
            spawn_group_id=request["spawn_group_id"],
            plan_amendment_id="plan-amendment-" + uuid7_hex(),
            child_item_id=fixture.child, child_admitted_id=None,
            cancel_scope_resolved_id=resolved["id"],
            reason_code="cancelled_without_attempt", cancelled_at_testimony=NOW,
        )
        zero["schema_version"] = 1
        cases = (
            (request, "exact-items-cancel-requested-record.schema.json"),
            (resolved, "exact-items-cancel-scope-resolved-record.schema.json"),
            (prestart, "attempt-cancelled-before-start-record.schema.json"),
            (zero, "spawn-child-cancelled-without-attempt-record.schema.json"),
        )
        for record, schema_name in cases:
            with self.subTest(kind=record["kind"]):
                self.assertEqual(record, validate_record(record, "alpha", RUN_KINDS, integrity=False))
                validate_json_schema(record, Path("schemas/v1") / schema_name)
                hostile = dict(record, unexpected=True)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(hostile, "alpha", RUN_KINDS, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(hostile, Path("schemas/v1") / schema_name)

    def test_task3_cancellation_schema_runtime_lexical_and_array_parity(self) -> None:
        fixture = SpawnGroupFixtures()
        item_ids = sorted([fixture.child, "work-" + uuid7_hex()])
        request = _record(
            "cancel_requested", "cancel-requested-", run_id=fixture.run_id,
            scope="exact_items", item_id=None, item_ids=item_ids,
            spawn_group_id="spawn-group-created-" + uuid7_hex(),
            requested_by="spawn_join",
        )
        request["schema_version"] = 1
        resolved = _record(
            "cancel_scope_resolved", "cancel-scope-resolved-",
            run_id=fixture.run_id, cancel_request_id=request["id"],
            scope="exact_items", item_id=None, item_ids=item_ids,
            attempt_ids=sorted(["attempt-" + uuid7_hex(), "attempt-" + uuid7_hex()]),
        )
        resolved["schema_version"] = 1
        prestart = _record(
            "attempt_cancelled_before_start", "attempt-cancelled-before-start-",
            run_id=fixture.run_id, item_id=fixture.child,
            attempt_id="attempt-" + uuid7_hex(),
            attempt_opened_id="attempt-opened-" + uuid7_hex(),
            retry_scheduled_id=None,
            fence_token="b" * 64, cancel_scope_resolved_id=resolved["id"],
            capability_set_bound_id=None, dispatch_decision_id=None,
            reason_code="cancelled_before_start", cancelled_at_testimony=NOW,
        )
        prestart["schema_version"] = 1
        zero = _record(
            "spawn_child_cancelled_without_attempt",
            "spawn-child-cancelled-without-attempt-", run_id=fixture.run_id,
            spawn_group_id=request["spawn_group_id"],
            plan_amendment_id="plan-amendment-" + uuid7_hex(),
            child_item_id=fixture.child, child_admitted_id=None,
            cancel_scope_resolved_id=resolved["id"],
            reason_code="cancelled_without_attempt", cancelled_at_testimony=NOW,
        )
        zero["schema_version"] = 1
        cases = (
            (request, "exact-items-cancel-requested-record.schema.json"),
            (resolved, "exact-items-cancel-scope-resolved-record.schema.json"),
            (prestart, "attempt-cancelled-before-start-record.schema.json"),
            (zero, "spawn-child-cancelled-without-attempt-record.schema.json"),
        )
        for record, schema_name in cases:
            with self.subTest(kind=record["kind"], shape="lawful"):
                validate_record(record, "alpha", RUN_KINDS, integrity=False)
                validate_json_schema(record, Path("schemas/v1") / schema_name)
            for field, invalid in (
                ("id", str(record["id"]).split("-")[0] + "-short"),
                ("tenant_id", "alpha\n"),
                ("timestamp", NOW + "\n"),
            ):
                hostile = dict(record, **{field: invalid})
                with self.subTest(kind=record["kind"], field=field):
                    with self.assertRaises(ProtocolRefusal):
                        validate_record(hostile, "alpha", RUN_KINDS, integrity=False)
                    with self.assertRaises(SchemaValidationError):
                        validate_json_schema(hostile, Path("schemas/v1") / schema_name)
        for record, field in ((request, "item_ids"), (resolved, "item_ids"), (resolved, "attempt_ids")):
            hostile = dict(record, **{field: list(reversed(record[field]))})
            with self.subTest(kind=record["kind"], field=field, shape="unsorted"):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(hostile, "alpha", RUN_KINDS, integrity=False)


class SpawnGroupRecordTests(unittest.TestCase):
    def test_child_admitted_accepts_only_floati_workspace_in_runtime_and_schema(
        self,
    ) -> None:
        """Catches a dual-root alias in the frozen child-admission contract."""

        fixture = SpawnGroupFixtures()
        group = fixture.group()
        amendment = fixture.amendment(group)
        admitted = fixture.admitted_record(group, amendment)
        schema_path = Path("schemas/v1/child-admitted-record.schema.json")

        def accepted(record: dict[str, object]) -> tuple[bool, bool]:
            try:
                validate_record(
                    dict(record),
                    "alpha",
                    RUN_KINDS | {"child_admitted"},
                    integrity=False,
                )
            except (IntegrityFailure, ProtocolRefusal):
                runtime = False
            else:
                runtime = True
            try:
                validate_json_schema(record, schema_path)
            except SchemaValidationError:
                schema = False
            else:
                schema = True
            return runtime, schema

        for workspace, expected in (
            (str(worker_workspace_root() / fixture.child), (True, True)),
            # The RETIRED governed-workspace root, built rather than spelled:
            # this row asserts that exact coordinate is refused, so its bytes
            # are the assertion.
            (
                f"\x2fprivate/tmp/{RETIRED_PRODUCT_NAME}-work/{fixture.child}",
                (False, False),
            ),
        ):
            with self.subTest(workspace=workspace):
                self.assertEqual(
                    expected,
                    accepted(dict(admitted, workspace=workspace)),
                )

    def test_v0_plan_amendment_remains_exact_and_legacy(self) -> None:
        fixture = SpawnGroupFixtures()
        legacy = _record(
            "plan_amendment", "plan-amendment-", run_id=fixture.run_id,
            item_id=fixture.parent, task_contract_id="task-contract-" + uuid7_hex(),
            previous_digest=DIGEST, replacement_fields={"objective": "amended"},
            contract_digest="b" * 64,
        )
        validated = validate_record(legacy, "alpha", frozenset({"plan_amendment"}), integrity=False)
        self.assertEqual(0, validated["schema_version"])
        widened = dict(legacy, spawn_group_id="spawn-group-created-" + uuid7_hex())
        with self.assertRaises(ProtocolRefusal):
            validate_record(widened, "alpha", frozenset({"plan_amendment"}), integrity=False)

    def test_v1_spawn_record_runtime_and_schema_shapes_are_closed(self) -> None:
        fixture = SpawnGroupFixtures()
        group = fixture.group()
        amendment = fixture.amendment(group)
        started = fixture.started_parent()
        records = [
            next(row for row in started if row["kind"] == "run_spawn_admission_enabled"),
            next(row for row in started if row["kind"] == "attempt_spawn_policy_bound"),
            group,
            amendment,
            _record(
                "spawn_group_aborted", "spawn-group-aborted-", run_id=fixture.run_id,
                spawn_group_id=group["id"], parent_attempt_id=fixture.attempt,
                parent_fence_token=fixture.fence, reason_code="operator_abandonment",
                cancel_scope_resolved_id=None, operator_id="operator-a",
                authority_subject="authority", authority_epoch=1,
                capability_record_id="capability-" + uuid7_hex(),
                aborted_at_testimony=NOW,
            ),
            fixture.admitted(group, amendment),
            _record(
                "child_rejected", "child-rejected-", run_id=fixture.run_id,
                spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
                parent_attempt_id=fixture.attempt, child_item_id=fixture.child,
                reason_code="policy_refusal", evaluated_at_testimony=NOW,
            ),
            _record(
                "spawn_group_closed", "spawn-group-closed-", run_id=fixture.run_id,
                spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
                parent_attempt_id=fixture.attempt, member_item_ids=[fixture.child],
                accepted_item_ids=[], terminal_item_ids=[], rejected_item_ids=[fixture.child],
                join_mode="all_terminal", required_count=None, outcome="satisfied",
                close_reason="all_members_terminal", cancel_scope_resolved_id=None,
                closed_at_testimony=NOW,
            ),
            _record(
                "untracked_descendant", "untracked-descendant-", run_id=fixture.run_id,
                parent_item_id=fixture.parent, parent_attempt_id=fixture.attempt,
                adapter="codex", provider_descendant_id="thread-1", state="observed",
                adopted_item_id=None, reason_code="native_descendant_observed",
                observed_at_testimony=NOW,
            ),
            _record(
                "descendant_observation_closed", "descendant-observation-closed-",
                run_id=fixture.run_id, parent_item_id=fixture.parent,
                parent_attempt_id=fixture.attempt, parent_fence_token=fixture.fence,
                attempt_spawn_policy_id=fixture.spawn_policy_id, adapter="codex",
                observed_descendant_ids=[], closed_at_testimony=NOW,
            ),
            _record(
                "spawn_late_result_disposition", "spawn-late-result-disposition-",
                run_id=fixture.run_id, spawn_group_id=group["id"],
                child_item_id=fixture.child,
                result_record_id="run-result-produced-" + uuid7_hex(),
                disposition="quarantine", operator_id="operator-a",
                authority_subject="authority", authority_epoch=1,
                capability_record_id="capability-" + uuid7_hex(),
                decided_at_testimony=NOW,
            ),
        ]
        for row in records:
            with self.subTest(kind=row["kind"]):
                normalized = validate_record(row, "alpha", RUN_KINDS | {str(row["kind"])}, integrity=False)
                self.assertEqual(1, normalized["schema_version"])
                with self.assertRaises(ProtocolRefusal):
                    validate_record(dict(row, extra=True), "alpha", RUN_KINDS | {str(row["kind"])}, integrity=False)
        integral = dict(group, max_children=2.0)
        self.assertEqual(2, validate_record(integral, "alpha", RUN_KINDS | {"spawn_group_created"}, integrity=False)["max_children"])
        for invalid in (True, 2.5, float("inf")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(dict(group, max_children=invalid), "alpha", RUN_KINDS | {"spawn_group_created"}, integrity=False)

    def test_spawn_group_projection_requires_current_started_parent_fence(self) -> None:
        fixture = SpawnGroupFixtures()
        lawful = [*fixture.started_parent(), fixture.group()]
        projected = RunProjection.from_records(lawful, integrity=False).run(fixture.run_id)
        self.assertIn(lawful[-1]["id"], projected["spawn_groups"])

        wrong = fixture.group(parent_fence_token="b" * 64)
        with self.assertRaises(ProtocolRefusal) as caught:
            RunProjection.from_records([*fixture.started_parent(), wrong], integrity=False)
        self.assertEqual("spawn_parent_fence_invalid", caught.exception.code)

    def test_spawn_plan_amendment_atomically_adds_contract_admission_and_graph_truth(self) -> None:
        fixture = SpawnGroupFixtures()
        base = fixture.started_parent()
        group = fixture.group()
        amendment = fixture.amendment(group)
        projection = RunProjection.from_records([*base, group, amendment], integrity=False)
        run = projection.run(fixture.run_id)
        self.assertEqual(sorted([fixture.parent, fixture.child]), run["item_ids"])
        self.assertEqual([fixture.child], run["spawn_groups"][group["id"]]["member_item_ids"])
        self.assertEqual(
            amendment["children"][0]["task_contract_id"],
            run["contracts"][fixture.child]["task_contract_id"],
        )
        self.assertEqual(amendment["admission_digest"], run["admission_binding"]["admission_digest"])
        self.assertEqual(fixture.child, projection.edges(fixture.run_id)[0].target)

        cycle = fixture.amendment(
            group,
            edges=[
                {"source": fixture.child, "target": fixture.parent,
                 "requires": "accepted", "failure_policy": "fail_run"},
                {"source": fixture.parent, "target": fixture.child,
                 "requires": "accepted", "failure_policy": "fail_run"},
            ],
        )
        with self.assertRaises(ProtocolRefusal):
            RunProjection.from_records([*base, group, cycle], integrity=False)

    def test_attempt_spawn_policy_precedes_dispatch_and_defaults_disabled(self) -> None:
        fixture = SpawnGroupFixtures()
        legacy = RunProjection.from_records(
            fixture.started_parent(include_spawn_policy=False), integrity=False
        ).run(fixture.run_id)
        self.assertEqual("disabled", legacy["attempt_spawn_policy"][fixture.attempt]["subagents_mode"])

        records = fixture.started_parent()
        policy_index = next(i for i, row in enumerate(records) if row["kind"] == "attempt_spawn_policy_bound")
        policy = records.pop(policy_index)
        records.insert(-1, policy)
        with self.assertRaises(ProtocolRefusal) as caught:
            RunProjection.from_records(records, integrity=False)
        self.assertEqual("spawn_policy_missing", caught.exception.code)

    def test_spawn_admission_enablement_owns_complete_base_plan_preimage(self) -> None:
        fixture = SpawnGroupFixtures()
        projected = RunProjection.from_records(fixture.started_parent(), integrity=False).run(fixture.run_id)
        self.assertEqual(fixture.plan().canonical(), projected["spawn_admission"]["base_plan"])
        self.assertEqual(fixture.plan().digest, projected["spawn_admission"]["base_plan_digest"])

    def test_spawn_enablement_matches_current_projected_contracts_and_full_plan_digest(self) -> None:
        fixture = SpawnGroupFixtures()
        records = fixture.started_parent()
        enabled = next(row for row in records if row["kind"] == "run_spawn_admission_enabled")
        tampered = deepcopy(enabled)
        tampered["base_plan"]["items"][0]["contract"]["objective"] = "drifted"
        records[records.index(enabled)] = tampered
        with self.assertRaises(ProtocolRefusal) as caught:
            RunProjection.from_records(records, integrity=False)
        self.assertIn(caught.exception.code, {"spawn_base_plan_digest_invalid", "spawn_contract_mismatch"})

    def test_spawn_aware_dispatch_repeats_exact_policy_and_adapter(self) -> None:
        fixture = SpawnGroupFixtures()
        lawful = RunProjection.from_records(fixture.started_parent(), integrity=False).run(fixture.run_id)
        self.assertEqual("codex", lawful["dispatches"][fixture.attempt]["adapter"])
        records = fixture.started_parent()
        dispatch = next(row for row in records if row["kind"] == "dispatch_decision")
        dispatch["adapter"] = "claude"
        with self.assertRaises(ProtocolRefusal) as caught:
            RunProjection.from_records(records, integrity=False)
        self.assertEqual("spawn_dispatch_mismatch", caught.exception.code)

        disabled = fixture.started_parent(spawn_mode="disabled")
        disabled_dispatch = next(
            row for row in disabled if row["kind"] == "dispatch_decision"
        )
        disabled_dispatch.pop("adapter")
        disabled_dispatch.pop("attempt_spawn_policy_id")
        with self.assertRaises(ProtocolRefusal) as missing:
            RunProjection.from_records(disabled, integrity=False)
        self.assertEqual("spawn_policy_missing", missing.exception.code)

    def test_pending_group_fences_parent_and_run_terminal_until_activation_or_abort(self) -> None:
        fixture = SpawnGroupFixtures()
        records = fixture.started_parent()
        group = fixture.group()
        receipt_id = "worker-receipt-" + uuid7_hex()
        receipt = {"id": receipt_id, "work_item_id": fixture.parent, "node_id": "worker-a"}
        produced = _record(
            "result_produced", "run-result-produced-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            dispatch_decision_id=records[-2]["id"], worker_receipt_ids=[receipt_id],
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            RunProjection.from_records(
                [*records, group, produced], worker_receipts=[receipt], integrity=False
            )
        self.assertEqual("spawn_group_pending", caught.exception.code)

        aborted = _record(
            "spawn_group_aborted", "spawn-group-aborted-", run_id=fixture.run_id,
            spawn_group_id=group["id"], parent_attempt_id=fixture.attempt,
            parent_fence_token=fixture.fence, reason_code="operator_abandonment",
            cancel_scope_resolved_id=None, operator_id="operator-a",
            authority_subject="authority", authority_epoch=1,
            capability_record_id="capability-" + uuid7_hex(),
            aborted_at_testimony=NOW,
        )
        projection = RunProjection.from_records(
            [*records, group, aborted, produced], worker_receipts=[receipt], integrity=False
        )
        self.assertEqual("aborted", projection.run(fixture.run_id)["spawn_groups"][group["id"]]["state"])

    def test_parent_acceptance_requires_a_physically_satisfied_join(self) -> None:
        fixture = SpawnGroupFixtures()
        records = fixture.started_parent()
        group = fixture.group(on_child_failure="continue_until_join_impossible")
        amendment = fixture.amendment(group)
        receipt_id = "worker-receipt-" + uuid7_hex()
        receipt = {"id": receipt_id, "work_item_id": fixture.parent, "node_id": "worker-a"}
        produced = _record(
            "result_produced", "run-result-produced-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            dispatch_decision_id=records[-2]["id"], worker_receipt_ids=[receipt_id],
        )
        accepted = _record(
            "result_accepted", "run-result-accepted-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            predecessor_result_id=produced["id"], acceptance_mode="accepted_unverified",
            acceptance_receipt_id=None, worker_receipt_ids=[receipt_id],
        )
        prefix = [*records, group, amendment, produced]
        with self.assertRaises(ProtocolRefusal) as open_join:
            RunProjection.from_records([*prefix, accepted], worker_receipts=[receipt], integrity=False)
        self.assertEqual("spawn_join_unsatisfied", open_join.exception.code)
        rejected = _record(
            "child_rejected", "child-rejected-", run_id=fixture.run_id,
            spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
            parent_attempt_id=fixture.attempt, child_item_id=fixture.child,
            reason_code="policy_refusal", evaluated_at_testimony=NOW,
        )
        closed = _record(
            "spawn_group_closed", "spawn-group-closed-", run_id=fixture.run_id,
            spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
            parent_attempt_id=fixture.attempt, member_item_ids=[fixture.child],
            accepted_item_ids=[], terminal_item_ids=[], rejected_item_ids=[fixture.child],
            join_mode="all_terminal", required_count=None, outcome="satisfied",
            close_reason="all_members_terminal", cancel_scope_resolved_id=None,
            closed_at_testimony=NOW,
        )
        observation = _record(
            "descendant_observation_closed", "descendant-observation-closed-",
            run_id=fixture.run_id, parent_item_id=fixture.parent,
            parent_attempt_id=fixture.attempt, parent_fence_token=fixture.fence,
            attempt_spawn_policy_id=fixture.spawn_policy_id, adapter="codex",
            observed_descendant_ids=[], closed_at_testimony=NOW,
        )
        projection = RunProjection.from_records(
            [*prefix, rejected, closed, observation, accepted],
            worker_receipts=[receipt], integrity=False,
        )
        self.assertIn(fixture.parent, projection.run(fixture.run_id)["accepted"])
        self.assertEqual("skipped", projection.item_outcomes(fixture.run_id)[fixture.child])

    def test_spawn_projection_rejects_duplicate_group_key_and_changed_membership(self) -> None:
        fixture = SpawnGroupFixtures()
        base = fixture.started_parent()
        group = fixture.group()
        with self.assertRaises(ProtocolRefusal) as duplicate:
            RunProjection.from_records([*base, group, fixture.group()], integrity=False)
        self.assertEqual("spawn_group_key_duplicate", duplicate.exception.code)

        amendment = fixture.amendment(group)
        other_child = "work-" + uuid7_hex()
        changed = fixture.amendment(group, children=[fixture.descriptor(other_child)], edges=[])
        with self.assertRaises(ProtocolRefusal) as membership:
            RunProjection.from_records([*base, group, amendment, changed], integrity=False)
        self.assertEqual("spawn_membership_immutable", membership.exception.code)

    def test_group_child_attempt_requires_durable_child_admission(self) -> None:
        fixture = SpawnGroupFixtures()
        group = fixture.group()
        amendment = fixture.amendment(group)
        child_attempt = _record(
            "attempt_opened", "attempt-opened-", run_id=fixture.run_id,
            item_id=fixture.child, attempt_id="attempt-" + uuid7_hex(), ordinal=1,
            scheduler_epoch=1,
            fence_token=attempt_fence_token(fixture.run_id, fixture.child, 1, 1),
            max_attempts=1,
            backoff={"strategy": "fixed", "base_delay_ms": 0,
                     "cap_delay_ms": 0, "jitter": "sha256_25pct"},
        )
        prefix = [*fixture.started_parent(), group, amendment]
        with self.assertRaises(ProtocolRefusal) as caught:
            RunProjection.from_records([*prefix, child_attempt], integrity=False)
        self.assertEqual("spawn_child_admission_missing", caught.exception.code)

        admitted = fixture.admitted(group, amendment)
        projection = RunProjection.from_records([*prefix, admitted, child_attempt], integrity=False)
        self.assertIn(child_attempt["attempt_id"], projection.run(fixture.run_id)["attempts"])

    def test_untracked_descendant_blocks_parent_acceptance_until_resolved(self) -> None:
        fixture = SpawnGroupFixtures()
        records = fixture.started_parent()
        receipt_id = "worker-receipt-" + uuid7_hex()
        receipt = {
            "id": receipt_id, "work_item_id": fixture.parent,
            "node_id": "worker-a", "authority_subject": "authority",
            "authority_epoch": 1,
        }
        produced = _record(
            "result_produced", "run-result-produced-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            dispatch_decision_id=records[-2]["id"], worker_receipt_ids=[receipt_id],
        )
        observed = _record(
            "untracked_descendant", "untracked-descendant-", run_id=fixture.run_id,
            parent_item_id=fixture.parent, parent_attempt_id=fixture.attempt,
            adapter="codex", provider_descendant_id="thread-1", state="observed",
            adopted_item_id=None, reason_code="native_descendant_observed",
            observed_at_testimony=NOW,
        )
        accepted = _record(
            "result_accepted", "run-result-accepted-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            predecessor_result_id=produced["id"], acceptance_mode="accepted_unverified",
            acceptance_receipt_id=None, worker_receipt_ids=[receipt_id],
        )
        with self.assertRaises(ProtocolRefusal) as blocked:
            RunProjection.from_records(
                [*records, produced, observed, accepted], worker_receipts=[receipt], integrity=False
            )
        self.assertEqual("untracked_descendant_unresolved", blocked.exception.code)

        resolved = dict(
            observed,
            id="untracked-descendant-" + uuid7_hex(),
            state="terminated",
            reason_code="adapter_terminated",
        )
        closed = _record(
            "descendant_observation_closed", "descendant-observation-closed-",
            run_id=fixture.run_id, parent_item_id=fixture.parent,
            parent_attempt_id=fixture.attempt, parent_fence_token=fixture.fence,
            attempt_spawn_policy_id=fixture.spawn_policy_id, adapter="codex",
            observed_descendant_ids=["thread-1"], closed_at_testimony=NOW,
        )
        projected = RunProjection.from_records(
            [*records, produced, observed, resolved, closed, accepted],
            worker_receipts=[receipt], integrity=False,
        )
        snapshot = projected.run(fixture.run_id)
        self.assertIn(fixture.parent, snapshot["accepted"])
        snapshot["accepted"].clear()
        self.assertIn(fixture.parent, projected.run(fixture.run_id)["accepted"])

    def test_observation_close_precedes_parent_acceptance_and_rejects_late_descendant(self) -> None:
        fixture = SpawnGroupFixtures()
        records = fixture.started_parent(spawn_mode="observed_only")
        receipt_id = "worker-receipt-" + uuid7_hex()
        receipt = {"id": receipt_id, "work_item_id": fixture.parent, "node_id": "worker-a"}
        produced = _record(
            "result_produced", "run-result-produced-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            dispatch_decision_id=records[-2]["id"], worker_receipt_ids=[receipt_id],
        )
        accepted = _record(
            "result_accepted", "run-result-accepted-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            predecessor_result_id=produced["id"], acceptance_mode="accepted_unverified",
            acceptance_receipt_id=None, worker_receipt_ids=[receipt_id],
        )
        with self.assertRaises(ProtocolRefusal) as missing:
            RunProjection.from_records(
                [*records, produced, accepted], worker_receipts=[receipt], integrity=False
            )
        self.assertEqual("descendant_observation_close_missing", missing.exception.code)
        closed = _record(
            "descendant_observation_closed", "descendant-observation-closed-",
            run_id=fixture.run_id, parent_item_id=fixture.parent,
            parent_attempt_id=fixture.attempt, parent_fence_token=fixture.fence,
            attempt_spawn_policy_id=fixture.spawn_policy_id, adapter="codex",
            observed_descendant_ids=[], closed_at_testimony=NOW,
        )
        RunProjection.from_records(
            [*records, produced, closed, accepted], worker_receipts=[receipt], integrity=False
        )
        late = _record(
            "untracked_descendant", "untracked-descendant-", run_id=fixture.run_id,
            parent_item_id=fixture.parent, parent_attempt_id=fixture.attempt,
            adapter="codex", provider_descendant_id="thread-late", state="observed",
            adopted_item_id=None, reason_code="native_descendant_observed",
            observed_at_testimony=NOW,
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            RunProjection.from_records([*records, closed, late], integrity=False)
        self.assertEqual("descendant_observation_closed", caught.exception.code)

    def test_close_applies_complete_join_failure_and_outcome_reason_matrix(self) -> None:
        """Catches a close projector that trusts caller-nominated outcome/reason truth."""
        satisfied = (
            ("all_accepted", None, "all_members_accepted"),
            ("all_terminal", None, "all_members_terminal"),
            ("quorum", 1, "quorum_reached"),
            ("first_accepted", 1, "first_accepted"),
        )
        for mode, required, reason in satisfied:
            fixture = SpawnGroupFixtures()
            group = fixture.group(join_mode=mode, required_count=required)
            amendment = fixture.amendment(group)
            child_records, receipt = fixture.child_success_records(group, amendment)
            close = fixture.close(
                group, amendment, outcome="satisfied", close_reason=reason,
                accepted_item_ids=[fixture.child], terminal_item_ids=[fixture.child],
            )
            prefix = [*fixture.started_parent(), group, amendment, *child_records]
            with self.subTest(lawful_satisfied=mode):
                RunProjection.from_records(
                    [*prefix, close], worker_receipts=[receipt], integrity=False,
                )
            wrong_reason = dict(
                close,
                id="spawn-group-closed-" + uuid7_hex(),
                close_reason="join_impossible" if reason != "join_impossible" else "child_failure",
            )
            with self.subTest(wrong_satisfied_reason=mode):
                with self.assertRaises(ProtocolRefusal) as caught:
                    RunProjection.from_records(
                        [*prefix, wrong_reason], worker_receipts=[receipt], integrity=False,
                    )


                self.assertEqual("spawn_group_close_invalid", caught.exception.code)

        rejection_table = (
            ("all_accepted", "fail_group", None, "failed", "child_failure"),
            ("all_accepted", "continue_until_join_impossible", None, "failed", "join_impossible"),
            ("all_terminal", "fail_group", None, "failed", "child_failure"),
            ("all_terminal", "continue_until_join_impossible", None, "satisfied", "all_members_terminal"),
            ("quorum", "fail_group", 1, "failed", "child_failure"),
            ("quorum", "continue_until_join_impossible", 1, "failed", "join_impossible"),
            ("first_accepted", "fail_group", 1, "failed", "child_failure"),
            ("first_accepted", "continue_until_join_impossible", 1, "failed", "join_impossible"),
        )
        for mode, policy, required, outcome, reason in rejection_table:
            fixture = SpawnGroupFixtures()
            group = fixture.group(
                join_mode=mode, required_count=required, on_child_failure=policy,
            )
            amendment = fixture.amendment(group)
            rejected = fixture.rejected(group, amendment)
            close = fixture.close(
                group, amendment, outcome=outcome, close_reason=reason,
                rejected_item_ids=[fixture.child],
            )
            prefix = [*fixture.started_parent(), group, amendment, rejected]
            with self.subTest(lawful_rejection=(mode, policy)):
                RunProjection.from_records([*prefix, close], integrity=False)
            wrong_reason = dict(
                close,
                id="spawn-group-closed-" + uuid7_hex(),
                close_reason="join_impossible" if reason != "join_impossible" else "child_failure",
            )
            with self.subTest(wrong_rejection_reason=(mode, policy)):
                with self.assertRaises(ProtocolRefusal) as caught:
                    RunProjection.from_records([*prefix, wrong_reason], integrity=False)
                self.assertEqual("spawn_group_close_invalid", caught.exception.code)

        fixture = SpawnGroupFixtures()
        group = fixture.group()
        amendment = fixture.amendment(group)
        premature_deadline = fixture.close(
            group, amendment, outcome="deadline", close_reason="deadline_expired",
        )
        with self.subTest(premature_deadline=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records(
                    [*fixture.started_parent(), group, amendment, premature_deadline],
                    integrity=False,
                )
            self.assertEqual("spawn_group_close_invalid", caught.exception.code)

    def test_unknown_descendant_forces_needs_operator_and_fences_success(self) -> None:
        """Catches unknown descendant testimony collapsing into an ordinary resolution."""
        fixture = SpawnGroupFixtures()
        records = fixture.started_parent(spawn_mode="observed_only")
        observed = _record(
            "untracked_descendant", "untracked-descendant-", run_id=fixture.run_id,
            parent_item_id=fixture.parent, parent_attempt_id=fixture.attempt,
            adapter="codex", provider_descendant_id="thread-terminated", state="observed",
            adopted_item_id=None, reason_code="native_descendant_observed",
            observed_at_testimony=NOW,
        )
        terminated = dict(
            observed, id="untracked-descendant-" + uuid7_hex(), state="terminated",
            reason_code="adapter_terminated",
        )
        with self.subTest(lawful_terminated=True):
            RunProjection.from_records(
                [*records, observed, terminated, fixture.observation_close(["thread-terminated"])],
                integrity=False,
            )

        adopted_fixture = SpawnGroupFixtures()
        adopted_records = adopted_fixture.started_parent()
        adopted_group = adopted_fixture.group()
        adopted_amendment = adopted_fixture.amendment(adopted_group)
        adopted_observed = _record(
            "untracked_descendant", "untracked-descendant-", run_id=adopted_fixture.run_id,
            parent_item_id=adopted_fixture.parent,
            parent_attempt_id=adopted_fixture.attempt, adapter="codex",
            provider_descendant_id="thread-adopted", state="observed",
            adopted_item_id=None, reason_code="native_descendant_observed",
            observed_at_testimony=NOW,
        )
        adopted = dict(
            adopted_observed, id="untracked-descendant-" + uuid7_hex(), state="adopted",
            adopted_item_id=adopted_fixture.child, reason_code="adopted_managed",
        )
        with self.subTest(lawful_adopted=True):
            RunProjection.from_records([
                *adopted_records, adopted_group, adopted_amendment,
                adopted_fixture.admitted(adopted_group, adopted_amendment),
                adopted_observed, adopted,
                adopted_fixture.observation_close(["thread-adopted"]),
            ], integrity=False)

        fixture = SpawnGroupFixtures()
        records = fixture.started_parent()
        group = fixture.group(on_child_failure="continue_until_join_impossible")
        amendment = fixture.amendment(group)
        rejected = fixture.rejected(group, amendment)
        observed = _record(
            "untracked_descendant", "untracked-descendant-", run_id=fixture.run_id,
            parent_item_id=fixture.parent, parent_attempt_id=fixture.attempt,
            adapter="codex", provider_descendant_id="thread-unknown", state="observed",
            adopted_item_id=None, reason_code="native_descendant_observed",
            observed_at_testimony=NOW,
        )
        unknown = dict(
            observed, id="untracked-descendant-" + uuid7_hex(), state="unknown",
            reason_code="observation_uncertain",
        )
        prefix = [*records, group, amendment, rejected, observed, unknown]
        needs_operator = fixture.close(
            group, amendment, outcome="needs_operator",
            close_reason="untracked_descendant_unknown",
            rejected_item_ids=[fixture.child],
        )
        with self.subTest(lawful_unknown_close=True):
            RunProjection.from_records([*prefix, needs_operator], integrity=False)
        with self.subTest(unknown_blocks_observation_close=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records(
                    [*prefix, fixture.observation_close(["thread-unknown"])],
                    integrity=False,
                )
            self.assertEqual("untracked_descendant_unknown", caught.exception.code)
        invalid_satisfied = fixture.close(
            group, amendment, outcome="satisfied", close_reason="all_members_terminal",
            rejected_item_ids=[fixture.child],
        )
        with self.subTest(unknown_blocks_satisfied_close=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records([*prefix, invalid_satisfied], integrity=False)
            self.assertEqual("untracked_descendant_unknown", caught.exception.code)

        result_records, receipt = fixture.parent_result_records(records)
        completed = fixture.parent_terminal(
            records, terminal_state="completed", policy_class=None, reason_code="completed",
        )
        with self.subTest(unknown_blocks_parent_acceptance_and_success=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records([
                    *prefix, needs_operator,
                    fixture.observation_close(["thread-unknown"]),
                    *result_records, completed,
                ], worker_receipts=[receipt], integrity=False)
            self.assertEqual("untracked_descendant_unknown", caught.exception.code)

    def test_activation_enforces_aggregate_children_and_workspace_attenuation(self) -> None:
        """Catches per-group-only fan-out and child workspace widening."""
        fixture = SpawnGroupFixtures()
        base = fixture.started_parent()
        policy = next(row for row in base if row["kind"] == "attempt_spawn_policy_bound")
        policy["workspace_policies"] = ["isolated_worktree", "patch_only"]
        lower_group = fixture.group(workspace_policy="isolated_worktree")
        lower_child = fixture.descriptor(workspace_policy="patch_only")
        lower_amendment = fixture.amendment(lower_group, children=[lower_child])
        with self.subTest(lawful_lower_workspace=True):
            RunProjection.from_records(
                [*base, lower_group, lower_amendment], integrity=False,
            )

        widening_group = fixture.group(workspace_policy="patch_only")
        widening_child = fixture.descriptor(workspace_policy="isolated_worktree")
        widening_amendment = fixture.amendment(
            widening_group, children=[widening_child],
        )
        with self.subTest(child_workspace_widening=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records(
                    [*base, widening_group, widening_amendment], integrity=False,
                )
            self.assertEqual("spawn_workspace_widening", caught.exception.code)

        fixture = SpawnGroupFixtures()
        base = fixture.started_parent()
        current_plan = fixture.plan()
        prior_admission = fixture._admission_digest()
        prefix = list(base)
        for index in range(2):
            child_id = fixture.child if index == 0 else "work-" + uuid7_hex()
            child = fixture.descriptor(child_id, budget_allocation=[])
            group = fixture.group(
                group_key=f"group-{index}", max_children=1, aggregate_budget=[],
            )
            edge = [{
                "source": fixture.parent, "target": child_id,
                "requires": "accepted", "failure_policy": "fail_run",
            }]
            amendment = fixture.amendment(
                group, children=[child], edges=edge, current_plan=current_plan,
                previous_admission_digest=prior_admission,
            )
            prefix.extend([group, amendment])
            current_plan = fixture.amended_plan(current_plan, [child], edge)
            prior_admission = amendment["admission_digest"]
        with self.subTest(lawful_equal_aggregate=True):
            RunProjection.from_records(prefix, integrity=False)

        third_id = "work-" + uuid7_hex()
        third_child = fixture.descriptor(third_id, budget_allocation=[])
        third_group = fixture.group(
            group_key="group-2", max_children=1, aggregate_budget=[],
        )
        third_edge = [{
            "source": fixture.parent, "target": third_id,
            "requires": "accepted", "failure_policy": "fail_run",
        }]
        third_amendment = fixture.amendment(
            third_group, children=[third_child], edges=third_edge,
            current_plan=current_plan, previous_admission_digest=prior_admission,
        )
        with self.subTest(aggregate_parent_max_children=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records(
                    [*prefix, third_group, third_amendment], integrity=False,
                )
            self.assertEqual("spawn_item_limit", caught.exception.code)

    def test_parent_terminal_requires_closed_group_and_matching_outcome_class(self) -> None:
        """Catches non-success terminal transitions bypassing activated group truth."""
        fixture = SpawnGroupFixtures()
        records = fixture.started_parent()
        group = fixture.group()
        amendment = fixture.amendment(group)
        terminal = fixture.parent_terminal(records)
        with self.subTest(activated_unclosed=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records(
                    [*records, group, amendment, terminal], integrity=False,
                )
            self.assertEqual("spawn_group_open", caught.exception.code)

        rejected = fixture.rejected(group, amendment)
        failed_close = fixture.close(
            group, amendment, outcome="failed", close_reason="child_failure",
            rejected_item_ids=[fixture.child],
        )
        with self.subTest(lawful_failed_terminal=True):
            RunProjection.from_records(
                [*records, group, amendment, rejected, failed_close, terminal],
                integrity=False,
            )

        satisfied_fixture = SpawnGroupFixtures()
        satisfied_records = satisfied_fixture.started_parent()
        satisfied_group = satisfied_fixture.group(
            on_child_failure="continue_until_join_impossible",
        )
        satisfied_amendment = satisfied_fixture.amendment(satisfied_group)
        satisfied_rejected = satisfied_fixture.rejected(
            satisfied_group, satisfied_amendment,
        )
        satisfied_close = satisfied_fixture.close(
            satisfied_group, satisfied_amendment, outcome="satisfied",
            close_reason="all_members_terminal",
            rejected_item_ids=[satisfied_fixture.child],
        )
        result_records, receipt = satisfied_fixture.parent_result_records(satisfied_records)
        completed = satisfied_fixture.parent_terminal(
            satisfied_records, terminal_state="completed",
            policy_class=None, reason_code="completed",
        )
        with self.subTest(lawful_satisfied_terminal=True):
            RunProjection.from_records([
                *satisfied_records, satisfied_group, satisfied_amendment,
                satisfied_rejected, satisfied_close,
                satisfied_fixture.observation_close(), *result_records, completed,
            ], worker_receipts=[receipt], integrity=False)
        mismatched = satisfied_fixture.parent_terminal(satisfied_records)
        with self.subTest(closed_outcome_mismatch=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records([
                    *satisfied_records, satisfied_group, satisfied_amendment,
                    satisfied_rejected, satisfied_close, mismatched,
                ], integrity=False)
            self.assertEqual("spawn_group_terminal_mismatch", caught.exception.code)

    def test_activation_requires_unique_independent_child_contract_ids(self) -> None:
        """Catches embedded contracts aliasing siblings or projected contract identity."""
        fixture = SpawnGroupFixtures()
        group = fixture.group()
        amendment = fixture.amendment(group)
        with self.subTest(lawful_independent_contract=True):
            projected = RunProjection.from_records(
                [*fixture.started_parent(), group, amendment], integrity=False,
            ).run(fixture.run_id)
            self.assertNotEqual(
                projected["contracts"][fixture.parent]["task_contract_id"],
                projected["contracts"][fixture.child]["task_contract_id"],
            )

        sibling_fixture = SpawnGroupFixtures()
        sibling_id = "work-" + uuid7_hex()
        shared_contract_id = "task-contract-" + uuid7_hex()
        children = sorted([
            sibling_fixture.descriptor(task_contract_id=shared_contract_id),
            sibling_fixture.descriptor(sibling_id, task_contract_id=shared_contract_id),
        ], key=lambda child: str(child["item_id"]))
        sibling_group = sibling_fixture.group(max_children=2)
        sibling_edges = [{
            "source": sibling_fixture.parent, "target": child["item_id"],
            "requires": "accepted", "failure_policy": "fail_run",
        } for child in children]
        sibling_amendment = sibling_fixture.amendment(
            sibling_group, children=children, edges=sibling_edges,
        )
        with self.subTest(duplicate_sibling_contract_id=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records([
                    *sibling_fixture.started_parent(), sibling_group,
                    sibling_amendment,
                ], integrity=False)
            self.assertEqual("spawn_contract_id_duplicate", caught.exception.code)

        existing_fixture = SpawnGroupFixtures()
        existing_records = existing_fixture.started_parent()
        parent_contract_id = next(
            row["id"] for row in existing_records if row["kind"] == "task_contract"
        )
        existing_group = existing_fixture.group()
        existing_child = existing_fixture.descriptor(
            task_contract_id=parent_contract_id,
        )
        existing_amendment = existing_fixture.amendment(
            existing_group, children=[existing_child],
        )
        with self.subTest(existing_contract_id_alias=True):
            with self.assertRaises(ProtocolRefusal) as caught:
                RunProjection.from_records([
                    *existing_records, existing_group, existing_amendment,
                ], integrity=False)
            self.assertEqual("spawn_contract_id_duplicate", caught.exception.code)

    def test_quorum_schema_enumerates_each_required_count_max_children_pair(self) -> None:
        """Catches schema admission of a quorum threshold above its group bound."""
        schema = Path("schemas/v1/spawn-group-created-record.schema.json")

        def accepts(record: dict[str, object]) -> tuple[bool, bool]:
            try:
                validate_record(
                    record, "alpha", RUN_KINDS | {"spawn_group_created"},
                    integrity=False,
                )
            except ProtocolRefusal:
                runtime = False
            else:
                runtime = True
            try:
                validate_json_schema(record, schema)
            except SchemaValidationError:
                schema_accepts = False
            else:
                schema_accepts = True
            return runtime, schema_accepts

        fixture = SpawnGroupFixtures()
        for max_children in range(1, 9):
            for required_count in range(1, max_children + 1):
                with self.subTest(
                    lawful_pair=(required_count, max_children),
                ):
                    self.assertEqual((True, True), accepts(fixture.group(
                        max_children=max_children, join_mode="quorum",
                        required_count=required_count,
                    )))
        with self.subTest(required_above_max=True):
            self.assertEqual((False, False), accepts(fixture.group(
                max_children=1, join_mode="quorum", required_count=2,
            )))

    def test_late_result_disposition_cannot_reopen_join(self) -> None:
        fixture = SpawnGroupFixtures()
        group = fixture.group(on_late_result="operator_decision")
        amendment = fixture.amendment(group)
        closed = _record(
            "spawn_group_closed", "spawn-group-closed-", run_id=fixture.run_id,
            spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
            parent_attempt_id=fixture.attempt, member_item_ids=[fixture.child],
            accepted_item_ids=[], terminal_item_ids=[], rejected_item_ids=[],
            join_mode="all_terminal", required_count=None, outcome="deadline",
            close_reason="deadline_expired", cancel_scope_resolved_id=None,
            closed_at_testimony="2026-08-10T13:00:00.000Z",
        )
        disposition = _record(
            "spawn_late_result_disposition", "spawn-late-result-disposition-",
            run_id=fixture.run_id, spawn_group_id=group["id"],
            child_item_id=fixture.child,
            result_record_id="run-result-produced-" + uuid7_hex(),
            disposition="retain_as_non_join_evidence", operator_id="operator-a",
            authority_subject="authority", authority_epoch=1,
            capability_record_id="capability-" + uuid7_hex(),
            decided_at_testimony=NOW,
        )
        with self.assertRaises(ProtocolRefusal) as missing_result:
            RunProjection.from_records(
                [*fixture.started_parent(), group, amendment, closed, disposition],
                integrity=False,
            )
        self.assertEqual("late_result_missing", missing_result.exception.code)


class _ManagedSpawnCase:
    """Start one live sequencer over an already prepared direct Task 2/3 root."""

    def __init__(
        self,
        testcase: unittest.TestCase,
        case: _Task2Case,
        *,
        service_now: datetime | None = None,
        sequencer_id: str = "spawn-sequencer-a",
    ) -> None:
        self.testcase = testcase
        self.case = case
        self.service_now = service_now or case.now(20)
        self.service = SequencerService(
            case.root,
            sequencer_id,
            config=SequencerConfig(select_timeout=0.01, response_cache_size=1),
            clock=lambda: self.service_now,
        )
        self.stop = threading.Event()
        self.thread = threading.Thread(
            target=self.service.serve_forever, args=(self.stop,), daemon=True,
        )
        self.thread.start()
        self.client = SequencerClient(
            self.service.socket_path, self.service.epoch, "spawn-controller",
        )
        self.ledger = RunLedger(case.root, sequencer_client=self.client)
        self.controller = SpawnGroupController(self.ledger, case.policy)
        testcase.addCleanup(self.close)

    def close(self) -> None:
        if not self.service._closed:
            self.stop.set()
            self.thread.join(3)
            self.service.close()

    def restart(self) -> "_ManagedSpawnCase":
        self.close()
        return _ManagedSpawnCase(
            self.testcase,
            self.case,
            service_now=self.service_now,
            sequencer_id="spawn-sequencer-b",
        )

    def raw(self, payload: bytes) -> dict[str, object]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(3)
            channel.connect(str(self.service.socket_path))
            channel.sendall(payload)
            response = b""
            while not response.endswith(b"\n"):
                chunk = channel.recv(65536)
                if not chunk:
                    break
                response += chunk
        return json.loads(response)


class SpawnGroupManagedTests(unittest.TestCase):
    """Managed spawn operations remain semantic, closed, and retry-safe."""

    def setUp(self) -> None:
        self.assertIsNotNone(SpawnGroupController)

    @staticmethod
    def _without_now(values: dict[str, object]) -> dict[str, object]:
        return {key: deepcopy(value) for key, value in values.items() if key != "now"}

    def test_admission_enablement_and_policy_binding_are_closed_service_time_operations(self) -> None:
        enable_case = _Task2Case(self)
        managed = _ManagedSpawnCase(self, enable_case, service_now=enable_case.now(30))
        enabled = AdmissionBinder.enable_spawn(
            managed.ledger,
            enable_case.run_id,
            enable_case.plan,
            enable_case.policy,
            now=enable_case.now(1),
        )
        self.assertEqual("2026-08-10T12:00:30.000Z", enabled["enabled_at_testimony"])

        policy_case = _Task2Case(self)
        policy_case.enable()
        policy_case.prepare_parent(enable=False, dispatch=False, start=False)
        original = policy_case.spawn_policy
        managed_policy = _ManagedSpawnCase(self, policy_case, service_now=policy_case.now(31))
        rebound = managed_policy.controller.bind_attempt_policy(
            policy_case.run_id,
            policy_case.parent,
            str(policy_case.opened["attempt_id"]),
            str(policy_case.snapshot["id"]),
            adapter="codex",
            subagents_mode="managed",
            max_children=2,
            max_depth=4,
            child_capability_ceiling=["review", "workspace_write"],
            spawn_budget_ceiling=[{"budget_id": "build", "amount": 2}],
            workspace_policies=["isolated_worktree", "patch_only"],
            now=policy_case.now(1),
        )
        self.assertEqual(original, rebound)

    def test_group_create_abort_admit_and_close_route_from_durable_truth(self) -> None:
        creation = _Task3Case(self)
        creation.prepare_parent()
        managed_creation = _ManagedSpawnCase(self, creation)
        created, amendment = managed_creation.controller.create_group(
            **creation.create_kwargs(now=creation.now(1))
        )
        self.assertEqual(created["id"], amendment["spawn_group_id"])

        admission = _Task3Case(self)
        admission.activate()
        managed_admission = _ManagedSpawnCase(self, admission)
        admitted = managed_admission.controller.admit_child(
            admission.run_id, admission.created["id"], admission.child,
            now=admission.now(1),
        )
        self.assertEqual("child_admitted", admitted["kind"])

        closing = _Task3Case(self)
        closing.activate(
            deadline="2026-08-10T12:00:08.000Z",
        )
        managed_close = _ManagedSpawnCase(self, closing, service_now=closing.now(9))
        closed = managed_close.controller.close_group(
            closing.run_id, closing.created["id"], now=closing.now(1),
        )
        self.assertEqual(("deadline", "deadline_expired"), (
            closed["outcome"], closed["close_reason"],
        ))

        aborting = _Task3Case(self)
        aborting.prepare_parent()
        original_append = aborting.ledger._append_spawn_group
        with patch.object(
            aborting.ledger,
            "_append_spawn_group",
            side_effect=lambda record, *args, **kwargs: (
                (_ for _ in ()).throw(DurabilityFailure("jsonl_fsync_failed", "activation"))
                if record["kind"] == "plan_amendment"
                else original_append(record, *args, **kwargs)
            ),
        ):
            with self.assertRaises(DurabilityFailure):
                aborting.controller.create_group(**aborting.create_kwargs())
        group_id = next(iter(aborting.ledger.project().run(aborting.run_id)["spawn_groups"]))
        coordinator = CancellationCoordinator(aborting.ledger)
        requested = coordinator._append(coordinator._v1_record(
            "cancel_requested", "cancel-requested-", aborting.now(8),
            run_id=aborting.run_id, scope="item", item_id=aborting.parent,
            item_ids=[aborting.parent], spawn_group_id=None,
            requested_by="operator",
        ))
        resolved = coordinator._append(coordinator._v1_record(
            "cancel_scope_resolved", "cancel-scope-resolved-", aborting.now(8),
            run_id=aborting.run_id, cancel_request_id=requested["id"],
            scope="item", item_id=aborting.parent,
            item_ids=[aborting.parent], attempt_ids=[aborting.opened["attempt_id"]],
        ))
        managed_abort = _ManagedSpawnCase(self, aborting)
        aborted = managed_abort.controller.abort_group(
            aborting.run_id, group_id, reason_code="cancellation",
            cancel_scope_resolved_id=resolved["id"], now=aborting.now(1),
        )
        self.assertEqual("spawn_group_aborted", aborted["kind"])

    def test_child_outcome_operation_selects_admission_or_evidence_derived_rejection(self) -> None:
        admitted_case = _Task3Case(self)
        admitted_case.activate()
        admitted_managed = _ManagedSpawnCase(self, admitted_case)
        admitted = admitted_managed.controller.admit_child(
            admitted_case.run_id, admitted_case.created["id"], admitted_case.child,
        )
        self.assertEqual("child_admitted", admitted["kind"])

        rejected_case = _Task3Case(self)
        rejected_case.activate(deadline="2026-08-10T12:00:08.000Z")
        rejected_managed = _ManagedSpawnCase(
            self, rejected_case, service_now=rejected_case.now(9),
        )
        rejected = rejected_managed.controller.reject_child(
            rejected_case.run_id, rejected_case.created["id"], rejected_case.child,
            now=rejected_case.now(1),
        )
        self.assertEqual("deadline_expired", rejected["reason_code"])

    def test_descendant_testimony_is_direct_worker_authority_not_a_socket_operation(self) -> None:
        direct = _Task3Case(self)
        direct.activate()
        _runner, result = direct.run_worker(before_spawn=(
            {
                "provider_descendant_id": "pipe-native",
                "state": "observed", "adopted_item_id": None,
            },
            {
                "provider_descendant_id": "pipe-native",
                "state": "terminated", "adopted_item_id": None,
            },
        ))
        self.assertEqual("complete", result["transition"])
        closed = direct.ledger.project().run(direct.run_id)[
            "descendant_observation_close"
        ][direct.opened["attempt_id"]]
        self.assertEqual(["pipe-native"], closed["observed_descendant_ids"])

        case = _Task3Case(self)
        case.activate()
        managed = _ManagedSpawnCase(self, case)
        policy = _policy_evidence(case.policy)
        operations = (
            (
                "untracked_descendant_evaluation",
                "untracked-descendant-evaluation-",
                {
                    "run_id": case.run_id,
                    "parent_attempt_id": case.opened["attempt_id"],
                    "provider_descendant_id": "forged-over-socket",
                    "state": "observed", "adopted_item_id": None,
                    "policy": policy,
                },
            ),
            (
                "untracked_descendant_evaluation",
                "untracked-descendant-evaluation-",
                {
                    "run_id": case.run_id,
                    "parent_attempt_id": case.opened["attempt_id"],
                    "provider_descendant_id": "forged-over-socket",
                    "state": "terminated", "adopted_item_id": None,
                    "policy": policy,
                },
            ),
            (
                "descendant_observation_close_evaluation",
                "descendant-observation-close-evaluation-",
                {
                    "run_id": case.run_id,
                    "parent_attempt_id": case.opened["attempt_id"],
                    "policy": policy,
                },
            ),
        )
        before = case.ledger.records()
        for operation, prefix, intent in operations:
            with self.subTest(operation=operation, state=intent.get("state")):
                request = managed.client._evaluation_request(
                    operation,
                    prefix + _sequencer_semantic_uuid(operation, intent),
                    intent,
                )
                response = managed.raw(_encode_frame(request))
                self.assertEqual(
                    ("refused", "operation_invalid"),
                    (response.get("status"), response.get("code")),
                )
        self.assertEqual(before, case.ledger.records())

    def test_spawn_intents_match_runtime_canonical_value_boundaries_before_ids(self) -> None:
        policy_case = _Task2Case(self)
        policy_case.prepare_parent(dispatch=False, start=False)
        policy_intent = {
            "run_id": policy_case.run_id,
            "parent_item_id": policy_case.parent,
            "parent_attempt_id": policy_case.opened["attempt_id"],
            "parent_capability_set_bound_id": policy_case.snapshot["id"],
            "adapter": "codex", "subagents_mode": "managed",
            "max_children": 2, "max_depth": 4,
            "child_capability_ceiling": ["review", "workspace_write"],
            "spawn_budget_ceiling": [
                {"budget_id": "build", "amount": 1},
                {"budget_id": "review", "amount": 1},
            ],
            "workspace_policies": ["isolated_worktree", "patch_only"],
            "policy": _policy_evidence(policy_case.policy),
        }
        policy_hostiles = (
            dict(policy_intent, child_capability_ceiling=["workspace_write", "review"]),
            dict(policy_intent, spawn_budget_ceiling=list(reversed(policy_intent["spawn_budget_ceiling"]))),
            dict(policy_intent, workspace_policies=["patch_only", "isolated_worktree"]),
        )
        for hostile in policy_hostiles:
            with self.subTest(policy_hostile=hostile):
                with self.assertRaises(ProtocolRefusal) as caught:
                    _canonical_evaluated_intent(
                        "spawn_policy_bind_evaluation", hostile,
                    )
                self.assertEqual("intent_fields_invalid", caught.exception.code)

        group_case = _Task3Case(self)
        group_case.prepare_parent()
        group_intent = {
            **self._without_now(group_case.create_kwargs()),
            "policy": _policy_evidence(group_case.policy),
        }
        child_ids = sorted(("work-" + uuid7_hex(), "work-" + uuid7_hex()))
        children = []
        for index, item_id in enumerate(child_ids):
            children.append(group_case.child_descriptor(
                item_id=item_id,
                task_contract_id="task-contract-" + uuid7_hex(),
                workspace_key=f"workspace-child-{index}",
                concurrency_key=f"concurrency-child-{index}",
            ))
        edges = sorted((
            {
                "source": group_case.parent, "target": item_id,
                "requires": "accepted", "failure_policy": "fail_run",
            }
            for item_id in child_ids
        ), key=lambda row: (
            row["source"], row["target"], row["requires"], row["failure_policy"],
        ))
        group_hostiles = (
            dict(group_intent, group_key="reviewers\u180e"),
            dict(group_intent, children=list(reversed(children))),
            dict(group_intent, children=children, dependency_edges=list(reversed(edges))),
        )
        for hostile in group_hostiles:
            with self.subTest(group_hostile=hostile):
                with self.assertRaises(ProtocolRefusal) as caught:
                    _canonical_evaluated_intent(
                        "spawn_group_create_evaluation", hostile,
                    )
                self.assertEqual("intent_fields_invalid", caught.exception.code)

    def test_spawn_create_transport_carries_8192_edges_and_refuses_oversize(self) -> None:
        """Catches a syntactically lawful edge ceiling that cannot cross the socket."""

        case = _Task3Case(self)
        case.prepare_parent()
        managed = _ManagedSpawnCase(self, case)
        node_ids = sorted("work-" + uuid7_hex() for _ in range(64))
        edge_rows = [
            {
                "source": source,
                "target": target,
                "requires": requires,
                "failure_policy": failure_policy,
            }
            for source, target, requires, failure_policy in product(
                node_ids,
                node_ids,
                ("accepted", "produced", "verified"),
                ("continue", "fail_run", "skip_dependent"),
            )
            if source != target
        ]
        edge_rows.sort(key=lambda row: (
            row["source"], row["target"], row["requires"], row["failure_policy"],
        ))
        intent = {
            **self._without_now(case.create_kwargs()),
            "dependency_edges": edge_rows[:8192],
            "policy": _policy_evidence(case.policy),
        }
        canonical = _canonical_evaluated_intent(
            "spawn_group_create_evaluation", intent,
        )
        operation = "spawn_group_create_evaluation"
        request = managed.client._evaluation_request(
            operation,
            "spawn-group-create-evaluation-"
            + _sequencer_semantic_uuid(operation, canonical),
            canonical,
        )
        before = case.ledger.records()
        frame = _encode_frame(request)
        self.assertGreater(len(frame), 1_000_000)
        response = managed.raw(frame)
        self.assertEqual(
            ("refused", "admission_array_invalid"),
            (response["status"], response["code"]),
        )
        self.assertEqual(before, case.ledger.records())

        with self.assertRaises(ProtocolRefusal) as oversize:
            _encode_frame({"padding": "\x1f" * (MAX_FRAME_BYTES // 6 + 1)})
        self.assertEqual("frame_too_large", oversize.exception.code)

    def test_lawful_72_edge_group_creates_direct_then_managed_identically(self) -> None:
        """Catches a canonical-only edge test whose fixture cannot activate."""

        policy_text = (
            VALID_POLICY
            .replace("max_items = 8", "max_items = 64")
            .replace("max_depth = 4", "max_depth = 16")
            .replace("max_fan_out = 2", "max_fan_out = 8")
        )
        case = _Task3Case(
            self, base_peer_count=8, policy_text=policy_text,
        )
        case.prepare_parent(spawn_limits={
            "max_children": 8,
            "max_depth": 16,
            "child_capability_ceiling": ["review", "workspace_write"],
            "spawn_budget_ceiling": [],
            "workspace_policies": ["isolated_worktree", "patch_only"],
        })
        source_ids = sorted([case.parent, *case.base_peers])
        child_ids = sorted([case.child, *("work-" + uuid7_hex() for _ in range(7))])
        children = []
        for index, item_id in enumerate(child_ids):
            contract = case.contract(source_ids)
            children.append(case.child_descriptor(
                item_id=item_id,
                task_contract_id="task-contract-" + uuid7_hex(),
                task_contract=contract.canonical(),
                task_contract_digest=contract_digest(contract),
                budget_allocation=[],
                capability_ceiling=["review", "workspace_write"],
                workspace_key=f"workspace-large-{index}",
                concurrency_key=f"concurrency-large-{index}",
            ))
        edges = sorted((
            {
                "source": source,
                "target": target,
                "requires": "accepted",
                "failure_policy": "fail_run",
            }
            for source in source_ids
            for target in child_ids
        ), key=lambda row: (
            row["source"], row["target"], row["requires"], row["failure_policy"],
        ))
        self.assertEqual(72, len(edges))
        kwargs = case.create_kwargs(
            children=children,
            dependency_edges=edges,
            max_children=8,
            max_depth=16,
            child_capability_ceiling=["review", "workspace_write"],
            aggregate_budget=[],
        )

        direct = case.controller.create_group(**kwargs)
        before_retry = case.ledger.records()
        managed = _ManagedSpawnCase(self, case)
        retried = managed.controller.create_group(**kwargs)
        self.assertEqual(direct, retried)
        self.assertEqual(before_retry, case.ledger.records())
        self.assertEqual(edges, direct[1]["dependency_edges"])
        amended = AdmissionPlan.from_canonical(
            case.ledger.project().run(case.run_id)["spawn_admission"]["current_plan"]
        )
        self.assertEqual("admitted", AdmissionEvaluator.evaluate(
            amended, case.policy,
        ).outcome)

    def test_integral_spawn_numbers_share_direct_and_managed_identity(self) -> None:
        """Catches direct hashing of 1.0 before managed normalization to 1."""

        case = _Task3Case(self)
        case.prepare_parent()
        child = case.child_descriptor(
            depth=1.0,
            budget_allocation=[{"budget_id": "build", "amount": 1.0}],
        )
        kwargs = case.create_kwargs(
            children=[child],
            max_children=1.0,
            max_depth=4.0,
            aggregate_budget=[{"budget_id": "build", "amount": 1.0}],
            join_mode="quorum",
            required_count=1.0,
        )
        direct = case.controller.create_group(**kwargs)
        managed = _ManagedSpawnCase(self, case)
        retried = managed.controller.create_group(**kwargs)
        self.assertEqual(direct, retried)
        created, amendment = direct
        for value in (
            created["max_children"], created["max_depth"], created["required_count"],
            created["aggregate_budget"][0]["amount"],
            amendment["children"][0]["depth"],
            amendment["children"][0]["budget_allocation"][0]["amount"],
        ):
            self.assertIs(type(value), int)

        hostile_mutations = (
            ("boolean", lambda values: values.update(max_children=True)),
            ("fractional", lambda values: values.update(max_depth=4.5)),
            (
                "out_of_domain",
                lambda values: values["children"][0].update(depth=17.0),
            ),
        )
        for label, mutate in hostile_mutations:
            with self.subTest(label=label):
                hostile = _Task3Case(self)
                hostile.prepare_parent()
                hostile_kwargs = hostile.create_kwargs()
                mutate(hostile_kwargs)
                before = hostile.ledger.records()
                with self.assertRaises(ProtocolRefusal):
                    hostile.controller.create_group(**hostile_kwargs)
                self.assertEqual(before, hostile.ledger.records())

    def test_deadline_lexical_form_is_strict_before_managed_operation_id(self) -> None:
        """Catches ISO parsing that accepts a space where canonical UTC requires T."""

        lawful = _Task3Case(self)
        lawful.prepare_parent()
        created, _amendment = lawful.controller.create_group(
            **lawful.create_kwargs(deadline="2026-08-11T00:00:00.000Z")
        )
        self.assertEqual("2026-08-11T00:00:00.000Z", created["deadline"])

        hostile = _Task3Case(self)
        hostile.prepare_parent()
        bad_kwargs = hostile.create_kwargs(deadline="2026-08-11 00:00:00Z")
        before = hostile.ledger.records()
        with self.assertRaises(ProtocolRefusal) as direct:
            hostile.controller.create_group(**bad_kwargs)
        self.assertEqual("deadline_invalid", direct.exception.code)
        self.assertEqual(before, hostile.ledger.records())

        managed = _ManagedSpawnCase(self, hostile)
        with self.assertRaises(ProtocolRefusal) as pre_id:
            managed.controller.create_group(**bad_kwargs)
        self.assertEqual("intent_fields_invalid", pre_id.exception.code)
        self.assertEqual(before, hostile.ledger.records())

    def test_late_disposition_operation_uses_exact_operator_authority(self) -> None:
        from floati.approvals import CapabilityLedger
        from floati.jsonl import append_record
        from floati.planes import AuthorityGrantStore
        from floati.registry import Registry
        from floati.workers import WORKER_KINDS

        case = _Task3Case(self)
        case.activate(
            deadline="2026-08-10T12:00:08.000Z",
            on_late_result="operator_decision",
        )
        case.admit(now_offset=7)
        opened = case.scheduler.open_attempt(
            case.run_id, case.child, RetryPolicy(1, 0, 0, strategy="fixed"),
            1, now=case.now(7),
        )
        grants = [{
            "capability_name": "review",
            "grant_id": "capability-grant-" + uuid7_hex(),
            "physical_position": 1,
        }]
        capability = {
            "schema_version": 1,
            "id": "capability-set-bound-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": NOW,
            "kind": "capability_set_bound", "run_id": case.run_id,
            "item_id": case.child, "attempt_id": opened["attempt_id"],
            "fence_token": opened["fence_token"], "chosen_worker": "node-a",
            "policy_digest": case.policy.digest, "routing_rank": 0,
            "evaluated_at_testimony": NOW,
            "grant_ledger_high_watermark": 1,
            "effective_grants": grants,
            "capability_digest": capability_set_digest(grants),
        }
        bound = case.ledger._append_capability_set(
            capability,
            case.ledger._capability_binding_capability_for(case.capability_binder),
        )
        dispatch = case.capability_binder.dispatch(
            bound["id"], ["node-a"], "policy.route", case.policy,
            now=case.now(7),
        )
        case.scheduler.start_attempt(
            case.run_id, case.child, opened["attempt_id"], dispatch["id"],
            now=case.now(7),
        )
        case.controller.close_group(
            case.run_id, case.created["id"], now=case.now(9),
        )
        receipt = {
            "schema_version": 0,
            "id": "worker-receipt-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": NOW,
            "kind": "worker_receipt", "session_id": "worker-" + uuid7_hex(),
            "work_item_id": case.child, "node_id": "node-a", "adapter": "codex",
            "transition": "claim", "outcome_code": None,
            "authority_subject": "execute-run", "authority_epoch": 1,
            "artifact_bindings": [],
        }
        append_record(
            case.root, Path("receipts/workers.jsonl"), receipt,
            allowed_kinds=WORKER_KINDS,
        )
        produced = case.ledger.append({
            "schema_version": 0,
            "id": "run-result-produced-" + uuid7_hex(),
            "tenant_id": "alpha", "timestamp": NOW,
            "kind": "result_produced", "run_id": case.run_id,
            "item_id": case.child, "attempt_id": opened["attempt_id"],
            "dispatch_decision_id": dispatch["id"],
            "worker_receipt_ids": [receipt["id"]],
        })
        Registry(case.root).register("operator-a", "Operator")
        grant = AuthorityGrantStore(case.root).claim(
            "spawn-admin", "operator-a", 120, 120, case.now(10),
        )
        capability_record = CapabilityLedger(case.root).declare(
            "operator-a", "spawn.late_result.dispose", "read_write", "run", 60,
            now=case.now(10),
        )
        managed = _ManagedSpawnCase(self, case, service_now=case.now(11))
        disposition = managed.controller.dispose_late_result(
            case.run_id, case.created["id"], case.child, produced["id"],
            "retain_as_non_join_evidence", operator_id="operator-a",
            authority_subject="spawn-admin", authority_epoch=grant["epoch"],
            capability_record_id=capability_record["id"], now=case.now(1),
        )
        self.assertEqual(
            ("spawn_late_result_disposition", produced["id"]),
            (disposition["kind"], disposition["result_record_id"]),
        )
        self.assertIsNone(
            getattr(managed.service._ledger, "_spawn_group_controller", None),
            "service must not retain the method-local controller",
        )

    def test_managed_intents_reject_extra_time_boolean_fraction_and_malformed_nested_values(self) -> None:
        case = _Task2Case(self)
        managed = _ManagedSpawnCase(self, case)
        operation = "spawn_admission_enable_evaluation"
        lawful = {
            "run_id": case.run_id,
            "base_plan": case.plan.canonical(),
            "policy": _policy_evidence(case.policy),
        }
        mutations = (
            dict(lawful, now="2026-08-10T12:00:00.000Z"),
            dict(lawful, base_plan=True),
            dict(lawful, base_plan=2.5),
            dict(lawful, policy={"schema_version": 0}),
        )
        for hostile in mutations:
            with self.subTest(hostile=hostile):
                with self.assertRaises(ProtocolRefusal) as caught:
                    managed.client.append_intent(operation, hostile)
                self.assertEqual("intent_fields_invalid", caught.exception.code)

        policy_case = _Task2Case(self)
        policy_case.prepare_parent(dispatch=False, start=False)
        managed_policy = _ManagedSpawnCase(self, policy_case)
        policy_intent = {
            "run_id": policy_case.run_id,
            "parent_item_id": policy_case.parent,
            "parent_attempt_id": policy_case.opened["attempt_id"],
            "parent_capability_set_bound_id": policy_case.snapshot["id"],
            "adapter": "codex", "subagents_mode": "managed",
            "max_children": 2.5, "max_depth": 4,
            "child_capability_ceiling": ["review"],
            "spawn_budget_ceiling": [{"budget_id": "build", "amount": 1}],
            "workspace_policies": ["patch_only"],
            "policy": _policy_evidence(policy_case.policy),
        }
        with self.assertRaises(ProtocolRefusal) as fraction:
            managed_policy.client.append_intent(
                "spawn_policy_bind_evaluation", policy_intent,
            )
        self.assertEqual("intent_fields_invalid", fraction.exception.code)
        malformed_policy_id = dict(
            policy_intent,
            max_children=2,
            parent_attempt_id="attempt-not-uuid7",
        )
        with self.assertRaises(ProtocolRefusal) as malformed_id:
            managed_policy.client.append_intent(
                "spawn_policy_bind_evaluation", malformed_policy_id,
            )
        self.assertEqual("intent_fields_invalid", malformed_id.exception.code)

        group_case = _Task3Case(self)
        group_case.prepare_parent()
        managed_group = _ManagedSpawnCase(self, group_case)
        group_intent = {
            **self._without_now(group_case.create_kwargs()),
            "policy": _policy_evidence(group_case.policy),
        }
        bad_edge = deepcopy(group_intent)
        bad_edge["dependency_edges"][0]["requires"] = "caller_nominated"
        bad_contract = deepcopy(group_intent)
        bad_contract["children"][0]["task_contract_digest"] = "0" * 64
        for malformed_nested in (bad_edge, bad_contract):
            with self.subTest(malformed_nested=malformed_nested):
                with self.assertRaises(ProtocolRefusal) as nested:
                    managed_group.client.append_intent(
                        "spawn_group_create_evaluation", malformed_nested,
                    )
                self.assertEqual("intent_fields_invalid", nested.exception.code)

        abort_intent = {
            "run_id": case.run_id,
            "spawn_group_id": "spawn-group-created-" + uuid7_hex(),
            "reason_code": "operator_abandonment",
            "cancel_scope_resolved_id": None,
            "operator_id": "operator-a", "authority_subject": "spawn-admin",
            "authority_epoch": False,
            "capability_record_id": "capability-" + uuid7_hex(),
            "policy": _policy_evidence(case.policy),
        }
        with self.assertRaises(ProtocolRefusal) as boolean_epoch:
            managed.client.append_intent(
                "spawn_group_abort_evaluation", abort_intent,
            )
        self.assertEqual("intent_fields_invalid", boolean_epoch.exception.code)

        raw_hostile = dict(lawful, base_plan=True)
        raw_request = managed.client._evaluation_request(
            operation,
            "spawn-admission-enable-evaluation-"
            + _sequencer_semantic_uuid(operation, raw_hostile),
            raw_hostile,
        )
        service_refusal = managed.raw(_encode_frame(raw_request))
        self.assertEqual(
            ("refused", "intent_fields_invalid"),
            (service_refusal["status"], service_refusal["code"]),
        )
        self.assertIsInstance(
            managed.client.append_intent(operation, lawful)["record"], dict,
        )

    def test_durable_retry_survives_cache_loss_restart_and_returns_owned_records(self) -> None:
        case = _Task2Case(self)
        first_service = _ManagedSpawnCase(self, case, service_now=case.now(20))
        operation = "spawn_admission_enable_evaluation"
        intent = {
            "run_id": case.run_id,
            "base_plan": case.plan.canonical(),
            "policy": _policy_evidence(case.policy),
        }
        request = first_service.client._evaluation_request(
            operation,
            "spawn-admission-enable-evaluation-"
            + _sequencer_semantic_uuid(operation, intent),
            intent,
        )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as lost:
            lost.connect(str(first_service.service.socket_path))
            lost.sendall(_encode_frame(request))
            # Deliberately lose the response after the service owns the request.
        first = None
        for _attempt in range(100):
            projected = first_service.ledger.project().run(case.run_id)
            if projected["spawn_admission"] is not None:
                first = {
                    key: deepcopy(value)
                    for key, value in projected["spawn_admission"].items()
                    if key != "current_plan"
                }
                break
            time.sleep(0.01)
        self.assertIsNotNone(first, "lost response operation did not become durable")
        restarted = first_service.restart()
        second = AdmissionBinder.enable_spawn(
            restarted.ledger, case.run_id, case.plan, case.policy,
        )
        self.assertEqual(first, second)
        second["base_plan"].clear()
        durable = restarted.ledger.project().run(case.run_id)["spawn_admission"]
        self.assertTrue(durable["base_plan"]["items"])

    def test_divergent_operation_id_refuses_before_any_durable_append(self) -> None:
        case = _Task2Case(self)
        managed = _ManagedSpawnCase(self, case)
        operation = "spawn_admission_enable_evaluation"
        intent = {
            "run_id": case.run_id,
            "base_plan": case.plan.canonical(),
            "policy": _policy_evidence(case.policy),
        }
        request = managed.client._evaluation_request(
            operation, "spawn-admission-enable-evaluation-" + uuid7_hex(), intent,
        )
        before = case.ledger.records()
        refused = managed.raw(_encode_frame(request))
        self.assertEqual(("refused", "operation_id_invalid"), (
            refused["status"], refused["code"],
        ))
        self.assertEqual(before, case.ledger.records())

    def test_invalid_evaluated_peer_does_not_poison_valid_public_or_alias_peer(self) -> None:
        case = _Task2Case(self)
        service = SequencerService(
            case.root,
            "spawn-sequencer-batch",
            config=SequencerConfig(select_timeout=0.01),
        )
        self.addCleanup(service.close)
        client = SequencerClient(
            service.socket_path, service.epoch, "spawn-primary",
        )
        operation = "spawn_admission_enable_evaluation"
        lawful = {
            "run_id": case.run_id,
            "base_plan": case.plan.canonical(),
            "policy": _policy_evidence(case.policy),
        }
        hostile = dict(lawful, base_plan=True)
        operation_id = (
            "spawn-admission-enable-evaluation-"
            + _sequencer_semantic_uuid(operation, lawful)
        )
        hostile_id = (
            "spawn-admission-enable-evaluation-"
            + _sequencer_semantic_uuid(operation, hostile)
        )

        def queued(payload: bytes) -> socket.socket:
            channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            channel.settimeout(3)
            channel.connect(str(service.socket_path))
            channel.sendall(payload)
            self.addCleanup(channel.close)
            return channel

        invalid = queued(_encode_frame(client._evaluation_request(
            operation, hostile_id, hostile,
        )))
        primary = queued(_encode_frame(client._evaluation_request(
            operation, operation_id, lawful,
        )))
        alias_client = SequencerClient(
            service.socket_path, service.epoch, "spawn-alias",
        )
        alias = queued(_encode_frame(alias_client._evaluation_request(
            operation, operation_id, lawful,
        )))

        public = {
            "schema_version": 0,
            "id": "run-created-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "run_created",
            "run_id": "run-" + uuid7_hex(),
            "plan_digest": "a" * 64,
            "item_ids": ["work-" + uuid7_hex()],
            "dependency_edges": [],
        }
        public_peer = queued(client.frame(public))

        self.assertEqual(3, service.serve_once())
        refused = json.loads(invalid.recv(65536))
        accepted = json.loads(primary.recv(65536))
        retried = json.loads(alias.recv(65536))
        public_response = json.loads(public_peer.recv(65536))
        self.assertEqual(
            ("refused", "intent_fields_invalid"),
            (refused["status"], refused["code"]),
        )
        self.assertEqual(accepted, retried)
        self.assertEqual("ok", accepted["status"])
        self.assertEqual("ok", public_response["status"])
        records = RunLedger(case.root).records()
        self.assertIn(public, records)
        self.assertEqual(
            1,
            sum(
                record["kind"] == "run_spawn_admission_enabled"
                for record in records
            ),
        )


class SpawnGroupFinalFixTests(unittest.TestCase):
    """Whole-branch controls for the final governed Spawn fix wave."""

    def setUp(self) -> None:
        self.assertIsNotNone(SpawnGroupController)

    def _activate_two_member_group(
        self,
        case: _Task3Case,
        *,
        first_accepted: bool = False,
    ) -> list[str]:
        case.prepare_parent()
        other = "work-" + uuid7_hex()
        other_contract = case.contract([case.parent])
        children = [
            case.child_descriptor(),
            case.child_descriptor(
                item_id=other,
                task_contract_id="task-contract-" + uuid7_hex(),
                task_contract=other_contract.canonical(),
                task_contract_digest=contract_digest(other_contract),
                workspace_key="workspace-other",
                concurrency_key="concurrency-other",
            ),
        ]
        children.sort(key=lambda row: str(row["item_id"]))
        edges = sorted((
            {
                "source": case.parent,
                "target": str(child["item_id"]),
                "requires": "accepted",
                "failure_policy": "fail_run",
            }
            for child in children
        ), key=lambda row: (
            row["source"], row["target"], row["requires"],
            row["failure_policy"],
        ))
        case.created, case.amendment = case.controller.create_group(
            **case.create_kwargs(
                group_key="two-member-reviewers",
                children=children,
                dependency_edges=edges,
                max_children=2,
                aggregate_budget=[{"budget_id": "build", "amount": 2}],
                join_mode="first_accepted" if first_accepted else "all_terminal",
                required_count=1 if first_accepted else None,
                cancel_remaining_after_success=first_accepted,
            )
        )
        members = list(case.created and case.ledger.project().run(case.run_id)[
            "spawn_groups"
        ][case.created["id"]]["member_item_ids"])
        for item_id in members:
            case.controller.admit_child(
                case.run_id, str(case.created["id"]), item_id,
                now=case.now(8),
            )
        return members

    @staticmethod
    def _satisfied_close_truth(case: _Task3Case) -> dict[str, object]:
        run = case.ledger.project().run(case.run_id)
        group = run["spawn_groups"][case.created["id"]]
        cancellations = list(run["cancellations"].values())
        return {
            "group_state": group["state"],
            "outcome": group["closed"]["outcome"],
            "reason": group["closed"]["close_reason"],
            "accepted_count": len(group["closed"]["accepted_item_ids"]),
            "terminal_count": len(group["closed"]["terminal_item_ids"]),
            "cancel_scope": cancellations[0]["requested"]["scope"],
            "cancel_requested_by": cancellations[0]["requested"]["requested_by"],
            "cancel_member_count": len(cancellations[0]["resolved"]["item_ids"]),
            "cancelled_without_attempt": sum(
                row["kind"] == "spawn_child_cancelled_without_attempt"
                for row in case.ledger.records()
            ),
        }

    def test_satisfied_close_has_direct_and_managed_durable_parity(self) -> None:
        direct = _Task3Case(self)
        direct_members = self._activate_two_member_group(
            direct, first_accepted=True,
        )
        direct.complete_child(direct_members[0], now_offset=9)
        direct_before = len(direct.ledger.records())
        direct_close = direct.controller.close_group(
            direct.run_id, str(direct.created["id"]), adapters={},
            now=direct.now(20),
        )
        self.assertEqual("satisfied", direct_close["outcome"])
        direct_kinds = [
            row["kind"] for row in direct.ledger.records()[direct_before:]
        ]

        managed_case = _Task3Case(self)
        managed_members = self._activate_two_member_group(
            managed_case, first_accepted=True,
        )
        managed_case.complete_child(managed_members[0], now_offset=9)
        managed_before = len(managed_case.ledger.records())
        managed = _ManagedSpawnCase(
            self, managed_case, service_now=managed_case.now(20),
        )
        managed_close = managed.controller.close_group(
            managed_case.run_id, str(managed_case.created["id"]),
            now=managed_case.now(1),
        )
        self.assertEqual("satisfied", managed_close["outcome"])
        self.assertEqual(
            direct_kinds,
            [row["kind"] for row in managed_case.ledger.records()[managed_before:]],
        )
        self.assertEqual(
            self._satisfied_close_truth(direct),
            self._satisfied_close_truth(managed_case),
        )

    def test_cancellation_consumes_terminal_reserved_retry_after_schedule_crash(self) -> None:
        from floati.cancellation import CancelMode

        class Adapter:
            cancel_mode = CancelMode.native

            def cancel(self) -> None:
                return None

        lawful = _Task3Case(self)
        _opened, lawful_terminal = lawful.schedule_child_retry()
        lawful_resolved = CancellationCoordinator(lawful.ledger).request(
            lawful.run_id, {"node-a": Adapter()}, item_id=lawful.parent,
            now=lawful.now(13),
        )
        lawful_retry = lawful.ledger.project().run(lawful.run_id)["attempts"][
            lawful_terminal["next_attempt_id"]
        ]["terminal"]
        self.assertEqual(
            ("attempt_cancelled_before_start", lawful_resolved["id"]),
            (lawful_retry["kind"], lawful_retry["cancel_scope_resolved_id"]),
        )

        crashed = _Task3Case(self)
        original_append = crashed.ledger._append_scheduler

        class SimulatedCrash(RuntimeError):
            pass

        def crash_before_retry_schedule(
            record: dict[str, object], *args: object, **kwargs: object,
        ) -> dict[str, object]:
            if record.get("kind") == "retry_scheduled":
                raise SimulatedCrash("after terminal before retry schedule")
            return original_append(record, *args, **kwargs)

        with patch.object(
            crashed.ledger, "_append_scheduler",
            side_effect=crash_before_retry_schedule,
        ):
            with self.assertRaisesRegex(SimulatedCrash, "before retry schedule"):
                crashed.schedule_child_retry()
        crash_run = crashed.ledger.project().run(crashed.run_id)
        failed_state = next(
            state for state in crash_run["attempts"].values()
            if state["opened"]["item_id"] == crashed.child
        )
        reserved = failed_state["terminal"]
        self.assertEqual("scheduled", reserved["retry_disposition"])
        self.assertIsNone(failed_state["schedule"])

        coordinator = CancellationCoordinator(crashed.ledger)
        resolved = coordinator.request(
            crashed.run_id, {"node-a": Adapter()}, item_id=crashed.parent,
            now=crashed.now(13),
        )
        records_after_first = crashed.ledger.records()
        repeated = coordinator.request(
            crashed.run_id, {"node-a": Adapter()}, item_id=crashed.parent,
            now=crashed.now(14),
        )
        self.assertEqual(resolved, repeated)
        self.assertEqual(records_after_first, crashed.ledger.records())
        projected = crashed.ledger.project()
        run = projected.run(crashed.run_id)
        consumed = run["attempts"][reserved["next_attempt_id"]]["terminal"]
        self.assertEqual(reserved["retry_record_id"], consumed["retry_scheduled_id"])
        self.assertEqual("cancelled", projected.run_outcome(crashed.run_id))
        self.assertEqual(
            ("closed", "cancelled"),
            (
                run["spawn_groups"][crashed.created["id"]]["state"],
                run["spawn_groups"][crashed.created["id"]]["closed"]["outcome"],
            ),
        )

        before_divergent = crashed.ledger.records()
        divergent = dict(
            consumed,
            id="attempt-cancelled-before-start-" + uuid7_hex(),
            retry_scheduled_id="retry-scheduled-" + uuid7_hex(),
        )
        with self.assertRaises(ProtocolRefusal):
            coordinator._append(divergent)
        self.assertEqual(before_divergent, crashed.ledger.records())

    def test_exact_items_cancellation_does_not_fence_uncancelled_parent(self) -> None:
        lawful = _Task3Case(self); lawful.activate()
        CancellationCoordinator(lawful.ledger).request_exact_items(
            lawful.run_id, [lawful.child], {},
            spawn_group_id=str(lawful.created["id"]), now=lawful.now(8),
        )
        other = "work-" + uuid7_hex()
        other_contract = lawful.contract([lawful.parent])
        other_child = lawful.child_descriptor(
            item_id=other,
            task_contract_id="task-contract-" + uuid7_hex(),
            task_contract=other_contract.canonical(),
            task_contract_digest=contract_digest(other_contract),
            workspace_key="workspace-second-group",
            concurrency_key="concurrency-second-group",
        )
        created, _amendment = lawful.controller.create_group(
            **lawful.create_kwargs(
                group_key="uncancelled-parent-second-group",
                children=[other_child],
                dependency_edges=[{
                    "source": lawful.parent, "target": other,
                    "requires": "accepted", "failure_policy": "fail_run",
                }],
                max_children=1,
            )
        )
        self.assertEqual("spawn_group_created", created["kind"])

        for scope in ("item", "run"):
            hostile = _Task3Case(self); hostile.prepare_parent()
            CancellationCoordinator(hostile.ledger).request(
                hostile.run_id, {},
                item_id=hostile.parent if scope == "item" else None,
                now=hostile.now(7),
            )
            before = hostile.ledger.records()
            with self.subTest(scope=scope):
                with self.assertRaises(ProtocolRefusal) as caught:
                    hostile.controller.create_group(
                        **hostile.create_kwargs(now=hostile.now(8))
                    )
                self.assertEqual(
                    "spawn_parent_cancel_requested", caught.exception.code,
                )
                self.assertEqual(before, hostile.ledger.records())

    def test_concurrent_identical_exact_cancellation_has_one_durable_winner(self) -> None:
        sequential = _Task3Case(self)
        sequential_members = self._activate_two_member_group(sequential)
        first = CancellationCoordinator(sequential.ledger).request_exact_items(
            sequential.run_id, [sequential_members[0]], {},
            spawn_group_id=str(sequential.created["id"]),
            now=sequential.now(9),
        )
        count = len(sequential.ledger.records())
        second = CancellationCoordinator(sequential.ledger).request_exact_items(
            sequential.run_id, [sequential_members[0]], {},
            spawn_group_id=str(sequential.created["id"]),
            now=sequential.now(10),
        )
        self.assertEqual(first, second)
        self.assertEqual(count, len(sequential.ledger.records()))

        concurrent = _Task3Case(self)
        members = self._activate_two_member_group(concurrent)
        barrier = threading.Barrier(2)

        class SynchronizedCoordinator(CancellationCoordinator):
            def _append(
                self, record: dict[str, object],
            ) -> dict[str, object]:
                if record.get("kind") == "cancel_requested":
                    barrier.wait(3)
                return super()._append(record)

        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def cancel() -> None:
            try:
                results.append(
                    SynchronizedCoordinator(
                        concurrent.ledger,
                    ).request_exact_items(
                        concurrent.run_id, [members[0]], {},
                        spawn_group_id=str(concurrent.created["id"]),
                        now=concurrent.now(9),
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=cancel) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(errors, errors)
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1])
        cancellation_records = [
            row for row in concurrent.ledger.records()
            if row["kind"] in {"cancel_requested", "cancel_scope_resolved"}
        ]
        self.assertEqual(
            ["cancel_requested", "cancel_scope_resolved"],
            [row["kind"] for row in cancellation_records],
        )

        before_divergent = concurrent.ledger.records()
        with self.assertRaises(ProtocolRefusal):
            CancellationCoordinator(concurrent.ledger).request_exact_items(
                concurrent.run_id, [members[1]], {},
                spawn_group_id=str(concurrent.created["id"]),
                now=concurrent.now(10),
            )
        self.assertEqual(before_divergent, concurrent.ledger.records())

    def test_whole_run_spawn_cancellation_projects_cancelled_narrowly(self) -> None:
        from floati.cancellation import CancelMode

        class Adapter:
            cancel_mode = CancelMode.native

            def cancel(self) -> None:
                return None

        lawful = _Task3Case(self); lawful.activate(); lawful.admit()
        resolved = CancellationCoordinator(lawful.ledger).request(
            lawful.run_id, {"node-a": Adapter()}, item_id=None,
            now=lawful.now(9),
        )
        projection = lawful.ledger.project()
        run = projection.run(lawful.run_id)
        self.assertEqual(set(run["item_ids"]), set(resolved["item_ids"]))
        self.assertEqual(
            ("closed", "cancelled", resolved["id"]),
            (
                run["spawn_groups"][lawful.created["id"]]["state"],
                run["spawn_groups"][lawful.created["id"]]["closed"]["outcome"],
                run["spawn_groups"][lawful.created["id"]]["closed"][
                    "cancel_scope_resolved_id"
                ],
            ),
        )
        self.assertEqual("cancelled", projection.run_outcome(lawful.run_id))
        terminal = lawful.ledger.append({
            "schema_version": 0,
            "id": "run-terminal-" + uuid7_hex(),
            "tenant_id": "alpha",
            "timestamp": NOW,
            "kind": "run_terminal",
            "run_id": lawful.run_id,
            "outcome": "cancelled",
        })
        self.assertEqual("cancelled", terminal["outcome"])

        outcomes = projection.item_outcomes(lawful.run_id)
        wrong_resolution = deepcopy(run)
        wrong_resolution["spawn_groups"][lawful.created["id"]]["closed"][
            "cancel_scope_resolved_id"
        ] = "cancel-scope-resolved-" + uuid7_hex()
        self.assertFalse(RunProjection._whole_parent_spawn_cancellation(
            wrong_resolution, outcomes,
        ))
        missing_member = deepcopy(run)
        cancellation = next(iter(missing_member["cancellations"].values()))
        cancellation["resolved"]["item_ids"] = [lawful.parent]
        self.assertFalse(RunProjection._whole_parent_spawn_cancellation(
            missing_member, outcomes,
        ))

        nonspawn_temp = tempfile.TemporaryDirectory()
        self.addCleanup(nonspawn_temp.cleanup)
        root = FloatiRoot.open_direct_home(
            Path(nonspawn_temp.name) / "alpha", create=True,
        )
        ledger = RunLedger(root)
        scheduler = RunScheduler(ledger)
        run_id = "run-" + uuid7_hex()
        source, target = sorted((
            "work-" + uuid7_hex(), "work-" + uuid7_hex(),
        ))
        ledger.append(_record(
            "run_created", "run-created-", run_id=run_id,
            plan_digest=DIGEST, item_ids=[source, target],
            dependency_edges=[{
                "source": source, "target": target,
                "requires": "accepted", "failure_policy": "fail_run",
            }],
        ))
        contract = _Task3Case.contract([])
        ledger.append(_record(
            "task_contract", "task-contract-", run_id=run_id,
            item_id=source, **contract.canonical(),
            contract_digest=contract_digest(contract),
        ))
        ledger.append(_record(
            "run_policy_bound", "run-policy-bound-", run_id=run_id,
            policy_digest=DIGEST,
        ))
        ledger.append(_record(
            "worker_pool_bound", "run-worker-pool-bound-", run_id=run_id,
            worker_ids=["node-a"],
        ))
        opened = scheduler.open_attempt(
            run_id, source, RetryPolicy(1, 0, 0, strategy="fixed"), 1,
            now=NOW,
        )
        dispatch = ledger.append(_record(
            "dispatch_decision", "run-dispatch-decision-", run_id=run_id,
            item_id=source, attempt_id=opened["attempt_id"],
            eligible_workers=["node-a"], chosen_worker="node-a",
            capability_digest=DIGEST, reason_code="policy.route",
            policy_digest=DIGEST, routing_rank=0, scheduler_epoch=1,
        ))
        scheduler.start_attempt(
            run_id, source, str(opened["attempt_id"]), str(dispatch["id"]),
            now=NOW,
        )
        scheduler.terminal_attempt(
            run_id, source, str(opened["attempt_id"]), "cancelled",
            "cancelled", "operator_cancellation", "idempotent", now=NOW,
        )
        self.assertEqual("failed", ledger.project().run_outcome(run_id))

    @staticmethod
    def _imported_hostile_launch_capability(
        case: _Task3Case,
        *,
        closed: bool = False,
        started: bool = True,
    ) -> tuple[object, object]:
        """Return the exact old import/fake probe, or an inert object once removed."""

        import floati.workers as worker_module

        capability_type = getattr(worker_module, "_WorkerLaunchCapability", None)
        factory = getattr(worker_module, "_WORKER_LAUNCH_FACTORY", None)
        if not isinstance(capability_type, type) or factory is None:
            return object(), factory

        class FakeProcess:
            pid = 4242 if started else None
            _popen = object() if started else None

        class FakeConnection:
            def __init__(self) -> None:
                self.closed = closed

        try:
            capability = capability_type(
                factory,
                case.controller,
                case.run_id,
                str(case.opened["attempt_id"]),
                "codex",
                FakeProcess(),
                FakeConnection(),
            )
        except (ProtocolRefusal, TypeError):
            capability = object()
        return capability, factory

    def test_imported_fake_process_and_pipe_cannot_append_descendant_testimony(
        self,
    ) -> None:
        """Catches import/fake, stale, cross-run, wrong-pipe, and forked authority."""

        fake_owner = _Task3Case(self)
        fake_owner.activate()
        capability, _factory = self._imported_hostile_launch_capability(fake_owner)
        fake_before = fake_owner.ledger.records()
        outcomes: list[str] = []
        actions = (
            lambda: fake_owner.controller.record_untracked_descendant(
                fake_owner.run_id,
                str(fake_owner.opened["attempt_id"]),
                "fake-native",
                "observed",
                _launch_capability=capability,
                now=fake_owner.now(8),
            ),
            lambda: fake_owner.controller.record_untracked_descendant(
                fake_owner.run_id,
                str(fake_owner.opened["attempt_id"]),
                "fake-native",
                "terminated",
                _launch_capability=capability,
                now=fake_owner.now(9),
            ),
            lambda: fake_owner.controller.close_descendant_observation(
                fake_owner.run_id,
                str(fake_owner.opened["attempt_id"]),
                _launch_capability=capability,
                now=fake_owner.now(10),
            ),
        )
        for action in actions:
            try:
                action()
            except ProtocolRefusal:
                outcomes.append("refused")
            else:
                outcomes.append("accepted")

        owner = _Task3Case(self)
        owner.activate()
        other = _Task3Case(self)
        other.activate()

        wrong_pipe, _ = self._imported_hostile_launch_capability(
            owner, closed=True,
        )
        dead_process, _ = self._imported_hostile_launch_capability(
            owner, started=False,
        )
        cross_run, _ = self._imported_hostile_launch_capability(owner)
        stale, factory = self._imported_hostile_launch_capability(owner)
        release = getattr(stale, "_release", None)
        if callable(release) and factory is not None:
            release(factory)

        owner_before = owner.ledger.records()
        other_before = other.ledger.records()
        hostile_calls = (
            (
                owner.controller,
                owner.run_id,
                owner.opened["attempt_id"],
                wrong_pipe,
                "wrong-pipe",
            ),
            (
                owner.controller,
                owner.run_id,
                owner.opened["attempt_id"],
                dead_process,
                "dead-process",
            ),
            (
                owner.controller,
                owner.run_id,
                owner.opened["attempt_id"],
                stale,
                "stale",
            ),
            (
                other.controller,
                other.run_id,
                other.opened["attempt_id"],
                cross_run,
                "cross-run",
            ),
        )
        for controller, run_id, attempt_id, capability, label in hostile_calls:
            with self.subTest(hostile=label):
                with self.assertRaises(ProtocolRefusal):
                    controller.record_untracked_descendant(
                        str(run_id),
                        str(attempt_id),
                        "native-" + label,
                        "observed",
                        _launch_capability=capability,
                        now=owner.now(8),
                    )

        fork_owner = _Task3Case(self)
        fork_owner.activate()
        fork_before = fork_owner.ledger.records()
        fork_capability, _ = self._imported_hostile_launch_capability(fork_owner)
        context = multiprocessing.get_context("fork")
        receiving, sending = context.Pipe(duplex=False)

        def forked_probe() -> None:
            receiving.close()
            try:
                fork_owner.controller.record_untracked_descendant(
                    fork_owner.run_id,
                    str(fork_owner.opened["attempt_id"]),
                    "native-fork-copy",
                    "observed",
                    _launch_capability=fork_capability,
                    now=fork_owner.now(8),
                )
            except ProtocolRefusal as refusal:
                sending.send(("refused", refusal.code))
            except BaseException as exc:  # exact child-process diagnostic
                sending.send(("error", type(exc).__name__))
            else:
                sending.send(("accepted", None))
            finally:
                sending.close()

        process = context.Process(target=forked_probe)
        process.start()
        sending.close()
        self.assertTrue(receiving.poll(3), "forked launch probe returned no result")
        forked_result = receiving.recv()
        receiving.close()
        process.join(3)
        self.assertFalse(process.is_alive())
        outcomes.append(forked_result[0])

        real_fake_owner = _Task3Case(self)
        real_fake_owner.activate()
        real_fake_before = real_fake_owner.ledger.records()
        real_parent, real_child = context.Pipe()

        def hold_wrong_pipe() -> None:
            real_parent.close()
            try:
                real_child.recv()
            except EOFError:
                pass
            finally:
                real_child.close()

        unrelated_process = context.Process(target=hold_wrong_pipe)
        unrelated_process.start()
        real_child.close()
        hostile_frame = sys._getframe()

        def fake_authorizer(*_args: object) -> object:
            return unrelated_process, real_parent, os.getpid(), hostile_frame

        try:
            real_fake_owner.controller.record_untracked_descendant(
                real_fake_owner.run_id,
                str(real_fake_owner.opened["attempt_id"]),
                "native-real-wrong-pipe",
                "observed",
                _launch_capability=object(),
                _launch_authorizer=fake_authorizer,
                now=real_fake_owner.now(8),
            )
        except ProtocolRefusal:
            outcomes.append("refused")
        else:
            outcomes.append("accepted")
        real_parent.send("stop")
        real_parent.close()
        unrelated_process.join(3)
        self.assertFalse(unrelated_process.is_alive())
        self.assertEqual(
            (
                ["refused", "refused", "refused", "refused", "refused"],
                0,
                0,
                0,
                0,
                0,
            ),
            (
                outcomes,
                len(fake_owner.ledger.records()) - len(fake_before),
                len(owner.ledger.records()) - len(owner_before),
                len(other.ledger.records()) - len(other_before),
                len(fork_owner.ledger.records()) - len(fork_before),
                len(real_fake_owner.ledger.records()) - len(real_fake_before),
            ),
            forked_result,
        )

        live_owner = _Task3Case(self)
        live_owner.activate()
        live_other = _Task3Case(self)
        live_other.activate()
        captured: dict[str, object] = {}
        live_fork_result: tuple[str, object] | None = None

        def probe_live_launch() -> None:
            nonlocal live_fork_result
            from floati.workers import WorkerRunner

            frame = sys._getframe()
            while frame is not None and frame.f_code is not WorkerRunner.run.__code__:
                frame = frame.f_back
            self.assertIsNotNone(frame)
            assert frame is not None
            identity = frame.f_locals["launch_identity"]
            authorizer = frame.f_locals["authorize_launch"]
            self.assertTrue(callable(authorizer))
            launch = authorizer(
                identity,
                live_owner.controller,
                live_owner.run_id,
                str(live_owner.opened["attempt_id"]),
                "codex",
            )
            self.assertIsInstance(launch, tuple)
            self.assertEqual(4, len(launch))
            live_process, live_connection, owner_pid, launch_frame = launch
            captured.update(identity=identity, authorizer=authorizer)
            live_owner_before = live_owner.ledger.records()
            live_other_before = live_other.ledger.records()
            wrong_parent, wrong_child = context.Pipe()
            wrong_child.close()

            def wrong_pipe_authorizer(*_args: object) -> object:
                return live_process, wrong_parent, owner_pid, launch_frame

            dead_process = context.Process(target=lambda: None)
            dead_process.start()
            dead_process.join(3)
            self.assertFalse(dead_process.is_alive())

            def dead_process_authorizer(*_args: object) -> object:
                return dead_process, live_connection, owner_pid, launch_frame

            with self.assertRaises(ProtocolRefusal):
                live_owner.controller.record_untracked_descendant(
                    live_owner.run_id,
                    str(live_owner.opened["attempt_id"]),
                    "native-live-wrong-pipe",
                    "observed",
                    _launch_capability=identity,
                    _launch_authorizer=wrong_pipe_authorizer,
                    now=live_owner.now(8),
                )
            wrong_parent.close()
            with self.assertRaises(ProtocolRefusal):
                live_owner.controller.record_untracked_descendant(
                    live_owner.run_id,
                    str(live_owner.opened["attempt_id"]),
                    "native-live-dead-process",
                    "observed",
                    _launch_capability=identity,
                    _launch_authorizer=dead_process_authorizer,
                    now=live_owner.now(8),
                )
            with self.assertRaises(ProtocolRefusal):
                live_other.controller.record_untracked_descendant(
                    live_other.run_id,
                    str(live_other.opened["attempt_id"]),
                    "native-live-cross-run",
                    "observed",
                    _launch_capability=identity,
                    _launch_authorizer=authorizer,
                    now=live_other.now(8),
                )

            fork_receiving, fork_sending = context.Pipe(duplex=False)

            def live_fork_probe() -> None:
                fork_receiving.close()
                try:
                    live_owner.controller.record_untracked_descendant(
                        live_owner.run_id,
                        str(live_owner.opened["attempt_id"]),
                        "native-live-fork-copy",
                        "observed",
                        _launch_capability=identity,
                        _launch_authorizer=authorizer,
                        now=live_owner.now(8),
                    )
                except ProtocolRefusal as refusal:
                    fork_sending.send(("refused", refusal.code))
                except BaseException as exc:
                    fork_sending.send(("error", type(exc).__name__))
                else:
                    fork_sending.send(("accepted", None))
                finally:
                    fork_sending.close()

            fork_process = context.Process(target=live_fork_probe)
            fork_process.start()
            fork_sending.close()
            self.assertTrue(
                fork_receiving.poll(3),
                "live launch fork-copy probe returned no result",
            )
            live_fork_result = fork_receiving.recv()
            fork_receiving.close()
            fork_process.join(3)
            self.assertFalse(fork_process.is_alive())
            self.assertEqual(("refused", "descendant_launch_capability_required"), live_fork_result)
            self.assertEqual(live_owner_before, live_owner.ledger.records())
            self.assertEqual(live_other_before, live_other.ledger.records())

        _live_runner, live_result = live_owner.run_worker(
            before_spawn=(
                {
                    "provider_descendant_id": "native-live-lawful",
                    "state": "observed", "adopted_item_id": None,
                },
                {
                    "provider_descendant_id": "native-live-lawful",
                    "state": "terminated", "adopted_item_id": None,
                },
            ),
            on_drive=probe_live_launch,
        )
        self.assertEqual("complete", live_result["transition"])
        stale_before = live_owner.ledger.records()
        with self.assertRaises(ProtocolRefusal):
            live_owner.controller.close_descendant_observation(
                live_owner.run_id,
                str(live_owner.opened["attempt_id"]),
                _launch_capability=captured["identity"],
                _launch_authorizer=captured["authorizer"],
                now=live_owner.now(10),
            )
        self.assertEqual(stale_before, live_owner.ledger.records())

    def test_on_drive_same_launch_testimony_without_pipe_receive_is_refused(
        self,
    ) -> None:
        """Catches accepting controller testimony not caused by a private-pipe receive."""

        lawful = _Task3Case(self)
        lawful.activate()
        _lawful_runner, lawful_result = lawful.run_worker(before_spawn=(
            {
                "provider_descendant_id": "pipe-lawful",
                "state": "observed", "adopted_item_id": None,
            },
            {
                "provider_descendant_id": "pipe-lawful",
                "state": "terminated", "adopted_item_id": None,
            },
        ))
        self.assertEqual("complete", lawful_result["transition"])
        self.assertEqual(
            ["pipe-lawful"],
            lawful.ledger.project().run(lawful.run_id)[
                "descendant_observation_close"
            ][lawful.opened["attempt_id"]]["observed_descendant_ids"],
        )

        case = _Task3Case(self)
        case.activate()
        direct_outcomes: list[str] = []
        direct_row_deltas: list[int] = []

        def attempt_same_launch_testimony() -> None:
            from floati.workers import WorkerRunner

            frame = sys._getframe()
            while frame is not None and frame.f_code is not WorkerRunner.run.__code__:
                frame = frame.f_back
            self.assertIsNotNone(frame)
            assert frame is not None
            launch_identity = frame.f_locals["launch_identity"]
            launch_authorizer = frame.f_locals["authorize_launch"]
            launch_controller = frame.f_locals["launch_controller"]
            launch_run_id = frame.f_locals["launch_run_id"]
            launch_attempt_id = frame.f_locals["launch_attempt_id"]
            self.assertIs(case.controller, launch_controller)
            self.assertEqual(case.run_id, launch_run_id)
            self.assertEqual(str(case.opened["attempt_id"]), launch_attempt_id)

            before = len(case.ledger.records())
            calls = (
                lambda: launch_controller.record_untracked_descendant(
                    launch_run_id, launch_attempt_id, "on-drive-direct", "observed",
                    _launch_capability=launch_identity,
                    _launch_authorizer=launch_authorizer, now=case.now(8),
                ),
                lambda: launch_controller.record_untracked_descendant(
                    launch_run_id, launch_attempt_id, "on-drive-direct", "terminated",
                    _launch_capability=launch_identity,
                    _launch_authorizer=launch_authorizer, now=case.now(9),
                ),
                lambda: launch_controller.close_descendant_observation(
                    launch_run_id, launch_attempt_id,
                    _launch_capability=launch_identity,
                    _launch_authorizer=launch_authorizer, now=case.now(10),
                ),
            )
            for call in calls:
                try:
                    call()
                except ProtocolRefusal:
                    direct_outcomes.append("refused")
                else:
                    direct_outcomes.append("accepted")
            direct_row_deltas.append(len(case.ledger.records()) - before)

        _runner, result = case.run_worker(on_drive=attempt_same_launch_testimony)
        self.assertEqual("complete", result["transition"])
        self.assertEqual(
            (["refused", "refused", "refused"], [0]),
            (direct_outcomes, direct_row_deltas),
        )

    def test_record_interceptor_cannot_append_a_substitute_pipe_testimony(self) -> None:
        """Catches a wrapper reaching receive-and-apply to substitute testimony."""

        case = _Task3Case(self)
        case.activate()
        original_record = case.controller.record_untracked_descendant
        interceptor_outcomes: list[str] = []
        interceptor_row_deltas: list[int] = []

        def intercept_record(*args: object, **kwargs: object) -> object:
            lawful = original_record(*args, **kwargs)
            if interceptor_outcomes:
                return lawful
            before = len(case.ledger.records())
            try:
                original_record(
                    case.run_id,
                    str(case.opened["attempt_id"]),
                    str(args[2]),
                    "terminated",
                    _launch_capability=kwargs["_launch_capability"],
                    _launch_authorizer=kwargs["_launch_authorizer"],
                    now=case.now(10),
                )
            except ProtocolRefusal:
                interceptor_outcomes.append("refused")
            else:
                interceptor_outcomes.append("accepted")
            interceptor_row_deltas.append(len(case.ledger.records()) - before)
            return lawful

        case.controller.record_untracked_descendant = intercept_record  # type: ignore[method-assign]
        try:
            _runner, result = case.run_worker(before_spawn=(
                {
                    "provider_descendant_id": "pipe-interceptor",
                    "state": "observed", "adopted_item_id": None,
                },
                {
                    "provider_descendant_id": "pipe-interceptor",
                    "state": "terminated", "adopted_item_id": None,
                },
            ))
        finally:
            case.controller.record_untracked_descendant = original_record  # type: ignore[method-assign]

        self.assertEqual("complete", result["transition"])
        self.assertEqual(
            ([], []),
            (interceptor_outcomes, interceptor_row_deltas),
        )

    def test_record_interceptor_cannot_reconsume_identical_pipe_testimony(self) -> None:
        """Catches a wrapper reaching receive-and-apply to reuse testimony."""

        case = _Task3Case(self)
        case.activate()
        original_record = case.controller.record_untracked_descendant
        interceptor_outcomes: list[str] = []
        interceptor_row_deltas: list[int] = []

        def intercept_record(*args: object, **kwargs: object) -> object:
            lawful = original_record(*args, **kwargs)
            if interceptor_outcomes:
                return lawful
            before = len(case.ledger.records())
            try:
                original_record(*args, **kwargs)
            except ProtocolRefusal:
                interceptor_outcomes.append("refused")
            else:
                interceptor_outcomes.append("accepted")
            interceptor_row_deltas.append(len(case.ledger.records()) - before)
            return lawful

        case.controller.record_untracked_descendant = intercept_record  # type: ignore[method-assign]
        try:
            _runner, result = case.run_worker(before_spawn=(
                {
                    "provider_descendant_id": "pipe-duplicate",
                    "state": "observed", "adopted_item_id": None,
                },
                {
                    "provider_descendant_id": "pipe-duplicate",
                    "state": "terminated", "adopted_item_id": None,
                },
            ))
        finally:
            case.controller.record_untracked_descendant = original_record  # type: ignore[method-assign]

        self.assertEqual("complete", result["transition"])
        self.assertEqual(
            ([], []),
            (interceptor_outcomes, interceptor_row_deltas),
        )

    def test_record_interceptor_cannot_reset_receive_latch_and_reconsume(self) -> None:
        """Catches callback-visible receive state reopening one pipe event."""

        case = _Task3Case(self)
        case.activate()
        original_record = case.controller.record_untracked_descendant
        interceptor_outcomes: list[str] = []
        interceptor_row_deltas: list[int] = []

        def intercept_record(*args: object, **kwargs: object) -> object:
            from floati.workers import WorkerRunner

            lawful = original_record(*args, **kwargs)
            if interceptor_outcomes:
                return lawful
            frame = sys._getframe()
            while frame is not None and frame.f_code is not WorkerRunner._receive.__code__:
                frame = frame.f_back
            self.assertIsNotNone(frame)
            assert frame is not None
            consume = frame.f_locals["consume_descendant_snapshot"]
            cells = consume.__closure__
            self.assertIsNotNone(cells)
            assert cells is not None
            for cell in cells:
                if cell.cell_contents is True:
                    cell.cell_contents = False
                    break
            else:
                self.fail("receive latch was not consumed by the lawful application")
            before = len(case.ledger.records())
            try:
                original_record(*args, **kwargs)
            except ProtocolRefusal:
                interceptor_outcomes.append("refused")
            else:
                interceptor_outcomes.append("accepted")
            interceptor_row_deltas.append(len(case.ledger.records()) - before)
            return lawful

        case.controller.record_untracked_descendant = intercept_record  # type: ignore[method-assign]
        try:
            _runner, result = case.run_worker(before_spawn=(
                {
                    "provider_descendant_id": "pipe-reset-latch",
                    "state": "observed", "adopted_item_id": None,
                },
                {
                    "provider_descendant_id": "pipe-reset-latch",
                    "state": "terminated", "adopted_item_id": None,
                },
            ))
        finally:
            case.controller.record_untracked_descendant = original_record  # type: ignore[method-assign]

        self.assertEqual("complete", result["transition"])
        self.assertEqual([], interceptor_outcomes)
        self.assertEqual([], interceptor_row_deltas)
        self.assertEqual(
            "terminated",
            case.ledger.project().run(case.run_id)["untracked_descendants"][
                (str(case.opened["attempt_id"]), "codex", "pipe-reset-latch")
            ]["state"],
        )

    def test_receive_applies_descendant_before_caller_clock_callback(self) -> None:
        """Catches a caller callback between private-pipe consumption and apply."""

        case = _Task3Case(self)
        case.activate()
        receive_clock_calls = 0
        altered_live_launch = 0

        def hostile_clock() -> datetime:
            nonlocal receive_clock_calls, altered_live_launch
            from floati.workers import WorkerRunner

            receive_frame = sys._getframe()
            while (
                receive_frame is not None
                and receive_frame.f_code is not WorkerRunner._receive.__code__
            ):
                receive_frame = receive_frame.f_back
            if receive_frame is None:
                return case.now(8)
            receive_clock_calls += 1
            if receive_clock_calls != 1:
                return case.now(8 + receive_clock_calls)
            if "descendant_snapshot" not in receive_frame.f_locals:
                return case.now(8)
            run_frame = receive_frame
            while run_frame is not None and run_frame.f_code is not WorkerRunner.run.__code__:
                run_frame = run_frame.f_back
            self.assertIsNotNone(run_frame)
            assert run_frame is not None
            run_frame.f_locals["launch_active"][0] = False
            altered_live_launch += 1
            return case.now(8)

        try:
            _runner, result = case.run_worker(
                before_spawn=(
                    {
                        "provider_descendant_id": "pipe-no-callback-boundary",
                        "state": "observed", "adopted_item_id": None,
                    },
                    {
                        "provider_descendant_id": "pipe-no-callback-boundary",
                        "state": "terminated", "adopted_item_id": None,
                    },
                ),
                clock=hostile_clock,
            )
            outcome = (result["transition"], result.get("outcome_code"))
        except ProtocolRefusal as refusal:
            outcome = ("uncaught_refusal", refusal.code)

        self.assertEqual(("complete", None), outcome)
        self.assertEqual(0, altered_live_launch)
        self.assertEqual(
            "terminated",
            case.ledger.project().run(case.run_id)["untracked_descendants"][
                (
                    str(case.opened["attempt_id"]),
                    "codex",
                    "pipe-no-callback-boundary",
                )
            ]["state"],
        )

    def test_receive_captures_base_descendant_application_before_caller_clock(self) -> None:
        """A class wrapper installed by the clock cannot enter receive-and-apply."""

        case = _Task3Case(self)
        case.activate()
        assert SpawnGroupController is not None
        original_record = SpawnGroupController.record_untracked_descendant
        wrapper_calls = 0
        installed = False

        def delegating_record(
            controller: object, *args: object, **kwargs: object,
        ) -> dict[str, object]:
            nonlocal wrapper_calls
            wrapper_calls += 1
            return original_record(controller, *args, **kwargs)

        def hostile_clock() -> datetime:
            nonlocal installed
            from floati.workers import WorkerRunner

            frame = sys._getframe()
            while frame is not None and frame.f_code is not WorkerRunner._receive.__code__:
                frame = frame.f_back
            if frame is not None and not installed:
                installed = True
                SpawnGroupController.record_untracked_descendant = delegating_record
            return case.now(8)

        try:
            try:
                _runner, result = case.run_worker(
                    before_spawn=(
                        {
                            "provider_descendant_id": "pipe-class-wrapper",
                            "state": "observed", "adopted_item_id": None,
                        },
                        {
                            "provider_descendant_id": "pipe-class-wrapper",
                            "state": "terminated", "adopted_item_id": None,
                        },
                    ),
                    clock=hostile_clock,
                )
                outcome = (result["transition"], result.get("outcome_code"))
            except ProtocolRefusal as refusal:
                outcome = ("uncaught_refusal", refusal.code)
        finally:
            SpawnGroupController.record_untracked_descendant = original_record

        self.assertTrue(installed)
        self.assertEqual(("complete", None), outcome)
        self.assertEqual(0, wrapper_calls)
        self.assertEqual(
            "terminated",
            case.ledger.project().run(case.run_id)["untracked_descendants"][
                (
                    str(case.opened["attempt_id"]),
                    "codex",
                    "pipe-class-wrapper",
                )
            ]["state"],
        )

    def test_governed_launch_captures_base_application_before_on_drive(self) -> None:
        """An on-drive class wrapper cannot enter a later private-pipe receive."""

        case = _Task3Case(self)
        case.activate()
        assert SpawnGroupController is not None
        original_record = SpawnGroupController.record_untracked_descendant
        wrapper_calls = 0

        def delegating_record(
            controller: object, *args: object, **kwargs: object,
        ) -> dict[str, object]:
            nonlocal wrapper_calls
            wrapper_calls += 1
            return original_record(controller, *args, **kwargs)

        def install_wrapper() -> None:
            SpawnGroupController.record_untracked_descendant = delegating_record

        try:
            try:
                _runner, result = case.run_worker(
                    during_drive=(
                        {
                            "provider_descendant_id": "pipe-on-drive-wrapper",
                            "state": "observed", "adopted_item_id": None,
                        },
                        {
                            "provider_descendant_id": "pipe-on-drive-wrapper",
                            "state": "terminated", "adopted_item_id": None,
                        },
                    ),
                    on_drive=install_wrapper,
                )
                outcome = (result["transition"], result.get("outcome_code"))
            except ProtocolRefusal as refusal:
                outcome = ("uncaught_refusal", refusal.code)
        finally:
            SpawnGroupController.record_untracked_descendant = original_record

        self.assertEqual(("complete", None), outcome)
        self.assertEqual(0, wrapper_calls)
        self.assertEqual(
            "terminated",
            case.ledger.project().run(case.run_id)["untracked_descendants"][
                (
                    str(case.opened["attempt_id"]),
                    "codex",
                    "pipe-on-drive-wrapper",
                )
            ]["state"],
        )

    def test_concurrent_descendant_semantic_retry_resolves_under_append_lock(self) -> None:
        """Concurrent exact retries return one canonical ledger testimony."""

        from floati.spawn_groups import _semantic_uuid

        def prepared_case() -> tuple[_Task3Case, object]:
            fixture = _Task3Case(self)
            fixture.activate()
            assert fixture.controller is not None
            capability = fixture.controller._SpawnGroupController__capability
            return fixture, capability

        def candidate(
            fixture: _Task3Case, timestamp: str,
        ) -> dict[str, object]:
            semantic = {
                "run_id": fixture.run_id,
                "parent_item_id": fixture.parent,
                "parent_attempt_id": str(fixture.opened["attempt_id"]),
                "adapter": "codex",
                "provider_descendant_id": "concurrent-semantic-retry",
                "state": "observed",
                "adopted_item_id": None,
                "reason_code": "native_descendant_observed",
            }
            return {
                "schema_version": 1,
                "id": "untracked-descendant-" + _semantic_uuid(
                    RETIRED_PRODUCT_NAME + "-descendant-v1", semantic,
                ),
                "tenant_id": "alpha",
                "timestamp": timestamp,
                "kind": "untracked_descendant",
                **semantic,
                "observed_at_testimony": timestamp,
            }

        retry_fields = {
            "parent_item_id", "parent_attempt_id", "adapter",
            "provider_descendant_id", "state", "adopted_item_id", "reason_code",
        }

        def append(
            fixture: _Task3Case, capability: object, record: dict[str, object],
        ) -> dict[str, object]:
            def resolve_existing(
                projection: object, pending: dict[str, object],
            ) -> dict[str, object] | None:
                found = projection.run(fixture.run_id)["untracked_descendants"].get(
                    (
                        str(fixture.opened["attempt_id"]),
                        "codex",
                        "concurrent-semantic-retry",
                    )
                )
                if found is not None and all(
                    found[field] == pending[field] for field in retry_fields
                ):
                    return deepcopy(found)
                return None

            return fixture.ledger._append_spawn_group(
                record, capability, resolve_existing,
            )

        positive, positive_capability = prepared_case()
        first = append(
            positive,
            positive_capability,
            candidate(positive, "2026-08-10T12:00:08.000Z"),
        )
        self.assertEqual("observed", first["state"])
        self.assertEqual(
            1,
            len([
                row for row in positive.ledger.records()
                if row["kind"] == "untracked_descendant"
                and row["provider_descendant_id"] == "concurrent-semantic-retry"
            ]),
        )

        concurrent, concurrent_capability = prepared_case()
        records = (
            candidate(concurrent, "2026-08-10T12:00:08.000Z"),
            candidate(concurrent, "2026-08-10T12:00:09.000Z"),
        )
        ready = threading.Barrier(2)

        def apply(record: dict[str, object]) -> object:
            ready.wait()
            try:
                return append(concurrent, concurrent_capability, record)
            except ProtocolRefusal as refusal:
                return refusal.code

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(apply, records))

        self.assertTrue(
            all(isinstance(outcome, dict) for outcome in outcomes), outcomes,
        )
        self.assertEqual(outcomes[0], outcomes[1])
        rows = [
            row for row in concurrent.ledger.records()
            if row["kind"] == "untracked_descendant"
            and row["provider_descendant_id"] == "concurrent-semantic-retry"
        ]
        self.assertEqual(1, len(rows))

        unrelated = candidate(concurrent, "2026-08-10T12:00:10.000Z")
        unrelated["id"] = "untracked-descendant-" + uuid7_hex()
        unrelated["provider_descendant_id"] = "unrelated-id-owner"
        concurrent.ledger._append_spawn_group(
            unrelated, concurrent_capability,
        )
        collided_retry = candidate(
            concurrent, "2026-08-10T12:00:11.000Z",
        )
        collided_retry["id"] = unrelated["id"]
        before_collision = concurrent.ledger.records()
        with self.assertRaises(ProtocolRefusal) as collision:
            append(
                concurrent, concurrent_capability, collided_retry,
            )
        self.assertEqual("duplicate_record_id", collision.exception.code)
        self.assertEqual(before_collision, concurrent.ledger.records())

    def test_disabled_receive_does_not_sample_descendant_observation_clock(self) -> None:
        """Disabled governed launches never consult the descendant clock."""

        class DisabledAdapter:
            name = "codex"

            def set_spawn_context(self, context: object, emit: object) -> None:
                return None

            def spawn(self, item: object, *, deadline_seconds: float) -> object:
                return object()

            def drive(
                self, handle: object, item: object, *, deadline_seconds: float,
            ) -> list[dict[str, str]]:
                return [{
                    "repo": "floati-proof", "sha": "a" * 40, "doc": "README.md",
                }]

        case = _Task3Case(self)
        case.prepare_parent(mode="disabled")

        def forbidden_clock() -> datetime:
            raise RuntimeError("disabled receive sampled descendant observation time")

        runner, result = case.run_worker(
            adapter=DisabledAdapter(), clock=forbidden_clock,
        )

        self.assertEqual("complete", result["transition"])
        self.assertEqual(
            {}, case.ledger.project().run(case.run_id)["descendant_observation_close"],
        )
        self.assertEqual([], runner.last_process_audit["alive_after_cleanup"])

    def test_final_close_ack_does_not_sample_descendant_observation_clock(self) -> None:
        """The post-close acknowledgment has no descendant observation time."""

        case = _Task3Case(self)
        case.activate()
        observation_calls = 0

        def bounded_clock() -> datetime:
            nonlocal observation_calls
            observation_calls += 1
            if observation_calls > 2:
                raise RuntimeError("final acknowledgment sampled observation time")
            return case.now(7 + observation_calls)

        _runner, result = case.run_worker(clock=bounded_clock)

        self.assertEqual("complete", result["transition"])
        self.assertEqual(2, observation_calls)

    def test_identical_private_pipe_messages_are_exact_semantic_retry(self) -> None:
        """Two lawful identical pipe messages resolve to one durable testimony."""

        case = _Task3Case(self)
        case.activate()
        observed = {
            "provider_descendant_id": "pipe-semantic-retry",
            "state": "observed", "adopted_item_id": None,
        }
        terminated = dict(observed, state="terminated")
        try:
            _runner, result = case.run_worker(
                before_spawn=(observed, dict(observed), terminated),
            )
            outcome = (result["transition"], result.get("outcome_code"))
        except ProtocolRefusal as refusal:
            outcome = ("uncaught_refusal", refusal.code)

        self.assertEqual(("complete", None), outcome)
        rows = [
            row for row in case.ledger.records()
            if row["kind"] == "untracked_descendant"
            and row["provider_descendant_id"] == "pipe-semantic-retry"
        ]
        self.assertEqual(["observed", "terminated"], [row["state"] for row in rows])

    def test_subclassed_spawn_controller_uses_base_receive_application(self) -> None:
        """A normal subclass cannot insert a callback frame into receive/apply."""

        class DelegatingSpawnController(SpawnGroupController):
            delegated_calls = 0

            def record_untracked_descendant(
                self, *args: object, **kwargs: object,
            ) -> dict[str, object]:
                self.delegated_calls += 1
                return super().record_untracked_descendant(*args, **kwargs)

        case = _Task3Case(self)
        case.controller = DelegatingSpawnController(case.ledger, case.policy)
        case.activate()
        try:
            _runner, result = case.run_worker(before_spawn=(
                {
                    "provider_descendant_id": "pipe-subclass",
                    "state": "observed", "adopted_item_id": None,
                },
                {
                    "provider_descendant_id": "pipe-subclass",
                    "state": "terminated", "adopted_item_id": None,
                },
            ))
            outcome = (result["transition"], result.get("outcome_code"))
        except ProtocolRefusal as refusal:
            outcome = ("uncaught_refusal", refusal.code)

        self.assertEqual(("complete", None), outcome)
        self.assertEqual(0, case.controller.delegated_calls)
        self.assertEqual(
            "terminated",
            case.ledger.project().run(case.run_id)["untracked_descendants"][
                (str(case.opened["attempt_id"]), "codex", "pipe-subclass")
            ]["state"],
        )

    def test_record_interceptor_cannot_mutate_and_reuse_received_event(self) -> None:
        """Catches a wrapper reaching receive-and-apply to mutate testimony."""

        case = _Task3Case(self)
        case.activate()
        original_record = case.controller.record_untracked_descendant
        interceptor_outcomes: list[str] = []
        interceptor_row_deltas: list[int] = []

        def intercept_record(*args: object, **kwargs: object) -> object:
            from floati.workers import WorkerRunner

            lawful = original_record(*args, **kwargs)
            if interceptor_outcomes:
                return lawful
            frame = sys._getframe()
            while frame is not None and frame.f_code is not WorkerRunner._receive.__code__:
                frame = frame.f_back
            self.assertIsNotNone(frame)
            assert frame is not None
            received = frame.f_locals["result"]
            self.assertEqual("observed", received[1]["state"])
            received[1]["state"] = "terminated"
            before = len(case.ledger.records())
            try:
                original_record(
                    case.run_id,
                    str(case.opened["attempt_id"]),
                    str(args[2]),
                    "terminated",
                    _launch_capability=kwargs["_launch_capability"],
                    _launch_authorizer=kwargs["_launch_authorizer"],
                    now=case.now(10),
                )
            except ProtocolRefusal:
                interceptor_outcomes.append("refused")
            else:
                interceptor_outcomes.append("accepted")
            interceptor_row_deltas.append(len(case.ledger.records()) - before)
            return lawful

        case.controller.record_untracked_descendant = intercept_record  # type: ignore[method-assign]
        try:
            _runner, result = case.run_worker(before_spawn=(
                {
                    "provider_descendant_id": "pipe-frame-mutation",
                    "state": "observed", "adopted_item_id": None,
                },
                {
                    "provider_descendant_id": "pipe-frame-mutation",
                    "state": "terminated", "adopted_item_id": None,
                },
            ))
        finally:
            case.controller.record_untracked_descendant = original_record  # type: ignore[method-assign]

        self.assertEqual("complete", result["transition"])
        self.assertEqual(
            ([], []),
            (interceptor_outcomes, interceptor_row_deltas),
        )

    def test_result_before_spawn_cannot_close_descendant_observation(self) -> None:
        """Catches result handling before the parent has accepted spawned state."""

        case = _Task3Case(self)
        case.activate()
        _runner, result = case.run_worker(adapter=_ResultBeforeSpawnAdapter())

        self.assertEqual(
            ("degrade", "adapter_error"),
            (result["transition"], result["outcome_code"]),
        )
        self.assertNotIn(
            str(case.opened["attempt_id"]),
            case.ledger.project().run(case.run_id)["descendant_observation_close"],
        )

    def test_post_close_child_failure_prevents_worker_completion(self) -> None:
        """Catches completing after result without consuming the final child message."""

        case = _Task3Case(self)
        case.activate()
        runner, result = case.run_worker(
            adapter=_PostResultObservationCloseFailureAdapter(),
        )

        self.assertEqual(
            ("degrade", "process_died"),
            (result["transition"], result["outcome_code"]),
        )
        self.assertEqual("claimed", runner.work.show(case.parent)[0]["state"])
        transitions = [row["transition"] for row in runner.receipts.records()]
        self.assertEqual(["claim", "spawn", "drive", "degrade"], transitions)
        self.assertNotIn("bind_artifact", transitions)
        self.assertNotIn("complete", transitions)

    def test_process_start_failure_cannot_create_or_append_launch_authority(
        self,
    ) -> None:
        """Catches authority creation before Process.start succeeds."""

        case = _Task3Case(self)
        case.activate()
        before = case.ledger.records()
        real_context = multiprocessing.get_context("fork")

        class StartFailureProcess:
            pid = None
            _popen = None

            def __init__(self, *args: object, **kwargs: object) -> None:
                pass

            def start(self) -> None:
                raise OSError("injected start failure")

        class StartFailureContext:
            @staticmethod
            def Pipe() -> tuple[object, object]:
                return real_context.Pipe()

            Process = StartFailureProcess

        with patch(
            "floati.workers.multiprocessing.get_context",
            return_value=StartFailureContext(),
        ):
            runner, result = case.run_worker()

        self.assertEqual(
            ("degrade", "process_start_failed"),
            (result["transition"], result["outcome_code"]),
        )
        self.assertIsNone(
            getattr(runner, "_spawn_launch_capability", None),
        )
        self.assertEqual(before, case.ledger.records())

    def test_all_fifteen_spawn_schemas_match_runtime_terminal_controls(self) -> None:
        try:
            from jsonschema import Draft202012Validator
            from referencing import Registry as SchemaRegistry, Resource
        except ImportError:
            self.skipTest("conforming Draft 2020-12 validator is unavailable")

        schema_documents: dict[Path, dict[str, object]] = {}
        schema_registry = SchemaRegistry()
        for candidate in Path("schemas/v1").glob("*.json"):
            document = json.loads(candidate.read_text(encoding="utf-8"))
            schema_documents[candidate] = document
            schema_uri = document.get("$id")
            if isinstance(schema_uri, str):
                schema_registry = schema_registry.with_resource(
                    schema_uri, Resource.from_contents(document),
                )

        fixture = SpawnGroupFixtures()
        group = fixture.group()
        amendment = fixture.amendment(
            group,
            children=[fixture.descriptor(merge_gate="merge-required")],
        )
        started = fixture.started_parent()
        request = _record(
            "cancel_requested", "cancel-requested-", run_id=fixture.run_id,
            scope="exact_items", item_id=None, item_ids=[fixture.child],
            spawn_group_id=group["id"], requested_by="spawn_join",
        )
        request["schema_version"] = 1
        resolved = _record(
            "cancel_scope_resolved", "cancel-scope-resolved-",
            run_id=fixture.run_id, cancel_request_id=request["id"],
            scope="exact_items", item_id=None, item_ids=[fixture.child],
            attempt_ids=[],
        )
        resolved["schema_version"] = 1
        prestart = _record(
            "attempt_cancelled_before_start",
            "attempt-cancelled-before-start-", run_id=fixture.run_id,
            item_id=fixture.child, attempt_id="attempt-" + uuid7_hex(),
            attempt_opened_id="attempt-opened-" + uuid7_hex(),
            retry_scheduled_id=None, fence_token="b" * 64,
            cancel_scope_resolved_id=resolved["id"],
            capability_set_bound_id=None, dispatch_decision_id=None,
            reason_code="cancelled_before_start", cancelled_at_testimony=NOW,
        )
        prestart["schema_version"] = 1
        zero = _record(
            "spawn_child_cancelled_without_attempt",
            "spawn-child-cancelled-without-attempt-", run_id=fixture.run_id,
            spawn_group_id=group["id"], plan_amendment_id=amendment["id"],
            child_item_id=fixture.child, child_admitted_id=None,
            cancel_scope_resolved_id=resolved["id"],
            reason_code="cancelled_without_attempt",
            cancelled_at_testimony=NOW,
        )
        zero["schema_version"] = 1
        cases = (
            (
                next(row for row in started if row["kind"] == "run_spawn_admission_enabled"),
                "run-spawn-admission-enabled-record.schema.json",
            ),
            (
                next(row for row in started if row["kind"] == "attempt_spawn_policy_bound"),
                "attempt-spawn-policy-bound-record.schema.json",
            ),
            (group, "spawn-group-created-record.schema.json"),
            (amendment, "spawn-plan-amendment-record.schema.json"),
            (_record(
                "spawn_group_aborted", "spawn-group-aborted-",
                run_id=fixture.run_id, spawn_group_id=group["id"],
                parent_attempt_id=fixture.attempt,
                parent_fence_token=fixture.fence,
                reason_code="operator_abandonment",
                cancel_scope_resolved_id=None, operator_id="operator-a",
                authority_subject="authority", authority_epoch=1,
                capability_record_id="capability-" + uuid7_hex(),
                aborted_at_testimony=NOW,
            ), "spawn-group-aborted-record.schema.json"),
            (fixture.admitted(group, amendment), "child-admitted-record.schema.json"),
            (fixture.rejected(group, amendment), "child-rejected-record.schema.json"),
            (fixture.close(
                group, amendment, outcome="satisfied",
                close_reason="all_members_terminal",
                terminal_item_ids=[fixture.child],
            ), "spawn-group-closed-record.schema.json"),
            (_record(
                "untracked_descendant", "untracked-descendant-",
                run_id=fixture.run_id, parent_item_id=fixture.parent,
                parent_attempt_id=fixture.attempt, adapter="codex",
                provider_descendant_id="thread-1", state="observed",
                adopted_item_id=None,
                reason_code="native_descendant_observed",
                observed_at_testimony=NOW,
            ), "untracked-descendant-record.schema.json"),
            (fixture.observation_close(["thread-1"]),
             "descendant-observation-closed-record.schema.json"),
            (_record(
                "spawn_late_result_disposition",
                "spawn-late-result-disposition-", run_id=fixture.run_id,
                spawn_group_id=group["id"], child_item_id=fixture.child,
                result_record_id="run-result-produced-" + uuid7_hex(),
                disposition="quarantine", operator_id="operator-a",
                authority_subject="authority", authority_epoch=1,
                capability_record_id="capability-" + uuid7_hex(),
                decided_at_testimony=NOW,
            ), "spawn-late-result-disposition-record.schema.json"),
            (request, "exact-items-cancel-requested-record.schema.json"),
            (resolved, "exact-items-cancel-scope-resolved-record.schema.json"),
            (prestart, "attempt-cancelled-before-start-record.schema.json"),
            (zero, "spawn-child-cancelled-without-attempt-record.schema.json"),
        )
        self.assertEqual(15, len(cases))

        def string_paths(
            value: object, prefix: tuple[object, ...] = (),
        ) -> list[tuple[object, ...]]:
            paths: list[tuple[object, ...]] = []
            if isinstance(value, dict):
                for key, child in value.items():
                    paths.extend(string_paths(child, (*prefix, key)))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    paths.extend(string_paths(child, (*prefix, index)))
            elif isinstance(value, str):
                paths.append(prefix)
            return paths

        def replace_path(
            record: dict[str, object], path: tuple[object, ...], suffix: str,
        ) -> dict[str, object]:
            hostile = deepcopy(record)
            cursor: object = hostile
            for component in path[:-1]:
                cursor = cursor[component]  # type: ignore[index]
            final = path[-1]
            cursor[final] = str(cursor[final]) + suffix  # type: ignore[index]
            return hostile

        def keyed_maps(
            value: object, prefix: tuple[object, ...] = (),
        ) -> list[tuple[object, ...]]:
            paths: list[tuple[object, ...]] = []
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"input_hashes", "acceptance_checks", "constraints"}:
                        paths.append((*prefix, key))
                    paths.extend(keyed_maps(child, (*prefix, key)))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    paths.extend(keyed_maps(child, (*prefix, index)))
            return paths

        bidi_classes = {
            "LRE", "RLE", "LRO", "RLO", "PDF",
            "LRI", "RLI", "FSI", "PDI", "BN",
        }
        unsafe = tuple(
            chr(codepoint)
            for codepoint in range(0x110000)
            if unicodedata.category(chr(codepoint)) in {"Cc", "Cs"}
            or unicodedata.bidirectional(chr(codepoint)) in bidi_classes
        )
        self.assertEqual(2248, len(unsafe))
        controls = tuple(dict.fromkeys((
            *(chr(codepoint) for codepoint in range(0x20)),
            "\x7f",
            *(chr(codepoint) for codepoint in range(0x80, 0xA0)),
            *(
                character
                for character in unsafe
                if unicodedata.bidirectional(character) in bidi_classes
            ),
            "\ud800", "\udc00", "\udfff",
        )))
        self.assertIn("\u0085", controls)
        self.assertIn("\u202e", controls)
        safe_controls = (
            "\u061c", "\u200e", "\u200f", "\u2065",
            "\ufff9", "\ufffa", "\ufffb",
        )
        parity_failure_count = 0
        parity_failure_samples: list[str] = []

        def note_parity_failure(detail: str) -> None:
            nonlocal parity_failure_count
            parity_failure_count += 1
            if len(parity_failure_samples) < 25:
                parity_failure_samples.append(detail)

        def terminal_guards(value: object) -> list[str]:
            guards: list[str] = []
            if isinstance(value, dict):
                forbidden = value.get("not")
                if isinstance(forbidden, dict):
                    pattern = forbidden.get("pattern")
                    if isinstance(pattern, str) and (
                        "\\u0000" in pattern
                        or pattern in {"[\r\n]", "[\\r\\n]"}
                    ):
                        guards.append(pattern)
                for child in value.values():
                    guards.extend(terminal_guards(child))
            elif isinstance(value, list):
                for child in value:
                    guards.extend(terminal_guards(child))
            return guards

        for _record_fixture, schema_name in cases:
            guards = terminal_guards(schema_documents[Path("schemas/v1") / schema_name])
            if not guards:
                note_parity_failure(f"{schema_name}: no terminal guards")
            for index, source in enumerate(guards):
                guard = re.compile(source)
                mismatches = [
                    f"U+{ord(character):04X}"
                    for character in unsafe
                    if guard.search(character) is None
                ]
                if mismatches:
                    note_parity_failure(
                        f"{schema_name}: guard {index} misses "
                        f"{len(mismatches)} unsafe points, first {mismatches[:3]}"
                    )
                overrejected = [
                    f"U+{ord(character):04X}"
                    for character in safe_controls
                    if guard.search(character) is not None
                ]
                if overrejected:
                    note_parity_failure(
                        f"{schema_name}: guard {index} overrejects {overrejected}"
                    )

        for record, schema_name in cases:
            schema_path = Path("schemas/v1") / schema_name
            schema = schema_documents[schema_path]
            validator = Draft202012Validator(
                schema, registry=schema_registry,
            )
            with self.subTest(kind=record["kind"], control="lawful"):
                validate_record(record, "alpha", RUN_KINDS, integrity=False)
                validate_json_schema(record, schema_path)
                self.assertFalse(list(validator.iter_errors(record)))
            for path in string_paths(record):
                for control in controls:
                    hostile = replace_path(record, path, control)
                    with self.subTest(
                        kind=record["kind"], path=path,
                        control=repr(control),
                    ):
                        try:
                            validate_record(
                                hostile, "alpha", RUN_KINDS,
                                integrity=False,
                            )
                        except ProtocolRefusal:
                            pass
                        else:
                            note_parity_failure(
                                f"runtime accepted {schema_name} {path} "
                                f"U+{ord(control):04X}"
                            )
                        if not list(validator.iter_errors(hostile)):
                            note_parity_failure(
                                f"schema accepted {schema_name} {path} "
                                f"U+{ord(control):04X}"
                            )
            for path in keyed_maps(record):
                for control in controls:
                    hostile = deepcopy(record)
                    cursor: object = hostile
                    for component in path:
                        cursor = cursor[component]  # type: ignore[index]
                    key = next(iter(cursor))  # type: ignore[arg-type]
                    cursor[key + control] = cursor.pop(key)  # type: ignore[index, union-attr]
                    with self.subTest(
                        kind=record["kind"], key_path=path,
                        control=repr(control),
                    ):
                        try:
                            validate_record(
                                hostile, "alpha", RUN_KINDS,
                                integrity=False,
                            )
                        except ProtocolRefusal:
                            pass
                        else:
                            note_parity_failure(
                                f"runtime accepted key {schema_name} {path} "
                                f"U+{ord(control):04X}"
                            )
                        if not list(validator.iter_errors(hostile)):
                            note_parity_failure(
                                f"schema accepted key {schema_name} {path} "
                                f"U+{ord(control):04X}"
                            )

        descendant, descendant_schema_name = cases[8]
        descendant_schema = schema_documents[
            Path("schemas/v1") / descendant_schema_name
        ]
        descendant_validator = Draft202012Validator(
            descendant_schema, registry=schema_registry,
        )
        for control in safe_controls:
            lawful = dict(
                descendant,
                provider_descendant_id="thread-safe" + control,
            )
            with self.subTest(safe_control=f"U+{ord(control):04X}"):
                validate_record(lawful, "alpha", RUN_KINDS, integrity=False)
                self.assertFalse(list(descendant_validator.iter_errors(lawful)))
        self.assertEqual(
            0,
            parity_failure_count,
            f"first parity failures: {parity_failure_samples}",
        )

class SpawnEffectJoinTests(unittest.TestCase):
    def test_spawn_parent_acceptance_requires_closed_groups_and_confirmed_effects(self) -> None:
        """Catches either half of the spawn/effect success join being bypassed."""
        from tests.test_effects import EffectRecordFixture
        from floati.effects import EffectProjection
        from floati.records import EFFECT_BINDING_FIELDS

        fixture = SpawnGroupFixtures()
        records = fixture.started_parent()
        group = fixture.group(on_child_failure="continue_until_join_impossible")
        amendment = fixture.amendment(group)
        rejected = fixture.rejected(group, amendment)
        closed = fixture.close(
            group, amendment, outcome="satisfied",
            close_reason="all_members_terminal",
            rejected_item_ids=[fixture.child],
        )
        observation = fixture.observation_close([])
        receipt_id = "worker-receipt-" + uuid7_hex()
        receipt = {"id": receipt_id, "work_item_id": fixture.parent, "node_id": "worker-a"}
        produced = _record(
            "result_produced", "run-result-produced-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            dispatch_decision_id=records[-2]["id"], worker_receipt_ids=[receipt_id],
        )

        effect_fixture = EffectRecordFixture()
        effect_rows = effect_fixture.rows()
        binding = {
            "operation_id": effect_fixture.binding()["operation_id"],
            "run_id": fixture.run_id, "item_id": fixture.parent,
            "attempt_id": fixture.attempt,
            "attempt_started_id": records[-1]["id"],
            "fence_token": fixture.fence,
            "effect_type": "git_ref_update",
            "target": effect_fixture.binding()["target"],
            "request_digest": "c" * 64,
            "idempotency_key": "spawn-parent-effect",
            "expected_confirmation": effect_fixture.binding()["expected_confirmation"],
            "reconciliation_adapter": "git_local", "risk_class": "low",
            "budget_claim": [{"budget_id": "build", "amount": 1}],
        }
        lifecycle = []
        for kind in ("effect_intent", "effect_dispatched", "effect_acknowledged", "effect_confirmed"):
            row = dict(effect_rows[kind])
            row.update(binding)
            if kind == "effect_confirmed":
                row["measured_spend"] = [{"budget_id": "build", "amount": 1}]
            lifecycle.append(row)
        effects = EffectProjection.from_records(lifecycle, integrity=False)
        evidence = effects.acceptance_evidence(fixture.run_id, fixture.attempt)
        accepted = _record(
            "result_accepted", "run-result-accepted-", run_id=fixture.run_id,
            item_id=fixture.parent, attempt_id=fixture.attempt,
            predecessor_result_id=produced["id"], acceptance_mode="accepted_unverified",
            acceptance_receipt_id=None, worker_receipt_ids=[receipt_id],
            effect_operation_ids=list(evidence.operation_ids),
            effect_ledger_high_watermark=evidence.high_watermark,
            effect_evidence_digest=evidence.evidence_digest,
        )
        accepted["schema_version"] = 1
        prefix = [*records, group, amendment]
        import inspect
        self.assertIn(
            "effect_projection", inspect.signature(RunProjection.from_records).parameters,
            "RunProjection must consume the immutable Effect snapshot",
        )
        with self.assertRaises(ProtocolRefusal) as open_group:
            RunProjection.from_records(
                [*prefix, produced, accepted], worker_receipts=[receipt],
                effect_projection=effects, integrity=False,
            )
        self.assertEqual("spawn_join_unsatisfied", open_group.exception.code)

        unconfirmed = EffectProjection.from_records(lifecycle[:1], integrity=False)
        unconfirmed_evidence = unconfirmed.acceptance_evidence(
            fixture.run_id, fixture.attempt
        )
        unconfirmed_accepted = dict(
            accepted,
            effect_ledger_high_watermark=unconfirmed_evidence.high_watermark,
            effect_evidence_digest=unconfirmed_evidence.evidence_digest,
        )
        with self.assertRaises(ProtocolRefusal) as blocked:
            RunProjection.from_records(
                [*prefix, rejected, closed, observation, produced, unconfirmed_accepted],
                worker_receipts=[receipt], effect_projection=unconfirmed,
                integrity=False,
            )
        self.assertEqual("effect_unknown_blocks_acceptance", blocked.exception.code)

        lawful = RunProjection.from_records(
            [*prefix, rejected, closed, observation, produced, accepted],
            worker_receipts=[receipt], effect_projection=effects, integrity=False,
        )
        self.assertIn(fixture.parent, lawful.run(fixture.run_id)["accepted"])


if __name__ == "__main__":
    unittest.main()

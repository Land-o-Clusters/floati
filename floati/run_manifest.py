"""Attempt-path observations and closed derived run-manifest facts."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional

from . import __version__
from .errors import ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .records import _SPECS, validate_record
from .root import FloatiRoot


__all__ = ("RunManifestStore", "validate_run_manifest_fact")

RUN_MANIFEST_FACT_KINDS = frozenset({"run_manifest_fact"})
RUN_MANIFEST_FACT_FIELDS = _SPECS["run_manifest_fact"][1]
RUN_ENVIRONMENT_KINDS = frozenset({"run_environment_observed"})


def validate_run_manifest_fact(
    fact: Mapping[str, object], expected_tenant: str
) -> Dict[str, object]:
    """Validate one complete derived fact without writing or projecting it."""

    if type(fact) is not dict or set(fact) != RUN_MANIFEST_FACT_FIELDS:
        raise ProtocolRefusal(
            "run_manifest_fields_invalid",
            "run manifest fact requires the exact closed schema-v1 field set",
        )
    return validate_record(
        dict(fact),
        expected_tenant,
        RUN_MANIFEST_FACT_KINDS,
        integrity=False,
    )


def _timestamp(value: Optional[datetime]) -> str:
    current = datetime.now(timezone.utc) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ProtocolRefusal("time_invalid", "run observation requires an aware datetime")
    return current.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _toolchain_fingerprint() -> str:
    """Hash the declared local toolchain set: Python implementation/version + Floati."""

    payload = {
        "floati": __version__,
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


def _workspace_commit(workspace: object) -> Optional[str]:
    if workspace is None:
        return None
    path = Path(str(workspace)).expanduser()
    try:
        result = subprocess.run(
            fixed_git_command("/usr/bin/git", path, ("rev-parse", "HEAD")),
            env=fixed_git_environment("/usr/bin/git"), capture_output=True,
            text=True, check=False, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and len(value) == 40 else None


def _adapter_observation(adapter: object, field: str) -> Optional[str]:
    value = getattr(adapter, field, None)
    return value if isinstance(value, str) and value else None


class RunManifestStore:
    """Write worker observations, close facts, and list durable manifest facts."""

    observation_path = Path("runs/environment-observations.jsonl")
    manifest_path = Path("runs/manifests.jsonl")

    def __init__(
        self, root: FloatiRoot, *, projection_loader: Optional[Callable[[], object]] = None,
    ) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "run manifests require one Floati root")
        self.root = root
        self._projection_loader = projection_loader

    def observations(self) -> list[Dict[str, object]]:
        return read_records_snapshot(
            self.root, self.observation_path, allowed_kinds=RUN_ENVIRONMENT_KINDS,
        )

    def records(self) -> list[Dict[str, object]]:
        return read_records_snapshot(
            self.root, self.manifest_path, allowed_kinds=RUN_MANIFEST_FACT_KINDS,
        )

    def observe_environment(
        self, *, run_id: str, item_id: str, attempt_id: str, adapter: str,
        harness_version: Optional[str] = None,
        model_observed: Optional[str] = None,
        provider_observed: Optional[str] = None,
        workspace_base_commit: Optional[str] = None,
        toolchain_fingerprint: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        observations = {
            "harness_version": harness_version,
            "model_observed": model_observed,
            "provider_observed": provider_observed,
            "toolchain_fingerprint": toolchain_fingerprint,
            "workspace_base_commit": workspace_base_commit,
        }
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "run-environment-observed-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(now),
            "kind": "run_environment_observed",
            "run_id": run_id,
            "item_id": item_id,
            "attempt_id": attempt_id,
            "adapter": adapter,
            **observations,
            "unknown_fields": sorted(
                field for field, value in observations.items() if value is None
            ),
            "self_reported_fields": sorted(
                field for field, value in observations.items() if value is not None
            ),
        }
        record = validate_record(
            record, self.root.tenant_id, RUN_ENVIRONMENT_KINDS, integrity=False,
        )

        def decide(rows: list[Dict[str, object]]):
            existing = next(
                (row for row in rows if row["attempt_id"] == attempt_id), None
            )
            if existing is None:
                return record, record
            semantic = set(record) - {"id", "timestamp"}
            if any(existing[field] != record[field] for field in semantic):
                raise ProtocolRefusal(
                    "run_environment_observation_divergent",
                    "one attempt cannot change its observed run environment",
                )
            return existing, None

        return transact(
            self.root, self.observation_path, decide,
            allowed_kinds=RUN_ENVIRONMENT_KINDS,
        )

    def observe_worker_environment(
        self, *, run_id: str, item_id: str, attempt_id: str, adapter_name: str,
        adapter: object, workspace: object, now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        return self.observe_environment(
            run_id=run_id, item_id=item_id, attempt_id=attempt_id,
            adapter=adapter_name,
            harness_version=_adapter_observation(adapter, "harness_version"),
            model_observed=_adapter_observation(adapter, "model_observed"),
            provider_observed=_adapter_observation(adapter, "provider_observed"),
            workspace_base_commit=_workspace_commit(workspace),
            toolchain_fingerprint=_toolchain_fingerprint(), now=now,
        )

    def _projection(self) -> object:
        if self._projection_loader is not None:
            return self._projection_loader()
        from .runtruth import RunLedger

        return RunLedger(self.root).project()

    def derive_attempt(self, attempt_id: str) -> Optional[Dict[str, object]]:
        observation = next(
            (row for row in self.observations() if row["attempt_id"] == attempt_id),
            None,
        )
        projection = self._projection()
        selected = next(
            (
                run for run in projection._runs.values()
                if attempt_id in run["attempts"]
            ),
            None,
        )
        if selected is None:
            raise ProtocolRefusal(
                "run_manifest_attempt_missing",
                "observation does not bind an existing run attempt",
            )
        state = selected["attempts"][attempt_id]
        terminal = state["terminal"]
        if terminal is None:
            return None
        capability = selected["capability_sets"].get(attempt_id)
        dispatch = selected["dispatches"].get(attempt_id)
        item_id = state["opened"]["item_id"]
        contract = selected["contracts"].get(item_id)
        if (
            capability is None or dispatch is None or contract is None
            or (observation is None and "adapter" not in dispatch)
        ):
            return None
        observation_record_absent = observation is None
        if observation is None:
            observation = {
                "run_id": selected["run_id"],
                "item_id": item_id,
                "attempt_id": attempt_id,
                "adapter": dispatch["adapter"],
                "harness_version": None,
                "model_observed": None,
                "provider_observed": None,
                "workspace_base_commit": None,
                "toolchain_fingerprint": None,
                "unknown_fields": [
                    "harness_version", "model_observed", "provider_observed",
                    "toolchain_fingerprint", "workspace_base_commit",
                ],
                "self_reported_fields": [],
            }
        if (
            observation["run_id"] != selected["run_id"]
            or observation["item_id"] != state["opened"]["item_id"]
            or dispatch.get("adapter", observation["adapter"])
            != observation["adapter"]
        ):
            raise ProtocolRefusal(
                "run_manifest_binding_invalid",
                "observation does not match the terminal attempt binding",
            )
        admission = next((
            group["admissions"][observation["item_id"]]
            for group in selected["spawn_groups"].values()
            if observation["item_id"] in group["admissions"]
        ), None)
        consumption = state["approval_consumption"]
        approvals = [] if consumption is None else sorted({
            str(consumption["approval_request_id"]),
            str(consumption["approval_decision_id"]),
            str(consumption["id"]),
        })
        terminal_outcome = {
            "completed": "succeeded", "failed": "failed",
            "cancelled": "cancelled", "skipped": "skipped",
            "needs_operator": "needs_operator", "uncertain": "uncertain",
        }.get(str(terminal["terminal_state"]), "uncertain")
        observation_reason = (
            "run_environment_observation_record_absent"
            if observation_record_absent
            else "run_environment_observation_absent"
        )
        unknown_reasons = {
            str(field): observation_reason
            for field in observation["unknown_fields"]
        }
        unknown_reasons.update({
            "tool_set": "attempt_tool_set_source_absent",
            "verification_commands": "attempt_verification_command_source_absent",
            "operator_interventions": "attempt_operator_intervention_source_absent",
        })
        if admission is None:
            unknown_reasons["budget_allocation"] = (
                "attempt_budget_allocation_source_absent"
            )
        unknown_fields = sorted(unknown_reasons)
        fact: Dict[str, object] = {
            "schema_version": 1,
            "id": "run-manifest-" + attempt_id.removeprefix("attempt-"),
            "tenant_id": self.root.tenant_id,
            "timestamp": terminal["timestamp"],
            "kind": "run_manifest_fact",
            "attempt_id": attempt_id,
            "run_id": selected["run_id"],
            "item_id": observation["item_id"],
            "adapter": observation["adapter"],
            "harness_version": observation["harness_version"],
            "model_observed": observation["model_observed"],
            "provider_observed": observation["provider_observed"],
            "capability_set_bound_id": capability["id"],
            "task_contract_id": contract["task_contract_id"],
            "task_contract_digest": contract["contract_digest"],
            "policy_digest": dispatch["policy_digest"],
            "tool_set": None,
            "workspace_base_commit": observation["workspace_base_commit"],
            "toolchain_fingerprint": observation["toolchain_fingerprint"],
            "budget_allocation": None if admission is None else admission["budget_allocation"],
            "approvals_consumed": approvals,
            "verification_commands": None,
            "operator_interventions": None,
            "terminal_outcome": terminal_outcome,
            "unknown_fields": unknown_fields,
            "unknown_sources": [
                {"field": field, "reason": unknown_reasons[field]}
                for field in unknown_fields
            ],
            "self_reported_fields": observation["self_reported_fields"],
        }
        return validate_run_manifest_fact(fact, self.root.tenant_id)

    def close_attempt(self, attempt_id: str) -> Optional[Dict[str, object]]:
        fact = self.derive_attempt(attempt_id)
        if fact is None:
            return None

        def decide(rows: list[Dict[str, object]]):
            existing = next(
                (row for row in rows if row["attempt_id"] == attempt_id), None
            )
            if existing is None:
                return fact, fact
            if existing != fact:
                raise ProtocolRefusal(
                    "run_manifest_source_drift",
                    "stored manifest no longer equals its re-derived source fact",
                )
            return existing, None

        return transact(
            self.root, self.manifest_path, decide,
            allowed_kinds=RUN_MANIFEST_FACT_KINDS,
        )

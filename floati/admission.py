"""Pure, immutable preflight admission for explicit HM-3I plan inputs.

This module deliberately has no root, ledger, adapter, subprocess, network, or
wall-clock dependency.  It answers only whether an already supplied plan and
finite repository policy meet their declared hard limits.  Before analysis it
also rejects post-load public-object mutation when the current typed semantics
no longer equal the object's cached canonical bytes and digest.  That check is
not an origin, provenance, or durable-admission-authority claim.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .contracts import TaskContract
from .errors import ProtocolRefusal
from .policy import RepositoryPolicy, RoutingRule
from .policy import validate_repository_policy_integrity
from .records import run_admission_digest
from .root import IDENTIFIER_PATTERN
from .ids import uuid7_hex


MAX_ADMISSION_PLAN_BYTES = 64 * 1024
MAX_PLAN_WORKERS = 64
MAX_PLAN_ITEMS = 64
MAX_PLAN_RESERVATIONS = 64
MAX_PLAN_EDGES = 512
MAX_REQUESTED_ATTEMPTS = 64

_WORK_ID = re.compile(r"^work-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$")
_REASON_CATEGORIES = (
    "graph",
    "fan_out",
    "capability",
    "cancellation",
    "callback",
    "workspace",
    "concurrency",
    "retry",
    "budget",
    "operator",
    "merge",
)
_CATEGORY_INDEX = {category: index for index, category in enumerate(_REASON_CATEGORIES)}
_EFFECT_SAFETY = frozenset(("idempotent", "non_idempotent", "unknown_effect"))
_RETRY_CLASSES = frozenset(
    (
        "transient",
        "permanent",
        "operator_required",
        "policy_refusal",
        "cancelled",
        "unknown_effect",
    )
)
_EDGE_REQUIRES = frozenset(("produced", "verified", "accepted"))
_FAILURE_POLICIES = frozenset(("fail_run", "skip_dependent", "continue"))
_POLICY_LIMIT_BOUNDS = {
    "max_items": (1, 64),
    "max_depth": (1, 16),
    "max_fan_out": (1, 8),
    "max_active_attempts": (1, 8),
}
_POLICY_BUDGET_UNITS = frozenset(("attempts", "tokens", "milliseconds", "microcurrency"))
_POLICY_CANCEL_MODES = frozenset(("native", "local_process_only", "unavailable"))
_POLICY_RISK_CLASSES = ("low", "medium", "high", "critical")
_POLICY_MAX_NAMED_ENTRIES = 64
_POLICY_MAX_TEXT_LENGTH = 256
_POLICY_MAX_ARGV_ITEMS = 32


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or IDENTIFIER_PATTERN.fullmatch(value) is None:
        _refuse("admission_identifier_invalid", field + " must be a bounded lowercase identifier")
    return value


def _work_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _WORK_ID.fullmatch(value) is None:
        _refuse("admission_item_id_invalid", field + " must use the work-UUIDv7 domain")
    return value


def _bounded_integer(value: object, field: str, lower: int, upper: int) -> int:
    if not _is_integer(value) or value < lower or value > upper:
        _refuse("admission_integer_invalid", field + " must be a bounded integer")
    return int(value)


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        _refuse("admission_boolean_invalid", field + " must be a boolean")
    return value


def _exact_mapping(value: object, fields: Sequence[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != set(fields):
        _refuse("admission_fields_invalid", field + " must contain exactly " + ", ".join(fields))
    return value


def _bounded_list(value: object, field: str, lower: int, upper: int) -> List[object]:
    if not isinstance(value, list) or not lower <= len(value) <= upper:
        _refuse("admission_array_invalid", field + " must be a bounded array")
    return value


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _refuse("admission_duplicate_key", "plan objects must not repeat keys")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> object:
    _refuse("admission_json_invalid", value + " is not valid I-JSON")
    raise AssertionError("unreachable")


def _reject_raw_dot_components(raw_path: str) -> None:
    spelling = raw_path.replace(os.altsep, os.sep) if os.altsep else raw_path
    if any(component in (".", "..") for component in spelling.split(os.sep)):
        _refuse("admission_plan_path_invalid", "plan path must not contain lexical dot components")


def _reject_symlink_components(candidate: Path) -> None:
    current = Path(candidate.anchor)
    start = 1 if candidate.anchor else 0
    for part in candidate.parts[start:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            raise ProtocolRefusal("admission_plan_unreadable", "plan path cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            _refuse("admission_plan_symlinked", "plan and every lexical component must not be symlinked")


def _validate_plan_path(path: Union[Path, str]) -> Path:
    try:
        raw_path = os.fspath(path)
    except Exception as exc:
        raise ProtocolRefusal("admission_plan_path_invalid", "an explicit filesystem path is required") from exc
    if not isinstance(raw_path, str):
        _refuse("admission_plan_path_invalid", "plan path must be text, not bytes")
    if "\x00" in raw_path:
        _refuse("admission_plan_path_invalid", "plan path must not contain NUL")
    try:
        raw_path.encode("utf-8")
    except UnicodeError as exc:
        raise ProtocolRefusal("admission_plan_path_invalid", "plan path must be valid UTF-8 text") from exc
    _reject_raw_dot_components(raw_path)
    try:
        candidate = Path(raw_path)
    except Exception as exc:
        raise ProtocolRefusal("admission_plan_path_invalid", "an explicit filesystem path is required") from exc
    if not candidate.is_absolute():
        _refuse("admission_plan_path_not_absolute", "plan path must be absolute")
    if any(part in (".", "..") for part in candidate.parts):
        _refuse("admission_plan_path_invalid", "plan path must not contain lexical dot components")
    _reject_symlink_components(candidate)
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError as exc:
        raise ProtocolRefusal("admission_plan_missing", "the explicit plan does not exist") from exc
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal("admission_plan_unreadable", "the explicit plan cannot be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode):
        _refuse("admission_plan_not_regular", "plan must be a regular file")
    return candidate


def _load_plan_object(path: Union[Path, str]) -> Mapping[str, Any]:
    candidate = _validate_plan_path(path)
    try:
        data = candidate.read_bytes()
    except FileNotFoundError as exc:
        raise ProtocolRefusal("admission_plan_missing", "the explicit plan does not exist") from exc
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal("admission_plan_unreadable", "the explicit plan cannot be read") from exc
    if len(data) > MAX_ADMISSION_PLAN_BYTES:
        _refuse("admission_plan_oversize", "plan exceeds the bounded v0 byte limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolRefusal("admission_plan_not_utf8", "plan must be valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite_constant,
        )
    except ProtocolRefusal:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal("admission_plan_json_invalid", "plan must be one bounded JSON object") from exc
    if not isinstance(raw, dict):
        _refuse("admission_plan_json_invalid", "plan must be one JSON object")
    return raw


def _compact_ijson(value: Mapping[str, object], code: str) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(code, "value cannot form canonical I-JSON") from exc


@dataclass(frozen=True)
class AdmissionWorker:
    """One concrete prospective node and immutable Item 7 profile."""

    node_id: str
    worker_profile: str

    def canonical(self) -> Dict[str, str]:
        return {"node_id": self.node_id, "worker_profile": self.worker_profile}


@dataclass(frozen=True)
class BudgetReservation:
    """One explicit hard allocation in a named policy budget."""

    budget_id: str
    amount: int

    def canonical(self) -> Dict[str, object]:
        return {"budget_id": self.budget_id, "amount": self.amount}


@dataclass(frozen=True)
class AdmissionItem:
    """A validated immutable task-contract attachment for admission only."""

    item_id: str
    contract: TaskContract
    capability_selector: str
    requires_cancellation: bool
    requires_callback: bool
    workspace_key: str
    concurrency_key: str
    retry_class: str
    effect_safety: str
    merge_gate: Optional[str]

    def canonical(self) -> Dict[str, object]:
        return {
            "item_id": self.item_id,
            "contract": self.contract.canonical(),
            "capability_selector": self.capability_selector,
            "requires_cancellation": self.requires_cancellation,
            "requires_callback": self.requires_callback,
            "workspace_key": self.workspace_key,
            "concurrency_key": self.concurrency_key,
            "retry_class": self.retry_class,
            "effect_safety": self.effect_safety,
            "merge_gate": self.merge_gate,
        }


@dataclass(frozen=True)
class AdmissionDependency:
    """A pure plan edge; it does not create a runtime dependency object."""

    source: str
    target: str
    requires: str
    failure_policy: str

    def canonical(self) -> Dict[str, str]:
        return {
            "source": self.source,
            "target": self.target,
            "requires": self.requires,
            "failure_policy": self.failure_policy,
        }


@dataclass(frozen=True, init=False)
class AdmissionPlan:
    """One strict explicit plan and its canonical immutable semantic bytes."""

    schema_version: int
    workers: Tuple[AdmissionWorker, ...]
    max_active_attempts: int
    budget_reservations: Tuple[BudgetReservation, ...]
    items: Tuple[AdmissionItem, ...]
    dependency_edges: Tuple[AdmissionDependency, ...]
    _canonical: bytes = field(repr=False)
    _digest: str = field(repr=False)

    @classmethod
    def load(cls, path: Union[Path, str]) -> "AdmissionPlan":
        raw = _load_plan_object(path)
        return cls.from_canonical(raw)

    @classmethod
    def from_canonical(cls, raw: object) -> "AdmissionPlan":
        """Revalidate one in-memory canonical plan at a typed service boundary."""

        root = _exact_mapping(
            raw,
            (
                "schema_version",
                "workers",
                "max_active_attempts",
                "budget_reservations",
                "items",
                "dependency_edges",
            ),
            "plan root",
        )
        schema_version = _bounded_integer(root["schema_version"], "schema_version", 0, 0)

        worker_values = _bounded_list(root["workers"], "workers", 1, MAX_PLAN_WORKERS)
        workers: List[AdmissionWorker] = []
        for index, raw_worker in enumerate(worker_values):
            worker = _exact_mapping(raw_worker, ("node_id", "worker_profile"), "workers[" + str(index) + "]")
            workers.append(
                AdmissionWorker(
                    _identifier(worker["node_id"], "workers.node_id"),
                    _identifier(worker["worker_profile"], "workers.worker_profile"),
                )
            )
        worker_keys = [worker.node_id for worker in workers]
        if worker_keys != sorted(worker_keys) or len(set(worker_keys)) != len(worker_keys):
            _refuse("admission_workers_order_invalid", "workers must be lexical-sorted and unique by node_id")

        max_active_attempts = _bounded_integer(
            root["max_active_attempts"], "max_active_attempts", 1, MAX_REQUESTED_ATTEMPTS
        )

        reservation_values = _bounded_list(
            root["budget_reservations"], "budget_reservations", 0, MAX_PLAN_RESERVATIONS
        )
        reservations: List[BudgetReservation] = []
        for index, raw_reservation in enumerate(reservation_values):
            reservation = _exact_mapping(
                raw_reservation, ("budget_id", "amount"), "budget_reservations[" + str(index) + "]"
            )
            reservations.append(
                BudgetReservation(
                    _identifier(reservation["budget_id"], "budget_reservations.budget_id"),
                    _bounded_integer(reservation["amount"], "budget_reservations.amount", 1, 1_000_000_000),
                )
            )
        reservation_keys = [reservation.budget_id for reservation in reservations]
        if reservation_keys != sorted(reservation_keys) or len(set(reservation_keys)) != len(reservation_keys):
            _refuse("admission_budget_order_invalid", "budget_reservations must be lexical-sorted and unique")

        item_values = _bounded_list(root["items"], "items", 1, MAX_PLAN_ITEMS)
        items: List[AdmissionItem] = []
        contract_fields = (
            "objective",
            "non_goals",
            "areas_to_avoid",
            "input_hashes",
            "acceptance_checks",
            "constraints",
            "risk_class",
            "retry_policy",
            "dependencies",
        )
        for index, raw_item in enumerate(item_values):
            item = _exact_mapping(
                raw_item,
                (
                    "item_id",
                    "contract",
                    "capability_selector",
                    "requires_cancellation",
                    "requires_callback",
                    "workspace_key",
                    "concurrency_key",
                    "retry_class",
                    "effect_safety",
                    "merge_gate",
                ),
                "items[" + str(index) + "]",
            )
            contract_raw = _exact_mapping(item["contract"], contract_fields, "items.contract")
            try:
                contract = TaskContract.create(**dict(contract_raw))
            except ProtocolRefusal:
                raise
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                raise ProtocolRefusal("admission_contract_invalid", "item contract is not a valid TaskContract") from exc
            effect_safety = item["effect_safety"]
            if not isinstance(effect_safety, str) or effect_safety not in _EFFECT_SAFETY:
                _refuse("admission_effect_safety_invalid", "effect_safety is outside the frozen vocabulary")
            merge_gate = item["merge_gate"]
            if merge_gate is not None:
                merge_gate = _identifier(merge_gate, "items.merge_gate")
            items.append(
                AdmissionItem(
                    _work_id(item["item_id"], "items.item_id"),
                    contract,
                    _identifier(item["capability_selector"], "items.capability_selector"),
                    _bool(item["requires_cancellation"], "items.requires_cancellation"),
                    _bool(item["requires_callback"], "items.requires_callback"),
                    _identifier(item["workspace_key"], "items.workspace_key"),
                    _identifier(item["concurrency_key"], "items.concurrency_key"),
                    _identifier(item["retry_class"], "items.retry_class"),
                    effect_safety,
                    merge_gate,
                )
            )
        item_keys = [item.item_id for item in items]
        if item_keys != sorted(item_keys):
            _refuse("admission_items_order_invalid", "items must be in canonical item_id order")

        edge_values = _bounded_list(root["dependency_edges"], "dependency_edges", 0, MAX_PLAN_EDGES)
        edges: List[AdmissionDependency] = []
        for index, raw_edge in enumerate(edge_values):
            edge = _exact_mapping(
                raw_edge,
                ("source", "target", "requires", "failure_policy"),
                "dependency_edges[" + str(index) + "]",
            )
            requires = edge["requires"]
            failure_policy = edge["failure_policy"]
            if not isinstance(requires, str) or requires not in _EDGE_REQUIRES:
                _refuse("admission_edge_requires_invalid", "dependency requires is outside the frozen vocabulary")
            if not isinstance(failure_policy, str) or failure_policy not in _FAILURE_POLICIES:
                _refuse("admission_edge_failure_policy_invalid", "dependency failure_policy is outside the frozen vocabulary")
            edges.append(
                AdmissionDependency(
                    _work_id(edge["source"], "dependency_edges.source"),
                    _work_id(edge["target"], "dependency_edges.target"),
                    requires,
                    failure_policy,
                )
            )
        edge_keys = [(edge.source, edge.target, edge.requires, edge.failure_policy) for edge in edges]
        if edge_keys != sorted(edge_keys):
            _refuse("admission_edges_order_invalid", "dependency_edges must be in canonical edge order")

        return cls._create(
            schema_version,
            tuple(workers),
            max_active_attempts,
            tuple(reservations),
            tuple(items),
            tuple(edges),
        )

    @classmethod
    def _create(
        cls,
        schema_version: int,
        workers: Tuple[AdmissionWorker, ...],
        max_active_attempts: int,
        budget_reservations: Tuple[BudgetReservation, ...],
        items: Tuple[AdmissionItem, ...],
        dependency_edges: Tuple[AdmissionDependency, ...],
    ) -> "AdmissionPlan":
        semantic = {
            "schema_version": schema_version,
            "workers": [worker.canonical() for worker in workers],
            "max_active_attempts": max_active_attempts,
            "budget_reservations": [reservation.canonical() for reservation in budget_reservations],
            "items": [item.canonical() for item in items],
            "dependency_edges": [edge.canonical() for edge in dependency_edges],
        }
        canonical = _compact_ijson(semantic, "admission_plan_canonical_invalid")
        instance = object.__new__(cls)
        object.__setattr__(instance, "schema_version", schema_version)
        object.__setattr__(instance, "workers", workers)
        object.__setattr__(instance, "max_active_attempts", max_active_attempts)
        object.__setattr__(instance, "budget_reservations", budget_reservations)
        object.__setattr__(instance, "items", items)
        object.__setattr__(instance, "dependency_edges", dependency_edges)
        object.__setattr__(instance, "_canonical", canonical)
        object.__setattr__(instance, "_digest", hashlib.sha256(canonical).hexdigest())
        return instance

    def canonical_bytes(self) -> bytes:
        """Return the exact semantic bytes covered by ``digest``."""

        return self._canonical

    @property
    def digest(self) -> str:
        return self._digest

    def canonical(self) -> Dict[str, object]:
        """Return a fresh plain semantic projection without exposing mutable internals."""

        return json.loads(self._canonical.decode("utf-8"))


@dataclass(frozen=True)
class AdmissionReason:
    """One ordered, machine-only hard-limit or external-gate finding."""

    category: str
    code: str
    subject: Optional[str] = None
    limit: Optional[int] = None
    actual: Optional[int] = None

    def __post_init__(self) -> None:
        if not isinstance(self.category, str) or self.category not in _CATEGORY_INDEX:
            _refuse("admission_reason_invalid", "reason category is outside the frozen admission order")
        _identifier(self.code, "reason.code")
        if self.subject is not None:
            _identifier(self.subject, "reason.subject")
        for field_name, value in (("limit", self.limit), ("actual", self.actual)):
            if value is not None and (not _is_integer(value) or value < 0):
                _refuse("admission_reason_invalid", field_name + " must be a nonnegative integer when present")

    def machine(self) -> Dict[str, object]:
        value: Dict[str, object] = {"category": self.category, "code": self.code}
        if self.subject is not None:
            value["subject"] = self.subject
        if self.limit is not None:
            value["limit"] = self.limit
        if self.actual is not None:
            value["actual"] = self.actual
        return value

    def order_key(self) -> Tuple[int, str, str, str]:
        remaining = {key: value for key, value in self.machine().items() if key not in {"category", "code", "subject"}}
        return (
            _CATEGORY_INDEX[self.category],
            self.code,
            "" if self.subject is None else self.subject,
            json.dumps(remaining, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )


@dataclass(frozen=True)
class AdmissionArtifact:
    """The timestamp-free, non-authoritative result of pure plan analysis."""

    outcome: str
    plan_digest: str
    policy_digest: str
    reasons: Tuple[AdmissionReason, ...] = ()
    admission_schema_version: int = 0
    kind: str = "plan_admission"

    def __post_init__(self) -> None:
        if (
            not _is_integer(self.admission_schema_version)
            or self.admission_schema_version != 0
            or not isinstance(self.kind, str)
            or self.kind != "plan_admission"
        ):
            _refuse("admission_artifact_invalid", "artifact must use the exact version-zero plan_admission contract")
        if not isinstance(self.outcome, str) or self.outcome not in {"admitted", "refused", "needs_operator"}:
            _refuse("admission_artifact_invalid", "artifact outcome is outside the frozen admission vocabulary")
        if not isinstance(self.plan_digest, str) or re.fullmatch(r"[0-9a-f]{64}", self.plan_digest) is None:
            _refuse("admission_artifact_invalid", "artifact plan_digest must be lowercase SHA-256")
        if not isinstance(self.policy_digest, str) or re.fullmatch(r"[0-9a-f]{64}", self.policy_digest) is None:
            _refuse("admission_artifact_invalid", "artifact policy_digest must be lowercase SHA-256")
        if not isinstance(self.reasons, tuple) or any(not isinstance(reason, AdmissionReason) for reason in self.reasons):
            _refuse("admission_artifact_invalid", "artifact reasons must be immutable admission reasons")
        for reason in self.reasons:
            reason.__post_init__()

    def machine(self) -> Dict[str, object]:
        return {
            "admission_schema_version": self.admission_schema_version,
            "kind": self.kind,
            "outcome": self.outcome,
            "plan_digest": self.plan_digest,
            "policy_digest": self.policy_digest,
            "reasons": [reason.machine() for reason in self.reasons],
        }

    def canonical_bytes(self) -> bytes:
        return _compact_ijson(self.machine(), "admission_artifact_invalid")


def _validate_plan_semantic_cache(plan: AdmissionPlan) -> None:
    """Reject post-load field changes that no longer match the bound plan bytes."""

    if (
        not isinstance(plan.workers, tuple)
        or any(not isinstance(worker, AdmissionWorker) for worker in plan.workers)
        or not isinstance(plan.budget_reservations, tuple)
        or any(
            not isinstance(reservation, BudgetReservation)
            for reservation in plan.budget_reservations
        )
        or not isinstance(plan.items, tuple)
        or any(not isinstance(item, AdmissionItem) for item in plan.items)
        or not isinstance(plan.dependency_edges, tuple)
        or any(
            not isinstance(edge, AdmissionDependency)
            for edge in plan.dependency_edges
        )
    ):
        _refuse(
            "admission_plan_integrity_invalid",
            "plan fields must retain their loaded immutable value types",
        )
    try:
        semantic = {
            "schema_version": plan.schema_version,
            "workers": [worker.canonical() for worker in plan.workers],
            "max_active_attempts": plan.max_active_attempts,
            "budget_reservations": [
                reservation.canonical() for reservation in plan.budget_reservations
            ],
            "items": [item.canonical() for item in plan.items],
            "dependency_edges": [edge.canonical() for edge in plan.dependency_edges],
        }
        current = _compact_ijson(semantic, "admission_plan_integrity_invalid")
        cached = plan.canonical_bytes()
        digest = plan.digest
    except ProtocolRefusal:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal(
            "admission_plan_integrity_invalid",
            "plan fields cannot be rederived as canonical semantics",
        ) from exc
    if (
        not isinstance(cached, bytes)
        or not isinstance(digest, str)
        or current != cached
        or hashlib.sha256(current).hexdigest() != digest
    ):
        _refuse(
            "admission_plan_integrity_invalid",
            "plan fields diverge from their cached canonical bytes or digest",
        )


def _validate_policy_semantic_cache(policy: RepositoryPolicy) -> Tuple[RoutingRule, ...]:
    """Recheck current policy semantics and return routing derived from them."""

    try:
        semantic = policy.canonical()
        current = _compact_ijson(semantic, "admission_policy_integrity_invalid")
        cached = policy.canonical_bytes
        digest = policy.digest
        derived_routes = tuple(
            sorted(
                policy.routing.values(),
                key=lambda route: (route.rank, route.route_id),
            )
        )
    except ProtocolRefusal:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal(
            "admission_policy_integrity_invalid",
            "policy fields cannot be rederived as canonical semantics",
        ) from exc
    if (
        not isinstance(cached, bytes)
        or not isinstance(digest, str)
        or current != cached
        or hashlib.sha256(current).hexdigest() != digest
        or not isinstance(policy.routes, tuple)
        or policy.routes != derived_routes
    ):
        _refuse(
            "admission_policy_integrity_invalid",
            "policy fields or derived routes diverge from their cached semantics",
        )
    return derived_routes


def _append_reason(
    reasons: List[AdmissionReason],
    category: str,
    code: str,
    subject: Optional[str] = None,
    *,
    limit: Optional[int] = None,
    actual: Optional[int] = None,
) -> None:
    reasons.append(AdmissionReason(category, code, subject, limit, actual))


def _unique_sorted_reasons(reasons: Iterable[AdmissionReason]) -> Tuple[AdmissionReason, ...]:
    unique: Dict[Tuple[int, str, str, str], AdmissionReason] = {}
    for reason in reasons:
        unique[reason.order_key()] = reason
    return tuple(unique[key] for key in sorted(unique))


def _graph_adjacency(plan: AdmissionPlan) -> Dict[str, Tuple[str, ...]]:
    members = {item.item_id for item in plan.items}
    adjacency: Dict[str, List[str]] = {item_id: [] for item_id in members}
    for edge in plan.dependency_edges:
        if edge.source in members and edge.target in members and edge.source != edge.target:
            adjacency[edge.source].append(edge.target)
    return {source: tuple(sorted(set(targets))) for source, targets in adjacency.items()}


def _has_path(adjacency: Mapping[str, Sequence[str]], source: str, target: str) -> bool:
    pending = list(adjacency.get(source, ()))
    seen = set()
    while pending:
        current = pending.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        pending.extend(adjacency.get(current, ()))
    return False


def _graph_reasons(plan: AdmissionPlan, policy: RepositoryPolicy) -> Tuple[List[AdmissionReason], Dict[str, Tuple[str, ...]]]:
    reasons: List[AdmissionReason] = []
    item_ids = [item.item_id for item in plan.items]
    members = set(item_ids)
    if len(item_ids) > policy.limits["max_items"]:
        _append_reason(
            reasons,
            "graph",
            "item_limit_exceeded",
            "items",
            limit=policy.limits["max_items"],
            actual=len(item_ids),
        )
    for item_id in sorted(set(item_ids)):
        if item_ids.count(item_id) > 1:
            _append_reason(reasons, "graph", "item_duplicate", item_id, actual=item_ids.count(item_id))
        if _WORK_ID.fullmatch(item_id) is None:
            _append_reason(reasons, "graph", "item_id_invalid", "items")

    edge_keys = set()
    valid_edges: List[AdmissionDependency] = []
    for edge in plan.dependency_edges:
        key = (edge.source, edge.target, edge.requires, edge.failure_policy)
        if key in edge_keys:
            _append_reason(reasons, "graph", "edge_duplicate", edge.source)
        edge_keys.add(key)
        if edge.source == edge.target:
            _append_reason(reasons, "graph", "edge_self", edge.source)
            continue
        missing = False
        if edge.source not in members:
            _append_reason(reasons, "graph", "edge_source_unknown", edge.source)
            missing = True
        if edge.target not in members:
            _append_reason(reasons, "graph", "edge_target_unknown", edge.target)
            missing = True
        if not missing:
            valid_edges.append(edge)

    incoming: Dict[str, List[str]] = {item_id: [] for item_id in members}
    for edge in valid_edges:
        incoming[edge.target].append(edge.source)
    for item in plan.items:
        expected = tuple(sorted(incoming.get(item.item_id, ())))
        if item.contract.dependencies != expected:
            _append_reason(reasons, "graph", "contract_dependencies_mismatch", item.item_id)

    adjacency = _graph_adjacency(plan)
    indegree = {item_id: 0 for item_id in members}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    ready = sorted(item_id for item_id, degree in indegree.items() if degree == 0)
    depth = {item_id: 1 for item_id in ready}
    visited = 0
    while ready:
        source = ready.pop(0)
        visited += 1
        for target in adjacency.get(source, ()):
            depth[target] = max(depth.get(target, 1), depth[source] + 1)
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    if visited != len(members):
        cycle_subject = min(members) if members else "items"
        _append_reason(reasons, "graph", "cycle", cycle_subject)
    elif depth and max(depth.values()) > policy.limits["max_depth"]:
        _append_reason(
            reasons,
            "graph",
            "depth_limit_exceeded",
            "depth",
            limit=policy.limits["max_depth"],
            actual=max(depth.values()),
        )
    return reasons, adjacency


def _eligible_workers(
    item: AdmissionItem,
    plan: AdmissionPlan,
    policy: RepositoryPolicy,
    routes: Sequence[RoutingRule],
) -> Tuple[AdmissionWorker, ...]:
    selector = policy.capability_selectors.get(item.capability_selector)
    if selector is None:
        return ()
    eligible: List[AdmissionWorker] = []
    for worker in plan.workers:
        profile = policy.worker_profiles.get(worker.worker_profile)
        if profile is None or not set(selector.all_of).issubset(profile.capabilities):
            continue
        if any(
            route.worker_profile == worker.worker_profile
            and route.capability_selector == item.capability_selector
            for route in routes
        ):
            eligible.append(worker)
    return tuple(eligible)


class AdmissionEvaluator:
    """Total deterministic analysis over one loaded plan and one loaded policy."""

    @staticmethod
    def evaluate(plan: AdmissionPlan, policy: RepositoryPolicy) -> AdmissionArtifact:
        if not isinstance(plan, AdmissionPlan):
            _refuse("admission_plan_required", "evaluation requires a loaded AdmissionPlan")
        if not isinstance(policy, RepositoryPolicy):
            _refuse("admission_policy_required", "evaluation requires a loaded RepositoryPolicy")

        _validate_plan_semantic_cache(plan)
        routes = _validate_policy_semantic_cache(policy)

        reasons, adjacency = _graph_reasons(plan, policy)

        outgoing: Dict[str, int] = {}
        for edge in plan.dependency_edges:
            outgoing[edge.source] = outgoing.get(edge.source, 0) + 1
        for source, count in sorted(outgoing.items()):
            if count > policy.limits["max_fan_out"]:
                _append_reason(
                    reasons,
                    "fan_out",
                    "fan_out_limit_exceeded",
                    source,
                    limit=policy.limits["max_fan_out"],
                    actual=count,
                )

        for profile_id in sorted({worker.worker_profile for worker in plan.workers}):
            if profile_id not in policy.worker_profiles:
                _append_reason(reasons, "capability", "worker_profile_unknown", profile_id)
        eligible: Dict[str, Tuple[AdmissionWorker, ...]] = {}
        for item in plan.items:
            if item.capability_selector not in policy.capability_selectors:
                _append_reason(reasons, "capability", "selector_unknown", item.capability_selector)
                eligible[item.item_id] = ()
                continue
            matching = _eligible_workers(item, plan, policy, routes)
            eligible[item.item_id] = matching
            if not matching:
                _append_reason(reasons, "capability", "coverage_missing", item.item_id)
                continue
            if item.requires_cancellation and not any(
                policy.worker_profiles[worker.worker_profile].cancel_mode != "unavailable"
                for worker in matching
            ):
                _append_reason(reasons, "cancellation", "cancellation_unavailable", item.item_id)
            if item.requires_callback and not any(
                policy.worker_profiles[worker.worker_profile].callback_support
                for worker in matching
            ):
                _append_reason(reasons, "callback", "callback_unavailable", item.item_id)

        for category, key_name, code in (
            ("workspace", "workspace_key", "workspace_order_missing"),
            ("concurrency", "concurrency_key", "concurrency_order_missing"),
        ):
            groups: Dict[str, List[AdmissionItem]] = {}
            for item in plan.items:
                groups.setdefault(str(getattr(item, key_name)), []).append(item)
            for key, grouped_items in sorted(groups.items()):
                conflicts = 0
                for index, left in enumerate(grouped_items):
                    for right in grouped_items[index + 1 :]:
                        if left.item_id == right.item_id:
                            continue
                        if not _has_path(adjacency, left.item_id, right.item_id) and not _has_path(
                            adjacency, right.item_id, left.item_id
                        ):
                            conflicts += 1
                if conflicts:
                    _append_reason(reasons, category, code, key, actual=conflicts)

        capacity_workers = {
            worker.node_id
            for matching in eligible.values()
            for worker in matching
        }
        capacity = sum(
            policy.worker_profiles[worker.worker_profile].max_concurrency
            for worker in plan.workers
            if worker.node_id in capacity_workers and worker.worker_profile in policy.worker_profiles
        )
        if plan.max_active_attempts > policy.limits["max_active_attempts"]:
            _append_reason(
                reasons,
                "concurrency",
                "global_limit_exceeded",
                "max_active_attempts",
                limit=policy.limits["max_active_attempts"],
                actual=plan.max_active_attempts,
            )
        if plan.max_active_attempts > capacity:
            _append_reason(
                reasons,
                "concurrency",
                "capacity_limit_exceeded",
                "max_active_attempts",
                limit=capacity,
                actual=plan.max_active_attempts,
            )

        for item in plan.items:
            if item.retry_class not in _RETRY_CLASSES or item.retry_class not in policy.retry_classes:
                _append_reason(reasons, "retry", "retry_class_unknown", item.retry_class)
                continue
            max_attempts = item.contract.retry_policy[0]
            if policy.retry_classes[item.retry_class] and max_attempts > 1 and item.effect_safety != "idempotent":
                _append_reason(reasons, "retry", "automatic_retry_unsafe", item.item_id)

        totals: Dict[str, int] = {}
        for reservation in plan.budget_reservations:
            budget = policy.budgets.get(reservation.budget_id)
            if budget is None:
                _append_reason(reasons, "budget", "budget_unknown", reservation.budget_id)
                continue
            totals[reservation.budget_id] = totals.get(reservation.budget_id, 0) + reservation.amount
        for budget_id, total in sorted(totals.items()):
            budget = policy.budgets[budget_id]
            if total > budget.limit:
                _append_reason(
                    reasons,
                    "budget",
                    "budget_limit_exceeded",
                    budget_id,
                    limit=budget.limit,
                    actual=total,
                )

        for item in plan.items:
            if policy.approval_requirements[item.contract.risk_class]:
                _append_reason(reasons, "operator", "approval_required", item.item_id)
            if item.merge_gate is None:
                continue
            gate = policy.merge_gates.get(item.merge_gate)
            if gate is None:
                _append_reason(reasons, "merge", "merge_gate_unknown", item.merge_gate)
            elif any(verification_id not in policy.verification for verification_id in gate.verification_ids):
                _append_reason(reasons, "merge", "merge_verification_missing", item.merge_gate)
            else:
                _append_reason(reasons, "merge", "merge_gate_pending", item.merge_gate)

        ordered = _unique_sorted_reasons(reasons)
        hard_invalid = any(
            reason.category != "operator"
            and not (reason.category == "merge" and reason.code == "merge_gate_pending")
            for reason in ordered
        )
        outcome = "refused" if hard_invalid else ("needs_operator" if ordered else "admitted")
        return AdmissionArtifact(outcome, plan.digest, policy.digest, ordered)

    @staticmethod
    def require_current_admission(
        plan: AdmissionPlan, policy: RepositoryPolicy, artifact: AdmissionArtifact
    ) -> None:
        """Re-evaluate the exact immutable pair before one invocation-time write seam."""

        if not isinstance(plan, AdmissionPlan):
            _refuse("admission_plan_required", "run creation requires a loaded AdmissionPlan")
        if not isinstance(policy, RepositoryPolicy):
            _refuse("admission_policy_required", "run creation requires a loaded RepositoryPolicy")
        if not isinstance(artifact, AdmissionArtifact):
            _refuse("admission_artifact_required", "run creation requires an AdmissionArtifact")
        if artifact.plan_digest != plan.digest or artifact.policy_digest != policy.digest:
            _refuse("admission_digest_mismatch", "artifact digests do not bind the supplied plan and policy")
        current = AdmissionEvaluator.evaluate(plan, policy)
        if artifact.canonical_bytes() != current.canonical_bytes():
            _refuse("admission_stale", "artifact does not equal the current deterministic admission result")
        if current.outcome != "admitted":
            _refuse("admission_not_admitted", "only an admitted current plan may create a run")


def validate_admission_plan_integrity(plan: object) -> AdmissionPlan:
    """Return one loaded plan only when its live fields still match its semantic cache."""

    if not isinstance(plan, AdmissionPlan):
        _refuse("admission_plan_required", "a validated admission plan is required")
    _validate_plan_semantic_cache(plan)
    return plan


class AdmissionBinder:
    """The sole producer of durable runtime-admission bindings."""

    def __init__(self, run_ledger: object) -> None:
        from .runtruth import RunLedger

        if not isinstance(run_ledger, RunLedger):
            _refuse("run_ledger_required", "admission binding requires the canonical RunLedger")
        self.run_ledger = run_ledger
        self.__binding_capability = run_ledger._admission_binding_capability_for(self)

    @staticmethod
    def _timestamp(now: Optional[datetime]) -> str:
        current = datetime.now(timezone.utc) if now is None else now
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            _refuse("time_invalid", "an aware UTC-compatible datetime is required")
        value = current.astimezone(timezone.utc)
        return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    @classmethod
    def bind(
        cls,
        run_ledger: object,
        run_id: str,
        plan: AdmissionPlan,
        policy: RepositoryPolicy,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        binder = cls(run_ledger)
        return binder._bind(run_id, plan, policy, now=now)

    def _bind(
        self,
        run_id: str,
        plan: AdmissionPlan,
        policy: RepositoryPolicy,
        *,
        now: Optional[datetime],
        record_id: Optional[str] = None,
        _service_capability: object = None,
    ) -> Dict[str, object]:
        plan = validate_admission_plan_integrity(plan)
        policy = validate_repository_policy_integrity(policy)
        if record_id is not None:
            from .sequencer import (
                _known_service_record_id,
                _policy_evidence,
            )

            intent = {
                "run_id": run_id,
                "plan": plan.canonical(),
                "policy": _policy_evidence(policy),
            }
            if (
                _known_service_record_id(
                    self.run_ledger.root,
                    "admission_binding_evaluation",
                    intent,
                    record_id,
                )
                and not self.run_ledger._has_evaluated_service_capability(
                    _service_capability
                )
            ):
                _refuse(
                    "evaluated_service_only",
                    "service-derived admission identity requires live service authority",
                )
        remote = getattr(self.run_ledger._sequencer_client, "bind_admission", None)
        if callable(remote):
            return self.run_ledger._canonical_client_response(
                remote(run_id, plan, policy, self._timestamp(now))
            )
        run = self.run_ledger.project().run(run_id)
        if run["plan_digest"] != plan.digest:
            _refuse("run_admission_plan_mismatch", "plan must equal the run's durable plan evidence")
        durable_policy_digest = None
        if run["policy"] is not None:
            durable_policy_digest = run["policy"]["policy_digest"]
        elif run["admitted_pair_proof"]["status"] != "unavailable":
            durable_policy_digest = run["admitted_pair_proof"]["policy_digest"]
        if durable_policy_digest is not None and durable_policy_digest != policy.digest:
            _refuse("run_admission_policy_mismatch", "policy must equal the run's durable policy evidence")
        if run["admission_binding"]["status"] == "bound":
            _refuse("run_admission_duplicate", "run admission can bind once")
        if run["attempts"]:
            _refuse("run_admission_late", "run admission must bind before every attempt")
        current = AdmissionEvaluator.evaluate(plan, policy)
        if current.outcome != "admitted":
            _refuse("run_admission_not_admitted", "durable binding requires a currently admitted pair")

        workers = [worker.canonical() for worker in plan.workers]
        reservations = [reservation.canonical() for reservation in plan.budget_reservations]
        items = [
            {
                "item_id": item.item_id,
                "workspace_key": item.workspace_key,
                "concurrency_key": item.concurrency_key,
                "capability_selector": item.capability_selector,
            }
            for item in plan.items
        ]
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": record_id or "run-admission-bound-" + uuid7_hex(),
            "tenant_id": self.run_ledger.root.tenant_id,
            "timestamp": self._timestamp(now),
            "kind": "run_admission_bound",
            "run_id": run_id,
            "plan_digest": plan.digest,
            "policy_digest": policy.digest,
            "max_active_attempts": plan.max_active_attempts,
            "workers": workers,
            "budget_reservations": reservations,
            "items": items,
            "admission_digest": run_admission_digest(
                workers, plan.max_active_attempts, reservations, items
            ),
        }
        return self.run_ledger._append_admission_binding(
            record, self.__binding_capability
        )

    @classmethod
    def enable_spawn(
        cls,
        run_ledger: object,
        run_id: str,
        base_plan: AdmissionPlan,
        policy: RepositoryPolicy,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        """Bind the complete, re-evaluated AdmissionPlan preimage once."""

        binder = cls(run_ledger)
        return binder._enable_spawn(run_id, base_plan, policy, now=now)

    def _enable_spawn(
        self,
        run_id: str,
        base_plan: AdmissionPlan,
        policy: RepositoryPolicy,
        *,
        now: Optional[datetime],
    ) -> Dict[str, object]:
        plan = validate_admission_plan_integrity(base_plan)
        policy = validate_repository_policy_integrity(policy)
        if self.run_ledger._sequencer_client is not None:
            from .sequencer import _policy_evidence

            return self.run_ledger._evaluate_spawn_intent(
                "spawn_admission_enable_evaluation",
                {
                    "run_id": run_id,
                    "base_plan": plan.canonical(),
                    "policy": _policy_evidence(policy),
                },
            )
        current = AdmissionEvaluator.evaluate(plan, policy)
        if current.outcome != "admitted":
            _refuse(
                "spawn_admission_not_admitted",
                "spawn enablement requires a currently admitted complete base plan",
            )
        projection = self.run_ledger.project()
        run = projection.run(run_id)
        binding = run["admission_binding"]
        if binding.get("status") != "bound":
            _refuse(
                "spawn_admission_binding_missing",
                "spawn enablement requires the durable run admission binding",
            )
        if run["plan_digest"] != plan.digest:
            _refuse(
                "spawn_base_plan_digest_invalid",
                "base plan must equal the current durable run plan",
            )
        physical_edges = [
            {
                "source": edge.source,
                "target": edge.target,
                "requires": edge.requires,
                "failure_policy": edge.failure_policy,
            }
            for edge in projection.edges(run_id)
        ]
        if physical_edges != [edge.canonical() for edge in plan.dependency_edges]:
            _refuse(
                "spawn_base_plan_edges_mismatch",
                "base plan dependency edges must equal physical run truth",
            )
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "run-spawn-admission-enabled-" + uuid7_hex(),
            "tenant_id": self.run_ledger.root.tenant_id,
            "timestamp": self._timestamp(now),
            "kind": "run_spawn_admission_enabled",
            "run_id": run_id,
            "run_admission_binding_id": binding["id"],
            "admission_digest": binding["admission_digest"],
            "policy_digest": policy.digest,
            "base_plan": plan.canonical(),
            "base_plan_digest": plan.digest,
            "enabled_at_testimony": self._timestamp(now),
        }

        semantic_fields = (
            "run_id", "run_admission_binding_id", "admission_digest",
            "policy_digest", "base_plan", "base_plan_digest",
        )

        def resolve_existing(projection: object, candidate: Dict[str, object]):
            projected = projection.run(run_id)["spawn_admission"]
            if projected is None:
                return None
            if any(projected[field] != candidate[field] for field in semantic_fields):
                _refuse(
                    "spawn_admission_input_divergent",
                    "spawn enablement retry changed its complete semantic input",
                )
            return {
                key: deepcopy(value)
                for key, value in projected.items()
                if key != "current_plan"
            }

        return self.run_ledger._append_spawn_admission(
            record,
            self.__binding_capability,
            resolve_existing,
        )


__all__ = [
    "AdmissionArtifact",
    "AdmissionBinder",
    "AdmissionDependency",
    "AdmissionEvaluator",
    "AdmissionItem",
    "AdmissionPlan",
    "AdmissionReason",
    "AdmissionWorker",
    "BudgetReservation",
    "validate_admission_plan_integrity",
]

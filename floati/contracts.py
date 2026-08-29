"""Immutable task contracts and receipt-bound acceptance provenance."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Dict, Mapping, Sequence, Tuple, Union

from .errors import ProtocolRefusal


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RISK_CLASSES = frozenset({"low", "medium", "high", "critical"})


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _compact_ijson(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        _refuse("contract_not_ijson", str(exc))
    raise AssertionError("unreachable")


def _bounded_text(value: object, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
        _refuse(field + "_invalid", field + " must be a bounded nonempty string")
    return value


def _string_list(value: Sequence[object], field: str, minimum: int = 1, maximum: int = 64) -> Tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not minimum <= len(value) <= maximum:
        _refuse(field + "_invalid", field + " must be a bounded list")
    values = tuple(_bounded_text(item, field) for item in value)
    if len(set(values)) != len(values):
        _refuse(field + "_invalid", field + " must not repeat values")
    return values


def _string_map(value: Mapping[str, object], field: str, *, hashes: bool = False) -> Tuple[Tuple[str, str], ...]:
    if not isinstance(value, Mapping) or not 1 <= len(value) <= 64:
        _refuse(field + "_invalid", field + " must be a bounded nonempty object")
    values = []
    for key, item in value.items():
        name, rendered = _bounded_text(key, field, 128), _bounded_text(item, field)
        if hashes and _SHA256.fullmatch(rendered) is None:
            _refuse(field + "_invalid", field + " values must be SHA-256 digests")
        values.append((name, rendered))
    return tuple(sorted(values))


@dataclass(frozen=True)
class TaskContract:
    """The full immutable, digest-bound task intent accepted by the scheduler."""

    objective: str
    non_goals: Tuple[str, ...]
    areas_to_avoid: Tuple[Tuple[str, str], ...]
    input_hashes: Tuple[Tuple[str, str], ...]
    acceptance_checks: Tuple[Tuple[str, str], ...]
    constraints: Tuple[Tuple[str, str], ...]
    risk_class: str
    retry_policy: Tuple[int, int, int, str]
    dependencies: Tuple[str, ...]

    @classmethod
    def create(cls, *, objective: object, non_goals: Sequence[object], areas_to_avoid: Sequence[Mapping[str, object]], input_hashes: Mapping[str, object], acceptance_checks: Mapping[str, object], constraints: Mapping[str, object], risk_class: object, retry_policy: Mapping[str, object], dependencies: Sequence[object]) -> "TaskContract":
        if not isinstance(areas_to_avoid, (tuple, list)) or not 1 <= len(areas_to_avoid) <= 64:
            _refuse("areas_to_avoid_invalid", "areas_to_avoid must be a bounded nonempty list")
        areas = []
        for area in areas_to_avoid:
            if not isinstance(area, Mapping) or set(area) != {"path", "region"}:
                _refuse("areas_to_avoid_invalid", "each avoid area must name exactly path and region")
            path = _bounded_text(area["path"], "area_path", 1024)
            if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
                _refuse("areas_to_avoid_invalid", "avoid paths must be repository-relative")
            areas.append((path, _bounded_text(area["region"], "area_region", 1024)))
        if len(set(areas)) != len(areas):
            _refuse("areas_to_avoid_invalid", "avoid areas must not repeat")
        if not isinstance(risk_class, str) or risk_class not in _RISK_CLASSES:
            _refuse("risk_class_invalid", "risk class is outside the closed v0 vocabulary")
        if not isinstance(retry_policy, Mapping) or set(retry_policy) != {"max_attempts", "backoff"}:
            _refuse("retry_policy_invalid", "retry policy must use its exact v0 fields")
        max_attempts, backoff = retry_policy["max_attempts"], retry_policy["backoff"]
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 32:
            _refuse("retry_policy_invalid", "max_attempts must be a bounded integer")
        if not isinstance(backoff, Mapping) or set(backoff) != {"base_delay_ms", "cap_delay_ms", "strategy"}:
            _refuse("retry_policy_invalid", "backoff must use its exact v0 fields")
        base, cap, strategy = backoff["base_delay_ms"], backoff["cap_delay_ms"], backoff["strategy"]
        if any(not isinstance(value, int) or isinstance(value, bool) for value in (base, cap)) or not 0 <= base <= cap <= 86400000:
            _refuse("retry_policy_invalid", "backoff delays must be bounded ordered integers")
        if not isinstance(strategy, str) or strategy not in {"fixed", "exponential"}:
            _refuse("retry_policy_invalid", "backoff strategy is outside the closed v0 vocabulary")
        return cls(_bounded_text(objective, "objective"), _string_list(non_goals, "non_goals"), tuple(sorted(areas)), _string_map(input_hashes, "input_hashes", hashes=True), _string_map(acceptance_checks, "acceptance_checks"), _string_map(constraints, "constraints"), str(risk_class), (max_attempts, base, cap, str(strategy)), _string_list(dependencies, "dependencies", 0))

    def canonical(self) -> Dict[str, object]:
        max_attempts, base, cap, strategy = self.retry_policy
        return {"objective": self.objective, "non_goals": list(self.non_goals), "areas_to_avoid": [{"path": path, "region": region} for path, region in self.areas_to_avoid], "input_hashes": dict(self.input_hashes), "acceptance_checks": dict(self.acceptance_checks), "constraints": dict(self.constraints), "risk_class": self.risk_class, "retry_policy": {"max_attempts": max_attempts, "backoff": {"base_delay_ms": base, "cap_delay_ms": cap, "strategy": strategy}}, "dependencies": list(self.dependencies)}

    def replaced(self, **changes: object) -> "TaskContract":
        if not changes or not set(changes) <= set(self.canonical()):
            _refuse("amendment_fields_invalid", "an amendment may replace only declared task-contract fields")
        value = self.canonical(); value.update(changes)
        return TaskContract.create(**value)  # type: ignore[arg-type]


def contract_digest(contract: Union[TaskContract, Mapping[str, object]]) -> str:
    value = contract.canonical() if isinstance(contract, TaskContract) else dict(contract)
    return hashlib.sha256(_compact_ijson(value)).hexdigest()


@dataclass(frozen=True)
class PlanAmendment:
    previous_digest: str
    replacement_fields: Tuple[Tuple[str, object], ...]
    contract_digest: str

    @classmethod
    def between(cls, previous: TaskContract, replacement: TaskContract) -> "PlanAmendment":
        before, after = previous.canonical(), replacement.canonical()
        fields = tuple((key, after[key]) for key in sorted(after) if before[key] != after[key])
        if not fields:
            _refuse("amendment_empty", "an amendment must replace at least one task-contract field")
        return cls(contract_digest(previous), fields, contract_digest(replacement))


@dataclass(frozen=True)
class AcceptanceReceipt:
    """Immutable acceptance testimony; a score is intentionally not part of it."""

    contract_digest: str
    check_ids: Tuple[str, ...]
    reviewer: str
    evidence_bindings: Tuple[str, ...]
    deviations: Tuple[str, ...]
    result: str

    @classmethod
    def create(cls, *, contract_digest: object, check_ids: Sequence[object], reviewer: object, evidence_bindings: Sequence[object], deviations: Sequence[object], result: object) -> "AcceptanceReceipt":
        if not isinstance(contract_digest, str) or _SHA256.fullmatch(contract_digest) is None:
            _refuse("contract_digest_invalid", "receipt must name a SHA-256 task-contract digest")
        if not isinstance(result, str) or result not in {"accepted", "rejected"}:
            _refuse("receipt_result_invalid", "receipt result is outside the closed v0 vocabulary")
        return cls(contract_digest, _string_list(check_ids, "check_ids"), _bounded_text(reviewer, "reviewer", 64), _string_list(evidence_bindings, "evidence_bindings", 1, 32), _string_list(deviations, "deviations", 0, 64), str(result))

    def canonical(self) -> Dict[str, object]:
        return {"contract_digest": self.contract_digest, "check_ids": list(self.check_ids), "reviewer": self.reviewer, "evidence_bindings": list(self.evidence_bindings), "deviations": list(self.deviations), "result": self.result}


class ContractHistory:
    """An append-only contract chain that never replaces historical contracts."""

    def __init__(self, initial: TaskContract) -> None:
        self._contracts = (initial,)

    def append(self, amendment: PlanAmendment, replacement: TaskContract) -> None:
        if amendment.previous_digest != contract_digest(self._contracts[-1]):
            _refuse("amendment_previous_digest_invalid", "amendment must name the current immutable contract digest")
        if amendment.contract_digest != contract_digest(replacement):
            _refuse("amendment_digest_invalid", "amendment digest must name its exact replacement contract")
        if PlanAmendment.between(self._contracts[-1], replacement) != amendment:
            _refuse("amendment_replacement_invalid", "amendment replacements must exactly match the replacement contract")
        self._contracts += (replacement,)

    def contracts(self) -> Tuple[TaskContract, ...]:
        return self._contracts

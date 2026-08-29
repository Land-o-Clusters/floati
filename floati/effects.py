"""Canonical physical-order projection and private writer for effect truth."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Mapping, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import _transact_effect_records, read_records
from .policy import (
    _REPOSITORY_POLICY_EFFECT_APPROVAL_REQUIRED as _effect_approval_required,
    _REPOSITORY_POLICY_EFFECT_BUDGET_LIMIT as _effect_budget_limit,
    _REPOSITORY_POLICY_TYPE as _EFFECT_POLICY_TYPE,
    validate_repository_policy_integrity as _validate_effect_policy,
)
from .records import (
    EFFECT_BINDING_FIELDS,
    EFFECT_COMPENSATION_REASONS,
    EFFECT_KINDS,
    validate_record,
)
from .root import FloatiRoot
from .effect_reconciliation_exec import observe_effect_reconciliation
from .effect_reconciliation_protocol import (
    ReconciliationRequest,
    ReconciliationResult,
    build_request,
    validate_result,
)


_PRIMARY_KINDS = frozenset({"effect_confirmed", "effect_failed", "effect_unknown"})
_TERMINAL_STATES = frozenset({
    "confirmed", "failed", "unknown",
    "reconciled_confirmed", "reconciled_failed", "reconciled_unknown",
})
_COMPENSATION_SOURCE_STATES = frozenset({
    "confirmed", "failed", "reconciled_confirmed",
    "reconciled_failed", "reconciled_unknown",
})
_COMPENSATION_PLAN_FIELDS = frozenset({
    "plan_version", "source_operation_id", "source_effect_evidence_id",
    "reason_code", "compensation_operation_id", "run_id", "item_id",
    "attempt_id", "fence_token", "effect_type", "target", "request_digest",
    "idempotency_key", "expected_confirmation", "reconciliation_adapter",
    "risk_class", "budget_claim", "requested_by",
})
_RECONCILIATION_FAILURE_REASONS = (
    (TimeoutError, "observer_timeout"),
    (ChildProcessError, "observer_child_died"),
    (OSError, "observer_cleanup_failed"),
)


def _canonical_ijson(value: object) -> bytes:
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
            "effect_input_invalid", "effect evidence cannot form canonical I-JSON"
        ) from exc


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(member) for key, member in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(member) for member in value)
    if isinstance(value, tuple):
        return tuple(_freeze(member) for member in value)
    return value


def _binding(record: Mapping[str, object]) -> dict[str, object]:
    return {field: deepcopy(record[field]) for field in EFFECT_BINDING_FIELDS}


def _semantic_row(record: Mapping[str, object]) -> dict[str, object]:
    return {
        key: deepcopy(value)
        for key, value in record.items()
        if key not in {"id", "timestamp"} and not key.endswith("_at_testimony")
    }


def _spend_rows(value: object) -> tuple[tuple[str, int], ...] | None:
    if value is None:
        return None
    return tuple((str(row["budget_id"]), int(row["amount"])) for row in value)


@dataclass(frozen=True)
class EffectAcceptanceEvidence:
    operation_ids: tuple[str, ...]
    high_watermark: int
    evidence_digest: str
    measured_spend: tuple[tuple[str, int], ...]
    blockers: tuple[str, ...]
    incomplete_spend_operation_ids: tuple[str, ...] = ()


class EffectProjection:
    """Immutable-view replay of one physically ordered Effect-ledger prefix."""

    def __init__(self) -> None:
        self._records: list[dict[str, object]] = []
        self._operations: dict[str, dict[str, object]] = {}
        self._attempts: dict[tuple[str, str], list[str]] = {}
        self._idempotency: dict[str, str] = {}
        self._seen_ids: set[str] = set()

    @classmethod
    def from_records(
        cls, records: Iterable[Mapping[str, object]], *, integrity: bool = True
    ) -> "EffectProjection":
        projection = cls()
        for record in records:
            projection._apply(record, integrity=integrity)
        return projection

    @staticmethod
    def _raise(integrity: bool, code: str, detail: str) -> None:
        error = IntegrityFailure if integrity else ProtocolRefusal
        raise error(code, detail)

    def _apply(self, raw: Mapping[str, object], *, integrity: bool) -> None:
        expected_tenant = (
            str(self._records[0]["tenant_id"])
            if self._records
            else (
                str(raw.get("tenant_id", ""))
                if isinstance(raw, Mapping)
                else ""
            )
        )
        record = validate_record(
            deepcopy(raw), expected_tenant,
            EFFECT_KINDS, integrity=integrity,
        )
        if self._records and record["tenant_id"] != self._records[0]["tenant_id"]:
            self._raise(integrity, "tenant_mismatch", "effect prefix crosses tenants")
        record_id = str(record["id"])
        if record_id in self._seen_ids:
            self._raise(integrity, "duplicate_record_id", "effect prefix repeats a record id")

        kind = str(record["kind"])
        operation_id = str(record["operation_id"])
        if kind == "effect_intent":
            self._apply_intent(record, operation_id, integrity)
        else:
            operation = self._operations.get(operation_id)
            if operation is None:
                self._raise(
                    integrity, "effect_transition_invalid",
                    "effect lifecycle row precedes its physical intent",
                )
            if _binding(record) != operation["binding"]:
                self._raise(
                    integrity, "effect_evidence_invalid",
                    "effect lifecycle row changes its immutable binding",
                )
            self._apply_lifecycle(record, operation, integrity)

        self._seen_ids.add(record_id)
        self._records.append(deepcopy(record))

    def _apply_intent(
        self, record: dict[str, object], operation_id: str, integrity: bool
    ) -> None:
        key = str(record["idempotency_key"])
        if operation_id in self._operations:
            self._raise(
                integrity, "effect_transition_invalid", "effect operation repeats its intent"
            )
        if key in self._idempotency:
            self._raise(
                integrity, "effect_idempotency_conflict",
                "effect idempotency key already binds another operation",
            )
        binding = _binding(record)
        operation = {
            **deepcopy(binding),
            "binding": binding,
            "intent_id": record["id"],
            "dispatch_id": None,
            "acknowledgement_id": None,
            "primary_outcome_id": None,
            "state": "intent",
            "current_evidence_id": record["id"],
            "current_record": deepcopy(record),
            "spend_status": "not_reported",
            "measured_spend": None,
            "compensation_state": "none",
            "compensation_proposal_id": None,
            "_intent_position": len(self._records) + 1,
            "_reconciliation_evidence_digests": set(),
            "_compensation_operation_id": None,
            "_compensation_request_digest": None,
            "_compensation_proposal_position": None,
        }
        self._operations[operation_id] = operation
        self._idempotency[key] = operation_id
        self._attempts.setdefault(
            (str(record["run_id"]), str(record["attempt_id"])), []
        ).append(operation_id)

    def _require_reference(
        self,
        record: Mapping[str, object],
        field: str,
        expected: object,
        integrity: bool,
    ) -> None:
        if record.get(field) != expected:
            self._raise(
                integrity, "effect_evidence_invalid",
                f"{field} does not reference the current physical operation",
            )

    def _validate_spend(
        self, record: Mapping[str, object], operation: Mapping[str, object], integrity: bool
    ) -> None:
        measured = _spend_rows(record.get("measured_spend"))
        claim = dict(_spend_rows(operation["budget_claim"]) or ())
        confirmed = (
            record.get("kind") == "effect_confirmed"
            or (
                record.get("kind") == "effect_reconciled"
                and record.get("reconciled_outcome") == "confirmed"
            )
        )
        if confirmed and (
            measured is None or set(dict(measured)) != set(claim)
        ):
            self._raise(
                integrity, "effect_evidence_invalid",
                "confirmed effect spend must cover every immutable budget claim key",
            )
        if measured is None:
            return
        if any(budget not in claim or amount > claim[budget] for budget, amount in measured):
            self._raise(
                integrity, "effect_budget_exceeded",
                "measured effect spend exceeds the immutable budget claim",
            )

    def _set_current(
        self, operation: dict[str, object], record: Mapping[str, object], state: str
    ) -> None:
        operation["state"] = state
        operation["current_evidence_id"] = record["id"]
        operation["current_record"] = deepcopy(record)
        if state in _TERMINAL_STATES:
            operation["spend_status"] = (
                "complete" if record["kind"] == "effect_confirmed" else record["spend_status"]
            )
            operation["measured_spend"] = _spend_rows(record.get("measured_spend"))

    def _apply_lifecycle(
        self, record: dict[str, object], operation: dict[str, object], integrity: bool
    ) -> None:
        kind = str(record["kind"])
        state = str(operation["state"])
        if kind != "compensation_executed":
            self._require_reference(record, "effect_intent_id", operation["intent_id"], integrity)

        if kind == "effect_dispatched":
            if state != "intent":
                self._raise(integrity, "effect_transition_invalid", "dispatch must follow intent")
            operation["dispatch_id"] = record["id"]
            self._set_current(operation, record, "dispatched")
            return

        if kind == "effect_acknowledged":
            if state != "dispatched":
                self._raise(
                    integrity, "effect_transition_invalid", "acknowledgement must follow dispatch"
                )
            self._require_reference(record, "effect_dispatched_id", operation["dispatch_id"], integrity)
            operation["acknowledgement_id"] = record["id"]
            self._set_current(operation, record, "acknowledged")
            return

        if kind in _PRIMARY_KINDS:
            if state not in {"dispatched", "acknowledged"}:
                self._raise(
                    integrity, "effect_transition_invalid",
                    "exactly one primary effect outcome may follow dispatch",
                )
            self._require_reference(record, "effect_dispatched_id", operation["dispatch_id"], integrity)
            if kind == "effect_confirmed":
                self._require_reference(
                    record, "effect_acknowledged_id", operation["acknowledgement_id"], integrity
                )
                if record["confirmation"] != operation["expected_confirmation"]:
                    self._raise(
                        integrity, "effect_confirmation_mismatch",
                        "confirmed evidence does not match the immutable expected confirmation",
                    )
            self._validate_spend(record, operation, integrity)
            operation["primary_outcome_id"] = record["id"]
            self._set_current(operation, record, kind.removeprefix("effect_"))
            return

        if kind == "effect_reconciled":
            if state not in {"failed", "unknown", "reconciled_failed", "reconciled_unknown"}:
                self._raise(
                    integrity, "effect_transition_invalid",
                    "only current failed or unknown effect evidence may reconcile",
                )
            self._require_reference(
                record, "prior_effect_evidence_id", operation["current_evidence_id"], integrity
            )
            outcome = str(record["reconciled_outcome"])
            evidence_digest = str(record["reconciliation_evidence_digest"])
            if evidence_digest in operation["_reconciliation_evidence_digests"]:
                self._raise(
                    integrity, "effect_evidence_invalid",
                    "later reconciliation must add a new evidence digest",
                )
            if outcome == "confirmed" and (
                record.get("confirmation") is None
                or record.get("spend_status") != "complete"
                or record.get("measured_spend") is None
            ):
                self._raise(
                    integrity, "effect_evidence_invalid",
                    "confirmed reconciliation requires complete confirmation and spend evidence",
                )
            if (
                outcome == "confirmed"
                and record["confirmation"] != operation["expected_confirmation"]
            ):
                self._raise(
                    integrity, "effect_confirmation_mismatch",
                    "reconciled confirmation does not match the immutable expectation",
                )
            if outcome != "confirmed" and record.get("confirmation") is not None:
                self._raise(
                    integrity, "effect_evidence_invalid",
                    "non-confirmed reconciliation cannot carry confirmation evidence",
                )
            self._validate_spend(record, operation, integrity)
            operation["_reconciliation_evidence_digests"].add(evidence_digest)
            self._set_current(operation, record, "reconciled_" + outcome)
            return

        if kind == "compensation_proposed":
            if state not in {
                "confirmed", "failed", "reconciled_confirmed",
                "reconciled_failed", "reconciled_unknown",
            } or operation["compensation_state"] != "none":
                self._raise(
                    integrity, "effect_transition_invalid",
                    "compensation proposal requires current terminal effect evidence",
                )
            if record["compensation_operation_id"] == operation["operation_id"]:
                self._raise(
                    integrity, "effect_transition_invalid",
                    "an effect operation cannot compensate itself",
                )
            self._require_reference(
                record, "source_effect_evidence_id", operation["current_evidence_id"], integrity
            )
            operation["compensation_state"] = "proposed"
            operation["compensation_proposal_id"] = record["id"]
            operation["_compensation_operation_id"] = record["compensation_operation_id"]
            operation["_compensation_request_digest"] = record["compensation_request_digest"]
            operation["_compensation_proposal_position"] = len(self._records) + 1
            return

        if kind == "compensation_executed":
            if operation["compensation_state"] != "proposed":
                self._raise(
                    integrity, "effect_transition_invalid",
                    "compensation execution requires a physical proposal",
                )
            self._require_reference(
                record, "compensation_proposal_id", operation["compensation_proposal_id"], integrity
            )
            compensation_id = str(record["compensation_operation_id"])
            if compensation_id != operation["_compensation_operation_id"]:
                self._raise(
                    integrity, "effect_evidence_invalid",
                    "compensation execution changes the proposal operation binding",
                )
            compensation = self._operations.get(compensation_id)
            if compensation is None or compensation["state"] not in {
                "confirmed", "reconciled_confirmed",
            }:
                self._raise(
                    integrity, "effect_transition_invalid",
                    "compensation execution requires separately confirmed effect truth",
                )
            if compensation["_intent_position"] <= operation["_compensation_proposal_position"]:
                self._raise(
                    integrity, "effect_transition_invalid",
                    "compensation operation intent must physically follow its proposal",
                )
            if compensation["request_digest"] != operation["_compensation_request_digest"]:
                self._raise(
                    integrity, "effect_evidence_invalid",
                    "compensation operation request digest must match its proposal",
                )
            self._require_reference(
                record, "compensation_terminal_evidence_id",
                compensation["current_evidence_id"], integrity,
            )
            operation["compensation_state"] = "executed"
            return

        self._raise(integrity, "effect_transition_invalid", "unsupported effect transition")

    def operation(self, operation_id: str) -> Mapping[str, object]:
        operation = self._operations.get(operation_id)
        if operation is None:
            raise ProtocolRefusal("effect_operation_missing", "effect operation is absent")
        public = {
            key: deepcopy(value)
            for key, value in operation.items()
            if not key.startswith("_")
            and key not in {"binding", "current_record", "intent_id", "dispatch_id", "acknowledgement_id", "primary_outcome_id", "compensation_proposal_id"}
        }
        return _freeze(public)  # type: ignore[return-value]

    def for_attempt(self, run_id: str, attempt_id: str) -> tuple[Mapping[str, object], ...]:
        return tuple(
            self.operation(operation_id)
            for operation_id in self._attempts.get((run_id, attempt_id), ())
        )

    def acceptance_evidence(self, run_id: str, attempt_id: str) -> EffectAcceptanceEvidence:
        operation_ids = tuple(sorted(self._attempts.get((run_id, attempt_id), ())))
        terminal_rows: list[dict[str, object]] = []
        spend: dict[str, int] = {}
        blockers: list[str] = []
        incomplete_spend: list[str] = []
        for operation_id in operation_ids:
            operation = self._operations[operation_id]
            state = str(operation["state"])
            if operation["compensation_state"] == "proposed":
                compensation = self._operations.get(
                    str(operation["_compensation_operation_id"])
                )
                if (
                    compensation is None
                    or compensation["_intent_position"]
                    <= operation["_compensation_proposal_position"]
                    or compensation["request_digest"]
                    != operation["_compensation_request_digest"]
                ):
                    blockers.append(
                        f"{operation_id}:compensation_intent_missing"
                    )
            if state in _TERMINAL_STATES:
                terminal_rows.append(deepcopy(operation["current_record"]))
            if state not in {"confirmed", "reconciled_confirmed"}:
                blockers.append(f"{operation_id}:{state}")
                if operation["spend_status"] != "complete":
                    incomplete_spend.append(operation_id)
                continue
            for budget, amount in operation["measured_spend"] or ():
                spend[budget] = spend.get(budget, 0) + amount
        payload = {
            "effect_operation_ids": list(operation_ids),
            "effect_ledger_high_watermark": len(self._records),
            "terminal_effect_rows": terminal_rows,
        }
        return EffectAcceptanceEvidence(
            operation_ids=operation_ids,
            high_watermark=len(self._records),
            evidence_digest=hashlib.sha256(_canonical_ijson(payload)).hexdigest(),
            measured_spend=tuple(sorted(spend.items())),
            blockers=tuple(blockers),
            incomplete_spend_operation_ids=tuple(incomplete_spend),
        )

    def post_watermark_intent_ids(
        self, run_id: str, attempt_id: str, high_watermark: int,
    ) -> tuple[str, ...]:
        """Name same-attempt operations whose intent is after an accepted prefix."""

        return tuple(sorted(
            str(operation_id)
            for operation_id in self._attempts.get((run_id, attempt_id), ())
            if self._operations[operation_id]["_intent_position"] > high_watermark
        ))


class EffectLedger:
    """The sole effect truth path; writes require controller-owned authority."""

    relative_path = Path("effects/records.jsonl")

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "effect ledger requires a writable FloatiRoot")
        self.root = root
        self.__controller_capability: Optional[object] = None
        self.__controller_owner: Optional[object] = None
        self.__controller_owner_pid: Optional[int] = None

    def records(self) -> list[dict[str, object]]:
        return deepcopy(read_records(
            self.root, self.relative_path, allowed_kinds=set(EFFECT_KINDS)
        ))

    def project(self) -> EffectProjection:
        return EffectProjection.from_records(self.records())

    def _controller_capability_for(self, controller: object) -> object:
        try:
            caller = sys._getframe(1)
        except ValueError:
            caller = None
        sealed_caller = caller.f_back if caller is not None else None
        sealed_args = (
            sealed_caller.f_locals.get("args")
            if sealed_caller is not None
            else None
        )
        if (
            type(controller) is not EffectController
            or getattr(controller, "ledger", None) is not self
            or caller is None
            or caller.f_code is not _EFFECT_CONTROLLER_INIT_BODY_CODE
            or caller.f_locals.get("self") is not controller
            or sealed_caller is None
            or sealed_caller.f_code is not _EFFECT_POLICY_SEAL_CODE
            or not isinstance(sealed_args, tuple)
            or not sealed_args
            or sealed_args[0] is not controller
            or getattr(
                sealed_caller.f_locals.get("method"), "__code__", None
            ) is not caller.f_code
            or caller.f_locals.get("policy_operations")
            is not sealed_caller.f_locals.get("original")
            or (
                self.__controller_owner is not None
                and self.__controller_owner is not controller
            )
            or (
                self.__controller_owner_pid is not None
                and self.__controller_owner_pid != os.getpid()
            )
        ):
            raise ProtocolRefusal(
                "effect_controller_only",
                "effect append capability requires the exact controller-owned ledger",
            )
        if self.__controller_capability is None:
            self.__controller_capability = object()
            self.__controller_owner = controller
            self.__controller_owner_pid = os.getpid()
        return self.__controller_capability

    def _append_controller(
        self,
        raw_record: Mapping[str, object],
        capability: object = None,
        resolve_existing: Optional[
            Callable[[EffectProjection, dict[str, object]], Optional[dict[str, object]]]
        ] = None,
    ) -> dict[str, object]:
        try:
            caller = sys._getframe(1)
        except ValueError:
            caller = None
        if (
            capability is None
            or capability is not self.__controller_capability
            or caller is None
            or caller.f_code is not _EFFECT_CONTROLLER_APPEND_CODE
            or caller.f_locals.get("self") is not self.__controller_owner
            or self.__controller_owner_pid != os.getpid()
        ):
            raise ProtocolRefusal(
                "effect_controller_only", "effect rows require controller-owned authority"
            )
        record = validate_record(
            deepcopy(raw_record), self.root.tenant_id, EFFECT_KINDS, integrity=False
        )

        def decide(existing: list[dict[str, object]]):
            projection = EffectProjection.from_records(existing, integrity=True)
            for prior in existing:
                if prior["id"] != record["id"]:
                    continue
                if prior == record:
                    return deepcopy(prior), None
                raise ProtocolRefusal(
                    "duplicate_record_id", "effect record id cannot change semantic payload"
                )

            if record["kind"] == "effect_intent":
                operation_id = projection._idempotency.get(str(record["idempotency_key"]))
                if operation_id is not None:
                    operation = projection._operations[operation_id]
                    if operation["binding"] != _binding(record):
                        raise ProtocolRefusal(
                            "effect_idempotency_conflict",
                            "effect idempotency key cannot change immutable binding",
                        )
                    intent_id = operation["intent_id"]
                    canonical = next(item for item in existing if item["id"] == intent_id)
                    return deepcopy(canonical), None
            else:
                operation = projection._operations.get(str(record["operation_id"]))
                if operation is not None and operation["binding"] == _binding(record):
                    semantic = _semantic_row(record)
                    for prior in existing:
                        if (
                            prior["operation_id"] == record["operation_id"]
                            and prior["kind"] == record["kind"]
                            and _semantic_row(prior) == semantic
                            and (
                                record["kind"] != "effect_reconciled"
                                or operation["current_evidence_id"] == prior["id"]
                            )
                        ):
                            return deepcopy(prior), None
            if resolve_existing is not None:
                resolved = resolve_existing(projection, record)
                if resolved is not None:
                    return deepcopy(resolved), None
            projection._apply(record, integrity=False)
            return deepcopy(record), record

        return deepcopy(_transact_effect_records(self.root, decide))


_EFFECT_LEDGER_APPEND_CODE = EffectLedger._append_controller.__code__


def _effect_now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ProtocolRefusal(
            "time_invalid", "effect testimony requires an aware UTC-compatible time"
        )
    return current.astimezone(timezone.utc)


def _effect_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _semantic_operation_id(tenant_id: str, idempotency_key: object) -> str:
    payload = _canonical_ijson({
        "domain": "slipway-effect-operation-v1",
        "tenant_id": tenant_id,
        "idempotency_key": idempotency_key,
    })
    digest = bytearray(hashlib.sha256(payload).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x70
    digest[8] = (digest[8] & 0x3F) | 0x80
    return "effect-op-" + digest.hex()


def _seal_effect_policy_bindings(method: Callable[..., object]):
    """Fail closed if any module-level policy binding changes after import."""

    original = (
        _EFFECT_POLICY_TYPE,
        _validate_effect_policy,
        _effect_approval_required,
        _effect_budget_limit,
    )
    @wraps(method)
    def sealed(*args: object, **kwargs: object):
        if (
            _EFFECT_POLICY_TYPE is not original[0]
            or _validate_effect_policy is not original[1]
            or _effect_approval_required is not original[2]
            or _effect_budget_limit is not original[3]
        ):
            raise ProtocolRefusal(
                "effect_policy_binding_tampered",
                "effect policy authority bindings changed after module load",
            )
        return method(*args, **kwargs)

    return sealed


class EffectController:
    """Construct effect lifecycle rows from typed arguments and durable truth."""

    @_seal_effect_policy_bindings
    def __init__(
        self,
        ledger: EffectLedger,
        run_ledger: object,
        policy: object,
        approvals: object = None,
    ) -> None:
        try:
            policy_call = sys._getframe(1)
            policy_body_code = sys._getframe(0).f_code
        except ValueError:
            policy_call = None
            policy_body_code = None
        policy_method = (
            policy_call.f_locals.get("method") if policy_call is not None else None
        )
        policy_operations = (
            policy_call.f_locals.get("original") if policy_call is not None else None
        )
        if (
            policy_call is None
            or policy_call.f_code is not _EFFECT_POLICY_SEAL_CODE
            or getattr(policy_method, "__code__", None) is not policy_body_code
            or not isinstance(policy_operations, tuple)
            or len(policy_operations) != 4
        ):
            raise ProtocolRefusal(
                "effect_policy_binding_tampered",
                "effect policy authority requires the sealed controller call path",
            )
        policy_type, validate_policy, _, _ = policy_operations

        from .approvals import ApprovalLedger
        from .runtruth import RunLedger

        if type(ledger) is not EffectLedger:
            raise ProtocolRefusal(
                "effect_ledger_required", "effect controller requires EffectLedger"
            )
        if type(run_ledger) is not RunLedger:
            raise ProtocolRefusal(
                "run_ledger_required", "effect controller requires canonical RunLedger"
            )
        if run_ledger._sequencer_client is not None:
            raise ProtocolRefusal(
                "effect_managed_evaluation_required",
                "managed effects require a later service-owned evaluation path",
            )
        selected = ApprovalLedger(run_ledger.root) if approvals is None else approvals
        if type(selected) is not ApprovalLedger:
            raise ProtocolRefusal(
                "approval_ledger_required",
                "effect controller requires canonical ApprovalLedger",
            )
        homes = {
            ledger.root.tenant_home,
            run_ledger.root.tenant_home,
            selected.root.tenant_home,
        }
        if len(homes) != 1:
            raise ProtocolRefusal(
                "effect_root_mismatch", "effect, run, and approval truth must share one root"
            )
        if type(policy) is not policy_type:
            raise ProtocolRefusal(
                "policy_required", "effect controller requires repository policy"
            )
        self.ledger = ledger
        self.run_ledger = run_ledger
        self.policy = validate_policy(policy)
        self.approvals = selected
        self.__owner_pid = os.getpid()
        self.__capability = ledger._controller_capability_for(self)

    def _append_owned(
        self,
        record: dict[str, object],
        resolve_existing: Optional[
            Callable[[EffectProjection, dict[str, object]], Optional[dict[str, object]]]
        ] = None,
    ) -> dict[str, object]:
        if self.__owner_pid != os.getpid():
            raise ProtocolRefusal(
                "effect_controller_process_owner_required",
                "effect rows require the controller-owning process",
            )
        try:
            caller = sys._getframe(1)
        except ValueError:
            caller = None
        sealed_caller = caller.f_back if caller is not None else None
        sealed_args = (
            sealed_caller.f_locals.get("args")
            if sealed_caller is not None
            else None
        )
        sealed_method = (
            caller is not None
            and caller.f_code in _EFFECT_CONTROLLER_SEALED_METHOD_CODES
            and sealed_caller is not None
            and sealed_caller.f_code is _EFFECT_POLICY_SEAL_CODE
            and isinstance(sealed_args, tuple)
            and bool(sealed_args)
            and sealed_args[0] is self
            and getattr(
                sealed_caller.f_locals.get("method"), "__code__", None
            ) is caller.f_code
            and caller.f_locals.get("policy_operations")
            is sealed_caller.f_locals.get("original")
        )
        direct_method = (
            caller is not None
            and caller.f_code in _EFFECT_CONTROLLER_DIRECT_METHOD_CODES
        )
        if (
            caller is None
            or caller.f_locals.get("self") is not self
            or not (sealed_method or direct_method)
        ):
            raise ProtocolRefusal(
                "effect_controller_only",
                "effect rows require exact controller method construction",
            )
        return self.ledger._append_controller(
            record, self.__capability, resolve_existing
        )

    def _require_worker_pipe_receive(
        self,
        capability: object,
        context: Mapping[str, object],
        authorizer: object,
        event: Mapping[str, object],
    ) -> None:
        """Require one exact live parent receive before worker effect application."""

        from multiprocessing.connection import Connection
        from multiprocessing.process import BaseProcess
        from types import FrameType

        from .worker_bootstrap_protocol import BootstrapChannel
        from .worker_exec import SpawnedWorkerProcess
        from .workers import WorkerRunner

        if self.__owner_pid != os.getpid() or not callable(authorizer):
            raise ProtocolRefusal(
                "effect_pipe_receive_required",
                "worker effect testimony requires its live parent receive",
            )
        try:
            launch = authorizer(capability, self, context)
        except Exception:
            launch = None
        if not isinstance(launch, tuple) or len(launch) != 4:
            raise ProtocolRefusal(
                "effect_pipe_receive_required",
                "worker effect testimony requires its live parent receive",
            )
        process, connection, owner_pid, launch_frame = launch
        try:
            application_frame = sys._getframe(1)
        except ValueError:
            application_frame = None
        receive_frame = (
            application_frame.f_back if application_frame is not None else None
        )
        active_launch = False
        current_frame = sys._getframe()
        while current_frame is not None:
            if current_frame is launch_frame:
                active_launch = True
                break
            current_frame = current_frame.f_back
        frame_locals = (
            launch_frame.f_locals if isinstance(launch_frame, FrameType) else {}
        )
        runner = frame_locals.get("self")
        legacy_transport = (
            isinstance(process, BaseProcess)
            and getattr(process, "_parent_pid", None) == owner_pid
            and process.pid is not None
            and getattr(process, "_popen", None) is not None
            and isinstance(connection, Connection)
            and not connection.closed
        )
        exec_transport = (
            type(process) is SpawnedWorkerProcess
            and getattr(process, "_parent_pid", None) == owner_pid
            and type(process.pid) is int
            and type(connection) is BootstrapChannel
            and connection._socket.fileno() >= 0
        )
        if (
            type(owner_pid) is not int
            or owner_pid != os.getpid()
            or not isinstance(launch_frame, FrameType)
            or launch_frame.f_code is not WorkerRunner.run.__code__
            or not active_launch
            or not isinstance(runner, WorkerRunner)
            or runner.effect_controller is not self
            or frame_locals.get("process") is not process
            or frame_locals.get("parent") is not connection
            or frame_locals.get("process_started") is not True
            or frame_locals.get("effect_application_identity") is not capability
            or frame_locals.get("effect_context") is not context
            or application_frame is None
            or application_frame.f_code is not WorkerRunner._apply_effect_event.__code__
            or application_frame.f_locals.get("event") is not event
            or receive_frame is None
            or receive_frame.f_code is not WorkerRunner._receive.__code__
            or receive_frame.f_locals.get("effect_event") is not event
            or not (legacy_transport or exec_transport)
        ):
            raise ProtocolRefusal(
                "effect_pipe_receive_required",
                "worker effect testimony requires the exact live parent process and pipe",
            )

    @_seal_effect_policy_bindings
    def intent(
        self,
        *,
        run_id: object,
        item_id: object,
        attempt_id: object,
        fence_token: object,
        effect_type: object,
        target: object,
        request_digest: object,
        idempotency_key: object,
        expected_confirmation: object,
        reconciliation_adapter: object,
        risk_class: object,
        budget_claim: object,
        requested_by: object,
        approval_request_id: object = None,
        approval_decision_id: object = None,
        approval_consumption_id: object = None,
        now: Optional[datetime] = None,
    ) -> dict[str, object]:
        try:
            policy_call = sys._getframe(1)
            policy_body_code = sys._getframe(0).f_code
        except ValueError:
            policy_call = None
            policy_body_code = None
        policy_method = (
            policy_call.f_locals.get("method") if policy_call is not None else None
        )
        policy_operations = (
            policy_call.f_locals.get("original") if policy_call is not None else None
        )
        if (
            policy_call is None
            or policy_call.f_code is not _EFFECT_POLICY_SEAL_CODE
            or getattr(policy_method, "__code__", None) is not policy_body_code
            or not isinstance(policy_operations, tuple)
            or len(policy_operations) != 4
        ):
            raise ProtocolRefusal(
                "effect_policy_binding_tampered",
                "effect policy authority requires the sealed controller call path",
            )
        _, validate_policy, approval_required, budget_limit = policy_operations

        from .runtruth import effect_acceptance_guard

        current = _effect_now(now)
        policy = validate_policy(self.policy)
        with effect_acceptance_guard(self.ledger.root):
            context = self.run_ledger._project_under_effect_acceptance_guard().effect_intent_context(
                run_id, item_id, attempt_id, fence_token
            )
            if context["policy_digest"] != policy.digest:
                raise ProtocolRefusal(
                    "effect_policy_mismatch",
                    "effect policy must equal the durable run policy binding",
                )
            required = approval_required(policy, risk_class)
            references = (approval_request_id, approval_decision_id)
            if required and any(value is None for value in references):
                raise ProtocolRefusal(
                    "effect_approval_required",
                    "repository risk policy requires exact effect approval",
                )
            if any(value is not None for value in references):
                if any(value is None for value in references):
                    raise ProtocolRefusal(
                        "effect_approval_incomplete",
                        "effect approval request and decision must remain paired",
                    )
                self.approvals.require_approved_action(
                    approval_request_id,
                    approval_decision_id,
                    requester=requested_by,
                    exact_action_digest=request_digest,
                    now=current,
                )
            expected_consumption = context["approval_consumption_id"]
            if (
                approval_consumption_id != expected_consumption
                or (
                    expected_consumption is not None
                    and (
                        approval_request_id
                        != context["approval_consumption_request_id"]
                        or approval_decision_id
                        != context["approval_consumption_decision_id"]
                        or request_digest
                        != context["approval_consumption_action_digest"]
                    )
                )
            ):
                raise ProtocolRefusal(
                    "effect_approval_consumption_mismatch",
                    "effect intent must repeat the exact current resume approval consumption",
                )
            operation_id = _semantic_operation_id(
                self.ledger.root.tenant_id, idempotency_key
            )
            timestamp = _effect_timestamp(current)
            record: dict[str, object] = {
                "schema_version": 1,
                "id": "effect-intent-" + uuid7_hex(),
                "tenant_id": self.ledger.root.tenant_id,
                "timestamp": timestamp,
                "kind": "effect_intent",
                "operation_id": operation_id,
                "run_id": run_id,
                "item_id": item_id,
                "attempt_id": attempt_id,
                "attempt_started_id": context["attempt_started_id"],
                "fence_token": context["fence_token"],
                "effect_type": effect_type,
                "target": deepcopy(target),
                "request_digest": request_digest,
                "idempotency_key": idempotency_key,
                "expected_confirmation": deepcopy(expected_confirmation),
                "reconciliation_adapter": reconciliation_adapter,
                "risk_class": risk_class,
                "budget_claim": deepcopy(budget_claim),
                "requested_by": requested_by,
                "approval_request_id": approval_request_id,
                "approval_decision_id": approval_decision_id,
                "approval_consumption_id": approval_consumption_id,
                "intended_at_testimony": timestamp,
            }
            validate_record(
                deepcopy(record), self.ledger.root.tenant_id, EFFECT_KINDS,
                integrity=False,
            )
            reservations = {
                row["budget_id"]: row["amount"]
                for row in context["budget_reservations"]
            }

            def authorize_claim(
                projection: EffectProjection, candidate: dict[str, object]
            ) -> Optional[dict[str, object]]:
                claim = {
                    row["budget_id"]: row["amount"]
                    for row in candidate["budget_claim"]
                }
                for budget_id, amount in claim.items():
                    policy_limit = budget_limit(policy, budget_id)
                    if amount > policy_limit:
                        raise ProtocolRefusal(
                            "effect_policy_budget_exceeded",
                            "effect claim exceeds repository policy budget",
                        )
                    if amount > reservations.get(budget_id, -1):
                        raise ProtocolRefusal(
                            "effect_run_budget_exceeded",
                            "effect claim exceeds durable run reservation",
                        )
                aggregate: dict[str, int] = {}
                for operation in projection._operations.values():
                    if operation["run_id"] != run_id:
                        continue
                    for budget_id, amount in _spend_rows(
                        operation["budget_claim"]
                    ) or ():
                        aggregate[budget_id] = aggregate.get(budget_id, 0) + amount
                for budget_id, amount in claim.items():
                    aggregate[budget_id] = aggregate.get(budget_id, 0) + amount
                if any(
                    amount > reservations.get(budget_id, -1)
                    for budget_id, amount in aggregate.items()
                ):
                    raise ProtocolRefusal(
                        "effect_run_budget_exceeded",
                        "aggregate effect claims exceed durable run reservation",
                    )
                return None

            return self._append_owned(record, authorize_claim)

    def _operation_binding(self, operation_id: object) -> dict[str, object]:
        if not isinstance(operation_id, str):
            raise ProtocolRefusal(
                "effect_operation_id_invalid", "effect operation id must be text"
            )
        operation = self.ledger.project()._operations.get(operation_id)
        if operation is None:
            raise ProtocolRefusal(
                "effect_operation_missing", "effect operation is absent"
            )
        return deepcopy(operation)

    def dispatched(
        self,
        operation_id: object,
        *,
        dispatch_adapter: object,
        dispatch_evidence_digest: object,
        now: Optional[datetime] = None,
    ) -> dict[str, object]:
        operation = self._operation_binding(operation_id)
        current = _effect_now(now)
        timestamp = _effect_timestamp(current)
        record = {
            "schema_version": 1,
            "id": "effect-dispatched-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": timestamp,
            "kind": "effect_dispatched",
            **deepcopy(operation["binding"]),
            "effect_intent_id": operation["intent_id"],
            "dispatch_adapter": dispatch_adapter,
            "dispatch_evidence_digest": dispatch_evidence_digest,
            "dispatched_at_testimony": timestamp,
        }
        return self._append_owned(record)

    def acknowledged(
        self,
        operation_id: object,
        *,
        acknowledgement_digest: object,
        now: Optional[datetime] = None,
    ) -> dict[str, object]:
        operation = self._operation_binding(operation_id)
        current = _effect_now(now)
        timestamp = _effect_timestamp(current)
        record = {
            "schema_version": 1,
            "id": "effect-acknowledged-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": timestamp,
            "kind": "effect_acknowledged",
            **deepcopy(operation["binding"]),
            "effect_intent_id": operation["intent_id"],
            "effect_dispatched_id": operation["dispatch_id"],
            "acknowledgement_digest": acknowledgement_digest,
            "acknowledged_at_testimony": timestamp,
        }
        return self._append_owned(record)

    def _negative_outcome(
        self,
        kind: str,
        operation_id: object,
        *,
        reason_code: object,
        evidence_digest: object,
        spend_status: object,
        measured_spend: object,
        now: Optional[datetime],
    ) -> dict[str, object]:
        try:
            caller = sys._getframe(1)
        except ValueError:
            caller = None
        if (
            caller is None
            or caller.f_code not in _EFFECT_CONTROLLER_NEGATIVE_CALLER_CODES
            or caller.f_locals.get("self") is not self
        ):
            raise ProtocolRefusal(
                "effect_controller_only",
                "negative effect rows require a public typed controller method",
            )
        operation = self._operation_binding(operation_id)
        current = _effect_now(now)
        timestamp = _effect_timestamp(current)
        failed = kind == "effect_failed"
        record = {
            "schema_version": 1,
            "id": ("effect-failed-" if failed else "effect-unknown-") + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": timestamp,
            "kind": kind,
            **deepcopy(operation["binding"]),
            "effect_intent_id": operation["intent_id"],
            "effect_dispatched_id": operation["dispatch_id"],
            "reason_code": reason_code,
            ("failure_evidence_digest" if failed else "unknown_evidence_digest"):
                evidence_digest,
            "spend_status": spend_status,
            "measured_spend": deepcopy(measured_spend),
            ("failed_at_testimony" if failed else "unknown_at_testimony"): timestamp,
        }
        return self._append_owned(record)

    def failed(
        self,
        operation_id: object,
        *,
        reason_code: object,
        evidence_digest: object,
        spend_status: object,
        measured_spend: object = None,
        now: Optional[datetime] = None,
    ) -> dict[str, object]:
        return self._negative_outcome(
            "effect_failed", operation_id, reason_code=reason_code,
            evidence_digest=evidence_digest, spend_status=spend_status,
            measured_spend=measured_spend, now=now,
        )

    def unknown(
        self,
        operation_id: object,
        *,
        reason_code: object,
        evidence_digest: object,
        spend_status: object,
        measured_spend: object = None,
        now: Optional[datetime] = None,
    ) -> dict[str, object]:
        return self._negative_outcome(
            "effect_unknown", operation_id, reason_code=reason_code,
            evidence_digest=evidence_digest, spend_status=spend_status,
            measured_spend=measured_spend, now=now,
        )

    def reconcile(
        self,
        operation_id: object,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, object]:
        operation = self._operation_binding(operation_id)
        if operation["state"] not in {
            "failed", "unknown", "reconciled_failed", "reconciled_unknown",
        }:
            raise ProtocolRefusal(
                "effect_transition_invalid",
                "reconciliation requires current failed or unknown effect evidence",
            )
        local_identity = self._local_reconciliation_identity(operation)
        request = build_request(
            operation_id=operation["operation_id"],
            current_evidence_id=operation["current_evidence_id"],
            adapter=operation["reconciliation_adapter"],
            target=deepcopy(operation["target"]),
            expected_confirmation=deepcopy(operation["expected_confirmation"]),
            budget_claim=dict(_spend_rows(operation["budget_claim"]) or ()),
            local_repository_identity=local_identity,
        )
        try:
            result = observe_effect_reconciliation(request)
        except Exception as exc:
            reason = next(
                (
                    candidate
                    for error_type, candidate in _RECONCILIATION_FAILURE_REASONS
                    if isinstance(exc, error_type)
                ),
                "observer_protocol_invalid",
            )
            result = self._parent_reconciliation_failure(request, reason)
        if type(result) is not ReconciliationResult:
            result = self._parent_reconciliation_failure(
                request, "observer_protocol_invalid",
            )
        selected = validate_result(result, request)
        return self._append_reconciliation_result(request, selected, now=now)

    @staticmethod
    def _local_reconciliation_identity(
        operation: Mapping[str, object],
    ) -> Optional[tuple[int, int]]:
        """Snapshot only the local repository identity carried into the launcher."""

        if operation["reconciliation_adapter"] != "git_local":
            return None
        target = operation["target"]
        coordinate = target.get("coordinate") if isinstance(target, Mapping) else None
        try:
            metadata = os.stat(coordinate) if isinstance(coordinate, str) else None
        except (OSError, TypeError, ValueError):
            metadata = None
        return (0, 1) if metadata is None else (metadata.st_dev, metadata.st_ino)

    @staticmethod
    def _parent_reconciliation_failure(
        request: ReconciliationRequest,
        reason_code: str,
    ) -> ReconciliationResult:
        """Construct a closed unknown result without retaining child testimony."""

        from .effect_reconciliation_protocol import build_result

        return build_result(
            request,
            outcome="unknown",
            reason_code=reason_code,
        )

    def _append_reconciliation_result(
        self,
        request: ReconciliationRequest,
        result: ReconciliationResult,
        *,
        now: Optional[datetime],
    ) -> dict[str, object]:
        """Refresh request-bound truth and append under the sole ledger transaction."""

        selected_request = build_request(
            operation_id=request.operation_id,
            current_evidence_id=request.current_evidence_id,
            adapter=request.adapter,
            target=deepcopy(request.target),
            expected_confirmation=deepcopy(request.expected_confirmation),
            budget_claim=deepcopy(request.budget_claim),
            local_repository_identity=request.local_repository_identity,
            request_id=request.request_id,
        )
        selected_result = validate_result(result, selected_request)
        captured_operation = self._operation_binding(selected_request.operation_id)
        request_truth = {
            "operation_id": selected_request.operation_id,
            "current_evidence_id": selected_request.current_evidence_id,
            "adapter": selected_request.adapter,
            "target": selected_request.target,
            "expected_confirmation": selected_request.expected_confirmation,
            "budget_claim": selected_request.budget_claim,
        }
        current = _effect_now(now)
        timestamp = _effect_timestamp(current)
        measured = (
            None
            if selected_result.measured_spend is None
            else [
                {"budget_id": budget_id, "amount": amount}
                for budget_id, amount in sorted(selected_result.measured_spend.items())
            ]
        )
        record = {
            "schema_version": 1,
            "id": "effect-reconciled-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": timestamp,
            "kind": "effect_reconciled",
            **deepcopy(captured_operation["binding"]),
            "effect_intent_id": captured_operation["intent_id"],
            "prior_effect_evidence_id": selected_request.current_evidence_id,
            "reconciled_outcome": selected_result.outcome,
            "reconciliation_evidence_digest": selected_result.evidence_digest,
            "confirmation": deepcopy(selected_result.confirmation),
            "spend_status": selected_result.spend_status,
            "measured_spend": measured,
            "reconciled_at_testimony": timestamp,
        }

        def refresh_and_resolve(
            projection: EffectProjection,
            pending: dict[str, object],
        ) -> Optional[dict[str, object]]:
            operation = projection._operations.get(selected_request.operation_id)
            if operation is None:
                raise ProtocolRefusal(
                    "effect_reconciliation_stale",
                    "reconciliation operation disappeared before append",
                )
            expected_binding = deepcopy(operation["binding"])

            exact_fields = (
                "operation_id", "effect_intent_id", "prior_effect_evidence_id",
                "reconciled_outcome", "reconciliation_evidence_digest",
                "confirmation", "spend_status", "measured_spend",
            )
            for prior in projection._records:
                if (
                    prior["kind"] == "effect_reconciled"
                    and all(prior[field] == pending[field] for field in exact_fields)
                    and _binding(prior) == expected_binding
                    and operation["current_evidence_id"] == prior["id"]
                ):
                    return deepcopy(prior)

            current_truth = {
                "operation_id": operation["operation_id"],
                "current_evidence_id": operation["current_evidence_id"],
                "adapter": operation["reconciliation_adapter"],
                "target": operation["target"],
                "expected_confirmation": operation["expected_confirmation"],
                "budget_claim": dict(_spend_rows(operation["budget_claim"]) or ()),
            }
            if (
                current_truth != request_truth
                or operation["state"] not in {
                    "failed", "unknown", "reconciled_failed", "reconciled_unknown",
                }
                or pending["effect_intent_id"] != operation["intent_id"]
                or _binding(pending) != expected_binding
            ):
                raise ProtocolRefusal(
                    "effect_reconciliation_stale",
                    "reconciliation request no longer matches current durable truth",
                )
            return None

        return self._append_owned(record, refresh_and_resolve)

    def compensation_preview(
        self,
        operation_id: object,
        *,
        reason_code: object,
        effect_type: object,
        target: object,
        request_digest: object,
        idempotency_key: object,
        expected_confirmation: object,
        reconciliation_adapter: object,
        risk_class: object,
        budget_claim: object,
        requested_by: object,
    ) -> dict[str, object]:
        operation = self._operation_binding(operation_id)
        if (
            operation["state"] not in _COMPENSATION_SOURCE_STATES
            or operation["compensation_state"] != "none"
        ):
            raise ProtocolRefusal(
                "effect_transition_invalid",
                "compensation preview requires uncompensated terminal effect truth",
            )
        if reason_code not in EFFECT_COMPENSATION_REASONS:
            raise ProtocolRefusal(
                "effect_input_invalid", "compensation reason is outside the closed set"
            )
        compensation_operation_id = _semantic_operation_id(
            self.ledger.root.tenant_id, idempotency_key
        )
        plan = {
            "plan_version": 1,
            "source_operation_id": operation["operation_id"],
            "source_effect_evidence_id": operation["current_evidence_id"],
            "reason_code": reason_code,
            "compensation_operation_id": compensation_operation_id,
            "run_id": operation["run_id"],
            "item_id": operation["item_id"],
            "attempt_id": operation["attempt_id"],
            "fence_token": operation["fence_token"],
            "effect_type": effect_type,
            "target": deepcopy(target),
            "request_digest": request_digest,
            "idempotency_key": idempotency_key,
            "expected_confirmation": deepcopy(expected_confirmation),
            "reconciliation_adapter": reconciliation_adapter,
            "risk_class": risk_class,
            "budget_claim": deepcopy(budget_claim),
            "requested_by": requested_by,
        }
        preview_timestamp = "2000-01-01T00:00:00.000Z"
        validate_record({
            "schema_version": 1,
            "id": "effect-intent-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": preview_timestamp,
            "kind": "effect_intent",
            "operation_id": compensation_operation_id,
            "run_id": operation["run_id"],
            "item_id": operation["item_id"],
            "attempt_id": operation["attempt_id"],
            "attempt_started_id": operation["attempt_started_id"],
            "fence_token": operation["fence_token"],
            "effect_type": effect_type,
            "target": deepcopy(target),
            "request_digest": request_digest,
            "idempotency_key": idempotency_key,
            "expected_confirmation": deepcopy(expected_confirmation),
            "reconciliation_adapter": reconciliation_adapter,
            "risk_class": risk_class,
            "budget_claim": deepcopy(budget_claim),
            "requested_by": requested_by,
            "approval_request_id": None,
            "approval_decision_id": None,
            "approval_consumption_id": None,
            "intended_at_testimony": preview_timestamp,
        }, self.ledger.root.tenant_id, EFFECT_KINDS, integrity=False)
        return {
            "plan": plan,
            "plan_digest": hashlib.sha256(_canonical_ijson(plan)).hexdigest(),
        }

    @_seal_effect_policy_bindings
    def compensation_confirm(
        self,
        operation_id: object,
        *,
        plan: object,
        plan_digest: object,
        approval_request_id: object = None,
        approval_decision_id: object = None,
        approval_consumption_id: object = None,
        now: Optional[datetime] = None,
    ) -> dict[str, dict[str, object]]:
        try:
            policy_call = sys._getframe(1)
            policy_body_code = sys._getframe(0).f_code
        except ValueError:
            policy_call = None
            policy_body_code = None
        policy_method = (
            policy_call.f_locals.get("method") if policy_call is not None else None
        )
        policy_operations = (
            policy_call.f_locals.get("original") if policy_call is not None else None
        )
        if (
            policy_call is None
            or policy_call.f_code is not _EFFECT_POLICY_SEAL_CODE
            or getattr(policy_method, "__code__", None) is not policy_body_code
            or not isinstance(policy_operations, tuple)
            or len(policy_operations) != 4
        ):
            raise ProtocolRefusal(
                "effect_policy_binding_tampered",
                "effect policy authority requires the sealed controller call path",
            )
        _, validate_policy, approval_required, budget_limit = policy_operations

        if not isinstance(plan, Mapping) or set(plan) != _COMPENSATION_PLAN_FIELDS:
            raise ProtocolRefusal(
                "effect_input_invalid", "compensation confirmation requires one exact plan"
            )
        selected_plan = deepcopy(dict(plan))
        expected_digest = hashlib.sha256(_canonical_ijson(selected_plan)).hexdigest()
        if plan_digest != expected_digest:
            raise ProtocolRefusal(
                "effect_evidence_invalid", "compensation plan digest does not match the plan"
            )
        operation = self._operation_binding(operation_id)
        if (
            selected_plan["source_operation_id"] != operation["operation_id"]
            or selected_plan["source_effect_evidence_id"] != operation["current_evidence_id"]
            or selected_plan["run_id"] != operation["run_id"]
            or selected_plan["item_id"] != operation["item_id"]
            or selected_plan["attempt_id"] != operation["attempt_id"]
            or selected_plan["fence_token"] != operation["fence_token"]
        ):
            raise ProtocolRefusal(
                "effect_evidence_invalid",
                "compensation plan no longer names the exact current source effect",
            )
        if selected_plan["reason_code"] not in EFFECT_COMPENSATION_REASONS:
            raise ProtocolRefusal(
                "effect_input_invalid", "compensation reason is outside the closed set"
            )
        if selected_plan["compensation_operation_id"] != _semantic_operation_id(
            self.ledger.root.tenant_id, selected_plan["idempotency_key"]
        ) or selected_plan["compensation_operation_id"] == operation["operation_id"]:
            raise ProtocolRefusal(
                "effect_evidence_invalid", "compensation operation identity is invalid"
            )
        if operation["state"] not in _COMPENSATION_SOURCE_STATES:
            raise ProtocolRefusal(
                "effect_transition_invalid", "compensation requires terminal source effect truth"
            )

        current = _effect_now(now)
        policy = validate_policy(self.policy)
        required = approval_required(policy, selected_plan["risk_class"])
        approval_pair = (approval_request_id, approval_decision_id)
        if required and any(value is None for value in approval_pair):
            raise ProtocolRefusal(
                "effect_approval_required",
                "repository risk policy requires exact compensation approval",
            )
        if any(value is not None for value in approval_pair):
            if any(value is None for value in approval_pair):
                raise ProtocolRefusal(
                    "effect_approval_incomplete",
                    "compensation approval request and decision must remain paired",
                )
            self.approvals.require_approved_action(
                approval_request_id,
                approval_decision_id,
                requester=selected_plan["requested_by"],
                exact_action_digest=selected_plan["request_digest"],
                now=current,
            )

        from .runtruth import effect_acceptance_guard

        with effect_acceptance_guard(self.ledger.root):
            context = (
                self.run_ledger._project_under_effect_acceptance_guard()
                .effect_intent_context(
                    selected_plan["run_id"], selected_plan["item_id"],
                    selected_plan["attempt_id"], selected_plan["fence_token"],
                )
            )
            if context["policy_digest"] != policy.digest:
                raise ProtocolRefusal(
                    "effect_policy_mismatch",
                    "effect policy must equal the durable run policy binding",
                )
            expected_consumption = context["approval_consumption_id"]
            if (
                approval_consumption_id != expected_consumption
                or (
                    expected_consumption is not None
                    and (
                        approval_request_id
                        != context["approval_consumption_request_id"]
                        or approval_decision_id
                        != context["approval_consumption_decision_id"]
                        or selected_plan["request_digest"]
                        != context["approval_consumption_action_digest"]
                    )
                )
            ):
                raise ProtocolRefusal(
                    "effect_approval_consumption_mismatch",
                    "compensation intent must repeat the exact resume approval consumption",
                )
            claim = dict(_spend_rows(selected_plan["budget_claim"]) or ())
            reservations = {
                row["budget_id"]: row["amount"]
                for row in context["budget_reservations"]
            }
            for budget_id, amount in claim.items():
                if amount > budget_limit(policy, budget_id):
                    raise ProtocolRefusal(
                        "effect_policy_budget_exceeded",
                        "compensation claim exceeds repository policy budget",
                    )
                if amount > reservations.get(budget_id, -1):
                    raise ProtocolRefusal(
                        "effect_run_budget_exceeded",
                        "compensation claim exceeds durable run reservation",
                    )

            operation = self._operation_binding(operation_id)
            if (
                selected_plan["source_operation_id"] != operation["operation_id"]
                or selected_plan["source_effect_evidence_id"]
                != operation["current_evidence_id"]
                or selected_plan["run_id"] != operation["run_id"]
                or selected_plan["item_id"] != operation["item_id"]
                or selected_plan["attempt_id"] != operation["attempt_id"]
                or selected_plan["fence_token"] != operation["fence_token"]
            ):
                raise ProtocolRefusal(
                    "effect_evidence_invalid",
                    "compensation plan no longer names exact fenced source truth",
                )
            projection = self.ledger.project()
            existing_operation_id = projection._idempotency.get(
                str(selected_plan["idempotency_key"])
            )
            if existing_operation_id not in {
                None, selected_plan["compensation_operation_id"],
            }:
                raise ProtocolRefusal(
                    "effect_idempotency_conflict",
                    "compensation idempotency key already binds another operation",
                )

            def authorize_claim(
                current_projection: EffectProjection,
                _candidate: dict[str, object],
            ) -> Optional[dict[str, object]]:
                aggregate: dict[str, int] = {}
                for candidate in current_projection._operations.values():
                    if (
                        candidate["run_id"] != selected_plan["run_id"]
                        or candidate["operation_id"]
                        == selected_plan["compensation_operation_id"]
                    ):
                        continue
                    for budget_id, amount in (
                        _spend_rows(candidate["budget_claim"]) or ()
                    ):
                        aggregate[budget_id] = aggregate.get(budget_id, 0) + amount
                for budget_id, amount in claim.items():
                    aggregate[budget_id] = aggregate.get(budget_id, 0) + amount
                if any(
                    amount > reservations.get(budget_id, -1)
                    for budget_id, amount in aggregate.items()
                ):
                    raise ProtocolRefusal(
                        "effect_run_budget_exceeded",
                        "aggregate compensation claims exceed durable run reservation",
                    )
                return None

            timestamp = _effect_timestamp(current)
            if operation["compensation_state"] == "none":
                proposal_record = {
                    "schema_version": 1,
                    "id": "compensation-proposed-" + uuid7_hex(),
                    "tenant_id": self.ledger.root.tenant_id,
                    "timestamp": timestamp,
                    "kind": "compensation_proposed",
                    **deepcopy(operation["binding"]),
                    "effect_intent_id": operation["intent_id"],
                    "source_effect_evidence_id": operation["current_evidence_id"],
                    "reason_code": selected_plan["reason_code"],
                    "compensation_plan_digest": expected_digest,
                    "compensation_request_digest": selected_plan["request_digest"],
                    "compensation_operation_id": selected_plan["compensation_operation_id"],
                    "compensation_risk_class": selected_plan["risk_class"],
                    "approval_request_id": approval_request_id,
                    "approval_decision_id": approval_decision_id,
                    "approval_consumption_id": approval_consumption_id,
                    "proposed_at_testimony": timestamp,
                }
                proposal = self._append_owned(proposal_record)
            elif (
                operation["compensation_state"] == "proposed"
                and operation["_compensation_operation_id"]
                == selected_plan["compensation_operation_id"]
                and operation["_compensation_request_digest"]
                == selected_plan["request_digest"]
            ):
                proposal = next(
                    row for row in self.ledger.records()
                    if row["id"] == operation["compensation_proposal_id"]
                )
                if (
                    proposal["compensation_plan_digest"] != expected_digest
                    or proposal["reason_code"] != selected_plan["reason_code"]
                    or proposal["compensation_risk_class"]
                    != selected_plan["risk_class"]
                    or proposal["approval_request_id"] != approval_request_id
                    or proposal["approval_decision_id"] != approval_decision_id
                    or proposal["approval_consumption_id"]
                    != approval_consumption_id
                ):
                    raise ProtocolRefusal(
                        "effect_evidence_invalid",
                        "compensation retry changes its proposal",
                    )
            else:
                raise ProtocolRefusal(
                    "effect_transition_invalid",
                    "source effect already has another compensation",
                )

            compensation_record = {
                "schema_version": 1,
                "id": "effect-intent-" + uuid7_hex(),
                "tenant_id": self.ledger.root.tenant_id,
                "timestamp": timestamp,
                "kind": "effect_intent",
                "operation_id": selected_plan["compensation_operation_id"],
                "run_id": selected_plan["run_id"],
                "item_id": selected_plan["item_id"],
                "attempt_id": selected_plan["attempt_id"],
                "attempt_started_id": context["attempt_started_id"],
                "fence_token": context["fence_token"],
                "effect_type": selected_plan["effect_type"],
                "target": deepcopy(selected_plan["target"]),
                "request_digest": selected_plan["request_digest"],
                "idempotency_key": selected_plan["idempotency_key"],
                "expected_confirmation": deepcopy(
                    selected_plan["expected_confirmation"]
                ),
                "reconciliation_adapter": selected_plan["reconciliation_adapter"],
                "risk_class": selected_plan["risk_class"],
                "budget_claim": deepcopy(selected_plan["budget_claim"]),
                "requested_by": selected_plan["requested_by"],
                "approval_request_id": approval_request_id,
                "approval_decision_id": approval_decision_id,
                "approval_consumption_id": approval_consumption_id,
                "intended_at_testimony": timestamp,
            }
            validate_record(
                deepcopy(compensation_record), self.ledger.root.tenant_id,
                EFFECT_KINDS, integrity=False,
            )
            compensation = self._append_owned(
                compensation_record, authorize_claim,
            )
            return {"proposal": proposal, "operation": compensation}

    def compensation_executed(
        self,
        operation_id: object,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, object]:
        operation = self._operation_binding(operation_id)
        if operation["compensation_state"] != "proposed":
            raise ProtocolRefusal(
                "effect_transition_invalid",
                "compensation execution requires an exact proposal",
            )
        compensation = self._operation_binding(operation["_compensation_operation_id"])
        if compensation["state"] not in {"confirmed", "reconciled_confirmed"}:
            raise ProtocolRefusal(
                "effect_transition_invalid",
                "compensation execution requires separately confirmed terminal evidence",
            )
        if compensation["request_digest"] != operation["_compensation_request_digest"]:
            raise ProtocolRefusal(
                "effect_evidence_invalid",
                "compensation operation no longer matches its proposal",
            )
        current = _effect_now(now)
        timestamp = _effect_timestamp(current)
        record = {
            "schema_version": 1,
            "id": "compensation-executed-" + uuid7_hex(),
            "tenant_id": self.ledger.root.tenant_id,
            "timestamp": timestamp,
            "kind": "compensation_executed",
            **deepcopy(operation["binding"]),
            "compensation_proposal_id": operation["compensation_proposal_id"],
            "compensation_operation_id": compensation["operation_id"],
            "compensation_terminal_evidence_id": compensation["current_evidence_id"],
            "executed_at_testimony": timestamp,
        }
        return self._append_owned(record)


_EFFECT_CONTROLLER_APPEND_CODE = EffectController._append_owned.__code__
_EFFECT_POLICY_SEAL_CODE = EffectController.__init__.__code__
_EFFECT_CONTROLLER_INIT_BODY_CODE = EffectController.__init__.__wrapped__.__code__
_EFFECT_CONTROLLER_SEALED_METHOD_CODES = frozenset({
    EffectController.intent.__wrapped__.__code__,
    EffectController.compensation_confirm.__wrapped__.__code__,
})
_EFFECT_CONTROLLER_DIRECT_METHOD_CODES = frozenset({
    EffectController.dispatched.__code__,
    EffectController.acknowledged.__code__,
    EffectController._negative_outcome.__code__,
    EffectController._append_reconciliation_result.__code__,
    EffectController.compensation_executed.__code__,
})
_EFFECT_CONTROLLER_NEGATIVE_CALLER_CODES = frozenset({
    EffectController.failed.__code__, EffectController.unknown.__code__,
})


def _worker_effect_operation(
    controller: object,
    context: Mapping[str, object],
    idempotency_key: object,
) -> dict[str, object]:
    """Resolve one worker report through fresh canonical Effect truth."""

    if type(controller) is not EffectController:
        raise ProtocolRefusal(
            "effect_controller_invalid",
            "worker effect reporting requires the exact EffectController",
        )
    if not isinstance(idempotency_key, str):
        raise ProtocolRefusal(
            "effect_pipe_event_invalid",
            "worker effect correlation requires a text idempotency key",
        )
    expected = (
        context["run_id"], context["item_id"], context["attempt_id"],
        context["fence_token"], idempotency_key,
    )
    matches = [
        operation
        for operation in controller.ledger.project()._operations.values()
        if (
            operation["run_id"], operation["item_id"],
            operation["attempt_id"], operation["fence_token"],
            operation["idempotency_key"],
        ) == expected
    ]
    if len(matches) != 1:
        raise ProtocolRefusal(
            "effect_pipe_operation_invalid",
            "worker effect report must resolve one exact durable operation",
        )
    return deepcopy(matches[0])


def _worker_uncertain_operations(
    controller: object,
    context: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Return freshly projected dispatched operations without terminal evidence."""

    if type(controller) is not EffectController:
        raise ProtocolRefusal(
            "effect_controller_invalid",
            "worker effect reporting requires the exact EffectController",
        )
    expected = (
        context["run_id"], context["item_id"], context["attempt_id"],
        context["fence_token"],
    )
    operations = [
        deepcopy(operation)
        for operation in controller.ledger.project()._operations.values()
        if (
            operation["run_id"], operation["item_id"],
            operation["attempt_id"], operation["fence_token"],
        ) == expected
        and operation["dispatch_id"] is not None
        and operation["primary_outcome_id"] is None
    ]
    operations.sort(key=lambda operation: str(operation["operation_id"]))
    return tuple(operations)


__all__ = [
    "EffectAcceptanceEvidence", "EffectController", "EffectLedger", "EffectProjection",
]

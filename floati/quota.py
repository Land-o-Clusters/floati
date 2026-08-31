"""Stamped provider quota facts and their append-only local receipts."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .records import validate_record
from .registry import utc_now
from .root import FloatiRoot, validate_identifier


QUOTA_RECEIPT_KINDS = frozenset({"quota_receipt_record"})
QUOTA_STATE_KINDS = frozenset({"consumed_fraction", "session_tokens", "unknown"})
QUOTA_STAMPS = frozenset({"MEASURED", "DERIVED", "ESTIMATE"})
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3})?Z$"
)


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _bounded_text(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= maximum
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _time(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None


@dataclass(frozen=True)
class QuotaState:
    kind: str
    value: Optional[str]

    def __post_init__(self) -> None:
        canonical: Optional[str] = None
        if self.kind == "unknown":
            if self.value is not None:
                raise ProtocolRefusal(
                    "quota_state_invalid", "unknown quota state cannot carry a number"
                )
        elif self.kind == "consumed_fraction":
            try:
                number = Decimal(str(self.value))
            except (InvalidOperation, ValueError):
                number = Decimal("NaN")
            if not number.is_finite() or number < 0 or number > 1:
                raise ProtocolRefusal(
                    "quota_state_invalid",
                    "consumed fraction must be finite and between zero and one",
                )
            canonical = f"{number:.6f}"
        elif self.kind == "session_tokens":
            try:
                number = Decimal(str(self.value))
            except (InvalidOperation, ValueError):
                number = Decimal("NaN")
            if (
                not number.is_finite()
                or number < 0
                or number != number.to_integral_value()
            ):
                raise ProtocolRefusal(
                    "quota_state_invalid",
                    "session tokens must be a non-negative integer",
                )
            canonical = str(int(number))
        else:
            raise ProtocolRefusal(
                "quota_state_invalid", "quota state kind is not ruled"
            )
        object.__setattr__(self, "value", canonical)

    def to_dict(self) -> Dict[str, object]:
        return {"kind": self.kind, "value": self.value}

    @classmethod
    def from_dict(cls, payload: object) -> "QuotaState":
        if not isinstance(payload, dict) or set(payload) != {"kind", "value"}:
            raise ProtocolRefusal("quota_state_invalid", "quota state is malformed")
        return cls(kind=payload["kind"], value=payload["value"])


@dataclass(frozen=True)
class QuotaFact:
    provider: str
    surface: str
    state: QuotaState
    stamp: str
    source: str
    evidence_digest: str
    observed_at: str
    resets_at: Optional[str]

    def __post_init__(self) -> None:
        try:
            provider = validate_identifier(self.provider, "provider")
        except ProtocolRefusal:
            provider = ""
        observed = _time(self.observed_at)
        reset = None if self.resets_at is None else _time(self.resets_at)
        if (
            provider != self.provider
            or not _bounded_text(self.surface, maximum=256)
            or not isinstance(self.state, QuotaState)
            or self.stamp not in QUOTA_STAMPS
            or not _bounded_text(self.source, maximum=4096)
            or not isinstance(self.evidence_digest, str)
            or _DIGEST.fullmatch(self.evidence_digest) is None
            or observed is None
            or (self.resets_at is not None and reset is None)
            or (reset is not None and observed is not None and reset < observed)
        ):
            raise ProtocolRefusal(
                "quota_fact_invalid",
                "quota fact requires a ruled provider, surface, state, stamp, source, digest, and UTC time",
            )

    def to_dict(self) -> Dict[str, object]:
        return {
            "provider": self.provider,
            "surface": self.surface,
            "state": self.state.to_dict(),
            "stamp": self.stamp,
            "source": self.source,
            "evidence_digest": self.evidence_digest,
            "observed_at": self.observed_at,
            "resets_at": self.resets_at,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "QuotaFact":
        fields = {
            "provider", "surface", "state", "stamp", "source",
            "evidence_digest", "observed_at", "resets_at",
        }
        if not isinstance(payload, dict) or set(payload) != fields:
            raise ProtocolRefusal("quota_fact_invalid", "quota fact is malformed")
        return cls(
            provider=payload["provider"],
            surface=payload["surface"],
            state=QuotaState.from_dict(payload["state"]),
            stamp=payload["stamp"],
            source=payload["source"],
            evidence_digest=payload["evidence_digest"],
            observed_at=payload["observed_at"],
            resets_at=payload["resets_at"],
        )


@dataclass(frozen=True)
class QuotaReceipt:
    schema_version: int
    provider: str
    endpoint_id: str
    facts: Tuple[QuotaFact, ...]
    idempotency_key: str
    receipt_digest: str

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        endpoint_id: str,
        facts: Sequence[QuotaFact],
        idempotency_key: str,
    ) -> "QuotaReceipt":
        selected = tuple(
            sorted(facts, key=lambda fact: (
                fact.surface, fact.observed_at, fact.evidence_digest
            ))
        )
        base: Dict[str, object] = {
            "schema_version": 0,
            "kind": "quota_receipt",
            "provider": provider,
            "endpoint_id": endpoint_id,
            "facts": [fact.to_dict() for fact in selected],
            "idempotency_key": idempotency_key,
        }
        return cls(
            schema_version=0,
            provider=provider,
            endpoint_id=endpoint_id,
            facts=selected,
            idempotency_key=idempotency_key,
            receipt_digest=_sha256(_canonical(base)),
        )

    def __post_init__(self) -> None:
        try:
            provider = validate_identifier(self.provider, "provider")
        except ProtocolRefusal:
            provider = ""
        facts_valid = all(isinstance(fact, QuotaFact) for fact in self.facts)
        ordered = tuple(sorted(
            self.facts,
            key=lambda fact: (fact.surface, fact.observed_at, fact.evidence_digest),
        )) if facts_valid else ()
        identities = tuple(
            (fact.surface, fact.observed_at)
            for fact in self.facts
            if isinstance(fact, QuotaFact)
        )
        expected = _sha256(_canonical(self._base_dict())) if ordered else ""
        if (
            self.schema_version != 0
            or isinstance(self.schema_version, bool)
            or provider != self.provider
            or not _bounded_text(self.endpoint_id, maximum=256)
            or not 1 <= len(self.facts) <= 16
            or not facts_valid
            or any(fact.provider != self.provider for fact in self.facts)
            or self.facts != ordered
            or len(set(identities)) != len(identities)
            or not _bounded_text(self.idempotency_key, maximum=128)
            or not isinstance(self.receipt_digest, str)
            or _DIGEST.fullmatch(self.receipt_digest) is None
            or self.receipt_digest != expected
        ):
            raise IntegrityFailure(
                "quota_receipt_invalid", "quota receipt is malformed or inconsistent"
            )

    def _base_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "quota_receipt",
            "provider": self.provider,
            "endpoint_id": self.endpoint_id,
            "facts": [fact.to_dict() for fact in self.facts],
            "idempotency_key": self.idempotency_key,
        }

    def to_dict(self) -> Dict[str, object]:
        return {**self._base_dict(), "receipt_digest": self.receipt_digest}

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8") + "\n"

    @classmethod
    def from_dict(cls, payload: object) -> "QuotaReceipt":
        fields = {
            "schema_version", "kind", "provider", "endpoint_id", "facts",
            "idempotency_key", "receipt_digest",
        }
        try:
            if not isinstance(payload, dict) or set(payload) != fields:
                raise IntegrityFailure("quota_receipt_invalid", "quota receipt is malformed")
            if payload["kind"] != "quota_receipt" or not isinstance(payload["facts"], list):
                raise IntegrityFailure("quota_receipt_invalid", "quota receipt is malformed")
            return cls(
                schema_version=payload["schema_version"],
                provider=payload["provider"],
                endpoint_id=payload["endpoint_id"],
                facts=tuple(QuotaFact.from_dict(fact) for fact in payload["facts"]),
                idempotency_key=payload["idempotency_key"],
                receipt_digest=payload["receipt_digest"],
            )
        except IntegrityFailure:
            raise
        except (KeyError, TypeError, ProtocolRefusal, ValueError) as exc:
            raise IntegrityFailure(
                "quota_receipt_invalid", "quota receipt is malformed"
            ) from exc


class QuotaLedger:
    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "quota_root_invalid", "quota ledger requires a validated root"
            )
        self.root = root

    @staticmethod
    def _relative(provider: str) -> Path:
        selected = validate_identifier(provider, "provider")
        return Path("receipts/quota") / f"{selected}.jsonl"

    @staticmethod
    def _receipt_from_row(row: Mapping[str, object]) -> QuotaReceipt:
        return QuotaReceipt.from_dict({
            "schema_version": row["schema_version"],
            "kind": "quota_receipt",
            "provider": row["provider"],
            "endpoint_id": row["endpoint_id"],
            "facts": row["facts"],
            "idempotency_key": row["idempotency_key"],
            "receipt_digest": row["receipt_digest"],
        })

    def append(self, receipt: QuotaReceipt) -> Dict[str, object]:
        if not isinstance(receipt, QuotaReceipt):
            raise ProtocolRefusal(
                "quota_receipt_invalid", "quota ledger accepts only validated receipts"
            )
        relative = self._relative(receipt.provider)
        row: Dict[str, object] = {
            "schema_version": 0,
            "id": "quota-receipt-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "quota_receipt_record",
            "provider": receipt.provider,
            "endpoint_id": receipt.endpoint_id,
            "facts": [fact.to_dict() for fact in receipt.facts],
            "idempotency_key": receipt.idempotency_key,
            "receipt_digest": receipt.receipt_digest,
        }
        semantic_fields = (
            "provider", "endpoint_id", "facts", "idempotency_key", "receipt_digest"
        )

        def decide(prior: list[Dict[str, object]]):
            existing = next(
                (
                    candidate for candidate in reversed(prior)
                    if candidate["idempotency_key"] == receipt.idempotency_key
                ),
                None,
            )
            if existing is not None:
                if all(existing[field] == row[field] for field in semantic_fields):
                    return existing, None
                raise ProtocolRefusal(
                    "quota_receipt_idempotency_conflict",
                    "quota receipt key has different semantic content",
                )
            validate_record(row, self.root.tenant_id, QUOTA_RECEIPT_KINDS, integrity=False)
            return row, row

        return transact(
            self.root,
            relative,
            decide,
            allowed_kinds=QUOTA_RECEIPT_KINDS,
        )

    def latest(self, provider: str) -> Optional[QuotaReceipt]:
        latest = self.latest_record(provider)
        return None if latest is None else latest[1]

    def latest_record(self, provider: str) -> Optional[Tuple[str, QuotaReceipt]]:
        rows = read_records_snapshot(
            self.root,
            self._relative(provider),
            allowed_kinds=QUOTA_RECEIPT_KINDS,
        )
        if not rows:
            return None
        return str(rows[-1]["id"]), self._receipt_from_row(rows[-1])


def require_schedulable_fraction(fact: QuotaFact) -> str:
    if not isinstance(fact, QuotaFact):
        raise ProtocolRefusal(
            "quota_fact_invalid", "quota scheduling requires a validated fact"
        )
    if fact.state.kind == "unknown":
        raise ProtocolRefusal(
            "quota_fact_unknown",
            f"quota is unknown; stamp={fact.stamp} source={fact.source}",
        )
    if fact.stamp == "ESTIMATE":
        raise ProtocolRefusal(
            "quota_fact_not_evaluable",
            f"quota estimate cannot schedule; stamp={fact.stamp} source={fact.source}",
        )
    if fact.state.kind != "consumed_fraction":
        raise ProtocolRefusal(
            "quota_fact_not_fractional",
            f"quota state {fact.state.kind} is not a consumed fraction",
        )
    if fact.stamp not in {"MEASURED", "DERIVED"} or fact.state.value is None:
        raise ProtocolRefusal(
            "quota_fact_not_evaluable",
            f"quota fact cannot schedule; stamp={fact.stamp} source={fact.source}",
        )
    return fact.state.value

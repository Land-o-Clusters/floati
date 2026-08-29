"""Post-v1 evidence-backed capability grants and physical lifecycle projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Type

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records, transact
from .policy import RepositoryPolicy, validate_repository_policy_integrity
from .planes import _cas_lock
from .records import capability_grant_digest
from .root import FloatiRoot, validate_identifier


GRANT_KINDS = frozenset({"capability_grant", "capability_revoked"})
REVOCATION_REASONS = frozenset({
    "operator_revoked", "authority_revoked", "worker_unregistered", "policy_replaced",
})


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise ProtocolRefusal("time_invalid", "persisted capability time is invalid") from exc


@dataclass(frozen=True)
class EffectiveCapabilitySet:
    """One physical grant-ledger observation for a worker and policy."""

    worker_id: str
    policy_digest: str
    evaluated_at_testimony: str
    high_watermark: int
    grant_triples: List[Tuple[str, str, int]]


class CapabilityGrantLedger:
    """Append-only v1 grant/revocation lifecycle; declarations never enter it."""

    relative_path = Path("capabilities/grants.jsonl")

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal("root_required", "capability grants require a writable FloatiRoot")
        self.root = root

    def records(self) -> List[Dict[str, object]]:
        return read_records(self.root, self.relative_path, allowed_kinds=set(GRANT_KINDS))

    def _project_records(
        self,
        records: List[Dict[str, object]],
        worker_id: str,
        policy_digest: str,
        evaluated_at: datetime,
        *,
        integrity: bool,
    ) -> EffectiveCapabilitySet:
        error = IntegrityFailure if integrity else ProtocolRefusal
        requests = read_records(
            self.root, "approvals/requests.jsonl", allowed_kinds={"approval_request"}
        )
        decisions = read_records(
            self.root, "approvals/decisions.jsonl", allowed_kinds={"approval_decision"}
        )
        _validate_persisted_grant_evidence(
            self.root, records, requests, decisions, error=error
        )
        return _project(
            records, worker_id, policy_digest, evaluated_at, integrity=integrity
        )

    def grant(
        self,
        worker_id: str,
        capability_name: str,
        policy: RepositoryPolicy,
        approval_request_id: str,
        approval_decision_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        worker = validate_identifier(worker_id, "worker_id")
        policy = validate_repository_policy_integrity(policy)
        if capability_name not in policy.capability_registry:
            raise ProtocolRefusal("capability_unregistered", "grant capability is outside capability_registry")
        current = _now(now)
        requests = read_records(
            self.root, "approvals/requests.jsonl", allowed_kinds={"approval_request"}
        )
        decisions = read_records(
            self.root, "approvals/decisions.jsonl", allowed_kinds={"approval_decision"}
        )
        request = next((row for row in requests if row["id"] == approval_request_id), None)
        decision = next((row for row in decisions if row["id"] == approval_decision_id), None)
        if request is None or decision is None or decision["request_id"] != approval_request_id:
            raise ProtocolRefusal("capability_approval_missing", "grant requires one exact request and decision")
        if request["requester"] != worker:
            raise ProtocolRefusal("capability_worker_mismatch", "approval requester must equal the grant worker")
        if request["capability"] != capability_name:
            raise ProtocolRefusal("capability_approval_mismatch", "approval capability must equal the grant capability")
        expected_scope = "worker:" + worker
        if request["scope"] != expected_scope or decision["granted_scope"] != expected_scope:
            raise ProtocolRefusal("capability_scope_mismatch", "v1 capability scope is the exact worker identity")
        if decision["decision"] != "approved" or decision["expires_at"] is None:
            raise ProtocolRefusal("capability_approval_denied", "only an approved decision can evidence a grant")
        if (
            decision["authority_subject"] != request["authority_subject"]
            or decision["authority_epoch"] != request["authority_epoch"]
        ):
            raise ProtocolRefusal("capability_authority_mismatch", "approval authority binding must remain exact")
        if current >= _parse_time(decision["expires_at"]):
            raise ProtocolRefusal("capability_approval_expired", "approved capability evidence has expired")
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "capability-grant-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "capability_grant",
            "worker_id": worker,
            "capability_name": capability_name,
            "policy_digest": policy.digest,
            "approval_request_id": approval_request_id,
            "approval_decision_id": approval_decision_id,
            "authority_subject": decision["authority_subject"],
            "authority_epoch": decision["authority_epoch"],
            "expires_at": decision["expires_at"],
        }
        record["grant_digest"] = capability_grant_digest(record)

        def choose(records: List[Dict[str, object]]):
            self._project_records(
                records, worker, policy.digest, current, integrity=True
            )
            _validate_persisted_grant_evidence(
                self.root,
                records + [record],
                requests,
                decisions,
                error=ProtocolRefusal,
            )
            return record, record

        authority_relative = Path("authority-grants") / f"{decision['authority_subject']}.jsonl"
        authority_path = self.root.resolve_relative(authority_relative)
        # Authority lifecycle writers use this same bounded CAS lock. Holding it
        # through the grant append makes authority closure physically before or
        # after the grant instead of a timestamp-shaped race.
        with _cas_lock(authority_path):
            authority_records = read_records(
                self.root, authority_relative, allowed_kinds={"authority_grant"}
            )
            if not authority_records:
                raise ProtocolRefusal("capability_authority_missing", "approval authority evidence is absent")
            authority = authority_records[-1]
            if (
                authority["epoch"] != decision["authority_epoch"]
                or authority["holder"] != decision["decider"]
            ):
                raise ProtocolRefusal("capability_authority_stale", "approval authority epoch or holder is no longer current")
            if (
                authority["state"] != "active"
                or current < _parse_time(authority["renewed_at"])
                or current >= _parse_time(authority["expires_at"])
            ):
                raise ProtocolRefusal("capability_authority_inactive", "approval authority is not active at grant creation")
            return transact(
                self.root, self.relative_path, choose, allowed_kinds=set(GRANT_KINDS)
            )

    def revoke(
        self,
        grant_id: str,
        reason_code: str,
        *,
        replacement_policy_digest: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        if reason_code not in REVOCATION_REASONS:
            raise ProtocolRefusal("revocation_reason_invalid", "revocation reason is outside the closed v1 set")
        if reason_code == "policy_replaced":
            if not isinstance(replacement_policy_digest, str) or len(replacement_policy_digest) != 64 or any(
                character not in "0123456789abcdef" for character in replacement_policy_digest
            ):
                raise ProtocolRefusal(
                    "replacement_policy_digest_required",
                    "policy_replaced requires the replacing lowercase SHA-256 digest",
                )
        elif replacement_policy_digest is not None:
            raise ProtocolRefusal(
                "replacement_policy_digest_invalid",
                "only policy_replaced can name a replacement policy digest",
            )
        current = _now(now)
        record: Dict[str, object] = {
            "schema_version": 1,
            "id": "capability-revoked-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "capability_revoked",
            "grant_id": grant_id,
            "reason_code": reason_code,
            "replacement_policy_digest": replacement_policy_digest,
        }

        def choose(records: List[Dict[str, object]]):
            self._project_records(
                records, "projection-audit", "0" * 64, current, integrity=True
            )
            grants = {
                str(row["id"]): row for row in records if row["kind"] == "capability_grant"
            }
            if grant_id not in grants:
                raise ProtocolRefusal("capability_grant_unknown", "revocation must name a prior physical grant")
            if any(row["kind"] == "capability_revoked" and row["grant_id"] == grant_id for row in records):
                raise ProtocolRefusal("capability_already_revoked", "a capability grant can be revoked once")
            if (
                reason_code == "policy_replaced"
                and replacement_policy_digest == grants[grant_id]["policy_digest"]
            ):
                raise ProtocolRefusal(
                    "replacement_policy_digest_unchanged",
                    "policy_replaced must name a different replacing policy digest",
                )
            return record, record

        return transact(
            self.root, self.relative_path, choose, allowed_kinds=set(GRANT_KINDS)
        )

    def effective(
        self,
        worker_id: str,
        policy_digest: str,
        evaluated_at: Optional[datetime] = None,
    ) -> EffectiveCapabilitySet:
        worker = validate_identifier(worker_id, "worker_id")
        if not isinstance(policy_digest, str) or len(policy_digest) != 64 or any(
            character not in "0123456789abcdef" for character in policy_digest
        ):
            raise ProtocolRefusal("policy_digest_invalid", "policy digest must be lowercase SHA-256")
        current = _now(evaluated_at)
        return self._project_records(
            self.records(), worker, policy_digest, current, integrity=True
        )


def _unique_by_id(
    records: List[Dict[str, object]],
    *,
    duplicate_code: str,
    error: Type[Exception],
) -> Dict[str, Dict[str, object]]:
    indexed: Dict[str, Dict[str, object]] = {}
    for record in records:
        record_id = str(record["id"])
        if record_id in indexed:
            raise error(duplicate_code, "durable approval evidence contains a duplicate id")
        indexed[record_id] = record
    return indexed


def _validate_persisted_grant_evidence(
    root: FloatiRoot,
    grant_records: List[Dict[str, object]],
    request_records: List[Dict[str, object]],
    decision_records: List[Dict[str, object]],
    *,
    error: Type[Exception],
) -> None:
    requests = _unique_by_id(
        request_records, duplicate_code="capability_approval_request_duplicate", error=error
    )
    decisions = _unique_by_id(
        decision_records, duplicate_code="capability_approval_decision_duplicate", error=error
    )
    decision_requests: Dict[str, str] = {}
    for decision in decision_records:
        request_id = str(decision["request_id"])
        if request_id in decision_requests:
            raise error(
                "capability_approval_request_redecided",
                "one approval request cannot have multiple durable decisions",
            )
        decision_requests[request_id] = str(decision["id"])
    used_decisions: Dict[str, str] = {}
    authority_cache: Dict[str, List[Dict[str, object]]] = {}

    def refuse(code: str, detail: str) -> None:
        raise error(code, detail)

    for grant in (row for row in grant_records if row["kind"] == "capability_grant"):
        request = requests.get(str(grant["approval_request_id"]))
        decision = decisions.get(str(grant["approval_decision_id"]))
        if request is None or decision is None:
            refuse(
                "capability_approval_missing",
                "persisted grant requires its exact durable request and decision",
            )
        if decision["request_id"] != request["id"]:
            refuse(
                "capability_approval_missing",
                "persisted decision must name the grant's exact approval request",
            )
        if request["requester"] != grant["worker_id"]:
            refuse(
                "capability_worker_mismatch",
                "persisted approval requester must equal the grant worker",
            )
        if request["capability"] != grant["capability_name"]:
            refuse(
                "capability_approval_mismatch",
                "persisted approval capability must equal the grant capability",
            )
        if decision["decision"] != "approved" or decision["expires_at"] is None:
            refuse(
                "capability_approval_denied",
                "denied approval evidence cannot authorize a persisted grant",
            )
        expected_scope = "worker:" + str(grant["worker_id"])
        if request["scope"] != expected_scope or decision["granted_scope"] != expected_scope:
            refuse(
                "capability_scope_mismatch",
                "persisted capability approval must retain exact worker scope",
            )
        if decision["expires_at"] != grant["expires_at"]:
            refuse(
                "capability_expiry_mismatch",
                "persisted grant expiry must equal its approval decision expiry",
            )
        if (
            request["authority_subject"] != grant["authority_subject"]
            or decision["authority_subject"] != grant["authority_subject"]
            or request["authority_epoch"] != grant["authority_epoch"]
            or decision["authority_epoch"] != grant["authority_epoch"]
        ):
            refuse(
                "capability_authority_mismatch",
                "persisted approval and grant authority bindings must remain exact",
            )
        decision_id = str(decision["id"])
        prior_grant = used_decisions.get(decision_id)
        if prior_grant is not None:
            refuse(
                "capability_approval_reused",
                "one approval decision cannot authorize multiple physical grants",
            )
        used_decisions[decision_id] = str(grant["id"])

        subject = str(grant["authority_subject"])
        authority_records = authority_cache.get(subject)
        if authority_records is None:
            authority_records = read_records(
                root,
                Path("authority-grants") / f"{subject}.jsonl",
                allowed_kinds={"authority_grant"},
            )
            authority_cache[subject] = authority_records
        matching_frames = [
            frame
            for frame in authority_records
            if frame["epoch"] == grant["authority_epoch"]
            and frame["holder"] == decision["decider"]
            and frame["state"] == "active"
        ]
        request_time = _parse_time(request["requested_at"])
        request_expiry = _parse_time(request["expires_at"])
        decision_time = _parse_time(decision["decided_at"])
        decision_expiry = _parse_time(decision["expires_at"])
        if request_expiry != request_time + timedelta(
            seconds=int(request["requested_ttl_seconds"])
        ):
            refuse(
                "capability_approval_lifetime_invalid",
                "persisted request expiry must equal its declared TTL",
            )
        if (
            int(decision["granted_ttl_seconds"]) > int(request["requested_ttl_seconds"])
            or decision_expiry
            != decision_time + timedelta(seconds=int(decision["granted_ttl_seconds"]))
        ):
            refuse(
                "capability_approval_lifetime_invalid",
                "persisted decision expiry must equal its bounded declared TTL",
            )
        if decision_time < request_time or decision_time >= request_expiry or decision_expiry > request_expiry:
            refuse(
                "capability_approval_lifetime_invalid",
                "persisted decision must remain within its approval request lifetime",
            )
        request_authority = any(
            _parse_time(frame["renewed_at"]) <= request_time
            and request_expiry <= _parse_time(frame["expires_at"])
            and _parse_time(frame["expires_at"])
            == _parse_time(frame["renewed_at"])
            + timedelta(seconds=int(frame["ttl_seconds"]))
            for frame in matching_frames
        )
        decision_authority = any(
            _parse_time(frame["renewed_at"]) <= decision_time
            and decision_time < _parse_time(frame["expires_at"])
            and decision_expiry <= _parse_time(frame["expires_at"])
            and _parse_time(frame["expires_at"])
            == _parse_time(frame["renewed_at"])
            + timedelta(seconds=int(frame["ttl_seconds"]))
            for frame in matching_frames
        )
        if not request_authority or not decision_authority:
            refuse(
                "capability_authority_missing",
                "persisted grant lacks an active authority frame covering its approval",
            )


def _project(
    records: List[Dict[str, object]],
    worker_id: str,
    policy_digest: str,
    evaluated_at: datetime,
    *,
    integrity: bool = False,
) -> EffectiveCapabilitySet:
    error = IntegrityFailure if integrity else ProtocolRefusal

    def refuse(code: str, detail: str) -> None:
        raise error(code, detail)

    if not isinstance(policy_digest, str) or len(policy_digest) != 64 or any(
        character not in "0123456789abcdef" for character in policy_digest
    ):
        refuse("policy_digest_invalid", "policy digest must be lowercase SHA-256")
    grants: Dict[str, Tuple[Dict[str, object], int]] = {}
    revoked = set()
    for position, record in enumerate(records, start=1):
        if record["kind"] == "capability_grant":
            grants[str(record["id"])] = (record, position)
        else:
            grant_id = str(record["grant_id"])
            if grant_id not in grants:
                refuse("capability_revocation_forward", "revocation must follow its physical grant")
            if grant_id in revoked:
                refuse("capability_revocation_duplicate", "grant has multiple physical revocations")
            revoked.add(grant_id)
    triples = sorted(
        (
            str(record["capability_name"]),
            grant_id,
            position,
        )
        for grant_id, (record, position) in grants.items()
        if grant_id not in revoked
        and record["worker_id"] == worker_id
        and record["policy_digest"] == policy_digest
        and evaluated_at < _parse_time(record["expires_at"])
    )
    return EffectiveCapabilitySet(
        worker_id=worker_id,
        policy_digest=policy_digest,
        evaluated_at_testimony=_timestamp(evaluated_at),
        high_watermark=len(records),
        grant_triples=triples,
    )


__all__ = [
    "CapabilityGrantLedger", "EffectiveCapabilitySet", "capability_grant_digest",
]

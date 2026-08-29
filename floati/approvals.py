"""Dark capability declarations and authority-bound approval receipts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import append_record, read_records, transact
from .records import _sha256, validate_record
from .registry import Registry
from .root import FloatiRoot, validate_identifier


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return current.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_time(value: object) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _ttl(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 86400:
        raise ProtocolRefusal("ttl_invalid", "TTL must be 1 through 86400 seconds")
    return value


def _action_digest(value: object) -> str:
    def refuse(code: str, detail: str) -> None:
        raise ProtocolRefusal(code, detail)

    _sha256(value, "exact_action_digest", refuse)
    return value  # type: ignore[return-value]


class CapabilityLedger:
    def __init__(self, root: FloatiRoot, registry: Optional[Registry] = None) -> None:
        self.root = root
        self.registry = registry or Registry(root)
        self.relative_path = Path("capabilities/records.jsonl")

    def declare(
        self,
        node_id: str,
        capability: str,
        mode: str,
        scope: str,
        ttl_seconds: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        self.registry.require_active(node_id)
        current = _now(now)
        ttl = _ttl(ttl_seconds)
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "capability-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "capability",
            "node_id": node_id,
            "capability": capability,
            "mode": mode,
            "scope": scope,
            "observed_at": _timestamp(current),
            "expires_at": _timestamp(current + timedelta(seconds=ttl)),
        }
        append_record(self.root, self.relative_path, record, allowed_kinds={"capability"})
        return record

    def current(self, node_id: str, capability: str, now: Optional[datetime] = None) -> Dict[str, object]:
        current = _now(now)
        records = read_records(self.root, self.relative_path, allowed_kinds={"capability"})
        selected = [
            record for record in records
            if record["node_id"] == node_id and record["capability"] == capability
        ]
        if not selected:
            raise ProtocolRefusal("capability_unknown", "capability has no declared evidence")
        result = dict(selected[-1])
        result["status"] = "expired" if current >= _parse_time(result["expires_at"]) else "current"
        return result


class ApprovalLedger:
    def __init__(self, root: FloatiRoot, registry: Optional[Registry] = None) -> None:
        self.root = root
        self.registry = registry or Registry(root)
        self.request_path = Path("approvals/requests.jsonl")
        self.decision_path = Path("approvals/decisions.jsonl")

    def request(
        self,
        requester: str,
        capability: str,
        scope: str,
        ttl_seconds: int,
        authority_subject: str,
        authority_epoch: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        return self._request(
            requester,
            capability,
            scope,
            ttl_seconds,
            authority_subject,
            authority_epoch,
            now=now,
        )

    def request_for_action(
        self,
        requester: str,
        capability: str,
        scope: str,
        ttl_seconds: int,
        exact_action_digest: str,
        authority_subject: str,
        authority_epoch: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        digest = _action_digest(exact_action_digest)
        return self._request(
            requester,
            capability,
            scope,
            ttl_seconds,
            authority_subject,
            authority_epoch,
            exact_action_digest=digest,
            now=now,
        )

    def _request(
        self,
        requester: str,
        capability: str,
        scope: str,
        ttl_seconds: int,
        authority_subject: str,
        authority_epoch: int,
        *,
        exact_action_digest: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        self.registry.require_active(requester)
        current = _now(now)
        ttl = _ttl(ttl_seconds)
        subject = validate_identifier(authority_subject, "authority_subject")
        grant = self._authority(subject, authority_epoch, current)
        expires = current + timedelta(seconds=ttl)
        if expires > _parse_time(grant["expires_at"]):
            raise ProtocolRefusal("approval_ttl_exceeds_authority", "request cannot outlive its authority grant")
        record: Dict[str, object] = {
            "schema_version": 1 if exact_action_digest is not None else 0,
            "id": "approval-request-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "approval_request",
            "requester": requester,
            "capability": capability,
            "scope": scope,
            "requested_ttl_seconds": ttl,
            "requested_at": _timestamp(current),
            "expires_at": _timestamp(expires),
            "authority_subject": subject,
            "authority_epoch": authority_epoch,
        }
        if exact_action_digest is not None:
            record["exact_action_digest"] = _action_digest(exact_action_digest)
        append_record(self.root, self.request_path, record, allowed_kinds={"approval_request"})
        return record

    def decide(
        self,
        request_id: str,
        decider: str,
        decision: str,
        reason_code: Optional[str],
        *,
        granted_scope: Optional[str] = None,
        granted_ttl_seconds: Optional[int] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        self.registry.require_active(decider)
        current = _now(now)
        requests = read_records(self.root, self.request_path, allowed_kinds={"approval_request"})
        request = next((item for item in requests if item["id"] == request_id), None)
        if request is None:
            raise ProtocolRefusal("approval_request_unknown", "approval request does not exist")
        if current >= _parse_time(request["expires_at"]):
            raise ProtocolRefusal("approval_request_expired", "approval request has expired")
        grant = self._authority(str(request["authority_subject"]), int(request["authority_epoch"]), current)
        if grant["holder"] != decider:
            raise ProtocolRefusal("authority_holder_mismatch", "only the exact authority holder may decide")

        expires_at: Optional[str] = None
        if decision == "approved":
            if granted_scope != request["scope"]:
                raise ProtocolRefusal("approval_scope_broadened", "v0 approvals require an exact requested scope")
            if not isinstance(granted_ttl_seconds, int) or isinstance(granted_ttl_seconds, bool):
                raise ProtocolRefusal("approval_ttl_invalid", "approved decisions require a positive grant TTL")
            ttl = _ttl(granted_ttl_seconds)
            expiry = current + timedelta(seconds=ttl)
            if ttl > request["requested_ttl_seconds"] or expiry > _parse_time(request["expires_at"]) or expiry > _parse_time(grant["expires_at"]):
                raise ProtocolRefusal("approval_ttl_broadened", "approved grant cannot outlive request or authority")
            if reason_code is not None:
                raise ProtocolRefusal("reason_code_invalid", "approved decisions cannot carry denial reasons")
            expires_at = _timestamp(expiry)
        elif decision == "denied":
            if granted_scope is not None or granted_ttl_seconds is not None:
                raise ProtocolRefusal("denial_grant_invalid", "denied decisions cannot carry grant fields")
        else:
            raise ProtocolRefusal("decision_invalid", "decision must be approved or denied")

        record: Dict[str, object] = {
            "schema_version": request["schema_version"],
            "id": "approval-decision-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(current),
            "kind": "approval_decision",
            "request_id": request_id,
            "decider": decider,
            "decision": decision,
            "granted_scope": granted_scope,
            "granted_ttl_seconds": granted_ttl_seconds,
            "reason_code": reason_code,
            "decided_at": _timestamp(current),
            "expires_at": expires_at,
            "authority_subject": request["authority_subject"],
            "authority_epoch": request["authority_epoch"],
        }
        if request["schema_version"] == 1:
            record["exact_action_digest"] = request["exact_action_digest"]
        validate_record(record, self.root.tenant_id, frozenset({"approval_decision"}), integrity=False)

        def choose(records: List[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            if any(item["request_id"] == request_id for item in records):
                raise ProtocolRefusal("approval_already_decided", "approval request already has a decision")
            return record, record

        return transact(self.root, self.decision_path, choose, allowed_kinds={"approval_decision"})

    def require_approved_action(
        self,
        request_id: object,
        decision_id: object,
        *,
        requester: object,
        exact_action_digest: object,
        now: Optional[datetime] = None,
    ) -> tuple[Dict[str, object], Dict[str, object]]:
        """Read and validate one exact schema-v1 action approval pair."""

        current = _now(now)
        requests = read_records(
            self.root, self.request_path, allowed_kinds={"approval_request"}
        )
        decisions = read_records(
            self.root, self.decision_path, allowed_kinds={"approval_decision"}
        )
        request = next((row for row in requests if row["id"] == request_id), None)
        decision = next((row for row in decisions if row["id"] == decision_id), None)
        if request is None or decision is None:
            raise ProtocolRefusal(
                "effect_approval_missing",
                "effect approval requires its exact durable request and decision",
            )
        digest = _action_digest(exact_action_digest)
        if (
            request["schema_version"] != 1
            or request.get("exact_action_digest") != digest
            or request.get("requester") != requester
            or decision["schema_version"] != 1
            or decision.get("request_id") != request["id"]
            or decision.get("exact_action_digest") != digest
            or decision.get("granted_scope") != request.get("scope")
            or decision.get("authority_subject")
            != request.get("authority_subject")
            or decision.get("authority_epoch") != request.get("authority_epoch")
            or _parse_time(decision["decided_at"])
            < _parse_time(request["requested_at"])
        ):
            raise ProtocolRefusal(
                "effect_approval_action_mismatch",
                "effect approval must bind the exact requester and action digest",
            )
        if decision.get("decision") != "approved":
            raise ProtocolRefusal(
                "effect_approval_not_approved",
                "effect approval decision must be approved",
            )
        expiry = decision.get("expires_at")
        if (
            expiry is None
            or current >= _parse_time(expiry)
            or _parse_time(expiry) > _parse_time(request["expires_at"])
        ):
            raise ProtocolRefusal(
                "effect_approval_expired",
                "effect approval decision has expired",
            )
        return dict(request), dict(decision)

    def _authority(self, subject: str, epoch: int, current: datetime) -> Dict[str, object]:
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
            raise ProtocolRefusal("authority_epoch_invalid", "authority epoch must be a positive integer")
        records = read_records(
            self.root,
            Path("authority-grants") / f"{subject}.jsonl",
            allowed_kinds={"authority_grant"},
        )
        if not records:
            raise ProtocolRefusal("authority_missing", "approval authority does not exist")
        grant = records[-1]
        if grant["epoch"] != epoch:
            raise ProtocolRefusal("authority_epoch_mismatch", "approval authority epoch does not match")
        if grant["state"] != "active" or current >= _parse_time(grant["expires_at"]):
            raise ProtocolRefusal("authority_inactive", "approval authority is not active")
        return grant

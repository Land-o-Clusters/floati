"""Dark managed-session adoption records for a future opt-in seam of a
consuming observer app."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records, transact
from .planes import AuthorityGrantStore
from .registry import Registry
from .root import FloatiRoot, validate_identifier


KINDS = {"session_adoption", "session_release"}
LEDGER = Path("managed/sessions.jsonl")


def _utc(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "managed-session time must include a UTC offset")
    return current.astimezone(timezone.utc)


def _format(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _project(records: list[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    projected: Dict[str, Dict[str, object]] = {}
    for record in records:
        session_id = str(record["session_id"])
        if record["kind"] == "session_adoption":
            prior = projected.get(session_id)
            if prior is not None and prior["state"] == "adopted":
                raise IntegrityFailure(
                    "managed_transition_invalid",
                    f"session {session_id} has two active adoptions",
                )
            projected[session_id] = {
                "session_id": session_id,
                "mode": "MANAGED",
                "state": "adopted",
                "manager_node_id": record["manager_node_id"],
                "lease_subject": record["lease_subject"],
                "lease_epoch": record["lease_epoch"],
                "lease_expires_at": record["lease_expires_at"],
                "adoption_id": record["id"],
                "release_id": None,
            }
            continue
        prior = projected.get(session_id)
        if (
            prior is None
            or prior["state"] != "adopted"
            or prior["adoption_id"] != record["adoption_id"]
            or prior["manager_node_id"] != record["manager_node_id"]
            or prior["lease_subject"] != record["lease_subject"]
            or prior["lease_epoch"] != record["lease_epoch"]
        ):
            raise IntegrityFailure(
                "managed_transition_invalid",
                f"release for session {session_id} does not bind its active adoption",
            )
        prior = dict(prior)
        prior["state"] = "released"
        prior["release_id"] = record["id"]
        projected[session_id] = prior
    return projected


class ManagedSessions:
    """Append and project the future Managed Mode opt-in contract."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root

    def project(self) -> Dict[str, Dict[str, object]]:
        return _project(read_records(self.root, LEDGER, allowed_kinds=KINDS))

    def adopt(
        self,
        session_id: str,
        manager_node_id: str,
        lease_subject: str,
        lease_epoch: int,
        lease_expires_at: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        session = validate_identifier(session_id, "session")
        manager = validate_identifier(manager_node_id, "manager_node")
        subject = validate_identifier(lease_subject, "lease_subject")
        if not isinstance(lease_epoch, int) or isinstance(lease_epoch, bool) or lease_epoch < 1:
            raise ProtocolRefusal("lease_epoch_invalid", "lease epoch must be a positive integer")
        current = _utc(now)
        Registry(self.root).require_active(manager)
        lease_path = AuthorityGrantStore(self.root).path_for(subject)
        lease_records = read_records(
            self.root,
            lease_path.relative_to(self.root.tenant_home),
            allowed_kinds={"authority_grant"},
        )
        lease = lease_records[-1] if lease_records else None
        if (
            lease is None
            or lease.get("holder") != manager
            or lease.get("epoch") != lease_epoch
            or lease.get("state") != "active"
            or lease.get("expires_at") != lease_expires_at
        ):
            raise ProtocolRefusal(
                "managed_lease_mismatch",
                "managed adoption must bind the manager's exact active authority lease",
            )
        if current >= _parse(str(lease_expires_at)):
            raise ProtocolRefusal("managed_lease_expired", "managed adoption lease is expired")
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "adoption-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": _format(current),
            "kind": "session_adoption",
            "session_id": session,
            "mode": "MANAGED",
            "manager_node_id": manager,
            "lease_subject": subject,
            "lease_epoch": lease_epoch,
            "lease_expires_at": lease_expires_at,
        }

        def decide(records: list[Dict[str, object]]):
            prior = _project(records).get(session)
            if prior is not None and prior["state"] == "adopted":
                raise ProtocolRefusal(
                    "managed_session_already_adopted",
                    f"session {session} already has an active manager",
                )
            return record, record

        return transact(self.root, LEDGER, decide, allowed_kinds=KINDS)

    def release(
        self,
        session_id: str,
        manager_node_id: str,
        lease_epoch: int,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        session = validate_identifier(session_id, "session")
        manager = validate_identifier(manager_node_id, "manager_node")
        if not isinstance(lease_epoch, int) or isinstance(lease_epoch, bool) or lease_epoch < 1:
            raise ProtocolRefusal("lease_epoch_invalid", "lease epoch must be a positive integer")
        timestamp = _format(_utc(now))

        def decide(records: list[Dict[str, object]]):
            active = _project(records).get(session)
            if (
                active is None
                or active["state"] != "adopted"
                or active["manager_node_id"] != manager
                or active["lease_epoch"] != lease_epoch
            ):
                raise ProtocolRefusal(
                    "managed_release_mismatch",
                    "release must bind the exact active adoption manager and lease epoch",
                )
            record: Dict[str, object] = {
                "schema_version": 0,
                "id": "release-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": timestamp,
                "kind": "session_release",
                "session_id": session,
                "adoption_id": active["adoption_id"],
                "manager_node_id": manager,
                "lease_subject": active["lease_subject"],
                "lease_epoch": lease_epoch,
            }
            return record, record

        return transact(self.root, LEDGER, decide, allowed_kinds=KINDS)

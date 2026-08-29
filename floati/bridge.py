"""Dark local-only bilateral bridge contracts; never a transport or consumer."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import append_record, read_records_snapshot
from .registry import Registry
from .root import FloatiRoot, validate_identifier


BRIDGE_KINDS = {"bridge_consent", "bridge_record", "bridge_forward", "bridge_denial"}
CONSENTS = Path("bridges/consents.jsonl")
RECORDS = Path("bridges/records.jsonl")
FORWARDS = Path("bridges/forwards.jsonl")
DENIALS = Path("bridges/denials.jsonl")


def _utc(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "bridge time must include a UTC offset")
    return current.astimezone(timezone.utc)


def _timestamp(value: Optional[datetime]) -> str:
    return _utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LocalBridgeV0:
    def __init__(self, left: FloatiRoot, right: FloatiRoot) -> None:
        if not isinstance(left, FloatiRoot) or not isinstance(right, FloatiRoot):
            raise TypeError("bridge endpoints must be validated local FloatiRoot values")
        self.left = left
        self.right = right
        self.bridge_id = "bridge-" + uuid7_hex()

    def _same(self) -> bool:
        return self.left.path == self.right.path and self.left.tenant_id == self.right.tenant_id

    def _pair(self, root: FloatiRoot, peer: FloatiRoot) -> tuple[FloatiRoot, FloatiRoot]:
        if root.path == self.left.path and peer.path == self.right.path:
            return self.left, self.right
        if root.path == self.right.path and peer.path == self.left.path:
            return self.right, self.left
        self._refuse(self.left, self.right, "bridge_root_unknown")

    def _direction(self, source: FloatiRoot, destination: FloatiRoot) -> str:
        if source.path == self.left.path and destination.path == self.right.path:
            return "left_to_right"
        if source.path == self.right.path and destination.path == self.left.path:
            return "right_to_left"
        return "invalid"

    def _refuse(self, source: FloatiRoot, destination: FloatiRoot, code: str, *, now: Optional[datetime] = None):
        timestamp = _timestamp(now)
        seen = set()
        for root in (source, destination):
            identity = (root.path, root.tenant_id)
            if identity in seen:
                continue
            seen.add(identity)
            record: Dict[str, object] = {
                "schema_version": 0,
                "id": "bridge-denial-" + uuid7_hex(),
                "tenant_id": root.tenant_id,
                "timestamp": timestamp,
                "kind": "bridge_denial",
                "bridge_id": self.bridge_id,
                "source_tenant_id": source.tenant_id,
                "destination_tenant_id": destination.tenant_id,
                "direction": self._direction(source, destination),
                "reason_code": code,
                "stamp": "advisory_not_consumption",
            }
            append_record(root, DENIALS, record, allowed_kinds={"bridge_denial"})
        raise ProtocolRefusal(code, code.replace("_", " "))

    def _latest_consent(self, root: FloatiRoot, peer: FloatiRoot) -> Optional[Dict[str, object]]:
        rows = read_records_snapshot(root, CONSENTS, allowed_kinds={"bridge_consent"})
        matches = [
            row for row in rows
            if row["bridge_id"] == self.bridge_id and row["peer_tenant_id"] == peer.tenant_id
        ]
        return matches[-1] if matches else None

    def consent(
        self,
        root: FloatiRoot,
        peer: FloatiRoot,
        actor: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        local, remote = self._pair(root, peer)
        if self._same():
            return self._refuse(local, remote, "bridge_same_root", now=now)
        try:
            owner = validate_identifier(actor, "actor")
            Registry(local).require_active(owner)
        except ProtocolRefusal:
            return self._refuse(local, remote, "bridge_sender_inactive", now=now)
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "bridge-consent-" + uuid7_hex(),
            "tenant_id": local.tenant_id,
            "timestamp": _timestamp(now),
            "kind": "bridge_consent",
            "bridge_id": self.bridge_id,
            "peer_tenant_id": remote.tenant_id,
            "actor": owner,
            "direction": "bidirectional",
            "state": "granted",
            "scope": "advisory_not_consumption",
        }
        append_record(local, CONSENTS, record, allowed_kinds={"bridge_consent"})
        return record

    def revoke(
        self,
        root: FloatiRoot,
        peer: FloatiRoot,
        actor: str,
        *,
        now: Optional[datetime] = None,
    ) -> Dict[str, object]:
        local, remote = self._pair(root, peer)
        try:
            owner = validate_identifier(actor, "actor")
        except ProtocolRefusal:
            return self._refuse(local, remote, "bridge_sender_inactive", now=now)
        prior = self._latest_consent(local, remote)
        if prior is None:
            return self._refuse(local, remote, "bridge_consent_missing", now=now)
        if prior["actor"] != owner:
            return self._refuse(local, remote, "bridge_consent_mismatch", now=now)
        record = dict(prior)
        record["id"] = "bridge-consent-" + uuid7_hex()
        record["timestamp"] = _timestamp(now)
        record["state"] = "revoked"
        append_record(local, CONSENTS, record, allowed_kinds={"bridge_consent"})
        return record

    def establish(self, *, now: Optional[datetime] = None) -> Dict[str, object]:
        if self._same():
            return self._refuse(self.left, self.right, "bridge_same_root", now=now)
        left_consent = self._latest_consent(self.left, self.right)
        right_consent = self._latest_consent(self.right, self.left)
        if left_consent is None or right_consent is None:
            return self._refuse(self.left, self.right, "bridge_consent_missing", now=now)
        if left_consent["state"] != "granted" or right_consent["state"] != "granted":
            return self._refuse(self.left, self.right, "bridge_consent_revoked", now=now)
        timestamp = _timestamp(now)
        result: Optional[Dict[str, object]] = None
        for root in (self.left, self.right):
            record: Dict[str, object] = {
                "schema_version": 0,
                "id": "bridge-record-" + uuid7_hex(),
                "tenant_id": root.tenant_id,
                "timestamp": timestamp,
                "kind": "bridge_record",
                "bridge_id": self.bridge_id,
                "left_tenant_id": self.left.tenant_id,
                "right_tenant_id": self.right.tenant_id,
                "left_consent_id": left_consent["id"],
                "right_consent_id": right_consent["id"],
                "transport": "local_filesystem",
                "scope": "advisory_not_consumption",
                "state": "active",
            }
            append_record(root, RECORDS, record, allowed_kinds={"bridge_record"})
            if root.path == self.left.path:
                result = record
        assert result is not None
        return result

    def _active_record(self, root: FloatiRoot) -> Optional[Dict[str, object]]:
        rows = read_records_snapshot(root, RECORDS, allowed_kinds={"bridge_record"})
        matches = [row for row in rows if row["bridge_id"] == self.bridge_id]
        return matches[-1] if matches else None

    def forward(
        self,
        source: FloatiRoot,
        sender: str,
        recipient: str,
        repo: str,
        sha: str,
        doc: str,
        note: str,
        *,
        now: Optional[datetime] = None,
        transport: str = "local_filesystem",
    ) -> Dict[str, object]:
        if source.path == self.left.path:
            destination = self.right
        elif source.path == self.right.path:
            destination = self.left
        else:
            return self._refuse(self.left, self.right, "bridge_root_unknown", now=now)
        if self._same():
            return self._refuse(source, destination, "bridge_same_root", now=now)
        if transport != "local_filesystem":
            return self._refuse(source, destination, "bridge_transport_forbidden", now=now)
        source_record = self._active_record(source)
        destination_record = self._active_record(destination)
        if (
            source_record is None or destination_record is None
            or source_record["state"] != "active" or destination_record["state"] != "active"
        ):
            return self._refuse(source, destination, "bridge_not_active", now=now)
        source_consent = self._latest_consent(source, destination)
        destination_consent = self._latest_consent(destination, source)
        if source_consent is None or destination_consent is None:
            return self._refuse(source, destination, "bridge_consent_missing", now=now)
        if source_consent["state"] != "granted" or destination_consent["state"] != "granted":
            return self._refuse(source, destination, "bridge_consent_revoked", now=now)
        left_consent = source_consent if source.path == self.left.path else destination_consent
        right_consent = destination_consent if source.path == self.left.path else source_consent
        if any(
            row["left_consent_id"] != left_consent["id"]
            or row["right_consent_id"] != right_consent["id"]
            for row in (source_record, destination_record)
        ):
            return self._refuse(source, destination, "bridge_consent_mismatch", now=now)
        try:
            actor = validate_identifier(sender, "sender")
            Registry(source).require_active(actor)
        except ProtocolRefusal:
            return self._refuse(source, destination, "bridge_sender_inactive", now=now)
        try:
            target = validate_identifier(recipient, "recipient")
            Registry(destination).require_active(target)
        except ProtocolRefusal:
            return self._refuse(source, destination, "bridge_recipient_inactive", now=now)
        timestamp = _timestamp(now)
        forward_id = "forward-" + uuid7_hex()

        def record_for(root: FloatiRoot, direction: str) -> Dict[str, object]:
            return {
                "schema_version": 0,
                "id": "bridge-forward-" + uuid7_hex(),
                "tenant_id": root.tenant_id,
                "timestamp": timestamp,
                "kind": "bridge_forward",
                "bridge_id": self.bridge_id,
                "forward_id": forward_id,
                "direction": direction,
                "source_tenant_id": source.tenant_id,
                "destination_tenant_id": destination.tenant_id,
                "sender": actor,
                "recipient": target,
                "repo": repo,
                "sha": sha,
                "doc": doc,
                "note": note,
                "source_consent_id": source_consent["id"],
                "destination_consent_id": destination_consent["id"],
                "stamp": "advisory_not_consumption",
            }

        outbound = record_for(source, "outbound")
        inbound = record_for(destination, "inbound")
        try:
            append_record(destination, FORWARDS, inbound, allowed_kinds={"bridge_forward"})
            append_record(source, FORWARDS, outbound, allowed_kinds={"bridge_forward"})
        except ProtocolRefusal:
            return self._refuse(source, destination, "bridge_direction_invalid", now=now)
        return outbound

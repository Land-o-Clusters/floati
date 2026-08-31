"""Tenant-local node registry with fail-closed lookups."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from .copy import (
    REGISTRY_RETIRE_ALREADY_RETIRED_DETAIL,
    REGISTRY_RETIRE_UNKNOWN_NODE_DETAIL,
)
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records, read_records_snapshot, transact
from .records import validate_role
from .root import FloatiRoot, validate_identifier


REGISTRY_KINDS = {
    "registry_entry", "node_lease", "provider_switch_receipt",
    "registry_role_record", "lane_spawn_receipt", "lane_teardown_receipt",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Registry:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.relative_path = Path("registry/entries.jsonl")
        self.path = root.resolve_relative(self.relative_path)

    def register(self, node_id: str, role: str) -> Dict[str, object]:
        node = self.resolve_node_id(node_id, require_active=False)
        role = validate_role(role)
        record: Dict[str, object] = {
            "schema_version": 0,
            "id": "registry-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "registry_entry",
            "node_id": node,
            "role": role,
            "state": "active",
        }
        def decide(records: list[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            if any(item.get("node_id") == node for item in records):
                raise ProtocolRefusal("registry_duplicate", f"node {node} is already registered")
            return record, record

        return transact(self.root, self.relative_path, decide, allowed_kinds=REGISTRY_KINDS)

    def retire(self, node_id: str) -> Dict[str, object]:
        """Append one retirement row for a node that is retiring itself."""

        node = validate_identifier(node_id, "node")

        def decide(records: list[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            latest: Optional[Dict[str, object]] = None
            for item in records:
                if item.get("kind") == "registry_entry" and item.get("node_id") == node:
                    latest = item
            if latest is None:
                raise ProtocolRefusal("unknown_node", REGISTRY_RETIRE_UNKNOWN_NODE_DETAIL)
            if latest.get("state") == "retired":
                raise ProtocolRefusal(
                    "registry_already_retired", REGISTRY_RETIRE_ALREADY_RETIRED_DETAIL
                )
            record: Dict[str, object] = {
                "schema_version": 0,
                "id": "registry-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": utc_now(),
                "kind": "registry_entry",
                "node_id": node,
                "role": validate_role(latest["role"]),
                "state": "retired",
            }
            return record, record

        return transact(self.root, self.relative_path, decide, allowed_kinds=REGISTRY_KINDS)

    def require_active(self, node_id: str) -> Dict[str, object]:
        node = self.resolve_node_id(node_id)
        record = self._latest(node)
        if record is None or record.get("state") != "active":  # defensive against replacement races
            raise ProtocolRefusal("unknown_node", f"node {node!r} is not active")
        return record

    def node_lease_state(
        self, node_id: str, *, now: Optional[datetime] = None
    ) -> Dict[str, object]:
        """Project one node's lease at an act boundary without appending state."""

        node = self.resolve_node_id(node_id)
        current = datetime.now(timezone.utc) if now is None else now
        if (
            not isinstance(current, datetime)
            or current.tzinfo is None
            or current.utcoffset() is None
        ):
            raise ProtocolRefusal(
                "time_invalid", "lease evaluation requires an aware datetime"
            )
        current = current.astimezone(timezone.utc)
        latest: Optional[Dict[str, object]] = None
        for record in read_records_snapshot(
            self.root, self.relative_path, allowed_kinds=REGISTRY_KINDS
        ):
            if record.get("kind") == "node_lease" and record.get("node_id") == node:
                latest = record
        if latest is None:
            return {"node_lease_id": None, "state": "not_leased", "expires_at": None}
        state = str(latest["state"])
        expires_at = latest.get("expires_at")
        if state == "active":
            assert isinstance(expires_at, str)
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if current >= expiry:
                state = "expired"
        return {
            "node_lease_id": latest["id"],
            "state": state,
            "expires_at": expires_at,
        }

    def require_protocol_lease(
        self, node_id: str, *, now: Optional[datetime] = None, act: str
    ) -> Dict[str, object]:
        lease = self.node_lease_state(node_id, now=now)
        if lease["state"] == "expired":
            raise ProtocolRefusal(
                "node_lease_expired",
                f"{act} refused: node {node_id} lease {lease['node_lease_id']} "
                f"expired at {lease['expires_at']}",
            )
        return lease

    def resolve_node_id(
        self,
        node_id: str,
        *,
        field: str = "node",
        require_active: bool = True,
        unknown_code: str = "unknown_node",
    ) -> str:
        """Return the one canonical registry spelling before callers create state."""

        node = validate_identifier(node_id, field)
        if not require_active:
            return node
        active_nodes = self.active_node_ids()
        if node not in active_nodes:
            roster = ", ".join(active_nodes) or "(none)"
            if unknown_code == "unknown_sender":
                detail = (
                    f"message refused: unknown sender {node!r}; "
                    f"registered active nodes: {roster}"
                )
            elif unknown_code == "unknown_recipient":
                detail = (
                    f"message refused: unknown recipient {node!r}; "
                    f"registered active nodes: {roster}"
                )
            else:
                detail = f"node {node!r} is not active"
            raise ProtocolRefusal(unknown_code, detail)
        return node

    def active_node_ids(self) -> Tuple[str, ...]:
        latest: Dict[str, Dict[str, object]] = {}
        for record in read_records_snapshot(
            self.root, self.relative_path, allowed_kinds=REGISTRY_KINDS
        ):
            if record.get("kind") == "registry_entry":
                latest[str(record["node_id"])] = record
        return tuple(sorted(
            node for node, record in latest.items()
            if record.get("state") == "active"
        ))

    def _latest(self, node_id: str) -> Optional[Dict[str, object]]:
        for record in reversed(read_records(self.root, self.relative_path, allowed_kinds=REGISTRY_KINDS)):
            if record.get("kind") == "registry_entry" and record.get("node_id") == node_id:
                return record
        return None

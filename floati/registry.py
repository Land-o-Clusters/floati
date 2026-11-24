"""Tenant-local node registry with fail-closed lookups."""

from __future__ import annotations

import shlex
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .bus_epoch import shared_epoch_operation
from .copy import (
    REGISTRY_RETIRE_ALREADY_RETIRED_DETAIL,
    REGISTRY_RETIRE_UNKNOWN_NODE_DETAIL,
)
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import (
    _locked_path,
    read_records_compatible_with_versions,
    transact,
)
from .records import READER_VERSION, validate_role
from .root import FloatiRoot, validate_identifier
from .seat_declaration import FleetGovernance


REGISTRY_KINDS = {
    "registry_entry", "node_lease", "provider_switch_receipt",
    "registry_role_record", "registry_role_transfer",
    "lane_spawn_receipt", "lane_teardown_receipt",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def node_lifetime(records: List[Dict[str, Any]], node_id: str) -> str:
    """Temporary means this node has a lease row; permanent means it does not."""

    for record in records:
        if record.get("kind") == "node_lease" and record.get("node_id") == node_id:
            return "temporary"
    return "permanent"


def node_retire_command(root: FloatiRoot, node_id: str) -> str:
    """The retire command a temporary add already projects for this node."""

    node = validate_identifier(node_id, "node")
    return " ".join(
        shlex.quote(part)
        for part in (
            "floati", "node", "retire", "--root", str(root.path), "--node", node,
        )
    )


def expired_node_leases(
    root: FloatiRoot,
    records: List[Dict[str, Any]],
    now: datetime,
) -> List[Dict[str, object]]:
    """Active nodes whose latest lease is past `now`, newest registration last."""

    latest_registry: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if record.get("kind") == "registry_entry" and isinstance(record.get("node_id"), str):
            latest_registry[record["node_id"]] = record
    found: List[Dict[str, object]] = []
    for node_id in sorted(latest_registry):
        entry = latest_registry[node_id]
        if entry.get("state") != "active":
            continue
        try:
            state = Registry._node_lease_state_from_records(node_id, records, now=now)
        except ProtocolRefusal:
            continue
        expires_at = state.get("expires_at")
        if state.get("state") != "expired" or not isinstance(expires_at, str):
            continue
        found.append(
            {
                "plane": "node",
                "node_id": node_id,
                "expires_at": expires_at,
                "retire_command": node_retire_command(root, node_id),
            }
        )
    return found


def read_registry_compatible(
    root: FloatiRoot, *, snapshot: bool = True
) -> Tuple[List[Dict[str, Any]], List[Dict[str, object]], Dict[str, int]]:
    """Read known registry rows and report well-formed newer kinds."""

    return read_records_compatible_with_versions(
        root,
        Path("registry/entries.jsonl"),
        allowed_kinds=set(REGISTRY_KINDS),
        snapshot=snapshot,
    )


def registration_record(tenant, node, role, *, record_id=None, timestamp=None):
    return {"schema_version": 0, "id": "registry-" + (record_id or uuid7_hex()),
            "tenant_id": tenant, "timestamp": timestamp or utc_now(),
            "kind": "registry_entry", "node_id": validate_identifier(node, "node"),
            "role": validate_role(role), "state": "active"}


class Registry:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.relative_path = Path("registry/entries.jsonl")
        self.path = root.resolve_relative(self.relative_path)
        self._invocation_records: Optional[List[Dict[str, Any]]] = None
        self._selected_invocation = None

    @contextmanager
    def read_once(self, *, node_id=None):
        """Reuse one validated registry parse for this invocation's lookups only."""

        if self._invocation_records is not None:
            yield self
            return
        if node_id is not None:
            node = validate_identifier(node_id, "node")
            previous = self._selected_invocation
            if previous is not None and previous[0] == node:
                yield self
                return
            from .registry_prefix import read_selected
            self._selected_invocation = (node, read_selected(self.root, node))
            try:
                yield self
            finally:
                self._selected_invocation = previous
            return
        self._invocation_records = self.known_records()
        try:
            yield self
        finally:
            self._invocation_records = None

    def known_records(self, *, snapshot: bool = True) -> List[Dict[str, Any]]:
        if self._invocation_records is not None:
            return self._invocation_records
        records, _unrecognized, _versions = read_registry_compatible(
            self.root, snapshot=snapshot
        )
        return records

    def skip_receipt(self) -> Optional[Dict[str, object]]:
        """Name one unknown kind this reader skipped, with the reader schema."""

        _records, unrecognized, _versions = read_registry_compatible(self.root)
        if not unrecognized:
            return None
        first = unrecognized[0]
        return {
            "kind": str(first["kind"]),
            "reader_schema_version": READER_VERSION,
            "first_id": str(first["first_id"]),
            "ledger": self.relative_path.as_posix(),
        }

    @shared_epoch_operation
    def register(self, node_id: str, role: str) -> Dict[str, object]:
        node = self.resolve_node_id(node_id, require_active=False)
        record = registration_record(self.root.tenant_id, node, role)
        def decide(records: list[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
            if any(item.get("node_id") == node for item in records):
                raise ProtocolRefusal("registry_duplicate", f"node {node} is already registered")
            return record, record

        coordination_relative = Path("receipts/wake-coordination") / node / "lane.lock"
        coordination_lock = self.root.resolve_relative(coordination_relative)
        with _locked_path(
            coordination_lock, exclusive=True, relative=coordination_relative
        ):
            pass
        return transact(
            self.root, self.relative_path, decide, allowed_kinds=REGISTRY_KINDS
        )

    def record_governance(
        self,
        *,
        topology: str,
        coordinator: str,
        coordinator_authority: Tuple[str, ...],
        owner_tier: Tuple[str, ...],
    ) -> FleetGovernance:
        """Record immutable fleet governance outside the append-only node ledger."""

        return FleetGovernance.create(
            self.root,
            topology=topology,
            coordinator=coordinator,
            coordinator_authority=coordinator_authority,
            owner_tier=owner_tier,
        )

    def governance(self) -> Optional[FleetGovernance]:
        """Return the validated governed-onboarding record, when one exists."""

        return FleetGovernance.load(self.root)

    @shared_epoch_operation
    def retire(self, node_id: str) -> Dict[str, object]:
        """Append one retirement row for the explicitly selected node."""

        from .lane_retirement import (
            close_retiring_node_lanes, preflight_retirement_records, retirement_lane_scope,
        )

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
            preflight_retirement_records(self.root, records, [record], REGISTRY_KINDS)
            close_retiring_node_lanes(self.root, node, completed_lanes)
            return record, record

        with retirement_lane_scope(self.root) as completed_lanes:
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

        node = validate_identifier(node_id, "node")
        return self._node_lease_state_from_records(node, self._selected_rows(node), now=now)

    def _selected_rows(self, node):
        if self._invocation_records is not None:
            return self._invocation_records
        if self._selected_invocation is not None and self._selected_invocation[0] == node:
            return self._selected_invocation[1]
        return self.known_records()

    @staticmethod
    def _node_lease_state_from_records(
        node_id: str, records: List[Dict[str, Any]], *, now: Optional[datetime] = None
    ) -> Dict[str, object]:
        """Project only one already-validated registration/lease snapshot."""
        node = validate_identifier(node_id, "node")
        registration = next((row for row in reversed(records)
            if row.get("kind") == "registry_entry" and row.get("node_id") == node), None)
        if registration is None or registration.get("state") != "active":
            raise ProtocolRefusal("unknown_node", f"node {node!r} is not active",
                remedy="use the exact active registered node for this lease observation")
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
        for record in records:
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
        selected = (self._invocation_records is None and self._selected_invocation is not None
                    and self._selected_invocation[0] == node)
        active_nodes = None if selected else self.active_node_ids()
        registration = self._latest(node) if selected else None
        active = (registration is not None and registration.get("state") == "active"
                  if selected else node in active_nodes)
        if not active:
            if active_nodes is None:
                active_nodes = self.active_node_ids()
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
            raise ProtocolRefusal(
                unknown_code,
                detail,
                "pass --from/--to as one of the registered nodes named in detail",
            )
        return node

    def active_node_ids(self) -> Tuple[str, ...]:
        latest: Dict[str, Dict[str, object]] = {}
        for record in self.known_records():
            if record.get("kind") == "registry_entry":
                latest[str(record["node_id"])] = record
        return tuple(sorted(
            node for node, record in latest.items()
            if record.get("state") == "active"
        ))

    def _latest(self, node_id: str) -> Optional[Dict[str, object]]:
        for record in reversed(self._selected_rows(node_id)):
            if record.get("kind") == "registry_entry" and record.get("node_id") == node_id:
                return record
        return None

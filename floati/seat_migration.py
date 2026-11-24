"""BL-10: migrate a pre-governance registered seat once, by verb, with a receipt.

A seat registered before the governed path existed has an active registry
entry, a waiter workspace binding and no declaration. The migration derives
the fleet governance from the ruled values (the coordinator from the
registry's architect role where one is recorded, otherwise the argument),
publishes the workspace declaration from those records, and receipts the act.
Boarding itself never changes; the migration runs ahead of the first board.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .codex_wait_contract import resolve_participant
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import transact
from .registry import Registry, utc_now
from .seat_declaration import (
    COORDINATOR_AUTHORITIES,
    OWNER_TIERS,
    FleetGovernance,
    SeatDeclaration,
)

KIND = "seat_migration_receipt"


def coordinate_digest(node_id: str, workspace: Path) -> str:
    return hashlib.sha256((node_id + "\0" + str(workspace)).encode()).hexdigest()


def remedy_for(root, node_id: str, workspace: Path) -> str:
    """The one runnable command that resolves an undeclared boarding."""
    key = "migrate-" + coordinate_digest(node_id, Path(workspace))[:16]
    return (
        "floati seat migrate --root " + str(root.path) + " --as " + node_id
        + " --workspace " + str(workspace) + " --idempotency-key " + key
    )


def _derive_coordinator(root, coordinator):
    """The registry's architect role decides; the argument resolves ambiguity."""
    if coordinator is not None:
        return coordinator
    latest = {}
    for record in Registry(root).known_records():
        if record.get("kind") == "registry_entry":
            latest[str(record["node_id"])] = record
    architects = sorted(
        node for node, record in latest.items()
        if record.get("state") == "active" and record.get("role") == "architect"
    )
    if len(architects) == 1:
        return architects[0]
    if not architects:
        raise ProtocolRefusal(
            "arguments_invalid",
            "seat migration needs a coordinator: pass --coordinator or register one active architect-role node",
        )
    raise ProtocolRefusal(
        "arguments_invalid",
        "multiple active architect-role nodes; pass --coordinator explicitly",
    )


def migrate(root, node_id, workspace, *, idempotency_key, coordinator=None):
    """Publish governance and the workspace declaration for a registered seat."""
    node = Registry(root).resolve_node_id(node_id, field="node")
    workspace_path = Path(workspace)
    participant = resolve_participant(root.tenant_home, workspace_path)
    if participant is None:
        raise ProtocolRefusal(
            "seat_board_workspace_unbound",
            "workspace has no waiter binding; migration resolves a mapped, registered seat",
            remedy=(
                "map the workspace to the node through the waiter consent flow "
                "(floati wake daemon bind), then retry"
            ),
        )
    if participant.binding.node_id != node:
        raise ProtocolRefusal(
            "workspace_identity_mismatch",
            "workspace map binds " + participant.binding.node_id + ", not " + node,
        )
    # F2-RETIRED-HOME-DISARM-1: the migration's own act includes the homes
    # the seat left behind — their armed hooks are disarmed and receipted
    # before the migration receipts, so a crash never leaves a migration
    # receipt over still-armed old homes.
    from .retired_home_disarm import disarm_retired_homes

    disarm_retired_homes(
        root, node, new_home=workspace_path,
        idempotency_key=idempotency_key + "-disarm",
    )
    governance = FleetGovernance.load(root)
    governance_created = False
    if governance is None:
        selected = _derive_coordinator(root, coordinator)
        governance = FleetGovernance.create(
            root,
            topology="star",
            coordinator=selected,
            coordinator_authority=COORDINATOR_AUTHORITIES,
            owner_tier=OWNER_TIERS,
        )
        governance_created = True
    declaration, declaration_created = SeatDeclaration.publish_declared_workspace(
        workspace_path, node, root, governance
    )
    digest = coordinate_digest(node, workspace_path)
    row = {
        "schema_version": 1,
        "kind": KIND,
        "tenant_id": root.tenant_id,
        "id": "seat-migration-" + uuid7_hex(),
        "timestamp": utc_now(),
        "node_id": node,
        "workspace": str(workspace_path),
        "coordinator": governance.coordinator,
        "idempotency_key": idempotency_key,
        "coordinate_digest": digest,
        "declaration_created": declaration_created,
        "governance_created": governance_created,
    }
    relative = Path("receipts/seat-migration") / (node + ".jsonl")

    def decide(prior):
        matching = [r for r in prior if r["idempotency_key"] == idempotency_key]
        if matching:
            if matching[-1]["coordinate_digest"] != digest:
                raise ProtocolRefusal(
                    "seat_migration_key_conflict",
                    "migration key has different coordinates",
                    remedy="use a new key for different migration coordinates",
                )
            return matching[-1], None
        return row, row

    return transact(root, relative, decide, allowed_kinds={KIND})

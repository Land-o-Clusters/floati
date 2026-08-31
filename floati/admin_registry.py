"""One sealed registry-admin adapter for preview-first lifecycle mutations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence

from .bus_epoch import shared_epoch_operation
from .errors import ProtocolRefusal
from .jsonl import read_records_snapshot, transact_records
from .node_wizard import NodeAddPlan, NodeRetirePlan
from .provider_switch import ProviderSwitchPlan
from .registry import REGISTRY_KINDS, Registry
from .role_assignment import RoleAssignmentPlan
from .root import FloatiRoot, validate_identifier
from .seat_declaration import SeatDeclaration, WorkspaceBinding


class RegistryAdminBackend:
    """Commit exact previewed rows through the registry's single ledger."""

    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.registry = Registry(root)

    def _records(self) -> list[Dict[str, Any]]:
        return read_records_snapshot(
            self.root,
            self.registry.relative_path,
            allowed_kinds=REGISTRY_KINDS,
        )

    def _latest(self, kind: str, node_id: str) -> Optional[Dict[str, Any]]:
        node = validate_identifier(node_id, "node")
        for record in reversed(self._records()):
            if record.get("kind") == kind and record.get("node_id") == node:
                return dict(record)
        return None

    def active_node(self, node_id: str) -> Dict[str, Any]:
        return dict(self.registry.require_active(node_id))

    def active_lease(self, node_id: str) -> Optional[Dict[str, Any]]:
        latest = self._latest("node_lease", node_id)
        return latest if latest is not None and latest.get("state") == "active" else None

    @shared_epoch_operation
    def _commit(
        self,
        records: Sequence[Dict[str, Any]],
        decide: Any,
    ) -> Dict[str, Any]:
        frozen = tuple(dict(record) for record in records)

        def transaction(existing: list[Dict[str, Any]]):
            decide(existing, frozen)
            return {"records": [dict(record) for record in frozen]}, frozen

        return transact_records(
            self.root,
            self.registry.relative_path,
            transaction,
            allowed_kinds=REGISTRY_KINDS,
        )

    @staticmethod
    def _latest_registry(
        records: Iterable[Dict[str, Any]], node_id: str
    ) -> Optional[Dict[str, Any]]:
        latest = None
        for record in records:
            if record.get("kind") == "registry_entry" and record.get("node_id") == node_id:
                latest = record
        return latest

    def _prepare_workspace(self, node_id: str) -> WorkspaceBinding:
        return WorkspaceBinding.prepare(self.root, node_id)

    def _add_commit_state(
        self, records: Sequence[Dict[str, Any]]
    ) -> Optional[bool]:
        """Return true/false only when exact append presence can be reconciled."""

        try:
            existing = self._records()
        except Exception:
            return None
        by_id = {record.get("id"): record for record in existing}
        matches = [by_id.get(record.get("id")) == record for record in records]
        if matches and all(matches):
            return True
        if any(record.get("id") in by_id for record in records):
            return None
        return False

    def commit_add(self, plan: NodeAddPlan) -> Dict[str, Any]:
        binding = self._prepare_workspace(plan.node_id)
        publication = None
        commit_started = False

        def decide(existing: list[Dict[str, Any]], records: Sequence[Dict[str, Any]]) -> None:
            if self._latest_registry(existing, plan.node_id) is not None:
                raise ProtocolRefusal("registry_duplicate", "node is already registered")
            if not records or records[0].get("kind") != "registry_entry":
                raise ProtocolRefusal("registry_preview_invalid", "add preview must begin with registry entry")
            if records[0].get("node_id") != plan.node_id or records[0].get("state") != "active":
                raise ProtocolRefusal("registry_preview_invalid", "add preview identity is invalid")
            leases = [record for record in records if record.get("kind") == "node_lease"]
            if (plan.lifetime == "temporary") != (len(leases) == 1):
                raise ProtocolRefusal("node_lease_invalid", "temporary lifetime and lease preview disagree")

        try:
            if plan.governance is not None:
                publication = SeatDeclaration.create(
                    binding,
                    plan.node_id,
                    self.root,
                    plan.governance,
                )
            commit_started = True
            result = self._commit(plan.records, decide)
        except Exception:
            commit_state = self._add_commit_state(plan.records) if commit_started else False
            if commit_state is False:
                if publication is not None:
                    binding.remove_owned_marker(publication.ownership)
                binding.rollback_created_directories()
            raise
        finally:
            binding.close()
        result["workspace"] = str(binding.path)
        return result

    def commit_retire(self, plan: NodeRetirePlan) -> Dict[str, Any]:
        def decide(existing: list[Dict[str, Any]], records: Sequence[Dict[str, Any]]) -> None:
            active = self._latest_registry(existing, plan.node_id)
            if active is None or active.get("state") != "active":
                raise ProtocolRefusal("unknown_node", "node is not active")
            if not records or records[0].get("kind") != "registry_entry":
                raise ProtocolRefusal("registry_preview_invalid", "retire preview must begin with registry entry")
            if records[0].get("node_id") != plan.node_id or records[0].get("state") != "retired":
                raise ProtocolRefusal("registry_preview_invalid", "retire preview identity is invalid")
            active_lease = None
            for record in existing:
                if record.get("kind") == "node_lease" and record.get("node_id") == plan.node_id:
                    active_lease = record
            retired = [record for record in records if record.get("kind") == "node_lease"]
            if active_lease is not None and active_lease.get("state") == "active":
                if len(retired) != 1 or retired[0].get("predecessor_lease_id") != active_lease.get("id"):
                    raise ProtocolRefusal("node_lease_invalid", "retirement does not close the active lease")

        return self._commit(plan.records, decide)

    def active_assignment(self, node_id: str) -> Dict[str, Any]:
        active = self.active_node(node_id)
        model = None
        for record in self._records():
            if (
                record.get("kind") == "provider_switch_receipt"
                and record.get("node_id") == node_id
                and record.get("registry_entry_id") == active.get("id")
            ):
                model = record.get("model")
        return dict(active, model=model)

    def active_nodes(self) -> list[Dict[str, Any]]:
        """Return current active registry rows without caching topology."""

        return [
            dict(self.registry.require_active(node_id))
            for node_id in self.registry.active_node_ids()
        ]

    def role_record(self, node_id: str) -> Dict[str, Any]:
        """Return the current typed role record for one active node."""

        self.active_node(node_id)
        latest = self._latest("registry_role_record", node_id)
        if latest is None or latest.get("state") != "active":
            raise ProtocolRefusal("role_assignment_missing", "node has no active role record")
        return latest

    def commit_switch(self, plan: ProviderSwitchPlan) -> Dict[str, Any]:
        def decide(existing: list[Dict[str, Any]], records: Sequence[Dict[str, Any]]) -> None:
            active = self._latest_registry(existing, plan.node_id)
            if active is None or active.get("state") != "active":
                raise ProtocolRefusal("provider_assignment_invalid", "node is not active")
            if len(records) != 2 or [row.get("kind") for row in records] != [
                "registry_entry", "provider_switch_receipt",
            ]:
                raise ProtocolRefusal("provider_preview_invalid", "switch preview shape is invalid")
            replacement, receipt = records
            if (
                replacement.get("node_id") != plan.node_id
                or replacement.get("state") != "active"
                or receipt.get("previous_registry_entry_id") != active.get("id")
                or receipt.get("registry_entry_id") != replacement.get("id")
            ):
                raise ProtocolRefusal("provider_preview_invalid", "switch preview binding is invalid")

        return self._commit(plan.records, decide)

    def current_architect(self) -> Dict[str, Any]:
        architects = []
        for node in self.registry.active_node_ids():
            record = self.registry.require_active(node)
            if str(record.get("role", "")).casefold() == "architect":
                architects.append(record)
        if len(architects) != 1:
            raise ProtocolRefusal("role_architect_invalid", "fleet must have one active architect")
        return dict(architects[0])

    def commit_role(self, plan: RoleAssignmentPlan) -> Dict[str, Any]:
        def decide(existing: list[Dict[str, Any]], records: Sequence[Dict[str, Any]]) -> None:
            active = self._latest_registry(existing, plan.node_id)
            if active is None or active.get("state") != "active":
                raise ProtocolRefusal("role_node_invalid", "node is not active")
            if len(records) != 1 or records[0].get("kind") != "registry_role_record":
                raise ProtocolRefusal("role_preview_invalid", "role preview shape is invalid")
            latest_role = None
            for record in existing:
                if record.get("kind") == "registry_role_record" and record.get("node_id") == plan.node_id:
                    latest_role = record
            predecessor = None if latest_role is None else latest_role.get("id")
            if records[0].get("predecessor_role_record_id") != predecessor:
                raise ProtocolRefusal("role_preview_invalid", "role predecessor is stale")

        return self._commit((plan.record,), decide)

"""One sealed registry-admin adapter for preview-first lifecycle mutations."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, TYPE_CHECKING

from .bus_epoch import shared_epoch_operation
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import transact_records
from .node_wizard import NodeAddPlan, NodeRetirePlan
from .provider_switch import ProviderSwitchPlan
from .registry import REGISTRY_KINDS, Registry, utc_now, read_registry_compatible
from .role_assignment import RoleAssignmentPlan
from .root import FloatiRoot, validate_identifier
from .sandbox_probe import probe_write_set
from .sandbox_remedy import remedy_for
from .seat_declaration import SeatDeclaration, WorkspaceBinding

if TYPE_CHECKING:
    from .role_templates import RoleTemplate


# The remedy every architect-count refusal names (ARCH-0/ARCH-1).
ARCHITECT_REMEDY = "run role transfer-architect --to NODE to name the one active architect"


class RegistryAdminBackend:
    """Commit exact previewed rows through the registry's single ledger."""

    def __init__(
        self, root: FloatiRoot, *, repository: Optional[Path] = None
    ) -> None:
        self.root = root
        self.registry = Registry(root)
        self.repository = (
            None
            if repository is None
            else Path(repository).expanduser().resolve()
        )

    def _records(self) -> list[Dict[str, Any]]:
        records, _unrecognized, _versions = read_registry_compatible(self.root)
        return records

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
        records: Sequence[Mapping[str, Any]],
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

    def _preflight_declaration(self, plan: NodeAddPlan) -> None:
        facts = probe_write_set(
            self.root,
            plan.node_id,
            repository=self.repository,
        )
        problems = tuple(
            fact
            for fact in facts
            if fact.get("verdict") in {"refused", "unknown"}
        )
        if not problems:
            return
        detail = "seat declaration write preflight failed: " + "; ".join(
            f"{fact.get('coordinate')} path="
            f"{'null' if fact.get('path') is None else fact.get('path')} "
            f"reason={fact.get('reason_code')}"
            for fact in problems
        )
        refused_paths = tuple(
            str(fact["path"])
            for fact in problems
            if fact.get("path") is not None
        )
        raise ProtocolRefusal(
            "seat_declaration_deaf",
            detail,
            remedy=remedy_for(plan.harness, refused_paths),
        )

    def _add_commit_state(
        self, records: Sequence[Mapping[str, Any]]
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
                self._preflight_declaration(plan)
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

    @shared_epoch_operation
    def commit_retire(self, plan: NodeRetirePlan) -> Dict[str, Any]:
        from .lane_retirement import (
            close_retiring_node_lanes, preflight_retirement_records, retirement_lane_scope,
        )

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
            preflight_retirement_records(self.root, existing, records, REGISTRY_KINDS)
            close_retiring_node_lanes(self.root, plan.node_id, completed_lanes)

        with retirement_lane_scope(self.root) as completed_lanes:
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
        """Resolve the one active architect from latest role records (ARCH-0).

        The entry ``role`` field is harness vocabulary; a node's role is its
        latest ``registry_role_record`` (ruling 2026-09-05, ARCH-1). Zero or
        more than one architect refuses, naming the transfer verb.
        """

        architects = []
        for node in self.registry.active_node_ids():
            record = self._latest("registry_role_record", node)
            if (
                record is not None
                and record.get("state") == "active"
                and str(record.get("template_role", "")).casefold() == "architect"
            ):
                architects.append(node)
        if len(architects) != 1:
            raise ProtocolRefusal(
                "role_architect_invalid",
                "fleet must have one active architect",
                remedy=ARCHITECT_REMEDY,
            )
        return self.registry.require_active(architects[0])

    def transfer_architect(
        self,
        *,
        to: str,
        idempotency_key: str,
        declared_templates: Mapping[str, "RoleTemplate"],
    ) -> Dict[str, Any]:
        """Move the architect role in the role-record vocabulary (ARCH-1).

        One transfer commits, under one idempotency key and one transaction:
        the vacated node's next role record, the target's architect role
        record, and a `registry_role_transfer` receipt naming both nodes,
        both before/after roles, and the two record ids. The composed state
        is checked inside the transaction before any record is written; a
        result of 0 or >1 architects refuses and writes nothing. The entry
        `role` field is never touched. A replay under the same key returns
        the first receipt as a no-op.

        ARCH-DECL-1-F1: every written record pins the DECLARED template's
        version and digest, resolved at transfer time — never a copy of
        the predecessor's pin, which goes stale the moment the declared
        template is revised and which doctor's delivery health then
        refuses.
        """

        fallback_template = declared_templates.get("builder")
        if fallback_template is None:
            raise ProtocolRefusal(
                "role_template_unknown",
                "the shipped default role template is not shipped",
                remedy="reinstall the governed bundle; roles/shipped must carry the builder template",
            )
        architect_template = declared_templates.get("architect")
        if architect_template is None:
            raise ProtocolRefusal(
                "role_template_unknown",
                "the architect role template is not declared",
                remedy="reinstall the governed bundle; roles/shipped must carry the architect template",
            )

        for record in self._records():
            if (
                record.get("kind") == "registry_role_transfer"
                and record.get("idempotency_key") == idempotency_key
            ):
                return {
                    "receipt": record,
                    "replayed": True,
                    "replayed_receipt_id": record["id"],
                }

        target_node = validate_identifier(to, "node")
        vacated_entry = self.current_architect()
        vacated_node = str(vacated_entry["node_id"])
        if target_node == vacated_node:
            raise ProtocolRefusal(
                "role_transfer_invalid",
                "the named node already holds the architect role; name a different active node",
            )
        self.active_node(target_node)
        vacated_role = self.role_record(vacated_node)
        if str(vacated_role.get("template_role", "")).casefold() != "architect":
            raise ProtocolRefusal(
                "role_transfer_invalid",
                "the current architect's latest role record is not the architect role",
            )
        target_latest = self._latest("registry_role_record", target_node)
        to_role_before = None if target_latest is None else str(target_latest["template_role"])
        vacated_next = self._role_after_vacating(
            vacated_role, target_node=target_node, declared_templates=declared_templates
        )

        vacated_record: Dict[str, Any] = {
            "schema_version": 0,
            "id": "registry-role-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "registry_role_record",
            "node_id": vacated_node,
            "template_role": vacated_next["template_role"],
            "template_version": vacated_next["template_version"],
            "template_sha256": vacated_next["template_sha256"],
            "answers": vacated_next["answers"],
            "state": "active",
            "predecessor_role_record_id": vacated_role["id"],
        }
        target_record: Dict[str, Any] = {
            "schema_version": 0,
            "id": "registry-role-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "registry_role_record",
            "node_id": target_node,
            "template_role": architect_template.role,
            "template_version": architect_template.template_version,
            "template_sha256": architect_template.digest,
            "answers": dict(vacated_role["answers"]),
            "state": "active",
            "predecessor_role_record_id": (
                None if target_latest is None else target_latest["id"]
            ),
        }
        receipt: Dict[str, Any] = {
            "schema_version": 0,
            "id": "registry-role-transfer-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "registry_role_transfer",
            "idempotency_key": idempotency_key,
            "from_node": vacated_node,
            "to_node": target_node,
            "from_role_before": str(vacated_role["template_role"]),
            "from_role_after": str(vacated_next["template_role"]),
            "to_role_before": to_role_before,
            "to_role_after": str(vacated_role["template_role"]),
            "target_role_record_id": target_record["id"],
            "vacated_role_record_id": vacated_record["id"],
            "state": "complete",
        }

        def decide(existing: list[Dict[str, Any]], records: Sequence[Dict[str, Any]]) -> None:
            if [row.get("kind") for row in records] != [
                "registry_role_record", "registry_role_record", "registry_role_transfer",
            ]:
                raise ProtocolRefusal(
                    "role_transfer_preview_invalid", "transfer preview shape is invalid"
                )
            for row in existing:
                if (
                    row.get("kind") == "registry_role_transfer"
                    and row.get("idempotency_key") == idempotency_key
                ):
                    raise ProtocolRefusal(
                        "idempotency_conflict",
                        f"transfer key already names receipt {row['id']}",
                    )
            entry_latest: Dict[str, Dict[str, Any]] = {}
            existing_role_latest: Dict[str, Dict[str, Any]] = {}
            composed_role_latest: Dict[str, Dict[str, Any]] = {}
            for row in existing:
                if row.get("kind") == "registry_entry":
                    entry_latest[str(row["node_id"])] = row
                elif row.get("kind") == "registry_role_record":
                    existing_role_latest[str(row["node_id"])] = row
            composed_role_latest.update(existing_role_latest)
            for row in records:
                if row.get("kind") == "registry_role_record":
                    composed_role_latest[str(row["node_id"])] = row

            def architects(nodes_latest: Mapping[str, Dict[str, Any]]) -> list[str]:
                return sorted(
                    node
                    for node, entry in entry_latest.items()
                    if entry.get("state") == "active"
                    and (nodes_latest.get(node, {}).get("state") == "active")
                    and str(
                        nodes_latest.get(node, {}).get("template_role", "")
                    ).casefold()
                    == "architect"
                )

            if architects(existing_role_latest) != [vacated_node]:
                raise ProtocolRefusal(
                    "role_architect_invalid",
                    "fleet must have one active architect",
                    remedy=ARCHITECT_REMEDY,
                )
            if architects(composed_role_latest) != [target_node]:
                raise ProtocolRefusal(
                    "role_architect_invalid",
                    "the transfer would not leave exactly one active architect",
                    remedy=ARCHITECT_REMEDY,
                )
            if vacated_record["predecessor_role_record_id"] != (
                existing_role_latest.get(vacated_node, {}) or {}
            ).get("id"):
                raise ProtocolRefusal(
                    "role_transfer_preview_invalid", "vacated predecessor is stale"
                )
            expected_target_predecessor = existing_role_latest.get(target_node)
            expected_target_predecessor_id = (
                None if expected_target_predecessor is None else expected_target_predecessor["id"]
            )
            if target_record["predecessor_role_record_id"] != expected_target_predecessor_id:
                raise ProtocolRefusal(
                    "role_transfer_preview_invalid", "target predecessor is stale"
                )
            if (
                receipt["vacated_role_record_id"] != vacated_record["id"]
                or receipt["target_role_record_id"] != target_record["id"]
                or receipt["from_node"] != vacated_node
                or receipt["to_node"] != target_node
            ):
                raise ProtocolRefusal(
                    "role_transfer_preview_invalid", "transfer receipt binding is invalid"
                )

        self._commit((vacated_record, target_record, receipt), decide)
        return {"receipt": receipt, "replayed": False}

    def _role_after_vacating(
        self,
        vacated_role: Dict[str, Any],
        *,
        target_node: str,
        declared_templates: Mapping[str, "RoleTemplate"],
    ) -> Dict[str, Any]:
        """Resolve the role the vacated node holds after handing over."""

        prior: Optional[Dict[str, Any]] = None
        for record in self._records():
            if (
                record.get("kind") == "registry_role_record"
                and record.get("node_id") == vacated_role["node_id"]
                and str(record.get("template_role", "")).casefold() != "architect"
                and record.get("id") != vacated_role["id"]
            ):
                prior = record
        if prior is not None:
            declared = declared_templates.get(str(prior["template_role"]))
            if declared is None:
                raise ProtocolRefusal(
                    "role_template_unknown",
                    "the vacated node's prior role "
                    f"{str(prior['template_role'])!r} is not declared",
                    remedy="reassign the role explicitly; its template is no longer declared",
                )
            return {
                "template_role": declared.role,
                "template_version": declared.template_version,
                "template_sha256": declared.digest,
                "answers": dict(prior["answers"]),
            }
        inherited = dict(vacated_role.get("answers") or {})
        missing = sorted(
            key for key in ("repo", "never_touch") if not str(inherited.get(key) or "").strip()
        )
        if missing:
            raise ProtocolRefusal(
                "role_transfer_invalid",
                "cannot compose the shipped default role; the architect record lacks answers: "
                + ", ".join(missing),
            )
        answers = {
            "repo": str(inherited["repo"]),
            "never_touch": str(inherited["never_touch"]),
            "reports_to": target_node,
        }
        fallback_template = declared_templates.get("builder")
        if fallback_template is None:
            raise ProtocolRefusal(
                "role_template_unknown",
                "the shipped default role template is not shipped",
                remedy="reinstall the governed bundle; roles/shipped must carry the builder template",
            )
        return {
            "template_role": fallback_template.role,
            "template_version": fallback_template.template_version,
            "template_sha256": fallback_template.digest,
            "answers": answers,
        }

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

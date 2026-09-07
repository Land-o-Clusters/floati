"""ARCH-1: `role transfer-architect` moves the role in the role-record vocabulary.

Ruling docs/rulings/2026-09-05-arch-1-the-role-lives-in-the-role-record.md:
one transfer writes TWO `registry_role_record`s under one idempotency key
(the target becomes architect, the vacated node takes its prior role or the
shipped default), the composed state is checked before anything writes, the
entry `role` field is never touched, and the transfer receipt is its own
record kind because the role record's field set is closed.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from floati.admin_registry import ARCHITECT_REMEDY, RegistryAdminBackend
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.mcp import run_cli_artifact
from floati.registry import Registry
from floati.role_assignment import RoleAssignmentPlan, RoleStepWizard
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

ARCHITECT_ANSWERS = ("floati", "foreign-project", "owner-tier")


class TransferArchitectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root_path = Path(self.temporary.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.root_path, create=True)
        self.backend = RegistryAdminBackend(self.root)
        self.templates = load_shipped_role_templates(Path("roles/shipped"))

    def _register(self, node: str) -> None:
        Registry(self.root).register(node, "Codex")

    def _assign(self, node: str, template: str, answers: tuple[str, ...]) -> None:
        wizard = RoleStepWizard(
            self.root,
            self.backend,
            self.templates,
            id_factory=uuid7_hex,
        )
        wizard.assign_from_keys([node, template, *answers], io.StringIO())

    def _assign_architect(self, node: str) -> None:
        self._assign(node, "architect", ARCHITECT_ANSWERS)

    def _commit_record(
        self, node: str, template_role: str, predecessor: Optional[str]
    ) -> Dict[str, Any]:
        """Append one role record exactly as the wizard shapes them."""

        template = self.templates[template_role]
        record: Dict[str, Any] = {
            "schema_version": 0,
            "id": "registry-role-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            "kind": "registry_role_record",
            "node_id": node,
            "template_role": template.role,
            "template_version": template.template_version,
            "template_sha256": template.digest,
            "answers": {
                "repo": "floati",
                "never_touch": "foreign-project",
                "reports_to": "architect-x",
            },
            "state": "active",
            "predecessor_role_record_id": predecessor,
        }
        self.backend.commit_role(
            RoleAssignmentPlan(node_id=node, template_role=template.role, record=record)
        )
        return record

    def _transfer(self, to: str, key: str) -> tuple[int, Dict[str, Any]]:
        return run_cli_artifact([
            "role", "transfer-architect",
            "--root", str(self.root_path),
            "--to", to,
            "--idempotency-key", key,
        ])

    def _role_records(self) -> list[Dict[str, Any]]:
        from floati.jsonl import read_records_snapshot
        from floati.registry import REGISTRY_KINDS

        return [
            dict(record)
            for record in read_records_snapshot(
                self.root,
                Path("registry/entries.jsonl"),
                allowed_kinds=REGISTRY_KINDS,
            )
            if record.get("kind") == "registry_role_record"
        ]

    def _transfer_receipts(self) -> list[Dict[str, Any]]:
        from floati.jsonl import read_records_snapshot
        from floati.registry import REGISTRY_KINDS

        return [
            dict(record)
            for record in read_records_snapshot(
                self.root,
                Path("registry/entries.jsonl"),
                allowed_kinds=REGISTRY_KINDS,
            )
            if record.get("kind") == "registry_role_transfer"
        ]

    def test_transfer_moves_the_role_to_the_named_node(self) -> None:
        """RED: no verb parses - the documented flow could not hand over the role."""

        self._register("architect-a")
        self._register("lane-b")
        self._assign_architect("architect-a")

        exit_code, artifact = self._transfer("lane-b", "transfer-e2e-1")

        self.assertEqual(0, exit_code, artifact)
        self.assertEqual("ok", artifact["status"])
        self.assertEqual(
            "lane-b",
            self.backend.current_architect()["node_id"],
        )
        vacated = self.backend.role_record("architect-a")
        self.assertEqual("builder", vacated["template_role"])
        self.assertEqual("lane-b", vacated["answers"]["reports_to"])

    def test_transfer_receipt_names_both_nodes_and_both_roles(self) -> None:
        """Catches a transfer whose receipt cannot be audited."""

        self._register("architect-a")
        self._register("lane-b")
        self._assign_architect("architect-a")

        _, artifact = self._transfer("lane-b", "transfer-e2e-2")

        evidence = artifact["evidence"]
        receipt = evidence["receipt"]
        self.assertEqual("architect-a", receipt["from_node"])
        self.assertEqual("architect", receipt["from_role_before"])
        self.assertEqual("builder", receipt["from_role_after"])
        self.assertEqual("lane-b", receipt["to_node"])
        self.assertEqual("architect", receipt["to_role_after"])

    def test_transfer_with_prior_role_restores_it(self) -> None:
        """Catches the vacated node losing the role it held before architect."""

        self._register("architect-a")
        self._register("lane-b")
        self._assign("architect-a", "reviewer", ("floati", "foreign-project", "architect-x"))
        prior = self.backend.role_record("architect-a")
        self._commit_record("architect-a", "architect", prior["id"])

        exit_code, artifact = self._transfer("lane-b", "transfer-e2e-3")

        self.assertEqual(0, exit_code, artifact)
        vacated = self.backend.role_record("architect-a")
        self.assertEqual("reviewer", vacated["template_role"])

    def test_composed_state_refuses_and_writes_nothing(self) -> None:
        """Catches a transfer composing zero or two architects."""

        self._register("architect-a")
        self._register("architect-b")
        self._register("lane-b")
        self._assign_architect("architect-a")
        self._assign_architect("architect-b")
        records_before = self._role_records()
        receipts_before = self._transfer_receipts()

        exit_code, artifact = self._transfer("lane-b", "transfer-e2e-4")

        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual(records_before, self._role_records())
        self.assertEqual(receipts_before, self._transfer_receipts())

        inactive_exit, inactive_artifact = self._transfer("ghost-node", "transfer-e2e-5")
        self.assertEqual(20, inactive_exit, inactive_artifact)
        self.assertEqual("refused", inactive_artifact["status"])
        self.assertEqual(records_before, self._role_records())

    def test_retired_node_with_stale_architect_record_is_excluded(self) -> None:
        """ARCH-0-F1: witnesses the active-node filter the invariant leans on.

        A retired node whose latest role record still says architect must not
        satisfy the invariant: `active_node_ids()` filters it mechanically,
        and this pin makes that exclusion a measured behavior.
        """

        self._register("architect-a")
        self._register("lane-b")
        self._assign_architect("architect-a")
        Registry(self.root).retire("architect-a")

        with self.assertRaises(ProtocolRefusal) as raised:
            self.backend.current_architect()
        self.assertEqual("role_architect_invalid", raised.exception.code)
        self.assertEqual(ARCHITECT_REMEDY, raised.exception.remedy)

    def test_self_transfer_refuses_by_code_under_a_fresh_key(self) -> None:
        """ARCH-1 Am.2: naming the current architect under a new key refuses.

        The already-architect target is the coded case, not the replay path:
        role_transfer_invalid, and nothing is written.
        """

        self._register("architect-a")
        self._register("lane-b")
        self._assign_architect("architect-a")
        records_before = self._role_records()
        receipts_before = self._transfer_receipts()

        exit_code, artifact = self._transfer("architect-a", "transfer-self-1")

        self.assertEqual(20, exit_code, artifact)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("role_transfer_invalid", artifact["evidence"]["code"])
        self.assertEqual(records_before, self._role_records())
        self.assertEqual(receipts_before, self._transfer_receipts())

    def test_replay_under_the_same_key_is_a_no_op_naming_the_first(self) -> None:
        """Catches a replayed transfer double-writing the role records."""

        self._register("architect-a")
        self._register("lane-b")
        self._assign_architect("architect-a")

        first_exit, first = self._transfer("lane-b", "transfer-e2e-6")
        self.assertEqual(0, first_exit, first)
        first_receipts = self._transfer_receipts()
        self.assertEqual(1, len(first_receipts))

        replay_exit, replay = self._transfer("lane-b", "transfer-e2e-6")
        self.assertEqual(0, replay_exit, replay)
        self.assertEqual(
            first_receipts[0]["id"],
            replay["evidence"]["replayed_receipt_id"],
        )
        self.assertEqual(1, len(self._transfer_receipts()))
        self.assertEqual(
            1 + 2,
            len(self._role_records()),
        )


    def test_transfer_writes_the_declared_template_by_digest(self) -> None:
        """ARCH-DECL-1-F1: the transfer pins the DECLARED template, not a copy.

        Measured on the live root: the transfer copied the predecessor's
        stale pin (template_version 1, template_sha256 7ac392eb…) into
        the new architect record while the declared template is version
        2, so doctor's delivery health refused with
        `delivery_health_unavailable` — "active role evidence for the
        seated architect node does not match a declared template". The
        reader is right: a role record the product writes must validate
        against the template the product ships, by digest.
        """

        from floati.doctor import _role_cadences

        self._register("architect-x")
        self._register("builder-x")
        stale: Dict[str, Any] = {
            "schema_version": 0,
            "id": "registry-role-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": (
                datetime.now(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            ),
            "kind": "registry_role_record",
            "node_id": "architect-x",
            "template_role": "architect",
            "template_version": 1,
            "template_sha256": (
                "7ac392ebe9f68676e117282d81e65cccfb7d19cf249e1159f012bfa89b689d24"
            ),
            "answers": {
                "repo": "floati",
                "never_touch": "foreign-project",
                "reports_to": "owner-tier",
            },
            "state": "active",
            "predecessor_role_record_id": None,
        }
        self.backend.commit_role(
            RoleAssignmentPlan(
                node_id="architect-x", template_role="architect", record=stale
            )
        )

        exit_code, artifact = self._transfer("builder-x", "arch-decl-1")
        self.assertEqual(0, exit_code, artifact)

        declared = self.templates["architect"]
        target = next(
            row
            for row in self._role_records()
            if row["node_id"] == "builder-x" and row["template_role"] == "architect"
        )
        self.assertEqual(declared.template_version, target["template_version"])
        self.assertEqual(declared.digest, target["template_sha256"])
        vacated = next(
            row
            for row in reversed(self._role_records())
            if row["node_id"] == "architect-x"
        )
        builder = self.templates["builder"]
        self.assertEqual(builder.template_version, vacated["template_version"])
        self.assertEqual(builder.digest, vacated["template_sha256"])

        cadences = _role_cadences(
            Path(__file__).resolve().parents[1],
            ["architect-x", "builder-x"],
            self._role_records(),
            root=self.root,
        )
        self.assertEqual(
            {"architect-x": builder.cadence, "builder-x": declared.cadence},
            cadences,
        )


if __name__ == "__main__":
    unittest.main()

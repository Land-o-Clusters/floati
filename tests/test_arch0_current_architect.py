"""ARCH-0: the architect invariant reads the wrong field.

`current_architect()` scanned the registry ENTRY's ``role`` field, but the
documented fleet flow writes the HARNESS there and ``node role --template
architect`` writes a ``registry_role_record`` the invariant never read: after
the documented flow the fleet's own architect invariant refused with zero
architects, and the shipped ``<architect>`` default refused the same way.
The ruling (docs/rulings/2026-09-05-arch-1-the-role-lives-in-the-role-record.md)
fixes the resolution: the architect is the one active node whose LATEST role
record says ``template_role=architect``.
"""

from __future__ import annotations

import io
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from floati.admin_registry import ARCHITECT_REMEDY, RegistryAdminBackend
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.role_assignment import RoleAssignmentPlan, RoleStepWizard
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT

ARCHITECT_ANSWERS = ("floati", "foreign-project", "owner-tier")
TRANSFER_VERB = "role transfer-architect --to NODE"
assert TRANSFER_VERB in ARCHITECT_REMEDY
ARCHITECT_ANSWERS_MAP = {
    "architect": {
        "repo": "floati",
        "never_touch": "foreign-project",
        "owner_stops": "owner-tier",
    },
    "builder": {
        "repo": "floati",
        "never_touch": "foreign-project",
        "reports_to": "architect-a",
    },
}


class CurrentArchitectResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
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

    def test_documented_flow_resolves_the_assigned_architect(self) -> None:
        """Catches the invariant reading the harness field instead of the role record."""

        self._register("architect-a")
        self._register("lane-a")
        self._assign_architect("architect-a")

        resolved = self.backend.current_architect()
        self.assertEqual("architect-a", resolved["node_id"])
        self.assertEqual("active", resolved["state"])

    def test_zero_architects_refuse_naming_the_transfer_verb(self) -> None:
        """Catches a roleless fleet refusing without the transfer remedy."""

        self._register("architect-a")
        self._register("lane-a")

        with self.assertRaises(ProtocolRefusal) as raised:
            self.backend.current_architect()
        self.assertEqual("role_architect_invalid", raised.exception.code)
        self.assertEqual(ARCHITECT_REMEDY, raised.exception.remedy)

    def test_two_architects_refuse_naming_the_transfer_verb(self) -> None:
        """Catches an ambiguous fleet refusing without the transfer remedy."""

        self._register("architect-a")
        self._register("architect-b")
        self._assign_architect("architect-a")
        self._assign_architect("architect-b")

        with self.assertRaises(ProtocolRefusal) as raised:
            self.backend.current_architect()
        self.assertEqual("role_architect_invalid", raised.exception.code)
        self.assertEqual(ARCHITECT_REMEDY, raised.exception.remedy)

    def test_architect_default_resolves_the_assigned_architect(self) -> None:
        """Catches the shipped <architect> default refusing in the documented flow."""

        self._register("architect-a")
        self._register("lane-a")
        self._assign_architect("architect-a")

        wizard = RoleStepWizard(
            self.root,
            self.backend,
            self.templates,
            id_factory=uuid7_hex,
        )
        wizard.assign_from_keys(
            ["lane-a", "builder", "floati", "foreign-project", ""],
            io.StringIO(),
        )
        committed = self.backend.role_record("lane-a")
        self.assertEqual("architect-a", committed["answers"]["reports_to"])

    def test_latest_role_record_wins_over_a_stale_one(self) -> None:
        """Catches an earlier role record shadowing the node's current role."""

        self._register("architect-a")
        self._register("lane-a")
        self._assign_architect("architect-a")

        stale = self._commit_record("lane-a", "architect", None)
        self._commit_record("lane-a", "builder", stale["id"])

        resolved = self.backend.current_architect()
        self.assertEqual("architect-a", resolved["node_id"])

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
            "answers": dict(ARCHITECT_ANSWERS_MAP.get(template_role, {})),
            "state": "active",
            "predecessor_role_record_id": predecessor,
        }
        self.backend.commit_role(
            RoleAssignmentPlan(node_id=node, template_role=template.role, record=record)
        )
        return record


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from floati.errors import ProtocolRefusal
from floati.role_assignment import RoleAssignmentPlan, RoleStepWizard
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from tests.schema_validation import SchemaValidationError, validate_json_schema


ID_ASSIGN = "018f7e9b3c137abc8def0123456789ab"
ID_NODE = "018f7e9b3c137abc9def0123456789ab"
ID_ARCHITECT = "018f7e9b3c137abcbdef0123456789ab"


class RecordingRoleBackend:
    def __init__(self, preview_stream: io.StringIO) -> None:
        self.preview_stream = preview_stream
        self.node = {
            "schema_version": 0,
            "id": "registry-" + ID_NODE,
            "tenant_id": "fleet",
            "timestamp": "2026-08-27T20:00:00.000Z",
            "kind": "registry_entry",
            "node_id": "alpha",
            "role": "Codex",
            "state": "active",
        }
        self.architect = {
            "schema_version": 0,
            "id": "registry-" + ID_ARCHITECT,
            "tenant_id": "fleet",
            "timestamp": "2026-08-27T20:00:00.000Z",
            "kind": "registry_entry",
            "node_id": "architect-a",
            "role": "Architect",
            "state": "active",
        }
        self.node_reads = 0
        self.architect_reads = 0
        self.committed: Optional[RoleAssignmentPlan] = None

    def active_node(self, node_id: str) -> dict[str, Any]:
        self.node_reads += 1
        if node_id != self.node["node_id"]:
            raise ProtocolRefusal("unknown_node", "node is not active")
        return dict(self.node)

    def current_architect(self) -> dict[str, Any]:
        self.architect_reads += 1
        return dict(self.architect)

    def commit_role(self, plan: RoleAssignmentPlan) -> dict[str, Any]:
        previews = [
            json.loads(line.removeprefix("ledger preview: "))
            for line in self.preview_stream.getvalue().splitlines()
            if line.startswith("ledger preview: ")
        ]
        if not previews or previews[-1] != plan.record:
            raise AssertionError("the exact role record must be flushed before commit")
        self.committed = plan
        return {"record": dict(plan.record)}


class RoleStepWizardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.output = io.StringIO()
        self.backend = RecordingRoleBackend(self.output)
        self.templates = load_shipped_role_templates(Path("roles/shipped"))
        self.wizard = RoleStepWizard(
            self.root,
            self.backend,
            self.templates,
            id_factory=lambda: ID_ASSIGN,
            now=lambda: datetime(2026, 8, 27, 23, 15, tzinfo=timezone.utc),
        )

    def test_role_step_previews_exact_version_bound_record_before_one_commit(self) -> None:
        """Catches the wizard persisting answers without exact template provenance."""
        result = self.wizard.assign_from_keys(
            ["alpha", "builder", "floati", "shared-core", "architect-a"],
            self.output,
        )

        template = self.templates["builder"]
        expected = {
            "schema_version": 0,
            "id": "registry-role-" + ID_ASSIGN,
            "tenant_id": "fleet",
            "timestamp": "2026-08-27T23:15:00.000Z",
            "kind": "registry_role_record",
            "node_id": "alpha",
            "template_role": "builder",
            "template_version": 2,
            "template_sha256": template.digest,
            "answers": {
                "repo": "floati",
                "never_touch": "shared-core",
                "reports_to": "architect-a",
            },
            "state": "active",
            "predecessor_role_record_id": None,
        }
        self.assertEqual(expected, result["record"])
        self.assertEqual(expected, self.backend.committed.record)
        validate_json_schema(
            result["record"], Path("schemas/v0/registry-role-record.schema.json")
        )

    def test_architect_default_is_resolved_from_live_registry_evidence(self) -> None:
        """Catches a wizard reusing a cached architect from a stale fleet map."""
        self.backend.architect["node_id"] = "architect-new"

        result = self.wizard.assign_from_keys(
            ["alpha", "builder", "floati", "shared-core", ""], self.output
        )

        self.assertEqual("architect-new", result["record"]["answers"]["reports_to"])
        self.assertEqual(1, self.backend.architect_reads)
        self.assertNotIn("fleet_map", result["record"])
        self.assertNotIn("boot_command", result["record"])

    def test_explicit_reports_to_answer_does_not_read_architect_state(self) -> None:
        """Catches an explicit answer being overwritten by ambient registry state."""
        result = self.wizard.assign_from_keys(
            ["alpha", "builder", "floati", "shared-core", "lead-b"], self.output
        )

        self.assertEqual("lead-b", result["record"]["answers"]["reports_to"])
        self.assertEqual(0, self.backend.architect_reads)

    def test_plain_fallback_asks_only_the_selected_template_questions(self) -> None:
        """Catches the interview inventing questions outside the typed template."""
        self.wizard.assign_plain(
            io.StringIO("alpha\nbuilder\nfloati\nshared-core\n\n"), self.output
        )

        rendered = self.output.getvalue()
        for question in self.templates["builder"].questions:
            self.assertIn(question.ask, rendered)
        self.assertNotIn("owner-tier decisions", rendered)
        self.assertEqual("architect-a", self.backend.committed.record["answers"]["reports_to"])

    def test_answer_count_refuses_before_registry_lookup_or_preview(self) -> None:
        """Catches missing or extra answers being guessed into a role record."""
        cases = (
            ["alpha", "builder", "floati", "shared-core"],
            ["alpha", "builder", "floati", "shared-core", "architect-a", "extra"],
        )
        for values in cases:
            with self.subTest(count=len(values)):
                with self.assertRaises(ProtocolRefusal) as raised:
                    self.wizard.assign_from_keys(values, self.output)
                self.assertEqual("wizard_input_invalid", raised.exception.code)
                self.assertEqual(0, self.backend.node_reads)
                self.assertIsNone(self.backend.committed)
                self.assertEqual("", self.output.getvalue())

    def test_blank_required_answer_refuses_before_registry_lookup(self) -> None:
        """Catches an undeclared default being invented for a required question."""
        with self.assertRaises(ProtocolRefusal) as raised:
            self.wizard.assign_from_keys(
                ["alpha", "builder", "", "shared-core", "architect-a"], self.output
            )

        self.assertEqual("role_answer_required", raised.exception.code)
        self.assertEqual(0, self.backend.node_reads)
        self.assertIsNone(self.backend.committed)

    def test_cross_tenant_or_retired_node_refuses_before_preview(self) -> None:
        """Catches non-active fleet evidence authorizing a role assignment."""
        cases = (("tenant_id", "foreign"), ("state", "retired"))
        for field, value in cases:
            with self.subTest(field=field):
                original = self.backend.node[field]
                self.backend.node[field] = value
                with self.assertRaises(ProtocolRefusal) as raised:
                    self.wizard.assign_from_keys(
                        ["alpha", "builder", "floati", "shared-core", "architect-a"],
                        self.output,
                    )
                self.assertEqual("role_assignment_invalid", raised.exception.code)
                self.assertIsNone(self.backend.committed)
                self.assertEqual("", self.output.getvalue())
                self.backend.node[field] = original

    def test_invalid_live_architect_evidence_refuses_without_commit(self) -> None:
        """Catches an inactive or foreign architect satisfying the live default."""
        self.backend.architect["tenant_id"] = "foreign"

        with self.assertRaises(ProtocolRefusal) as raised:
            self.wizard.assign_from_keys(
                ["alpha", "builder", "floati", "shared-core", ""], self.output
            )

        self.assertEqual("role_architect_invalid", raised.exception.code)
        self.assertIsNone(self.backend.committed)
        self.assertEqual("", self.output.getvalue())

    def test_schema_forbids_cached_projection_fields(self) -> None:
        """Catches a durable role record growing stale boot projection state."""
        result = self.wizard.assign_from_keys(
            ["alpha", "builder", "floati", "shared-core", "architect-a"],
            self.output,
        )
        stale = dict(result["record"])
        stale["fleet_map"] = {"architect": "architect-old"}

        with self.assertRaises(SchemaValidationError):
            validate_json_schema(
                stale, Path("schemas/v0/registry-role-record.schema.json")
            )


if __name__ == "__main__":
    unittest.main()

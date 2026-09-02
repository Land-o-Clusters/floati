from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Mapping, Sequence

from floati.errors import ProtocolRefusal
from floati.node_projections import (
    ManagedVerbShape,
    NodeBootProjection,
    NodeTeardownProjection,
    render_node_projection,
)
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from tests.schema_validation import SchemaValidationError, validate_json_schema


ROLE_ID = "018f7e9b3c137abc8def0123456789ab"


class MutableLedgerSource:
    """A deterministic live-source fixture whose ledgers can change between reads."""

    def __init__(self, root: FloatiRoot, templates: Mapping[str, Any]) -> None:
        self.root = root
        self.templates = templates
        self.nodes: list[dict[str, object]] = [
            {
                "schema_version": 0,
                "id": "registry-architect-018f7e9b3c137abc8def0123456789ab",
                "tenant_id": root.tenant_id,
                "node_id": "architect-a",
                "role": "architect",
                "harness": "Claude",
                "state": "active",
            },
            {
                "schema_version": 0,
                "id": "registry-builder-018f7e9b3c137abc8def0123456789ab",
                "tenant_id": root.tenant_id,
                "node_id": "builder-a",
                "role": "builder",
                "harness": "Codex",
                "state": "active",
            },
            {
                "schema_version": 0,
                "id": "registry-reviewer-018f7e9b3c137abc8def0123456789ab",
                "tenant_id": root.tenant_id,
                "node_id": "reviewer-a",
                "role": "reviewer",
                "harness": "Codex",
                "state": "active",
            },
        ]
        self.roles: dict[str, dict[str, object]] = {
            "builder-a": {
                "schema_version": 0,
                "id": "registry-role-" + ROLE_ID,
                "tenant_id": root.tenant_id,
                "timestamp": "2026-08-27T23:15:00.000Z",
                "kind": "registry_role_record",
                "node_id": "builder-a",
                "template_role": "builder",
                "template_version": 1,
                "template_sha256": templates["builder"].digest,
                "answers": {
                    "repo": "floati",
                    "never_touch": "shared-core",
                    "reports_to": "architect-a",
                },
                "state": "active",
                "predecessor_role_record_id": None,
            }
        }
        self.declared: list[dict[str, object]] = [
            {
                "bus_id": "fleet",
                "root": str(root.path),
                "architect_node": "architect-a",
                "downstream": [],
            }
        ]
        self.wakes = {"builder-a": "armed"}
        self.verbs = {
            "Codex": ManagedVerbShape(
                harness="Codex",
                executable="~/.codex/bin/codex-fleet-bus",
                profile=public_ids.compose('puddle-floati-', public_ids.builder('puddle')),
            )
        }

    def active_node(self, node_id: str) -> Mapping[str, object]:
        for node in self.nodes:
            if node.get("node_id") == node_id:
                return dict(node)
        raise ProtocolRefusal("node_projection_node_missing", "node is not active")

    def active_nodes(self) -> Sequence[Mapping[str, object]]:
        return [dict(node) for node in self.nodes]

    def role_record(self, node_id: str) -> Mapping[str, object]:
        record = self.roles.get(node_id)
        if record is None:
            raise ProtocolRefusal("node_projection_role_missing", "role record is absent")
        return dict(record)

    def declared_roots(self) -> Sequence[Mapping[str, object]]:
        return [dict(declaration) for declaration in self.declared]

    def wake_status(self, node_id: str) -> str:
        return self.wakes.get(node_id, "none")

    def managed_verbs(self, node_id: str, harness: str) -> ManagedVerbShape:
        del node_id
        try:
            return self.verbs[harness]
        except KeyError as exc:
            raise ProtocolRefusal(
                "node_projection_managed_bus_missing", "managed bus shape is absent"
            ) from exc


class NodeProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        self.templates = load_shipped_role_templates(
            Path(__file__).parents[1] / "roles" / "shipped"
        )
        self.source = MutableLedgerSource(self.root, self.templates)
        self.boot = NodeBootProjection(
            self.root, "builder-a", self.source, self.templates
        )
        self.teardown = NodeTeardownProjection(
            self.root, "builder-a", self.source, self.templates
        )

    def test_stale_fleet_map_cannot_be_constructed_and_live_reprojection_wins(self) -> None:
        """Catches a boot projection retaining topology from before its invocation."""
        self.source.nodes = [self.source.nodes[0], self.source.nodes[1]]
        self.source.nodes[0] = dict(self.source.nodes[0], node_id="architect-new")
        self.source.declared = [
            dict(self.source.declared[0], architect_node="architect-new")
        ]

        projected = self.boot.project()

        self.assertEqual("architect-new", projected["fleet_map"]["architect_node"])
        self.assertEqual(
            ["architect-new", "builder-a"],
            [row["node_id"] for row in projected["fleet_map"]["nodes"]],
        )
        with self.assertRaises(TypeError):
            NodeBootProjection(
                self.root,
                "builder-a",
                self.source,
                self.templates,
                fleet_map={},
            )

    def test_boot_projects_live_role_fleet_wake_and_exact_managed_bus_shape(self) -> None:
        """Catches a prompt omitting role fences or teaching a seat to guess bus flags."""
        projected = self.boot.project()

        self.assertEqual("node_boot_projection", projected["kind"])
        self.assertEqual("builder-a", projected["node_id"])
        self.assertEqual("Codex", projected["harness"])
        self.assertEqual(
            str(self.root.path / "nodes" / "builder-a"), projected["workspace"]
        )
        self.assertEqual(
            str(self.root.path / "nodes" / "builder-a" / "STATE.md"),
            projected["state_file"],
        )
        self.assertEqual("builder", projected["role"]["template_role"])
        self.assertEqual(
            [
                "Never bypass a failing gate, ownership boundary, or required receipt.",
                "Never treat a local commit as banked until the named ref is pushed.",
            ],
            projected["role"]["fences"],
        )
        self.assertEqual("armed", projected["wake"]["status"])
        self.assertTrue(projected["wake"]["poll_at_row_boundaries"])
        self.assertEqual(
            ["ack", "--id", "--session"],
            projected["managed_bus"]["ack"],
        )
        self.assertEqual(
            ["send", "--to", "--sha", "--doc", "--idempotency-key", "--note"],
            projected["managed_bus"]["send"],
        )
        self.assertEqual(["--reply-to"], projected["managed_bus"]["optional_send"])
        self.assertTrue(projected["prompt"].startswith("Read your state file first:"))
        self.assertIn(projected["role"]["stops"][0], projected["prompt"])
        self.assertIn(projected["role"]["fences"][1], projected["prompt"])
        self.assertIn("--idempotency-key", projected["prompt"])
        self.assertIn(
            "ack --id <message-id> [--id <message-id> ...] --session <session-id>",
            projected["prompt"],
        )
        self.assertIn("floati wake pause --root", projected["prompt"])
        self.assertIn("--as builder-a --session <session-id>", projected["prompt"])
        self.assertIn("floati wake resume --root", projected["prompt"])
        self.assertIn("floati wake status --root", projected["prompt"])

    def test_boot_json_and_plain_render_are_deterministic_ascii_twins(self) -> None:
        """Catches a human board diverging from the machine projection or using unsafe output."""
        first = self.boot.project()
        second = self.boot.project()

        self.assertEqual(first, second)
        self.assertEqual(first, json.loads(self.boot.to_json()))
        rendered = render_node_projection(first)
        rendered.encode("ascii")
        self.assertIn("NODE BOOT PROJECTION", rendered)
        self.assertIn("architect-a", rendered)
        self.assertIn("WAKE: armed; poll at row boundaries: yes", rendered)

    def test_projection_does_not_create_or_interpret_state_or_mutate_live_source(self) -> None:
        """Catches a read-only projection turning state or source ledgers into a side effect."""
        before_nodes = [dict(node) for node in self.source.nodes]
        before_roles = {node_id: dict(record) for node_id, record in self.source.roles.items()}
        before_declared = [dict(declaration) for declaration in self.source.declared]

        self.boot.project()
        self.teardown.project()

        self.assertFalse((self.root.path / "nodes").exists())
        self.assertEqual(before_nodes, self.source.nodes)
        self.assertEqual(before_roles, self.source.roles)
        self.assertEqual(before_declared, self.source.declared)

    def test_teardown_reuses_live_context_and_preserves_ritual_order(self) -> None:
        """Catches teardown becoming a destructive shortcut or losing the banked-work fence."""
        projected = self.teardown.project()

        self.assertEqual("node_teardown_projection", projected["kind"])
        self.assertEqual(projected["fleet_map"], self.boot.project()["fleet_map"])
        self.assertEqual(
            [
                "read_state",
                "flush_state",
                "check_committed_and_banked",
                "push_and_envelope_unbanked",
                "report_drained",
                "close_lease",
                "retire_without_deleting_workspace",
            ],
            [step["kind"] for step in projected["ritual"]],
        )
        ritual_text = "\n".join(step["instruction"] for step in projected["ritual"])
        self.assertLess(ritual_text.index("flush"), ritual_text.index("push"))
        self.assertLess(ritual_text.index("push"), ritual_text.index("DRAINED"))
        self.assertIn("STATE.md", projected["command"])
        self.assertIn("never delete", projected["prompt"])

    def test_unknown_wake_or_managed_bus_shape_refuses_without_guessing(self) -> None:
        """Catches unknown operational state being turned into a fabricated instruction."""
        self.source.wakes["builder-a"] = "maybe"
        with self.assertRaises(ProtocolRefusal) as raised:
            self.boot.project()
        self.assertEqual("node_projection_wake_invalid", raised.exception.code)

        self.source.wakes["builder-a"] = "none"
        self.source.verbs = {}
        with self.assertRaises(ProtocolRefusal) as raised:
            self.boot.project()
        self.assertEqual("node_projection_managed_bus_missing", raised.exception.code)

    def test_role_provenance_mismatch_refuses_before_rendering(self) -> None:
        """Catches a boot prompt projecting a template different from the assigned role record."""
        self.source.roles["builder-a"] = dict(
            self.source.roles["builder-a"], template_role="sre"
        )

        with self.assertRaises(ProtocolRefusal) as raised:
            self.boot.project()

        self.assertEqual("node_projection_role_mismatch", raised.exception.code)

    def test_projection_artifacts_validate_against_the_versioned_schema(self) -> None:
        """Catches a derived artifact growing fields the train cannot consume."""
        schema = Path(__file__).parents[1] / "schemas" / "v0" / "node-lifecycle-projection.schema.json"
        boot = self.boot.project()
        teardown = self.teardown.project()

        validate_json_schema(boot, schema)
        validate_json_schema(teardown, schema)
        stale = dict(boot)
        stale["fleet_map"] = dict(boot["fleet_map"], cached_at="yesterday")
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(stale, schema)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.node_explain import NodeExplainProjection, render_node_explanation
from floati.node_projections import NodeBootProjection
from floati.role_templates import load_shipped_role_templates
from floati.root import FloatiRoot
from tests.test_node_projections import MutableLedgerSource


class NodeExplainProjectionTests(unittest.TestCase):
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
        self.explain = NodeExplainProjection(
            self.root, "builder-a", self.source, self.templates
        )

    def test_explanation_reprojects_live_d3_record_after_fleet_change(self) -> None:
        first = self.explain.project()
        self.source.nodes = [
            dict(node)
            for node in self.source.nodes
            if node["node_id"] != "reviewer-a"
        ]
        self.source.nodes[0] = dict(self.source.nodes[0], node_id="architect-new")
        self.source.declared = [
            dict(self.source.declared[0], architect_node="architect-new")
        ]

        second = self.explain.project()

        self.assertEqual("architect-a", first["fleet_map"]["architect_node"])
        self.assertEqual("architect-new", second["fleet_map"]["architect_node"])
        self.assertEqual(
            ["architect-new", "builder-a"],
            [row["node_id"] for row in second["fleet_map"]["nodes"]],
        )

    def test_json_twin_is_the_same_live_record_as_the_wrapped_boot_projection(self) -> None:
        wrapped = NodeExplainProjection.from_boot(self.boot)

        self.assertEqual(self.boot.project(), wrapped.project())
        self.assertEqual(self.boot.to_json(), wrapped.to_json())
        self.assertEqual(wrapped.project(), json.loads(wrapped.to_json()))

    def test_prose_answers_what_and_why_from_every_current_projection_section(self) -> None:
        rendered = self.explain.render()
        rendered.encode("ascii")

        for expected in (
            "NODE EXPLANATION",
            "WHAT THIS NODE IS: builder-a using harness Codex.",
            "WHY THIS ROLE: builder is assigned from the live role record.",
            "reports_to: architect-a",
            "CURRENT ARCHITECT: architect-a",
            "CURRENT FLEET NODES:",
            "DECLARED ROOTS:",
            "WORKSPACE:",
            "STATE FILE:",
            "WAKE: armed; poll at row boundaries: yes",
            public_ids.compose('MANAGED BUS: ~/.codex/bin/codex-fleet-bus puddle-floati-', public_ids.builder('puddle')),
        ):
            self.assertIn(expected, rendered)

    def test_explanation_has_no_stale_map_constructor_and_no_state_side_effect(self) -> None:
        with self.assertRaises(TypeError):
            NodeExplainProjection(
                self.root,
                "builder-a",
                self.source,
                self.templates,
                fleet_map={},
            )

        self.explain.project()
        self.explain.render()
        self.assertFalse((self.root.path / "nodes").exists())

    def test_renderer_refuses_non_boot_records_before_making_up_an_explanation(self) -> None:
        with self.assertRaises(ProtocolRefusal) as raised:
            render_node_explanation({"kind": "node_teardown_projection"})
        self.assertEqual("node_explain_output_invalid", raised.exception.code)


if __name__ == "__main__":
    unittest.main()

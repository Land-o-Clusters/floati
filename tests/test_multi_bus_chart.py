from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.multi_bus_chart import MultiBusHarborChart, render_multi_bus_chart
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema


NOW = datetime(2026, 8, 27, 22, 0, tzinfo=timezone.utc)


class MultiBusHarborChartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.alpha = FloatiRoot.open_direct_home(self.base / "alpha", create=True)
        self.beta = FloatiRoot.open_direct_home(self.base / "beta", create=True)
        self.foreign = FloatiRoot.open_direct_home(self.base / "undeclared", create=True)
        self._registry(self.alpha, "architect-a", "Architect", "2026-08-27T21:58:00.000Z")
        self._registry(self.alpha, public_ids.builder('a'), "Codex", "2026-08-27T21:59:00.000Z")
        self._registry(self.beta, "architect-b", "Architect", "2026-08-27T21:55:00.000Z")
        self._registry(self.foreign, "hidden", "Codex", "2026-08-27T21:59:59.000Z")
        self.registry_path = self.base / "declared-roots.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "roots": [
                        {
                            "bus_id": "alpha",
                            "root": str(self.alpha.path),
                            "architect_node": "architect-a",
                            "downstream": ["beta"],
                        },
                        {
                            "bus_id": "beta",
                            "root": str(self.beta.path),
                            "architect_node": "architect-b",
                            "downstream": [],
                        },
                    ],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _registry(
        root: FloatiRoot, node: str, role: str, timestamp: str
    ) -> None:
        append_record(
            root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "registry-" + uuid7_hex(),
                "tenant_id": root.tenant_id,
                "timestamp": timestamp,
                "kind": "registry_entry",
                "node_id": node,
                "role": role,
                "state": "active",
            },
            allowed_kinds={"registry_entry"},
        )

    def test_chart_uses_only_declared_roots_and_derives_nodes_from_ledgers(self) -> None:
        """Catches chart discovery scanning a declared root's parent."""
        artifact = MultiBusHarborChart(self.registry_path, now=NOW).artifact()

        self.assertEqual(["alpha", "beta"], [bus["bus_id"] for bus in artifact["buses"]])
        self.assertEqual(
            ["architect-a", public_ids.builder('a')],
            [node["id"] for node in artifact["buses"][0]["nodes"]],
        )
        self.assertNotIn("undeclared", json.dumps(artifact))
        self.assertNotIn("hidden", json.dumps(artifact))

    def test_json_twin_is_deterministic_and_includes_architect_edges_and_activity_age(self) -> None:
        """Catches nondeterministic topology or wall-clock testimony in the JSON twin."""
        chart = MultiBusHarborChart(self.registry_path, now=NOW)

        first = chart.artifact()
        second = chart.artifact()

        self.assertEqual(first, second)
        self.assertEqual(60, first["buses"][0]["last_activity_age_seconds"])
        self.assertEqual("architect-a", first["buses"][0]["architect_node"])
        self.assertEqual(
            [{"source": "alpha", "target": "beta"}], first["relationships"]
        )
        self.assertEqual(first, json.loads(chart.to_json()))

    def test_ascii_twin_contains_only_ascii_and_the_same_bus_relationships(self) -> None:
        """Catches the human chart diverging from JSON topology or using terminal-only glyphs."""
        artifact = MultiBusHarborChart(self.registry_path, now=NOW).artifact()

        rendered = render_multi_bus_chart(artifact)

        rendered.encode("ascii")
        self.assertIn("FLOATI // MULTI-BUS HARBOR CHART", rendered)
        self.assertIn("alpha [architect: architect-a] [last activity: 60s]", rendered)
        self.assertIn("alpha -> beta", rendered)
        self.assertNotIn("undeclared", rendered)

    def test_registry_refuses_relative_duplicate_or_unknown_downstream_roots(self) -> None:
        """Catches implicit path resolution or ambiguous topology being guessed."""
        cases = (
            {
                "schema_version": 0,
                "roots": [{
                    "bus_id": "alpha", "root": "relative",
                    "architect_node": "architect-a", "downstream": [],
                }],
            },
            {
                "schema_version": 0,
                "roots": [
                    {
                        "bus_id": "alpha", "root": str(self.alpha.path),
                        "architect_node": "architect-a", "downstream": [],
                    },
                    {
                        "bus_id": "alpha", "root": str(self.beta.path),
                        "architect_node": "architect-b", "downstream": [],
                    },
                ],
            },
            {
                "schema_version": 0,
                "roots": [{
                    "bus_id": "alpha", "root": str(self.alpha.path),
                    "architect_node": "architect-a", "downstream": ["missing"],
                }],
            },
        )
        for index, payload in enumerate(cases):
            with self.subTest(case=index):
                path = self.base / f"invalid-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(ProtocolRefusal):
                    MultiBusHarborChart(path, now=NOW).artifact()

    def test_chart_is_read_only_for_registry_and_every_declared_bus(self) -> None:
        """Catches chart rendering creating locks, receipts, or repair files."""
        before = self._snapshot()

        MultiBusHarborChart(self.registry_path, now=NOW).artifact()

        self.assertEqual(before, self._snapshot())

    def test_declared_roots_file_validates_against_the_published_schema(self) -> None:
        """Catches runtime accepting a declaration shape the published contract omits."""
        validate_json_schema(
            json.loads(self.registry_path.read_text(encoding="utf-8")),
            Path("schemas/v0/declared-roots.schema.json"),
        )

    def _snapshot(self) -> dict[str, bytes]:
        return {
            str(path.relative_to(self.base)): path.read_bytes()
            for path in self.base.rglob("*")
            if path.is_file()
        }


if __name__ == "__main__":
    unittest.main()

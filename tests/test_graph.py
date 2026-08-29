from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts
from floati.runtruth import RunLedger
from tests.schema_validation import validate_json_schema

from floati.graph import HarborGraph, HarborTraffic
from floati.graph_render import render_harbor_chart


NOW = datetime(2026, 8, 1, 13, 0, 0, tzinfo=timezone.utc)


class HarborGraphContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "alpha"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        registry.register("lane-b", "Claude")
        registry.register("lane-a", "Codex")

    def graph(self):
        self.assertIsNotNone(HarborGraph, "Harbor Chart projector must exist")
        return HarborGraph(self.root)

    def traffic(self):
        self.assertIsNotNone(HarborTraffic, "Harbor Chart traffic projector must exist")
        return HarborTraffic(self.root)

    def test_topology_is_sorted_typed_and_derived_only_from_ledgers(self) -> None:
        work = WorkLog(self.root)
        first = work.add("first", "lane-a", [], now=NOW)
        second = work.add("second", "lane-b", [], needs=[str(first["id"])], now=NOW)
        authority = AuthorityGrantStore(self.root).claim(
            "build", "lane-a", 300, 240, NOW
        )
        work.claim(
            str(first["id"]), "lane-a", "build", int(authority["epoch"]), now=NOW
        )
        session_id = "worker-" + uuid7_hex()
        WorkerReceipts(self.root).append(
            session_id, str(first["id"]), "lane-a", "codex", "claim", None, [], now=NOW
        )

        first_artifact = self.graph().artifact()
        second_artifact = self.graph().artifact()

        self.assertEqual(first_artifact, second_artifact)
        self.assertEqual(0, first_artifact["schema_version"])
        self.assertEqual("0", first_artifact["topology_version"])
        self.assertEqual("alpha", first_artifact["tenant_id"])
        self.assertEqual(["lane-a", "lane-b"], [row["id"] for row in first_artifact["nodes"]])
        self.assertEqual("node", first_artifact["nodes"][0]["kind"])
        self.assertEqual(
            [{
                "kind": "work_dependency",
                "source": str(first["id"]),
                "target": str(second["id"]),
                "requires": "accepted",
                "failure_policy": "fail_run",
            }],
            first_artifact["edges"],
        )
        self.assertEqual(session_id, first_artifact["workers"][0]["id"])
        self.assertEqual("worker", first_artifact["workers"][0]["kind"])
        self.assertEqual([], first_artifact["bridge_stubs"])
        self.assertNotIn("observed_at", first_artifact)

    def test_corrupt_allowlisted_ledger_is_not_projected_as_empty(self) -> None:
        registry = self.root.resolve_relative("registry/entries.jsonl")
        with registry.open("a", encoding="utf-8") as handle:
            handle.write('{"kind":"registry_entry"}\n')

        with self.assertRaises(IntegrityFailure):
            self.graph().artifact()

    def test_published_fixture_validates_against_topology_schema(self) -> None:
        fixture = json.loads(
            Path("tests/fixtures/graph/v0/harbor-chart.json").read_text(encoding="utf-8")
        )
        validate_json_schema(fixture, Path("schemas/v0/harbor-chart-topology.schema.json"))

    def test_canonical_run_edges_precede_and_suppress_matching_legacy_needs(self) -> None:
        from floati.jsonl import append_record
        source = "work-018f7e9b3c117abc8def0123456789ab"
        target = "work-018f7e9b3c127abc8def0123456789ab"
        for item_id, title, owner, needs in ((source, "first", "lane-a", []), (target, "second", "lane-b", [source])):
            append_record(self.root, "work/items.jsonl", {"schema_version": 0, "id": item_id, "tenant_id": "alpha", "timestamp": "2026-08-02T12:00:00.000Z", "kind": "work_item", "title": title, "owner": owner, "artifact_bindings": [], "needs": needs}, allowed_kinds={"work_item", "work_transition"})
        ledger = RunLedger(self.root)
        run_id = "run-" + uuid7_hex()
        ledger.append({"schema_version": 0, "id": "run-created-" + uuid7_hex(), "tenant_id": "alpha",
            "timestamp": "2026-08-02T12:00:00.000Z", "kind": "run_created", "run_id": run_id,
            "plan_digest": "a" * 64, "item_ids": [source, target],
            "dependency_edges": [{"source": source, "target": target, "requires": "verified"}]})
        self.assertEqual([{"kind": "run_dependency", "source": source, "target": target, "requires": "verified", "failure_policy": "fail_run"}], self.graph().artifact()["edges"])

    def test_legacy_bare_needs_render_accepted_without_canonical_run_edge(self) -> None:
        work = WorkLog(self.root)
        source = work.add("legacy source", "lane-a", [], now=NOW)
        target = work.add("legacy target", "lane-b", [], needs=[str(source["id"])], now=NOW)
        self.assertEqual([{"kind": "work_dependency", "source": str(source["id"]), "target": str(target["id"]), "requires": "accepted", "failure_policy": "fail_run"}], self.graph().artifact()["edges"])

    def test_traffic_projection_counts_only_directed_envelopes_and_denials(self) -> None:
        events = EventLog(self.root)
        for index in range(2):
            events.send(
                "lane-a",
                "lane-b",
                "floati",
                "a" * 40,
                "docs/evidence/example.md",
                f"message {index}",
                idempotency_key=f"traffic-{index}",
            )
        with self.assertRaises(ProtocolRefusal):
            events.send(
                "lane-a",
                "lane-b",
                "floati",
                "a" * 40,
                "docs/evidence/example.md",
                "refused",
                idempotency_key="traffic-denied",
                reply_to="msg-" + "0" * 32,
            )

        first = self.traffic().artifact()
        second = self.traffic().artifact()

        self.assertEqual(first, second)
        self.assertEqual(
            [
                {
                    "sender": "lane-a",
                    "recipient": "lane-b",
                    "envelope_count": 2,
                    "denial_count": 1,
                },
            ],
            first["pairs"],
        )
        self.assertEqual(
            {"schema_version", "tenant_id", "pairs", "unrecognized_kinds"},
            set(first),
        )
        self.assertNotIn("note", json.dumps(first))
        self.assertNotIn("body", json.dumps(first))

    def test_published_traffic_fixture_validates_against_its_own_schema(self) -> None:
        fixture = json.loads(
            Path("tests/fixtures/graph/v0/harbor-chart-traffic.json").read_text(
                encoding="utf-8"
            )
        )
        validate_json_schema(
            fixture, Path("schemas/v0/harbor-chart-traffic.schema.json")
        )

    def test_human_chart_composes_topology_and_traffic_with_typed_absence(self) -> None:
        self.assertIsNotNone(render_harbor_chart, "human Harbor Chart renderer must exist")
        events = EventLog(self.root)
        events.send(
            "lane-a",
            "lane-b",
            "floati",
            "b" * 40,
            "docs/evidence/example.md",
            "one crossing",
            idempotency_key="map-crossing",
        )
        topology = self.graph().artifact()
        traffic = self.traffic().artifact()

        first = render_harbor_chart(topology, traffic, color=False)
        second = render_harbor_chart(topology, traffic, color=False)
        unavailable = render_harbor_chart(topology, None, color=False)

        self.assertEqual(first, second)
        self.assertIn("FLOATI // HARBOR CHART", first)
        self.assertIn("┌", first)
        self.assertIn("lane-a", first)
        self.assertIn("lane-b", first)
        self.assertIn("lane-a ── 1 envelope · 0 denials ──▶ lane-b", first)
        self.assertNotIn("\x1b", first)
        self.assertIn("traffic: unavailable", unavailable)
    def test_cli_emits_human_chart_or_frozen_json_artifact(self) -> None:

        command = ["scripts/floati", "graph", "--root", str(self.home)]
        human = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(0, human.returncode, human.stderr)
        self.assertTrue(human.stdout.startswith("FLOATI // HARBOR CHART\n"))
        self.assertEqual("", human.stderr)

        result = subprocess.run(
            command + ["--json"], check=False, capture_output=True, text=True
        )
        self.assertEqual(0, result.returncode, result.stderr)
        artifact = json.loads(result.stdout)
        self.assertEqual("graph", artifact["command"])
        self.assertEqual("ok", artifact["status"])
        self.assertEqual("alpha", artifact["evidence"]["tenant_id"])


if __name__ == "__main__":
    unittest.main()

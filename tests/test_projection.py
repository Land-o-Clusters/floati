from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.planes import AuthorityGrantStore, LivenessPresenceStore
from floati.registry import REGISTRY_KINDS, Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from floati.workers import WorkerReceipts


NOW = datetime(2026, 7, 31, 12, 0, 0, tzinfo=timezone.utc)


class FleetProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "Codex")
        registry.register("bravo", "Codex")
        LivenessPresenceStore(self.root).observe(public_ids.worker('alpha'), 60, NOW)
        AuthorityGrantStore(self.root).claim("work-claims", public_ids.worker('alpha'), 60, 60, NOW)
        WorkLog(self.root).add("open item", public_ids.worker('alpha'), [], now=NOW)

        events = EventLog(self.root)
        self.message = events.send(
            public_ids.worker('alpha'), "bravo", "slipway", "a" * 40,
            "docs/evidence/checkpoint.md", "notice", idempotency_key="projection-mail",
        )
        events.present("bravo")
        SparseCursor(self.root).ack(
            "bravo", [self.message["id"]], acting_session_id="projection-session"
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            events.send(
                public_ids.worker('alpha'), "bravo", "slipway", "a" * 40,
                "docs/evidence/checkpoint.md", "denied",
                idempotency_key="projection-mail",
            )
        self.assertEqual("idempotency_conflict", caught.exception.code)

    def test_status_projection_summarizes_fleet_work_and_receipts(self) -> None:
        from floati.projection import FleetProjection

        snapshot = FleetProjection(self.root).snapshot(NOW + timedelta(seconds=10))

        self.assertNotIn("status_schema_version", snapshot)
        self.assertNotIn("kind", snapshot)
        self.assertNotIn("root", snapshot)
        self.assertNotIn("tenant_id", snapshot)
        self.assertNotIn("mode", snapshot)
        self.assertEqual([public_ids.worker('alpha'), "bravo"], [node["node_id"] for node in snapshot["nodes"]])
        self.assertEqual({"open": 1, "claimed": 0, "completed": 0}, snapshot["work_counts"])
        self.assertEqual({"delivery": 1, "ack": 1, "denial": 1}, snapshot["receipt_counts"])
        self.assertEqual(0, snapshot["stale_lease_count"])

    def test_versioned_status_artifact_adds_contract_fields_and_report_only_mode(self) -> None:
        from floati.projection import FleetProjection

        snapshot = FleetProjection(self.root).status_artifact(
            NOW + timedelta(seconds=10)
        )

        self.assertEqual(0, snapshot["status_schema_version"])
        self.assertEqual("fleet_status", snapshot["kind"])
        self.assertEqual(str(self.root.path), snapshot["root"])
        self.assertEqual(self.root.tenant_id, snapshot["tenant_id"])
        self.assertEqual("report_only", snapshot["mode"])

    def test_status_accepts_a_node_lease_written_to_the_registry_ledger(self) -> None:
        """Catches status declaring a narrower vocabulary than the registry writer."""
        from floati.projection import FleetProjection

        append_record(
            self.root,
            "registry/entries.jsonl",
            {
                "schema_version": 0,
                "id": "lease-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-07-31T12:00:00.000Z",
                "kind": "node_lease",
                "node_id": public_ids.worker('alpha'),
                "workspace": str(self.root.path / "nodes" / public_ids.worker('alpha')),
                "expires_at": "2026-07-31T12:01:00.000Z",
                "state": "active",
            },
            allowed_kinds=set(REGISTRY_KINDS),
        )

        snapshot = FleetProjection(self.root).status_artifact(
            NOW + timedelta(minutes=2)
        )

        self.assertEqual([public_ids.worker('alpha'), "bravo"], [row["node_id"] for row in snapshot["nodes"]])

    def test_receipts_keeps_delivery_ack_and_denial_histories_distinct(self) -> None:
        from floati.projection import FleetProjection

        history = FleetProjection(self.root).receipts("bravo")

        self.assertEqual(1, len(history["deliveries"]))
        self.assertEqual(1, len(history["acks"]))
        self.assertEqual(1, len(history["denials"]))
        self.assertEqual(self.message["id"], history["deliveries"][0]["item_ids"][0])
        self.assertEqual(self.message["id"], history["acks"][0]["item_ids"][0])
        self.assertEqual("idempotency_conflict", history["denials"][0]["reason_code"])

    def test_status_projects_worker_state_from_receipts_only(self) -> None:
        work = WorkLog(self.root)
        item = work.show()[0]
        work.claim(item["id"], public_ids.worker('alpha'), "work-claims", 1, now=NOW)
        WorkerReceipts(self.root).append(
            "worker-018f0f23abcd71238000000000000000",
            item["id"], public_ids.worker('alpha'), "codex", "claim", None, [], now=NOW,
        )

        from floati.projection import FleetProjection

        snapshot = FleetProjection(self.root).snapshot(NOW + timedelta(seconds=10))

        self.assertEqual("claim", snapshot["workers"][0]["state"])
        self.assertNotIn("pid", snapshot["workers"][0])

    def test_status_counts_confirmed_failed_unknown_and_compensation_states(self) -> None:
        from floati.projection import EffectStatusProjection
        from tests.test_effect_cli import mixed_effect_rows, write_effect_rows

        write_effect_rows(self.root, mixed_effect_rows())

        summary = EffectStatusProjection(self.root).summary()

        self.assertEqual(
            {"confirmed": 4, "failed": 1, "unknown": 1, "incomplete": 0},
            summary["counts"],
        )
        self.assertEqual(
            {"none": 4, "proposed": 1, "executed": 1},
            summary["compensation_counts"],
        )
        self.assertEqual(
            [
                {"state": "unknown", "count": 1},
                {"state": "incomplete", "count": 1},
                {"state": "failed", "count": 1},
                {"state": "confirmed", "count": 3},
            ],
            summary["attention"],
        )

    def test_status_artifact_matches_v1_effect_status_schema(self) -> None:
        from floati.projection import EffectStatusProjection
        from tests.schema_validation import validate_json_schema
        from tests.test_effect_cli import lifecycle_rows, write_effect_rows

        _, rows = lifecycle_rows("unknown")
        write_effect_rows(self.root, rows)
        artifact = {
            "schema_version": 1,
            "artifact_version": 0,
            "command": "effects",
            "status": "ok",
            "evidence": EffectStatusProjection(self.root).artifact(NOW),
        }

        validate_json_schema(
            artifact,
            Path("schemas/v1/effect-status-artifact.schema.json"),
        )


if __name__ == "__main__":
    unittest.main()

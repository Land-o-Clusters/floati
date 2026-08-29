from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from floati.doctor import Doctor
from floati.errors import IntegrityFailure
from floati.framing import encode_frame
from floati.graph import HarborGraph, HarborTraffic
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.supervisor import Supervisor


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)


class CrossVersionReaderLawTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "cross-version", create=True
        )
        registry = Registry(self.root)
        registry.register("alice", "architect")
        registry.register("bravo", "worker")

    def append_raw_event(self, record: object) -> None:
        path = self.root.resolve_relative("events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(encode_frame(record))

    def unknown(self, record_id: str) -> dict[str, object]:
        return {
            "schema_version": 2,
            "id": record_id,
            "tenant_id": self.root.tenant_id,
            "timestamp": "2026-08-28T16:00:00.000Z",
            "kind": "future_receipt",
            "payload": {"newer": True},
        }

    def test_whole_fleet_surfaces_skip_count_and_name_well_formed_unknown_kinds(self) -> None:
        first_id = "future-receipt-01a04900000070008000000000000000"
        self.append_raw_event(self.unknown(first_id))
        self.append_raw_event(
            self.unknown("future-receipt-01a04900000170008000000000000000")
        )
        expected = [
            {
                "kind": "future_receipt",
                "count": 2,
                "first_id": first_id,
            }
        ]

        failures: list[str] = []
        artifacts: dict[str, dict[str, object]] = {}
        calls = {
            "status": lambda: FleetProjection(self.root).status_artifact(NOW),
            "supervise": lambda: Supervisor(self.root).snapshot(NOW),
            "graph": lambda: HarborTraffic(self.root).artifact(),
        }
        for name, call in calls.items():
            try:
                artifacts[name] = call()
            except Exception as exc:  # RED records every dark whole-ledger surface.
                failures.append(f"{name}:{type(exc).__name__}:{exc}")
        doctor, _return_code = Doctor(Path.cwd(), self.root.path, ref="HEAD").artifact()
        artifacts["doctor"] = doctor

        self.assertEqual([], failures)
        for name, artifact in artifacts.items():
            with self.subTest(surface=name):
                self.assertEqual(expected, artifact["unrecognized_kinds"])

    def test_malformed_known_kind_stays_fatal_and_names_its_coordinate(self) -> None:
        record_id = "msg-01a04900000070008000000000000000"
        self.append_raw_event(
            {
                "schema_version": 0,
                "id": record_id,
                "tenant_id": self.root.tenant_id,
                "timestamp": "2026-08-28T16:00:00.000Z",
                "kind": "message_envelope",
            }
        )

        for name, call in {
            "status": lambda: FleetProjection(self.root).status_artifact(NOW),
            "supervise": lambda: Supervisor(self.root).snapshot(NOW),
            "graph": lambda: HarborTraffic(self.root).artifact(),
        }.items():
            with self.subTest(surface=name), self.assertRaises(IntegrityFailure) as raised:
                call()
            self.assertEqual("record_fields_invalid", raised.exception.code)
            self.assertIn("events.jsonl", raised.exception.detail)
            self.assertIn(record_id, raised.exception.detail)
            self.assertIn("message_envelope", raised.exception.detail)

        doctor, return_code = Doctor(Path.cwd(), self.root.path, ref="HEAD").artifact()
        self.assertEqual(33, return_code)
        finding = next(row for row in doctor["findings"] if row["severity"] == "error")
        self.assertIn("events.jsonl", finding["detail"])
        self.assertIn(record_id, finding["detail"])
        self.assertIn("message_envelope", finding["detail"])


if __name__ == "__main__":
    unittest.main()

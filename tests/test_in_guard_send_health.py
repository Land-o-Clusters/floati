from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.doctor import Doctor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.supervisor import Supervisor


NOW = datetime(2026, 8, 29, 20, 0, tzinfo=timezone.utc)
SHA = "a" * 40


class InGuardSendHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "fleet"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.registry = Registry(self.root)
        self.log = EventLog(self.root, self.registry)
        # Deliberately non-sorted insertion order; projections and refusal
        # remedies must expose the canonical sorted active roster.
        self.registry.register("sender-z", "worker")
        self.registry.register("sender-a", "worker")

    def _register_recipient(self) -> None:
        self.registry.register("recipient-a", "worker")

    def _send_at(self, timestamp: str, note: str, *, key: str) -> dict:
        with patch("floati.events.utc_now", return_value=timestamp):
            return self.log.send(
                "sender-a", "recipient-a", "repo", SHA, "doc", note,
                idempotency_key=key,
            )

    def test_send_to_unregistered_recipient_refuses_with_roster(self) -> None:
        """Catches send accepting an unregistered recipient or omitting the active roster remedy."""
        before = self.log.path.read_bytes() if self.log.path.exists() else b""
        with self.assertRaisesRegex(
            ProtocolRefusal, "registered nodes: sender-a, sender-z"
        ) as caught:
            self.log.send("sender-a", "missing", "repo", SHA, "doc", "note")
        self.assertEqual("recipient_unregistered", caught.exception.code)
        after = self.log.path.read_bytes() if self.log.path.exists() else b""
        self.assertEqual(before, after)

    def test_send_without_wake_arrangement_appends_and_warns(self) -> None:
        """Catches send refusing a registered recipient without a lease instead of returning readiness testimony."""
        self._register_recipient()
        result = self.log.send(
            "sender-a", "recipient-a", "repo", SHA, "doc", "note",
            idempotency_key="no-wake",
        )
        self.assertIn("recipient_readiness", result)
        readiness = result["recipient_readiness"]
        self.assertEqual("recipient_not_listening", readiness["state"])
        self.assertEqual("no_active_lease", readiness["reason"])
        self.assertRegex(
            readiness["observed_at"],
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$",
        )
        self.assertEqual(1, len(self.log.records()))
        self.assertNotIn("offline", str(readiness).lower())
        self.assertNotIn("down", str(readiness).lower())

    def test_send_receipt_get_keeps_in_process_envelope_lookup_compatible(self) -> None:
        """Catches a wrapped receipt whose mapping get cannot reach durable mail fields."""
        self._register_recipient()
        result = self.log.send(
            "sender-a", "recipient-a", "repo", SHA, "doc", "note",
            idempotency_key="get-compatibility",
        )
        self.assertEqual(result["message"]["id"], result.get("id"))

    def test_send_readiness_stamp_matches_explicit_lease_observation_and_replay(self) -> None:
        """Catches a readiness fact stamped from envelope time rather than the lease clock."""
        self._register_recipient()
        observed = NOW - timedelta(minutes=5)
        expected = "2026-08-29T19:55:00.000Z"
        with patch("floati.events.utc_now", return_value="2026-08-29T21:00:00.000Z"):
            first = self.log.send(
                "sender-a", "recipient-a", "repo", SHA, "doc", "note",
                idempotency_key="explicit-observation", now=observed,
            )
        with patch("floati.events.utc_now", return_value="2026-08-29T22:00:00.000Z"):
            replay = self.log.send(
                "sender-a", "recipient-a", "repo", SHA, "doc", "note",
                idempotency_key="explicit-observation", now=NOW,
            )

        self.assertEqual(expected, first["recipient_readiness"]["observed_at"])
        self.assertEqual(first, replay)

    def _prepare_pending_mail(self) -> None:
        self._register_recipient()
        self._send_at("2026-08-29T19:50:00.000Z", "old", key="old")
        self._send_at("2026-08-29T19:55:00.000Z", "new", key="new")

    def test_pending_and_drained_mail_project_oldest_unread_in_status(self) -> None:
        """Catches status dropping a stamped oldest-unread fact or counting drained mail."""
        self._prepare_pending_mail()
        status = FleetProjection(self.root).status_artifact(NOW)
        node_row = next(row for row in status["nodes"] if row["node_id"] == "recipient-a")
        expected = {
            "node": "recipient-a",
            "age_minutes": 10,
            "observed_at": "2026-08-29T20:00:00Z",
        }
        self.assertEqual(expected, node_row["oldest_unread"])

        with patch("floati.events.utc_now", return_value="2026-08-29T20:00:00.000Z"):
            self.log.present("recipient-a", now=NOW)
        status_after = FleetProjection(self.root).status_artifact(NOW)
        node_after = next(row for row in status_after["nodes"] if row["node_id"] == "recipient-a")
        self.assertIsNone(node_after["oldest_unread"])

    def test_supervisor_projects_and_drains_oldest_unread(self) -> None:
        """Catches unread health being owned only by the status projection."""
        self._prepare_pending_mail()
        before = Supervisor(self.root).snapshot(NOW)
        row = next(row for row in before["nodes"] if row["node_id"] == "recipient-a")
        self.assertEqual(
            {
                "node": "recipient-a",
                "age_minutes": 10,
                "observed_at": "2026-08-29T20:00:00Z",
            },
            row["oldest_unread"],
        )

        with patch("floati.events.utc_now", return_value="2026-08-29T20:00:00.000Z"):
            self.log.present("recipient-a", now=NOW)
        after = Supervisor(self.root).snapshot(NOW)
        row_after = next(row for row in after["nodes"] if row["node_id"] == "recipient-a")
        self.assertIsNone(row_after["oldest_unread"])

    def test_v1_health_schema_shapes_are_exact_and_nullable(self) -> None:
        """Catches v1 status and doctor schemas silently leaving health facts untyped."""
        schema_root = Path(__file__).resolve().parents[1] / "schemas" / "v1"
        with (schema_root / "fleet-status-artifact.schema.json").open() as handle:
            status = json.load(handle)
        with (schema_root / "doctor-artifact.schema.json").open() as handle:
            doctor = json.load(handle)

        expected = {
            "oneOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["node", "age_minutes", "observed_at"],
                    "properties": {
                        "node": {"type": "string", "minLength": 1, "maxLength": 64},
                        "age_minutes": {"type": "integer", "minimum": 0},
                        "observed_at": {"type": "string", "format": "date-time"},
                    },
                },
            ]
        }
        self.assertIn("oldest_unread", status["$defs"]["node"]["required"])
        self.assertEqual(expected, status["$defs"]["oldest_unread"])
        self.assertEqual(
            {"$ref": "fleet-status-artifact.schema.json#/$defs/oldest_unread"},
            doctor["$defs"]["finding"]["properties"]["oldest_unread"],
        )

    def test_pending_and_drained_mail_project_oldest_unread_in_doctor(self) -> None:
        """Catches doctor dropping a stamped oldest-unread fact or counting drained mail."""
        self._prepare_pending_mail()
        expected = {
            "node": "recipient-a",
            "age_minutes": 10,
            "observed_at": "2026-08-29T20:00:00Z",
        }

        with patch("floati.doctor._utc_now", return_value=NOW):
            doctor, _return_code = Doctor(Path.cwd(), self.home, ref="HEAD").artifact()
        finding = next(
            finding for finding in doctor["findings"]
            if finding.get("code") == "delivery_health" and finding.get("subject") == "recipient-a"
        )
        self.assertEqual(expected, finding["oldest_unread"])

        with patch("floati.events.utc_now", return_value="2026-08-29T20:00:00.000Z"):
            self.log.present("recipient-a", now=NOW)
        with patch("floati.doctor._utc_now", return_value=NOW):
            doctor_after, _return_code = Doctor(Path.cwd(), self.home, ref="HEAD").artifact()
        finding_after = next(
            finding for finding in doctor_after["findings"]
            if finding.get("code") == "delivery_health" and finding.get("subject") == "recipient-a"
        )
        self.assertIsNone(finding_after["oldest_unread"])


if __name__ == "__main__":
    unittest.main()

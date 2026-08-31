from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati import fixture_ids as public_ids
from floati.codex_wait_contract import (
    CodexWaitConsentLedger,
    CodexWaitSessionLedger,
    resolve_participant,
)
from floati.doctor import Doctor
from floati.events import EventLog
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class WakeHealthSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = FloatiRoot.open_direct_home(
            self.base / "wake-health-surface", create=True
        )
        registry = Registry(self.root)
        registry.register("architect", "architect")
        registry.register(public_ids.builder("health"), "Codex")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        mapping = self.root.path / "codex-wait" / "workspaces.v0.json"
        mapping.parent.mkdir()
        mapping.write_text(
            json.dumps(
                {
                    "schema_version": 0,
                    "tenant_id": self.root.tenant_id,
                    "mappings": [
                        {"workspace": str(self.workspace), "node_id": public_ids.builder("health")}
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        participant = resolve_participant(self.root.path, self.workspace)
        assert participant is not None
        consent = CodexWaitConsentLedger(self.root).arm(
            participant.binding,
            hook_timeout_seconds=10,
            wait_deadline_seconds=2,
            idempotency_key="health-consent",
        )
        CodexWaitSessionLedger(self.root).arm(
            participant.binding,
            consent,
            "seat-claim",
            idempotency_key="health-claim",
        )
        self.observation = datetime(2026, 8, 30, 12, 10, tzinfo=timezone.utc)
        with mock.patch("floati.events.utc_now", return_value="2026-08-30T12:00:00.000Z"):
            self.message = EventLog(self.root).send(
                "architect", public_ids.builder("health"), "floati", "a" * 40,
                "docs/evidence/wake-health.md", "wake health",
                idempotency_key="wake-health-message",
            )

    def test_fact_names_stale_claim_unread_mail_and_absolute_entrypoint(self) -> None:
        from floati.wake_health import WakeHealthProjection

        fact = WakeHealthProjection(self.root).fact(public_ids.builder("health"), self.observation)
        self.assertEqual("stale_claim_with_unread_mail", fact["state"])
        self.assertEqual("seat-claim", fact["claim_session"])
        self.assertIsNone(fact["last_seen_session"])
        self.assertEqual(10, fact["oldest_unread"]["age_minutes"])
        self.assertTrue(Path(str(fact["documented_entrypoint"])).is_absolute())
        self.assertTrue(fact["documented_entrypoint_resolves"])
        self.assertEqual("2026-08-30T12:10:00Z", fact["observed_at"])
        self.assertIn(str(fact["documented_entrypoint"]), str(fact["remedy"]))
        validate_json_schema(fact, Path("schemas/v1/wake-health-fact.schema.json"))

    def test_status_and_doctor_share_the_same_wake_health_fact(self) -> None:
        status = FleetProjection(self.root).status_artifact(self.observation)
        node = next(row for row in status["nodes"] if row["node_id"] == public_ids.builder("health"))
        fact = node["wake_health"]
        self.assertEqual("stale_claim_with_unread_mail", fact["state"])

        with mock.patch("floati.doctor._utc_now", return_value=self.observation):
            artifact, _status = Doctor(
                REPOSITORY_ROOT, self.root.path, ref="HEAD"
            ).artifact()
        finding = next(
            row for row in artifact["findings"]
            if row.get("code") == "wake_health" and row.get("subject") == public_ids.builder("health")
        )
        self.assertEqual(fact, finding["wake_health"])
        self.assertEqual("error", finding["severity"])


if __name__ == "__main__":
    unittest.main()

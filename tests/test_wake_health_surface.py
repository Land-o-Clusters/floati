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
from floati.errors import ProtocolRefusal
from floati.projection import FleetProjection
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.schema_validation import validate_json_schema
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class WakeHealthSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
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
        import shlex
        notice, command_text = str(fact['remedy']).split(' to: ', 1)
        self.assertIn('Acting session is not known', notice)
        self.assertIn('--session', notice)
        self.assertIn('takeover_report', notice)
        command = shlex.split(command_text)
        self.assertEqual(["seat", "board"], command[1:3])
        self.assertEqual(str(self.workspace), command[command.index("--workspace") + 1])
        self.assertEqual(str(self.root.path), command[command.index("--root") + 1])
        self.assertEqual(public_ids.builder("health"), command[command.index("--as") + 1])
        self.assertIn("--take-over", command)
        self.assertNotIn('--session', command)
        self.assertNotIn(fact['claim_session'], command)

        validate_json_schema(fact, Path("schemas/v1/wake-health-fact.schema.json"))

    def test_superseded_exit_projects_a_schema_valid_health_fact(self) -> None:
        from floati.wake_exit import WakeExitLedger
        from floati.wake_health import WakeHealthProjection
        import hashlib

        node = public_ids.builder("health")
        WakeExitLedger(self.root).record(
            node_id=node,
            session_digest=hashlib.sha256(b"seat-former").hexdigest(),
            reason_code="superseded_session", waited_seconds=1,
            idempotency_key="health-superseded-exit",
        )
        fact = WakeHealthProjection(self.root).fact(node, self.observation)
        self.assertEqual("superseded_session", fact["waiter_exit_reason"])
        validate_json_schema(fact, Path("schemas/v1/wake-health-fact.schema.json"))

    def test_health_schema_change_only_widens_exit_reason(self) -> None:
        old = json.loads((REPOSITORY_ROOT / "tests/fixtures/f2-wake-health-v1-before.schema.json").read_text())
        new = json.loads((REPOSITORY_ROOT / "tests/fixtures/f2-wake-health-v1-recent-before.schema.json").read_text())
        old_enum = old["properties"]["waiter_exit_reason"]["enum"]
        new_enum = new["properties"]["waiter_exit_reason"]["enum"]
        self.assertEqual([*old_enum, "superseded_session"], new_enum)
        new["properties"]["waiter_exit_reason"]["enum"] = old_enum
        self.assertEqual(old, new)

    def test_queued_attempt_does_not_prove_claimant_observed_mail(self) -> None:
        from floati.wake_hold import WakeHoldController, WakeAttemptLedger
        from floati.wake_health import WakeHealthProjection
        node = public_ids.builder("health")
        decision = WakeHoldController(self.root).evaluate(node, idempotency_key="health-queue")
        WakeAttemptLedger(self.root).record(
            recipient=node, acting_session_id="seat-claim", item_ids=[self.message["id"]],
            decision_receipt_id=decision["receipt"]["id"], message_worker_session_id=None,
            idempotency_key="health-queue-attempt", outcome="queued",
        )
        EventLog(self.root).send(
            "architect", node, "floati", "a" * 40, "docs/evidence/wake-health.md",
            "later mail", idempotency_key="health-later-mail",
        )
        fact = WakeHealthProjection(self.root).fact(node, self.observation)
        self.assertIsNone(fact["last_seen_session"])
        self.assertEqual("stale_claim_with_unread_mail", fact["state"])

    def test_refused_only_claimant_with_unread_mail_stays_stale_and_doctor_errors(self) -> None:
        """Catches a refused wake falsely upgrading the current claim's health."""
        from floati.wake_hold import WakeHoldController, WakeAttemptLedger
        from floati.wake_health import WakeHealthProjection

        node = public_ids.builder("health")
        decision = WakeHoldController(self.root).evaluate(node, idempotency_key="health-refused")
        with self.assertRaises(ProtocolRefusal):
            WakeAttemptLedger(self.root).record(
                recipient=node, acting_session_id="seat-claim", item_ids=[self.message["id"]],
                decision_receipt_id=decision["receipt"]["id"], message_worker_session_id=None,
                idempotency_key="health-refused-attempt", outcome="refused", reason_code="wake_prompt_failed",
            )
        EventLog(self.root).send(
            "architect", node, "floati", "a" * 40, "docs/evidence/wake-health.md",
            "later mail", idempotency_key="health-refused-later",
        )
        fact = WakeHealthProjection(self.root).fact(node, self.observation)
        self.assertIsNone(fact["last_seen_session"])
        self.assertEqual("stale_claim_with_unread_mail", fact["state"])
        with mock.patch("floati.doctor._utc_now", return_value=self.observation):
            artifact, _status = Doctor(REPOSITORY_ROOT, self.root.path, ref="HEAD").artifact()
        finding = next(row for row in artifact["findings"]
            if row.get("code") == "wake_health" and row.get("subject") == node)
        self.assertEqual("error", finding["severity"])

    def test_doctor_uses_one_clock_for_all_recent_session_windows(self) -> None:
        """Catches doctor sampling a different hour boundary for each node."""
        Registry(self.root).register(public_ids.builder("second-health"), "Cursor")
        from datetime import timedelta

        ticks = iter(self.observation + timedelta(seconds=value) for value in range(100))
        with mock.patch("floati.doctor._utc_now", side_effect=lambda: next(ticks)):
            artifact, _status = Doctor(REPOSITORY_ROOT, self.root.path, ref="HEAD").artifact()
        facts = [row["wake_health"] for row in artifact["findings"] if row.get("code") == "wake_health"]
        self.assertEqual(3, len(facts))
        self.assertTrue(all("recent_sessions" in fact for fact in facts), "doctor omits recent session windows")
        self.assertEqual(1, len({fact["recent_sessions"]["window_end"] for fact in facts}))

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

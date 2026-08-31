"""RED-first tests for H2 (delivery-health scoreboard) + H3 (doctor --probe).

Intake: docs/design/floati-hardening-intake-2026-08-22.md (H2/H3, launch
cut, owner-ratified @09283dc7). Laws under test:
- H2: per registered node — undelivered count, oldest-age, time since last
  drain; aged pending = RED with the intake's plain sentence; silence is
  not health (counters stated even when green); inactive nodes unscored.
- H3: loopback envelope per node, drain verified inside a budget,
  per-node PASS/DEAF; DEAF degrades doctor; probe appends only its own
  loopback envelope.
"""

from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.delivery_health import (
    DELIVERY_STALL_RED_MINUTES,
    DeliveryHealthAnalyzer,
)
from floati.doctor import Doctor
from floati.doctor_probe import DoctorProbe
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot

NOW = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_before_now: float) -> str:
    moment = NOW - timedelta(minutes=minutes_before_now)
    return (moment.isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


class DeliveryHealthTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "root"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.registry = Registry(self.root)
        self.log = EventLog(self.root)

    def _send_at(self, minutes_before_now: float, sender: str, recipient: str,
                 note: str = "hello") -> str:
        with patch("floati.events.utc_now", return_value=_ts(minutes_before_now)):
            envelope = self.log.send(
                sender, recipient, "repo", "0" * 64, "docs/x.md", note)
        return envelope["id"]

    def _drain_at(self, minutes_before_now: float, recipient: str) -> dict:
        with patch("floati.events.utc_now", return_value=_ts(minutes_before_now)):
            _, receipt = self.log.present(recipient)
        assert receipt is not None
        return receipt


class DeliveryHealthScoreboardTests(DeliveryHealthTestBase):
    def test_doctor_artifact_supplies_a_datetime_to_live_delivery_health(self):
        """Catches the Doctor boundary passing its RFC3339 string clock to datetime arithmetic."""
        self.registry.register(public_ids.worker('alpha'), "opencode")
        self.registry.register("bob", "opencode")
        self._send_at(3, public_ids.worker('alpha'), "bob")
        self._drain_at(1, "bob")

        artifact, _status = Doctor(Path.cwd(), self.home, ref="HEAD").artifact()

        self.assertIn("findings", artifact)

    def test_aged_pending_is_red_with_intake_sentence(self):
        self.registry.register(public_ids.worker('alpha'), "opencode")
        self.registry.register("bob", "opencode")
        self._send_at(61, public_ids.worker('alpha'), "bob", "old")
        self._send_at(42, public_ids.worker('alpha'), "bob", "younger")
        # bob never drained.
        report = DeliveryHealthAnalyzer.analyze(
            events=self.log.records(),
            root=self.root,
            nodes=[public_ids.worker('alpha'), "bob"],
            now=NOW,
        )
        bob = report.by_node["bob"]
        self.assertTrue(bob.red)
        self.assertEqual(bob.undelivered_count, 2)
        self.assertEqual(bob.oldest_pending_minutes, 61)
        self.assertIsNone(bob.last_drain_minutes_ago)
        self.assertEqual(
            bob.sentence,
            "bob: 2 undelivered, oldest 61m, no drain on record, "
            "RED threshold 15m RULED",
        )
        finding = report.findings_by_node["bob"]
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(finding["code"], "delivery_health")

    def test_fresh_pending_with_recent_drain_is_stated_not_red(self):
        self.registry.register(public_ids.worker('alpha'), "opencode")
        self.registry.register("bob", "opencode")
        self._send_at(3, public_ids.worker('alpha'), "bob")
        self._drain_at(1, "bob")  # drains everything pending at t-1m
        self._send_at(0.5, public_ids.worker('alpha'), "bob")  # arrives after the drain
        report = DeliveryHealthAnalyzer.analyze(
            events=self.log.records(), root=self.root,
            nodes=[public_ids.worker('alpha'), "bob"], now=NOW,
        )
        bob = report.by_node["bob"]
        self.assertFalse(bob.red)
        self.assertEqual(bob.undelivered_count, 1)
        self.assertEqual(bob.last_drain_minutes_ago, 1)
        self.assertEqual(f"{bob.undelivered_count}", "1")

    def test_zero_pending_states_silence_honestly(self):
        self.registry.register(public_ids.worker('alpha'), "opencode")
        report = DeliveryHealthAnalyzer.analyze(
            events=self.log.records(), root=self.root,
            nodes=[public_ids.worker('alpha')], now=NOW,
        )
        alice = report.by_node[public_ids.worker('alpha')]
        self.assertFalse(alice.red)
        self.assertEqual(alice.undelivered_count, 0)
        self.assertIn("0 undelivered", alice.sentence)

    def test_drained_mail_does_not_count_as_undelivered(self):
        self.registry.register(public_ids.worker('alpha'), "opencode")
        self.registry.register("bob", "opencode")
        self._send_at(50, public_ids.worker('alpha'), "bob")
        self._drain_at(10, "bob")  # bob drained the old mail; nothing new
        report = DeliveryHealthAnalyzer.analyze(
            events=self.log.records(), root=self.root,
            nodes=[public_ids.worker('alpha'), "bob"], now=NOW,
        )
        bob = report.by_node["bob"]
        self.assertFalse(bob.red)
        self.assertEqual(bob.undelivered_count, 0)
        self.assertEqual(bob.last_drain_minutes_ago, 10)

    def test_inactive_nodes_are_never_scored(self):
        self.registry.register(public_ids.worker('alpha'), "opencode")
        self.registry.register("ghost", "opencode")
        self.registry.retire("ghost")
        report = DeliveryHealthAnalyzer.analyze(
            events=self.log.records(), root=self.root,
            nodes=[public_ids.worker('alpha')], now=NOW,
        )
        self.assertNotIn("ghost", report.by_node)

    def test_threshold_constant_precedes_incident_scale(self):
        # Incident: "idle"/"deaf" indistinguishable for 40+ minutes. The RED
        # line must trip well before that scale.
        self.assertLess(DELIVERY_STALL_RED_MINUTES, 40)


class DoctorProbeTests(DeliveryHealthTestBase):
    def setUp(self):
        super().setUp()
        self.registry.register(public_ids.worker('alpha'), "opencode")
        self.registry.register("deaf", "opencode")

    def test_pass_when_drain_lands_within_budget(self):
        probe = DoctorProbe(
            self.root, budget_seconds=5,
            sleeper=self._draining_sleeper(public_ids.worker('alpha')),
        )
        result = probe.run([public_ids.worker('alpha')])
        self.assertEqual(result.by_node[public_ids.worker('alpha')].verdict, "PASS")
        self.assertEqual(result.rc, 0)
        # The probe appended exactly one loopback envelope to worker-alpha.
        envelopes = [r for r in self.log.records()
                     if r["kind"] == "message_envelope"
                     and r["recipient"] == public_ids.worker('alpha')]
        self.assertEqual(len(envelopes), 1)
        self.assertIn("doctor-probe", envelopes[0]["note"])

    def test_deaf_when_budget_expires_without_drain(self):
        probe = DoctorProbe(
            self.root, budget_seconds=3,
            sleeper=lambda _: None,
        )
        result = probe.run(["deaf"])
        self.assertEqual(result.by_node["deaf"].verdict, "DEAF")
        self.assertEqual(result.rc, 35)
        finding = result.findings_by_node["deaf"]
        self.assertEqual(finding["severity"], "error")
        self.assertIn("DEAF", finding["detail"])

    def test_mixed_fleet_reports_each_node_and_degrades_once(self):
        probe = DoctorProbe(
            self.root, budget_seconds=4,
            sleeper=self._draining_sleeper(public_ids.worker('alpha')),
        )
        result = probe.run([public_ids.worker('alpha'), "deaf"])
        self.assertEqual(result.by_node[public_ids.worker('alpha')].verdict, "PASS")
        self.assertEqual(result.by_node["deaf"].verdict, "DEAF")
        self.assertEqual(result.rc, 35)

    def test_unknown_node_refuses_typed(self):
        probe = DoctorProbe(
            self.root, budget_seconds=1, sleeper=lambda _: None,
        )
        with self.assertRaises(Exception):
            probe.run(["nobody-here"])

    # -- helpers ------------------------------------------------------------

    def _draining_sleeper(self, node: str):
        """Injected sleeper whose FIRST tick performs the node's own drain,
        simulating a live wake layer observing the probe."""

        def _tick(_seconds: float) -> None:
            self.log.present(node)

        return _tick


if __name__ == "__main__":
    unittest.main()

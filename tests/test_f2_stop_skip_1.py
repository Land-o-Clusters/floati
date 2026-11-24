"""F2-STOP-SKIP-1 RED: doctor says nothing about a seat a skipped Stop left behind.

Amended done-when (OPUS-RULED 2026-10-03; the architect's 2026-10-11
envelope restates it verbatim): waiting mail older than a stated threshold
with no stop hold started since it -> a typed UNKNOWN doctor finding
(node, oldest mail age, last hold start, the honest sentence that a long
turn and a skipped Stop look alike, remedy: look at the seat); a fresh
hold or no waiting mail never raises it.

At main, doctor reports delivery health for exactly this fixture and says
nothing about the skip: the first test reds on that silence. The controls
pin the guards the amendment names. Blocker verified on main 5445ad54:
`floati/stop_hold.py` StopHoldLedger (F2-READINESS-1) is present.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati import fixture_ids as public_ids
from floati.doctor import Doctor
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.stop_hold import KIND, StopHoldLedger

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_before_now: float) -> str:
    moment = NOW - timedelta(minutes=minutes_before_now)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StopSkipDoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "root"
        self.root = FloatiRoot.open_direct_home(self.home, create=True)
        self.registry = Registry(self.root)
        self.log = EventLog(self.root)
        self.alpha = public_ids.worker("alpha")
        self.registry.register(self.alpha, "opencode")
        self.registry.register("bob", "opencode")

    def _send_at(self, minutes_before_now: float, note: str = "hello") -> str:
        with patch("floati.events.utc_now", return_value=_ts(minutes_before_now)):
            envelope = self.log.send(self.alpha, "bob", "repo", "0" * 64, "docs/x.md", note)
        return envelope["id"]

    def _seed_hold_start(self, minutes_before_now: float) -> str:
        """One real-shape start row: the hook armed, once, long ago."""

        started_at = _ts(minutes_before_now)
        identity = "stop-hold-" + uuid7_hex()
        row = {
            "schema_version": 1,
            "id": identity,
            "tenant_id": self.root.tenant_id,
            "timestamp": started_at,
            "kind": KIND,
            "invocation_id": identity,
            "node_id": "bob",
            "acting_session_id": "sess-" + uuid7_hex(),
            "consent_receipt_id": "codex-wait-consent-" + uuid7_hex(),
            "workspace": str(self.home),
            "harness": "cursor",
            "started_at": started_at,
            "deadline_seconds": 1500.0,
            "phase": "start",
        }
        relative = StopHoldLedger.relative("bob")
        path = self.root.tenant_home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        # The reader must accept exactly what the finding will rely on.
        rows = StopHoldLedger(self.root).read("bob")
        assert any(candidate["id"] == identity for candidate in rows)
        return started_at

    def findings(self):
        # Doctor reads the real wall clock; pin it to the frame the
        # backdated envelopes and hold rows are stamped in.
        with patch("floati.doctor._utc_now", return_value=NOW):
            artifact, _status = Doctor(Path.cwd(), self.home, ref="HEAD").artifact()
        return artifact.get("findings", [])

    def stop_skip_findings(self):
        return [
            finding
            for finding in self.findings()
            if finding.get("code") == "stop_skip_unknown"
        ]

    def test_aged_waiting_mail_with_no_hold_since_it_is_a_typed_unknown_finding(self):
        """THE RED: a stale hold, waiting mail older than the threshold, and doctor is silent."""

        stale_hold_started_at = self._seed_hold_start(120)
        self._send_at(61, "the seat never drained this")
        found = self.stop_skip_findings()
        self.assertEqual(1, len(found), "doctor must name the skipped-Stop UNKNOWN")
        finding = found[0]
        self.assertEqual("warning", finding["severity"])
        self.assertEqual("bob", finding["stop_skip"]["node"])
        self.assertGreaterEqual(
            finding["stop_skip"]["oldest_waiting_mail_age_minutes"], 61)
        self.assertEqual(
            stale_hold_started_at, finding["stop_skip"]["last_hold_started_at"])
        self.assertIn("a long turn and a skipped Stop look alike", finding["detail"])
        self.assertIn("stated not measured", finding["detail"])
        self.assertEqual("look at the seat", finding["remediation"])

    def test_aged_waiting_mail_with_no_hold_on_record_names_the_absence(self):
        self._send_at(61, "never held, never drained")
        found = self.stop_skip_findings()
        self.assertEqual(1, len(found))
        self.assertIsNone(found[0]["stop_skip"]["last_hold_started_at"])
        self.assertIn("none on record", found[0]["detail"])

    def test_a_fresh_hold_started_since_the_mail_never_raises_it(self):
        self._send_at(61, "old, but the hook has held since")
        self._seed_hold_start(1)
        self.assertEqual([], self.stop_skip_findings())

    def test_no_waiting_mail_never_raises_it(self):
        self._seed_hold_start(120)
        self.assertEqual([], self.stop_skip_findings())

    def test_waiting_mail_younger_than_the_stated_threshold_never_raises_it(self):
        self._send_at(3, "too young to call unknown")
        self.assertEqual([], self.stop_skip_findings())


if __name__ == "__main__":
    unittest.main()

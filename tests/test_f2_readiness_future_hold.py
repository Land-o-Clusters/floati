"""F2 gate P4: native holds cannot witness readiness before their start."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.mail_health import RecipientReadiness
from floati.stop_hold import StopHoldLedger
from tests import test_f2_readiness_hold as native_holds


HARNESSES = ("Codex", "Cursor", "ZCode")


class NativeFutureHoldReadinessTests(unittest.TestCase):
    def _observe_during_real_hold(self, harness: str, offset: timedelta) -> dict:
        fixture = native_holds.NativeStopHoldReadinessTests("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.fixture(harness)
        observed = {}

        def while_holding() -> None:
            rows = StopHoldLedger(fixture.root).read(fixture.node)
            self.assertEqual(1, len(rows), "observe the open native invocation, before its exit")
            start = rows[0]
            self.assertEqual("start", start["phase"])
            self.assertEqual(fixture.node, start["node_id"])
            self.assertEqual(fixture.session, start["acting_session_id"])
            self.assertEqual(fixture.consent["id"], start["consent_receipt_id"])
            self.assertEqual(harness.casefold(), start["harness"])
            started_at = datetime.fromisoformat(start["started_at"].replace("Z", "+00:00"))
            observed_at = (started_at + offset).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            paths = (
                fixture.root.tenant_home / "registry/entries.jsonl",
                fixture.root.tenant_home / "codex-wait/workspaces.v0.json",
                fixture.root.tenant_home / StopHoldLedger.relative(fixture.node),
                fixture.root.tenant_home / CodexWaitConsentLedger._relative(fixture.node),
                fixture.root.tenant_home / CodexWaitSessionLedger._relative(fixture.node),
            )
            before = {path: path.read_bytes() if path.exists() else None for path in paths}
            readiness = RecipientReadiness.observe(
                fixture.root, fixture.node, observed_at=observed_at
            ).artifact()
            self.assertEqual(before, {
                path: path.read_bytes() if path.exists() else None for path in paths
            }, "the temporal observation must not rewrite native testimony")
            self.assertEqual([start], StopHoldLedger(fixture.root).read(fixture.node))
            self.assertEqual("", fixture.stdout.getvalue(), "hold observation precedes native output")
            self.assertEqual(observed_at, readiness["observed_at"])
            self.assertEqual(["lease", "stop_hook", "seat_board"], readiness["checked_mechanisms"])
            self.assertEqual("absent", readiness["mechanism_evidence"][0]["state"],
                             "an administrative lease must not mask the hold result")
            self.assertNotEqual("arranged", readiness["mechanism_evidence"][2]["state"],
                                "no completed board may mask the hold result")
            observed.update(readiness=readiness, start=start, observed_at=observed_at)

        fixture.run_hold(harness, while_holding)
        fixture.assert_closed_hold()
        self.assertTrue(observed, "the real waiter must execute the observation callback")
        observed["hold_path"] = str(fixture.root.tenant_home / StopHoldLedger.relative(fixture.node))
        return observed

    def _assert_current_hold_ready(self, observed: dict) -> None:
        readiness, start = observed["readiness"], observed["start"]
        self.assertEqual("recipient_wake_arranged", readiness["state"], readiness)
        self.assertEqual("stop_hook", readiness["reason"], readiness)
        fact = readiness["mechanism_evidence"][1]
        self.assertEqual("arranged", fact["state"], fact)
        self.assertEqual("active_stop_hold", fact["reason"], fact)
        self.assertIn({"path": observed["hold_path"], "record_id": start["id"]}, fact["sources"])

    def test_same_start_timestamp_qualifies_each_real_native_hold(self) -> None:
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                observed = self._observe_during_real_hold(harness, timedelta(0))
                self.assertEqual(observed["start"]["started_at"], observed["observed_at"])
                self._assert_current_hold_ready(observed)

    def test_past_start_within_its_budget_qualifies_each_real_native_hold(self) -> None:
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                observed = self._observe_during_real_hold(harness, timedelta(seconds=1))
                self.assertLess(observed["start"]["started_at"], observed["observed_at"])
                self._assert_current_hold_ready(observed)

    def test_future_start_one_millisecond_ahead_cannot_qualify_any_native_hold(self) -> None:
        for harness in HARNESSES:
            with self.subTest(harness=harness):
                observed = self._observe_during_real_hold(harness, timedelta(milliseconds=-1))
                self.assertGreater(observed["start"]["started_at"], observed["observed_at"])
                readiness = observed["readiness"]
                self.assertEqual("unknown", readiness["state"], readiness)
                fact = readiness["mechanism_evidence"][1]
                self.assertEqual("unknown", fact["state"], fact)
                self.assertNotEqual("active_stop_hold", fact["reason"], fact)
                self.assertNotIn({"path": observed["hold_path"],
                                  "record_id": observed["start"]["id"]}, fact["sources"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import runpy
import time
import unittest
from unittest import mock

from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import consent_ledger_relative
from tests import test_wake_waiter_continuity as fixtures
from tests.waiter_timing import scripted_readiness
from tests.private_artifacts import require_private_artifact


class IntegrityCadenceTests(unittest.TestCase):
    fixture = fixtures.WakeWaiterContinuityTests.fixture
    rows = staticmethod(fixtures.WakeWaiterContinuityTests.rows)

    def setUp(self):
        fixtures.WakeWaiterContinuityTests.setUp(self)
        self.elapsed = 0.0
        self.epoch = float(int(time.time()))
        # A short fixture deployment cadence; production derives its fallback
        # from the shared arm default. Clocks are the only simulated service.
        default = mock.patch("floati.codex_wait.DEFAULT_WAIT_DEADLINE_SECONDS", 2, create=True)
        default.start()
        self.addCleanup(default.stop)
        stamp = mock.patch("floati.wake_exit.utc_now", side_effect=self.timestamp)
        stamp.start()
        self.addCleanup(stamp.stop)

    def timestamp(self):
        return datetime.fromtimestamp(self.epoch + self.elapsed, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def sleep(self, seconds):
        self.elapsed += seconds

    def run_fault(self, root, workspace):
        output = io.StringIO()
        run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": "seat-cadence"},
            stdout=output, stderr=io.StringIO(),
            monotonic=lambda: self.elapsed, wall_time=lambda: self.epoch + self.elapsed,
            wait_readable=scripted_readiness((self.sleep), interval=(1.0)), sleep=self.sleep,
        )
        return json.loads(output.getvalue())

    def test_persistent_corrupt_consent_allows_only_one_fault_turn_per_window(self):
        root, workspace, participant = self.fixture("consent-cadence")
        path = root.resolve_relative(consent_ledger_relative(participant.binding.node_id))
        path.write_bytes(b"{")
        first = self.run_fault(root, workspace)
        self.assertEqual(0.0, self.elapsed, "first fault must wake immediately")
        second = self.run_fault(root, workspace)
        self.assertAlmostEqual(2.0, self.elapsed, places=2, msg="same fault must wait out its hold window")
        third = self.run_fault(root, workspace)
        self.assertAlmostEqual(4.0, self.elapsed, places=2)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        diagnostics = [row["integrity_failure"] for row in self.rows(root)]
        self.assertEqual([1, 2, 3], [row["occurrence_count"] for row in diagnostics])
        self.assertEqual([2, 2, 2], [row["hold_window_seconds"] for row in diagnostics])
        self.assertEqual([0, 2, 2], [row["waited_seconds"] for row in self.rows(root)])

    def test_different_check_on_same_input_still_wakes_immediately(self):
        root, workspace, participant = self.fixture("changed-check")
        path = root.resolve_relative(consent_ledger_relative(participant.binding.node_id))
        path.write_bytes(b"{")
        first = self.run_fault(root, workspace)
        path.write_bytes(b"not-json\n")
        second = self.run_fault(root, workspace)
        self.assertEqual(0.0, self.elapsed)
        self.assertNotEqual(first["reason"], second["reason"])
        self.assertEqual([1, 1], [row["integrity_failure"]["occurrence_count"] for row in self.rows(root)])

    def test_pre_breaker_session_fault_also_observes_the_hold_cadence(self):
        from pathlib import Path

        root, workspace, participant = self.fixture("authority-cadence")
        path = root.resolve_relative(Path("receipts/codex-wait-session") / (participant.binding.node_id + ".jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{")
        first = self.run_fault(root, workspace)
        self.assertEqual(0.0, self.elapsed)
        second = self.run_fault(root, workspace)
        self.assertAlmostEqual(2.0, self.elapsed, places=2)
        self.assertEqual(first, second)

    def test_unreadable_exit_testimony_cannot_spin_fault_turns(self):
        root, workspace, participant = self.fixture("exit-read-cadence")
        root.resolve_relative(consent_ledger_relative(participant.binding.node_id)).write_bytes(b"{")
        path = root.resolve_relative(Path("receipts/wake-waiter-exit") / (participant.binding.node_id + ".jsonl"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{")
        first = self.run_fault(root, workspace)
        self.assertEqual(2.0, self.elapsed)
        second = self.run_fault(root, workspace)
        self.assertEqual(4.0, self.elapsed)
        self.assertEqual(first, second)
        self.assertEqual(b"{", path.read_bytes())

    def test_failed_exit_write_holds_only_one_total_window(self):
        root, workspace, participant = self.fixture("exit-write-cadence")
        root.resolve_relative(consent_ledger_relative(participant.binding.node_id)).write_bytes(b"{")
        first = self.run_fault(root, workspace)
        self.assertEqual(0.0, self.elapsed)
        with mock.patch("floati.wake_exit.WakeExitLedger.record", side_effect=OSError("private-content")):
            second = self.run_fault(root, workspace)
            self.assertEqual(2.0, self.elapsed, "failed write must not add a second hold window")
            third = self.run_fault(root, workspace)
            self.assertEqual(4.0, self.elapsed)
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertNotIn("private-content", json.dumps(second))

    def test_expired_window_allows_an_immediate_new_fault(self):
        root, workspace, participant = self.fixture("elapsed-cadence")
        root.resolve_relative(consent_ledger_relative(participant.binding.node_id)).write_bytes(b"{")
        first = self.run_fault(root, workspace)
        self.elapsed = 2.0
        second = self.run_fault(root, workspace)
        self.assertEqual(2.0, self.elapsed)
        self.assertEqual(first, second)

    def test_arm_default_and_gateway_use_the_same_wait_deadline(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger, DEFAULT_WAIT_DEADLINE_SECONDS

        require_private_artifact(self, "tools/codex/codex-fleet-bus.py")
        root, _workspace, participant = self.fixture("arm-default-cadence")
        gateway = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/codex/codex-fleet-bus.py"))
        self.assertEqual(DEFAULT_WAIT_DEADLINE_SECONDS, gateway["FIXED_WAIT_DEADLINE_SECONDS"])
        receipt = CodexWaitConsentLedger(root).arm(
            participant.binding, hook_timeout_seconds=gateway["FIXED_HOOK_TIMEOUT_SECONDS"],
            idempotency_key="arm-shared-deadline",
        )
        self.assertEqual(DEFAULT_WAIT_DEADLINE_SECONDS, receipt["wait_deadline_seconds"])

    def test_unreadable_consent_preserves_the_previously_proven_shorter_window(self):
        import os
        from floati.codex_wait_contract import DEFAULT_WAIT_DEADLINE_SECONDS

        root, workspace, participant = self.fixture("known-short-window")
        path = root.resolve_relative(consent_ledger_relative(participant.binding.node_id))

        def corrupt(seconds):
            self.sleep(seconds)
            replacement = path.with_suffix(".replacement")
            replacement.write_bytes(b"{")
            os.replace(replacement, path)

        first = io.StringIO()
        with mock.patch("floati.codex_wait.DEFAULT_WAIT_DEADLINE_SECONDS", DEFAULT_WAIT_DEADLINE_SECONDS):
            run_stop_waiter(
                bus_home=root.path,
                hook_payload={"cwd": str(workspace), "session_id": "seat-cadence"},
                stdout=first, stderr=io.StringIO(),
                monotonic=lambda: self.elapsed, wall_time=lambda: self.epoch + self.elapsed,
                wait_readable=scripted_readiness((corrupt), interval=(1.0)), sleep=corrupt,
            )
            self.assertEqual(1.0, self.elapsed)
            second = self.run_fault(root, workspace)
        self.assertEqual(3.0, self.elapsed, "known two-second wait must survive unreadable consent")
        self.assertEqual(json.loads(first.getvalue()), second)
        self.assertEqual([2, 2], [row["integrity_failure"]["hold_window_seconds"] for row in self.rows(root)])
        self.assertEqual([1, 2], [row["integrity_failure"]["occurrence_count"] for row in self.rows(root)])

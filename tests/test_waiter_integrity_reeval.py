"""WAITER-INTEGRITY-REEVAL-1: repair must not leave an integrity hold deaf.

The deterministic cases inject only native clocks/sleeps (and, where named,
an exit-receipt I/O failure). The final case delegates every sleep to real
time.sleep. All files, claims, mail and delivery receipts belong to scratch
fixtures. A prompt typed integrity return is a valid alternative to in-place
re-evaluation; that branch must permit a native re-arm within the original
residual budget. No evaluator, watch, authority or binding reader is replaced.
"""
from __future__ import annotations

from contextlib import nullcontext
import io
import json
import time
import unittest
from unittest import mock

from floati import codex_wait
from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import CodexWaitConsentLedger, consent_ledger_relative
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.waiter_lock_retry import waiter_lock_window
from floati.wake_control import WakeController
from floati.records import WAKE_HOLD_KINDS
from tests import test_wake_waiter_continuity as fixtures


class WaiterIntegrityReevaluationTests(unittest.TestCase):
    fixture = fixtures.WakeWaiterContinuityTests.fixture
    rows = staticmethod(fixtures.WakeWaiterContinuityTests.rows)
    session = "seat-integrity-reeval"
    budget = 6

    def setUp(self):
        fixtures.WakeWaiterContinuityTests.setUp(self)
        self.clock = 0.0
        self.epoch = float(int(time.time()))

    def prepared(self, label, *, fault="events", budget=None):
        root, workspace, participant = self.fixture(label)
        CodexWaitConsentLedger(root).arm(
            participant.binding, hook_timeout_seconds=10,
            wait_deadline_seconds=budget or self.budget,
            idempotency_key="reeval-consent-" + label,
        )
        relative = ("events.jsonl" if fault == "events" else
                    consent_ledger_relative(participant.binding.node_id))
        path = root.resolve_relative(relative)
        original = path.read_bytes() if path.exists() else b""
        path.write_bytes(b"{")
        return root, workspace, participant, path, original

    def invoke(self, root, workspace, *, sleep=None, window=None, real=False):
        from tests.waiter_timing import scripted_readiness
        output, errors, result = io.StringIO(), io.StringIO(), {}
        timing = ({"monotonic": time.monotonic, "wall_time": time.time,
                   "sleep": sleep or time.sleep} if real else
                  {"monotonic": lambda: self.clock,
                   "wall_time": lambda: self.epoch + self.clock,
                   "sleep": sleep or self.advance})
        timing["wait_readable"] = scripted_readiness(timing["sleep"])
        code = run_stop_waiter(
            bus_home=root.path,
            hook_payload={"cwd": str(workspace), "session_id": self.session},
            stdout=output, stderr=errors, _waiter_result=result,
            **({"_waiter_window": window} if window is not None else {}),
            **timing,
        )
        self.assertEqual(0, code, errors.getvalue())
        if not output.getvalue() and result == {
            "kind": "authority_changed", "reason": "paused", "decision": {},
        }:
            # Native startup pause is intentionally silent (codex_wait's
            # is_session_paused branch); its invocation result is typed.
            self.assertEqual("", errors.getvalue())
            return result
        self.assertEqual(1, len(output.getvalue().splitlines()), errors.getvalue())
        self.assertEqual(json.loads(output.getvalue()), result["decision"])
        return result

    def advance(self, seconds):
        self.clock += seconds

    def repair_and_send(self, root, participant, path, original):
        path.write_bytes(original)
        return EventLog(root).send(
            "architect", participant.binding.node_id, "repo", "a" * 40,
            "docs/proof.md", "scratch integrity repair notification",
            idempotency_key="reeval-exact-mail",
        )

    def assert_fresh(self, result, root, participant, envelope):
        self.assertEqual("fresh_work", result["kind"], result)
        self.assertEqual([envelope["id"]], result["item_ids"])
        self.assertIn(envelope["id"], result["decision"]["reason"])
        self.assertEqual([envelope["id"]], result["receipt"]["item_ids"])
        # Exact native terminal delivery, rather than a fabricated output ID.
        deliveries = read_records_snapshot(
            root, "receipts/deliveries/" + participant.binding.node_id + ".jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS),
        )
        self.assertTrue(any(envelope["id"] in row.get("item_ids", [])
                            for row in deliveries), deliveries)

    def repaired_repeat(self, fault, *, withdraw=False):
        root, workspace, participant, path, original = self.prepared(
            "repeat-" + fault + ("-withdraw" if withdraw else ""), fault=fault)
        first = self.invoke(root, workspace)
        self.assertEqual("integrity_failure", first["kind"], first)
        self.assertEqual(0.0, self.clock, "seed must be the native first alert")
        envelope = []

        def repair():
            if not envelope:
                if withdraw:
                    WakeController(root).pause(
                        participant.binding.node_id, self.session,
                        idempotency_key="reeval-withdraw-exact-session")
                envelope.append(self.repair_and_send(root, participant, path, original))

        def sleep(seconds):
            self.advance(seconds)
            repair()

        with waiter_lock_window(self.budget, lambda: self.clock, sleep) as window:
            # Advance within the scope, preserving its native original origin.
            self.clock = 2.0
            result = self.invoke(root, workspace, sleep=sleep, window=window)

        self.assertLessEqual(self.clock, self.budget, "no second hold window")
        if result["kind"] == "fresh_work":
            self.assertFalse(withdraw, "withdrawn authority must never publish mail")
            self.assertTrue(envelope, "fixture repair must precede delivery")
            self.assertLess(self.clock, self.budget, "fresh work must beat exhaustion")
            self.assert_fresh(result, root, participant, envelope[0])
            return
        self.assertIn(result["kind"], {"integrity_failure", "authority_changed"}, result)
        if result["kind"] == "integrity_failure":
            self.assertEqual(first["code"], result["code"], "honest native refusal")
            self.assertIn("integrity refusal", result["decision"]["reason"])
        else:
            self.assertTrue(withdraw, result)
            self.assertEqual("paused", result["reason"])
        self.assertLessEqual(
            self.clock, 3.0,
            "repaired integrity hold slept the entire original residual instead "
            "of re-evaluating or promptly returning typed for harness re-arm",
        )
        repair()  # A zero-sleep typed return also permits the external repair.
        residual = self.budget - self.clock
        with waiter_lock_window(residual, lambda: self.clock, self.advance) as window:
            successor = self.invoke(root, workspace, window=window)
        if withdraw:
            self.assertEqual("authority_changed", successor["kind"], successor)
            self.assertEqual("paused", successor["reason"])
            self.assertEqual({}, successor["decision"])
            self.assertNotIn("item_ids", successor)
            self.assertEqual("paused", self.rows(root)[-1]["reason_code"])
            self.assertFalse(root.resolve_relative(
                "receipts/deliveries/" + participant.binding.node_id + ".jsonl").exists(),
                "paused original or successor invocation must not deliver the pending exact ID")
        else:
            self.assert_fresh(successor, root, participant, envelope[0])
        self.assertLess(self.clock, self.budget, "re-arm must retain original residual")

    def test_repeated_native_events_integrity_hears_repair_or_returns_promptly(self):
        self.repaired_repeat("events")

    def test_repeated_native_consent_integrity_hears_repair_or_returns_promptly(self):
        self.repaired_repeat("consent")

    def test_native_repair_before_cooldown_witness_hears_mail_or_returns_promptly(self):
        # Diagnostic-boundary race injection: the original native diagnostic
        # reads its real prior receipt and returns its unchanged result. Repair
        # and exact native send then happen before _integrity_exit can sample a
        # cooldown witness. No content reader/evaluator/authority is replaced.
        root, workspace, participant, path, original = self.prepared("pre-witness-repair")
        first = self.invoke(root, workspace)
        self.assertEqual("integrity_failure", first["kind"], first)
        self.assertEqual(0.0, self.clock)
        original_diagnostic = codex_wait._integrity_diagnostic
        envelope = []

        def diagnose_then_repair(*args, **kwargs):
            diagnostic = original_diagnostic(*args, **kwargs)
            if not envelope:
                envelope.append(self.repair_and_send(root, participant, path, original))
            return diagnostic

        with waiter_lock_window(self.budget, lambda: self.clock, self.advance) as window:
            self.clock = 2.0
            with mock.patch("floati.codex_wait._integrity_diagnostic",
                            side_effect=diagnose_then_repair):
                result = self.invoke(root, workspace, window=window)
        self.assertTrue(envelope, "race must repair/send at the native diagnostic boundary")
        self.assertLessEqual(self.clock, self.budget, "no second hold window")
        if result["kind"] == "fresh_work":
            self.assert_fresh(result, root, participant, envelope[0])
        else:
            self.assertEqual("integrity_failure", result["kind"], result)
            self.assertEqual(first["code"], result["code"], "preserve original native refusal")
            self.assertIn("integrity refusal", result["decision"]["reason"])
            self.assertLessEqual(
                self.clock, 3.0,
                "repair before first cooldown witness remained deaf for the "
                "whole original residual window",
            )
            with waiter_lock_window(self.budget - self.clock,
                                    lambda: self.clock, self.advance) as window:
                successor = self.invoke(root, workspace, window=window)
            self.assert_fresh(successor, root, participant, envelope[0])
        self.assertLess(self.clock, self.budget, "delivery/re-arm must retain original residual")

    def test_withdrawal_during_integrity_hold_never_publishes_and_returns_promptly(self):
        self.repaired_repeat("events", withdraw=True)

    def test_exit_receipt_failure_hears_repaired_mail_or_returns_promptly(self):
        root, workspace, participant, path, original = self.prepared("write-failure-repair")
        envelope = []

        def repair():
            if not envelope:
                envelope.append(self.repair_and_send(root, participant, path, original))

        def sleep(seconds):
            self.advance(seconds)
            repair()

        with mock.patch("floati.wake_exit.WakeExitLedger.record",
                        side_effect=OSError("private fixture exit-write failure")):
            result = self.invoke(root, workspace, sleep=sleep)
        self.assertLessEqual(self.clock, self.budget, "failed receipt must not add a window")
        if result["kind"] == "fresh_work":
            self.assertTrue(envelope)
            self.assert_fresh(result, root, participant, envelope[0])
        else:
            self.assertEqual("integrity_failure", result["kind"], result)
            self.assertIn("integrity refusal", result["decision"]["reason"])
            self.assertNotIn("private fixture", json.dumps(result))
            self.assertLessEqual(
                self.clock, 1.0,
                "receipt failure suppressed repair/mail for the remaining hold window",
            )
            repair()
            with waiter_lock_window(self.budget - self.clock,
                                    lambda: self.clock, self.advance) as window:
                successor = self.invoke(root, workspace, window=window)
            self.assert_fresh(successor, root, participant, envelope[0])
        self.assertLess(self.clock, self.budget)

    def persistent_bad(self, *, write_failure=False):
        root, workspace, participant, _path, _original = self.prepared(
            "unchanged-write" if write_failure else "unchanged-bad")
        first = self.invoke(root, workspace)
        self.assertEqual("integrity_failure", first["kind"])
        with waiter_lock_window(self.budget, lambda: self.clock, self.advance) as window:
            self.clock = 2.0
            context = (mock.patch("floati.wake_exit.WakeExitLedger.record",
                                  side_effect=OSError("private fixture"))
                       if write_failure else nullcontext())
            with context:
                result = self.invoke(root, workspace, window=window)
        self.assertEqual("integrity_failure", result["kind"], result)
        self.assertEqual(first["code"], result["code"])
        self.assertNotIn("item_ids", result)
        self.assertLessEqual(self.clock, self.budget, "must retain original residual cap")
        deliveries = root.resolve_relative(
            "receipts/deliveries/" + participant.binding.node_id + ".jsonl")
        self.assertFalse(deliveries.exists(), "unrepaired corruption cannot publish fresh mail")
        self.assertNotIn("private fixture", json.dumps(result))

    def test_unchanged_bad_input_never_fresh_and_keeps_original_deadline(self):
        self.persistent_bad()

    def test_failed_exit_write_never_adds_to_original_residual_deadline(self):
        self.persistent_bad(write_failure=True)

    def test_native_rearm_after_first_alert_delivers_exact_repaired_mail(self):
        # Baseline control exercises the same exact-delivery assertions used by
        # both allowed success paths, independently of the failing cooldown.
        root, workspace, participant, path, original = self.prepared("native-rearm-control")
        first = self.invoke(root, workspace)
        self.assertEqual("integrity_failure", first["kind"], first)
        self.assertEqual(0.0, self.clock)
        envelope = self.repair_and_send(root, participant, path, original)
        with waiter_lock_window(self.budget, lambda: self.clock, self.advance) as window:
            successor = self.invoke(root, workspace, window=window)
        self.assert_fresh(successor, root, participant, envelope)
        self.assertLess(self.clock, self.budget)

    def test_real_clock_native_stop_does_not_finish_repaired_hold_at_deadline(self):
        # A real-clock, real-sleep native Stop, not a deciding performance run.
        # Synchronous scratch repair/send occurs before the first actual sleep;
        # no helper child/thread or process can outlive fixture cleanup.
        budget = 4
        root, workspace, participant, path, original = self.prepared(
            "real-clock-repair", budget=budget)
        first = self.invoke(root, workspace, real=True)
        self.assertEqual("integrity_failure", first["kind"], first)
        envelope = []

        def repair():
            if not envelope:
                envelope.append(self.repair_and_send(root, participant, path, original))

        def sleep(seconds):
            repair()
            time.sleep(seconds)  # Exact requested native duration, real clock.

        started = time.monotonic()
        result = self.invoke(root, workspace, sleep=sleep, real=True)
        elapsed = time.monotonic() - started
        if result["kind"] == "fresh_work":
            self.assertTrue(envelope)
            self.assert_fresh(result, root, participant, envelope[0])
        else:
            self.assertEqual("integrity_failure", result["kind"], result)
            self.assertEqual(first["code"], result["code"])
            self.assertLess(
                elapsed, budget - 0.5,
                "real native Stop returned integrity only after consuming its "
                "hold, although scratch input was repaired before first sleep",
            )
            repair()
            with waiter_lock_window(max(0.0, budget - elapsed),
                                    time.monotonic, time.sleep) as window:
                successor = self.invoke(root, workspace, window=window, real=True)
            self.assert_fresh(successor, root, participant, envelope[0])
        self.assertLess(time.monotonic() - started, budget)


if __name__ == "__main__":
    unittest.main()

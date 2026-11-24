"""Authorized Stop repairs a pending epoch before native presentation.

Small fixture histories only. Native watch polls, recovery, controllers,
decisions and readers remain real. Exact-wake controls sleep normally; the
deadline control advances its injected monotonic clock at recovery/readiness
so startup cost cannot consume the interval the test intends to observe.
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import tests  # Pin HOME before product imports.

from floati import bus_epoch, codex_wait
from floati.codex_wait_contract import (
    CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant,
)
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.waiter_consent import arm_waiter_consent
from floati.wake_control import WakeController
from floati.wake_hold import WakeHoldController
from floati.records import WAKE_HOLD_KINDS
from floati.stop_hold import _HoldEvidence
from tests import test_bus_epoch_roll as native_fixture
from tests.waiter_timing import scripted_readiness


class EpochPendingWaiterTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixture.GovernedBusEpochRollTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.root = self.fixture.root
        self.workspace = self.fixture.base / "waiter-workspace"
        self.workspace.mkdir()
        self.node = "recipient"
        self.session = "pending-epoch-waiter"
        arm_waiter_consent(self.root.path, self.workspace, self.node, "Codex", 10, 6)
        self.participant = resolve_participant(self.root.path, self.workspace)
        assert self.participant is not None
        consent = CodexWaitConsentLedger(self.root).require_armed(self.participant.binding)
        CodexWaitSessionLedger(self.root).arm(
            self.participant.binding, consent, self.session,
            idempotency_key="pending-epoch-session",
        )
        old = self._send("already-seen")
        EventLog(self.root).present(self.node)
        SparseCursor(self.root).ack(self.node, [old["id"]], acting_session_id="seed-ack")
        self.fixture._grant_roll_authority(self.root, "actor-a")

    def _send(self, key):
        return EventLog(self.root).send(
            "actor-a", self.node, "floati", "a" * 40,
            "docs/evidence/pending-epoch-fixture.md", "fixture notification",
            idempotency_key=key,
        )

    def _selected_bytes(self):
        return {
            relative: path.read_bytes()
            for relative, path, _plane, identity
            in bus_epoch._preflight_selected_members(self.root)
            if identity is not None
        }

    def _tree_bytes(self, path):
        if not path.exists():
            return None
        if path.is_file():
            return path.read_bytes()
        return {
            child.relative_to(path).as_posix(): child.read_bytes() if child.is_file() else None
            for child in sorted(path.rglob("*"))
        }

    def _run_pending(self, state, boundary, *, partial_frame=False, marker_payload=None, terminal=None):
        class SimulatedStop(Exception):
            pass

        def stop(name):
            if name == boundary:
                raise SimulatedStop()

        original_write = os.write
        events = self.root.resolve_relative("events.jsonl").stat()

        def native_short_write(descriptor, data):
            status = os.fstat(descriptor)
            if partial_frame and (status.st_dev, status.st_ino) == (events.st_dev, events.st_ino):
                return original_write(descriptor, data[:8])
            return original_write(descriptor, data)

        with mock.patch.object(os, "write", native_short_write):
            with self.assertRaises(SimulatedStop):
                bus_epoch.roll_bus_epoch(
                    self.root, actor="actor-a", idempotency_key="pending-waiter-" + state.lower(),
                    fault=stop,
                )
        pending = bus_epoch.observe_epoch_roll_state(self.root)
        self.assertEqual(state, pending["classification"])
        marker = pending["marker"]
        protected = [Path(pending["marker_path"])] + [
            Path(marker[field]) for field in ("archive_path", "staging_path")
        ]
        if marker_payload is not None:
            # Fixture-only interrupted/corrupt marker state; no product result
            # is mocked and no selected history is changed by this fault.
            protected[0].write_bytes(marker_payload)
        transition_before = {str(path): self._tree_bytes(path) for path in protected}
        selected_before = self._selected_bytes()
        history_paths = [
            self.root.resolve_relative(CursorPresentationLedger.relative(self.node)),
            self.root.resolve_relative(CursorPresentationLedger.witness_relative(self.node)),
        ]
        history_before = {str(path): self._tree_bytes(path) for path in history_paths}
        stdout, stderr = io.StringIO(), io.StringIO()
        native_result = {}
        clean_polls = []
        recovery = []
        sent = []
        clock = [0.0]
        waits = []
        observed_reads = {"sources": 0, "controller": 0, "hint": 0}
        original_changed = codex_wait._WaiterInputsWatch.changed
        original_sources = CursorPresentationLedger._sources
        original_read = WakeHoldController._read
        original_hint = codex_wait._WaiterInputsWatch._has_epoch_receipt
        original_reconcile = bus_epoch.reconcile_epoch_roll
        original_report = _HoldEvidence._report

        def report(hold, phase, failure):
            original_report(hold, phase, failure)
            stderr.write("hold diagnostic: %s: %s\n" % (
                getattr(failure, "code", type(failure).__name__),
                getattr(failure, "detail", str(failure))))

        def malformed_unchanged():
            self.assertTrue(protected[0].exists(), "malformed transition must remain preserved")
            self.assertEqual(selected_before, self._selected_bytes())
            self.assertEqual(transition_before, {str(path): self._tree_bytes(path) for path in protected})
            self.assertEqual(history_before, {str(path): self._tree_bytes(path) for path in history_paths})

        if terminal == "pause":
            status = codex_wait.run_stop_waiter(bus_home=self.root.path,
                hook_payload={"cwd": str(self.workspace), "session_id": self.session},
                stdout=stdout, stderr=stderr, _waiter_result=native_result)
            self.assertEqual(0, status)
            self.assertEqual("integrity_failure", native_result["kind"])
            self.assertEqual("epoch_marker_invalid", native_result["code"])
            malformed_unchanged()
            WakeController(self.root).pause(self.node, self.session, idempotency_key="pending-pause")
            paused = {}
            codex_wait.run_stop_waiter(bus_home=self.root.path,
                hook_payload={"cwd": str(self.workspace), "session_id": self.session},
                stdout=io.StringIO(), stderr=io.StringIO(), _waiter_result=paused)
            self.assertEqual(("authority_changed", "paused"), (paused["kind"], paused["reason"]))
            malformed_unchanged()
            return

        def observed_reconcile(root, *args, **kwargs):
            repaired = original_reconcile(root, *args, **kwargs)
            if repaired is not None:
                recovery.append(repaired)
                self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(root)["classification"])
                if terminal == "deadline":
                    # Recovery consumes part of the invocation's original
                    # budget. Real startup/IO never consumes fixture time.
                    clock[0] = 0.75
            return repaired

        def observed_changed(watch, *args, **kwargs):
            changed = original_changed(watch, *args, **kwargs)
            caller = sys._getframe(1)
            if (
                recovery and not clean_polls
                and caller.f_code is codex_wait._run_stop_waiter.__code__
                and caller.f_locals.get("idle_artifact") is not None
                and changed[0] is False
            ):
                # This is after native automatic recovery and evaluation and
                # its tail sleep, at the next genuinely clean native poll.
                self.assertFalse(protected[0].exists())
                clean_polls.append(changed)
            return changed

        def observed_sources(plane, home, node, *args, **kwargs):
            observed_reads["sources"] += 1
            self.assertFalse(protected[0].exists(), "pending presentation history must remain quarantined")
            return original_sources(plane, home, node, *args, **kwargs)

        def observed_read(controller, *args, **kwargs):
            observed_reads["controller"] += 1
            self.assertFalse(protected[0].exists(), "pending selected frames must not be consumed")
            return original_read(controller, *args, **kwargs)

        def observed_hint(watch, *args, **kwargs):
            observed_reads["hint"] += 1
            self.assertFalse(protected[0].exists(), "pending events must not be read for a watch hint")
            return original_hint(watch, *args, **kwargs)

        def sleep(seconds):
            self.assertEqual(1, len(recovery), "native Stop must repair before idle")
            self.assertFalse(protected[0].exists())
            if clean_polls and not sent and terminal is None:
                sent.append(self._send("post-recovery-exact-notification"))
            if terminal == "deadline":
                waits.append(seconds)
                clock[0] += seconds
            else:
                time.sleep(seconds)

        with mock.patch.object(_HoldEvidence, "_report", report),\
                mock.patch.object(codex_wait._WaiterInputsWatch, "changed", observed_changed),\
                mock.patch.object(CursorPresentationLedger, "_sources", observed_sources),\
                mock.patch.object(WakeHoldController, "_read", observed_read),\
                mock.patch.object(codex_wait._WaiterInputsWatch, "_has_epoch_receipt", observed_hint),\
                mock.patch.object(bus_epoch, "reconcile_epoch_roll", observed_reconcile):
            status = codex_wait.run_stop_waiter(
                bus_home=self.root.path,
                hook_payload={"cwd": str(self.workspace), "session_id": self.session},
                stdout=stdout, stderr=stderr, wait_readable=scripted_readiness((sleep), interval=(0.01)), sleep=sleep,
                poll_interval_seconds=0.01, _waiter_result=native_result,
                **({"monotonic": lambda: clock[0]} if terminal == "deadline" else {}),
            )

        self.assertEqual(0, status, stderr.getvalue())
        self.assertEqual(1, len(clean_polls), stderr.getvalue())
        for seam, count in observed_reads.items():
            self.assertGreater(count, 0, seam + " quarantine observer never executed after recovery")
        if terminal is not None:
            self.assertEqual(1, len(recovery))
            self.assertEqual([], sent)
            self.assertEqual("absent", bus_epoch.observe_epoch_roll_state(self.root)["classification"])
            self.assertNotIn("IntegrityFailure", stderr.getvalue())
            self.assertEqual("exhausted", native_result["kind"])
            self.assertTrue(waits, "the original residual window was never observed")
            self.assertLessEqual(clock[0], 1.0, "recovery renewed the original deadline")
            self.assertAlmostEqual(clock[0], 1.0, msg="recovery lost the original residual window")
            self.assertEqual("", stderr.getvalue(), "recovery must not lose native hold testimony")
            self.assertIn("deadline exhausted", json.loads(stdout.getvalue())["reason"])
            self.assertEqual(1, len(read_records_snapshot(
                self.root, "receipts/codex-wait-exhaustion/recipient.jsonl",
                allowed_kinds={"codex_wait_exhaustion_receipt"},
            )))
            return
        self.assertEqual(1, len(recovery))
        self.assertEqual(1, len(sent))
        message_id = sent[0]["id"]
        self.assertEqual(1, len(stdout.getvalue().splitlines()), stderr.getvalue())
        decision = json.loads(stdout.getvalue())
        self.assertEqual("block", decision["decision"])
        self.assertEqual([message_id], re.findall(r"msg-[0-9a-f]{32}", decision["reason"]))
        self.assertEqual([message_id], native_result["item_ids"])
        presentations = CursorPresentationLedger(self.root).read(self.node)
        self.assertEqual(["prepared", "flushed"], [row["phase"] for row in presentations])
        self.assertEqual([message_id], presentations[0]["item_ids"])
        deliveries = read_records_snapshot(
            self.root, "receipts/deliveries/recipient.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS),
        )
        exact_delivery = [row for row in deliveries if row["id"] == presentations[-1]["delivery_receipt_id"]]
        self.assertEqual(1, len(exact_delivery))
        self.assertEqual([message_id], exact_delivery[0]["item_ids"])
        attempts = read_records_snapshot(
            self.root, "receipts/wakes/recipient.jsonl", allowed_kinds={"wake_attempt_receipt"},
        )
        self.assertEqual(1, len(attempts))
        self.assertEqual(("woke", [message_id], presentations[-1]["id"]), (
            attempts[0]["outcome"], attempts[0]["item_ids"], attempts[0]["decision_receipt_id"],
        ))
        self.assertNotIn("IntegrityFailure", stderr.getvalue())

    def test_prepared_marker_automatically_recovers_before_clean_poll_and_exact_wake(self):
        self._run_pending("PREPARED", "marker_prepared_durable")

    def test_committed_marker_automatically_recovers_before_clean_poll_and_exact_wake(self):
        self._run_pending("COMMITTED", "marker_committed_durable")

    def test_partial_selected_frame_is_not_read_before_automatic_recovery(self):
        self._run_pending("COMMITTED", "live_events_write_1", partial_frame=True)

    def test_partial_marker_payload_uses_native_automatic_recovery(self):
        self._run_pending("PREPARED", "marker_prepared_durable", marker_payload=b'{"state":')

    def test_malformed_marker_payload_holds_until_native_authority_pause(self):
        self._run_pending("PREPARED", "marker_prepared_durable", marker_payload=b'{}\n', terminal="pause")

    def test_automatically_recovered_marker_keeps_original_deadline(self):
        CodexWaitConsentLedger(self.root).arm(
            self.participant.binding, hook_timeout_seconds=10, wait_deadline_seconds=1,
            idempotency_key="pending-short-deadline",
        )
        self._run_pending("COMMITTED", "marker_committed_durable", terminal="deadline")

    def test_current_session_starts_hold_with_replaced_current_consent(self):
        from floati.stop_hold import StopHoldLedger
        consent = CodexWaitConsentLedger(self.root).arm(self.participant.binding,
            hook_timeout_seconds=10, wait_deadline_seconds=1,
            idempotency_key="hold-replaced-current-consent")
        error = io.StringIO()
        hold = _HoldEvidence(error, lambda: "2026-10-25T12:00:00.000Z")
        self.addCleanup(hold.close)
        hold.start(self.participant, consent, self.session, 1)
        self.assertEqual("", error.getvalue(), "current consent must not invalidate the current session's hold")
        self.assertIsNotNone(hold.start_row)
        self.assertEqual(consent["id"], hold.start_row["consent_receipt_id"])
        self.assertTrue(hold.close(require_proof=True))
        self.assertEqual(["start", "exit"], [row["phase"]
            for row in StopHoldLedger(self.root).read(self.node)])


if __name__ == "__main__":
    unittest.main()

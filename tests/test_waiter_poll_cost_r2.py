"""Composed presentation/watch regressions on native scratch fleets only."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import io
from itertools import product
import json
import unittest
from unittest import mock

from floati import codex_wait, tui
from floati.bus_epoch import epoch_guard
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.cursor_presentations import CursorPresentationLedger, limit_selection
from floati.wake_hold import wake_coordination_guard
from tests import test_codex_wait as baseline
from tests.waiter_timing import scripted_readiness


def exercise_watch_paths(test, exercise, **variants):
    axes = {"fallback": (False, True), **variants}
    for index, values in enumerate(product(*axes.values())):
        arguments = dict(zip(axes, values))
        with test.subTest(**arguments):
            if index:
                # Each mutation gets a fresh fleet and presentation history.
                test.doCleanups()
                test.setUp()
            exercise(**arguments)


class WaiterPollCostR2Tests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.session = "r2-presentation-conversation"
        self.f.arm_session(self.session)
        self.node = self.f.participant.binding.node_id
        self.plane = CursorPresentationLedger(self.f.root)
        self.history = self.f.root.resolve_relative(self.plane.relative(self.node))
        self.witness = self.f.root.resolve_relative(self.plane.witness_relative(self.node))
        self.payload = {"cwd": str(self.f.workspace), "session_id": self.session}

    @contextmanager
    def selection(self):
        with epoch_guard(self.f.root, exclusive=False), wake_coordination_guard(self.f.root, self.node):
            binding = self.f.participant.binding
            consent = CodexWaitConsentLedger(self.f.root).require_armed(binding)
            classification = CodexWaitSessionLedger(self.f.root)._participate_already_guarded(
                binding, consent, self.session, classified=True)
            yield self.plane.select(binding, consent, classification, harness="codex")

    @contextmanager
    def watch(self, fallback):
        with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("scratch unavailable")) if fallback else _unpatched():
            errors = io.StringIO()
            watch = codex_wait._WaiterInputsWatch(self.f.participant,
                hashlib.sha256(self.session.encode()).hexdigest(), errors)
        try:
            if fallback:
                self.assertIsNone(watch.source)
                self.assertEqual(1, errors.getvalue().count("using bounded polling"))
            else:
                self.assertIsNotNone(watch.source, errors.getvalue())
                self.assertEqual("", errors.getvalue())
            watch.changed()  # Drain registration events before the isolated write.
            yield watch
        finally:
            watch.close()

    def prepare(self, selection):
        bounded, body = limit_selection(selection, lambda messages: {
            "decision": "block", "reason": "Exact native envelopes:\n" + json.dumps(messages)})
        return self.plane.prepare(bounded, body)

    def presentation_append(self, fallback):
        old = self.f.send("r2-unacked-prepare")
        with self.selection() as selection:
            before_events = self.f.root.resolve_relative("events.jsonl").read_bytes()
            with self.watch(fallback) as watch:
                self.prepare(selection)
                changed = watch.changed()
            self.assertEqual(before_events, self.f.root.resolve_relative("events.jsonl").read_bytes())
        rows = self.plane.read(self.node)
        self.assertEqual(["prepared"], [row["phase"] for row in rows])
        self.assertEqual([old["id"]], rows[0]["item_ids"])
        self.assertEqual((True, True), changed,
            "native unACKed presentation append did not invalidate the idle decision")

    def witness_reservation(self, fallback):
        old = self.f.send("r2-write-ahead-only")
        with self.selection() as selection:
            before_history = self.history.read_bytes()
            before_witness = self.witness.read_bytes()
            with self.watch(fallback) as watch:
                with mock.patch.object(self.plane, "_append_reserved", side_effect=OSError("scratch append stopped")):
                    with self.assertRaises(OSError):
                        self.prepare(selection)
                changed = watch.changed()
            self.assertEqual(before_history, self.history.read_bytes())
            self.assertNotEqual(before_witness, self.witness.read_bytes())
            self.assertEqual([old["id"]], json.loads(self.witness.read_bytes())["reserved_record"]["item_ids"])
        self.assertEqual((True, True), changed,
            "durable write-ahead reservation did not invalidate the idle decision")

    def held(self):
        old = self.f.send("r2-held-original")
        output, errors = io.StringIO(), io.StringIO()
        self.assertEqual(0, codex_wait.run_stop_waiter(bus_home=self.f.bus_home,
            hook_payload=self.payload, stdout=output, stderr=errors))
        self.assertIn(old["id"], json.loads(output.getvalue())["reason"], errors.getvalue())
        self.assertEqual(["prepared", "flushed"], [row["phase"] for row in self.plane.read(self.node)])

    def idle(self, change, fallback):
        clock, polls = [0.0], []
        output, errors = io.StringIO(), io.StringIO()
        def tick(seconds):
            clock[0] += seconds
            polls.append(clock[0])
            if len(polls) == 1:
                change()
        with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("scratch unavailable")) if fallback else _unpatched():
            self.assertEqual(0, codex_wait.run_stop_waiter(bus_home=self.f.bus_home,
                hook_payload=self.payload, stdout=output, stderr=errors,
                monotonic=lambda: clock[0], wait_readable=scripted_readiness((tick), interval=(0.1)), sleep=tick, poll_interval_seconds=0.1))
        return json.loads(output.getvalue()), errors.getvalue(), polls

    def changed_history(self, fallback, witness):
        self.held()
        before = self.f.root.resolve_relative("events.jsonl").read_bytes()
        change = (lambda: self.witness.write_bytes(b"{broken native witness\n")) if witness else self.history.unlink
        decision, errors, polls = self.idle(change, fallback)
        self.assertEqual(before, self.f.root.resolve_relative("events.jsonl").read_bytes())
        self.assertIn("presentation_history_unavailable", decision["reason"], errors)
        self.assertNotIn("new message(s)", decision["reason"])
        self.assertEqual([0.1], polls,
            "changed native presentation evidence was hidden by the idle cache until exhaustion")

    def fresh_tail(self, fallback):
        self.held()
        fresh = []
        decision, errors, polls = self.idle(lambda: fresh.append(self.f.send("r2-fresh-tail")), fallback)
        self.assertEqual([0.1], polls)
        self.assertIn(fresh[0]["id"], decision["reason"], errors)
        self.assertIn("1 new message(s)", decision["reason"])
        self.assertEqual([fresh[0]["id"]], self.plane.read(self.node)[-1]["item_ids"])

    def test_native_presentation_append_invalidates(self):
        exercise_watch_paths(self, self.presentation_append)

    def test_native_write_ahead_witness_invalidates(self):
        exercise_watch_paths(self, self.witness_reservation)

    def test_native_missing_history_is_refused_next_poll(self):
        exercise_watch_paths(self, self.changed_history, witness=(False, True))

    def test_native_fresh_mail_remains_reachable_next_poll(self):
        exercise_watch_paths(self, self.fresh_tail)


@contextmanager
def _unpatched():
    yield

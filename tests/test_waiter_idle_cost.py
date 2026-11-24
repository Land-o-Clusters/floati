"""Native controls for event-driven idle waiters; scratch roots only."""
from __future__ import annotations

import io
import json
import os
import select
import stat
import time
import unittest
from unittest import mock

from floati import codex_wait, cursor_wait
from floati.cursor import SparseCursor
from floati.cursor_presentations import CursorPresentationLedger
from floati.events import EventLog
from tests import test_codex_wait as baseline


class ObserveSelection:
    """Observe native admissions without profiling recursive memo walks."""

    def __init__(self, observe):
        # Preserve native dependency discovery through its wrapped seam.
        self._mock_wraps = CursorPresentationLedger._select_waiter
        self.observe = observe

    def __get__(self, instance, owner):
        if instance is None:
            return self
        return lambda *args, **kwargs: self(instance, *args, **kwargs)

    def __call__(self, *args, **kwargs):
        result = self._mock_wraps(*args, **kwargs)
        if isinstance(result, dict):
            self.observe(result)
        return result


class WaiterIdleCostTests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.node = self.f.participant.binding.node_id
        # Literal new hint coordinate is a writer/waiter integration contract,
        # not derived from the implementation helper being tested.
        self.hint = self.f.root.tenant_home / "state/wake-hints" / self.node / "generation"

    def assert_payload_free_hint(self):
        self.assertTrue(self.hint.exists(), "native writer did not notify its recipient")
        info = self.hint.lstat()
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(1, info.st_nlink)
        self.assertEqual(os.geteuid(), info.st_uid)
        self.assertEqual(0o600, stat.S_IMODE(info.st_mode))
        self.assertEqual(b"", self.hint.read_bytes(), "hint retained message or proof payload")
        return info

    def test_native_ack_notifies_after_exact_batch_is_actionable(self):
        """Missing ACK invalidation retains a held selection in an idle waiter."""
        message = self.f.send("hint-ack")
        messages, delivery = EventLog(self.f.root).present(self.node)
        self.assertIn(message["id"], [row["id"] for row in messages])
        self.assertIsNotNone(delivery)
        # Independently seed a subscription to isolate ACK notification.
        self.hint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.hint.touch(mode=0o600, exist_ok=True)
        self.assert_payload_free_hint()
        os.utime(self.hint, ns=(1000000000, 1000000000))
        receipt = SparseCursor(self.f.root).ack(
            self.node, [message["id"]], acting_session_id="hint-ack-session")
        self.assertEqual([message["id"]], receipt["item_ids"])
        self.assertNotEqual(1000000000, self.assert_payload_free_hint().st_mtime_ns)

    def test_native_quiet_wait_blocks_without_periodic_sleep(self):
        """Replacing an event wait with sleep/poll must fail on the actual loop."""
        output, error = io.StringIO(), io.StringIO()
        sleeps = []
        evaluations = []
        native_sleep = time.sleep
        started = time.monotonic()

        def admission_clock():
            # This control measures blocking after admission, not startup or
            # the product's invocation deadline (covered by deadline tests).
            # Once admitted, advance at the real monotonic rate.
            return started + (time.monotonic() - evaluations[0][0] if evaluations else 0.0)

        def observe(result):
            state = "caught_up" if (result.get("fresh_total"), result.get("held_total")) == (0, 0) else "not_caught_up"
            evaluations.append((time.monotonic(), state))

        def recording_sleep(seconds):
            sleeps.append(seconds)
            native_sleep(seconds)

        # Keep the native watch and wall-clock observations. The two-second
        # test budget starts only when the first native admission returns.
        with mock.patch.object(CursorPresentationLedger, "_select_waiter", ObserveSelection(observe)),\
                mock.patch.object(time, "sleep", recording_sleep):
            status = codex_wait.run_stop_waiter(
                bus_home=self.f.bus_home,
                hook_payload={"cwd": str(self.f.workspace), "session_id": "hint-idle"},
                stdout=output, stderr=error, monotonic=admission_clock,
                sleep=recording_sleep)
        self.assertEqual(0, status, error.getvalue())
        decision = json.loads(output.getvalue())
        self.assertEqual("(floati: wait deadline exhausted; end this turn to re-arm)",
                         decision["reason"], error.getvalue())
        self.assertNotIn("using bounded polling", error.getvalue(),
                         "this native event-source control ran in degraded fallback")
        self.assertGreaterEqual(len(evaluations), 2, "initial and terminal native reads did not both run")
        self.assertEqual("caught_up", evaluations[0][1], "initial native admission was not empty")
        self.quiet_observation = {
            "startup_seconds": evaluations[0][0] - started,
            "quiet_seconds": evaluations[-1][0] - evaluations[0][0],
        }
        self.assertGreaterEqual(self.quiet_observation["quiet_seconds"], 1.0,
                                "startup %.6fs; quiet %.6fs; no full quiet interval was observed"
                                % (self.quiet_observation["startup_seconds"],
                                   self.quiet_observation["quiet_seconds"]))
        self.assertEqual([], sleeps, "quiet native waiter still wakes on periodic sleep: " + repr(sleeps))

    def test_native_cursor_quiet_wait_blocks_without_periodic_sleep(self):
        """The separate Cursor loop must not retain standing polling load."""
        session = "cursor-idle-cost"
        self.f.arm_session(session)
        output, error = io.StringIO(), io.StringIO()
        sleeps, selections = [], []
        native_sleep = time.sleep
        started = time.monotonic()

        def admission_clock():
            return started + (time.monotonic() - selections[0][0] if selections else 0.0)

        def observe(result):
            selections.append((time.monotonic(), result["fresh_total"], result["held_total"]))

        def recording_sleep(seconds):
            sleeps.append(seconds)
            native_sleep(seconds)

        with mock.patch.object(CursorPresentationLedger, "_select_waiter", ObserveSelection(observe)),\
                mock.patch.object(time, "sleep", recording_sleep):
            # Keep the native reader/watch and real quiet-time observations.
            # The quiet budget starts when native admission returns.
            with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.f.workspace):
                status = cursor_wait.run_cursor_stop_wait(
                    root=self.f.root.path, node=self.node, runtime=self.f.base,
                    deadline_seconds=2, poll_seconds=1, hook_timeout_seconds=10,
                    payload={"status": "completed", "conversation_id": session, "loop_count": 0},
                    stdout=output, stderr=error, last_seen_root=self.f.workspace,
                    clock=admission_clock, sleep=recording_sleep)
        self.assertEqual(0, status, error.getvalue())
        self.assertIn("followup_message", json.loads(output.getvalue()))
        journal = self.f.root.tenant_home / "state/cursor-wait" / self.node / "journal.jsonl"
        rows = [json.loads(line) for line in journal.read_text().splitlines()]
        self.assertEqual("followup_rearm", rows[-1]["event"], error.getvalue())
        self.assertGreaterEqual(len(selections), 2, "initial and terminal native Cursor reads did not run")
        self.assertEqual((0, 0), selections[0][1:])
        self.assertGreaterEqual(selections[-1][0] - selections[0][0], 1.0,
                                "startup consumed the quiet interval")
        self.assertEqual([], sleeps, "quiet native Cursor waiter still polls: " + repr(sleeps))


class WaiterBackendObservationTests(unittest.TestCase):
    """The observer sees the backend actually selected, with bounded freshness."""

    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.node = self.f.participant.binding.node_id

    def test_actual_wait_backend_is_observed_and_unchanged_waits_do_not_rewrite(self):
        from datetime import datetime, timedelta, timezone
        from floati.wake_control import _digest

        path = self.f.root.tenant_home / "state/waiter-observations" / (self.node + ".json")
        for backend in ("blocking", "polling", "failed-source"):
            with self.subTest(backend=backend):
                watch = codex_wait._WaiterInputsWatch(
                    self.f.participant, _digest("backend-observation"), io.StringIO(),
                    workspace=self.f.workspace)
                self.addCleanup(watch.close)
                self.assertIsNotNone(watch.source)
                if backend == "polling":
                    watch.close()
                expected = "blocking" if backend == "blocking" else "polling"
                observations = []

                def observe():
                    self.assertTrue(path.is_file(), "selected wait backend has no observer testimony")
                    document = json.loads(path.read_text())
                    self.assertEqual(expected, document["mode"])
                    self.assertEqual(self.f.root.tenant_id, document["tenant_id"])
                    self.assertEqual(self.node, document["node_id"])
                    from floati.waiter_state import project_waiter_mode
                    now = datetime.now(timezone.utc)
                    cell = project_waiter_mode(self.f.root, self.node, now)
                    self.assertEqual("MEASURED", cell["stamp"])
                    self.assertEqual(expected, cell["value"])
                    self.assertGreaterEqual(cell["age_seconds"], 0)
                    expires = datetime.fromisoformat(document["valid_until"].replace("Z", "+00:00"))
                    expired = project_waiter_mode(self.f.root, self.node, expires + timedelta(seconds=1))
                    self.assertEqual("UNKNOWN", expired["stamp"])
                    self.assertIsNone(expired["value"])
                    self.assertEqual("waiter_observation_expired", expired["reason"])
                    observations.append((path.stat().st_mtime_ns, document))

                def ready(source, timeout):
                    if backend == "failed-source":
                        raise OSError("controlled event source loss")
                    observe()
                    return False

                for _ in range(2):
                    watch.wait(60, wait_readable=ready, sleep=lambda seconds: observe(), poll_seconds=1)
                self.assertEqual(2, len(observations))
                self.assertEqual(observations[0], observations[1],
                                 "unchanged waits amplified observer writes")
                watch.close()

    def test_unavailable_or_foreign_backend_observation_is_named_unknown(self):
        from datetime import datetime, timezone
        from floati.waiter_state import project_waiter_mode

        now = datetime.now(timezone.utc)
        absent = project_waiter_mode(self.f.root, self.node, now)
        self.assertEqual("UNKNOWN", absent["stamp"])
        self.assertEqual("waiter_observation_absent", absent["reason"])
        path = self.f.root.tenant_home / "state/waiter-observations" / (self.node + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "tenant_id": "foreign",
            "node_id": self.node, "mode": "blocking", "observed_at": now.isoformat(),
            "valid_until": now.isoformat()}))
        cell = project_waiter_mode(self.f.root, self.node, now)
        self.assertEqual("UNKNOWN", cell["stamp"])
        self.assertIsNone(cell["value"])
        self.assertEqual("waiter_observation_invalid", cell["reason"])


class WaiterHintRaceTests(unittest.TestCase):
    """Mail and a broken hint still reach the seat's own read."""

    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.node = self.f.participant.binding.node_id
        self.hint = self.f.root.tenant_home / "state/wake-hints" / self.node / "generation"

    def _codex(self, session, stdout, stderr, **kwargs):
        return codex_wait.run_stop_waiter(
            bus_home=self.f.bus_home,
            hook_payload={"cwd": str(self.f.workspace), "session_id": session},
            stdout=stdout, stderr=stderr, **kwargs)

    def unexpected_wait(self, *args):
        self.fail("already durable mail waited for a later hint or periodic sleep")

    def test_mail_sent_before_registration_is_admitted(self):
        session = "race-before"
        self.f.arm_session(session)
        message = self.f.send("race-before")
        stdout, stderr = io.StringIO(), io.StringIO()
        # Admission cost is not hint latency. Durable mail must be selected
        # without ever entering readiness or a periodic sleeper.
        status = self._codex(session, stdout, stderr, monotonic=lambda: 0.0,
            sleep=self.unexpected_wait, wait_readable=self.unexpected_wait)
        self.assertEqual(0, status, stderr.getvalue())
        decision = json.loads(stdout.getvalue())
        self.assertEqual("block", decision["decision"])
        self.assertIn(message["id"], decision["reason"])

    def test_mail_appended_during_evaluation_is_admitted(self):
        session = "race-during"
        self.f.arm_session(session)
        sent = []
        original = codex_wait._WaiterInputsWatch.changed

        def changed(watch):
            result = original(watch)
            if not sent:
                sent.append(self.f.send("race-during"))
            return result

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(codex_wait._WaiterInputsWatch, "changed", changed):
            status = self._codex(session, stdout, stderr, monotonic=lambda: 0.0,
                sleep=self.unexpected_wait, wait_readable=self.unexpected_wait)
        self.assertEqual(0, status, stderr.getvalue())
        self.assertTrue(sent, "evaluation never ran")
        decision = json.loads(stdout.getvalue())
        self.assertIn(sent[0]["id"], decision["reason"])

    def test_mail_appended_after_readiness_drain_is_admitted(self):
        session = "race-after-drain"
        self.f.arm_session(session)
        sent = []

        def readiness(source, timeout):
            self.assertFalse(sent, "durable mail needed another readiness wait")
            # Schedule the real append in the race between the last drain
            # and blocking. No startup event can mask the writer's signal.
            source.drain()
            sent.append(self.f.send("race-after-drain"))
            self.assertTrue(codex_wait.native_wait_readable(source, 0),
                "mail appended after the drain did not make the native source ready")
            return True

        stdout, stderr = io.StringIO(), io.StringIO()
        status = self._codex(session, stdout, stderr, monotonic=lambda: 0.0,
            sleep=self.unexpected_wait, wait_readable=readiness)
        self.assertEqual(0, status, stderr.getvalue())
        self.assertTrue(sent)
        decision = json.loads(stdout.getvalue())
        self.assertIn(sent[0]["id"], decision["reason"])

    def test_deleted_or_replaced_hint_is_registered_again(self):
        import select
        from floati.wake_control import _digest
        session = "race-rebind"
        self.f.arm_session(session)
        self.hint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.hint.write_bytes(b"")
        os.chmod(self.hint, 0o600)
        watch = codex_wait._WaiterInputsWatch(
            self.f.participant, _digest(session), io.StringIO(), workspace=self.f.workspace)
        self.addCleanup(watch.close)
        self.assertIsNotNone(watch.source, "hint watch fell back to polling")
        self.hint.unlink()
        from tests.readiness import wait_readable
        wait_readable(watch.source, "deleting the hint file did not wake its watch")
        watch.changed()
        descriptor = os.open(
            self.hint, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        # A filesystem may reuse the unlinked inode immediately. Discovery of
        # the recreated hint and its later writes must not depend on that.
        wait_readable(watch.source, "replaced hint file was not registered again")
        watch.changed()
        os.utime(self.hint, None)
        wait_readable(watch.source, "a later write on the replaced hint was invisible")

    def test_atomically_replaced_hint_is_registered_again(self):
        """Replacement must not leave the waiter bound to the original file."""
        from floati.wake_control import _digest
        session = "race-atomic-rebind"
        self.f.arm_session(session)
        self.hint.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.hint.write_bytes(b"")
        os.chmod(self.hint, 0o600)
        watch = codex_wait._WaiterInputsWatch(
            self.f.participant, _digest(session), io.StringIO(), workspace=self.f.workspace)
        self.addCleanup(watch.close)
        self.assertIsNotNone(watch.source, "hint watch fell back to polling")

        # Both files exist simultaneously, so this replacement cannot depend
        # on whether the filesystem recycles an inode after unlink.
        replacement = self.hint.with_name("replacement")
        descriptor = os.open(
            replacement, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        original, following = self.hint.stat(), replacement.stat()
        self.assertNotEqual((original.st_dev, original.st_ino),
                            (following.st_dev, following.st_ino))
        os.utime(replacement, ns=(1000000000, 1000000000))
        os.replace(replacement, self.hint)
        from tests.readiness import wait_readable
        wait_readable(watch.source, "atomically replacing the hint did not wake its watch")
        watch.changed()
        self.assertIsNotNone(watch.source, "replacement lost the native hint watch")
        os.utime(self.hint, ns=(2000000000, 2000000000))
        wait_readable(watch.source, "a later write on the atomically replaced hint was invisible")

    def test_lost_notification_is_discovered_by_the_safety_reread(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger
        CodexWaitConsentLedger(self.f.root).arm(
            self.f.participant.binding,
            hook_timeout_seconds=90,
            wait_deadline_seconds=70,
            idempotency_key="race-safety-consent",
        )
        clock = {"now": 0.0}
        recorded = []
        sent = []

        def monotonic():
            return clock["now"]

        def sleeper(seconds):
            clock["now"] += float(seconds)

        def lost(_source, timeout):
            recorded.append(float(timeout))
            if not sent:
                sent.append(self.f.send("race-lost"))
            sleeper(timeout)
            return False

        session = "race-lost"
        self.f.arm_session(session)
        stdout, stderr = io.StringIO(), io.StringIO()
        status = self._codex(
            session, stdout, stderr, monotonic=monotonic, sleep=sleeper, wait_readable=lost)
        self.assertEqual(0, status, stderr.getvalue())
        self.assertTrue(recorded, "the idle wait never asked the event source")
        self.assertGreater(min(recorded), 30.0, "lost mail was polled instead of the safety re-check")
        self.assertLessEqual(min(recorded), 60.0, "safety re-check exceeded its bound")
        self.assertLess(clock["now"], 70.0, "the safety re-check consumed the whole deadline")
        decision = json.loads(stdout.getvalue())
        self.assertTrue(sent)
        self.assertIn(sent[0]["id"], decision["reason"])
        self.assertGreater(clock["now"], 2.0, "lost mail was reported as a fast wake")

    def test_cursor_lost_notification_is_discovered_by_the_safety_reread(self):
        from floati.codex_wait_contract import CodexWaitConsentLedger
        CodexWaitConsentLedger(self.f.root).arm(
            self.f.participant.binding,
            hook_timeout_seconds=90,
            wait_deadline_seconds=70,
            idempotency_key="race-safety-cursor-consent",
        )
        clock = {"now": 0.0}
        recorded = []
        sent = []

        def cursor_time():
            return clock["now"]

        def cursor_sleep(seconds):
            clock["now"] += float(seconds)

        def lost(_source, timeout):
            recorded.append(float(timeout))
            if not sent:
                sent.append(self.f.send("race-lost-cursor"))
            cursor_sleep(timeout)
            return False

        session = "race-lost-cursor"
        self.f.arm_session(session)
        output, error = io.StringIO(), io.StringIO()
        with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.f.workspace):
            status = cursor_wait.run_cursor_stop_wait(
                root=self.f.root.path, node=self.node, runtime=self.f.base,
                deadline_seconds=70, poll_seconds=1, hook_timeout_seconds=90,
                payload={"status": "completed", "conversation_id": session, "loop_count": 0},
                stdout=output, stderr=error, last_seen_root=self.f.workspace,
                clock=cursor_time, sleep=cursor_sleep, wait_readable=lost)
        self.assertEqual(0, status, error.getvalue())
        self.assertTrue(recorded, "Cursor idle wait never asked the event source")
        self.assertGreater(min(recorded), 30.0,
                           "Cursor lost mail was polled instead of the safety re-check")
        self.assertLessEqual(min(recorded), 60.0, "Cursor safety re-check exceeded its bound")
        body = json.loads(output.getvalue())
        self.assertTrue(sent)
        self.assertIn(sent[0]["id"], body.get("followup_message", ""))
        self.assertGreater(clock["now"], 2.0, "Cursor lost mail was reported as a fast wake")

    def test_safety_timeout_does_not_reread_an_unchanged_ledger(self):
        """A quiet safety timeout is an identity check, not another projection."""
        from floati.codex_wait_contract import CodexWaitConsentLedger

        for harness in ("codex", "cursor"):
            with self.subTest(harness=harness):
                # Independent native state for each harness row.
                f = baseline.CodexWaitRuntimeTests()
                self.addCleanup(f.doCleanups)
                f.setUp()
                node = f.participant.binding.node_id
                CodexWaitConsentLedger(f.root).arm(f.participant.binding,
                    hook_timeout_seconds=90, wait_deadline_seconds=70,
                    idempotency_key="race-quiet-safety-consent")
                clock, recorded, selections = [0.0], [], []

                def sleeper(seconds):
                    clock[0] += float(seconds)

                def quiet(source, timeout):
                    if select.select([source], [], [], 0)[0]:
                        return True
                    recorded.append(float(timeout))
                    sleeper(timeout)
                    return False

                session = "race-quiet-safety-" + harness
                f.arm_session(session)
                output, error = io.StringIO(), io.StringIO()
                observer = ObserveSelection(lambda result: selections.append(clock[0]))
                with mock.patch.object(CursorPresentationLedger, "_select_waiter", observer):
                    if harness == "codex":
                        status = codex_wait.run_stop_waiter(bus_home=f.bus_home,
                            hook_payload={"cwd": str(f.workspace), "session_id": session},
                            stdout=output, stderr=error, monotonic=lambda: clock[0],
                            sleep=sleeper, wait_readable=quiet)
                    else:
                        with mock.patch("floati.cursor_wait.Path.cwd", return_value=f.workspace):
                            status = cursor_wait.run_cursor_stop_wait(
                                root=f.root.path, node=node, runtime=f.base,
                                deadline_seconds=70, poll_seconds=1, hook_timeout_seconds=90,
                                payload={"status": "completed", "conversation_id": session,
                                    "loop_count": 0}, stdout=output, stderr=error,
                                last_seen_root=f.workspace, clock=lambda: clock[0],
                                sleep=sleeper, wait_readable=quiet)
                self.assertEqual(0, status, error.getvalue())
                self.assertTrue(recorded, "the idle wait never reached the safety timeout")
                self.assertTrue(any(timeout > 30.0 for timeout in recorded),
                                "the quiet wait was not the safety re-check")
                self.assertEqual([], [stamp for stamp in selections if 0 < stamp < 70],
                                 "safety timeout reread an unchanged ledger")
                self.assertTrue(selections, "the waiter never admitted the ledger")
                self.assertGreaterEqual(selections[-1], 70, "the deadline admission did not run")
                self.assertEqual(70, clock[0])
                self.assertIn("re-arm" if harness == "codex" else "followup_message",
                              output.getvalue())


class WaiterVerdictInputTests(unittest.TestCase):
    """Every selected native input must invalidate an idle admission by safety."""

    def prepared_inputs(self, harness):
        from floati.codex_wait_contract import (
            CodexWaitConsentLedger, CodexWaitSessionLedger, WORKSPACE_MAP_RELATIVE,
        )

        f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.arm_codex_role_node("verdict-input")
        node = f.participant.binding.node_id
        session = "verdict-input-" + harness
        CodexWaitConsentLedger(f.root).arm(f.participant.binding,
            hook_timeout_seconds=90, wait_deadline_seconds=70,
            idempotency_key="verdict-input-consent")
        f.arm_session(session)
        consent = CodexWaitConsentLedger(f.root).require_armed(f.participant.binding)
        classification = CodexWaitSessionLedger(f.root).participate_classified(
            f.participant.binding, consent, session)
        CursorPresentationLedger(f.root).select(f.participant.binding, consent,
            classification, harness=harness)
        # Enumerated from native selection, authority, closure and maintenance
        # readers, not from the watch's list. Pre-existing leaves ensure a
        # missing leaf watch cannot hide behind parent-directory creation.
        coordinates = {
            "events": "events.jsonl", "registry": "registry/entries.jsonl",
            "mapping": str(WORKSPACE_MAP_RELATIVE),
            **{plane: "receipts/" + plane + "/" + node + ".jsonl"
               for plane in ("deliveries", "acks", "codex-wait-consent",
                   "codex-wait-session", "cursor-presentations", "wake-control",
                   "seat-teardown", "stop-holds")},
            "witness": "state/cursor-wait/" + node + "/presentation-history.v1.json",
            "holder": "state/codex-wait/" + node + "/holder.json",
            "policy": "state/ledger-policy.json",
        }
        for key, relative in coordinates.items():
            if key == "witness":
                continue  # Native selection materializes this before blocking.
            path = f.root.tenant_home / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                if key == "policy":
                    path.write_text('{"schema_version":0,"max_bytes":8388608,"max_age_days":7}\n')
                else:
                    path.write_bytes(b"{}\n" if key == "holder" else b"")
        return f, session, consent, coordinates

    def check_changed_input(self, axis, harness):
        from floati.codex_wait_contract import CodexWaitConsentLedger
        from floati.wake_control import WakeController
        from floati.wake_hold import WakeHoldController

        f, session, consent, coordinates = self.prepared_inputs(harness)
        node = f.participant.binding.node_id
        clock, changed, observations, waits = [0.0], [], [], []
        original_domain = WakeHoldController._EVENT_DOMAIN

        def sleeper(seconds):
            clock[0] += seconds

        def lost(source, timeout):
            import select
            waits.append(timeout)
            # Let the loop reconcile its own startup/witness writes before
            # changing an input. Otherwise an unrelated pending startup event
            # could mask a missing subscription.
            if len(waits) == 1:
                source.drain()
                return True
            if not changed:
                # Reconciliation can itself produce native watch events.
                # Discard them before the controlled change rather than
                # returning readiness forever with a frozen fixture clock.
                source.drain()
                changed.append(clock[0])
                if axis == "pause":
                    WakeController(f.root).pause(node, session,
                        idempotency_key="verdict-input-pause")
                elif axis == "consent-authority":
                    CodexWaitConsentLedger(f.root).arm(f.participant.binding,
                        hook_timeout_seconds=91, wait_deadline_seconds=70,
                        idempotency_key="verdict-input-new-consent")
                elif axis == "semantic-domain":
                    WakeHoldController._EVENT_DOMAIN = original_domain + "-during-hold"
                else:
                    path = f.root.tenant_home / coordinates[axis]
                    self.assertTrue(path.exists(), str(path))
                    info = path.stat()
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
                source.drain()  # Deliberately lose the notification.
            sleeper(timeout)
            return False

        output, errors = io.StringIO(), io.StringIO()
        observer_patch = mock.patch.object(CursorPresentationLedger, "_select_waiter",
            ObserveSelection(lambda result: observations.append(clock[0])))
        observer_patch.start()
        try:
            if harness == "codex":
                status = codex_wait.run_stop_waiter(bus_home=f.bus_home,
                    hook_payload={"cwd": str(f.workspace), "session_id": session},
                    stdout=output, stderr=errors, monotonic=lambda: clock[0],
                    sleep=sleeper, wait_readable=lost)
            else:
                with mock.patch("floati.cursor_wait.Path.cwd", return_value=f.workspace):
                    status = cursor_wait.run_cursor_stop_wait(root=f.root.path,
                        node=node, runtime=f.base, deadline_seconds=70, poll_seconds=1,
                        hook_timeout_seconds=90, payload={"status": "completed",
                            "conversation_id": session, "loop_count": 0},
                        stdout=output, stderr=errors, last_seen_root=f.workspace,
                        clock=lambda: clock[0], sleep=sleeper, wait_readable=lost)
        finally:
            observer_patch.stop()
            WakeHoldController._EVENT_DOMAIN = original_domain
        self.assertEqual(0, status, errors.getvalue())
        self.assertTrue(changed, errors.getvalue() + output.getvalue())
        self.assertTrue(waits)
        self.assertLessEqual(max(waits), 60)
        if axis == "pause" or (axis == "consent-authority" and harness == "codex"):
            self.assertLessEqual(clock[0] - changed[0], 60,
                "changed authority slept past its safety bound")
            self.assertNotIn("re-arm", output.getvalue())
        else:
            self.assertTrue(any(changed[0] < stamp <= changed[0] + 60
                                for stamp in observations),
                axis + " was not observed by the next safety boundary: " + repr(observations))

    def test_every_verdict_input_changed_during_hold_is_observed_within_safety_bound(self):
        from floati.wake_control import _digest

        # One native fixture holds the independent leaf table. Drain notifications
        # so dirty must reflect the changed input even when its event was lost.
        f, session, _, coordinates = self.prepared_inputs("codex")
        watch = codex_wait._WaiterInputsWatch(
            f.participant, _digest(session), io.StringIO(), workspace=f.workspace)
        try:
            self.assertIsNotNone(watch.source, "leaf control fell back to polling")
            watch.source.drain()
            self.assertEqual((False, False), watch.changed(), "leaf baseline was not clean")
            for axis, relative in coordinates.items():
                with self.subTest(leaf=axis):
                    path = f.root.tenant_home / relative
                    self.assertTrue(path.exists(), str(path))
                    info = path.stat()
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
                    watch.source.drain()
                    self.assertTrue(watch.changed()[1], axis + " did not invalidate the watch")
                    watch.source.drain()
                    self.assertEqual((False, False), watch.changed(),
                                     axis + " did not return to a clean baseline")
        finally:
            watch.close()

        for harness in ("codex", "cursor"):
            for axis in ("pause", "consent-authority", "semantic-domain", "events"):
                with self.subTest(harness=harness, axis=axis):
                    self.check_changed_input(axis, harness)


if __name__ == "__main__":
    unittest.main()

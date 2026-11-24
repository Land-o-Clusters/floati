"""Native Stop waiters recover transient ledger contention within one window."""

from tests.readiness import wait_event, wait_join, wait_until
from contextlib import contextmanager
import fcntl
import io
import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.events import EventLog
from floati.records import WAKE_HOLD_KINDS
from floati.root import FloatiRoot
from floati.registry import Registry
from floati.stop_hold import StopHoldLedger
from floati.waiter_consent import arm_waiter_consent
from floati.jsonl import read_records_snapshot
from tests.temp_roots import REAL_TEMP_ROOT
from tests.waiter_timing import scripted_readiness


class WaiterLockRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        Registry(self.root).register("seat", "Codex")
        Registry(self.root).register("sender", "worker")
        arm_waiter_consent(self.root.path, self.workspace, "seat", "Codex", 10, 3)
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        CodexWaitSessionLedger(self.root).arm(participant.binding, consent, "session",
                                            idempotency_key="native-session")
        self.output = io.StringIO()
        self.errors = io.StringIO()

    @contextmanager
    def lock_clock(self, on_sleep=None):
        """Advance product acquisition time only after real native contention."""
        from types import SimpleNamespace
        from floati import jsonl

        now, sleeps = [0.0], []
        def clock():
            return now[0]
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
            if on_sleep is not None:
                on_sleep(now, seconds)
        with mock.patch.object(jsonl, "time", SimpleNamespace(monotonic=clock, sleep=sleep)):
            yield SimpleNamespace(clock=clock, sleep=sleep, sleeps=sleeps)

    def assert_lock_held(self, relative):
        with self.root.resolve_relative(relative).open("rb") as handle:
            with self.assertRaises(BlockingIOError, msg="native fixture lock must remain held"):
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def held_lock(self, relative, seconds=None, *, unlocked=None):
        """Hold until release; timed contention begins at a real failed probe."""
        path = self.root.resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        ready = threading.Event()
        release = threading.Event()
        contended = threading.Event()
        identity = []
        original_flock = fcntl.flock

        def observe_contention(descriptor, operation):
            try:
                return original_flock(descriptor, operation)
            except BlockingIOError:
                state = os.fstat(descriptor)
                if identity == [(state.st_dev, state.st_ino)]:
                    contended.set()
                raise

        observer = mock.patch.object(fcntl, 'flock', observe_contention)
        observer.start()
        self.addCleanup(observer.stop)

        def hold():
            with path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                state = os.fstat(handle.fileno())
                identity.append((state.st_dev, state.st_ino))
                ready.set()
                if seconds is None:
                    wait_event(release, 'native scratch lock release')
                else:
                    wait_until(lambda: release.is_set() or contended.is_set(),
                               'native scratch lock first contended probe')
                    # This duration is the product contention stimulus. It
                    # starts after acquisition is attempted, not at startup.
                    release.wait(seconds)
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                if unlocked is not None:
                    unlocked.set()
        child = threading.Thread(target=hold)
        child.start()
        self.addCleanup(wait_join, child, 'native scratch lock holder cleanup')
        self.addCleanup(release.set)
        self.assertTrue(wait_event(ready, "test_waiter_lock_retry ready"), "native scratch lock must be held")
        return release

    def invoke(self, **kwargs):
        if "wait_readable" not in kwargs:
            sleeper = kwargs.get("sleep")
            window = kwargs.get("_waiter_window")
            if sleeper is None and window is not None:
                sleeper = window.sleep
            if sleeper is not None:
                kwargs["wait_readable"] = scripted_readiness(sleeper, interval=0.02)
        return run_stop_waiter(bus_home=self.root.path,
            hook_payload={"cwd": str(self.workspace), "session_id": "session"},
            stdout=self.output, stderr=self.errors, poll_interval_seconds=0.02, **kwargs)

    def cursor_at(self, **arguments):
        """Bind the native fixture's process cwd; payload cwd has no runtime authority."""
        from contextlib import nullcontext
        from unittest.mock import patch
        from floati.cursor_wait import run_cursor_stop_wait
        if "sleep" in arguments and "wait_readable" not in arguments:
            arguments["wait_readable"] = scripted_readiness(
                arguments["sleep"], interval=arguments.get("poll_seconds", 0.02))
        fixture_cwd = arguments["payload"].get("cwd")
        scope = (patch("floati.cursor_wait.Path.cwd", return_value=Path(fixture_cwd))
                 if fixture_cwd is not None else nullcontext())
        with scope:
            return run_cursor_stop_wait(**arguments)

    def test_native_event_lock_release_recovers_mail_in_same_hold(self):
        from types import SimpleNamespace
        from floati import jsonl
        from floati.waiter_lock_retry import WaiterLockWindow

        envelope = EventLog(self.root).send("sender", "seat", "repo", "a" * 40,
                                            "docs/proof.md", "native mail")
        unlocked = threading.Event()
        release = self.held_lock("events.jsonl.lock", unlocked=unlocked)
        now, retries = [0.0], []
        def clock():
            return now[0]
        def sleep(seconds):
            now[0] += seconds
        native_retry = WaiterLockWindow.retry
        def release_after_timeout(window, failure, *args, **kwargs):
            retries.append(failure.code)
            release.set()
            wait_event(unlocked, "native event lock released after failed acquisition")
            return native_retry(window, failure, *args, **kwargs)
        # The real flock must time out before its holder releases. Virtual
        # acquisition time separates that property from host descheduling;
        # the holder's native unlock event gates the real retry and delivery.
        with mock.patch.object(jsonl, "time", SimpleNamespace(monotonic=clock, sleep=sleep)),\
                mock.patch.object(WaiterLockWindow, "retry", release_after_timeout):
            self.assertEqual(0, self.invoke(monotonic=clock, sleep=sleep))
        self.assertEqual(["ledger_lock_timeout"], retries)
        lines = self.output.getvalue().splitlines()
        self.assertEqual(1, len(lines), self.errors.getvalue())
        self.assertIn(envelope["id"], json.loads(lines[0])["reason"])
        self.assertNotIn("integrity", self.errors.getvalue())
        self.assertEqual(["start", "exit"],
                         [row["phase"] for row in StopHoldLedger(self.root).read("seat")])

    def test_sustained_event_contention_exhausts_without_integrity_or_late_mail(self):
        from unittest.mock import patch
        from floati import codex_wait
        from floati.stop_hold import _HoldEvidence
        from floati.waiter_lock_retry import current_lock_window, lock_deadline_failure

        native_start = _HoldEvidence.start
        native_decline = codex_wait._integrity_exit
        for phase in ("before_hold", "after_hold"):
            with self.subTest(phase=phase):
                if phase == "after_hold":
                    self.setUp()
                try:
                    envelope = EventLog(self.root).send("sender", "seat", "repo", "a" * 40,
                                                        "docs/proof.md", "waiting mail")
                    participant = resolve_participant(self.root.path, self.workspace)
                    consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
                    deadline_seconds = consent["wait_deadline_seconds"]
                    starts, declines, releases = [], [], []

                    def start_then_contend(evidence, *arguments, **keywords):
                        observed = {}
                        starts.append(observed)
                        result = native_start(evidence, *arguments, **keywords)
                        observed["start"] = evidence.start_row
                        persisted = StopHoldLedger(self.root).read("seat")
                        observed["persisted"] = persisted
                        self.assertEqual([evidence.start_row], persisted)
                        if phase == "after_hold":
                            releases.append(self.held_lock("events.jsonl.lock"))
                        return result

                    def observe_decline(*arguments, **keywords):
                        window = current_lock_window()
                        self.assertIsNotNone(window)
                        self.assertEqual(deadline_seconds, keywords["deadline_seconds"])
                        self.assertAlmostEqual(window.started + deadline_seconds, window.deadline)
                        self.assertGreaterEqual(window.clock(), window.deadline,
                            "contention must exhaust the product's original consent deadline")
                        declines.append(keywords)
                        return native_decline(*arguments, **keywords)

                    if phase == "before_hold":
                        releases.append(self.held_lock("events.jsonl.lock"))
                    with patch.object(_HoldEvidence, "start", start_then_contend),\
                            patch.object(codex_wait, "_integrity_exit", observe_decline):
                        self.assertEqual(0, self.invoke())
                    # Model the observer being descheduled after the waiter
                    # returns. Scheduling latency cannot decide expiry.
                    time.sleep(0.2)
                    self.assertEqual(1, len(releases))
                    self.assertFalse(releases[0].is_set(),
                        "the waiter must finish before the contended lock is released")
                    with self.root.resolve_relative("events.jsonl.lock").open("a+b") as handle:
                        with self.assertRaises(BlockingIOError,
                                msg="the native event lock must still be held at waiter completion"):
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    reason = json.loads(self.output.getvalue())["reason"]
                    self.assertIn("deadline exhausted", reason)
                    self.assertIn("contended", reason)
                    self.assertNotIn(envelope["id"], reason)
                    self.assertNotIn("integrity", reason)
                    self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/seat.jsonl",
                                                              allowed_kinds=set(WAKE_HOLD_KINDS)))
                    self.assertEqual(1, len(declines))
                    self.assertEqual("epoch_maintenance", declines[0]["operation"])
                    self.assertIsNotNone(lock_deadline_failure(declines[0]["failure"]))
                    exits = read_records_snapshot(self.root, "receipts/wake-waiter-exit/seat.jsonl",
                                                  allowed_kinds={"wake_waiter_exit_receipt"})
                    self.assertEqual(["exhausted"], [row["reason_code"] for row in exits])
                    self.assertEqual(declines[0]["invocation_id"] + "-exit-exhausted",
                                     exits[0]["idempotency_key"])
                    holds = StopHoldLedger(self.root).read("seat")
                    if phase == "before_hold":
                        # Startup maintenance exhausted before hold admission.
                        self.assertEqual([], starts)
                        self.assertEqual([], holds)
                    else:
                        self.assertEqual(1, len(starts))
                        start = starts[0]["start"]
                        self.assertEqual([start], starts[0]["persisted"])
                        self.assertEqual(["start", "exit"], [row["phase"] for row in holds])
                        self.assertEqual(start, holds[0])
                        for key in ("invocation_id", "acting_session_id", "workspace",
                                    "consent_receipt_id", "started_at", "deadline_seconds"):
                            self.assertEqual(start[key], holds[1][key])
                finally:
                    # LIFO joins the real lock thread before its fixture is removed.
                    self.doCleanups()

    def test_short_native_startup_guard_budget_cannot_use_five_second_acquisition(self):
        arm_waiter_consent(self.root.path, self.workspace, "seat", "Codex", 10, 1)
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        CodexWaitSessionLedger(self.root).arm(participant.binding, consent, "session",
                                            idempotency_key="short-consent-session")
        relative = "receipts/wake-coordination/seat/lane.lock"
        self.held_lock(relative)
        with self.lock_clock() as timing:
            self.assertEqual(0, self.invoke(monotonic=timing.clock, sleep=timing.sleep))
            self.assertAlmostEqual(1, timing.clock())
        self.assert_lock_held(relative)
        self.assertIn("deadline exhausted", json.loads(self.output.getvalue())["reason"])
        self.assertEqual([], StopHoldLedger(self.root).read("seat"))

    def test_native_hold_start_and_delivery_locks_recover_without_duplicate_rows(self):
        for relative in ("receipts/stop-holds/seat.jsonl.lock",
                         "receipts/deliveries/seat.jsonl.lock",
                         "receipts/acks/seat.jsonl.lock"):
            with self.subTest(relative=relative):
                # Each subcase uses a complete, separate native invocation.
                self.setUp()
                envelope = EventLog(self.root).send("sender", "seat", "repo", "a" * 40,
                                                    "docs/proof.md", "waiting mail")
                if "acks" in relative:
                    path = self.root.resolve_relative("receipts/acks/seat.jsonl")
                    path.parent.mkdir(parents=True)
                    path.write_bytes(b"")
                self.held_lock(relative, 1.35)
                self.assertEqual(0, self.invoke())
                self.assertIn(envelope["id"], json.loads(self.output.getvalue())["reason"])
                holds = StopHoldLedger(self.root).read("seat")
                self.assertEqual(["start", "exit"], [row["phase"] for row in holds])
                deliveries = read_records_snapshot(self.root, "receipts/deliveries/seat.jsonl",
                                                    allowed_kinds=set(WAKE_HOLD_KINDS))
                self.assertEqual(1, len(deliveries))
                self.assertEqual([envelope["id"]], deliveries[0]["item_ids"])

    def test_native_malformed_events_still_emit_integrity_diagnostic(self):
        self.root.resolve_relative("events.jsonl").write_bytes(b"not-json\n")
        self.assertEqual(0, self.invoke())
        self.assertIn("integrity refusal", json.loads(self.output.getvalue())["reason"])

    def test_cursor_native_peek_recovers_after_lock_release(self):
        from floati.cursor_wait import run_cursor_stop_wait
        Registry(self.root).register("cursor-seat", "Cursor")
        self.workspace = self.base / "cursor-workspace"
        self.workspace.mkdir()
        arm_waiter_consent(self.root.path, self.workspace, "cursor-seat", "Cursor", 10, 3)
        envelope = EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
                                            "docs/proof.md", "cursor mail")
        self.held_lock("events.jsonl.lock", 1.35)
        status = self.cursor_at(root=self.root.path, node="cursor-seat", runtime=Path.cwd(),
            deadline_seconds=3, poll_seconds=0.02, hook_timeout_seconds=10,
            payload={"status": "completed", "conversation_id": "cursor-session", "cwd": str(self.workspace)},
            stdout=self.output, stderr=self.errors)
        self.assertEqual(0, status)
        self.assertNotIn("unreadable", self.output.getvalue())
        self.assertIn("followup_message", json.loads(self.output.getvalue()))
        self.assertIn("inbox", json.loads(self.output.getvalue())["followup_message"])
        deliveries = read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
                                            allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertEqual(1, len(deliveries))
        self.assertEqual([envelope["id"]], deliveries[0]["item_ids"])

    def test_delayed_native_start_metadata_retains_original_expiry(self):
        unlocked = threading.Event()
        release = self.held_lock("receipts/wake-coordination/seat/lane.lock", unlocked=unlocked)
        original = datetime.now(timezone.utc)
        delay = 1.25
        delayed = []
        def release_after_contention(now, seconds):
            if not delayed:
                delayed.append(True)
                now[0] = delay
                release.set()
                wait_event(unlocked, "delayed native startup guard unlock")
        with self.lock_clock(release_after_contention) as timing:
            self.assertEqual(0, self.invoke(monotonic=timing.clock, sleep=timing.sleep,
                wall_time=lambda: original.timestamp() + timing.clock()))
        self.assertEqual([True], delayed)
        start = StopHoldLedger(self.root).read("seat")[0]
        stamp = datetime.fromisoformat(start["started_at"].replace("Z", "+00:00"))
        expected = (original + timedelta(seconds=delay)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        self.assertEqual(expected, start["started_at"])
        self.assertAlmostEqual(3 - delay, start["deadline_seconds"])
        self.assertEqual((original + timedelta(seconds=3)).isoformat(timespec="milliseconds"),
                         (stamp + timedelta(seconds=start["deadline_seconds"])).isoformat(timespec="milliseconds"))

    def test_cursor_takeover_commits_during_native_retry_backoff(self):
        from unittest.mock import patch
        from floati.cursor_wait import run_cursor_stop_wait
        from floati.waiter_lock_retry import current_lock_window
        Registry(self.root).register("cursor-seat", "Cursor")
        self.workspace = self.base / "cursor-backoff-workspace"
        self.workspace.mkdir()
        arm_waiter_consent(self.root.path, self.workspace, "cursor-seat", "Cursor", 16, 8)
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        CodexWaitSessionLedger(self.root).arm(participant.binding, consent, "cursor-session",
                                             idempotency_key="cursor-original")
        EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40, "docs/proof.md", "mail")
        self.held_lock("events.jsonl.lock")
        takeovers, backoffs = [], []
        def replace():
            try:
                takeovers.append(CodexWaitSessionLedger(self.root).arm(
                    participant.binding, consent, "replacement-session",
                    idempotency_key="cursor-backoff-takeover", take_over=True))
            except Exception as exc:
                takeovers.append(exc)
        def takeover_during_backoff(now, seconds):
            window = current_lock_window()
            if window is not None and window.contended and seconds == 0.05 and not backoffs:
                backoffs.append(seconds)
                actor = threading.Thread(target=replace)
                actor.start()
                self.addCleanup(wait_join, actor, "cursor takeover cleanup")
                wait_join(actor, "cursor takeover commits with outer guards released")
                self.assertEqual(1, len(takeovers))
                self.assertIsInstance(takeovers[0], dict, takeovers)
                self.assert_lock_held("events.jsonl.lock")
        with patch("floati.cursor_wait.Path.cwd", return_value=self.workspace),\
                self.lock_clock(takeover_during_backoff) as timing:
            self.assertEqual(0, self.cursor_at(root=self.root.path, node="cursor-seat",
                runtime=self.workspace, deadline_seconds=8, poll_seconds=0.02, hook_timeout_seconds=16,
                payload={"status": "completed", "conversation_id": "cursor-session"},
                stdout=self.output, stderr=self.errors, clock=timing.clock, sleep=timing.sleep))
        self.assertEqual([0.05], backoffs)
        self.assertEqual("replacement-session", takeovers[0]["acting_session_id"])
        self.assertEqual({}, json.loads(self.output.getvalue()))
        journal = [json.loads(line) for line in (self.root.path / "state/cursor-wait/cursor-seat/journal.jsonl").read_text().splitlines()]
        self.assertEqual("superseded_session", journal[-1]["reason_code"])
        self.assertEqual("cursor-session", journal[-1]["calling_session_id"])
        self.assertNotIn("publication", journal[-1])
        self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_native_takeover_progresses_while_events_lock_remains_held(self):
        from floati.waiter_lock_retry import WaiterLockWindow

        arm_waiter_consent(self.root.path, self.workspace, "seat", "Codex", 12, 8)
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        CodexWaitSessionLedger(self.root).arm(participant.binding, consent, "session",
                                            idempotency_key="long-session")
        EventLog(self.root).send("sender", "seat", "repo", "a" * 40, "docs/proof.md", "mail")
        self.held_lock("events.jsonl.lock")
        takeover = []
        def replace():
            try:
                row = CodexWaitSessionLedger(self.root).arm(participant.binding, consent, "new-session",
                    idempotency_key="native-takeover", take_over=True)
                takeover.append(row)
            except Exception as failure:
                takeover.append(failure)
        native_retry = WaiterLockWindow.retry
        retries = []
        def retry(window, failure, *args, **kwargs):
            if not retries:
                retries.append(failure.code)
                actor = threading.Thread(target=replace)
                actor.start()
                self.addCleanup(wait_join, actor, "native takeover cleanup")
                wait_join(actor, "native takeover commits while event lock remains held")
                self.assert_lock_held("events.jsonl.lock")
            return native_retry(window, failure, *args, **kwargs)
        with mock.patch.object(WaiterLockWindow, "retry", retry), self.lock_clock() as timing:
            self.assertEqual(0, self.invoke(monotonic=timing.clock, sleep=timing.sleep))
        self.assertEqual(["ledger_lock_timeout"], retries)
        self.assertEqual(1, len(takeover))
        self.assertIsInstance(takeover[0], dict, takeover)
        self.assert_lock_held("events.jsonl.lock")
        self.assertEqual("new-session", takeover[0]["acting_session_id"])
        self.assertEqual({}, json.loads(self.output.getvalue()))
        self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/seat.jsonl",
                                                  allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_cursor_failed_first_output_never_replays_publication(self):
        from floati.bus_epoch import epoch_guard
        from floati.wake_hold import wake_coordination_guard
        for phase in ("write", "flush"):
            with self.subTest(phase=phase):
                self.setUp()
                workspace, _participant = self.cursor_native_fixture()
                envelope = EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
                    "docs/proof.md", "one publication")
                calls = []
                class Output:
                    def write(self, value):
                        calls.append("write")
                        if phase == "write" and calls.count("write") == 1:
                            raise OSError("first write fails; a second would succeed")
                        return len(value)
                    def flush(self):
                        calls.append("flush")
                        if phase == "flush" and calls.count("flush") == 1:
                            raise OSError("first flush fails; a second would succeed")
                output = self.output
                self.output = Output()
                with self.assertRaisesRegex(OSError, "first"):
                    self.invoke_cursor(workspace)
                self.output = output
                self.assertEqual(["write"] if phase == "write" else ["write", "flush"], calls)
                self.assertFalse((self.root.path / "state/cursor-wait/cursor-seat/journal.jsonl").exists())
                deliveries = read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
                    allowed_kinds=set(WAKE_HOLD_KINDS))
                self.assertEqual(1, len(deliveries))
                self.assertEqual([envelope["id"]], deliveries[0]["item_ids"])
                with epoch_guard(self.root, exclusive=True):
                    with wake_coordination_guard(self.root, "cursor-seat"):
                        pass

    def test_cursor_initial_guard_expiry_never_bootstraps_terminal_claim(self):
        for replace_consent in (False, True):
            with self.subTest(replace_consent=replace_consent):
                self.setUp()
                workspace, participant = self.cursor_native_fixture(1)
                EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
                    "docs/proof.md", "mail beyond initial guard")
                relative = "receipts/wake-coordination/cursor-seat/lane.lock"
                self.held_lock(relative)
                replacements = []
                def replace():
                    try:
                        replacements.append(CodexWaitConsentLedger(self.root).arm(
                            participant.binding, hook_timeout_seconds=10, wait_deadline_seconds=2,
                            idempotency_key="guard-contended-consent-replacement"))
                    except Exception as failure:
                        replacements.append(failure)
                def replace_after_contention(now, seconds):
                    if replace_consent and not replacements:
                        actor = threading.Thread(target=replace)
                        actor.start()
                        self.addCleanup(wait_join, actor, "initial guard consent replacement cleanup")
                        wait_join(actor, "initial guard contended consent replacement")
                        self.assertEqual(1, len(replacements))
                        self.assertIsInstance(replacements[0], dict, replacements)
                with self.lock_clock(replace_after_contention) as timing:
                    self.assertEqual(0, self.invoke_cursor(workspace, clock=timing.clock, sleep=timing.sleep))
                    self.assertAlmostEqual(1, timing.clock())
                self.assert_lock_held(relative)
                self.assertEqual(int(replace_consent), len(replacements))
                body = json.loads(self.output.getvalue())
                if replace_consent:
                    self.assertEqual({}, body)
                else:
                    self.assertEqual({}, body)
                    journal = self.root.path / "state/cursor-wait/cursor-seat/journal.jsonl"
                    self.assertEqual("deadline_guard_unavailable",
                        json.loads(journal.read_text().splitlines()[-1])["reason_code"])
                self.assertEqual([], read_records_snapshot(self.root,
                    "receipts/codex-wait-session/cursor-seat.jsonl",
                    allowed_kinds={"codex_wait_session_receipt"}))
                self.assertEqual([], read_records_snapshot(self.root,
                    "receipts/deliveries/cursor-seat.jsonl",
                    allowed_kinds=set(WAKE_HOLD_KINDS)))
                self.assertEqual([], StopHoldLedger(self.root).read("cursor-seat"))

    def test_cursor_deadline_cannot_publish_rearm_while_replacement_guard_is_busy(self):
        from floati.bus_epoch import epoch_guard
        from floati.wake_hold import wake_coordination_guard
        workspace, participant = self.cursor_native_fixture(1)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        ledger = CodexWaitSessionLedger(self.root)
        ledger.arm(participant.binding, consent, "cursor-session", idempotency_key="old-before-deadline")
        EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40, "docs/proof.md", "pending mail")
        ready, release = threading.Event(), threading.Event()
        results = []
        def replacement():
            try:
                with epoch_guard(self.root):
                    with wake_coordination_guard(self.root, "cursor-seat"):
                        ready.set()
                        if not wait_event(release, "test_waiter_lock_retry release"):
                            raise AssertionError("replacement fixture must release its owned guard finitely")
                        results.append(ledger._arm_already_guarded(participant.binding, consent,
                            "replacement-session", idempotency_key="replacement-after-deadline", take_over=True))
            except Exception as exc:
                results.append(exc)
        actor = threading.Thread(target=replacement)
        actor.start()
        self.addCleanup(wait_join, actor, "replacement guard cleanup")
        self.addCleanup(release.set)
        self.assertTrue(wait_event(ready, "test_waiter_lock_retry ready"))
        with self.lock_clock() as timing:
            self.assertEqual(0, self.invoke_cursor(workspace, clock=timing.clock, sleep=timing.sleep))
            self.assertAlmostEqual(1, timing.clock())
        self.assert_lock_held("receipts/wake-coordination/cursor-seat/lane.lock")
        self.assertEqual({}, json.loads(self.output.getvalue()))
        journal = self.root.path / "state/cursor-wait/cursor-seat/journal.jsonl"
        row = json.loads(journal.read_text().splitlines()[-1])
        self.assertEqual("deadline_guard_unavailable", row["reason_code"])
        self.assertNotIn("publication", row)
        self.assertEqual([], results, "replacement owns guard before appending its claim")
        self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS)))
        release.set()
        wait_join(actor, "test_waiter_lock_retry actor")
        self.assertEqual(1, len(results))
        self.assertIsInstance(results[0], dict, results)
        self.assertEqual("replacement-session", results[0]["acting_session_id"])

    def test_native_consent_replacement_during_retry_ends_old_authority(self):
        EventLog(self.root).send("sender", "seat", "repo", "a" * 40, "docs/proof.md", "mail")
        participant = resolve_participant(self.root.path, self.workspace)
        self.held_lock("events.jsonl.lock", 1.35)
        def replace():
            CodexWaitConsentLedger(self.root).arm(participant.binding,
                hook_timeout_seconds=10, wait_deadline_seconds=2,
                idempotency_key="native-replacement")
        actor = threading.Timer(0.2, replace)
        actor.start()
        self.addCleanup(actor.join, 5)
        self.assertEqual(0, self.invoke())
        wait_join(actor, "test_waiter_lock_retry actor")
        self.assertEqual({}, json.loads(self.output.getvalue()))
        exits = read_records_snapshot(self.root, "receipts/wake-waiter-exit/seat.jsonl",
                                      allowed_kinds={"wake_waiter_exit_receipt"})
        self.assertEqual(["consent_withdrawn"], [row["reason_code"] for row in exits])
        self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/seat.jsonl",
                                                  allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_cursor_mid_loop_recovers_mail_without_another_mtime_change(self):
        from floati.cursor_wait import run_cursor_stop_wait
        Registry(self.root).register("cursor-seat", "Cursor")
        self.workspace = self.base / "cursor-workspace"
        self.workspace.mkdir()
        arm_waiter_consent(self.root.path, self.workspace, "cursor-seat", "Cursor", 10, 3)
        sent = []
        def sleep(seconds):
            if not sent:
                sent.append(EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
                                                     "docs/proof.md", "mid-loop mail"))
                self.held_lock("events.jsonl.lock", 1.35)
            time.sleep(seconds)
        self.assertEqual(0, self.cursor_at(root=self.root.path, node="cursor-seat", runtime=Path.cwd(),
            deadline_seconds=3, poll_seconds=0.02, hook_timeout_seconds=10,
            payload={"status": "completed", "conversation_id": "cursor-session", "cwd": str(self.workspace)},
            stdout=self.output, stderr=self.errors, sleep=sleep,
            ledger_mtime=lambda: self.root.resolve_relative("events.jsonl").stat().st_size
                if self.root.resolve_relative("events.jsonl").exists() else 0))
        self.assertIn("inbox", json.loads(self.output.getvalue())["followup_message"])
        deliveries = read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
                                            allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertEqual(1, len(deliveries))
        self.assertEqual([sent[0]["id"]], deliveries[0]["item_ids"])

    def test_cursor_nested_workspace_retry_preserves_resolved_binding(self):
        from floati.cursor_wait import run_cursor_stop_wait
        Registry(self.root).register("cursor-seat", "Cursor")
        workspace = self.base / "cursor-workspace"
        nested = workspace / "nested"
        nested.mkdir(parents=True)
        arm_waiter_consent(self.root.path, workspace, "cursor-seat", "Cursor", 10, 3)
        sent = []
        def sleep(seconds):
            if not sent:
                sent.append(EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
                    "docs/proof.md", "nested mail"))
                self.held_lock("events.jsonl.lock", 1.35)
            time.sleep(seconds)
        self.assertEqual(0, self.cursor_at(root=self.root.path, node="cursor-seat", runtime=Path.cwd(),
            deadline_seconds=3, poll_seconds=0.02, hook_timeout_seconds=10,
            payload={"status": "completed", "conversation_id": "cursor-session", "cwd": str(nested)},
            stdout=self.output, stderr=self.errors, sleep=sleep))
        self.assertIn("followup_message", json.loads(self.output.getvalue()))
        self.assertIn("inbox", json.loads(self.output.getvalue())["followup_message"])
        deliveries = read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertEqual([sent[0]["id"]], deliveries[0]["item_ids"])

    def test_cursor_quiet_start_contention_observes_withdrawn_consent(self):
        from types import SimpleNamespace
        from floati import jsonl
        from floati.waiter_lock_retry import current_lock_window
        Registry(self.root).register("cursor-seat", "Cursor")
        workspace = self.base / "cursor-workspace"
        workspace.mkdir()
        arm_waiter_consent(self.root.path, workspace, "cursor-seat", "Cursor", 10, 3)
        participant = resolve_participant(self.root.path, workspace)
        release = self.held_lock("receipts/stop-holds/cursor-seat.jsonl.lock")
        now, replacements, failures = [100.0], [], []
        def clock():
            return now[0]
        def replace():
            try:
                CodexWaitConsentLedger(self.root).arm(participant.binding,
                    hook_timeout_seconds=10, wait_deadline_seconds=2,
                    idempotency_key="cursor-replacement")
                replacements.append(True)
            except Exception as failure:
                failures.append(failure)
        def sleep(seconds):
            now[0] += seconds
            # The retry backoff runs after the contended operation releases its
            # guards. Withdraw consent before releasing the held ledger lock.
            if seconds == 0.05 and not replacements:
                self.assertTrue(current_lock_window().contended)
                actor = threading.Thread(target=replace)
                actor.start()
                wait_join(actor, "cursor consent replacement")
                self.assertEqual([], failures)
                release.set()
        try:
            with mock.patch.object(jsonl, "time", SimpleNamespace(monotonic=clock, sleep=sleep)):
                self.assertEqual(0, self.invoke_cursor(workspace, clock=clock, sleep=sleep))
        finally:
            release.set()
        self.assertEqual([True], replacements)
        self.assertEqual({}, json.loads(self.output.getvalue()))
        self.assertEqual([], StopHoldLedger(self.root).read("cursor-seat"))

    def test_expired_epoch_contention_uses_injected_residual_without_hot_loop(self):
        from floati.bus_epoch import _acquire_epoch_descriptor
        from floati.errors import ProtocolRefusal
        from floati.waiter_lock_retry import waiter_lock_window
        import os
        descriptor = os.open(self.root.tenant_home, os.O_RDONLY)
        contender = os.open(self.root.tenant_home, os.O_RDONLY)
        self.addCleanup(os.close, descriptor)
        self.addCleanup(os.close, contender)
        fcntl.flock(contender, fcntl.LOCK_EX)
        now, sleeps = [0.0], []
        def sleep(seconds):
            self.assertLess(len(sleeps), 2, "retry sleep count must remain bounded")
            self.assertGreater(seconds, 0, "retry must advance the clock")
            sleeps.append(seconds)
            if len(sleeps) > 5:
                raise AssertionError("epoch poll continued after injected deadline")
            now[0] += seconds
        with waiter_lock_window(0.02, lambda: now[0], sleep):
            with self.assertRaisesRegex(ProtocolRefusal, "epoch_lock_timeout"):
                _acquire_epoch_descriptor(descriptor, exclusive=False)
        self.assertTrue(all(seconds > 0 for seconds in sleeps))
        self.assertAlmostEqual(0.02, sum(sleeps))

    def test_expired_guard_busy_fallback_refuses_native_superseded_claim(self):
        from floati.codex_wait import _integrity_exit
        from floati.waiter_lock_retry import (WaiterAuthorityChanged, WaiterLockDeadline,
            waiter_lock_window)
        import hashlib
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        ledger = CodexWaitSessionLedger(self.root)
        ledger.arm(participant.binding, consent, "new-session", take_over=True,
            idempotency_key="fallback-native-takeover")
        self.held_lock("receipts/wake-coordination/seat/lane.lock", 0.2)
        with waiter_lock_window(0, time.monotonic, time.sleep) as window:
            def check_authority(**kwargs):
                rows = read_records_snapshot(self.root, ledger._relative("seat"), allowed_kinds=ledger._KINDS)
                current = ledger._matching(rows, participant.binding)[-1]
                if current["acting_session_id"] != "session":
                    raise WaiterAuthorityChanged("superseded_session", current["acting_session_id"])
            window.check_authority = check_authority
            self.assertEqual(0, _integrity_exit(participant,
                session_digest=hashlib.sha256(b"session").hexdigest(), operation="wake_evaluation",
                failure=WaiterLockDeadline("native ledger"), waited_seconds=3,
                invocation_id="native-fallback", deadline_seconds=3,
                stdout=self.output, stderr=self.errors))
        self.assertEqual({}, json.loads(self.output.getvalue()))
        exits = read_records_snapshot(self.root, "receipts/wake-waiter-exit/seat.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"})
        self.assertEqual(["superseded_session"], [row["reason_code"] for row in exits])
        self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_published_wake_testimony_survives_subsequent_native_consent_change(self):
        envelope = EventLog(self.root).send("sender", "seat", "repo", "a" * 40,
            "docs/proof.md", "published mail")
        participant = resolve_participant(self.root.path, self.workspace)
        self.held_lock("events.jsonl.lock", 1.35)
        owner = self
        class Output(io.StringIO):
            changed = False
            def write(self, value):
                result = super().write(value)
                if value and not self.changed:
                    self.changed = True
                    CodexWaitConsentLedger(owner.root).arm(participant.binding,
                        hook_timeout_seconds=10, wait_deadline_seconds=2,
                        idempotency_key="after-published-consent")
                return result
        self.output = Output()
        outcome = {}
        try:
            self.assertEqual(0, self.invoke(_waiter_result=outcome))
        except Exception as failure:
            self.fail("completed publication raised " + repr(failure))
        self.assertEqual(1, len(self.output.getvalue().splitlines()))
        self.assertIn(envelope["id"], json.loads(self.output.getvalue())["reason"])
        self.assertEqual("fresh_work", outcome["kind"])
        self.assertEqual([envelope["id"]], outcome["item_ids"])
        self.assertEqual(json.loads(self.output.getvalue()), outcome["decision"])
        wakes = read_records_snapshot(self.root, "receipts/wakes/seat.jsonl",
            allowed_kinds={"wake_attempt_receipt"})
        self.assertEqual(["woke"], [row["outcome"] for row in wakes])
        exits = read_records_snapshot(self.root, "receipts/wake-waiter-exit/seat.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"})
        self.assertNotIn("integrity_failure", [row["reason_code"] for row in exits])
        self.assertEqual(["start", "exit"], [row["phase"] for row in StopHoldLedger(self.root).read("seat")])

    def test_contended_end_after_native_start_never_delays_committed_mail(self):
        from unittest import mock
        envelope = EventLog(self.root).send("sender", "seat", "repo", "a" * 40,
            "docs/proof.md", "mail before END contention")
        original = StopHoldLedger.start
        owner = self
        def start(ledger, *args, **kwargs):
            row = original(ledger, *args, **kwargs)
            owner.held_lock("receipts/stop-holds/seat.jsonl.lock", 1.35)
            return row
        clock, sleeps = [0.0], []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with mock.patch.object(StopHoldLedger, "start", start):
            self.assertEqual(0, self.invoke(monotonic=lambda: clock[0], sleep=sleep))
        self.assertEqual([], sleeps, "completed mail's END testimony must not wait on a lock")
        self.assertEqual(1, len(self.output.getvalue().splitlines()))
        self.assertIn(envelope["id"], json.loads(self.output.getvalue())["reason"])
        deliveries = read_records_snapshot(self.root, "receipts/deliveries/seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertEqual([envelope["id"]], deliveries[0]["item_ids"])
        self.assertEqual(1, len(deliveries))
        holds = StopHoldLedger(self.root).read("seat")
        self.assertEqual(["start"], [row["phase"] for row in holds])
        self.assertEqual(3, holds[0]["deadline_seconds"])
        self.assertIn("stop_hold_exit", self.errors.getvalue())

    def cursor_native_fixture(self, budget=3):
        Registry(self.root).register("cursor-seat", "Cursor")
        workspace = self.base / "cursor-workspace"
        workspace.mkdir()
        arm_waiter_consent(self.root.path, workspace, "cursor-seat", "Cursor", 10, budget)
        return workspace, resolve_participant(self.root.path, workspace)

    def invoke_cursor(self, workspace, **kwargs):
        from floati.cursor_wait import run_cursor_stop_wait
        if "sleep" in kwargs and "wait_readable" not in kwargs:
            kwargs["wait_readable"] = scripted_readiness(
                kwargs["sleep"], interval=kwargs.get("poll_seconds", 0.02))
        return self.cursor_at(root=self.root.path, node="cursor-seat", runtime=Path.cwd(),
            deadline_seconds=3, poll_seconds=0.02, hook_timeout_seconds=10,
            payload={"status": "completed", "conversation_id": "cursor-session", "cwd": str(workspace)},
            stdout=self.output, stderr=self.errors, **kwargs)

    def test_cursor_initial_mail_retry_rechecks_native_consent_and_pause(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from floati import jsonl
        from floati.waiter_lock_retry import current_lock_window
        from floati.wake_control import WakeController
        for change in ("consent", "pause"):
            with self.subTest(change=change):
                self.setUp()
                workspace, participant = self.cursor_native_fixture()
                EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
                    "docs/proof.md", "initial mail")
                release = self.held_lock("events.jsonl.lock")
                now, replacements, failures = [100.0], [], []
                def replace():
                    try:
                        if change == "consent":
                            CodexWaitConsentLedger(self.root).arm(participant.binding,
                                hook_timeout_seconds=10, wait_deadline_seconds=2,
                                idempotency_key="initial-consent-replacement")
                        else:
                            WakeController(self.root).pause("cursor-seat", "cursor-session",
                                idempotency_key="initial-pause")
                        replacements.append(change)
                    except Exception as failure:
                        failures.append(failure)
                def clock():
                    return now[0]
                def sleep(seconds):
                    now[0] += seconds
                    # Backoff follows an actual failed native acquisition,
                    # after its guards unwind. Replacement must occur here,
                    # rather than racing startup against a parent timer.
                    if seconds == 0.05 and not replacements:
                        self.assertTrue(current_lock_window().contended)
                        actor = threading.Thread(target=replace)
                        actor.start()
                        self.addCleanup(actor.join, 5)
                        wait_join(actor, "test_waiter_lock_retry actor")
                        self.assertFalse(actor.is_alive(), "replacement must commit during native backoff")
                        self.assertEqual([], failures)
                try:
                    with patch.object(jsonl, "time", SimpleNamespace(monotonic=clock, sleep=sleep)):
                        self.assertEqual(0, self.invoke_cursor(workspace, clock=clock, sleep=sleep))
                finally:
                    release.set()
                self.assertEqual([change], replacements)
                self.assertEqual({}, json.loads(self.output.getvalue()))
                journal = self.root.path / "state/cursor-wait/cursor-seat/journal.jsonl"
                self.assertEqual("consent_withdrawn" if change == "consent" else "paused",
                    json.loads(journal.read_text().splitlines()[-1])["reason_code"])
                self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
                    allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_cursor_initial_retry_uses_shorter_native_consent_window(self):
        workspace, _participant = self.cursor_native_fixture(1)
        EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
            "docs/proof.md", "initial mail")
        self.held_lock("events.jsonl.lock")
        with self.lock_clock() as timing:
            self.assertEqual(0, self.invoke_cursor(workspace, clock=timing.clock, sleep=timing.sleep))
            self.assertAlmostEqual(1, timing.clock())
        self.assert_lock_held("events.jsonl.lock")
        self.assertNotIn("inbox", json.loads(self.output.getvalue())["followup_message"])
        self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_cursor_native_mail_emit_does_not_wait_for_contended_end(self):
        workspace, _participant = self.cursor_native_fixture()
        now, sleeps, sent = [0.0], [], []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
            if not sent:
                sent.append(EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
                    "docs/proof.md", "mid-loop END mail"))
                self.held_lock("receipts/stop-holds/cursor-seat.jsonl.lock", 1.35)
        self.assertEqual(0, self.invoke_cursor(workspace, clock=lambda: now[0], sleep=sleep))
        self.assertEqual([0.02], sleeps)
        self.assertEqual(1, len(self.output.getvalue().splitlines()))
        self.assertIn("inbox", json.loads(self.output.getvalue())["followup_message"])
        deliveries = read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertEqual(1, len(deliveries))
        self.assertEqual([sent[0]["id"]], deliveries[0]["item_ids"])
        holds = StopHoldLedger(self.root).read("cursor-seat")
        self.assertEqual(["start"], [row["phase"] for row in holds])
        self.assertEqual(3, holds[0]["deadline_seconds"])
        self.assertIn("stop_hold_exit", self.errors.getvalue())

    def test_cursor_unbound_actual_workspace_preserves_train3_refusal(self):
        from floati.cursor_wait import run_cursor_stop_wait
        self.cursor_native_fixture()
        EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
            "docs/proof.md", "unproven-coordinate mail")
        self.held_lock("events.jsonl.lock", 1.35)
        self.assertEqual(0, self.cursor_at(root=self.root.path, node="cursor-seat", runtime=Path.cwd(),
            deadline_seconds=3, poll_seconds=0.02, hook_timeout_seconds=10,
            payload={"status": "completed", "conversation_id": "cursor-session"},
            stdout=self.output, stderr=self.errors))
        self.assertIn("cursor_wait_workspace_unbound", json.loads(self.output.getvalue())["followup_message"])
        self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS)))
        self.assertEqual([], StopHoldLedger(self.root).read("cursor-seat"))

    def test_cursor_malformed_native_consent_keeps_typed_unreadable_without_retry(self):
        from floati.errors import IntegrityFailure
        from floati.waiter_lock_retry import WaiterLockWindow
        workspace, participant = self.cursor_native_fixture()
        ledger = CodexWaitConsentLedger(self.root)
        self.root.resolve_relative(ledger._relative("cursor-seat")).write_bytes(b"not-json\n")
        with self.assertRaises(IntegrityFailure) as observed:
            ledger.require_armed(participant.binding)
        EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
            "docs/proof.md", "mail with malformed consent")
        self.held_lock("events.jsonl.lock")
        identity = self.root.resolve_relative("events.jsonl.lock").stat()
        native_flock = fcntl.flock
        attempts = []
        def observe_event_acquisition(descriptor, operation):
            opened = os.fstat(descriptor)
            if ((opened.st_dev, opened.st_ino) == (identity.st_dev, identity.st_ino)
                    and operation & fcntl.LOCK_NB):
                attempts.append(operation)
            return native_flock(descriptor, operation)
        with self.lock_clock() as timing,\
                mock.patch.object(fcntl, "flock", observe_event_acquisition),\
                mock.patch.object(WaiterLockWindow, "retry", side_effect=AssertionError(
                    "malformed consent must not enter native retry")):
            self.assertEqual(0, self.invoke_cursor(workspace, clock=timing.clock, sleep=timing.sleep))
            self.assertEqual([], timing.sleeps)
            self.assertEqual(0, timing.clock())
        self.assertEqual([], attempts, "malformed consent must precede event lock acquisition")
        self.assert_lock_held("events.jsonl.lock")
        self.assertIn(observed.exception.code, json.loads(self.output.getvalue())["followup_message"])
        self.assertEqual([], read_records_snapshot(self.root, "receipts/deliveries/cursor-seat.jsonl",
            allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_strict_native_resolver_preserves_default_missing_and_unmatched_modes(self):
        from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE, resolve_workspace_binding
        from floati.errors import ProtocolRefusal
        path = self.root.path / WORKSPACE_MAP_RELATIVE
        original = path.read_bytes()
        self.assertEqual(resolve_participant(self.root.path, self.workspace),
                         resolve_participant(self.root.path, self.workspace, _strict=True))
        unmatched = self.base / "unmatched"
        unmatched.mkdir()
        self.assertIsNone(resolve_participant(self.root.path, unmatched, _strict=True))
        self.assertEqual(original, path.read_bytes())
        for malformed in (b"not-json\n", b'{"schema_version":true}\n'):
            with self.subTest(malformed=malformed):
                path.write_bytes(malformed)
                self.assertIsNone(resolve_workspace_binding(self.root.path, self.workspace))
                with self.assertRaises(ProtocolRefusal) as observed:
                    resolve_workspace_binding(self.root.path, self.workspace, _strict=True)
                self.assertEqual("codex_wait_workspace_map_invalid", observed.exception.code)
                self.assertEqual(malformed, path.read_bytes())
        path.unlink()
        self.assertIsNone(resolve_workspace_binding(self.root.path, self.workspace))
        self.assertIsNone(resolve_workspace_binding(self.root.path, self.workspace, _strict=True))
        row = json.loads(original)
        row["mappings"][0]["node_id"] = "unknown-seat"
        path.write_text(json.dumps(row) + "\n")
        unknown = path.read_bytes()
        self.assertIsNone(resolve_participant(self.root.path, self.workspace))
        with self.assertRaises(ProtocolRefusal) as observed:
            resolve_participant(self.root.path, self.workspace, _strict=True)
        self.assertEqual("unknown_node", observed.exception.code)
        self.assertEqual(unknown, path.read_bytes())

    def test_contended_native_currency_preserves_malformed_map_refusal(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from floati import jsonl
        from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE
        from floati.waiter_lock_retry import current_lock_window
        for harness in ("codex", "cursor"):
            with self.subTest(harness=harness):
                self.setUp()
                workspace = self.workspace
                node = "seat"
                if harness == "cursor":
                    workspace, _participant = self.cursor_native_fixture()
                    node = "cursor-seat"
                EventLog(self.root).send("sender", node, "repo", "a" * 40,
                    "docs/proof.md", "mail before map corruption")
                release = self.held_lock("events.jsonl.lock")
                now, corrupted = [100.0], []
                def clock():
                    return now[0]
                def sleep(seconds):
                    # Schedule corruption after actual native contention,
                    # never against interpreter or authority-proof startup.
                    self.assertTrue(current_lock_window().contended)
                    now[0] += seconds
                    if now[0] >= 100.2 and not corrupted:
                        (self.root.path / WORKSPACE_MAP_RELATIVE).write_bytes(b"not-json\n")
                        corrupted.append(True)
                try:
                    with patch.object(jsonl, "time", SimpleNamespace(monotonic=clock, sleep=sleep)):
                        self.assertEqual(0, self.invoke(monotonic=clock, sleep=sleep)
                            if harness == "codex" else self.invoke_cursor(workspace, clock=clock, sleep=sleep))
                finally:
                    release.set()
                self.assertEqual([True], corrupted)
                decision = json.loads(self.output.getvalue())
                text = decision["reason"] if harness == "codex" else decision["followup_message"]
                self.assertIn("codex_wait_workspace_map_invalid", text)
                self.assertEqual([], read_records_snapshot(self.root, f"receipts/deliveries/{node}.jsonl",
                    allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_cursor_malformed_map_refusal_precedes_contention_observation(self):
        from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE
        workspace, _participant = self.cursor_native_fixture()
        EventLog(self.root).send("sender", "cursor-seat", "repo", "a" * 40,
            "docs/proof.md", "mail before early map corruption")
        release = self.held_lock("events.jsonl.lock")
        (self.root.path / WORKSPACE_MAP_RELATIVE).write_bytes(b"not-json\n")
        try:
            self.assertEqual(0, self.invoke_cursor(workspace))
        finally:
            release.set()
        decision = json.loads(self.output.getvalue())
        self.assertIn("codex_wait_workspace_map_invalid", decision["followup_message"])
        self.assertEqual([], read_records_snapshot(self.root,
            "receipts/deliveries/cursor-seat.jsonl", allowed_kinds=set(WAKE_HOLD_KINDS)))

    def test_native_quiet_reopen_authority_loss_has_exact_invocation_result(self):
        from floati.codex_wait_contract import WORKSPACE_MAP_RELATIVE, consent_ledger_relative
        for change, reason in (("map", "not_claimant"), ("consent", "consent_withdrawn")):
            with self.subTest(change=change):
                self.setUp()
                now, changed, result = [0.0], [], {}
                def sleep(seconds):
                    now[0] += seconds
                    if not changed:
                        changed.append(change)
                        relative = WORKSPACE_MAP_RELATIVE if change == "map" else consent_ledger_relative("seat")
                        (self.root.path / relative).unlink()
                self.assertEqual(0, self.invoke(monotonic=lambda: now[0], sleep=sleep, _waiter_result=result))
                self.assertEqual("", self.output.getvalue())
                self.assertEqual({"kind": "authority_changed", "reason": reason, "decision": {}}, result)
                rows = read_records_snapshot(self.root, "receipts/wake-waiter-exit/seat.jsonl",
                    allowed_kinds={"wake_waiter_exit_receipt"})
                self.assertEqual([reason], [row["reason_code"] for row in rows])

    def test_delegated_native_wait_retains_prepeek_origin_and_remaining_budget(self):
        from floati.waiter_lock_retry import waiter_lock_window
        clock, sleeps = [0.0], []
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with waiter_lock_window(3, lambda: clock[0], sleep) as window:
            clock[0] = 1.0  # Fixture time already spent in the caller's initial peek.
            checker = lambda: None
            window.check_authority = checker
            outcome = {}
            self.assertEqual(0, self.invoke(_waiter_window=window, _waiter_result=outcome))
            self.assertIs(checker, window.check_authority)
        self.assertAlmostEqual(2, sum(sleeps))
        self.assertAlmostEqual(3, clock[0])
        self.assertIn("deadline exhausted", json.loads(self.output.getvalue())["reason"])
        self.assertEqual("exhausted", outcome["kind"])
        self.assertEqual(json.loads(self.output.getvalue()), outcome["decision"])
        holds = StopHoldLedger(self.root).read("seat")
        self.assertEqual(["start", "exit"], [row["phase"] for row in holds])
        self.assertEqual(2, holds[0]["deadline_seconds"])

    def test_delegated_malformed_startup_cooldown_uses_only_original_residual(self):
        from floati.codex_wait import _integrity_exit
        from floati.errors import IntegrityFailure
        from floati.waiter_lock_retry import waiter_lock_window
        import hashlib
        ledger = CodexWaitConsentLedger(self.root)
        participant = resolve_participant(self.root.path, self.workspace)
        self.root.resolve_relative(ledger._relative("seat")).write_bytes(b"not-json\n")
        with self.assertRaises(IntegrityFailure) as observed:
            ledger.require_armed(participant.binding)
        session_digest = hashlib.sha256(b"session").hexdigest()
        # First native integrity alert is immediate; the repeated same fault
        # is the existing diagnostic cooldown that must fit the shared window.
        self.assertEqual(0, _integrity_exit(participant, session_digest=session_digest,
            operation="consent_read", failure=observed.exception, waited_seconds=0,
            invocation_id="native-first-alert", deadline_seconds=3,
            stdout=io.StringIO(), stderr=io.StringIO()))
        clock, sleeps, outcome = [0.0], [], {}
        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds
        with waiter_lock_window(3, lambda: clock[0], sleep) as window:
            clock[0] = 1.0
            self.assertEqual(0, _integrity_exit(participant,
                session_digest=session_digest, operation="consent_read",
                failure=observed.exception, waited_seconds=0, invocation_id="native-cooldown",
                deadline_seconds=3, stdout=self.output, stderr=self.errors,
                monotonic=window.clock, sleep=window.sleep, _waiter_result=outcome))
        self.assertAlmostEqual(2, sum(sleeps))
        self.assertAlmostEqual(3, clock[0])
        self.assertEqual("integrity_failure", outcome["kind"])
        self.assertEqual(json.loads(self.output.getvalue()), outcome["decision"])
        exits = read_records_snapshot(self.root, "receipts/wake-waiter-exit/seat.jsonl",
            allowed_kinds={"wake_waiter_exit_receipt"})
        self.assertEqual(3, exits[-1]["waited_seconds"])

    def test_retry_scope_resets_and_only_exact_typed_transient_is_retried(self):
        from floati.errors import IntegrityFailure, ProtocolRefusal
        from floati.waiter_lock_retry import current_lock_window, waiter_lock_window
        now, sleeps = [0.0], []
        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds
        failures = [IntegrityFailure("bad_record", "malformed"),
                    ProtocolRefusal("ledger_lock_timeout", "untyped"),
                    ProtocolRefusal("workspace_identity_mismatch", "policy")]
        self.assertIsNone(current_lock_window())
        with waiter_lock_window(0.12, lambda: now[0], sleep) as window:
            for failure in failures:
                calls = []
                def fail_once():
                    calls.append(None)
                    self.assertEqual(1, len(calls), "a nontransient failure must never be retried")
                    raise failure
                with self.assertRaises(type(failure)) as raised:
                    window.run(fail_once)
                self.assertIs(failure, raised.exception)
            self.assertEqual([], sleeps)
            timeout = ProtocolRefusal("ledger_lock_timeout", "contended", remedy={
                "kind": "transient", "action": "retry_once", "delay_seconds": 1, "holder": "unknown"})
            attempts = []
            def contended():
                attempts.append(None)
                self.assertLessEqual(len(attempts), 3, "retry without sleep/deadline must fail finitely")
                raise timeout
            with self.assertRaisesRegex(ProtocolRefusal, "waiter_lock_deadline"):
                window.run(contended)
            self.assertEqual(2, len(sleeps))
            self.assertAlmostEqual(0.05, sleeps[0])
            self.assertAlmostEqual(0.07, sleeps[1])
            self.assertTrue(all(seconds > 0 for seconds in sleeps))
            self.assertEqual(0.12, now[0])
        self.assertIsNone(current_lock_window())
        with self.assertRaises(ValueError):
            with waiter_lock_window(1, lambda: 0, sleep):
                raise ValueError("control")
        self.assertIsNone(current_lock_window())

"""ZCode pre-hold contention shares the native hold's original deadline."""

from tests.readiness import wait_event, wait_join, wait_until
import contextlib
import fcntl
import importlib.util
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.records import WAKE_HOLD_KINDS
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_consent import arm_waiter_consent
from tests.private_artifacts import require_private_artifact
from tests.temp_roots import REAL_TEMP_ROOT
from tests.waiter_timing import scripted_readiness


SOURCE = Path(__file__).resolve().parents[1]


class WaiterLockRetryBridgeTests(unittest.TestCase):
    def setUp(self):
        require_private_artifact(self, "hooks/stop-hook-bridge.py")
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".git").mkdir()
        (self.workspace / ".zcode").mkdir()
        (self.workspace / "floati").symlink_to(SOURCE / "floati", target_is_directory=True)
        Registry(self.root).register("seat", "ZCode")
        Registry(self.root).register("sender", "worker")
        arm_waiter_consent(self.root.path, self.workspace, "seat", "ZCode", 10, 3)
        self.participant = resolve_participant(self.root.path, self.workspace)
        self.consent = CodexWaitConsentLedger(self.root).require_armed(self.participant.binding)
        CodexWaitSessionLedger(self.root).arm(self.participant.binding, self.consent, "session",
                                            idempotency_key="bridge-native-session")
        spec = importlib.util.spec_from_file_location("native_bridge_retry", SOURCE / "hooks/stop-hook-bridge.py")
        self.bridge = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bridge)
        self.bridge.ROOT = str(self.root.path)
        self.bridge.NODE = "seat"
        self.bridge.SESSION = "session"
        self.bridge.REPO = str(self.workspace)
        self.output, self.errors = io.StringIO(), io.StringIO()

    def held_lock(self, relative, seconds, release=None):
        path = self.root.resolve_relative(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        ready = threading.Event()
        owned_release = threading.Event() if release is None else release
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
                if release is None:
                    wait_until(lambda: owned_release.is_set() or contended.is_set(),
                               'bridge lock first contended probe')
                    # Keep the intended product lock-hold duration, but begin
                    # it only when the real product encounters this lock.
                    owned_release.wait(seconds)
                else:
                    wait_event(owned_release, 'bridge owned lock release')
        child = threading.Thread(target=hold)
        child.start()
        self.addCleanup(wait_join, child, 'bridge owned lock holder cleanup')
        self.addCleanup(owned_release.set)
        self.assertTrue(wait_event(ready, "test_waiter_lock_retry_bridge ready"), "owned scratch holder must acquire its lock")
        return child

    def later(self, action, seconds=0.2):
        failures = []
        def invoke():
            try:
                action()
            except Exception as failure:
                failures.append(failure)
        actor = threading.Timer(seconds, invoke)
        actor.start()
        def finish():
            actor.join(5)
            self.assertFalse(actor.is_alive(), "native fixture actor must finish")
            self.assertEqual([], failures)
        self.addCleanup(finish)

    def send(self):
        return EventLog(self.root).send("sender", "seat", "repo", "a" * 40,
                                       "docs/proof.md", "bridge native mail")

    def invoke(self):
        payload = io.StringIO(json.dumps({"cwd": str(self.workspace), "session_id": "session"}))
        with mock.patch.object(self.bridge.os, "getcwd", return_value=str(self.workspace)),\
                mock.patch("sys.stdin", payload), contextlib.redirect_stdout(self.output),\
                contextlib.redirect_stderr(self.errors):
            status = self.bridge.main()
        self.assertEqual(0, status, self.errors.getvalue())

    @contextlib.contextmanager
    def controlled_clock(self, on_tick=lambda now: None):
        from floati import jsonl

        now = 100.0

        def monotonic():
            return now

        def sleep(seconds):
            nonlocal now
            now += seconds
            on_tick(now)

        clock = SimpleNamespace(monotonic=monotonic, sleep=sleep)
        with mock.patch.object(self.bridge, "time", clock), mock.patch.object(jsonl, "time", clock):
            yield clock

    @contextlib.contextmanager
    def retryable_events_contention(self):
        from floati.jsonl import LOCK_TIMEOUT_SECONDS

        release = threading.Event()
        holder = self.held_lock("events.jsonl.lock", None, release)

        def unlock_after_timeout(now):
            if now >= unlock_at and not release.is_set():
                release.set()
                wait_join(holder, "bridge event lock release")

        with self.controlled_clock(unlock_after_timeout) as clock:
            unlock_at = clock.monotonic() + LOCK_TIMEOUT_SECONDS + 0.35
            yield clock

    def firing(self):
        path = self.root.resolve_relative(f"state/zcode-hook/firings/{self.bridge.NODE}.jsonl")
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertEqual(1, len(rows), rows)
        return rows[0]

    def deliveries(self):
        return read_records_snapshot(self.root, "receipts/deliveries/seat.jsonl",
                                     allowed_kinds=set(WAKE_HOLD_KINDS))

    @contextlib.contextmanager
    def observed_legacy_handler(self):
        from floati import cli
        actual, observations = cli._inbox, []
        def inbox(arguments):
            result = actual(arguments)
            observations.append(result)
            return result
        with mock.patch.object(cli, "_inbox", inbox):
            yield observations
        self.assertEqual(1, len(observations), "legacy peek must call the exact native inbox handler once")

    def test_legacy_empty_handler_keeps_typed_silence_and_no_delivery(self):
        self.root.resolve_relative(CodexWaitConsentLedger._relative("seat")).unlink()
        with self.observed_legacy_handler() as observations:
            self.invoke()
        self.assertEqual("intentional_silence", observations[0][0])
        self.assertEqual("", self.output.getvalue())
        self.assertEqual([], self.deliveries())

    def test_legacy_mail_handler_presents_one_exact_batch_once(self):
        self.root.resolve_relative(CodexWaitConsentLedger._relative("seat")).unlink()
        envelope = self.send()
        with self.observed_legacy_handler() as observations:
            self.invoke()
        self.assertEqual("ok", observations[0][0])
        self.assertIn(envelope["id"], self.output.getvalue())
        self.assertEqual(1, len(self.output.getvalue().splitlines()))
        self.assertEqual([envelope["id"]], self.deliveries()[0]["item_ids"])
        self.assertEqual(1, len(self.deliveries()))

    def test_legacy_unknown_recipient_preserves_native_typed_refusal(self):
        self.bridge.NODE = "unknown"
        with self.observed_legacy_handler() as observations:
            self.invoke()
        self.assertEqual("refused", observations[0][0])
        self.assertEqual("recipient_unregistered", observations[0][1]["code"])
        self.assertEqual("recipient_unregistered", self.firing()["query_failed"])
        self.assertEqual([], self.deliveries())

    def test_legacy_retired_recipient_preserves_exact_handler_refusal(self):
        Registry(self.root).retire("seat")
        with self.observed_legacy_handler() as observations:
            messages, failure = self.bridge.bus_inbox(_inprocess=True)
        self.assertEqual([], messages)
        self.assertEqual("refused", observations[0][0])
        self.assertEqual("recipient_unregistered", failure["code"])
        self.assertEqual(observations[0][1]["remedy"], failure["remedy"])
        self.assertEqual([], self.deliveries())

    def test_legacy_root_refusal_outside_handler_uses_native_json_renderer(self):
        self.bridge.ROOT = str(self.base / "missing-root")
        artifact = json.loads(self.bridge.inprocess_inbox_artifact())
        self.assertEqual(0, artifact["artifact_version"])
        self.assertEqual("inbox", artifact["command"])
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("direct_home_missing", artifact["evidence"]["code"])
        self.assertEqual({"kind": "none", "why": "no action was named for this refusal"},
                         artifact["evidence"]["remedy"])
        self.assertEqual([], self.deliveries())

    def test_legacy_wrong_declared_scope_refuses_before_delivery(self):
        from floati.seat_declaration import FleetGovernance, SeatDeclaration
        governance = FleetGovernance.create(self.root, topology="star", coordinator="sender",
            coordinator_authority=["dispatch_bounded_work"], owner_tier=["publishing"])
        workspace = self.root.path / "nodes/sender"
        SeatDeclaration.create(workspace, "sender", self.root, governance)
        (workspace / ".git").mkdir()
        (workspace / "floati").symlink_to(SOURCE / "floati", target_is_directory=True)
        self.workspace = workspace
        self.bridge.REPO = str(workspace)
        self.send()
        with self.observed_legacy_handler() as observations:
            self.invoke()
        self.assertEqual("refused", observations[0][0])
        self.assertEqual("workspace_identity_mismatch", observations[0][1]["code"])
        self.assertEqual("workspace_identity_mismatch", self.firing()["query_failed"])
        self.assertEqual([], self.deliveries())

    def test_native_pre_hold_peek_recovers_without_duplicate_delivery_or_output(self):
        from floati import jsonl
        from floati.errors import ProtocolRefusal
        original = jsonl._locked_path
        observed = []
        @contextlib.contextmanager
        def acquisition(*args, **kwargs):
            try:
                with original(*args, **kwargs):
                    yield
            except ProtocolRefusal as failure:
                if failure.code == "ledger_lock_timeout":
                    observed.append(failure)
                raise
        envelope = self.send()
        with self.retryable_events_contention() as clock,\
                mock.patch.object(jsonl, "_locked_path", acquisition):
            started = clock.monotonic()
            self.invoke()
            elapsed = clock.monotonic() - started
        lines = self.output.getvalue().splitlines()
        self.assertEqual(1, len(lines))
        self.assertIn(envelope["id"], json.loads(lines[0])["reason"])
        self.assertNotIn("query_failed", self.firing())
        rows = self.deliveries()
        self.assertEqual(1, len(rows))
        self.assertEqual([envelope["id"]], rows[0]["item_ids"])
        self.assertGreaterEqual(len(observed), 1, "native acquisition must exceed its one-second attempt")
        self.assertLess(elapsed, self.consent["wait_deadline_seconds"])

    def test_native_pre_hold_contention_cannot_restart_quiet_hold_budget(self):
        from floati import codex_wait
        from floati.stop_hold import StopHoldLedger
        from floati.waiter_lock_retry import current_lock_window

        # Time advances only in the actual contention/readiness waits. Host
        # startup and terminal reporting cannot consume this property clock.
        delegated, completed = [], []
        # An absent event ledger returns [] without acquiring its lock.
        self.root.resolve_relative("events.jsonl").write_bytes(b"")
        native_waiter = codex_wait.run_stop_waiter
        budget = self.consent["wait_deadline_seconds"]
        def observe_waiter(**kwargs):
            window = kwargs["_waiter_window"]
            self.assertIs(current_lock_window(), window)
            self.assertTrue(window.contended, "pre-hold peek must encounter the real lock")
            self.assertAlmostEqual(started, window.started)
            self.assertAlmostEqual(started + budget, window.deadline)
            self.assertGreaterEqual(clock.monotonic(), started + 1.35)
            self.assertGreater(window.remaining(), 0)
            self.assertLessEqual(window.remaining(), budget - 1.35)
            delegated.append((window.started, window.deadline, window.remaining()))
            kwargs["wait_readable"] = scripted_readiness(clock.sleep, interval=0.02)
            result = native_waiter(**kwargs)
            self.assertEqual("exhausted", kwargs["_waiter_result"]["kind"])
            self.assertAlmostEqual(started + budget, clock.monotonic(),
                                   msg="quiet hold restarted the original budget")
            completed.append(True)
            return result

        with self.retryable_events_contention() as clock,\
                mock.patch.object(codex_wait, "run_stop_waiter", observe_waiter):
            started = clock.monotonic()
            self.invoke()
        self.assertEqual(1, len(delegated))
        self.assertEqual([True], completed)
        holds = StopHoldLedger(self.root).read("seat")
        self.assertEqual(["start", "exit"], [row["phase"] for row in holds])
        self.assertAlmostEqual(delegated[0][2], holds[0]["deadline_seconds"])
        self.assertEqual("", self.output.getvalue())
        self.assertEqual("deadline", self.firing()["outcome"])
        self.assertEqual([], self.deliveries())

    def test_native_claim_takeover_during_pre_hold_retry_cannot_deliver(self):
        self.send()
        self.held_lock("events.jsonl.lock", 1.35)
        self.later(lambda: CodexWaitSessionLedger(self.root).arm(
            self.participant.binding, self.consent, "replacement", take_over=True,
            idempotency_key="bridge-takeover"))
        self.invoke()
        self.assertEqual("", self.output.getvalue())
        self.assertEqual("superseded_session", self.firing()["why"])
        self.assertEqual([], self.deliveries())

    def test_native_consent_replacement_during_pre_hold_retry_cannot_deliver(self):
        self.send()
        self.held_lock("events.jsonl.lock", 1.35)
        self.later(lambda: CodexWaitConsentLedger(self.root).arm(
            self.participant.binding, hook_timeout_seconds=10, wait_deadline_seconds=2,
            idempotency_key="bridge-replacement"))
        self.invoke()
        self.assertEqual("", self.output.getvalue())
        self.assertEqual("consent_withdrawn", self.firing()["why"])
        self.assertEqual([], self.deliveries())

    def test_native_malformed_consent_is_typed_failure_before_delivery(self):
        self.send()
        self.root.resolve_relative(CodexWaitConsentLedger._relative("seat")).write_bytes(b"not-json\n")
        with self.controlled_clock() as clock:
            started = clock.monotonic()
            self.invoke()
            self.assertEqual(started, clock.monotonic())
        row = self.firing()
        self.assertEqual("malformed_json", row.get("query_failed"))
        self.assertNotIn("injected", row)
        self.assertIn("malformed_json", self.output.getvalue())
        self.assertEqual([], self.deliveries())

    def test_native_pause_during_pre_hold_retry_cannot_deliver(self):
        from floati.wake_control import WakeController
        self.send()
        self.held_lock("events.jsonl.lock", 1.35)
        self.later(lambda: WakeController(self.root).pause("seat", "session", idempotency_key="bridge-pause"))
        self.invoke()
        self.assertEqual("", self.output.getvalue())
        self.assertEqual("paused", self.firing()["why"])
        self.assertEqual([], self.deliveries())

    def test_native_quiet_flow_relays_one_committed_wake_without_second_present(self):
        from floati import codex_wait

        self.root.resolve_relative("events.jsonl").write_bytes(b"")
        sent = []
        relayed_at = []
        native_waiter = codex_wait.run_stop_waiter
        native_finish = self.bridge.finish_bridge
        budget = self.consent["wait_deadline_seconds"]

        def dispatch_waiter(**kwargs):
            def tick(seconds):
                clock.sleep(seconds)
                if not sent:
                    sent.append(self.send())

            kwargs["wait_readable"] = scripted_readiness(tick, interval=0.02)
            return native_waiter(**kwargs)

        def slow_finish(*args, **kwargs):
            status = native_finish(*args, **kwargs)
            relayed_at.append(clock.monotonic())
            # An observer can resume late even after the decision is flushed.
            clock.sleep(budget)
            return status

        with self.retryable_events_contention() as clock,\
                mock.patch.object(codex_wait, "run_stop_waiter", dispatch_waiter),\
                mock.patch.object(self.bridge, "finish_bridge", slow_finish):
            started = clock.monotonic()
            self.invoke()
        lines = self.output.getvalue().splitlines()
        self.assertEqual(1, len(lines))
        self.assertIn(sent[0]["id"], json.loads(lines[0])["reason"])
        rows = self.deliveries()
        self.assertEqual(1, len(rows))
        self.assertEqual([sent[0]["id"]], rows[0]["item_ids"])
        self.assertEqual("fresh_work_relayed", self.firing()["outcome"])
        self.assertEqual(1, len(relayed_at))
        self.assertLess(relayed_at[0] - started, budget)

    def test_absent_native_consent_keeps_one_shot_timeout_with_exact_remedy(self):
        from floati.jsonl import LOCK_POLL_SECONDS, LOCK_TIMEOUT_SECONDS

        self.root.resolve_relative(CodexWaitConsentLedger._relative("seat")).unlink()
        self.send()
        release = threading.Event()
        self.held_lock("events.jsonl.lock", None, release)
        try:
            with self.controlled_clock() as clock, self.observed_legacy_handler() as observations:
                started = clock.monotonic()
                self.invoke()
                elapsed = clock.monotonic() - started
        finally:
            release.set()
        self.assertAlmostEqual(LOCK_TIMEOUT_SECONDS, elapsed, delta=LOCK_POLL_SECONDS)
        status, evidence, _exit = observations[0]
        self.assertEqual("refused", status)
        self.assertEqual("ledger_lock_timeout", evidence["code"])
        self.assertEqual({"kind": "transient", "action": "retry_once", "delay_seconds": 1,
                          "holder": "unknown"}, evidence["remedy"])
        row = self.firing()
        self.assertEqual("ledger_lock_timeout", row["query_failed"])
        self.assertIn("retry once after 1 second", row["query_detail"])
        self.assertNotIn("injected", row)
        self.assertEqual([], self.deliveries())

    def test_bus_inbox_preserves_closed_transient_remedy_at_artifact_boundary(self):
        import subprocess
        remedy = {"kind": "transient", "action": "retry_once", "delay_seconds": 1, "holder": "unknown"}
        completed = subprocess.CompletedProcess([], 20, json.dumps({
            "status": "refused", "evidence": {"code": "ledger_lock_timeout", "detail": "contended", "remedy": remedy}}), "")
        with mock.patch.object(self.bridge.subprocess, "run", return_value=completed):
            messages, failure = self.bridge.bus_inbox()
        self.assertEqual([], messages)
        self.assertEqual(remedy, failure["remedy"])
        self.assertIn("retry once after 1 second", failure["detail"])

    def test_malformed_native_claim_is_typed_failure_before_delivery(self):
        self.send()
        self.root.resolve_relative(CodexWaitSessionLedger._relative("seat")).write_bytes(b"not-json\n")
        self.invoke()
        self.assertEqual("malformed_json", self.firing()["query_failed"])
        self.assertEqual([], self.deliveries())

    def test_native_retirement_during_pre_hold_retry_cannot_deliver(self):
        self.send()
        self.held_lock("events.jsonl.lock", 1.35)
        self.later(lambda: Registry(self.root).retire("seat"))
        self.invoke()
        self.assertEqual("unknown_node", self.firing()["query_failed"])
        self.assertEqual([], self.deliveries())

    def test_native_malformed_events_exit_as_integrity_without_retry(self):
        self.root.resolve_relative("events.jsonl").write_bytes(b"not-json\n")
        with self.controlled_clock() as clock:
            started = clock.monotonic()
            self.invoke()
            self.assertEqual(started, clock.monotonic())
        self.assertEqual("malformed_json", self.firing()["query_failed"])
        self.assertIn("malformed_json", self.output.getvalue())
        self.assertEqual([], self.deliveries())

    def test_committed_native_wake_is_not_erased_by_later_corrupt_observation(self):
        from floati import codex_wait
        original = codex_wait.run_stop_waiter
        sent = []
        self.later(lambda: sent.append(self.send()))
        def completed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.root.resolve_relative("receipts/deliveries/seat.jsonl").write_bytes(b"not-json\n")
            return result
        with mock.patch.object(codex_wait, "run_stop_waiter", completed):
            self.invoke()
        self.assertEqual(1, len(self.output.getvalue().splitlines()))
        self.assertIn(sent[0]["id"], json.loads(self.output.getvalue())["reason"])
        self.assertEqual("fresh_work_relayed", self.firing()["outcome"])

    def test_present_malformed_native_map_is_not_demoted_to_legacy(self):
        self.send()
        self.root.resolve_relative("codex-wait/workspaces.v0.json").write_bytes(b"not-json\n")
        self.invoke()
        self.assertEqual("codex_wait_workspace_map_invalid", self.firing().get("query_failed"))
        self.assertEqual([], self.deliveries())

    def test_absent_consent_cannot_mask_present_malformed_native_map(self):
        self.send()
        self.root.resolve_relative("codex-wait/workspaces.v0.json").write_text("not-json\n")
        self.root.resolve_relative(CodexWaitConsentLedger._relative("seat")).unlink()
        self.invoke()
        self.assertEqual("codex_wait_workspace_map_invalid", self.firing().get("query_failed"))
        self.assertEqual([], self.deliveries())

    def test_absent_map_cannot_mask_present_malformed_native_consent(self):
        self.send()
        self.root.resolve_relative("codex-wait/workspaces.v0.json").unlink()
        self.root.resolve_relative(CodexWaitConsentLedger._relative("seat")).write_text("not-json\n")
        self.invoke()
        self.assertEqual("malformed_json", self.firing().get("query_failed"))
        self.assertEqual([], self.deliveries())

    def test_absent_consent_cannot_mask_present_malformed_native_claim(self):
        self.send()
        self.root.resolve_relative(CodexWaitConsentLedger._relative("seat")).unlink()
        self.root.resolve_relative(CodexWaitSessionLedger._relative("seat")).write_text("not-json\n")
        self.invoke()
        self.assertEqual("malformed_json", self.firing().get("query_failed"))
        self.assertEqual([], self.deliveries())

    def test_symlink_native_map_is_not_demoted_to_legacy(self):
        self.send()
        path = self.root.resolve_relative("codex-wait/workspaces.v0.json")
        alias = self.base / "map-alias.json"
        alias.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(alias)
        self.invoke()
        self.assertEqual("codex_wait_workspace_map_invalid", self.firing().get("query_failed"))
        self.assertEqual([], self.deliveries())

    def test_missing_native_map_keeps_existing_one_shot_mail_delivery(self):
        envelope = self.send()
        self.root.resolve_relative("codex-wait/workspaces.v0.json").unlink()
        self.invoke()
        self.assertIn(envelope["id"], self.output.getvalue())
        self.assertFalse(self.firing()["waited"])
        self.assertEqual(1, len(self.deliveries()))

    def test_already_superseded_native_claim_is_authority_loss_before_delivery(self):
        self.send()
        CodexWaitSessionLedger(self.root).arm(self.participant.binding, self.consent, "replacement",
            take_over=True, idempotency_key="bridge-already-replaced")
        self.invoke()
        self.assertEqual("", self.output.getvalue())
        self.assertEqual("superseded_session", self.firing()["why"])
        self.assertEqual([], self.deliveries())

    def test_native_breaker_preserves_quiet_stop_instead_of_false_unproven_block(self):
        from floati.codex_wait import BREAKER_MAX_INVOCATIONS, _breaker_tripped
        for _ in range(BREAKER_MAX_INVOCATIONS):
            self.assertFalse(_breaker_tripped(self.root, "seat", now=time.time()))
        self.invoke()
        self.assertEqual("", self.output.getvalue())
        self.assertEqual("breaker", self.firing()["outcome"])
        self.assertEqual([], self.deliveries())

    def test_proven_claim_disappearance_during_contention_cannot_deliver(self):
        self.send()
        self.held_lock("events.jsonl.lock", 1.35)
        original, scheduled = EventLog._present_already_guarded, []
        def present(*args, **kwargs):
            if not scheduled:
                scheduled.append(True)
                self.later(lambda: self.root.resolve_relative(CodexWaitSessionLedger._relative("seat")).unlink())
            return original(*args, **kwargs)
        with mock.patch.object(EventLog, "_present_already_guarded", present):
            self.invoke()
        self.assertEqual("", self.output.getvalue())
        self.assertEqual("not_claimant", self.firing()["why"])
        self.assertEqual([], self.deliveries())

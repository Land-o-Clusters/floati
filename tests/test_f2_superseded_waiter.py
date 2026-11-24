"""F2 Am8: takeover linearizes against actual waiter reads and publication.

Scratch only. Race gates observe the real B-arm lane flock; they never replace
the arm, synthesize its receipt, or enter the sealed hold writer themselves.
The parent owns RED execution and banking before any production correction.
"""
from __future__ import annotations

from tests.readiness import wait_event
import copy
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import threading
import unittest
from unittest import mock

from floati import codex_wait, codex_wait_liveness, wake_daemon_adapters
from floati.errors import ProtocolRefusal
from floati.jsonl import read_records_snapshot
from floati.records import WAKE_HOLD_KINDS, validate_record
from floati.wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
from floati.wake_exit import WakeExitLedger
from floati.wake_hold import WakeHoldController
from tests import test_codex_wait as baseline
from tests.schema_validation import SchemaValidationError, validate_json_schema
from tests.waiter_timing import scripted_readiness


A = "thread-former-positive-a"
B = "thread-current-owner-b"
OLD_REASONS = (
    "exhausted", "paused", "not_claimant", "consent_withdrawn",
    "breaker", "integrity_failure",
)
SCHEMA = Path(__file__).resolve().parents[1] / "schemas/v1/wake-waiter-exit-receipt.schema.json"
OLD_SCHEMA = Path(__file__).resolve().parent / "fixtures/f2-wake-waiter-exit-v1-before.schema.json"
MAIN_SCHEMA = Path(__file__).resolve().parent / "fixtures/f2-wake-waiter-exit-v1-main-before.schema.json"


class _TakeoverRace:
    """Gate on B's actual flock result, never on a scheduler timing guess."""

    def __init__(self, case, *, after_arm=None):
        self.case = case
        self.after_arm = after_arm
        self.order = []
        self.errors = []
        self.first_attempt = threading.Event()
        self.finished = threading.Event()
        self.first_outcome = None
        self.thread = None
        self.real_flock = fcntl.flock
        path = case.f.root.resolve_relative(
            Path("receipts/wake-coordination") / case.node / "lane.lock")
        stat = path.stat()
        self.lock_identity = (stat.st_dev, stat.st_ino)

    def observed_flock(self, descriptor, operation):
        measured = os.fstat(descriptor)
        watched = (threading.current_thread() is self.thread
                   and operation & fcntl.LOCK_NB
                   and (measured.st_dev, measured.st_ino) == self.lock_identity)
        try:
            result = self.real_flock(descriptor, operation)
        except BlockingIOError:
            if watched and self.first_outcome is None:
                self.first_outcome = "blocked"
                self.order.append("b_lane_blocked")
                self.first_attempt.set()
            raise
        if watched and self.first_outcome is None:
            self.first_outcome = "acquired"
            self.order.append("b_lane_acquired")
            self.first_attempt.set()
        return result

    def launch(self):
        if self.thread is not None:
            return

        def take_over():
            try:
                receipt = self.case.f.arm_session(B)
                self.order.append("b_committed")
                if receipt["acting_session_id"] != B:
                    raise AssertionError("actual B-arm returned another session")
                if self.after_arm is not None:
                    self.after_arm()
            except BaseException as exc:
                self.errors.append(exc)
            finally:
                self.finished.set()

        self.thread = threading.Thread(target=take_over, name="f2-scratch-b-arm")
        self.thread.start()
        # These are failure bounds, not elapsed-time correctness assertions.
        if not wait_event(self.first_attempt, 'f2_superseded_waiter: self.first_attempt synchronization'):
            raise RuntimeError("actual B-arm never reached the exact node lane lock")
        if self.first_outcome == "acquired" and not wait_event(self.finished, 'f2_superseded_waiter: self.finished synchronization'):
            raise RuntimeError("unblocked actual B-arm did not finish")

    def collect(self):
        if self.thread is not None:
            self.thread.join(6)
            self.case.assertFalse(self.thread.is_alive(), "scratch B-arm was not collected")
        self.case.assertFalse(self.errors, repr(self.errors))


class _FlushGate(io.StringIO):
    def __init__(self, race):
        super().__init__()
        self.race = race

    def flush(self):
        self.race.launch()
        self.race.order.append("a_flushed")
        return super().flush()


class F2SupersededWaiterTests(unittest.TestCase):
    def setUp(self):
        # Composition reuses scratch setup without rediscovering baseline tests.
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.node = self.f.participant.binding.node_id

    def rows(self, directory, kinds):
        relative = Path("receipts") / directory / (self.node + ".jsonl")
        if not self.f.root.resolve_relative(relative).exists():
            return []
        return read_records_snapshot(self.f.root, relative, allowed_kinds=set(kinds))

    def exits(self):
        return self.rows("wake-waiter-exit", {"wake_waiter_exit_receipt"})

    def holds(self):
        return self.rows("deliveries", set(WAKE_HOLD_KINDS))

    def current(self):
        return self.rows("codex-wait-session", {"codex_wait_session_receipt"})[-1]

    def run_waiter(self, *, session=A, stdout=None, sleep=None):
        output = io.StringIO() if stdout is None else stdout
        errors = io.StringIO()
        clock = [0.0]
        def tick(seconds):
            clock[0] += seconds
            if sleep is not None:
                sleep()
        status = codex_wait.run_stop_waiter(
            bus_home=self.f.bus_home,
            hook_payload={"cwd": str(self.f.workspace), "session_id": session},
            stdout=output, stderr=errors, monotonic=lambda: clock[0],
            wait_readable=scripted_readiness((tick), interval=(1.0)), sleep=tick, poll_interval_seconds=1.0)
        self.assertEqual(0, status, errors.getvalue() or output.getvalue())
        return output.getvalue(), errors.getvalue()

    def assert_superseded(self, output, errors):
        self.assertEqual("{}\n", output)
        self.assertIn("superseded_session", errors)
        self.assertIn(A, errors)
        self.assertIn(B, errors)
        self.assertLessEqual(len(errors), 1024, "supersession diagnostic must be bounded")
        rows = self.exits()
        self.assertEqual(["superseded_session"], [row["reason_code"] for row in rows])
        self.assertEqual(hashlib.sha256(A.encode()).hexdigest(), rows[0]["session_digest"])
        self.assertEqual(set(json.loads(OLD_SCHEMA.read_bytes())["required"]), set(rows[0]))

    def test_former_positive_claimant_declines_before_any_mail_read(self):
        self.f.arm_session(A)
        self.f.arm_session(B)
        message = self.f.send("f2-before-entry")
        reads = []
        real_read = WakeHoldController._read
        def observed(controller, *args, **kwargs):
            reads.append(self.current()["acting_session_id"])
            return real_read(controller, *args, **kwargs)
        with mock.patch.object(WakeHoldController, "_read", observed):
            output, errors = self.run_waiter()
        self.assert_superseded(output, errors)
        self.assertEqual([], reads)
        self.assertEqual([], self.holds())
        self.assertEqual([], self.rows("wakes", {"wake_attempt_receipt"}))
        self.assertEqual(B, self.current()["acting_session_id"])
        self.assertTrue(message["id"])

    def test_running_former_claimant_reports_superseded_at_next_poll(self):
        self.f.arm_session(A)
        changed = []
        def replace():
            if not changed:
                changed.append(self.f.arm_session(B))
        output, errors = self.run_waiter(sleep=replace)
        self.assert_superseded(output, errors)
        self.assertEqual(1, self.exits()[0]["waited_seconds"])
        self.assertEqual([], self.holds())

    def test_never_claimant_remains_silent_and_byte_identical(self):
        self.f.arm_session(B)
        self.f.send("f2-never-claimant")
        before = self.f.bus_bytes()
        output, errors = self.run_waiter(session="thread-never-claimed")
        self.assertEqual("", output)
        self.assertEqual("", errors)
        self.assertEqual(before, self.f.bus_bytes())
        self.assertEqual([], self.exits())

    def test_map_withdrawal_keeps_not_claimant_and_empty_stdout(self):
        self.f.arm_session(A)
        withdrawn = []
        def withdraw():
            if not withdrawn:
                path = self.f.bus_home / "codex-wait/workspaces.v0.json"
                replacement = path.with_name(path.name + ".withdrawn")
                replacement.write_text(json.dumps({"schema_version": 0,
                    "tenant_id": self.f.root.tenant_id, "mappings": []}) + "\n")
                os.replace(replacement, path)
                withdrawn.append(True)
        output, errors = self.run_waiter(sleep=withdraw)
        self.assertEqual("", output)
        self.assertEqual("", errors)
        self.assertEqual(["not_claimant"], [row["reason_code"] for row in self.exits()])
        self.assertEqual([], self.holds())

    def assert_race_outcome(self, race, output, errors, message_id, reads):
        self.assertIsNotNone(race.thread, "the named race seam was never reached")
        self.assertEqual(B, self.current()["acting_session_id"])
        self.assertNotIn(B, reads, "displaced A read mail after B committed")
        if output == "{}\n":
            self.assert_superseded(output, errors)
            self.assertEqual([], self.holds())
            self.assertEqual([], reads)
        else:
            decision = json.loads(output)
            self.assertEqual("block", decision["decision"], errors)
            self.assertIn(message_id, decision["reason"])
            self.assertEqual([A], reads)
            self.assertEqual(1, len(self.holds()))
            self.assertEqual([message_id], self.holds()[0]["item_ids"])
            self.assertLess(race.order.index("a_flushed"), race.order.index("b_committed"),
                            "A published a mail block after takeover committed")
        before = self.f.bus_bytes()
        again, diagnostic = self.run_waiter()
        self.assertEqual("{}\n", again)
        self.assertIn("superseded_session", diagnostic)
        self.assertEqual(1 if output != "{}\n" else 0, len(self.holds()))
        # A repeat may add exit testimony, never new mail holds or a new claim.
        self.assertEqual(before["receipts/codex-wait-session/" + self.node + ".jsonl"],
                         self.f.bus_bytes()["receipts/codex-wait-session/" + self.node + ".jsonl"])
        # Historical A holds remain valid testimony. B's ordinary inbox path
        # must deliver pending mail; this row does not redefine held as fresh.
        from floati.events import EventLog
        messages, receipt = EventLog(self.f.root).present(self.node)
        self.assertEqual([message_id], [message["id"] for message in messages])
        # Direct presentation follows the waiter return; this fixture has no
        # own live presenter testimony for the ordinary inbox call.
        self.assertEqual("delivered_unpresented", receipt["kind"])
        self.assertEqual([], self.rows("acks", {"ack_receipt"}))

    def race_at(self, seam):
        self.f.arm_session(A)
        message = self.f.send("f2-race-" + seam)
        race = _TakeoverRace(self)
        self.addCleanup(race.collect)
        reads = []
        real_read = WakeHoldController._read
        real_evaluate = WakeHoldController.evaluate
        def observed_read(controller, *args, **kwargs):
            reads.append(self.current()["acting_session_id"])
            return real_read(controller, *args, **kwargs)
        def gated_evaluate(controller, *args, **kwargs):
            if seam == "evaluate":
                race.launch()
            return real_evaluate(controller, *args, **kwargs)
        if seam == "flush":
            output = _FlushGate(race)
        else:
            class Published(io.StringIO):
                def flush(self):
                    race.order.append("a_flushed")
                    return super().flush()
            output = Published()
        with mock.patch("floati.jsonl.fcntl.flock", side_effect=race.observed_flock),\
             mock.patch.object(WakeHoldController, "_read", observed_read),\
             mock.patch.object(WakeHoldController, "evaluate", gated_evaluate):
            text, errors = self.run_waiter(stdout=output)
            race.collect()
        self.assert_race_outcome(race, text, errors, message["id"], reads)

    def test_actual_takeover_at_evaluate_cannot_authorize_a_displaced_mail_read(self):
        self.race_at("evaluate")

    def test_actual_takeover_at_flush_cannot_commit_before_a_mail_publication(self):
        self.race_at("flush")

    def test_deadline_flush_finishes_before_actual_takeover_commits(self):
        self.f.arm_session(A)
        race = _TakeoverRace(self)
        self.addCleanup(race.collect)
        output = _FlushGate(race)
        with mock.patch("floati.jsonl.fcntl.flock", side_effect=race.observed_flock):
            text, errors = self.run_waiter(stdout=output)
            race.collect()
        self.assertEqual("block", json.loads(text)["decision"], errors)
        self.assertIn("deadline exhausted", json.loads(text)["reason"])
        self.assertEqual("blocked", race.first_outcome)
        self.assertLess(race.order.index("a_flushed"), race.order.index("b_committed"))
        self.assertEqual(["exhausted"], [row["reason_code"] for row in self.exits()])
        self.assertEqual([], self.holds())
        self.assertEqual([], self.rows("wakes", {"wake_attempt_receipt"}))
        again, diagnostic = self.run_waiter()
        self.assertEqual("{}\n", again)
        self.assertIn("superseded_session", diagnostic)

    def test_loop_authority_failure_releases_guard_without_read_hold_or_wake(self):
        from floati.codex_wait_contract import CodexWaitSessionLedger
        self.f.arm_session(A)
        message = self.f.send("f2-loop-authority-failure")
        real_transaction = CodexWaitSessionLedger._participate_already_guarded
        calls = []
        def startup_then_fail(ledger, *args, **kwargs):
            if calls:
                raise OSError("private-loop-authority-failure")
            calls.append(True)
            return real_transaction(ledger, *args, **kwargs)
        with mock.patch.object(CodexWaitSessionLedger, "_participate_already_guarded", startup_then_fail):
            output, errors = self.run_waiter()
        # Main's landed integrity refusal continues the native turn. F2 still
        # proves no mail read, hold append, or wake on failed seat authority.
        self.assertEqual("block", json.loads(output)["decision"])
        self.assertIn("check session_authority", json.loads(output)["reason"])
        self.assertNotIn("private-loop-authority-failure", output)
        self.assertIn("session_authority: OSError", errors)
        self.assertNotIn("private-loop-authority-failure", errors)
        self.assertEqual(["integrity_failure"], [row["reason_code"] for row in self.exits()])
        self.assertEqual([], self.holds())
        self.assertEqual([], self.rows("wakes", {"wake_attempt_receipt"}))
        self.f.arm_session(B)  # A callback exception must release the real guard.
        from floati.events import EventLog
        messages, _receipt = EventLog(self.f.root).present(self.node)
        self.assertEqual([message["id"]], [row["id"] for row in messages])

    def test_failed_mail_flush_retains_hold_without_false_woke_and_releases_guard(self):
        self.f.arm_session(A)
        message = self.f.send("f2-flush-failure")
        class FailedFlush(io.StringIO):
            def flush(self):
                raise OSError("fixture output cannot flush")
        self.run_waiter(stdout=FailedFlush())
        self.assertEqual(1, len(self.holds()))
        self.assertEqual([message["id"]], self.holds()[0]["item_ids"])
        self.assertEqual([], self.rows("wakes", {"wake_attempt_receipt"}))
        self.f.arm_session(B)
        from floati.events import EventLog
        messages, receipt = EventLog(self.f.root).present(self.node)
        self.assertEqual([message["id"]], [row["id"] for row in messages])
        # The failed-flush waiter returned before B's direct presentation;
        # this call has no own live presenter testimony.
        self.assertEqual("delivered_unpresented", receipt["kind"])

    def test_throwing_publish_callback_releases_guard_and_keeps_historical_hold(self):
        self.f.arm_session(A)
        message = self.f.send("f2-publish-callback-failure")
        def failed(_artifact):
            raise OSError("fixture publication failed")
        with self.assertRaisesRegex(OSError, "fixture publication failed"):
            WakeHoldController(self.f.root).evaluate(
                self.node, idempotency_key="f2-throwing-publish", _publish=failed,
            )
        self.assertEqual(1, len(self.holds()))
        self.assertEqual([], self.rows("wakes", {"wake_attempt_receipt"}))
        self.f.arm_session(B)
        from floati.events import EventLog
        messages, _receipt = EventLog(self.f.root).present(self.node)
        self.assertEqual([message["id"]], [row["id"] for row in messages])

    def codex_fixture(self):
        self.f.arm_codex_role_node("f2-codex-seat")
        self.node = self.f.participant.binding.node_id
        executable = self.f.base / "inert-codex"
        executable.write_bytes(b"#!/bin/sh\nexit 99\n")
        executable.chmod(0o700)
        patcher = mock.patch.object(wake_daemon_adapters, "CODEX_EXECUTABLE", executable)
        self.addCleanup(patcher.stop)
        patcher.start()

    def startup_race(self, writer, *, fail_holder=False):
        self.codex_fixture()
        self.f.arm_session(A)
        real_holder = codex_wait_liveness.write_holder_testimony
        real_binding = wake_daemon_adapters.record_codex_daemon_binding
        def publish_b():
            real_holder(self.f.root, self.node, B)
            real_binding(self.f.participant, B)
        race = _TakeoverRace(self, after_arm=publish_b)
        self.addCleanup(race.collect)
        def holder(*args, **kwargs):
            if writer == "holder":
                race.launch()
            if fail_holder:
                raise OSError("private-holder-failure")
            return real_holder(*args, **kwargs)
        def binding(*args, **kwargs):
            if writer == "adapter":
                race.launch()
            return real_binding(*args, **kwargs)
        with mock.patch("floati.jsonl.fcntl.flock", side_effect=race.observed_flock),\
             mock.patch.object(codex_wait_liveness, "write_holder_testimony", holder),\
             mock.patch.object(wake_daemon_adapters, "record_codex_daemon_binding", binding):
            self.run_waiter()
            race.collect()
        self.assertIsNotNone(race.thread, "startup publication seam was never reached")
        self.assertEqual(B, self.current()["acting_session_id"])
        holder_path = codex_wait_liveness.holder_testimony_path(self.f.root, self.node)
        self.assertEqual(B, json.loads(holder_path.read_bytes())["session_id"],
                         "displaced A overwrote current B holder testimony")
        coordinate = DaemonCoordinate(self.f.root, self.node, "codex")
        self.assertEqual(B, AdapterBindingStore(self.f.root).read(coordinate)["session_id"],
                         "displaced A overwrote current B adapter binding")

    def test_holder_publication_cannot_overwrite_b_after_takeover(self):
        self.startup_race("holder")

    def test_adapter_publication_cannot_overwrite_b_after_takeover(self):
        self.startup_race("adapter")

    def test_holder_failure_does_not_release_guard_before_adapter_publication(self):
        self.startup_race("holder", fail_holder=True)

    def evidence_failure(self, target, operation):
        self.codex_fixture()
        self.f.arm_session(A)
        # Codex-role fixture has a different recipient from the baseline send helper.
        from floati.events import EventLog
        message = EventLog(self.f.root).send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/ping.md", "fresh", idempotency_key="f2-failure-" + operation)
        with mock.patch(target, side_effect=OSError("private-write-failure")):
            output, errors = self.run_waiter()
        self.assertEqual("block", json.loads(output)["decision"])
        self.assertIn(message["id"], json.loads(output)["reason"])
        self.assertIn(operation + ": OSError", errors)
        self.assertNotIn("private-write-failure", errors)
        self.assertEqual(["woke"], [row["outcome"] for row in self.rows("wakes", {"wake_attempt_receipt"})])

    def test_holder_write_failure_keeps_best_effort_mail_publication(self):
        self.evidence_failure("floati.codex_wait_liveness.write_holder_testimony", "holder_testimony")

    def test_adapter_write_failure_keeps_best_effort_mail_publication(self):
        self.evidence_failure("floati.wake_daemon_adapters.record_codex_daemon_binding", "daemon_binding")

    def test_private_hold_append_stays_refusal_only_after_lawful_evaluation(self):
        self.f.arm_session(A)
        message = self.f.send("f2-sealed-hold")
        controller = WakeHoldController(self.f.root)
        result = controller.evaluate(self.node, idempotency_key="f2-lawful-hold")
        self.assertEqual([message["id"]], result["receipt"]["item_ids"])
        before = self.f.bus_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            controller._append_receipt_already_guarded(self.node, None, result["receipt"])
        self.assertEqual("wake_controller_only", caught.exception.code)
        self.assertEqual(before, self.f.bus_bytes())

    def test_all_six_historical_exit_reasons_validate_runtime_and_old_new_schemas(self):
        for reason in OLD_REASONS:
            with self.subTest(reason=reason):
                row = WakeExitLedger(self.f.root).record(node_id=self.node,
                    session_digest=hashlib.sha256(A.encode()).hexdigest(), reason_code=reason,
                    waited_seconds=1, idempotency_key="f2-history-" + reason)
                self.assertEqual(row, validate_record(row, self.f.root.tenant_id,
                    {"wake_waiter_exit_receipt"}, integrity=False))
                validate_json_schema(row, OLD_SCHEMA)
                validate_json_schema(row, SCHEMA)
        self.assertEqual(list(OLD_REASONS), [row["reason_code"] for row in self.exits()])

    def test_governed_schema_widening_changes_only_the_reason_enum(self):
        self.assertEqual("f1f336dc88c163a6821762decf76b49ae4319aa9e71349e43fec708af662844d",
                         hashlib.sha256(OLD_SCHEMA.read_bytes()).hexdigest(),
                         "the pre-F2 historical schema bytes changed")
        # The integrity car landed before this car qualified. Freeze its exact
        # main schema too, so F2's only delta remains the supersession enum.
        self.assertEqual("3bdac04e0a8cf6f5409c170c6f2f9fb4398326b981d46ba5b45e09acb3b4d44a",
                         hashlib.sha256(MAIN_SCHEMA.read_bytes()).hexdigest(),
                         "the landed integrity schema bytes changed")
        expected = copy.deepcopy(json.loads(MAIN_SCHEMA.read_bytes()))
        self.assertEqual(list(OLD_REASONS), expected["properties"]["reason_code"]["enum"])
        expected["properties"]["reason_code"]["enum"].append("superseded_session")
        # NEEDS-YOU-OBSERVED-1 later widens the live schema by observed_item;
        # this car's delta is held by the schema as it stood before that row.
        self.assertEqual(expected, json.loads(
            (SCHEMA.parents[2] / "tests/fixtures/needs-you-wake-exit-v1-before.schema.json").read_bytes()))

    def test_superseded_exit_is_receiptable_and_validates_only_the_new_schema(self):
        try:
            row = WakeExitLedger(self.f.root).record(node_id=self.node,
                session_digest=hashlib.sha256(A.encode()).hexdigest(), reason_code="superseded_session",
                waited_seconds=0, idempotency_key="f2-new-superseded")
        except ProtocolRefusal as exc:
            self.fail("governed superseded_session must be receiptable: " + exc.code)
        validate_record(row, self.f.root.tenant_id, {"wake_waiter_exit_receipt"}, integrity=False)
        validate_json_schema(row, SCHEMA)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(row, OLD_SCHEMA)

    def test_unruled_exit_reason_still_refuses_without_mutation(self):
        before = self.f.bus_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            WakeExitLedger(self.f.root).record(node_id=self.node,
                session_digest=hashlib.sha256(A.encode()).hexdigest(), reason_code="unruled_reason",
                waited_seconds=0, idempotency_key="f2-unruled")
        self.assertEqual("wake_waiter_exit_reason_invalid", caught.exception.code)
        self.assertEqual(before, self.f.bus_bytes())

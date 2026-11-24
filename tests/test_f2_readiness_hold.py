"""Am12/14: real Stop holds qualify send readiness, without a daemon.

RED source only. The parent owns execution and banking before implementation.
No proposed product module is imported and no hold testimony is manufactured:
each observation happens inside a real waiter's injected sleep callback.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import tests  # Pin HOME before any product import resolves a default path.

from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import (
    CodexWaitConsentLedger,
    CodexWaitSessionLedger,
    resolve_participant,
)
from floati.cursor_wait import install_cursor_hook, run_cursor_stop_wait
from floati.events import EventLog
from floati.fixture_ids import builder
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_waiter_lifecycle import retire_waiter
from floati.waiter_consent import arm_waiter_consent
from floati.records import WAKE_HOLD_KINDS
from tests.temp_roots import REAL_TEMP_ROOT
from tests.waiter_timing import scripted_readiness


PRIVATE_NOTE = "PRIVATE_BODY_MUST_NOT_ENTER_HOLD_TESTIMONY"
DEADLINE_SECONDS = 2
HOOK_TIMEOUT_SECONDS = 1800


def assert_closed_claim_visibility(test, readiness, claim, hold_rows, root):
    """F2-STALE-CLAIMANT-DEAF-1 permits visible non-recovery, not guessed death.

    Pin evidence relationships without choosing a new recovery margin or enum.
    A completed native hold must reach the dispatcher's existing typed facts.
    """
    claim_path = root.tenant_home / CodexWaitSessionLedger._relative(claim["node_id"])
    hold_path = root.tenant_home / "receipts/stop-holds" / (claim["node_id"] + ".jsonl")
    required = {(str(claim_path), claim["id"]),
        *((str(hold_path), row["id"]) for row in hold_rows)}
    facts = readiness["mechanism_evidence"]
    witnessed = [fact for fact in facts
        if fact["mechanism"] in {"stop_hook", "seat_board"}
        and required <= {(source["path"], source["record_id"]) for source in fact["sources"]}]
    test.assertTrue(witnessed,
        "closed claimant must be an affirmative send-readiness fact citing "
        "its claim and native start/exit, rather than generic unknown: " + str(readiness))
    for fact in witnessed:
        test.assertIn(fact["state"], {"absent", "unknown"}, fact)
        test.assertNotIn(fact["reason"], {"hook_provenance_unavailable",
            "no_board_or_claim", "boarding_not_live", "mechanism_evidence_incomplete"}, fact)


class _Clock:
    """Advance only the waiter's clock; never sleep or start a child."""

    def __init__(self, callback):
        self.elapsed = 0.0
        self.epoch = time.time()
        self.callback = callback
        self.called = False

    def monotonic(self):
        return self.elapsed

    def wall_time(self):
        return self.epoch + self.elapsed

    def now(self):
        return datetime.fromtimestamp(self.wall_time(), timezone.utc)

    def utc_now(self):
        return self.now().isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def sleep(self, seconds):
        if not self.called:
            self.called = True
            self.callback()
        self.elapsed += seconds


class NativeStopHoldReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        codex_home = self.base / "codex-home"
        codex_home.mkdir()
        environment = mock.patch.dict(os.environ, {"CODEX_HOME": str(codex_home)})
        environment.start()
        self.addCleanup(environment.stop)
        # The native waiter may record a daemon binding as best-effort evidence.
        # Refuse that optional path before it can resolve a host Codex executable.
        executable = mock.patch("floati.wake_daemon_adapters.CODEX_EXECUTABLE",
                                self.base / "absent-codex")
        executable.start()
        self.addCleanup(executable.stop)

    def fixture(self, harness):
        home = self.base / harness.casefold()
        self.root = FloatiRoot.open_direct_home(home / "fleet", create=True)
        self.node = builder("readiness-" + harness.casefold())
        registry = Registry(self.root)
        registry.register("architect", "architect")
        registry.register(self.node, harness)
        self.workspace = home / "workspace"
        self.workspace.mkdir()
        self.session = harness.casefold() + "-holding-session"
        self.arm = arm_waiter_consent(self.root.path, self.workspace, self.node,
            harness, HOOK_TIMEOUT_SECONDS, DEADLINE_SECONDS)
        participant = resolve_participant(self.root.path, self.workspace)
        self.assertIsNotNone(participant, "fixture must resolve its real workspace map")
        self.binding = participant.binding
        self.consent = CodexWaitConsentLedger(self.root).require_armed(self.binding)
        self.assertEqual(self.arm["consent_receipt_id"], self.consent["id"])
        self.assertEqual("not_leased", registry.node_lease_state(self.node)["state"])
        self.events = EventLog(self.root)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.at_sleep = {}
        self.runtime = home / "runtime"
        if harness == "Cursor":
            scripts = self.runtime / "scripts"
            scripts.mkdir(parents=True)
            launcher = scripts / "floati"
            launcher.write_text("#!/bin/sh\nexit 31\n", encoding="utf-8")
            launcher.chmod(0o700)
            installed = install_cursor_hook(workspace=self.workspace,
                root=self.root.path, node=self.node, runtime=self.runtime,
                deadline_seconds=DEADLINE_SECONDS)
            config = json.loads(Path(installed["hooks_path"]).read_text(encoding="utf-8"))
            hook = config["hooks"]["stop"][0]
            self.assertEqual(HOOK_TIMEOUT_SECONDS, hook["timeout"])
            self.assertIn(str(self.root.path), hook["command"])
            self.assertIn(self.node, hook["command"])

    def hold_rows(self):
        return read_records_snapshot(self.root,
            Path("receipts/stop-holds") / (self.node + ".jsonl"),
            allowed_kinds={"stop_hold_receipt"})

    def send(self, key, *, now=None):
        clock = getattr(self, "_hold_clock", None)
        if now is None:
            now = clock.now() if clock is not None else datetime.now(timezone.utc)
        return self.events.send("architect", self.node, "floati", "a" * 40,
            "docs/evidence/readiness.md", PRIVATE_NOTE,
            idempotency_key=key, now=now)

    def run_hold(self, harness, action=None):
        def while_holding():
            # Capture before sending or invalidating, so the later assertions
            # cannot confuse a post-return receipt with evidence of a live hold.
            self.at_sleep["rows"] = self.hold_rows()
            self.at_sleep["stdout"] = self.stdout.getvalue()
            if action is not None:
                action()

        self.clock = _Clock(while_holding)
        # Hold testimony, send readiness, and native delivery classification
        # must share the same fixture instant. Physical startup cannot expire
        # a hold whose injected waiter clock has not advanced.
        with mock.patch.object(self, "_hold_clock", self.clock, create=True),\
                mock.patch("floati.events.utc_now", self.clock.utc_now):
            if harness in {"Codex", "ZCode"}:
                code = run_stop_waiter(bus_home=self.root.path,
                    hook_payload={"cwd": str(self.workspace), "session_id": self.session},
                    stdout=self.stdout, stderr=self.stderr,
                    monotonic=self.clock.monotonic, wait_readable=scripted_readiness((self.clock.sleep), interval=(1)), sleep=self.clock.sleep,
                    wall_time=self.clock.wall_time, poll_interval_seconds=1)
            else:
                # Cursor claimant authority comes from the hook process's actual
                # workspace, independently of observational payload coordinates.
                with mock.patch("floati.cursor_wait.Path.cwd", return_value=self.workspace):
                    code = run_cursor_stop_wait(root=self.root.path, node=self.node,
                        runtime=self.runtime, deadline_seconds=DEADLINE_SECONDS,
                        poll_seconds=1, hook_timeout_seconds=HOOK_TIMEOUT_SECONDS,
                        payload={"status": "completed", "conversation_id": self.session,
                                 "cwd": str(self.workspace)},
                        stdout=self.stdout, stderr=self.stderr,
                        clock=self.clock.monotonic, sleep=self.clock.sleep,
                        wait_readable=scripted_readiness(self.clock.sleep, interval=1),
                        # Watch actual event bytes rather than depending on filesystem
                        # timestamp resolution; keep the real native inbox reader.
                        ledger_mtime=lambda: (self.events.path.stat().st_size
                            if self.events.path.exists() else 0),
                        now_utc=self.clock.utc_now,
                        last_seen_root=self.workspace)
        self.assertEqual(0, code, self.stderr.getvalue())
        self.assertTrue(self.clock.called, "fixture must reach an empty native hold")
        self.assertEqual("", self.at_sleep["stdout"], "observation must precede publication")

    def assert_closed_hold(self):
        rows = self.hold_rows()
        self.assertEqual(2, len(rows), "one real invocation needs start and exit testimony")
        start, exit_row = rows
        self.assertEqual(["start", "exit"], [row["phase"] for row in rows])
        self.assertEqual([start], self.at_sleep["rows"],
                         "the start must already be durable while sleep is active")
        self.assertIsInstance(start["invocation_id"], str)
        self.assertTrue(start["invocation_id"])
        self.assertLessEqual(len(start["invocation_id"]), 128)
        self.assertEqual(start["invocation_id"], exit_row["invocation_id"])
        started = datetime.fromisoformat(start["started_at"].replace("Z", "+00:00"))
        self.assertIsNotNone(started.tzinfo)
        for row in rows:
            self.assertEqual("stop_hold_receipt", row["kind"])
            self.assertEqual(self.node, row["node_id"])
            self.assertEqual(self.session, row["acting_session_id"])
            self.assertEqual(self.consent["id"], row["consent_receipt_id"])
            self.assertEqual(start["started_at"], row["started_at"])
            self.assertEqual(DEADLINE_SECONDS, row["deadline_seconds"])
            self.assertFalse({"body", "note", "message", "messages", "fresh_messages"} & set(row),
                             "hold testimony must not retain message content")
        self.assertNotIn(PRIVATE_NOTE, json.dumps(rows))

    def assert_stop_hook_ready(self, sent):
        readiness = sent["recipient_readiness"]
        self.assertEqual("recipient_wake_arranged", readiness.get("state"), readiness)
        self.assertEqual("stop_hook", readiness.get("reason"), readiness)

    def assert_no_stop_hook_witness(self, sent):
        readiness = sent["recipient_readiness"]
        self.assertEqual("unknown", readiness.get("state"), readiness)

    def readiness_during_mail_hold(self, harness):
        self.fixture(harness)
        self.run_hold(harness, lambda: self.at_sleep.update(sent=self.send("during-hold")))
        self.assert_stop_hook_ready(self.at_sleep["sent"])
        self.assert_closed_hold()

    def mail_closes_hold(self, harness):
        self.fixture(harness)
        self.run_hold(harness, lambda: self.at_sleep.update(sent=self.send("close-on-mail")))
        body = json.loads(self.stdout.getvalue())
        if harness == "Codex":
            self.assertEqual("block", body.get("decision"), body)
            self.assertIn(self.at_sleep["sent"]["message"]["id"], body["reason"])
        else:
            self.assertIn("unread mail", body.get("followup_message", ""), body)
        self.assert_closed_hold()

    def deadline_closes_hold(self, harness):
        self.fixture(harness)
        self.run_hold(harness)
        self.assertEqual(DEADLINE_SECONDS, self.clock.elapsed)
        body = json.loads(self.stdout.getvalue())
        if harness == "Codex":
            self.assertIn("deadline exhausted", body.get("reason", ""), body)
        else:
            self.assertIn("no mail", body.get("followup_message", ""), body)
        self.assert_closed_hold()

    def test_codex_active_empty_hold_qualifies_send_without_lease_or_daemon(self):
        """Catches a send saying deaf while the exact native Stop hook is holding."""
        self.readiness_during_mail_hold("Codex")

    def test_closed_codex_claim_non_recovery_is_cited_by_send_readiness(self):
        """RED: affirmative closure cannot silently strand a different chat."""
        self.fixture("Codex")
        self.run_hold("Codex")
        self.assert_closed_hold()
        holds = self.hold_rows()
        claim_path = CodexWaitSessionLedger._relative(self.node)
        claims = read_records_snapshot(self.root, claim_path,
            allowed_kinds={"codex_wait_session_receipt"})
        self.assertEqual(self.session, claims[-1]["acting_session_id"])
        # Observe after the native invocation's modeled deadline, not before
        # its exit timestamp. This is not a chosen auto-recovery margin.
        observed = datetime.fromisoformat(holds[-1]["timestamp"].replace("Z", "+00:00")) + timedelta(seconds=1)
        fresh = self.send("closed-claim-fresh", now=observed)
        ack_path = Path("receipts/acks") / (self.node + ".jsonl")
        acknowledgments = read_records_snapshot(self.root, ack_path, allowed_kinds={"ack_receipt"})
        output, errors = io.StringIO(), io.StringIO()
        code = run_stop_waiter(bus_home=self.root.path,
            hook_payload={"cwd": str(self.workspace), "session_id": "codex-successor"},
            stdout=output, stderr=errors, wall_time=lambda: observed.timestamp())
        self.assertEqual(0, code, errors.getvalue())
        self.assertEqual("", output.getvalue(), "non-recovery must not publish to an unauthorized successor")
        self.assertEqual("", errors.getvalue())
        self.assertEqual(claims, read_records_snapshot(self.root, claim_path,
            allowed_kinds={"codex_wait_session_receipt"}))
        deliveries = read_records_snapshot(self.root,
            Path("receipts/deliveries") / (self.node + ".jsonl"),
            allowed_kinds=set(WAKE_HOLD_KINDS))
        self.assertEqual([], deliveries)
        self.assertEqual(acknowledgments, read_records_snapshot(self.root, ack_path,
            allowed_kinds={"ack_receipt"}))
        self.assertTrue(fresh["message"]["id"])
        readiness = self.send("closed-claim-dispatcher", now=observed)["recipient_readiness"]
        assert_closed_claim_visibility(self, readiness, claims[-1], holds, self.root)

    def test_live_codex_hold_keeps_successor_out_and_send_ready(self):
        """Control: a genuinely holding claimant keeps its authority."""
        self.fixture("Codex")
        def contender():
            claims = read_records_snapshot(self.root, CodexWaitSessionLedger._relative(self.node),
                allowed_kinds={"codex_wait_session_receipt"})
            output, errors = io.StringIO(), io.StringIO()
            code = run_stop_waiter(bus_home=self.root.path,
                hook_payload={"cwd": str(self.workspace), "session_id": "codex-contender"},
                stdout=output, stderr=errors)
            self.assertEqual((0, "", ""), (code, output.getvalue(), errors.getvalue()))
            self.assertEqual(claims, read_records_snapshot(self.root, CodexWaitSessionLedger._relative(self.node),
                allowed_kinds={"codex_wait_session_receipt"}))
            self.at_sleep["sent"] = self.send("live-claim-fresh")
        self.run_hold("Codex", contender)
        self.assert_stop_hook_ready(self.at_sleep["sent"])
        self.assert_closed_hold()

    def test_cursor_active_empty_hold_qualifies_send_with_exact_shared_consent(self):
        """Catches a Cursor hold lacking recipient-bound consent/readiness testimony."""
        self.readiness_during_mail_hold("Cursor")

    def test_zcode_native_hold_uses_one_shared_writer_and_exact_harness(self):
        """Catches a bridge/native double start or Codex-label fallback for ZCode."""
        self.fixture("ZCode")
        self.run_hold("ZCode", lambda: self.at_sleep.update(sent=self.send("zcode-native")))
        body = json.loads(self.stdout.getvalue())
        self.assertEqual("block", body.get("decision"), body)
        self.assertIn(self.at_sleep["sent"]["message"]["id"], body["reason"])
        self.assert_closed_hold()
        self.assertEqual(["zcode", "zcode"], [row["harness"] for row in self.hold_rows()])
        self.assert_stop_hook_ready(self.at_sleep["sent"])

    def test_codex_mail_publication_closes_the_same_hold_invocation(self):
        """Catches retaining a live start after the waiter emits a real mail decision."""
        self.mail_closes_hold("Codex")

    def test_cursor_mail_publication_closes_the_same_hold_invocation(self):
        """Catches recording only a Cursor exit with no durable corresponding start."""
        self.mail_closes_hold("Cursor")

    def test_codex_deadline_closes_empty_hold_instead_of_leaving_ready_start(self):
        """Catches advertising the last invocation after its no-mail deadline ends."""
        self.deadline_closes_hold("Codex")

    def test_cursor_deadline_closes_empty_hold_instead_of_leaving_ready_start(self):
        """Catches treating a configured Cursor hook as an unbounded live hold."""
        self.deadline_closes_hold("Cursor")

    def test_codex_takeover_invalidates_old_hold_before_its_exit_is_written(self):
        """Catches trusting A's unmatched start after an actual native B takeover."""
        self.fixture("Codex")
        def take_over_and_send():
            self.at_sleep["takeover"] = CodexWaitSessionLedger(self.root).arm(
                self.binding, self.consent, "codex-successor-session",
                idempotency_key="takeover-during-hold", take_over=True)
            self.at_sleep["sent"] = self.send("after-native-takeover")
        self.run_hold("Codex", take_over_and_send)
        self.assertEqual("codex-successor-session", self.at_sleep["takeover"]["acting_session_id"])
        self.assert_no_stop_hook_witness(self.at_sleep["sent"])
        self.assert_closed_hold()

    def test_codex_withdrawal_invalidates_old_hold_before_its_exit_is_written(self):
        """Catches a retained start surviving real consent disarm and map removal."""
        self.fixture("Codex")
        def withdraw_and_send():
            self.at_sleep["withdrawal"] = retire_waiter(self.root, self.node,
                self.workspace, "withdraw-during-hold")
            self.at_sleep["sent"] = self.send("after-native-withdrawal")
        self.run_hold("Codex", withdraw_and_send)
        self.assertEqual("disarmed", self.at_sleep["withdrawal"]["state"])
        self.assert_no_stop_hook_witness(self.at_sleep["sent"])
        self.assert_closed_hold()

    def test_codex_closed_deadline_hold_cannot_qualify_later_send(self):
        """Catches a reader using historical start evidence despite its matching exit."""
        self.fixture("Codex")
        self.run_hold("Codex")
        self.assert_no_stop_hook_witness(self.send("after-codex-exit"))
        self.assert_closed_hold()

    def test_cursor_closed_deadline_hold_cannot_qualify_later_send(self):
        """Catches configured hooks plus historical start being mistaken for a live wait."""
        self.fixture("Cursor")
        self.run_hold("Cursor")
        self.assert_no_stop_hook_witness(self.send("after-cursor-exit"))
        self.assert_closed_hold()

    def evidence_fault_preserves_mail(self, harness, phase):
        """Fault the actual JSONL encoding boundary, not a waiter decision."""
        from floati import jsonl

        self.fixture(harness)
        encode = jsonl._encode_record
        def fail_hold_encode(row, *args, **kwargs):
            if row.get("kind") == "stop_hold_receipt" and row.get("phase") == phase:
                raise OSError("scratch hold evidence write fault")
            return encode(row, *args, **kwargs)
        with mock.patch("floati.jsonl._encode_record", side_effect=fail_hold_encode):
            self.run_hold(harness, lambda: self.at_sleep.update(sent=self.send("fault-mail")))
        body = json.loads(self.stdout.getvalue())
        if harness == "Codex":
            self.assertEqual("block", body.get("decision"), body)
            self.assertIn(self.at_sleep["sent"]["message"]["id"], body["reason"])
        else:
            self.assertIn("unread mail", body.get("followup_message", ""), body)
        self.assertIn("stop_hold_" + phase, self.stderr.getvalue())
        rows = self.hold_rows()
        self.assertEqual([] if phase == "start" else ["start"],
                         [row["phase"] for row in rows],
                         "failed testimony must not fabricate its missing phase")
        if phase == "exit":
            # A failed close cannot publish nonexistent invalidation. Its old
            # start still has a finite original budget, never a renewed one.
            later = self.send("after-failed-close-deadline",
                now=datetime.now(timezone.utc) + timedelta(seconds=DEADLINE_SECONDS + 1))
            self.assert_no_stop_hook_witness(later)

    def test_codex_failed_start_write_preserves_mail_without_fabricated_exit(self):
        cases = (("Codex", "start"), ("Cursor", "start"), ("Codex", "exit"), ("Cursor", "exit"))
        for index, case in enumerate(cases):
            harness, phase = case
            with self.subTest(harness=harness, phase=phase):
                if index:
                    self.doCleanups()
                    self.setUp()
                self.evidence_fault_preserves_mail(harness, phase)

    def closure_precedes_output(self, harness):
        self.fixture(harness)
        published = []
        fixture = self
        class CapturingOutput(io.StringIO):
            def write(self, value):
                published.append(fixture.hold_rows())
                return super().write(value)
        self.stdout = CapturingOutput()
        self.run_hold(harness, lambda: self.at_sleep.update(sent=self.send("before-output")))
        self.assert_closed_hold()
        self.assertTrue(published)
        self.assertTrue(all([row["phase"] for row in rows] == ["start", "exit"]
                            for rows in published), "exit must be durable before output reaches the harness")

    def test_codex_closes_hold_before_first_mail_output_write(self):
        self.closure_precedes_output("Codex")

    def test_cursor_closes_hold_before_first_mail_output_write(self):
        self.closure_precedes_output("Cursor")

    def test_codex_integrity_continuation_closes_original_hold_before_output(self):
        """Catches a post-start evidence refusal leaving the healthy start unmatched."""
        self.fixture("Codex")
        self.run_hold("Codex", lambda: self.events.path.write_bytes(b'{"broken":true}\n'))
        body = json.loads(self.stdout.getvalue())
        self.assertEqual("block", body.get("decision"), body)
        self.assertIn("integrity refusal", body.get("reason", ""), body)
        self.assert_closed_hold()

    def test_unsupported_legacy_role_keeps_continuation_without_new_stderr(self):
        """Catches labeling intentional testimony nonparticipation as a write fault."""
        self.fixture("worker")
        self.run_hold("Codex", lambda: self.at_sleep.update(sent=self.send("legacy-role")))
        body = json.loads(self.stdout.getvalue())
        self.assertEqual("block", body.get("decision"), body)
        self.assertIn(self.at_sleep["sent"]["message"]["id"], body["reason"])
        self.assertEqual("", self.stderr.getvalue())
        self.assertEqual([], self.hold_rows())

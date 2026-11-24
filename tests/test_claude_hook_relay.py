"""Claude hook context shares native inbox and conversation presentation truth.

Installation belongs in test_claude_hook_install; this module exercises the
new relay boundary without installing or invoking a live Claude session.
"""
import io
import itertools
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from floati import fixture_ids
from floati.codex_wait_contract import CodexWaitConsentLedger, resolve_participant
from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.waiter_consent import arm_waiter_consent
from tests.temp_roots import REAL_TEMP_ROOT
from tests.waiter_timing import scripted_readiness


class ClaudeHookRelayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node, self.sender = fixture_ids.builder("a"), fixture_ids.builder("b")
        Registry(self.root).register(self.node, "claude")
        Registry(self.root).register(self.sender, "claude")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        prior_cwd = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, prior_cwd)
        arm_waiter_consent(self.root.path, self.workspace, self.node, "claude", 10, 2)
        self.session = "claude-relay-scratch"
        self.now = [100.0]

    def send(self, key="one", note="exact scratch mail"):
        return EventLog(self.root).send(self.sender, self.node, "floati", "a" * 40,
            "docs/CONFLUENCE-v0.md", note, idempotency_key=key)["message"]

    def relay(self, event="PostToolUse", **payload):
        from floati.claude_hook_relay import run_relay
        output = io.StringIO()
        result = run_relay(bus_home=self.root.path, node=self.node,
            runtime=Path(__file__).resolve().parents[1],
            hook_payload={"cwd": str(self.workspace), "session_id": self.session,
                          "hook_event_name": event, **payload},
            stdout=output, clock=lambda: self.now[0])
        self.assertEqual(0, result)
        return output.getvalue()

    def stop(self):
        from floati.codex_wait import run_stop_waiter
        clock = [0.0]
        output = io.StringIO()
        self.assertEqual(0, run_stop_waiter(bus_home=self.root.path,
            hook_payload={"cwd": str(self.workspace), "session_id": self.session,
                          "hook_event_name": "Stop"},
            stdout=output, stderr=io.StringIO(), monotonic=lambda: clock[0],
            wait_readable=scripted_readiness(
                lambda seconds: clock.__setitem__(0, clock[0] + seconds), interval=1.0),
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
        return output.getvalue()

    def deliveries(self):
        return read_records_snapshot(self.root,
            SparseCursor(self.root)._delivery_relative_path_for(self.node),
            allowed_kinds={"delivery_receipt", "delivered_unpresented", "wake_hold_receipt"})

    def test_startup_and_tool_context_include_whole_envelope_and_ack_remains_explicit(self):
        from floati.fleet_presence import set_presence

        set_presence(self.root, self.sender, "AWAY", "overnight")
        for event in ("SessionStart", "PostToolUse", "UserPromptSubmit"):
            with self.subTest(event=event):
                message = self.send(event)
                self.now[0] += 31
                output = self.relay(event)
                self.assertTrue(output, "pending native mail needs hook context")
                body = json.loads(output)["hookSpecificOutput"]
                self.assertEqual(event, body["hookEventName"])
                self.assertIn("AWAY", body["additionalContext"].splitlines()[0])
                self.assertIn(json.dumps(message, sort_keys=True), body["additionalContext"])
                self.assertLessEqual(len(body["additionalContext"]), 10000)
                self.assertNotIn(message["id"], SparseCursor(self.root).acked_ids(self.node))
                self.assertEqual("delivery_receipt", self.deliveries()[-1]["kind"])
                SparseCursor(self.root).ack(self.node, [message["id"]], acting_session_id=self.session)

    def test_relay_then_stop_and_stop_then_relay_present_once(self):
        actions = {"relay": self.relay, "stop": self.stop,
                   "doorbell": lambda: self.relay("UserPromptSubmit")}
        for order in itertools.permutations(actions):
            with self.subTest(order=order):
                message = self.send("-".join(order))
                self.now[0] += 31
                outputs = [actions[action]() for action in order]
                self.assertIn(message["id"], outputs[0])
                for output in outputs[1:]:
                    self.assertNotIn(json.dumps(message, sort_keys=True), output)
                receipts = [row for row in self.deliveries() if message["id"] in row["item_ids"]]
                self.assertEqual(1, len(receipts))

    def test_concurrent_tool_hooks_present_one_context(self):
        message = self.send()
        with ThreadPoolExecutor(max_workers=4) as executor:
            outputs = list(executor.map(lambda _: self.relay(), range(4)))
        self.assertEqual(1, sum(message["id"] in output for output in outputs))
        self.assertEqual(1, len(self.deliveries()))

    def test_concurrent_prompt_hooks_present_one_context(self):
        message = self.send()
        with ThreadPoolExecutor(max_workers=4) as executor:
            outputs = list(executor.map(lambda _: self.relay("UserPromptSubmit"), range(4)))
        self.assertEqual(1, sum(message["id"] in output for output in outputs))
        self.assertEqual(1, len(self.deliveries()))

    def test_doorbell_prompt_presents_changed_mail_without_tool_throttle(self):
        self.relay("SessionStart")
        message = self.send()
        self.now[0] += 1
        self.assertIn(message["id"], self.relay("UserPromptSubmit"))
        self.assertEqual("", self.relay("UserPromptSubmit"))

    def test_unchanged_gate_reads_no_ledger_and_writes_nothing(self):
        self.assertEqual("", self.relay())
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.root.path.rglob("*") if path.is_file()}
        original = Path.open
        original_os_open = os.open
        def open_without_ledgers(path, *args, **kwargs):
            if path.suffix == ".jsonl":
                self.fail("gated invocation opened a ledger")
            return original(path, *args, **kwargs)
        def os_open_without_ledgers(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and str(path).endswith(".jsonl"):
                self.fail("gated invocation opened a ledger descriptor")
            return original_os_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", open_without_ledgers),\
                mock.patch.object(os, "open", os_open_without_ledgers):
            self.assertEqual("", self.relay())
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.root.path.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        marks = list((self.root.path / "state/claude-wait" / self.node).rglob("relay-*.json"))
        self.assertEqual(1, len(marks))
        self.assertEqual(0o600, marks[0].stat().st_mode & 0o777)

    def test_changed_ledger_is_throttled_but_stop_delivers_deferred_mail(self):
        self.relay()
        message = self.send()
        self.now[0] += 29
        self.assertEqual("", self.relay())
        self.assertIn(message["id"], self.stop())
        self.now[0] += 1
        self.assertEqual("", self.relay())

    def test_changing_cwd_within_the_seat_cannot_bypass_the_session_throttle(self):
        self.relay()
        message = self.send()
        child = self.workspace / "child"
        child.mkdir()
        self.now[0] += 1
        self.assertEqual("", self.relay(cwd=str(child)))
        self.assertIn(message["id"], self.stop())

    def test_pending_epoch_refuses_before_native_presentation_or_claim(self):
        from floati.bus_epoch import EPOCH_MARKER_PREFIX, EPOCH_MARKER_SUFFIX
        from floati.errors import ProtocolRefusal
        from floati.codex_wait_contract import CodexWaitSessionLedger
        self.send()
        marker = self.root.path / (EPOCH_MARKER_PREFIX + "scratch" + EPOCH_MARKER_SUFFIX)
        marker.write_text("{}\n")
        with self.assertRaises(ProtocolRefusal) as caught:
            self.relay()
        self.assertEqual("epoch_roll_recovery_required", caught.exception.code)
        self.assertFalse((self.root.path / "receipts/cursor-presentations").exists())
        self.assertFalse((self.root.path / CodexWaitSessionLedger._relative(self.node)).exists())
        self.assertEqual([], self.deliveries())

    def test_nonordinary_watermark_refuses_without_opening_a_blocking_stream(self):
        from floati.errors import ProtocolRefusal
        self.relay()
        mark = next((self.root.path / "state/claude-wait" / self.node).glob("relay-*.json"))
        mark.unlink()
        os.mkfifo(mark, 0o600)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.relay()
        self.assertEqual("claude_relay_state_invalid", caught.exception.code)

    def test_pause_and_foreign_session_keep_mail_on_the_stop_path(self):
        from floati.wake_control import WakeController
        from floati.codex_wait_contract import CodexWaitSessionLedger
        self.send()
        participant = resolve_participant(self.root.path, self.workspace)
        consent = CodexWaitConsentLedger(self.root).require_armed(participant.binding)
        CodexWaitSessionLedger(self.root).arm(participant.binding, consent, "predecessor",
            idempotency_key="fixture-predecessor")
        self.assertEqual("", self.relay())
        self.assertEqual([], self.deliveries())
        self.session = "predecessor"
        WakeController(self.root).pause(self.node, self.session, idempotency_key="fixture-pause")
        self.assertEqual("", self.relay())
        self.assertEqual([], self.deliveries())

    def test_interval_validation_and_boundary(self):
        from floati.errors import ProtocolRefusal
        for value in ("4", "3601", "nan", "no", "30.5"):
            with self.subTest(value=value), mock.patch.dict(os.environ,
                    {"FLOATI_CLAUDE_RELAY_INTERVAL_SECONDS": value}):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.relay()
                self.assertEqual("claude_relay_interval_invalid", caught.exception.code)
        with mock.patch.dict(os.environ, {"FLOATI_CLAUDE_RELAY_INTERVAL_SECONDS": "5"}):
            self.relay()
            message = self.send()
            self.now[0] += 4
            self.assertEqual("", self.relay())
            self.now[0] += 1
            self.assertIn(message["id"], self.relay())

    def test_full_read_uses_the_shared_inbox_reader(self):
        self.send()
        reader = EventLog._read_inbox_already_guarded
        with mock.patch.object(EventLog, "_read_inbox_already_guarded", autospec=True,
                               side_effect=reader) as shared:
            self.assertTrue(self.relay())
        self.assertEqual(1, shared.call_count)

    def test_cli_prints_only_hook_json_then_silence_on_unchanged_events(self):
        message = self.send()
        source = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=str(source), PYTHONDONTWRITEBYTECODE="1")
        options = ["hook", "relay", "--harness", "claude", "--root", str(self.root.path),
                "--as", self.node, "--runtime", str(source)]
        payload = json.dumps({"cwd": str(self.workspace), "session_id": self.session,
                              "hook_event_name": "SessionStart"})
        for index in range(2):
            with self.subTest(invocation=index):
                argv = ([str(source / "scripts/floati")] if index == 0 else
                        ["/usr/bin/python3", "-B", "-m", "floati"]) + options
                result = subprocess.run(argv, input=payload, text=True, capture_output=True,
                    env=environment, cwd=self.workspace, timeout=10)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("", result.stderr)
                if index == 0:
                    self.assertIn(message["id"], json.loads(result.stdout)
                        ["hookSpecificOutput"]["additionalContext"])
                else:
                    self.assertEqual("", result.stdout)

    def test_subagent_and_unsupported_events_do_not_consume_main_mail(self):
        message = self.send()
        for payload in ({"agent_id": "subagent"}, {"agent_id": "teammate", "agent_type": "Explore"}):
            with self.subTest(payload=payload):
                self.assertEqual("", self.relay(**payload))
        self.assertEqual("", self.relay("PreToolUse"))
        self.assertIn(message["id"], self.relay("SessionStart"))

    def test_context_limit_defers_whole_envelopes_to_stop(self):
        messages = [self.send(str(i), note="x" * 1024) for i in range(10)]
        output = self.relay()
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context), 10000)
        selected = [message for message in messages if message["id"] in context]
        self.assertTrue(selected)
        self.assertLess(len(selected), len(messages))
        for message in selected:
            self.assertIn(json.dumps(message, sort_keys=True), context)
        stop = self.stop()
        for message in messages[len(selected):]:
            self.assertIn(message["id"], stop)
        for message in selected:
            self.assertNotIn(message["id"], stop)

    def test_failed_flush_is_not_delivery_and_never_repeats_automatically(self):
        from floati.claude_hook_relay import run_relay
        message = self.send()
        class Broken(io.StringIO):
            def flush(self):
                raise OSError("constructed flush failure")
        with self.assertRaises(OSError):
            run_relay(bus_home=self.root.path, node=self.node,
                runtime=Path(__file__).resolve().parents[1],
                hook_payload={"cwd": str(self.workspace), "session_id": self.session,
                              "hook_event_name": "PostToolUse"},
                stdout=Broken(), clock=lambda: self.now[0])
        self.assertEqual([], self.deliveries())
        stopped = self.stop()
        self.assertIn("presentation_uncertain", stopped)
        self.assertNotIn(json.dumps(message, sort_keys=True), stopped)
        self.assertEqual([], self.deliveries())

    def test_post_flush_native_failure_keeps_one_json_context_and_uncertain_reservation(self):
        from floati.claude_hook_relay import main
        from floati.errors import DurabilityFailure
        message = self.send()
        output, error = io.StringIO(), io.StringIO()
        payload = {"cwd": str(self.workspace), "session_id": self.session,
                   "hook_event_name": "SessionStart"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                mock.patch("sys.stdout", output), mock.patch("sys.stderr", error),\
                mock.patch("floati.cursor_presentations.CursorPresentationLedger.flushed",
                    side_effect=DurabilityFailure("constructed_terminal_failure", "fixture")):
            self.assertEqual(0, main(["--harness", "claude", "--root", str(self.root.path),
                "--as", self.node, "--runtime", str(Path(__file__).resolve().parents[1])]))
        self.assertIn(message["id"], json.loads(output.getvalue())
            ["hookSpecificOutput"]["additionalContext"])
        self.assertEqual("constructed_terminal_failure", json.loads(error.getvalue())["code"])
        self.assertEqual(1, len(self.deliveries()))
        stopped = self.stop()
        self.assertIn("presentation_uncertain", stopped)
        self.assertNotIn(json.dumps(message, sort_keys=True), stopped)
        self.assertEqual(1, len(self.deliveries()))

    def test_guard_cleanup_failure_after_flush_never_appends_another_stdout_artifact(self):
        from contextlib import contextmanager
        from floati.car_fence import car_acceptance_guard
        from floati.claude_hook_relay import main
        from floati.errors import DurabilityFailure
        message = self.send()
        @contextmanager
        def failed_exit(root):
            with car_acceptance_guard(root) as fence:
                yield fence
                raise DurabilityFailure("constructed_guard_exit_failure", "fixture")
        output, error = io.StringIO(), io.StringIO()
        payload = {"cwd": str(self.workspace), "session_id": self.session,
                   "hook_event_name": "SessionStart"}
        with mock.patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                mock.patch("sys.stdout", output), mock.patch("sys.stderr", error),\
                mock.patch("floati.car_fence.car_acceptance_guard", failed_exit):
            self.assertEqual(0, main(["--harness", "claude", "--root", str(self.root.path),
                "--as", self.node, "--runtime", str(Path(__file__).resolve().parents[1])]))
        self.assertIn(message["id"], json.loads(output.getvalue())
            ["hookSpecificOutput"]["additionalContext"])
        self.assertEqual("constructed_guard_exit_failure", json.loads(error.getvalue())["code"])
        self.assertEqual(1, len(self.deliveries()))
        stopped = self.stop()
        self.assertIn("held_unacked", stopped)
        self.assertNotIn(json.dumps(message, sort_keys=True), stopped)
        self.assertEqual(1, len(self.deliveries()))


if __name__ == "__main__":
    unittest.main()

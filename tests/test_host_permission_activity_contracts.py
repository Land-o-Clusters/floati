"""Installed native activity shapes invalidate permission observations safely."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from floati.events import EventLog
from tests import test_host_permission_observations as fixture
from tests.readiness import run_process


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "tests/fixtures/f2-seat-stall-host-1/codex-activity-contracts.json"
SESSION_END_LAUNCHER = ROOT / "scripts/floati-codex-wait"


class HostPermissionActivityContractTests(unittest.TestCase):
    setUp = fixture.HostPermissionObservationsTests.setUp
    observer = fixture.HostPermissionObservationsTests.observer
    payload = fixture.HostPermissionObservationsTests.payload
    observe = fixture.HostPermissionObservationsTests.observe
    publish = fixture.HostPermissionObservationsTests.publish

    def activity(self, event):
        schema = json.loads(CONTRACTS.read_text())["events"][event]["schema"]
        values = {**self.fixture.payload(0, "successful output must not persist"),
            "hook_event_name": event, "turn_id": "later-turn",
            "source": "resume", "reason": "other", "trigger": "auto",
            "agent_id": "native-child", "agent_type": "fixture-agent",
            "agent_transcript_path": None, "last_assistant_message": "private assistant text",
            "stop_hook_active": False, "prompt": "private submitted prompt"}
        return {key: values[key] for key in schema["required"]}

    def test_embedded_contract_fixtures_keep_the_measured_exact_schema_pins(self):
        contracts = json.loads(CONTRACTS.read_text())
        self.assertEqual(12, len(contracts["events"]))
        for event, entry in contracts["events"].items():
            with self.subTest(event=event):
                data = json.dumps(entry["schema"], indent=2, sort_keys=True).encode()
                self.assertEqual(entry["length"], len(data))
                self.assertEqual(entry["sha256"], hashlib.sha256(data).hexdigest())
                self.assertFalse(entry["schema"]["additionalProperties"])
                self.assertEqual(event, entry["schema"]["properties"]["hook_event_name"]["const"])

    def test_all_supported_later_native_shapes_clear_without_invented_common_fields(self):
        for event in ("SessionStart", "SessionEnd", "PreCompact", "PostCompact",
                      "PreToolUse", "PostToolUse", "UserPromptSubmit",
                      "SubagentStart", "SubagentStop", "Stop"):
            with self.subTest(event=event):
                occurrence = self.observe()
                payload = self.activity(event)
                self.observe(payload, self.now + timedelta(seconds=1))
                self.assertIsNone(self.publish(occurrence, 900), event)
        self.assertEqual([], EventLog(self.root).event_records())

    def test_successful_tool_entrypoint_clears_before_its_refusal_filter(self):
        from floati.codex_tool_hook import run_tool_hook
        occurrence = self.observe()
        self.assertEqual(0, run_tool_hook(bus_home=self.root.path,
            hook_payload=self.activity("PostToolUse")))
        self.assertIsNone(self.publish(occurrence, 900))
        self.assertEqual([], EventLog(self.root).event_records())

    def test_stop_entrypoint_clears_before_entering_its_native_waiter(self):
        from floati.codex_wait import run_stop_waiter
        occurrence = self.observe()
        self.assertEqual(0, run_stop_waiter(bus_home=self.root.path,
            hook_payload=self.activity("Stop"), stdout=io.StringIO(), stderr=io.StringIO(),
            sleep=lambda seconds: None, poll_interval_seconds=0))
        self.assertIsNone(self.publish(occurrence, 900))
        self.assertEqual([], EventLog(self.root).event_records())

    def test_session_end_native_launcher_clears_without_turn_id_or_waiting(self):
        occurrence = self.observe()
        payload = self.activity("SessionEnd")
        self.assertNotIn("turn_id", payload)
        child = run_process([sys.executable, "-B", str(SESSION_END_LAUNCHER),
            "--root", str(self.root.path)], "session-end launcher exits",
            input=json.dumps(payload), text=True, capture_output=True, check=False)
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertEqual("", child.stdout)
        self.assertIsNone(self.publish(occurrence, 900))

    def test_malformed_and_foreign_activity_cannot_clear_a_native_occurrence(self):
        occurrence = self.observe()
        malformed = self.activity("SessionEnd")
        del malformed["reason"]
        self.assertIsNone(self.observe(malformed))
        forged = dict(self.activity("SessionEnd"), node_id="builder")
        self.assertIsNone(self.observe(forged))
        foreign = dict(self.activity("SessionEnd"), session_id="foreign-session")
        self.assertIsNone(self.observe(foreign))
        self.assertIsNotNone(self.publish(occurrence, 900))

    def test_observation_storage_never_retains_host_content(self):
        self.observe()
        for event in ("PostToolUse", "UserPromptSubmit", "SubagentStop"):
            self.observe(self.activity(event), self.now + timedelta(seconds=1))
        directory = self.root.tenant_home / "state/host-observations"
        files = [path for path in directory.rglob("*") if path.is_file()]
        self.assertTrue(files, "native observation did not retain its bounded sequence state")
        for path in files:
            text = path.read_text()
            self.assertNotIn("private assistant text", text)
            self.assertNotIn("private submitted prompt", text)
            self.assertNotIn("successful output must not persist", text)
            self.assertNotIn("codex-fleet-bus fixture-profile", text)

    def test_deleted_state_cannot_reset_sequence_and_revive_an_old_occurrence(self):
        occurrence = self.observe()
        directory = self.root.tenant_home / "state/host-observations"
        files = [path for path in directory.rglob("*") if path.is_file()]
        self.assertTrue(files)
        for path in files:
            path.unlink()
        self.assertIsNone(self.observe(at=self.now + timedelta(seconds=1)),
            "existing observation storage with missing state must fail closed")
        self.assertIsNone(self.publish(occurrence, 901))
        self.assertEqual([], EventLog(self.root).event_records())

    def test_symlinked_observation_parent_cannot_publish_or_write_through_alias(self):
        occurrence = self.observe()
        directory = self.root.tenant_home / "state/host-observations"
        renamed = directory.with_name("host-observations-original")
        directory.rename(renamed)
        directory.symlink_to(renamed, target_is_directory=True)
        self.assertIsNone(self.observe(at=self.now + timedelta(seconds=1)))
        self.assertIsNone(self.publish(occurrence, 901))
        self.assertEqual([], EventLog(self.root).event_records())


    def test_unhashable_event_metadata_is_silent_at_both_native_wait_entrypoints(self):
        from floati.cli import main
        occurrence = self.observe()
        for event in ([], {}):
            with self.subTest(case=event):
                payload = dict(self.activity("SessionEnd"), hook_event_name=event)
                stdout, stderr = io.StringIO(), io.StringIO()
                # The installed shim forwards to this same CLI entrypoint;
                # the SessionEnd test above retains its real launcher check.
                with patch("sys.stdin", io.StringIO(json.dumps(payload))),\
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    result = main(["wait", "--for", "fresh-work", "--root", str(self.root.path)])
                self.assertEqual(0, result, stderr.getvalue())
                self.assertEqual("", stdout.getvalue())
                self.assertEqual("", stderr.getvalue())
        self.assertIsNotNone(self.publish(occurrence, 900))


if __name__ == "__main__":
    unittest.main()

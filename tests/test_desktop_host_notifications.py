"""Native desktop notifications must publish once without inventing turn IDs."""
import json
import hashlib
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from io import StringIO
import subprocess
import unittest

import tests
from floati.codex_wait_contract import CodexWaitSessionLedger, resolve_participant
from floati.desktop_host_notifications import observe_desktop_notification
from floati.events import EventLog
from floati.registry import Registry
from floati.seat_execution import SeatExecutionController
from floati.wake_control import WakeController
from tests import test_seat_execution_host_tool_native as fixture
from tests.schema_validation import validate_json_schema, SchemaValidationError

ROOT = fixture.ROOT
CONTRACT = ROOT / "tests/fixtures/f2-seat-stall-host-1/desktop-notifications.json"
PROMPT = "abc12345-1234-4234-8234-123456789abc"
STAMP = "2026-10-25T12:00:00.000Z"


class DesktopHostNotificationTests(unittest.TestCase):
    executions = fixture.HostToolCallbackNativeTests.executions
    # Reuse only the real fleet setup, not the inherited Codex test cases.
    def setUp(self):
        fixture.HostToolCallbackNativeTests.setUp(self)
        # Each test has its own root; replace the fixture registration through
        # a fresh native register/retire cycle, never a ledger file edit.
        Registry(self.root).register("claude-builder", "Claude Code")
        from floati.waiter_consent import arm_waiter_consent
        self.node = "claude-builder"
        self.work.complete(self.item["id"], "builder", [fixture.BINDING], now=self.now)
        grant = fixture.AuthorityGrantStore(self.root).claim("claude-work", self.node, 60, 60, self.now)
        self.item = self.work.add("desktop notification", self.node, [fixture.BINDING], now=self.now,
            car_binding={"row": "F2-SEAT-STALL-HOST-1", "repository": "floati", "branch": "sol/car"})
        self.claim = self.work.claim(self.item["id"], self.node, "claude-work", grant["epoch"], now=self.now)
        self.desktop = self.base / "desktop"
        self.desktop.mkdir()
        arm_waiter_consent(self.root.path, self.desktop, self.node, "Claude Code", 2, 1)
        self.binding = resolve_participant(self.root.path, self.desktop).binding
        self.consent = fixture.CodexWaitConsentLedger(self.root).require_armed(self.binding)
        self.session_claim = CodexWaitSessionLedger(self.root).arm(self.binding, self.consent,
            fixture.SESSION, idempotency_key="desktop-fixture", take_over=True)

    def notification(self, kind="permission_prompt"):
        return {"session_id": fixture.SESSION, "cwd": str(self.desktop),
            "transcript_path": "/unread/private-transcript", "hook_event_name": "Notification",
            "prompt_id": PROMPT, "notification_type": kind,
            "message": "private approval command or assistant text", "title": "private title"}

    def notify(self, payload=None, harness="claude"):
        payload = self.notification() if payload is None else payload
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            returncode = observe_desktop_notification(self.root.path, harness, payload)
        child = subprocess.CompletedProcess([], returncode, stdout.getvalue(), stderr.getvalue())
        self.assertEqual(0, child.returncode, child.stdout + child.stderr)
        self.assertEqual("", child.stdout, "observers must never decide or hold host callbacks")
        return child

    def assert_notice(self, event, host="claude"):
        executions = self.executions()
        self.assertEqual(1, len(executions), "supported host notification did not publish one standalone outcome")
        row = executions[0]
        self.assertEqual(event, row["event"])
        self.assertEqual("desktop_host", row["source_kind"])
        self.assertEqual(host, row["source_binding"]["harness"])
        self.assertEqual(self.claim["id"], row["work_claim_id"])
        self.assertEqual(self.session_claim["id"], row["source_receipt_id"])
        self.assertEqual(fixture.SESSION, row["acting_session_id"])
        self.assertIsNone(row["command"])
        self.assertIsNone(row["detail"])
        self.assertIsNone(row["code"])
        rows = EventLog(self.root).event_records()
        envelopes = [r for r in rows if r["kind"] == "message_envelope"]
        self.assertEqual(1, len(envelopes))
        self.assertEqual("architect", envelopes[0]["recipient"])
        self.assertEqual(row["envelope_id"], envelopes[0]["id"])
        self.assertEqual(1, sum(r["kind"] == "send_readiness_observation" for r in rows))
        self.assertNotIn("private approval", json.dumps(rows))
        self.assertNotIn("private-transcript", json.dumps(rows))
        self.assertNotIn("private title", json.dumps(rows))
        validate_json_schema(row, ROOT / "schemas/v1/seat-execution-record.schema.json")
        return row

    def test_idle_notification_reports_completion_without_fake_running_or_command(self):
        self.notify(self.notification("idle_prompt"))
        row = self.assert_notice("ended")
        self.assertEqual(PROMPT, row["source_binding"]["native_prompt_id"])

    def test_exact_replay_and_repeated_notification_of_same_prompt_coalesce(self):
        self.notify()
        before = EventLog(self.root).event_records()
        self.notify()
        self.notify(dict(self.notification(), message="different private text"))
        self.assertEqual(before, EventLog(self.root).event_records())
        self.assert_notice("approval_pending")

    def test_native_next_prompt_is_a_distinct_occurrence(self):
        self.notify()
        self.notify(dict(self.notification(), prompt_id="abc12345-1234-4234-8234-123456789abd"))
        self.assertEqual(2, len(self.executions()))

    def test_missing_prompt_identity_is_named_and_never_fabricated(self):
        payload = self.notification()
        del payload["prompt_id"]
        child = self.notify(payload)
        self.assertIn("desktop_prompt_identity_absent", child.stderr)
        self.assertEqual([], self.executions())

    def test_predecision_and_stopping_gates_do_not_become_final_state(self):
        for event in ("PermissionRequest", "Stop"):
            self.notify(dict(self.notification(), hook_event_name=event))
        self.assertEqual([], self.executions())

    def test_foreign_session_workspace_and_delegated_callbacks_cannot_authorize(self):
        for change in ({"session_id": "foreign"}, {"cwd": str(self.base)},
                       {"agent_id": "subagent"}, {"node_id": self.node},
                       {"prompt_id": "invented-non-uuid"}):
            with self.subTest(change=change):
                self.notify(dict(self.notification(), **change))
                self.assertEqual([], self.executions())

    def test_paused_session_and_missing_car_cannot_publish(self):
        WakeController(self.root).pause(self.node, fixture.SESSION, idempotency_key="fixture-pause")
        self.notify()
        self.assertEqual([], self.executions())
        WakeController(self.root).resume(self.node, fixture.SESSION, idempotency_key="fixture-resume")
        self.work.complete(self.item["id"], self.node, [fixture.BINDING], now=self.now)
        self.notify()
        self.assertEqual([], self.executions())

    def test_superseded_session_claim_cannot_publish(self):
        CodexWaitSessionLedger(self.root).arm(self.binding, self.consent,
            "replacement", idempotency_key="replacement", take_over=True)
        self.notify()
        self.assertEqual([], self.executions())

    def test_ambiguous_car_cannot_be_selected_by_notification(self):
        grant = fixture.AuthorityGrantStore(self.root).claim("second-work", self.node, 60, 60, self.now)
        item = self.work.add("second car", self.node, [fixture.BINDING], now=self.now,
            car_binding={"row": "another-car", "repository": "floati", "branch": "sol/another"})
        self.work.claim(item["id"], self.node, "second-work", grant["epoch"], now=self.now)
        self.notify()
        self.assertEqual([], self.executions())

    def test_callback_contract_absences_and_observed_fact_reach_doctor_and_board(self):
        from floati.doctor import project_seat_execution_findings
        from floati.tui import model_from_root
        from floati.tui_render import _detail_lines
        self.notify()
        finding = next(r for r in project_seat_execution_findings(self.root) if r["subject"] == self.node)
        projection = json.loads(finding["detail"])
        self.assertEqual("approval_pending", projection["latest_execution"]["event"])
        self.assertEqual("SUPPORTED", projection["host_callbacks"]["pending_approval"]["state"])
        model = model_from_root(self.root)
        index = next(i for i, r in enumerate(model.nodes) if r["node_id"] == self.node)
        rendered = "\n".join(_detail_lines(model, 300, index))
        self.assertIn("FACT claude permission_prompt observed at", rendered)
        self.assertNotIn("private approval", rendered)
        codex_index = next(i for i, r in enumerate(model.nodes) if r["node_id"] == "builder")
        rendered = "\n".join(_detail_lines(model, 300, codex_index))
        self.assertIn("codex_pending_approval_callback_absent", rendered)
        self.assertIn("codex_ended_turn_callback_absent", rendered)

    def test_fixture_contract_pin_and_source_fields_match_native_payloads(self):
        from floati.desktop_host_notifications import CONTRACT_PIN
        self.assertEqual(CONTRACT_PIN, hashlib.sha256(CONTRACT.read_bytes()).hexdigest())
        contracts = json.loads(CONTRACT.read_text())
        for harness, payload in (("claude", self.notification()), ("grok-build", self.grok())):
            contract = contracts[harness]
            self.assertTrue(set(contract["required"]) <= set(payload))
            self.assertTrue(set(payload) <= set(contract["required"]) | set(contract["optional"]))
            self.assertEqual(contract["event_value"], payload[contract["event_field"]])

    def test_runtime_and_schema_reject_fabricated_source_binding(self):
        from floati.errors import ProtocolRefusal
        from floati.records import validate_record
        self.notify()
        row = self.assert_notice("approval_pending")
        for change in ({"harness": "codex"}, {"contract_pin": "f" * 64},
                       {"native_prompt_id": None}, {"native_timestamp": STAMP},
                       {"notification_type": "idle_prompt"}, {"hook_event_name": "Stop"}):
            with self.subTest(change=change):
                bad = deepcopy(row)
                bad["source_binding"].update(change)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(bad, self.root.tenant_id, {"seat_execution"}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(bad, ROOT / "schemas/v1/seat-execution-record.schema.json")

    def test_unclosed_running_testimony_vetoes_new_host_outcome(self):
        from floati.workers import WorkerReceipts
        from floati.ids import uuid7_hex
        receipts = WorkerReceipts(self.root)
        session = "worker-" + uuid7_hex()
        receipts.append(session, self.item["id"], self.node, "codex", "claim", None, [], now=self.now)
        start = receipts.append(session, self.item["id"], self.node, "codex", "spawn", None, [], now=self.now)
        controller = SeatExecutionController(self.root)
        capability = controller.bind_worker(start, self.desktop)
        self.addCleanup(lambda: controller.callback(capability,
            {"event": "unknown", "code": "fixture_closed", "detail": "fixture cleanup"}))
        before = EventLog(self.root).event_records()
        self.notify()
        self.assertEqual(before, EventLog(self.root).event_records())

    def grok(self, kind="permission_prompt"):
        Registry(self.root).register("grok-builder", "grok-build")
        from floati.waiter_consent import arm_waiter_consent
        self.work.complete(self.item["id"], self.node, [fixture.BINDING], now=self.now)
        self.node = "grok-builder"
        grant = fixture.AuthorityGrantStore(self.root).claim("grok-work", self.node, 60, 60, self.now)
        self.item = self.work.add("native grok notification", self.node, [fixture.BINDING], now=self.now,
            car_binding={"row": "F2-SEAT-STALL-HOST-1", "repository": "floati", "branch": "sol/car"})
        self.claim = self.work.claim(self.item["id"], self.node, "grok-work", grant["epoch"], now=self.now)
        workspace = self.base / "grok"
        workspace.mkdir()
        arm_waiter_consent(self.root.path, workspace, self.node, "grok-build", 2, 1)
        self.binding = resolve_participant(self.root.path, workspace).binding
        self.consent = fixture.CodexWaitConsentLedger(self.root).require_armed(self.binding)
        self.session_claim = CodexWaitSessionLedger(self.root).arm(self.binding, self.consent,
            fixture.SESSION, idempotency_key="grok-fixture", take_over=True)
        payload = {"hookEventName": "notification", "sessionId": fixture.SESSION,
            "cwd": str(workspace), "workspaceRoot": str(workspace), "timestamp": STAMP,
            "permissionMode": "default", "notificationType": kind, "message": "private approval command"}
        if kind == "permission_prompt":
            payload["promptId"] = "opaque-native-prompt"
        return payload

    def test_grok_pending_uses_native_camelcase_contract_and_prompt_identity(self):
        self.notify(self.grok(), "grok-build")
        self.assert_notice("approval_pending", "grok-build")

    def test_current_grok_native_event_alias_is_accepted_only_when_consistent(self):
        payload = self.grok()
        self.notify(dict(payload, hook_event_name="Stop"), "grok-build")
        self.assertEqual([], self.executions())
        self.notify(dict(payload, hook_event_name="Notification"), "grok-build")
        self.assert_notice("approval_pending", "grok-build")

    def test_native_dispatch_timestamp_keeps_documented_precision_and_offset(self):
        payload = self.grok("idle_prompt")
        payload["timestamp"] = "2026-10-25T12:00:00.123456+00:00"
        self.notify(payload, "grok-build")
        row = self.assert_notice("ended", "grok-build")
        self.assertEqual(payload["timestamp"], row["source_binding"]["native_timestamp"])

    def test_native_observer_is_part_of_the_installable_bundle(self):
        from floati.manifest import _deployable_paths
        self.assertIn("scripts/floati-desktop-observe", _deployable_paths(ROOT))

    def test_grok_idle_is_session_scoped_and_never_invents_native_turn_identity(self):
        payload = self.grok("idle_prompt")
        self.notify(payload, "grok-build")
        before = EventLog(self.root).event_records()
        self.notify(payload, "grok-build")
        self.assertEqual(before, EventLog(self.root).event_records())
        row = self.assert_notice("ended", "grok-build")
        self.assertIsNone(row["source_binding"]["native_prompt_id"])
        self.assertEqual(STAMP, row["source_binding"]["native_timestamp"])

    def test_host_selection_cannot_relabel_another_registered_harness(self):
        self.notify(self.grok(), "claude")
        self.assertEqual([], self.executions())

    def test_malformed_input_exits_without_deciding_or_publishing(self):
        child = subprocess.run(["/usr/bin/python3", "-B", str(ROOT / "scripts/floati-desktop-observe"),
            "--root", str(self.root.path), "--harness", "claude"],
            input="{", text=True, capture_output=True, timeout=10, check=False)
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertEqual("", child.stdout)
        self.assertEqual([], self.executions())


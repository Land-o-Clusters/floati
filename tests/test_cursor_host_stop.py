"""Fixture-native Cursor stop admission; no installed hook or host launch."""
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import tests
from floati.codex_wait_contract import (CodexWaitConsentLedger,
    CodexWaitSessionLedger, resolve_participant)
from floati.events import EventLog
from floati.planes import AuthorityGrantStore
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_execution import SeatExecutionController
from floati.waiter_consent import arm_waiter_consent
from floati.work import WorkLog


SESSION = "cursor-host-conversation"
GENERATION = "cursor-host-generation"
CURSOR_PIN = "044ce0931100af5fe95403e9a692b02932bc3e13a6763f576b851f503fefa518"
BINDING = {"repo": "floati", "sha": "a" * 40, "doc": "docs/car.md"}
CAR = {"row": "F2-SEAT-STALL-HOST-1", "repository": "floati", "branch": "codex/car"}


class CursorHostStopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-host-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        registry = Registry(self.root)
        registry.register("builder", "Cursor")
        registry.register("architect", "Codex")
        registry.record_governance(topology="star", coordinator="architect",
            coordinator_authority=("dispatch_bounded_work",), owner_tier=("publishing",))
        self.now = datetime.now(timezone.utc)
        self.work = WorkLog(self.root)
        self.grant = AuthorityGrantStore(self.root).claim("work-claims", "builder", 60, 60, self.now)
        self.item, self.claim = self.claim_car()
        arm_waiter_consent(self.root.path, self.workspace, "builder", "Cursor", 1800, 1700)
        self.binding = resolve_participant(self.root.path, self.workspace).binding
        self.consent = CodexWaitConsentLedger(self.root).require_armed(self.binding)
        self.session_claim = CodexWaitSessionLedger(self.root).arm(self.binding,
            self.consent, SESSION, idempotency_key="cursor-host-fixture", take_over=True)

    def claim_car(self):
        item = self.work.add("Cursor native lifecycle", "builder", [BINDING],
            car_binding=CAR, now=self.now)
        claim = self.work.claim(item["id"], "builder", "work-claims", self.grant["epoch"], now=self.now)
        return item, claim

    def payload(self, status="completed", loop_count=0):
        return {"hook_event_name": "stop", "cursor_version": "3.24.12",
            "conversation_id": SESSION, "generation_id": GENERATION,
            "status": status, "loop_count": loop_count,
            "workspace_roots": [str(self.workspace)], "model": "fixture-model",
            "user_email": None, "transcript_path": None}

    def observe(self, payload=None, workspace=None):
        self.assertIsNotNone(importlib.util.find_spec("floati.cursor_stop_hook"),
            "native Cursor stop has no authenticated lifecycle observer")
        from floati.cursor_stop_hook import observe_cursor_stop
        with mock.patch("pathlib.Path.cwd", return_value=workspace or self.workspace):
            return observe_cursor_stop(self.root.path, self.payload() if payload is None else payload)

    def executions(self):
        return [row for row in EventLog(self.root).event_records() if row["kind"] == "seat_execution"]

    def assert_one_outcome(self, status, loop_count):
        rows = self.executions()
        self.assertEqual(1, len(rows), "host terminal observation must not fabricate preceding running")
        row = rows[0]
        self.assertEqual("cursor_host", row["source_kind"])
        self.assertEqual("ended", row["event"])
        self.assertIsNone(row["command"])
        self.assertIsNone(row["code"])
        self.assertIsNone(row["detail"])
        self.assertEqual(self.claim["id"], row["work_claim_id"])
        self.assertEqual(SESSION, row["acting_session_id"])
        self.assertEqual(self.session_claim["id"], row["source_receipt_id"])
        self.assertEqual({"contract_pin": CURSOR_PIN, "cursor_version": "3.24.12",
            "conversation_id": SESSION, "generation_id": GENERATION,
            "status": status, "loop_count": loop_count,
            "consent_receipt_id": self.consent["id"],
            "session_claim_id": self.session_claim["id"]}, row["source_binding"])
        events = EventLog(self.root).event_records()
        envelopes = [r for r in events if r["kind"] == "message_envelope"]
        self.assertEqual(1, len(envelopes))
        self.assertEqual("architect", envelopes[0]["recipient"])
        self.assertEqual(row["envelope_id"], envelopes[0]["id"])
        self.assertIn(status, envelopes[0]["note"])
        self.assertNotIn("None", envelopes[0]["note"])
        self.assertEqual(1, sum(r["kind"] == "send_readiness_observation" for r in events))

    def test_completed_is_one_standalone_atomic_lifecycle_outcome(self):
        self.observe(self.payload("completed", 0))
        self.assert_one_outcome("completed", 0)

    def test_aborted_is_ended_without_invented_refusal(self):
        self.observe(self.payload("aborted", 2))
        self.assert_one_outcome("aborted", 2)

    def test_error_is_ended_without_borrowing_tool_error_body(self):
        payload = dict(self.payload("error", 3), command="private command", output="private body")
        self.observe(payload)
        self.assert_one_outcome("error", 3)

    def test_exact_replay_reuses_execution_envelope_and_readiness(self):
        self.observe()
        before = EventLog(self.root).event_records()
        self.observe()
        self.assertEqual(before, EventLog(self.root).event_records())
        self.assert_one_outcome("completed", 0)

    def test_changed_status_for_same_occurrence_cannot_publish_second_outcome(self):
        self.observe()
        before = EventLog(self.root).event_records()
        self.observe(self.payload("error"))
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_next_generation_is_a_distinct_native_occurrence(self):
        self.observe()
        self.observe(dict(self.payload(), generation_id="next-generation"))
        self.assertEqual(2, len(self.executions()))
        self.assertEqual(2, sum(r["kind"] == "message_envelope" for r in EventLog(self.root).event_records()))

    def test_missing_correlation_does_not_create_execution(self):
        for field in ("conversation_id", "generation_id", "status", "loop_count", "cursor_version", "hook_event_name"):
            with self.subTest(field=field):
                payload = self.payload()
                del payload[field]
                self.observe(payload)
                self.assertEqual([], self.executions())

    def test_malformed_or_unmeasured_payload_does_not_create_execution(self):
        cases = [("loop_count", True), ("loop_count", -1), ("loop_count", 1.5),
            ("loop_count", "1"), ("status", "running"), ("generation_id", ""),
            ("conversation_id", []), ("cursor_version", "3.24.13"),
            ("hook_event_name", "afterAgentResponse")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.observe(dict(self.payload(), **{field: value}))
                self.assertEqual([], self.executions())

    def test_payload_workspace_cannot_authorize_unmapped_actual_cwd(self):
        self.observe(dict(self.payload(), cwd=str(self.workspace)), workspace=self.base)
        self.assertEqual([], self.executions())

    def test_descendant_stop_uses_mapped_workspace_session_and_consent(self):
        descendant = self.workspace / "nested"
        descendant.mkdir()
        self.observe(self.payload("completed", 0), workspace=descendant)
        self.assert_one_outcome("completed", 0)

    def test_other_conversation_cannot_borrow_current_session_claim(self):
        self.observe(dict(self.payload(), conversation_id="foreign-conversation"))
        self.assertEqual([], self.executions())

    def test_superseded_session_claim_rejects_old_stop(self):
        CodexWaitSessionLedger(self.root).arm(self.binding, self.consent,
            "replacement-session", idempotency_key="replacement", take_over=True)
        self.observe()
        self.assertEqual([], self.executions())

    def test_paused_session_cannot_publish_stop(self):
        from floati.wake_control import WakeController
        WakeController(self.root).pause("builder", SESSION, idempotency_key="pause")
        self.observe()
        self.assertEqual([], self.executions())

    def test_retired_node_cannot_publish_stop(self):
        Registry(self.root).retire("builder")
        self.observe()
        self.assertEqual([], self.executions())

    def test_no_car_cannot_be_created_by_callback(self):
        self.work.complete(self.item["id"], "builder", [BINDING], now=self.now)
        self.observe()
        self.assertEqual([], self.executions())
        self.assertEqual("NO_CAR", SeatExecutionController(self.root).project("builder")["state"])

    def test_multiple_owned_cars_cannot_be_selected_by_payload(self):
        self.claim_car()
        self.observe(dict(self.payload(), work_item_id=self.item["id"]))
        self.assertEqual([], self.executions())

    def test_authenticated_live_running_vetoes_stop_publication(self):
        from floati.ids import uuid7_hex
        from floati.workers import WorkerReceipts
        receipts = WorkerReceipts(self.root)
        worker_session = "worker-" + uuid7_hex()
        receipts.append(worker_session, self.item["id"], "builder", "codex", "claim", None, [], now=self.now)
        start = receipts.append(worker_session, self.item["id"], "builder", "codex", "spawn", None, [], now=self.now)
        controller = SeatExecutionController(self.root)
        capability = controller.bind_worker(start, self.workspace)
        self.addCleanup(lambda: controller.callback(capability,
            {"event": "unknown", "code": "fixture_closed", "detail": "fixture cleanup"}))
        before = EventLog(self.root).event_records()
        self.observe()
        self.assertEqual(before, EventLog(self.root).event_records())
        self.assertEqual("RUNNING", controller.project("builder")["state"])

    def test_existing_waiter_route_observes_stop_and_keeps_cursor_stdout(self):
        from floati.cursor_wait import run_cursor_stop_wait
        output = io.StringIO()
        with mock.patch("pathlib.Path.cwd", return_value=self.workspace):
            result = run_cursor_stop_wait(root=self.root.path, node="builder", runtime=self.base,
                deadline_seconds=10, poll_seconds=1, hook_timeout_seconds=1800,
                payload=self.payload("error", 4), stdout=output, stderr=io.StringIO())
        self.assertEqual(0, result)
        body = json.loads(output.getvalue())
        self.assertEqual({"followup_message"}, set(body))
        self.assert_one_outcome("error", 4)


class CursorToolFailureTests(unittest.TestCase):
    setUp = CursorHostStopTests.setUp
    claim_car = CursorHostStopTests.claim_car
    executions = CursorHostStopTests.executions

    def payload(self, failure_type="permission_denied"):
        return {"hook_event_name": "postToolUseFailure", "cursor_version": "3.24.12",
            "conversation_id": SESSION, "generation_id": GENERATION,
            "tool_use_id": "cursor-native-tool-call", "tool_name": "Shell",
            "failure_type": failure_type, "is_interrupt": False, "duration": 5000,
            "error_message": "fixture private refusal body",
            "tool_input": {"command": "fixture private command"},
            "cwd": str(self.workspace), "workspace_roots": [str(self.workspace)],
            "model": "fixture-model", "user_email": None,
            "transcript_path": "/fixture/private/transcript"}

    def observe_failure(self, payload=None, workspace=None):
        self.assertIsNotNone(importlib.util.find_spec("floati.cursor_stop_hook"),
            "Cursor tool denial lacks an authenticated after-fact observer")
        from floati import cursor_stop_hook
        observer = getattr(cursor_stop_hook, "observe_cursor_tool_failure", None)
        self.assertTrue(callable(observer), "native tool failure admission is absent")
        with mock.patch("pathlib.Path.cwd", return_value=workspace or self.workspace):
            return observer(self.root.path, self.payload() if payload is None else payload)

    def test_permission_denied_publishes_one_fact_without_stalled_or_tool_body(self):
        self.observe_failure()
        rows = self.executions()
        self.assertEqual(1, len(rows), "after-fact observation must not invent running")
        row = rows[0]
        self.assertEqual("cursor_tool", row["source_kind"])
        self.assertEqual("tool_request_refused", row["event"])
        self.assertIsNone(row["command"])
        self.assertIsNone(row["code"])
        self.assertIsNone(row["detail"])
        binding = row["source_binding"]
        self.assertEqual({"contract_pin", "cursor_version", "hook_event_name",
            "conversation_id", "generation_id", "tool_use_id", "tool_name",
            "failure_type", "observed_at", "consent_receipt_id", "session_claim_id"}, set(binding))
        self.assertEqual(CURSOR_PIN, binding["contract_pin"])
        self.assertEqual("3.24.12", binding["cursor_version"])
        self.assertEqual("postToolUseFailure", binding["hook_event_name"])
        self.assertEqual(SESSION, binding["conversation_id"])
        self.assertEqual(GENERATION, binding["generation_id"])
        self.assertEqual("cursor-native-tool-call", binding["tool_use_id"])
        self.assertEqual("Shell", binding["tool_name"])
        self.assertEqual("permission_denied", binding["failure_type"])
        self.assertEqual(self.consent["id"], binding["consent_receipt_id"])
        self.assertEqual(self.session_claim["id"], binding["session_claim_id"])
        self.assertEqual(self.session_claim["id"], row["source_receipt_id"])
        observed = datetime.fromisoformat(binding["observed_at"].replace("Z", "+00:00"))
        self.assertGreaterEqual(observed, self.now)
        self.assertLessEqual(observed, datetime.now(timezone.utc))
        projection = SeatExecutionController(self.root).project("builder")
        self.assertNotEqual("STALLED", projection["state"])
        self.assertNotEqual("RUNNING", projection["state"])
        events = EventLog(self.root).event_records()
        envelopes = [r for r in events if r["kind"] == "message_envelope"]
        self.assertEqual(1, len(envelopes))
        self.assertEqual(row["envelope_id"], envelopes[0]["id"])
        self.assertEqual("architect", envelopes[0]["recipient"])
        self.assertEqual(1, sum(r["kind"] == "send_readiness_observation" for r in events))
        for private in ("fixture private command", "fixture private refusal body", "/fixture/private/transcript"):
            self.assertNotIn(private, json.dumps(events))

    def test_timeout_and_error_never_become_denial_even_when_prose_claims_denial(self):
        for failure in ("error", "timeout", "unknown"):
            with self.subTest(failure=failure):
                self.observe_failure(dict(self.payload(failure),
                    error_message="permission_denied: approval required; request refused"))
                self.assertEqual([], self.executions())

    def test_exact_replay_reuses_first_native_observation_time_and_atomic_notice(self):
        self.observe_failure()
        before = EventLog(self.root).event_records()
        self.observe_failure()
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_changed_tool_metadata_for_same_occurrence_cannot_publish_again(self):
        self.observe_failure()
        before = EventLog(self.root).event_records()
        self.observe_failure(dict(self.payload(), tool_name="MCP:fixture"))
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_equal_denials_with_distinct_native_tool_call_ids_are_distinct_facts(self):
        self.observe_failure()
        self.observe_failure(dict(self.payload(), tool_use_id="next-native-tool-call"))
        self.assertEqual(2, len(self.executions()))
        self.assertEqual(2, sum(r["kind"] == "message_envelope" for r in EventLog(self.root).event_records()))

    def test_missing_failure_correlation_cannot_borrow_current_seat_identity(self):
        for field in ("conversation_id", "generation_id", "tool_use_id", "tool_name",
                      "failure_type", "cursor_version", "hook_event_name"):
            with self.subTest(field=field):
                payload = self.payload()
                del payload[field]
                self.observe_failure(payload)
                self.assertEqual([], self.executions())

    def test_malformed_failure_metadata_is_not_native_denial_testimony(self):
        for field, value in (("tool_use_id", ""), ("tool_name", []),
                ("generation_id", ""), ("cursor_version", "3.24.13"),
                ("hook_event_name", "preToolUse"), ("duration", True),
                ("duration", -1), ("duration", "5000"), ("is_interrupt", "false")):
            with self.subTest(field=field, value=value):
                self.observe_failure(dict(self.payload(), **{field: value}))
                self.assertEqual([], self.executions())

    def test_declared_cwd_does_not_authorize_foreign_actual_workspace(self):
        self.observe_failure(workspace=self.base)
        self.assertEqual([], self.executions())

    def test_descendant_denial_uses_mapped_workspace_session_and_consent(self):
        descendant = self.workspace / "nested"
        descendant.mkdir()
        self.observe_failure(dict(self.payload(), cwd=str(descendant)), workspace=descendant)
        rows = self.executions()
        self.assertEqual(1, len(rows), "mapped descendant lost the root workspace's current claim")
        row = rows[0]
        self.assertEqual("tool_request_refused", row["event"])
        self.assertEqual("permission_denied", row["source_binding"]["failure_type"])
        self.assertEqual(self.claim["id"], row["work_claim_id"])
        self.assertEqual(self.session_claim["id"], row["source_receipt_id"])
        self.assertEqual(self.session_claim["id"], row["source_binding"]["session_claim_id"])
        self.assertEqual(self.consent["id"], row["source_binding"]["consent_receipt_id"])

    def test_foreign_conversation_denial_cannot_borrow_current_session(self):
        self.observe_failure(dict(self.payload(), conversation_id="other-conversation"))
        self.assertEqual([], self.executions())

    def test_paused_session_denial_cannot_publish(self):
        from floati.wake_control import WakeController
        WakeController(self.root).pause("builder", SESSION, idempotency_key="pause-denial")
        self.observe_failure()
        self.assertEqual([], self.executions())

    def test_missing_car_denial_cannot_create_a_car(self):
        self.work.complete(self.item["id"], "builder", [BINDING], now=self.now)
        self.observe_failure()
        self.assertEqual([], self.executions())

    def test_governed_existing_hook_route_emits_fact_and_no_waiter_followup(self):
        from floati.cursor_wait import run_cursor_stop_wait
        output = io.StringIO()
        elapsed = [0.0]
        with mock.patch("pathlib.Path.cwd", return_value=self.workspace):
            result = run_cursor_stop_wait(root=self.root.path, node="builder", runtime=self.base,
                deadline_seconds=10, poll_seconds=1, hook_timeout_seconds=1800,
                payload=self.payload(), stdout=output, stderr=io.StringIO(),
                clock=lambda: elapsed[0], sleep=lambda unused: elapsed.__setitem__(0, 10.0))
        self.assertEqual(0, result)
        self.assertEqual({}, json.loads(output.getvalue()))
        self.assertEqual(["tool_request_refused"], [r["event"] for r in self.executions()])


if __name__ == "__main__":
    unittest.main()

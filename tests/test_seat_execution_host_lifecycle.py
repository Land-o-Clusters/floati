"""Closed host model controls; callback installation/adoption is not claimed.

The fixture acts at the private model boundary after adapter authentication.
It exercises the real car/controller/EventLog; it cannot authenticate a live
host launch. Host observations must not mint a fictitious running command.
"""
from contextlib import contextmanager
from tests.readiness import wait_join, wait_poll
from copy import deepcopy
import gc
import hashlib
import multiprocessing
from multiprocessing import resource_tracker
from pathlib import Path
import unittest
from unittest import mock

import tests
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.ids import uuid7_hex
from floati.records import validate_record
from floati.registry import utc_now
from tests import test_seat_execution as fixture
from tests.schema_validation import SchemaValidationError, validate_json_schema


SCHEMA = Path(__file__).resolve().parents[1] / "schemas/v1/seat-execution-record.schema.json"
CODEX_PIN = "75c73d7a38cfc0e73ef06bd1fc506a44d25874522069ec4fb85e0bf1e7d6b8fb"
INTERRUPT_PIN = "be37402db3524543efe1e4058e6b9a3a53f7dd8f7791a9b75bb2accf7842196e"
CURSOR_PIN = "044ce0931100af5fe95403e9a692b02932bc3e13a6763f576b851f503fefa518"
OBSERVED_AT = "2026-10-16T01:00:00.000Z"


def _native_unclosed_running(root_path, workspace, item_id):
    """A real producer exits with durable start testimony still unclosed."""
    from floati.root import FloatiRoot
    from floati.seat_execution import SeatExecutionController
    from floati.workers import WorkerReceipts

    root = FloatiRoot.open_direct_home(Path(root_path))
    receipts = WorkerReceipts(root)
    session = "worker-" + uuid7_hex()
    receipts.append(session, item_id, "builder", "codex", "claim", None, [], now=fixture.NOW)
    receipt = receipts.append(session, item_id, "builder", "codex", "spawn", None, [], now=fixture.NOW)
    controller = SeatExecutionController(root, recipient="architect")
    capability = controller.bind_worker(receipt, Path(workspace))
    if capability is None:
        raise AssertionError("native worker did not retain its running capability")


def _forked_host_after_durable_close(controller, capability, running_capability, channel):
    """Retain the actual fork snapshot, then read the parent's durable close."""
    try:
        if running_capability is None:
            raise AssertionError("fixture lost its actual inherited running capability")
        channel.send(("snapshot", controller.project("builder")["state"]))
        if channel.recv() != "durable-close-written":
            raise AssertionError("fixture did not receive its native close barrier")
        accepted = controller.callback(capability,
            {"event": "permission_quiet", "code": None, "detail": None})
        channel.send(("accepted", accepted))
    except BaseException as exc:
        channel.send(("refused", getattr(exc, "code", type(exc).__name__)))
    finally:
        channel.close()


class HostLifecycleModelTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller
    invocation = fixture.SeatExecutionTests.invocation

    @staticmethod
    def reap_fixture(child):
        # These tests own every Process handle they create. No host/process
        # discovery or unrelated signal is used, including on assertion failure.
        try:
            wait_join(child, "owned host lifecycle fixture exits")
        except AssertionError:
            child.terminate()
            try:
                wait_join(child, "collect terminated host lifecycle fixture")
            except AssertionError:
                child.kill()
                wait_join(child, "collect killed host lifecycle fixture")
        child.close()

    def native_unclosed_running(self, item, context="fork"):
        tracker = resource_tracker._resource_tracker
        prior_tracker = tracker._pid
        owned_tracker = None
        child = multiprocessing.get_context(context).Process(
            target=_native_unclosed_running,
            args=(str(self.root.path), str(self.workspace), item["id"]))
        try:
            try:
                child.start()
            finally:
                # Spawn can start the helper before producer creation fails.
                if prior_tracker is None:
                    owned_tracker = tracker._pid
            child.join(10)
            self.assertFalse(child.is_alive(), "native fixture producer did not exit")
            self.assertEqual(0, child.exitcode, "native fixture producer failed before its durable start")
        finally:
            if child.pid is not None:
                self.reap_fixture(child)
            else:
                child.close()
            # Close the channel and wait for only the helper this fixture
            # created, after every producer channel has been closed. A suite's
            # preexisting helper remains owned by its original fixture.
            if owned_tracker is not None and tracker._pid == owned_tracker:
                tracker._stop()

    def binding(self, source):
        common = {"consent_receipt_id": "fixture-native-consent",
                  "session_claim_id": "fixture-native-session-claim"}
        if source == "codex_host":
            return {**common, "contract_pin": CODEX_PIN,
                    "hook_event_name": "PermissionRequest", "turn_id": "host-turn",
                    "observed_sequence": 1, "observed_at": OBSERVED_AT,
                    "tool_name": "Bash"}
        if source == "cursor_tool":
            return {**common, "contract_pin": CURSOR_PIN, "cursor_version": "3.24.12",
                    "hook_event_name": "postToolUseFailure",
                    "conversation_id": "cursor-conversation", "generation_id": "cursor-generation",
                    "tool_use_id": "cursor-tool-call", "tool_name": "shell",
                    "failure_type": "permission_denied", "observed_at": OBSERVED_AT}
        return {**common, "contract_pin": CURSOR_PIN, "cursor_version": "3.24.12",
                "conversation_id": "cursor-conversation", "generation_id": "cursor-generation",
                "loop_count": 0, "status": "completed"}

    def interrupt_binding(self):
        return dict(self.binding("codex_host"), contract_pin=INTERRUPT_PIN,
                    hook_event_name="Interrupt", tool_name=None)

    def issue(self, controller, source="codex_host", *, binding=None, invocation=None):
        binding = self.binding(source) if binding is None else binding
        try:
            return controller._issue(
                node="builder", session="native-host-session", workspace=self.workspace,
                invocation=invocation or "host-observation-" + uuid7_hex(), source=source,
                receipt_id=binding["session_claim_id"],
                source_pin=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                command=None, source_binding=binding, running=False)
        except ProtocolRefusal as exc:
            self.fail("closed host capability refused before its supported outcome: " + exc.code)

    @staticmethod
    def payload(source="codex_host", *, event=None):
        return {"event": event or {"codex_host": "permission_quiet", "cursor_host": "ended",
                                   "cursor_tool": "tool_request_refused"}[source],
                "code": None, "detail": None}

    def accept(self, controller, capability, source="codex_host", *, event=None):
        try:
            return controller.callback(capability, self.payload(source, event=event))
        except ProtocolRefusal as exc:
            self.fail("supported standalone host observation refused: " + exc.code)

    def record(self, source="codex_host", *, event=None):
        # Independent wire fixture permits malformed-shape controls to run
        # even before the new controller source is implemented.
        binding = self.interrupt_binding() if event == "interrupted" else self.binding(source)
        return {"schema_version": 1, "id": "seat-execution-" + uuid7_hex(),
                "tenant_id": self.root.tenant_id, "timestamp": utc_now(),
                "kind": "seat_execution", "node_id": "builder",
                "acting_session_id": "native-host-session", "work_item_id": "work-" + uuid7_hex(),
                "work_claim_id": "transition-" + uuid7_hex(),
                "invocation_id": "host-observation-" + uuid7_hex(), "source_kind": source,
                "source_receipt_id": binding["session_claim_id"], "source_pin": "b" * 64,
                "source_binding": binding, "workspace": str(self.workspace),
                **self.payload(source, event=event), "command": None, "envelope_id": None,
                "idempotency_key": "host-fixture-" + uuid7_hex()}

    def assert_rejected_by_runtime_and_schema(self, row):
        with self.assertRaises(ProtocolRefusal):
            validate_record(row, self.root.tenant_id, {"seat_execution"}, integrity=False)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(row, SCHEMA)

    @contextmanager
    def fresh_fixture(self):
        case = type(self)("runTest")
        try:
            case.setUp()
            yield case
        finally:
            case.doCleanups()

    def test_permission_fact_has_one_atomic_notice_without_invented_running(self):
        for source, outcome, note_fragment in (
                ("codex_host", "permission_quiet", "permission requested"),
                ("codex_host", "interrupted", "turn interrupted at " + OBSERVED_AT),
                ("cursor_tool", "tool_request_refused", "tool request refused at " + OBSERVED_AT)):
            with self.subTest(case=(source, outcome)), self.fresh_fixture() as case:
                item = case.car(); claim = case.claim(item)
                controller = case.controller()
                binding = case.interrupt_binding() if outcome == "interrupted" else case.binding(source)
                capability = case.issue(controller, source, binding=binding)
                case.assertEqual([], EventLog(case.root).event_records())
                accepted = case.accept(controller, capability, source, event=outcome)
                rows = EventLog(case.root).event_records()
                case.assertEqual(["message_envelope", "send_readiness_observation", "seat_execution"],
                                 [row["kind"] for row in rows])
                event = accepted["execution"]
                case.assertEqual(source, event["source_kind"])
                case.assertEqual(outcome, event["event"])
                case.assertEqual(claim["id"], event["work_claim_id"])
                for field in ("command", "code", "detail"):
                    case.assertIsNone(event[field])
                if outcome == "interrupted":
                    case.assertEqual(INTERRUPT_PIN, event["source_binding"]["contract_pin"])
                    case.assertEqual("Interrupt", event["source_binding"]["hook_event_name"])
                    case.assertIsNone(event["source_binding"]["tool_name"])
                if source == "cursor_tool":
                    case.assertEqual("permission_denied", event["source_binding"]["failure_type"])
                case.assertEqual(accepted["notice"]["message"]["id"], event["envelope_id"])
                note = accepted["notice"]["message"]["note"]
                case.assertIn(note_fragment, note)
                if outcome == "permission_quiet":
                    case.assertIn(OBSERVED_AT, note)
                    case.assertIn("Bash", note)
                    case.assertIn("no later host event recorded for this session", note)
                if outcome == "interrupted":
                    case.assertNotIn("permission requested", note)
                case.assertNotIn("None", note)
                case.assertNotIn("STALLED", note)
                case.assertEqual("UNKNOWN", controller.project("builder")["state"])

    def test_cursor_denial_exact_call_replay_retains_original_time_and_notice(self):
        self.claim(self.car())
        controller = self.controller(); invocation = "cursor-tool-" + uuid7_hex()
        first_capability = self.issue(controller, "cursor_tool", invocation=invocation)
        accepted = self.accept(controller, first_capability, "cursor_tool")
        before = EventLog(self.root).event_records()
        replay_capability = self.issue(controller, "cursor_tool", invocation=invocation)
        self.assertEqual(accepted, controller.callback(replay_capability, self.payload("cursor_tool")))
        self.assertEqual(OBSERVED_AT, accepted["execution"]["source_binding"]["observed_at"])
        self.assertEqual(before, EventLog(self.root).event_records())


    def test_cursor_denial_same_call_cannot_change_its_retained_metadata(self):
        self.claim(self.car())
        controller = self.controller(); invocation = "cursor-tool-" + uuid7_hex()
        capability = self.issue(controller, "cursor_tool", invocation=invocation)
        self.accept(controller, capability, "cursor_tool")
        before = EventLog(self.root).event_records()
        for change in ({"conversation_id": "another-conversation"},
                       {"generation_id": "another-generation"}, {"tool_use_id": "another-call"},
                       {"tool_name": "another-tool"}, {"observed_at": "2026-10-16T01:01:00.000Z"},
                       {"consent_receipt_id": "replacement-consent"},
                       {"session_claim_id": "replacement-session-claim"}):
            with self.subTest(change=change):
                changed = self.issue(controller, "cursor_tool", invocation=invocation,
                                     binding=dict(self.binding("cursor_tool"), **change))
                with self.assertRaises(ProtocolRefusal):
                    controller.callback(changed, self.payload("cursor_tool"))
                self.assertEqual(before, EventLog(self.root).event_records())

    def test_exact_terminal_replay_reuses_event_envelope_and_readiness_bytes(self):
        for outcome in ("permission_quiet", "interrupted"):
            with self.subTest(case=outcome), self.fresh_fixture() as case:
                case.claim(case.car())
                controller = case.controller()
                binding = case.interrupt_binding() if outcome == "interrupted" else case.binding("codex_host")
                capability = case.issue(controller, binding=binding)
                first = case.accept(controller, capability, event=outcome)
                before = EventLog(case.root).event_records()
                case.assertEqual(first, controller.callback(capability, case.payload(event=outcome)))
                case.assertEqual(before, EventLog(case.root).event_records())

    def test_changed_terminal_replay_cannot_publish_another_notice(self):
        self.claim(self.car())
        controller = self.controller(); capability = self.issue(controller)
        self.accept(controller, capability)
        before = EventLog(self.root).event_records()
        with self.assertRaises(ProtocolRefusal):
            controller.callback(capability, {"event": "permission_quiet",
                                             "code": None, "detail": "invented refusal"})
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_reused_invocation_cannot_change_the_observed_occurrence(self):
        self.claim(self.car())
        controller = self.controller(); invocation = "host-observation-" + uuid7_hex()
        first = self.issue(controller, invocation=invocation)
        self.accept(controller, first)
        before = EventLog(self.root).event_records()
        second = self.issue(controller, invocation=invocation,
                            binding=dict(self.binding("codex_host"), observed_sequence=2))
        with self.assertRaises(ProtocolRefusal):
            controller.callback(second, self.payload())
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_completed_claim_suppresses_fresh_terminal_host_acceptance(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); capability = self.issue(controller)
        self.work.complete(item["id"], "builder", [fixture.BINDING], now=fixture.NOW)
        with self.assertRaises(ProtocolRefusal):
            controller.callback(capability, self.payload())
        self.assertEqual([], EventLog(self.root).event_records())

    def test_accepted_host_outcome_replays_after_car_completion(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); capability = self.issue(controller)
        accepted = self.accept(controller, capability)
        self.work.complete(item["id"], "builder", [fixture.BINDING], now=fixture.NOW)
        before = EventLog(self.root).event_records()
        self.assertEqual(accepted, controller.callback(capability, self.payload()))
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_live_running_invocation_vetoes_host_acceptance_in_both_orders(self):
        item = self.car(); self.claim(item)
        controller = self.controller()
        older = self.issue(controller)
        running = self.invocation(controller, item)
        newer = self.issue(controller, "cursor_host")
        denied = self.issue(controller, "cursor_tool")
        interrupted = self.issue(controller, binding=self.interrupt_binding())
        before = EventLog(self.root).event_records()
        for capability, source, outcome in (
                (older, "codex_host", "permission_quiet"), (newer, "cursor_host", "ended"),
                (denied, "cursor_tool", "tool_request_refused"), (interrupted, "codex_host", "interrupted")):
            with self.subTest(case=(source, outcome)):
                with self.assertRaises(ProtocolRefusal) as caught:
                    controller.callback(capability, self.payload(source, event=outcome))
                self.assertEqual("seat_execution_running_unclosed", caught.exception.code)
                self.assertEqual(before, EventLog(self.root).event_records())
        self.assertIsNotNone(running)
        self.assertEqual("RUNNING", controller.project("builder")["state"])

    def test_other_process_unclosed_running_vetoes_all_new_host_outcomes(self):
        item = self.car(); self.claim(item)
        self.native_unclosed_running(item)
        controller = self.controller()
        projected = controller.project("builder")
        self.assertEqual("UNKNOWN", projected["state"])
        self.assertEqual("unclosed_native_invocation", projected["reason"])
        before = EventLog(self.root).event_records()
        self.assertEqual(["running"],
                         [row["event"] for row in before if row["kind"] == "seat_execution"])
        for source, event in (("codex_host", "permission_quiet"),
                              ("codex_host", "interrupted"), ("cursor_host", "ended"),
                              ("cursor_tool", "tool_request_refused")):
            with self.subTest(source=source, event=event):
                binding = self.interrupt_binding() if event == "interrupted" else self.binding(source)
                capability = self.issue(controller, source, binding=binding)
                with self.assertRaises(ProtocolRefusal):
                    controller.callback(capability, self.payload(source, event=event))
                self.assertEqual(before, EventLog(self.root).event_records())

    def test_dropped_running_capability_stays_unknown_and_vetoes_host_acceptance(self):
        item = self.car(); self.claim(item)
        controller = self.controller()
        running = self.invocation(controller, item)
        del running
        gc.collect()
        projected = controller.project("builder")
        self.assertEqual("UNKNOWN", projected["state"])
        self.assertEqual("unclosed_native_invocation", projected["reason"])
        capability = self.issue(controller)
        before = EventLog(self.root).event_records()
        with self.assertRaises(ProtocolRefusal):
            controller.callback(capability, self.payload())
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_exact_accepted_replay_survives_later_other_process_unclosed_running(self):
        item = self.car(); self.claim(item)
        controller = self.controller(); capability = self.issue(controller)
        accepted = self.accept(controller, capability)
        self.native_unclosed_running(item)
        before = EventLog(self.root).event_records()
        self.assertEqual(accepted, controller.callback(capability, self.payload()))
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_actual_durable_close_releases_veto_despite_stale_fork_active_snapshot(self):
        item = self.car(); self.claim(item)
        controller = self.controller()
        running = self.invocation(controller, item)
        capability = self.issue(controller)
        context = multiprocessing.get_context("fork")
        parent_channel, child_channel = context.Pipe()
        child = context.Process(target=_forked_host_after_durable_close,
                                args=(controller, capability, running, child_channel))
        child.start()
        child_channel.close()
        try:
            self.assertTrue(wait_poll(parent_channel, 'seat_execution_host_lifecycle: parent_channel synchronization'), "fork fixture did not report its inherited snapshot")
            self.assertEqual(("snapshot", "RUNNING"), parent_channel.recv())
            controller.callback(running, {"event": "ended", "code": "worker_completed_cleanup",
                                          "detail": "owned fixture worker cleaned"})
            parent_channel.send("durable-close-written")
            self.assertTrue(wait_poll(parent_channel, 'seat_execution_host_lifecycle: parent_channel synchronization'), "fork fixture did not return its acceptance result")
            disposition, result = parent_channel.recv()
            self.assertEqual("accepted", disposition, result)
            self.assertEqual("permission_quiet", result["execution"]["event"])
            self.assertIsNotNone(result["execution"]["envelope_id"])
            child.join(10)
            self.assertFalse(child.is_alive(), "fork fixture remained alive after publication")
            self.assertEqual(0, child.exitcode)
            rows = EventLog(self.root).event_records()
            self.assertEqual(["running", "ended", "permission_quiet"],
                             [row["event"] for row in rows if row["kind"] == "seat_execution"])
        finally:
            parent_channel.close()
            self.reap_fixture(child)

    def test_publication_failure_accepts_neither_host_fact_nor_notice(self):
        self.claim(self.car())
        controller = self.controller(); capability = self.issue(controller)
        before = EventLog(self.root).event_records()
        with mock.patch("floati.jsonl.os.fsync", side_effect=OSError("scratch disk full")):
            with self.assertRaises(Exception):
                controller.callback(capability, self.payload())
        self.assertEqual(before, EventLog(self.root).event_records())
        self.assertEqual("UNKNOWN", controller.project("builder")["state"])

    def test_supported_lifecycle_wire_shapes_validate_in_runtime_and_schema(self):
        for source, event in (("codex_host", "permission_quiet"),
                              ("codex_host", "interrupted"), ("cursor_host", "ended"),
                              ("cursor_tool", "tool_request_refused")):
            with self.subTest(source=source, event=event):
                row = self.record(source, event=event)
                try:
                    validate_record(row, self.root.tenant_id, {"seat_execution"}, integrity=False)
                except ProtocolRefusal as exc:
                    self.fail("supported lifecycle wire contract refused: " + exc.code)
                validate_json_schema(row, SCHEMA)

    def test_lifecycle_fields_cannot_borrow_command_code_or_refusal_body(self):
        for source, event in (("codex_host", "permission_quiet"),
                              ("codex_host", "interrupted"), ("cursor_host", "ended"),
                              ("cursor_tool", "tool_request_refused")):
            for field, value in (("command", "old command"), ("code", "old_refusal"),
                                 ("detail", "old refusal BODY")):
                with self.subTest(source=source, event=event, field=field):
                    self.assert_rejected_by_runtime_and_schema(
                        dict(self.record(source, event=event), **{field: value}))

    def test_host_source_event_pairs_do_not_admit_running_or_crossed_outcomes(self):
        for source, events in (("codex_host", ("running", "ended", "refused_action", "tool_request_refused", "unknown")),
                               ("cursor_host", ("running", "permission_quiet", "interrupted", "tool_request_refused", "followup_failed", "unknown")),
                               ("cursor_tool", ("running", "ended", "permission_quiet", "interrupted", "refused_action", "unknown"))):
            for event in events:
                with self.subTest(source=source, event=event):
                    self.assert_rejected_by_runtime_and_schema(dict(self.record(source), event=event))

    def test_codex_binding_is_closed_and_requires_true_observation_metadata(self):
        changes = ({"contract_pin": "a" * 64}, {"hook_event_name": "Stop"},
                   {"turn_id": ""}, {"observed_sequence": 0}, {"observed_sequence": True},
                   {"observed_at": "not-a-time"}, {"tool_name": ""},
                   {"request_id": "invented"})
        for change in changes:
            with self.subTest(change=change):
                row = self.record()
                row["source_binding"].update(change)
                self.assert_rejected_by_runtime_and_schema(row)
        row = self.record(); del row["source_binding"]["consent_receipt_id"]
        self.assert_rejected_by_runtime_and_schema(row)

    def test_interrupt_and_permission_contracts_cannot_borrow_each_other(self):
        row = self.record(event="interrupted")
        candidates = []
        for change in ({"contract_pin": CODEX_PIN}, {"hook_event_name": "PermissionRequest"},
                       {"tool_name": "Bash"}, {"observed_sequence": 0},
                       {"observed_sequence": True}, {"observed_at": "not-a-time"},
                       {"turn_id": ""}, {"request_id": "invented"}):
            candidate = deepcopy(row)
            candidate["source_binding"].update(change)
            candidates.append(candidate)
        candidates.append(dict(deepcopy(row), event="permission_quiet"))
        candidates.append(dict(self.record(), event="interrupted"))
        permission_without_tool = self.record()
        permission_without_tool["source_binding"]["tool_name"] = None
        candidates.append(permission_without_tool)
        missing_tool_slot = deepcopy(row)
        del missing_tool_slot["source_binding"]["tool_name"]
        candidates.append(missing_tool_slot)
        for candidate in candidates:
            with self.subTest(event=candidate["event"], binding=candidate["source_binding"]):
                self.assert_rejected_by_runtime_and_schema(candidate)

    def test_cursor_binding_is_closed_and_pinned_to_observed_contract(self):
        changes = ({"contract_pin": "a" * 64}, {"cursor_version": "unmeasured"},
                   {"conversation_id": ""}, {"generation_id": ""}, {"loop_count": -1},
                   {"loop_count": True}, {"status": "pending"}, {"request_id": "invented"})
        for change in changes:
            with self.subTest(change=change):
                row = self.record("cursor_host")
                row["source_binding"].update(change)
                self.assert_rejected_by_runtime_and_schema(row)
        row = self.record("cursor_host"); del row["source_binding"]["consent_receipt_id"]
        self.assert_rejected_by_runtime_and_schema(row)

    def test_host_source_receipt_must_equal_retained_native_session_claim(self):
        # Cross-field equality is a native invariant; the schema vocabulary
        # validates each closed field but has no data-reference comparison.
        for source in ("codex_host", "cursor_host", "cursor_tool"):
            with self.subTest(source=source):
                row = self.record(source)
                row["source_binding"]["session_claim_id"] = "another-claim"
                with self.assertRaises(ProtocolRefusal):
                    validate_record(row, self.root.tenant_id, {"seat_execution"}, integrity=False)

    def test_host_bindings_cannot_cross_each_other_or_legacy_sources(self):
        for source, binding_source in (("codex_host", "cursor_host"), ("cursor_host", "codex_host"),
                                       ("worker", "codex_host"), ("codex_tool", "cursor_host"),
                                       ("cursor_tool", "cursor_host"), ("cursor_host", "cursor_tool"),
                                       ("codex_host", "cursor_tool"), ("codex_tool", "cursor_tool")):
            with self.subTest(source=source, binding_source=binding_source):
                row = self.record(binding_source)
                row["source_kind"] = source
                self.assert_rejected_by_runtime_and_schema(row)

    def test_cursor_denial_requires_exact_supported_failure_binding(self):
        changes = ({"contract_pin": "a" * 64}, {"cursor_version": "unmeasured"},
                   {"hook_event_name": "stop"}, {"conversation_id": ""}, {"generation_id": ""},
                   {"tool_use_id": ""}, {"tool_name": ""}, {"observed_at": "not-a-time"},
                   {"failure_type": "tool_error"}, {"failure_type": "timeout"},
                   {"failure_type": None}, {"request_id": "invented"}, {"status": "error"})
        for change in changes:
            with self.subTest(change=change):
                row = self.record("cursor_tool")
                row["source_binding"].update(change)
                self.assert_rejected_by_runtime_and_schema(row)
        for missing in ("tool_use_id", "failure_type", "consent_receipt_id", "observed_at"):
            with self.subTest(missing=missing):
                row = self.record("cursor_tool")
                del row["source_binding"][missing]
                self.assert_rejected_by_runtime_and_schema(row)


if __name__ == "__main__":
    unittest.main()

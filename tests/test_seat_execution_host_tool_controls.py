"""Additional fixture-native host controls; live response shape remains unproved.

Mixed templated/piped callbacks must deliver complete JSON promptly. The owner
hook uses root only, without templated workspace/session overrides; a bounded
optional read does not authenticate or infer a host event from elapsed time.
"""
from tests.readiness import wait_event
from copy import deepcopy
import io
import os
import subprocess
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.records import validate_record
from floati.wake_control import WakeController
from tests import test_seat_execution_host_tool_native as native
from tests import test_seat_execution_worker_binding as native_worker
from tests.schema_validation import SchemaValidationError, validate_json_schema


class HostToolCallbackControls(unittest.TestCase):
    setUp = native.HostToolCallbackNativeTests.setUp
    payload = native.HostToolCallbackNativeTests.payload
    executions = native.HostToolCallbackNativeTests.executions
    validate_shipped_contract = native.HostToolCallbackNativeTests.validate_shipped_contract

    def hook(self, payload, *, options=(), outer_valid=True):
        # Reuse the frozen fixture's actual shim launch. Only deliberately
        # malformed outer inputs skip its pre-launch schema assertion.
        real_run = subprocess.run

        def launch(arguments, **kwargs):
            return real_run([*arguments, *options], **kwargs)

        with mock.patch.object(native.subprocess, "run", side_effect=launch):
            if outer_valid:
                return native.HostToolCallbackNativeTests.hook(self, payload)
            with mock.patch.object(self, "validate_shipped_contract", return_value=None):
                return native.HostToolCallbackNativeTests.hook(self, payload)

    def ignored(self, payload, **kwargs):
        before = EventLog(self.root).event_records()
        child = self.hook(payload, **kwargs)
        self.assertEqual("", child.stdout)
        self.assertEqual("", child.stderr)
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_exact_replay_keeps_all_accepted_event_bytes(self):
        self.hook(self.payload())
        before = EventLog(self.root).event_records()
        self.assertEqual(2, len(self.executions()))
        self.hook(self.payload())
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_same_invocation_cannot_change_body_code_or_command(self):
        self.hook(self.payload())
        before = EventLog(self.root).event_records()
        bodies = (native.BODY + "\nchanged detail", native.BODY.replace("FLEET_BUS", "OTHER"))
        for body in bodies:
            with self.subTest(body=body):
                self.ignored(self.payload(output=body))
        changed = self.payload()
        changed["tool_input"]["command"] += " --changed"
        self.ignored(changed)
        self.assertEqual(before, EventLog(self.root).event_records())

    def test_success_with_unrecognized_response_keys_is_silent(self):
        payload = self.payload(0)
        payload["tool_response"]["extra"] = native.BODY
        self.ignored(payload)

    def test_unmeasured_response_shapes_cannot_issue_testimony(self):
        responses = (None, "unmeasured text", {}, {"exit_code": 64},
            {"exit_code": 64, "output": native.BODY, "extra": True},
            {"exit_code": True, "output": native.BODY},
            {"exit_code": "64", "output": native.BODY},
            {"exit_code": 64.0, "output": native.BODY},
            {"exit_code": 1 << 63, "output": native.BODY},
            {"exit_code": -(1 << 63) - 1, "output": native.BODY},
            {"exit_code": 64, "output": None},
            {"exit_code": 64, "output": native.BODY + "x" * 8193})
        for response in responses:
            with self.subTest(response_type=type(response).__name__, response=response):
                payload = self.payload()
                payload["tool_response"] = response
                self.ignored(payload)

    def test_outer_authority_fields_do_not_authenticate_or_redirect(self):
        for field, value in (("authenticated", True), ("node_id", "architect"),
                ("work_item_id", self.item["id"]), ("work_claim_id", self.claim["id"]),
                ("recipient", "builder"), ("source_pin", "a" * 64)):
            with self.subTest(field=field):
                payload = self.payload()
                payload[field] = value
                self.ignored(payload, outer_valid=False)

    def test_subagent_fields_cannot_borrow_main_thread_authority(self):
        for fields in ({"agent_id": "delegated"}, {"agent_type": "explorer"},
                {"agent_id": "", "agent_type": ""}):
            with self.subTest(fields=fields):
                payload = self.payload()
                payload.update(fields)
                self.ignored(payload)

    def test_only_complete_main_thread_outer_contract_is_supported(self):
        for field in ("turn_id", "tool_use_id", "session_id", "model", "permission_mode"):
            with self.subTest(missing=field):
                payload = self.payload()
                del payload[field]
                self.ignored(payload, outer_valid=False)
        for field in ("turn_id", "tool_use_id"):
            with self.subTest(empty=field):
                payload = self.payload()
                payload[field] = ""
                self.ignored(payload)

    def test_unrecognized_tool_and_extra_command_keys_are_ignored(self):
        payload = self.payload()
        payload["tool_name"] = "unmeasured-tool"
        self.ignored(payload)
        payload = self.payload()
        payload["tool_input"]["authenticated"] = True
        self.ignored(payload)

    def test_unrelated_command_cannot_quote_a_managed_refusal(self):
        payload = self.payload()
        payload["tool_input"]["command"] = "echo quoted-token"
        self.ignored(payload)

    def test_unhashable_permission_mode_is_ignored_without_an_exception(self):
        for value in ({"mode": "default"}, ["default"]):
            with self.subTest(value=value):
                payload = self.payload()
                payload["permission_mode"] = value
                self.ignored(payload, outer_valid=False)

    def test_refusal_body_must_start_with_the_code_and_exit_must_be_64(self):
        for body in ("quoted output\n" + native.BODY, " " + native.BODY,
                native.BODY.replace("CODEX_", "OTHER_", 1)):
            with self.subTest(body=body):
                self.ignored(self.payload(output=body))
        self.ignored(self.payload(1))

    def test_paused_session_cannot_publish_a_host_refusal(self):
        WakeController(self.root).pause("builder", native.SESSION,
            idempotency_key="host-control-pause")
        self.ignored(self.payload())

    def test_superseded_session_cannot_publish_a_host_refusal(self):
        binding = resolve_participant(self.root.path, self.workspace).binding
        CodexWaitSessionLedger(self.root).arm(binding, self.consent, "replacement-session",
            idempotency_key="host-control-takeover", take_over=True)
        self.ignored(self.payload())

    def test_replaced_consent_cannot_borrow_the_prior_session_claim(self):
        binding = resolve_participant(self.root.path, self.workspace).binding
        CodexWaitConsentLedger(self.root).arm(binding, hook_timeout_seconds=2,
            wait_deadline_seconds=1, idempotency_key="host-control-new-consent")
        self.ignored(self.payload())

    def test_two_owned_claimed_cars_cannot_select_an_arbitrary_one(self):
        second = self.work.add("second host car", "builder", [native.BINDING], now=self.now,
            car_binding={"row": "second-host-car", "repository": "floati", "branch": "codex/other"})
        self.work.claim(second["id"], "builder", "work-claims", self.grant["epoch"], now=self.now)
        self.ignored(self.payload())

    def test_cli_overrides_cannot_replace_host_workspace_or_session(self):
        options = (("--workspace", str(self.base)), ("--session-id", "replacement-session"),
            ("--workspace", str(self.base), "--session-id", "replacement-session"))
        for case_index, supplied in enumerate(options):
            with self.subTest(case=(
                "workspace-only-override",
                "session-only-override",
                "workspace-and-session-overrides",
            )[case_index]):
                self.ignored(self.payload(), options=supplied)

    def test_direct_stdin_entry_uses_the_same_native_producer(self):
        from floati import codex_wait
        import json

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(codex_wait.sys, "stdin", io.StringIO(json.dumps(self.payload()))),\
                mock.patch.object(codex_wait.sys, "stdout", stdout),\
                mock.patch.object(codex_wait.sys, "stderr", stderr):
            status = codex_wait.main(["--root", str(self.root.path)])
        self.assertEqual(0, status)
        self.assertEqual("", stdout.getvalue())
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(["running", "refused_action"], [row["event"] for row in self.executions()])

    def test_native_host_binding_remains_closed_and_cannot_cross_worker_schema(self):
        self.hook(self.payload())
        row = self.executions()[-1]
        schema = native.ROOT / "schemas/v1/seat-execution-record.schema.json"
        self.assertEqual("codex_tool", row["source_kind"])
        self.assertEqual({"contract_pin", "turn_id", "tool_use_id", "tool_name",
            "consent_receipt_id", "session_claim_id"}, set(row["source_binding"]))
        self.assertEqual(native.SCHEMA_PIN, row["source_binding"]["contract_pin"])
        self.assertEqual(self.consent["id"], row["source_binding"]["consent_receipt_id"])
        self.assertEqual(self.session_claim["id"], row["source_binding"]["session_claim_id"])
        validate_record(deepcopy(row), self.root.tenant_id, {"seat_execution"}, integrity=False)
        validate_json_schema(row, schema)
        worker = native_worker._NativeWorkerCase(self)
        worker.bind_session()
        worker.bind_execution()
        worker_row = next(r for r in EventLog(worker.root).event_records() if r["kind"] == "seat_execution")
        crossed = (dict(deepcopy(row), source_kind="worker"),
            dict(deepcopy(row), source_binding=deepcopy(worker_row["source_binding"])))
        for candidate in crossed:
            with self.subTest(source=candidate["source_kind"]):
                with self.assertRaises(ProtocolRefusal):
                    validate_record(candidate, self.root.tenant_id, {"seat_execution"}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)

    def test_host_source_cannot_testify_to_worker_cursor_or_unknown_outcomes(self):
        self.hook(self.payload())
        row = self.executions()[-1]
        schema = native.ROOT / "schemas/v1/seat-execution-record.schema.json"
        for event in ("ended", "followup_failed", "unknown"):
            with self.subTest(event=event):
                candidate = dict(deepcopy(row), event=event)
                with self.assertRaises(ProtocolRefusal):
                    validate_record(candidate, self.root.tenant_id, {"seat_execution"}, integrity=False)
                with self.assertRaises(SchemaValidationError):
                    validate_json_schema(candidate, schema)

    def test_short_dispatch_notice_names_native_command_code_and_refusal(self):
        self.hook(self.payload())
        envelope = next(row for row in EventLog(self.root).event_records()
            if row["kind"] == "message_envelope")
        for text in (native.COMMAND, native.BODY, "CODEX_FLEET_BUS_APPROVAL_REQUIRED"):
            self.assertIn(text, envelope["note"])
        self.assertNotIn("worker_session_id", envelope)
        self.assertEqual("absent_legacy", envelope["attempt_binding"])

    def test_refusal_body_preserves_controls_while_dispatch_notice_escapes_them(self):
        import json

        body = native.BODY + "\ncontinued\x1b[31m"
        self.hook(self.payload(output=body))
        self.assertEqual(body, self.executions()[-1]["detail"])
        envelope = next(row for row in EventLog(self.root).event_records()
            if row["kind"] == "message_envelope")
        self.assertIn(json.dumps(body, ensure_ascii=True), envelope["note"])
        self.assertNotIn("\n", envelope["note"])
        self.assertNotIn("\x1b", envelope["note"])

    def test_failed_native_host_publication_accepts_no_refusal_or_dispatch_batch(self):
        from floati.codex_tool_hook import run_tool_hook
        from floati.seat_execution import SeatExecutionController

        native_send = EventLog.send
        publications = []

        def failed_notice(log, *arguments, **keywords):
            if keywords.get("_execution_publication") is not None:
                publications.append(True)
                with mock.patch("floati.jsonl.os.fsync", side_effect=OSError("scratch host publication disk failure")):
                    return native_send(log, *arguments, **keywords)
            return native_send(log, *arguments, **keywords)

        with mock.patch.object(EventLog, "send", new=failed_notice):
            status = run_tool_hook(bus_home=self.root.path, hook_payload=self.payload())
        self.assertEqual(0, status)
        self.assertEqual([True], publications)
        rows = EventLog(self.root).event_records()
        self.assertEqual(["running"], [row["event"] for row in self.executions()])
        self.assertFalse(any(row["kind"] in {"message_envelope", "send_readiness_observation"}
            for row in rows))
        self.assertEqual("UNKNOWN", SeatExecutionController(self.root).project("builder")["state"])

    def test_newer_native_worker_running_veto_survives_host_refusal_replay(self):
        from floati.codex_tool_hook import run_tool_hook
        from floati.ids import uuid7_hex
        from floati.seat_execution import SeatExecutionController
        from floati.workers import WorkerReceipts

        self.assertEqual(0, run_tool_hook(bus_home=self.root.path, hook_payload=self.payload()))
        controller = SeatExecutionController(self.root)
        receipts = WorkerReceipts(self.root)
        session = "worker-" + uuid7_hex()
        receipts.append(session, self.item["id"], "builder", "codex", "claim", None, [], now=self.now)
        spawn = receipts.append(session, self.item["id"], "builder", "codex", "spawn", None, [], now=self.now)
        invocation = controller.bind_worker(spawn, self.workspace)
        before = EventLog(self.root).event_records()
        self.assertEqual("RUNNING", controller.project("builder")["state"])
        self.assertEqual(0, run_tool_hook(bus_home=self.root.path, hook_payload=self.payload()))
        self.assertEqual(before, EventLog(self.root).event_records())
        self.assertEqual("RUNNING", controller.project("builder")["state"])
        self.assertIsNotNone(invocation)

    def retained_authority_race(self, operation):
        from floati import bus_epoch
        from floati.codex_tool_hook import run_tool_hook
        from floati.jsonl import read_records_snapshot
        from floati.seat_waiter_lifecycle import retire_waiter
        from floati.wake_control import is_session_paused

        binding = resolve_participant(self.root.path, self.workspace).binding
        attempted, completed = threading.Event(), threading.Event()
        failures, changed = [], []
        native_flock, native_send = bus_epoch.fcntl.flock, EventLog.send
        root_info = os.stat(self.root.tenant_home)
        root_identity = (root_info.st_dev, root_info.st_ino)

        def contender():
            try:
                if operation == "consent":
                    changed.append(CodexWaitConsentLedger(self.root).arm(binding,
                        hook_timeout_seconds=3, wait_deadline_seconds=1,
                        idempotency_key="host-control-raced-consent"))
                elif operation == "retire":
                    changed.append(retire_waiter(self.root, "builder", self.workspace,
                        "host-control-raced-retirement"))
                else:
                    changed.append(WakeController(self.root).pause("builder", native.SESSION,
                        idempotency_key="host-control-raced-pause"))
            except Exception as exc:
                failures.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=contender, name="host-native-authority-contender")

        def observed_flock(descriptor, flags):
            competing = threading.current_thread() is thread
            if competing and operation in {"consent", "retire"} and flags == bus_epoch.fcntl.LOCK_SH:
                info = os.fstat(descriptor)
                if (info.st_dev, info.st_ino) == root_identity:
                    # Registry preflight may use a blocking shared root lock
                    # before the consent transaction's nonblocking epoch path.
                    attempted.set()
            if competing and operation == "pause" and flags == bus_epoch.fcntl.LOCK_EX:
                # Pause's native lock is blocking; observe the actual acquisition
                # attempt before forwarding it, never replace the lock operation.
                attempted.set()
            try:
                return native_flock(descriptor, flags)
            except BlockingIOError:
                if competing and operation in {"consent", "retire"}:
                    info = os.fstat(descriptor)
                    if (info.st_dev, info.st_ino) == root_identity:
                        attempted.set()
                raise

        publications = []

        def publish(log, *arguments, **keywords):
            if keywords.get("_execution_publication") is None:
                return native_send(log, *arguments, **keywords)
            publications.append(True)
            with mock.patch.object(bus_epoch.fcntl, "flock", side_effect=observed_flock):
                thread.start()
                self.assertTrue(wait_event(attempted, 'seat_execution_host_tool_controls: attempted synchronization'),
                    "contender never reached native lock acquisition; failures=" + repr(failures))
                self.assertFalse(completed.wait(0.08), "authority changed before guarded host publication")
                result = native_send(log, *arguments, **keywords)
                self.assertFalse(completed.is_set(), "authority changed while host still retained its guards")
                self.assertEqual(self.consent["id"],
                    CodexWaitConsentLedger(self.root).require_armed(binding)["id"])
                self.assertFalse(is_session_paused(self.root, "builder", native.SESSION))
                return result

        try:
            with mock.patch.object(EventLog, "send", new=publish):
                self.assertEqual(0, run_tool_hook(bus_home=self.root.path, hook_payload=self.payload()))
        finally:
            if thread.ident is not None:
                # Join only after the adapter has unwound its retained guards.
                thread.join(6)
                self.assertFalse(thread.is_alive(), "native contender did not finish after guard release")
        self.assertEqual([], failures)
        self.assertEqual([True], publications)
        self.assertEqual(1, len(changed))
        rows = EventLog(self.root).event_records()
        self.assertEqual(["running", "refused_action"], [row["event"] for row in self.executions()])
        self.assertEqual(1, sum(row["kind"] == "message_envelope" for row in rows))
        self.assertEqual(1, sum(row["kind"] == "send_readiness_observation" for row in rows))
        if operation == "consent":
            self.assertEqual(changed[0]["id"], CodexWaitConsentLedger(self.root).require_armed(binding)["id"])
            self.assertNotEqual(self.consent["id"], changed[0]["id"])
        elif operation == "retire":
            self.assertEqual("disarmed", changed[0]["state"])
            self.assertEqual("removed", changed[0]["map_state"])
            self.assertIsNone(resolve_participant(self.root.path, self.workspace))
            rows = read_records_snapshot(self.root, CodexWaitConsentLedger._relative("builder"),
                allowed_kinds={"codex_wait_consent_receipt"})
            self.assertEqual("disarmed", rows[-1]["state"])
            self.assertEqual(changed[0]["consent_receipt_id"], rows[-1]["id"])
            with self.assertRaises(ProtocolRefusal) as refused:
                CodexWaitConsentLedger(self.root).require_armed(binding)
            self.assertEqual("codex_wait_consent_missing", refused.exception.code)
        else:
            self.assertTrue(is_session_paused(self.root, "builder", native.SESSION))

    def test_native_consent_writer_waits_for_guarded_host_publication(self):
        cases = ("consent", "pause", "retire")
        for index, operation in enumerate(cases):
            with self.subTest(operation=operation):
                if index:
                    self.doCleanups()
                    self.setUp()
                self.retained_authority_race(operation)

    def test_canonical_mapped_subdirectory_retains_native_claim_workspace(self):
        subdirectory = self.workspace / "nested"
        subdirectory.mkdir()
        payload = self.payload()
        payload["cwd"] = str(subdirectory)
        self.hook(payload)
        row = self.executions()[-1]
        self.assertEqual("refused_action", row["event"])
        self.assertEqual(str(subdirectory), row["workspace"])
        self.assertEqual(self.session_claim["id"], row["source_binding"]["session_claim_id"])
        self.assertEqual(self.claim["id"], row["work_claim_id"])

    def test_maximum_command_retains_a_dispatcher_refusal_preview(self):
        payload = self.payload()
        command = native.COMMAND + " " + "x" * (1024 - len(native.COMMAND) - 1)
        payload["tool_input"]["command"] = command
        self.hook(payload)
        row = self.executions()[-1]
        self.assertEqual(command, row["command"])
        self.assertEqual(native.BODY, row["detail"])
        envelope = next(row for row in EventLog(self.root).event_records()
            if row["kind"] == "message_envelope")
        self.assertIn(native.COMMAND, envelope["note"])
        self.assertIn(native.BODY, envelope["note"])
        self.assertLessEqual(len(envelope["note"]), 1024)

    def test_both_options_preserve_empty_legacy_stop_and_reject_partial_open_stdin(self):
        from floati import cli

        args = SimpleNamespace(workspace=str(self.workspace), session_id=native.SESSION)
        for content in (b"", b'{"hook_event_name":"PostToolUse",'):
            with self.subTest(content=content):
                reader, writer = os.pipe()
                try:
                    with os.fdopen(reader, "r", encoding="utf-8") as stream:
                        reader = None
                        if content:
                            os.write(writer, content)
                        else:
                            # No bytes identify an event. Preserve the supported
                            # templated Stop shape, without waiting for pipe EOF.
                            self.assertIsNone(cli._optional_wait_input(stream))
                        started = time.monotonic()
                        with mock.patch.object(cli.sys, "stdin", stream):
                            payload = cli._wait_payload(args)
                        self.assertLess(time.monotonic() - started, 1)
                        expected = ({"_hook_input_incomplete": True} if content else
                            {"cwd": str(self.workspace), "session_id": native.SESSION})
                        self.assertEqual(expected, payload)
                finally:
                    try:
                        if reader is not None:
                            os.close(reader)
                    finally:
                        os.close(writer)

    def test_both_options_preserve_delayed_complete_input_without_waiting_for_eof(self):
        from floati import cli
        import json

        expected = self.payload()
        encoded = json.dumps(expected).encode("utf-8")
        args = SimpleNamespace(workspace=str(self.workspace), session_id=native.SESSION)
        reader, writer = os.pipe()
        release = threading.Event()
        failures = []

        def write_later():
            try:
                release.wait()
                time.sleep(0.03)
                os.write(writer, encoded)
            except Exception as exc:
                failures.append(exc)

        thread = threading.Thread(target=write_later, name="host-delayed-stdin-fixture")
        try:
            with os.fdopen(reader, "r", encoding="utf-8") as stream:
                reader = None
                thread.start()
                started = time.monotonic()
                release.set()
                with mock.patch.object(cli.sys, "stdin", stream):
                    payload = cli._wait_payload(args)
                elapsed = time.monotonic() - started
                self.assertEqual(expected, payload)
                self.assertLess(elapsed, 0.2)
        finally:
            release.set()
            if thread.ident is not None:
                thread.join(1)
                self.assertFalse(thread.is_alive())
            try:
                if reader is not None:
                    os.close(reader)
            finally:
                os.close(writer)
        self.assertEqual([], failures)


if __name__ == "__main__":
    unittest.main()

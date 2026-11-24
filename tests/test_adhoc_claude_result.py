"""Claude's initial session identity arrives after its draft result is written.

The independent child invents its own provider ID after writing a null-identity
draft. Only terminal testimony can supply the parent binding; no provider is
called and no private session history is read.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from floati.adhoc_profiles import AdhocProfiles
from floati.adhoc_records import CLAUDE_PERMISSION
from floati.adhoc_workspace import BorrowedWorkspace
from floati.adhoc_threads import _parse_result
from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.spawn_groups import SpawnGroupController
from floati.workers import WorkerReceipts
from tests.fixtures.adhoc_stage1 import AdhocFixture
from tests.test_adhoc_threads import ControllerFixture
from tests.readiness import communicate, wait_process


class _ClaudeDraftExecution:
    def __init__(self, case):
        self.case = case
        self.effect_controller = case.effects
        self.launches = 0
        self.last_native_events = []
        self.last_native_transport = None
        self.last_process_audit = {"adapter_pid": None, "registered_process_groups": [],
            "alive_after_cleanup": [], "isolation_backend": "fixture-process"}

    def run_exact(self, node, adapter, work, *, native_thread_context,
                  on_native_event, startup_prompt="", now=None):
        case = self.case
        self.launches += 1
        case.work.claim(work, node, "borrowed-work", 1, now=case.now)
        receipts = WorkerReceipts(case.root)
        session = "worker-" + uuid7_hex()
        receipts.append(session, work, node, adapter, "claim", None, [], now=case.now)
        receipts.append(session, work, node, adapter, "spawn", None, [], now=case.now)
        prompt = json.loads(startup_prompt)
        self.prompt = prompt
        payload = {"path": case.dispatch_record["result_document"],
            "dispatch_digest": native_thread_context["dispatch_digest"],
            "messages": prompt["messages"]}
        script = ("import json,pathlib,sys,uuid\n"
            "v=json.load(sys.stdin)\n"
            "draft={'schema_version':1,'dispatch_digest':v['dispatch_digest'],"
            "'provider_coordinate':None,'summary':'independent Claude draft',"
            "'inbox_dispositions':[{'message_id':m['id'],'disposition':'acted',"
            "'reason':'fixture explicitly acted','result_paths':[]} for m in v['messages']]}\n"
            "p=pathlib.Path(v['path']);p.parent.mkdir(parents=True,exist_ok=True)\n"
            "p.write_text(json.dumps(draft,sort_keys=True)+'\\n')\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'result':'fixture completed','session_id':str(uuid.uuid4())}),flush=True)\n")
        child = subprocess.Popen([str(Path(sys.executable).resolve()), "-B", "-c", script],
            cwd=case.profile_record["workspace"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            env={**os.environ, "HOME": str(case.base / "private-home")})
        try:
            stdout, stderr = communicate(child, "Claude fixture initial terminal frame",
                                         input=json.dumps(payload).encode())
        finally:
            if child.poll() is None:
                child.terminate()
                wait_process(child, "Claude fixture owned child cleanup")
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None:
                    stream.close()
        if child.returncode:
            raise RuntimeError("fixture child failed")
        terminal = json.loads(stdout)
        case.coordinate = {"provider": "claude_local", "session_id": terminal["session_id"], "session_file": None}
        case.context = deepcopy(native_thread_context)
        self.draft_bytes = (Path(case.profile_record["workspace"]) / payload["path"]).read_bytes()
        self.last_process_audit.update(adapter_pid=child.pid, registered_process_groups=[child.pid])
        self.last_native_transport = {"stdout_bytes": len(stdout), "stderr_bytes": len(stderr),
            "combined_bytes": len(stdout) + len(stderr), "protocol_records": 1,
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(), "exit_code": child.returncode}
        event = case.event("terminal")
        on_native_event(event)
        self.last_native_events.append(event)
        receipts.append(session, work, node, adapter, "drive", None, [], now=case.now)
        return receipts.append(session, work, node, adapter, "bind_artifact", None, [], now=case.now)


class AdhocClaudeResultTests(ControllerFixture, unittest.TestCase):
    def setUp(self):
        create, dispatch = AdhocProfiles.create, AdhocFixture.dispatch
        bind_policy = SpawnGroupController.bind_attempt_policy

        def claude_profile(owner, node, adapter, *args, **kwargs):
            return create(owner, node, "claude" if node == "node-a" else adapter, *args, **kwargs)

        def claude_dispatch(owner, profile, **changes):
            if profile["adapter"] == "claude":
                changes.setdefault("permission", deepcopy(CLAUDE_PERMISSION))
            return dispatch(owner, profile, **changes)

        def claude_policy(owner, *args, **kwargs):
            return bind_policy(owner, *args, **{**kwargs, "adapter": "claude"})

        # Underlying production producers still author genuine Claude profile
        # and dispatch records. The fixture's Codex defaults are only inputs.
        with mock.patch.object(AdhocProfiles, "create", claude_profile),\
             mock.patch.object(AdhocFixture, "dispatch", claude_dispatch),\
             mock.patch.object(SpawnGroupController, "bind_attempt_policy", claude_policy):
            super().setUp()
        self.execution = _ClaudeDraftExecution(self.case)

    def result_path(self):
        return Path(self.case.profile_record["workspace"]) / self.case.dispatch_record["result_document"]

    def test_initial_terminal_identity_binds_null_draft_before_banking_and_ack(self):
        code, _ = self.capture_run()
        self.assertEqual("adhoc_result_unbanked", code,
            "Claude's lawful null draft must bind its authenticated terminal identity before banking")
        self.assertIsNone(json.loads(self.execution.draft_bytes)["provider_coordinate"])
        self.assertEqual(set(), set(self.acked()))
        self.assertEqual(self.case.coordinate, json.loads(self.result_path().read_bytes())["provider_coordinate"])
        intent = self.saved("process_closed", stage="banking_intent")[0]["evidence"]
        self.assertEqual(hashlib.sha256(self.execution.draft_bytes).hexdigest(), intent["identity_binding"]["input_sha256"])
        self.bank_result()
        self.assertIsNone(self.capture_run()[0])
        self.assertEqual(1, self.execution.launches)
        self.assertEqual({self.case.envelope_record["id"]}, set(self.acked()))
        result = self.saved("process_closed", stage="result")[0]["evidence"]["result"]
        raw = self.case.git(self.case.repository, "show", result["sha"] + ":" + result["doc"])
        self.assertEqual(self.case.coordinate, json.loads(raw)["provider_coordinate"])

    def test_identity_binding_crash_replays_frozen_output_without_provider(self):
        with mock.patch.object(BorrowedWorkspace, "finalize", side_effect=RuntimeError("after identity binding")):
            with self.assertRaisesRegex(RuntimeError, "after identity binding"):
                self.run_dispatch()
        self.assertTrue(self.saved("process_closed", stage="banking_intent"))
        self.assertEqual(self.case.coordinate, json.loads(self.result_path().read_bytes())["provider_coordinate"])
        self.assertEqual("adhoc_result_unbanked", self.capture_run()[0])
        self.assertEqual(1, self.execution.launches)
        self.assertEqual(set(), set(self.acked()))

    def test_replaced_result_after_identity_intent_is_never_overwritten(self):
        authenticate = self.controller.journal._authenticate_lifecycle
        replaced = []

        def replace_after_proof(record, *args):
            authenticate(record, *args)
            if record["phase"] == "process_closed" and record["evidence"].get("stage") == "banking_intent":
                path = self.result_path()
                path.rename(path.with_name("retained-draft.json"))
                path.write_bytes(b"foreign replacement stays untouched\n")
                replaced.append(path.stat().st_ino)

        with mock.patch.object(self.controller.journal, "_authenticate_lifecycle", side_effect=replace_after_proof):
            code, _ = self.capture_run()
        self.assertTrue(replaced, "the fixture must cross the durable identity-intent boundary")
        self.assertIn(code, {"adhoc_binding_changed", "adhoc_result_disposition_invalid"})
        self.assertEqual(b"foreign replacement stays untouched\n", self.result_path().read_bytes())
        self.assertEqual(replaced[0], self.result_path().stat().st_ino)
        self.assertEqual(set(), set(self.acked()))
        self.assertEqual([], self.saved("process_closed", stage="result"))


class NonClaudeNullResultTests(unittest.TestCase):
    def test_codex_null_coordinate_stays_invalid_without_ack(self):
        coordinate = {"provider": "codex_local",
            "session_id": "019f6ed3-cc28-7000-8000-000000000001", "session_file": None}
        admission = {"dispatch_digest": "a" * 64, "dispatch": {"result_document": "result.json"}}
        value = {"schema_version": 1, "dispatch_digest": admission["dispatch_digest"],
            "provider_coordinate": coordinate, "summary": "fixture result", "inbox_dispositions": []}
        self.assertEqual((value, []), _parse_result(json.dumps(value).encode(), admission, coordinate, []))
        value["provider_coordinate"] = None
        with self.assertRaises(ProtocolRefusal) as caught:
            _parse_result(json.dumps(value).encode(), admission, coordinate, [])
        self.assertEqual("adhoc_result_disposition_invalid", caught.exception.code)

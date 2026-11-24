"""Scratch creation-only startup; a first model turn must never be started.

The pre-fix RED emitted thread.started and waited behind a cooperative gate.
This provider keeps that gate on turn/start and proves startup avoids the turn.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from floati import seat_onboarding
from floati.jsonl import read_records_snapshot
from floati.wake_control import WakeController
from tests import test_seat_onboard as baseline


def creation_provider(base, *, failure=None):
    executable = base / "creation-codex-fixture"
    executable.write_text("#!" + str(Path(sys.executable).resolve()) + "\n" + "\n".join([
        "import json, os, sys, time",
        "from pathlib import Path",
        "base = Path(" + repr(str(base)) + ")",
        "failure = " + repr(failure),
        "provider_home = Path(os.environ['CODEX_HOME'])",
        "if not (provider_home / 'config.toml').is_file(): raise SystemExit(10)",
        "store = provider_home / 'native-provider-store.json'",
        "if sys.argv[1:] != ['app-server', '--stdio']: raise SystemExit(9)",
        "for raw in sys.stdin:",
        "    request = json.loads(raw)",
        "    method = request['method']",
        "    if method == 'initialized': continue",
        "    with (provider_home / 'native-provider-methods.jsonl').open('a') as log:",
        "        log.write(json.dumps({'pid': os.getpid(), 'method': method, 'provider_home': str(provider_home), 'caller_thread_id': os.environ.get('CODEX_THREAD_ID'), 'caller_turn_id': os.environ.get('CODEX_TURN_ID')}) + '\\n')",
        "    if method == 'initialize':",
        "        result = {}",
        "        if failure == 'resume_status_initialize' and store.exists(): print(json.dumps({'method': 'thread/status/changed', 'params': {'threadId': 'fixture-paused-thread', 'status': {'type': 'idle'}}, 'emittedAtMs': 1790880712496}), flush=True)",
        "        if failure == 'goal_initialize' and store.exists(): print(json.dumps({'method': 'thread/goal/cleared', 'params': {'threadId': 'fixture-paused-thread'}, 'emittedAtMs': 1790880712496}), flush=True)",
        "        if failure == 'status_without_id': print(json.dumps({'method': 'thread/status/changed', 'params': {'status': {'type': 'idle'}}}), flush=True)",
        "        if failure == 'frame_count':",
        "            for index in range(129): print(json.dumps({'method': 'thread/started', 'params': {'thread': {'id': 'fixture-paused-thread'}}}), flush=True)",
        "    elif method == 'thread/start':",
        "        if request['params'].get('ephemeral') is not False: raise SystemExit(8)",
        "        thread = {'id': 'fixture-paused-thread', 'sessionId': 'fixture-paused-thread', 'ephemeral': False, 'cwd': os.getcwd(), 'turns': [], 'status': {'type': 'idle'}}",
        "        if failure != 'lazy_store' and not (failure and failure.startswith('read_')): store.write_text(json.dumps(thread))",
        "        result = {'thread': thread}",
        "    elif method == 'thread/read':",
        "        if request['params'] != {'threadId': thread['id'], 'includeTurns': True}: raise SystemExit(11)",
        "        if failure != 'read_absence_without_store': store.write_text(json.dumps(thread))",
        "        if failure and failure.startswith('read_absence'):",
        "            absent = {'id': request['id'], 'error': {'code': -32601, 'message': 'list_turns is not supported yet'}}",
        "            if failure == 'read_absence_wrong_id': absent['id'] = 999",
        "            if failure == 'read_absence_bad_code': absent['error']['code'] = -32600",
        "            if failure == 'read_absence_bool_code': absent['error']['code'] = True",
        "            if failure == 'read_absence_float_code': absent['error']['code'] = -32601.0",
        "            if failure == 'read_absence_bad_message': absent['error']['message'] = 'unsupported read'",
        "            if failure == 'read_absence_error_extra': absent['error']['data'] = {}",
        "            if failure == 'read_absence_outer_extra': absent['result'] = {}",
        "            print(json.dumps(absent), flush=True)",
        "            continue",
        "        result = {'thread': dict(thread)}",
        "        if failure == 'read_missing': del result['thread']['status']",
        "        if failure == 'read_wrong_id': result['thread']['id'] = 'different-thread'",
        "        if failure == 'read_prior_turn': result['thread']['turns'] = [{'id': 'prior-turn'}]",
        "    elif method == 'thread/resume':",
        "        if failure == 'resume_metadata_only' and request['params'].get('excludeTurns') is not True:",
        "            print(json.dumps({'id': request['id'], 'error': {'code': -32601, 'message': 'full-history hydration unsupported; excludeTurns required'}}), flush=True)",
        "            continue",
        "        if not store.exists():",
        "            print(json.dumps({'id': request['id'], 'error': {'code': -32600, 'message': 'no rollout found for thread id fixture-paused-thread'}}), flush=True)",
        "            continue",
        "        thread = json.loads(store.read_text())",
        "        if request['params']['threadId'] != thread['id']: raise SystemExit(7)",
        "        if failure and failure.startswith('resume_status') and failure != 'resume_status_initialize':",
        "            status = {'method': 'thread/status/changed', 'params': {'threadId': thread['id'], 'status': {'type': 'idle'}}, 'emittedAtMs': 1790880712496}",
        "            if failure == 'resume_status_wrong_id': status['params']['threadId'] = 'different-thread'",
        "            if failure == 'resume_status_active': status['params']['status'] = {'type': 'active'}",
        "            if failure == 'resume_status_bad_time': status['emittedAtMs'] = True",
        "            print(json.dumps(status), flush=True)",
        "        if failure == 'resume':",
        "            print(json.dumps({'id': request['id'], 'error': {'code': -32602, 'message': 'thread not found'}}), flush=True)",
        "            continue",
        "        result = {'thread': thread}",
        "        if failure == 'resume_prior_turn': result['thread']['turns'] = [{'id': 'prior-turn'}]",
        "        if failure == 'resume_missing_status': del result['thread']['status']",
        "    elif method == 'turn/start':",
        "        (base / 'unexpected-turn-started').write_text('turn started')",
        "        print(json.dumps({'type': 'thread.started', 'thread_id': 'fixture-paused-thread'}), flush=True)",
        "        while not (base / 'release-owned-child').exists(): time.sleep(0.005)",
        "        (base / 'unexpected-turn-completed').write_text('turn completed')",
        "        raise SystemExit(6)",
        "    else: raise SystemExit(5)",
        "    if method in ('thread/start', 'thread/resume') and failure and failure.startswith('remote_'):",
        "        notification = {'method': 'remoteControl/status/changed', 'params': {'status': 'disabled', 'serverName': 'fixture-workstation.local', 'installationId': 'c41f7868-69bd-43bf-933e-a6cfcc96d57e', 'environmentId': None}, 'emittedAtMs': 1790880712496}",
        "        if failure == 'remote_active': notification['params']['status'] = 'connected'",
        "        if failure == 'remote_missing': del notification['params']['installationId']",
        "        if failure == 'remote_extra': notification['params']['unreviewed'] = True",
        "        if failure == 'remote_environment': notification['params']['environmentId'] = 'foreign-environment'",
        "        if failure == 'remote_bad_id': notification['params']['installationId'] = 'not-a-uuid'",
        "        if failure == 'remote_bad_name': notification['params']['serverName'] = 'host' * 100",
        "        if failure == 'remote_bool_time': notification['emittedAtMs'] = True",
        "        if failure == 'remote_negative_time': notification['emittedAtMs'] = -1",
        "        if failure == 'remote_large_time': notification['emittedAtMs'] = 2 ** 53",
        "        print(json.dumps(notification), flush=True)",
        "        if failure == 'remote_disabled': print(json.dumps({'method': 'thread/started', 'params': {'thread': {'id': thread['id']}}, 'emittedAtMs': 1790880712497}), flush=True)",
        "    if method == 'thread/start' and failure == 'missing_status': del result['thread']['status']",
        "    if method == 'thread/start' and failure == 'duplicate':",
        "        print('{\"id\":2,\"id\":2,\"result\":' + json.dumps(result) + '}', flush=True)",
        "        continue",
        "    if method == 'thread/start' and failure == 'wrong_id': request['id'] = 999",
        "    if method == 'thread/start' and failure == 'deep':",
        "        print('{\"id\":2,\"result\":{\"nested\":' + '[' * 1500 + '0' + ']' * 1500 + '}}', flush=True)",
        "        continue",
        "    if method == 'thread/start' and failure in ('NaN', 'Infinity'):",
        "        print('{\"id\":2,\"result\":{\"thread\":' + json.dumps(result['thread']) + ',\"ignored\":' + failure + '}}', flush=True)",
        "        continue",
        "    if method == 'thread/start' and failure == 'oversized':",
        "        print('x' * 70000, flush=True)",
        "        continue",
        "    print(json.dumps({'id': request['id'], 'result': result}), flush=True)",
        "    if (method == 'thread/resume' and failure and failure.startswith('goal_') and failure != 'goal_initialize') or (method == 'thread/start' and failure == 'goal_create'):",
        "        cleared = {'method': 'thread/goal/cleared', 'params': {'threadId': thread['id']}, 'emittedAtMs': 1790880712496}",
        "        if failure == 'goal_no_id': del cleared['params']['threadId']",
        "        if failure == 'goal_wrong_id': cleared['params']['threadId'] = 'different-thread'",
        "        if failure == 'goal_extra': cleared['params']['goal'] = None",
        "        if failure == 'goal_set': cleared['method'] = 'thread/goal/set'",
        "        if failure == 'goal_bad_time': cleared['emittedAtMs'] = True",
        "        print(json.dumps(cleared), flush=True)",
        "    if method == 'thread/start' and failure == 'unsolicited_turn':",
        "        print(json.dumps({'method': 'turn/started', 'params': {'threadId': thread['id'], 'turn': {'id': 'foreign-turn'}}}), flush=True)",
    ]) + "\n")
    executable.chmod(0o700)
    return executable


class SeatOnboardCliHoldingTests(unittest.TestCase):
    setUp = baseline.SeatOnboardingTests.setUp
    git = baseline.SeatOnboardingTests.git
    cli = baseline.SeatOnboardingTests.cli
    add = baseline.SeatOnboardingTests.add

    def start(self, *, failure=None):
        executable = creation_provider(self.base, failure=failure)
        self.children = []
        children = self.children
        gate = self.base / "release-owned-child"
        original = subprocess.Popen
        def capture(*arguments, **options):
            child = original(*arguments, **options)
            if arguments and arguments[0][0] == str(executable):
                children.append(child)
            return child
        def cleanup():
            gate.touch()
            for child in children:
                child.wait(timeout=2)
        self.addCleanup(cleanup)
        with mock.patch.object(subprocess, "Popen", side_effect=capture):
            code, output, errors = self.add("--mode", "cli", "--codex-executable", str(executable))
        return code, json.loads(output)["evidence"], errors

    def record(self):
        path = seat_onboarding._record_path(self.root, self.home)
        return path, json.loads(path.read_text())

    def remove(self):
        code, output, errors = self.cli("seat", "remove", "--root", str(self.root.path), "--home", str(self.home))
        return code, json.loads(output)["evidence"], errors

    def assert_reversed_pause(self, thread_id):
        code, result, errors = self.remove()
        self.assertEqual(0, code, errors or result)
        self.assertFalse(self.home.exists())
        before, after = result["cli_pause"]["before"], result["cli_pause"]["after"]
        self.assertEqual(thread_id, before["session_id"])
        self.assertEqual("absent", after["state"])
        self.assertFalse(Path(after["marker"]).exists())
        self.assertFalse(WakeController._resume_quarantine(Path(after["marker"]), before["pause_receipt_id"]).exists())
        return result

    def test_start_is_saved_paused_and_resumable_without_a_turn(self):
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        thread = result["thread"]
        self.assertEqual("paused", thread["state"])
        self.assertEqual("saved_without_turn", thread["provider_state"])
        self.assertTrue(thread["resumable"])
        self.assertFalse(thread["turn_started"])
        self.assertEqual("unproven", result["hearing"])
        self.assertEqual("unproven", result["live_waiter"])
        self.assertEqual(2, len(self.children))
        self.assertEqual([0, 0], [child.poll() for child in self.children])
        self.assertEqual([child.pid for child in self.children], [row["pid"] for row in thread["providers"]])
        self.assertTrue(all(row["group_gone"] for row in thread["providers"]))
        requests = [json.loads(line) for line in (self.base / "native-provider-methods.jsonl").read_text().splitlines()]
        self.assertEqual(["initialize", "thread/start", "thread/read", "initialize", "thread/resume"], [row["method"] for row in requests])
        self.assertNotEqual(requests[0]["pid"], requests[3]["pid"])
        for name in ("release-owned-child", "unexpected-turn-started", "unexpected-turn-completed"):
            self.assertFalse((self.base / name).exists())
        self.assertIn(result["boot_prompt"], result["operator_action"])
        self.assertEqual([str(self.base / "creation-codex-fixture"), "--cd", str(self.home / "repo"), "resume", thread["thread_id"]], thread["resume_argv"])
        self.assertEqual(str(self.config.parent), thread["provider_home"])
        self.assertEqual({"CODEX_HOME": str(self.config.parent)}, thread["resume_env"])
        self.assertEqual("paused", WakeController(self.root).status(result["node_id"], thread["thread_id"])["state"])
        self.assert_reversed_pause(thread["thread_id"])

    def test_declared_codex_symlink_stays_resumable_after_version_target_changes(self):
        from floati import wake_daemon_adapters

        previous = creation_provider(self.base)
        declared = self.base / "codex"
        declared.symlink_to(previous)
        with mock.patch.object(wake_daemon_adapters, "CODEX_EXECUTABLE", declared):
            code, output, errors = self.add("--mode", "cli", "--codex-executable", str(declared))
            self.assertEqual(0, code, errors or output)
            first = json.loads(output)["evidence"]
            _, record = self.record()
            self.assertEqual(str(declared), record["request"]["codex_executable"])
            self.assertEqual(str(declared), first["thread"]["resume_argv"][0])
            upgraded = self.base / "upgraded-codex"
            upgraded.write_bytes(previous.read_bytes())
            upgraded.chmod(0o700)
            declared.unlink()
            declared.symlink_to(upgraded)
            previous.unlink()
            code, output, errors = self.add("--mode", "cli", "--codex-executable", str(declared))
            self.assertEqual(0, code, errors or output)
            replay = json.loads(output)["evidence"]
            self.assertEqual("historical", replay["thread"]["startup_receipt"])
            self.assertEqual(first["thread"]["resume_argv"], replay["thread"]["resume_argv"])
            self.assert_reversed_pause(first["thread"]["thread_id"])

    def test_codex_symlink_exception_does_not_allow_undeclared_or_invalid_executables(self):
        from floati import wake_daemon_adapters

        provider = creation_provider(self.base)
        declared = self.base / "codex"
        alias = self.base / "other-codex"
        alias.symlink_to(provider)
        with mock.patch.object(wake_daemon_adapters, "CODEX_EXECUTABLE", declared):
            for candidate, target in ((alias, None), (declared, "nonexecutable"), (declared, "missing")):
                with self.subTest(candidate=candidate, target=target):
                    if target is not None:
                        declared.unlink(missing_ok=True)
                        destination = self.base / target
                        if target == "nonexecutable":
                            destination.write_text("plain file\n")
                            destination.chmod(0o600)
                        declared.symlink_to(destination)
                    code, output, errors = self.add("--mode", "cli", "--codex-executable", str(candidate))
                    self.assertEqual(20, code, errors or output)
                    self.assertEqual("seat_onboard_invalid", json.loads(output)["evidence"]["code"])
                    self.assertFalse(self.home.exists())
                    self.assertFalse(seat_onboarding._record_path(self.root, self.home).exists())

    def test_creator_read_materializes_lazy_store_before_fresh_resume_without_turn(self):
        code, result, errors = self.start(failure="lazy_store")
        self.assertEqual(0, code, errors or result)
        requests = [json.loads(line) for line in (self.config.parent / "native-provider-methods.jsonl").read_text().splitlines()]
        self.assertEqual(["initialize", "thread/start", "thread/read", "initialize", "thread/resume"], [row["method"] for row in requests])
        self.assertEqual(requests[1]["pid"], requests[2]["pid"])
        self.assertNotEqual(requests[2]["pid"], requests[3]["pid"])
        self.assertFalse(result["thread"]["turn_started"])
        self.assertTrue(all(child.poll() == 0 for child in self.children))
        self.assert_reversed_pause(result["thread"]["thread_id"])

    def test_creator_read_rejects_malformed_different_or_prior_turn_evidence(self):
        for index, failure in enumerate(("read_missing", "read_wrong_id", "read_prior_turn")):
            with self.subTest(failure=failure):
                if index:
                    self.setUp()
                code, evidence, errors = self.start(failure=failure)
                self.assertEqual(20, code, errors or evidence)
                requests = [json.loads(line) for line in (self.config.parent / "native-provider-methods.jsonl").read_text().splitlines()]
                self.assertEqual(["initialize", "thread/start", "thread/read"], [row["method"] for row in requests])
                _, record = self.record()
                self.assertFalse(record["cli_start"]["resumable"])
                self.assertTrue(all(child.poll() == 0 for child in self.children))
                self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_fresh_resume_proves_metadata_without_full_history_hydration(self):
        code, result, errors = self.start(failure="resume_metadata_only")
        self.assertEqual(0, code, errors or result)
        frames = [json.loads(line) for line in (self.home / "scratch" / "cli-start.jsonl").read_text().splitlines()]
        outbound = [frame["message"] for frame in frames if frame.get("direction") == "out"]
        resumed = [frame for frame in outbound if frame.get("method") == "thread/resume"]
        self.assertEqual(1, len(resumed))
        self.assertIs(True, resumed[0]["params"]["excludeTurns"])
        self.assertEqual(result["thread"]["thread_id"], resumed[0]["params"]["threadId"])
        self.assertNotIn("turn/start", [frame.get("method") for frame in outbound])
        self.assertFalse(result["thread"]["turn_started"])
        self.assertEqual([0, 0], [child.poll() for child in self.children])
        self.assert_reversed_pause(result["thread"]["thread_id"])

    def test_metadata_resume_still_rejects_prior_turn_or_missing_status(self):
        for index, failure in enumerate(("resume_prior_turn", "resume_missing_status")):
            with self.subTest(failure=failure):
                if index:
                    self.setUp()
                code, evidence, errors = self.start(failure=failure)
                self.assertEqual(20, code, errors or evidence)
                _, record = self.record()
                self.assertFalse(record["cli_start"]["resumable"])
                self.assertEqual(2, len(self.children))
                self.assertTrue(all(child.poll() == 0 for child in self.children))
                self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_known_fresh_resume_status_can_precede_correlated_response(self):
        code, result, errors = self.start(failure="resume_status_first")
        self.assertEqual(0, code, errors or result)
        self.assertFalse(result["thread"]["turn_started"])
        self.assertEqual([0, 0], [child.poll() for child in self.children])
        self.assert_reversed_pause(result["thread"]["thread_id"])

    def test_fresh_resume_status_still_requires_initialized_known_exact_identity(self):
        for index, failure in enumerate(("resume_status_initialize", "resume_status_wrong_id", "resume_status_active", "resume_status_bad_time")):
            with self.subTest(failure=failure):
                if index:
                    self.setUp()
                code, evidence, errors = self.start(failure=failure)
                self.assertEqual(20, code, errors or evidence)
                self.assertIn("protocol_error", evidence["detail"])
                _, record = self.record()
                self.assertFalse(record["cli_start"]["resumable"])
                self.assertTrue(all(child.poll() == 0 for child in self.children))
                self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_owned_empty_goal_observation_after_resume_is_drained(self):
        code, result, errors = self.start(failure="goal_cleared")
        self.assertEqual(0, code, errors or result)
        frames = [json.loads(line) for line in (self.home / "scratch" / "cli-start.jsonl").read_text().splitlines()]
        self.assertEqual(1, len([frame for frame in frames if frame.get("method") == "thread/goal/cleared"]))
        outbound = [frame["message"].get("method") for frame in frames if frame.get("direction") == "out"]
        self.assertFalse(any(method.startswith("thread/goal/") or method == "turn/start" for method in outbound))
        self.assertEqual([0, 0], [child.poll() for child in self.children])
        self.assert_reversed_pause(result["thread"]["thread_id"])

    def test_goal_observation_requires_exact_resume_phase_identity_and_closed_shape(self):
        for index, failure in enumerate(("goal_initialize", "goal_create", "goal_no_id", "goal_wrong_id", "goal_extra", "goal_set", "goal_bad_time")):
            with self.subTest(failure=failure):
                if index:
                    self.setUp()
                code, evidence, errors = self.start(failure=failure)
                self.assertEqual(20, code, errors or evidence)
                self.assertIn("protocol_error", evidence["detail"])
                _, record = self.record()
                self.assertFalse(record["cli_start"]["resumable"])
                self.assertTrue(all(child.poll() == 0 for child in self.children))
                self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_named_read_absence_can_precede_independent_fresh_resume_proof(self):
        code, result, errors = self.start(failure="read_absence")
        self.assertEqual(0, code, errors or result)
        transcript = (self.home / "scratch" / "cli-start.jsonl").read_text()
        self.assertIn('"code": -32601, "message": "list_turns is not supported yet"', transcript)
        self.assertTrue(result["thread"]["resumable"])
        self.assertFalse(result["thread"]["turn_started"])
        self.assertEqual([0, 0], [child.poll() for child in self.children])
        self.assert_reversed_pause(result["thread"]["thread_id"])

    def test_named_read_absence_does_not_prove_persistence_without_fresh_resume(self):
        code, evidence, errors = self.start(failure="read_absence_without_store")
        self.assertEqual(20, code, errors or evidence)
        self.assertIn("resume failed", evidence["detail"])
        _, record = self.record()
        self.assertFalse(record["cli_start"]["resumable"])
        self.assertEqual(2, len(self.children))
        self.assertTrue(all(child.poll() == 0 for child in self.children))
        self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_read_absence_must_be_exact_closed_and_correlated(self):
        for index, failure in enumerate(("read_absence_wrong_id", "read_absence_bad_code", "read_absence_bool_code", "read_absence_float_code", "read_absence_bad_message", "read_absence_error_extra", "read_absence_outer_extra")):
            with self.subTest(failure=failure):
                if index:
                    self.setUp()
                code, evidence, errors = self.start(failure=failure)
                self.assertEqual(20, code, errors or evidence)
                self.assertIn("protocol_error", evidence["detail"])
                self.assertEqual(1, len(self.children))
                self.assertTrue(all(child.poll() == 0 for child in self.children))
                _, record = self.record()
                self.assertFalse(record["cli_start"]["resumable"])
                self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_native_disabled_remote_status_is_observational_and_timestamped(self):
        code, result, errors = self.start(failure="remote_disabled")
        self.assertEqual(0, code, errors or result)
        self.assertFalse(result["thread"]["turn_started"])
        self.assertTrue(all(child.poll() == 0 for child in self.children))
        transcript = (self.home / "scratch" / "cli-start.jsonl").read_text()
        self.assertEqual(2, transcript.count('"method": "remoteControl/status/changed"'))
        self.assert_reversed_pause(result["thread"]["thread_id"])

    def test_remote_active_or_malformed_status_cannot_be_adopted(self):
        for index, failure in enumerate(("remote_active", "remote_missing", "remote_extra", "remote_environment", "remote_bad_id", "remote_bad_name", "remote_bool_time", "remote_negative_time", "remote_large_time")):
            with self.subTest(failure=failure):
                if index:
                    self.setUp()
                code, evidence, errors = self.start(failure=failure)
                self.assertEqual(20, code, errors or evidence)
                self.assertIn("protocol_error", evidence["detail"])
                _, record = self.record()
                self.assertIsNone(record["cli_start"]["thread_id"])
                self.assertIsNone(record["cli_start"]["pause_intent"])
                self.assertTrue(all(child.poll() == 0 for child in self.children))
                code, result, errors = self.remove()
                self.assertEqual(0, code, errors or result)

    def test_first_board_resumes_pause_and_add_replay_labels_historical_start(self):
        from floati.seat_board import SeatBoard
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        thread = result["thread"]
        boarded = SeatBoard(self.root).board(result["node_id"], self.home / "repo", thread["thread_id"], idempotency_key="fixture-first-board")
        self.assertEqual("resumed", boarded["resume_outcome"])
        code, output, errors = self.add("--mode", "cli", "--codex-executable", str(self.base / "creation-codex-fixture"))
        self.assertEqual(0, code, errors or output)
        replay = json.loads(output)["evidence"]["thread"]
        self.assertEqual("historical", replay["startup_receipt"])
        self.assertEqual("not_paused", replay["current_wake_control_state"])
        self.assertEqual(2, len(self.children))
        self.assert_reversed_pause(thread["thread_id"])

    def test_resume_failure_is_typed_and_owned_pause_reverses(self):
        code, evidence, errors = self.start(failure="resume")
        self.assertEqual(20, code, errors or evidence)
        self.assertEqual("seat_onboard_invalid", evidence["code"])
        self.assertIn("resume", evidence["detail"])
        _, record = self.record()
        self.assertFalse(record["cli_start"]["resumable"])
        self.assertTrue(all(child.poll() == 0 for child in self.children))
        self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_unreceipted_reserved_marker_reconciles_before_removal(self):
        original = WakeController._append
        def interrupt(controller, node, row):
            if row["operation"] == "pause":
                raise KeyboardInterrupt("fixture abrupt cut after marker publication")
            return original(controller, node, row)
        with mock.patch.object(WakeController, "_append", new=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.start()
        _, record = self.record()
        start = record["cli_start"]
        self.assertIsNone(start["pause_receipt_id"])
        self.assertIsNotNone(start["pause_intent"])
        ledger = WakeController(self.root)._ledger(record["node_id"])
        self.assertEqual([], read_records_snapshot(self.root, ledger, allowed_kinds={"wake_control_receipt"}))
        self.assertTrue(all(child.poll() == 0 for child in self.children))
        self.assert_reversed_pause(start["thread_id"])
        rows = read_records_snapshot(self.root, ledger, allowed_kinds={"wake_control_receipt"})
        self.assertEqual(["pause", "resume"], [row["operation"] for row in rows])
        self.assertEqual(start["pause_intent"]["id"], rows[0]["id"])
        self.assertEqual(rows[0]["id"], rows[1]["predecessor_receipt_id"])

    def test_pause_receipt_save_gap_reconciles_reserved_evidence(self):
        original = seat_onboarding._save
        interrupted = []
        def lose_receipt_save(path, record):
            start = record.get("cli_start")
            if start and start["pause_receipt_id"] is not None and not interrupted:
                interrupted.append(start["pause_receipt_id"])
                start["pause_receipt_id"] = None
                raise OSError("fixture last durable state retains only reservation")
            return original(path, record)
        with mock.patch.object(seat_onboarding, "_save", side_effect=lose_receipt_save):
            code, evidence, errors = self.start()
        self.assertEqual(20, code, errors or evidence)
        _, record = self.record()
        self.assertIsNone(record["cli_start"]["pause_receipt_id"])
        self.assertEqual(interrupted[0], record["cli_start"]["pause_intent"]["id"])
        self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_superseding_pause_preserved_before_any_capability_removal(self):
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        node, session = result["node_id"], result["thread"]["thread_id"]
        controller = WakeController(self.root)
        controller.resume(node, session, idempotency_key="caller-resume")
        foreign = controller.pause(node, session, idempotency_key="caller-pause")
        before = {path: path.read_bytes() for path in (Path(foreign["marker"]), self.config, self.profiles)}
        home_before = seat_onboarding._inventory(self.home)
        code, evidence, errors = self.remove()
        self.assertEqual(20, code, errors or evidence)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(home_before, seat_onboarding._inventory(self.home))

    def test_unsolicited_turn_after_creation_refuses_and_reverses(self):
        code, evidence, errors = self.start(failure="unsolicited_turn")
        self.assertEqual(20, code, errors or evidence)
        _, record = self.record()
        self.assertFalse(record["cli_start"]["resumable"])
        self.assert_reversed_pause(record["cli_start"]["thread_id"])

    def test_missing_status_cannot_be_positive_creation_evidence(self):
        code, evidence, errors = self.start(failure="missing_status")
        self.assertEqual(20, code, errors or evidence)
        self.assertIn("active/prior turn", evidence["detail"])

    def test_remove_recovers_native_and_parent_progress_fault_cuts(self):
        from floati import codex_hook_install
        for cut in ("quarantine", "resume_receipt", "progress_save"):
            with self.subTest(cut=cut):
                if cut != "quarantine":
                    self.setUp()  # separate scratch root and provider store per cut
                code, result, errors = self.start()
                self.assertEqual(0, code, errors or result)
                _, record = self.record()
                start = record["cli_start"]
                controller = WakeController(self.root)
                marker = Path(controller.status(result["node_id"], start["thread_id"])["marker"])
                original_rename = codex_hook_install._rename_generation_noreplace
                original_append = WakeController._append
                original_save = seat_onboarding._save
                def interrupted_rename(source, target):
                    original_rename(source, target)
                    if source == marker:
                        raise KeyboardInterrupt("fixture after native quarantine")
                def interrupted_append(instance, node, row):
                    committed = original_append(instance, node, row)
                    if row["operation"] == "resume":
                        raise KeyboardInterrupt("fixture after native resume receipt")
                    return committed
                def interrupted_save(path, candidate):
                    if "cli" in candidate.get("removal_steps", []):
                        raise KeyboardInterrupt("fixture before parent cleanup progress save")
                    return original_save(path, candidate)
                patcher = (mock.patch.object(codex_hook_install, "_rename_generation_noreplace", side_effect=interrupted_rename)
                    if cut == "quarantine" else mock.patch.object(WakeController, "_append", new=interrupted_append)
                    if cut == "resume_receipt" else mock.patch.object(seat_onboarding, "_save", side_effect=interrupted_save))
                with patcher:
                    with self.assertRaises(KeyboardInterrupt):
                        self.remove()
                _, pending = self.record()
                self.assertEqual("removing", pending["state"])
                self.assertNotIn("cli", pending.get("removal_steps", []))
                self.assert_reversed_pause(start["thread_id"])
                rows = read_records_snapshot(self.root, controller._ledger(result["node_id"]), allowed_kinds={"wake_control_receipt"})
                self.assertEqual(1, len([row for row in rows if row["operation"] == "resume"]))

    def test_malformed_or_uncorrelated_protocol_cannot_be_adopted(self):
        for failure in ("duplicate", "wrong_id", "deep", "NaN", "Infinity", "status_without_id", "oversized", "frame_count"):
            with self.subTest(failure=failure):
                if failure != "duplicate":
                    self.setUp()
                code, evidence, errors = self.start(failure=failure)
                self.assertEqual(20, code, errors or evidence)
                _, record = self.record()
                self.assertIsNone(record["cli_start"]["pause_intent"])
                self.assertFalse(record["cli_start"]["resumable"])
                self.assertTrue(all(child.poll() == 0 for child in self.children))

    def test_uncertain_process_group_keeps_output_cut_pending_and_preserves_capabilities(self):
        with mock.patch("floati.thread_source._group_exists", return_value=True):
            code, evidence, errors = self.start()
        self.assertEqual(20, code, errors or evidence)
        _, record = self.record()
        self.assertEqual("pending", record["child_outputs"][-1]["state"])
        self.assertEqual(0, record["cli_start"]["providers"][0]["exit_code"])
        self.assertFalse(record["cli_start"]["providers"][0]["group_gone"])
        before = {path: path.read_bytes() for path in (self.config, self.profiles)}
        inventory = seat_onboarding._inventory(self.home)
        code, evidence, errors = self.remove()
        self.assertEqual(20, code, errors or evidence)
        self.assertIn("no finalized output inventory", evidence["detail"])
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(inventory, seat_onboarding._inventory(self.home))

    def test_tampered_reserved_session_refuses_before_pause_or_home_removal(self):
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        path, record = self.record()
        marker = Path(WakeController(self.root).status(result["node_id"], result["thread"]["thread_id"])["marker"])
        record["cli_start"]["pause_intent"]["session_digest"] = "f" * 64
        path.write_text(json.dumps(record) + "\n")
        before = {file: file.read_bytes() for file in (marker, self.profiles, self.config)}
        inventory = seat_onboarding._inventory(self.home)
        code, evidence, errors = self.remove()
        self.assertEqual(20, code, errors or evidence)
        self.assertEqual("seat_onboard_invalid", evidence["code"])
        self.assertEqual(before, {file: file.read_bytes() for file in before})
        self.assertEqual(inventory, seat_onboarding._inventory(self.home))

    def test_cleanup_progress_and_cached_start_fields_cannot_skip_native_reversal(self):
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        path, original = self.record()
        capability = seat_onboarding._cli_pause_capability(self.root, original)
        before = {file: file.read_bytes() for file in (Path(capability["marker"]), self.profiles, self.config)}
        inventory = seat_onboarding._inventory(self.home)
        variants = []
        for steps in (["registry"], ["cli", "registry"], ["cli"]):
            candidate = json.loads(json.dumps(original))
            candidate["removal_steps"] = steps
            candidate["cli_removal"] = {"before": None, "after": None}
            variants.append(candidate)
        candidate = json.loads(json.dumps(original))
        candidate["removal_steps"] = ["cli"]
        candidate["cli_removal"] = {"before": capability, "after": {**capability, "state": "absent"}}
        variants.append(candidate)  # an ordered but false completion still fails observation
        for field, value in (("provider_state", "active"), ("cached_session_state", "healthy"),
                ("exit_code", False), ("output_paths", ["repo/foreign"]), ("resume_prompt", "wrong boot")):
            candidate = json.loads(json.dumps(original))
            candidate["result"]["thread"][field] = value
            variants.append(candidate)
        for index, candidate in enumerate(variants):
            with self.subTest(index=index):
                path.write_text(json.dumps(candidate) + "\n")
                code, evidence, errors = self.remove()
                self.assertEqual(20, code, errors or evidence)
                self.assertEqual(before, {file: file.read_bytes() for file in before})
                self.assertEqual(inventory, seat_onboarding._inventory(self.home))
        path.write_text(json.dumps(original) + "\n")

    def test_completed_pause_cleanup_reobserves_supersession_before_other_cleanup(self):
        from floati import seat_harness_config
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        with mock.patch.object(seat_harness_config, "restore", side_effect=OSError("fixture config restore stopped")):
            code, evidence, errors = self.remove()
        self.assertEqual(20, code, errors or evidence)
        _, pending = self.record()
        self.assertEqual(["cli"], pending["removal_steps"])
        controller = WakeController(self.root)
        foreign = controller.pause(result["node_id"], result["thread"]["thread_id"], idempotency_key="caller-pause-after-cleanup")
        before = {file: file.read_bytes() for file in (Path(foreign["marker"]), self.profiles, self.config)}
        inventory = seat_onboarding._inventory(self.home)
        code, evidence, errors = self.remove()
        self.assertEqual(20, code, errors or evidence)
        self.assertEqual(before, {file: file.read_bytes() for file in before})
        self.assertEqual(inventory, seat_onboarding._inventory(self.home))

    def test_cached_removal_cannot_disagree_with_native_capability_inventory(self):
        code, result, errors = self.start()
        self.assertEqual(0, code, errors or result)
        self.assert_reversed_pause(result["thread"]["thread_id"])
        path, record = self.record()
        record["remove_result"]["cli_pause"]["after"]["state"] = "paused"
        path.write_text(json.dumps(record) + "\n")
        code, evidence, errors = self.remove()
        self.assertEqual(20, code, errors or evidence)
        self.assertIn("capability inventory", evidence["detail"])

    def test_alternate_explicit_provider_home_overrides_inherited_home_for_both_connections(self):
        alternate = self.base / "alternate provider home"
        alternate.mkdir()
        hooks = alternate / "hooks.json"
        hooks.write_bytes(self.hooks.read_bytes())
        config = alternate / "config.toml"
        config.write_text(self.config.read_text().replace(str(self.hooks), str(hooks)))
        self.config, self.hooks = config, hooks
        inherited = self.base / "foreign provider home"
        inherited.mkdir()
        sentinel = inherited / "personal.txt"
        sentinel.write_text("foreign home survives\n")
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(inherited), "CODEX_THREAD_ID": "caller-thread", "CODEX_TURN_ID": "caller-turn"}):
            code, result, errors = self.start()
            self.assertEqual("caller-thread", os.environ["CODEX_THREAD_ID"])
            self.assertEqual("caller-turn", os.environ["CODEX_TURN_ID"])
        self.assertEqual(0, code, errors or result)
        thread = result["thread"]
        self.assertEqual(str(alternate), thread["provider_home"])
        self.assertEqual({"CODEX_HOME": str(alternate)}, thread["resume_env"])
        requests = [json.loads(line) for line in (alternate / "native-provider-methods.jsonl").read_text().splitlines()]
        self.assertTrue(all(row["provider_home"] == str(alternate) for row in requests))
        self.assertTrue(all(row["caller_thread_id"] is None and row["caller_turn_id"] is None for row in requests))
        handoff = shlex.join(["env", "CODEX_HOME=" + str(alternate), *thread["resume_argv"]])
        self.assertIn(handoff, result["operator_action"])
        self.assertEqual("foreign home survives\n", sentinel.read_text())
        self.assertFalse((inherited / "native-provider-store.json").exists())
        self.assert_reversed_pause(thread["thread_id"])

    def test_nonstandard_cli_config_basename_refuses_before_seat_writes_or_provider_launch(self):
        alternate = self.base / "alternate-config.toml"
        alternate.write_bytes(self.config.read_bytes())
        executable = creation_provider(self.base)
        before = {file: file.read_bytes() for file in (self.profiles, alternate, self.root.path / "registry/entries.jsonl")}
        code, output, errors = self.add("--mode", "cli", "--codex-config", str(alternate),
            "--codex-executable", str(executable))
        self.assertEqual(20, code, errors or output)
        self.assertIn("config.toml", json.loads(output)["evidence"]["detail"])
        self.assertFalse(self.home.exists())
        self.assertFalse(seat_onboarding._record_path(self.root, self.home).exists())
        self.assertFalse((self.base / "native-provider-methods.jsonl").exists())
        self.assertEqual(before, {file: file.read_bytes() for file in before})


if __name__ == "__main__":
    unittest.main()

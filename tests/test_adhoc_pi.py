"""Persistent Pi contracts measured only with fake processes and private state."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

from floati.worker_bootstrap_protocol import NATIVE_THREAD_BOUNDS
from tests.readiness import run_process
from tests.temp_roots import REAL_TEMP_ROOT

DRIVER = Path(__file__).parent / "fixtures/adhoc-native/pi_driver.py"
TOKEN = "0199e000000070008000000000000001"


class AdhocPiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.slot, self.workspace, self.store = [self.base / name for name in
            ("provider", "workspace", "independent-provider-store")]
        for path in (self.slot, self.workspace, self.store):
            path.mkdir(mode=0o700)
        self.context = {"schema_version": 1,
            "profile_id": "adhoc-profile-" + TOKEN, "profile_digest": "1" * 64,
            "dispatch_id": "adhoc-dispatch-" + TOKEN, "dispatch_digest": "2" * 64,
            "node_id": "temporary", "node_lease_id": "lease-" + TOKEN,
            "work_item_id": "work-" + TOKEN, "operation": "start", "provider_coordinate": None,
            "provider_state_binding": {"path": str(self.slot), "dev": self.slot.stat().st_dev,
                                       "ino": self.slot.stat().st_ino},
            "workspace_binding": {"profile_id": "adhoc-profile-" + TOKEN,
                "lane_opening_id": "lane-workspace-" + TOKEN, "node_lease_id": "lease-" + TOKEN,
                "workspace": str(self.workspace)},
            "permission": {"mode": "allowlisted_tools", "tools": ["read", "write", "edit"]},
            "bounds": dict(NATIVE_THREAD_BOUNDS),
            "effect_binding": {"run_id": "run-" + TOKEN, "item_id": "work-" + TOKEN,
                               "attempt_id": "attempt-" + TOKEN}}

    def run_adapter(self, context=None, mode="complete", *, umask=-1):
        context_path = self.base / "context.json"
        context_path.write_text(json.dumps(self.context if context is None else context))
        home = self.base / "driver-home"
        home.mkdir(mode=0o700, exist_ok=True)
        env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        result = run_process([sys.executable, "-B", str(DRIVER), str(context_path), mode,
            str(self.store)], "Pi driver returns its result", env=env, capture_output=True,
            umask=umask)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        return json.loads(result.stdout)

    def launches(self):
        path = self.store / "launches.jsonl"
        return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines()]

    def requests(self):
        path = self.store / "requests.jsonl"
        return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines()]

    def start(self):
        result = self.run_adapter()
        self.assertEqual("ok", result["code"], "lawful persistent Pi start must run")
        self.assertEqual(["opened", "terminal"], [event["event"] for event in result["events"]])
        coordinate = result["events"][0]["provider_coordinate"]
        self.assertEqual(coordinate, result["events"][1]["provider_coordinate"])
        return coordinate

    def resume(self, coordinate):
        context = deepcopy(self.context)
        context.update(operation="resume", provider_coordinate=coordinate, dispatch_digest="3" * 64)
        return context

    def test_second_process_resumes_recorded_session_only(self):
        coordinate = self.start()
        result = self.run_adapter(self.resume(coordinate))
        self.assertEqual("ok", result["code"])
        self.assertEqual(coordinate, result["events"][0]["provider_coordinate"])
        state = json.loads((self.store / "state.json").read_text())
        self.assertEqual((1, 2), (state["creations"], state["turns"]))
        first, second = self.launches()
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertIsNone(first["resume"])
        self.assertEqual(coordinate["session_file"], second["resume"])
        self.assertTrue(Path(second["resume"]).is_absolute())
        self.assertEqual(str(self.slot), second["home"])
        self.assertEqual(str(self.workspace), second["cwd"])
        self.assertNotIn("--no-session", first["argv"])
        self.assertNotIn("--session-id", second["argv"])
        self.assertEqual("2", (self.workspace / "PROOF.txt").read_text())

    def test_native_session_creation_is_private_under_permissive_parent_umask(self):
        result = self.run_adapter(mode="upstream-file-mode", umask=0o022)
        self.assertEqual("ok", result["code"], "ordinary upstream file creation must remain private")
        coordinate = result["events"][-1]["provider_coordinate"]
        self.assertEqual(0, Path(coordinate["session_file"]).stat().st_mode & 0o077)
        self.assertEqual("ok", self.run_adapter(self.resume(coordinate),
            mode="upstream-file-mode", umask=0o022)["code"])

    def test_permission_omission_never_launches(self):
        context = deepcopy(self.context)
        context["permission"] = None
        self.assertEqual("native_permission_unsupported", self.run_adapter(context)["code"])
        self.assertEqual([], self.launches())

    def test_tools_disabled_disables_discovered_resources(self):
        context = deepcopy(self.context)
        context["permission"] = {"mode": "tools_disabled"}
        result = self.run_adapter(context)
        self.assertEqual("ok", result["code"])
        argv = self.launches()[0]["argv"]
        for flag in ("--no-tools", "--no-extensions", "--no-skills", "--no-prompt-templates",
                     "--no-themes", "--no-context-files"):
            self.assertIn(flag, argv)
        self.assertNotIn("--tools", argv)
        self.assertFalse((self.store / "prompt-before-ack").exists())

    def test_missing_saved_file_cannot_prompt_new_session(self):
        coordinate = self.start()
        path = Path(coordinate["session_file"])
        saved, permissions = path.read_bytes(), path.stat().st_mode & 0o777
        for case, mode, code in (("missing", "complete", "native_session_unmaterialized"),
                                 ("empty", "complete", "native_session_unmaterialized"),
                                 ("header", "complete", "native_session_mismatch"),
                                 ("substitute", "substitute", "native_session_mismatch"),
                                 ("wrong-path", "wrong-path", "native_session_mismatch")):
            with self.subTest(case=case):
                path.write_bytes(saved)
                path.chmod(permissions)
                launches = len(self.launches())
                turns = json.loads((self.store / "state.json").read_text())["turns"]
                prompts = sum(row["type"] == "prompt" for row in self.requests())
                if case == "missing":
                    path.unlink()
                elif case == "empty":
                    path.write_bytes(b"")
                elif case == "header":
                    header = json.loads(saved.splitlines()[0])
                    header["id"] = "foreign-session"
                    path.write_text(json.dumps(header) + "\n")
                result = self.run_adapter(self.resume(coordinate), mode=mode)
                self.assertEqual(code, result["code"])
                if case in ("missing", "empty", "header"):
                    self.assertEqual(launches, len(self.launches()))
                if case == "substitute":
                    self.assertEqual([], result["events"])
                self.assertFalse((self.store / "replacement-created").exists())
                self.assertEqual(turns, json.loads((self.store / "state.json").read_text())["turns"])
                self.assertEqual(prompts, sum(row["type"] == "prompt" for row in self.requests()))

    def test_state_must_be_idle_before_prompt(self):
        for mode in ("busy", "compacting", "queued"):
            with self.subTest(mode=mode):
                result = self.run_adapter(mode=mode)
                self.assertEqual("native_session_mismatch", result["code"])
                self.assertFalse(any(row["type"] == "prompt" for row in self.requests()))

    def test_allocated_without_header_is_not_resumable(self):
        result = self.run_adapter(mode="unmaterialized")
        self.assertEqual("native_session_unmaterialized", result["code"])
        self.assertEqual(["opened"], [event["event"] for event in result["events"]])
        coordinate = result["events"][0]["provider_coordinate"]
        self.assertFalse(Path(coordinate["session_file"]).exists())
        self.assertEqual("native_session_unmaterialized",
                         self.run_adapter(self.resume(coordinate))["code"])
        self.assertEqual(1, len(self.launches()))

    def test_agent_end_followed_by_retry_is_not_terminal(self):
        for mode, code in (("retry", "ok"),
                           ("settled-busy", "native_session_mismatch"),
                           ("extension-ui", "approval_required_unattended"),
                           ("tail-overflow", "native_output_limit"),
                           ("assistant-error", "turn_failed"),
                           ("assistant-error-retry", "ok"),
                           ("retry-exhausted", "turn_failed")):
            with self.subTest(mode=mode):
                self.store = self.base / mode
                self.store.mkdir(mode=0o700)
                continuation = self.workspace / "RETRY.txt"
                if continuation.exists():
                    continuation.unlink()
                result = self.run_adapter(mode=mode)
                self.assertEqual(code, result["code"])
                if code == "ok":
                    self.assertEqual("finished continuation", continuation.read_text())
                    self.assertTrue((self.store / "settled-state-checked").exists())
                    self.assertEqual(["opened", "terminal"], [event["event"] for event in result["events"]])
                else:
                    self.assertFalse(any(event["event"] == "terminal" for event in result["events"]))
                if mode in ("assistant-error", "assistant-error-retry", "retry-exhausted"):
                    self.assertEqual(1, sum(row["type"] == "prompt" for row in self.requests()))

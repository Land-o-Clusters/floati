"""Native Claude persistence is testified by two independent fake processes."""
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from floati.worker_bootstrap_protocol import NATIVE_THREAD_BOUNDS
from tests.readiness import run_process
from tests.temp_roots import REAL_TEMP_ROOT

DRIVER = Path(__file__).parent / "fixtures" / "adhoc-native" / "claude_driver.py"
TOKEN = "0199e000000070008000000000000001"


class AdhocClaudeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.slot = self.base / "provider"
        self.slot.mkdir(mode=0o700)
        credential = self.slot / "claude-oauth-token"
        credential.touch(mode=0o600)
        credential.write_text("fixture-private-slot-oauth-token\n")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir(mode=0o700)
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
            "permission": {"mode": "dontAsk", "tools": ["Read", "Write", "Edit"]},
            "bounds": dict(NATIVE_THREAD_BOUNDS),
            "effect_binding": {"run_id": "run-" + TOKEN, "item_id": "work-" + TOKEN,
                               "attempt_id": "attempt-" + TOKEN}}

    def run_adapter(self, context=None, mode="complete"):
        path = self.base / "context.json"
        path.write_text(json.dumps(self.context if context is None else context))
        # The driver's own HOME is also disposable and distinct from native state.
        home = self.base / "driver-home"
        home.mkdir(mode=0o700, exist_ok=True)
        env = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
        result = run_process([sys.executable, "-B", str(DRIVER), str(path), mode],
                             "Claude driver returns its result", env=env, capture_output=True)
        self.assertEqual(0, result.returncode, result.stderr.decode(errors="replace"))
        return json.loads(result.stdout)

    def launches(self):
        path = self.slot / "launches.jsonl"
        return [] if not path.exists() else [json.loads(line) for line in path.read_text().splitlines()]

    def start(self):
        result = self.run_adapter()
        self.assertEqual("ok", result["code"], "native persistent Claude launch must succeed")
        self.assertTrue(result["reaped"])
        self.assertEqual(["terminal"], [event["event"] for event in result["events"]])
        return result["events"][0]["provider_coordinate"]

    def resume(self, coordinate):
        context = deepcopy(self.context)
        context.update(operation="resume", provider_coordinate=coordinate,
                       dispatch_digest="3" * 64)
        return context

    def test_second_process_resumes_recorded_session_only(self):
        coordinate = self.start()
        saved_before = json.loads((self.slot / "saved-session.json").read_text())
        result = self.run_adapter(self.resume(coordinate))
        self.assertEqual("ok", result["code"])
        self.assertTrue(result["reaped"])
        self.assertEqual(coordinate, result["events"][0]["provider_coordinate"])
        saved_after = json.loads((self.slot / "saved-session.json").read_text())
        self.assertEqual(1, saved_after["creates"])
        self.assertEqual(1, saved_before["turns"])
        self.assertEqual(2, saved_after["turns"])
        first, second = self.launches()
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertIsNone(first["resume"])
        self.assertEqual(coordinate["session_id"], second["resume"])
        self.assertNotIn("--no-session-persistence", first["argv"])
        # Claude's own confinement is set explicitly on every launch, start and
        # resume alike: restricted mode plus the file-tool write block.
        for launch in (first, second):
            argv = launch["argv"]
            self.assertIn("--restricted", argv)
            self.assertEqual({"permissions": {"blockWritesOutsideWorkingDirectories": True}},
                             json.loads(argv[argv.index("--settings") + 1]))
        self.assertEqual("fixture turn 2\n", (self.workspace / "PROOF.txt").read_text())

    def test_private_slot_oauth_authenticates_after_keychain_login_failure(self):
        credential = self.slot / "claude-oauth-token"
        credential.unlink()
        provider = DRIVER.with_name("claude_provider.py")
        environment = {"PATH": os.defpath, "HOME": str(self.slot),
                       "CLAUDE_CONFIG_DIR": str(self.slot), "PYTHONDONTWRITEBYTECODE": "1"}
        login = run_process([sys.executable, "-B", str(provider), "auth", "login"],
            "Claude login fixture exits", env=environment, capture_output=True)
        self.assertEqual(1, login.returncode)
        self.assertIn(b"Keychain error", login.stderr)
        self.assertEqual([".claude.json"], [path.name for path in self.slot.iterdir()])
        if sys.platform == "darwin":
            with self.subTest(credential="missing"):
                result = self.run_adapter(mode="oauth")
                self.assertEqual("adhoc_binding_changed", result["code"],
                                 "macOS must refuse before a native child can fall back to Keychain")
                self.assertEqual([], result["events"])
                self.assertEqual([], self.launches())
        setup = run_process([sys.executable, "-B", str(provider), "setup-token"],
            "Claude setup-token fixture exits", env=environment, capture_output=True)
        self.assertEqual(0, setup.returncode)
        # Owner handoff stores the setup-token output once, without putting the
        # credential in argv, controller environment, a transcript or evidence.
        descriptor = os.open(credential, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(setup.stdout)
        conflicts = {name: "ambient-must-not-win" for name in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN_FD",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")}
        conflicts["CLAUDE_CODE_OAUTH_TOKEN"] = "ambient-token-must-not-win"
        with patch.dict(os.environ, conflicts):
            result = self.run_adapter(mode="oauth")
            self.assertEqual("ambient-token-must-not-win", os.environ["CLAUDE_CODE_OAUTH_TOKEN"])
        self.assertEqual("ok", result["code"],
                         "documented setup-token stored in the private slot must authenticate its child")
        self.assertTrue(result["reaped"])
        self.assertEqual(["terminal"], [event["event"] for event in result["events"]])
        for path in [*self.workspace.rglob("*"), *self.slot.rglob("*"), self.base / "context.json"]:
            if path.is_file() and path != credential:
                self.assertNotIn(setup.stdout.strip(), path.read_bytes(), str(path))
        self.assertNotIn(setup.stdout.decode().strip(), json.dumps(result))
        self.assertEqual(0o600, credential.stat().st_mode & 0o777)

    def test_oauth_credential_must_be_a_private_unaliased_slot_file(self):
        source = self.base / "outside-token"
        source.touch(mode=0o600)
        source.write_text("fixture-private-slot-oauth-token\n")
        for kind in ("symlink", "hardlink", "readable-by-group"):
            with self.subTest(kind=kind):
                slot = self.base / kind
                slot.mkdir(mode=0o700)
                credential = slot / "claude-oauth-token"
                if kind == "symlink":
                    credential.symlink_to(source)
                elif kind == "hardlink":
                    os.link(source, credential)
                else:
                    credential.touch(mode=0o640)
                    credential.write_text(source.read_text())
                context = deepcopy(self.context)
                context["provider_state_binding"] = {"path": str(slot),
                    "dev": slot.stat().st_dev, "ino": slot.stat().st_ino}
                result = self.run_adapter(context, mode="oauth")
                self.assertEqual("adhoc_binding_changed", result["code"])
                self.assertEqual([], result["events"])
                self.assertFalse((slot / "launches.jsonl").exists())

    def test_permission_omission_never_launches(self):
        context = deepcopy(self.context)
        context["permission"] = None
        result = self.run_adapter(context)
        self.assertEqual("native_permission_unsupported", result["code"])
        self.assertEqual([], self.launches())
        self.assertEqual([], list(self.workspace.iterdir()))

    def test_substituted_identity_never_completes(self):
        coordinate = self.start()
        result = self.run_adapter(self.resume(coordinate), mode="substitute")
        self.assertEqual("native_session_mismatch", result["code"])
        self.assertEqual([], result["events"])

    def test_create_crash_before_session_json_is_not_relaunched(self):
        result = self.run_adapter(mode="crash")
        self.assertEqual("adhoc_spawn_unknown", result["code"])
        self.assertEqual([], result["events"])
        self.assertEqual(1, len(self.launches()))

    def test_success_shaped_error_never_completes(self):
        result = self.run_adapter(mode="error")
        self.assertEqual("turn_failed", result["code"])
        self.assertEqual([], result["events"])

    def test_missing_identity_cannot_become_resumable(self):
        result = self.run_adapter(mode="missing-id")
        self.assertEqual("adhoc_spawn_unknown", result["code"])
        self.assertEqual([], result["events"])

    def test_stderr_limit_and_post_terminal_bytes_are_enforced(self):
        for mode, code in (("stderr-limit", "native_output_limit"),
                           ("post-terminal", "protocol_error")):
            with self.subTest(mode=mode):
                # Each variant is a distinct start and private provider slot.
                with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as isolated:
                    context = deepcopy(self.context)
                    slot = Path(isolated).resolve()
                    credential = slot / "claude-oauth-token"
                    credential.touch(mode=0o600)
                    credential.write_text("fixture-private-slot-oauth-token\n")
                    context["provider_state_binding"] = {"path": str(slot),
                        "dev": slot.stat().st_dev, "ino": slot.stat().st_ino}
                    result = self.run_adapter(context, mode=mode)
                    self.assertEqual(code, result["code"])
                    self.assertEqual([], result["events"])

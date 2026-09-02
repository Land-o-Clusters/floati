from __future__ import annotations

import json
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.host_paths import worker_workspace_root
from floati.ids import uuid7_hex
from floati.workers import WorkerAdapterFailure
from tests.temp_roots import REAL_TEMP_ROOT

try:
    import floati.adapters.codex_live as codex_live
    from floati.adapters.codex_live import AppServerSession, CodexAppServerAdapter
except (ImportError, ModuleNotFoundError):
    codex_live = None
    AppServerSession = None
    CodexAppServerAdapter = None


HARNESS = Path(__file__).parent / "fixtures" / "codex-app-server" / "reference_harness.py"
WORK_ID = "work-018f0f23abcd71238000000000000000"

# The dot-prefixed workspace name the pre-rename product wrote, built from
# the fence's own governed token rather than spelled: these fixtures drive a
# refusal (or assert an absence) whose whole mechanism is these exact bytes.
LEGACY_PREFIX = "." + RETIRED_PRODUCT_NAME



class CodexAppServerSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "floati-work" / WORK_ID
        self.workspace.mkdir(parents=True)

    def command(self, mode: str) -> tuple[str, ...]:
        return (str(Path(sys.executable).resolve()), str(HARNESS), "--mode", mode)

    def session(self, mode: str) -> object:
        self.assertIsNotNone(AppServerSession, "live app-server session must exist")
        return AppServerSession(self.command(mode), self.workspace)

    def requests(self) -> list[dict]:
        path = self.workspace / ".floati" / "harness-requests.jsonl"
        self.assertTrue(path.is_file(), "Floati evidence must include the request log")
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_session_drives_exact_stdio_sequence_and_correlates_responses(self) -> None:
        result = self.session("interleaved").run("Create PROOF.txt", deadline_seconds=5)

        self.assertIsNone(result)
        requests = self.requests()
        self.assertEqual(
            ["initialize", "initialized", "thread/start", "turn/start"],
            [message["method"] for message in requests],
        )
        self.assertEqual(
            {"name": "floati-worker", "version": "0"},
            requests[0]["params"]["clientInfo"],
        )
        thread = requests[2]["params"]
        self.assertEqual(str(self.workspace), thread["cwd"])
        self.assertEqual("on-request", thread["approvalPolicy"])
        self.assertEqual("user", thread["approvalsReviewer"])
        self.assertTrue(thread["ephemeral"])
        self.assertEqual("workspace-write", thread["sandbox"])
        turn = requests[3]["params"]
        self.assertEqual(str(self.workspace), turn["cwd"])
        self.assertEqual([{"type": "text", "text": "Create PROOF.txt"}], turn["input"])
        self.assertTrue((self.workspace / ".floati" / "transcript.jsonl").is_file())
        self.assertFalse(os.path.lexists(self.workspace / LEGACY_PREFIX))
        self.assertEqual("floati live worker proof\n", (self.workspace / "PROOF.txt").read_text())

    def test_session_refuses_legacy_workspace_before_evidence_or_process_start(self) -> None:
        self.assertIsNotNone(
            getattr(codex_live, "refuse_legacy_workspace_artifacts", None),
            "Codex session must preflight legacy workspace artifacts",
        )
        workspace = self.workspace / "legacy-session"
        workspace.mkdir(mode=0o700)
        legacy = workspace / f"{LEGACY_PREFIX}-session"
        contents = b"Codex legacy workspace sentinel\n"
        legacy.write_bytes(contents)
        metadata = legacy.lstat()
        identity = metadata.st_dev, metadata.st_ino
        session = AppServerSession(self.command("complete"), workspace)

        with (
            mock.patch.object(
                codex_live,
                "_open_private_file",
                side_effect=AssertionError("legacy refusal must precede transcript creation"),
            ),
            mock.patch.object(
                codex_live.subprocess,
                "Popen",
                side_effect=AssertionError("legacy refusal must precede provider launch"),
            ),
            self.assertRaises(ProtocolRefusal) as raised,
        ):
            session.start(deadline_seconds=5)

        self.assertEqual("legacy_workspace_artifacts", raised.exception.code)
        self.assertEqual(
            f"workspace refused: legacy artifact '{LEGACY_PREFIX}-session' predates the Floati rename; nothing was read, migrated, or deleted; start a fresh root, or archive the legacy artifacts yourself and run again",
            raised.exception.detail,
        )
        current = legacy.lstat()
        self.assertTrue(os.path.lexists(legacy))
        self.assertEqual(identity, (current.st_dev, current.st_ino))
        self.assertEqual(contents, legacy.read_bytes())
        self.assertFalse(os.path.lexists(workspace / ".floati"))
        self.assertFalse(os.path.lexists(workspace / ".git"))

    def test_failed_turn_and_malformed_envelope_are_distinct_typed_failures(self) -> None:
        for mode, code in (("failed", "turn_failed"), ("malformed", "protocol_error")):
            with self.subTest(mode=mode):
                workspace = self.workspace / mode
                workspace.mkdir()
                session = AppServerSession(self.command(mode), workspace)
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    session.run("Create PROOF.txt", deadline_seconds=5)
                self.assertEqual(code, caught.exception.code)

    def test_all_permission_requests_fail_closed_without_automatic_approval(self) -> None:
        cases = {
            "approval-command": {"decision": "cancel"},
            "approval-file": {"decision": "cancel"},
            "approval-permissions": {"permissions": {}, "scope": "turn"},
        }
        for mode, denial in cases.items():
            with self.subTest(mode=mode):
                workspace = self.workspace / mode
                workspace.mkdir()
                session = AppServerSession(self.command(mode), workspace)
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    session.run("Create PROOF.txt", deadline_seconds=5)
                self.assertEqual("approval_required_unattended", caught.exception.code)
                request_log = workspace / ".floati" / "harness-requests.jsonl"
                self.assertTrue(
                    request_log.is_file(),
                    "Floati evidence must include the request log",
                )
                records = [
                    json.loads(line)
                    for line in request_log.read_text(encoding="utf-8").splitlines()
                ]
                response = next(message for message in records if message.get("id") == "approval-1")
                self.assertEqual(denial, response["result"])
                self.assertNotIn("accept", json.dumps(response))

    def test_deadline_stops_a_hung_local_harness(self) -> None:
        with self.assertRaises(WorkerAdapterFailure) as caught:
            self.session("hang").run("Create PROOF.txt", deadline_seconds=0.1)

        self.assertEqual("process_timeout", caught.exception.code)


class CodexAppServerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_id = "work-" + uuid7_hex()
        self.workspace = worker_workspace_root() / self.work_id
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def command(self, mode: str) -> tuple[str, ...]:
        return (str(Path(sys.executable).resolve()), str(HARNESS), "--mode", mode)

    def item(self, workspace: Path | None = None) -> dict:
        return {
            "id": self.work_id,
            "title": "Create PROOF.txt",
            "workspace": str(workspace or self.workspace),
        }

    def prepared_round_trip(
        self, adapter: CodexAppServerAdapter, item: dict | None = None,
    ) -> tuple[dict, list[dict[str, str]]]:
        prepared_item = self.item() if item is None else item
        workspace = Path(str(prepared_item["workspace"]))
        workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace.mkdir(mode=0o700)
        metadata = workspace.lstat()
        adapter.set_prepared_workspace(
            str(workspace), metadata.st_dev, metadata.st_ino,
        )
        handle = adapter.spawn(prepared_item, deadline_seconds=5)
        return prepared_item, adapter.drive(
            handle, prepared_item, deadline_seconds=5,
        )

    @staticmethod
    def git(workspace: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments],
            cwd=workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def test_adapter_retains_clean_repository_and_binds_each_artifact_to_the_commit(self) -> None:
        self.assertIsNotNone(CodexAppServerAdapter, "live Codex adapter must exist")
        adapter = CodexAppServerAdapter(self.command("complete"))

        handle = adapter.spawn(self.item(), deadline_seconds=5)
        bindings = adapter.drive(handle, self.item(), deadline_seconds=5)

        self.assertEqual(1, len(bindings))
        binding = bindings[0]
        self.assertEqual(f"local/{self.work_id}", binding["repo"])
        self.assertEqual("PROOF.txt", binding["doc"])
        self.assertEqual(self.git(self.workspace, "rev-parse", "HEAD"), binding["sha"])
        self.assertEqual("commit", self.git(self.workspace, "cat-file", "-t", binding["sha"]))
        self.assertEqual("", self.git(self.workspace, "status", "--porcelain"))
        self.assertEqual(
            "Floati Worker <worker@floati.local>",
            self.git(self.workspace, "show", "-s", "--format=%an <%ae>", "HEAD"),
        )
        self.assertEqual(
            "floati live worker proof",
            self.git(self.workspace, "show", "HEAD:PROOF.txt"),
        )
        tracked = self.git(self.workspace, "ls-tree", "-r", "--name-only", "HEAD")
        self.assertEqual("PROOF.txt", tracked)
        self.assertTrue((self.workspace / ".floati" / "transcript.jsonl").is_file())
        self.assertFalse(os.path.lexists(self.workspace / LEGACY_PREFIX))

    def test_exec_adapter_provider_inherits_the_current_process_group(self) -> None:
        """Catches exec-owned Codex sessions creating a separate provider group."""
        observed_groups: list[int] = []
        real_popen = subprocess.Popen

        def capture_provider_group(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            command = args[0] if args else kwargs.get("args", ())
            if str(HARNESS) in command:
                observed_groups.append(os.getpgid(process.pid))
            return process

        adapter = CodexAppServerAdapter(
            self.command("complete"), isolate_process_group=False,
        )
        with mock.patch.object(codex_live.subprocess, "Popen", capture_provider_group):
            self.prepared_round_trip(adapter)

        self.assertEqual([os.getpgrp()], observed_groups)

    def test_built_in_adapter_accepts_only_exact_prepared_workspace_identity(self) -> None:
        """Catches an exact parent-prepared directory being rejected in the child."""
        adapter = CodexAppServerAdapter(self.command("complete"))

        item, bindings = self.prepared_round_trip(adapter)

        self.assertEqual(str(self.workspace), item["workspace"])
        self.assertEqual(["PROOF.txt"], [binding["doc"] for binding in bindings])
        metadata = self.workspace.lstat()
        self.assertEqual(os.getuid(), metadata.st_uid)
        self.assertEqual(0o700, stat.S_IMODE(metadata.st_mode))

        cases = ("wrong-identity", "wrong-mode", "not-empty", "non-canonical")
        for case in cases:
            with self.subTest(case=case):
                lawful_id = "work-" + uuid7_hex()
                lawful_path = self.workspace.parent / lawful_id
                self.addCleanup(shutil.rmtree, lawful_path, True)
                lawful_item = {
                    "id": lawful_id,
                    "title": "Create PROOF.txt",
                    "workspace": str(lawful_path),
                }
                _item, lawful_bindings = self.prepared_round_trip(
                    CodexAppServerAdapter(self.command("complete")), lawful_item,
                )
                self.assertEqual(
                    ["PROOF.txt"],
                    [binding["doc"] for binding in lawful_bindings],
                )

                work_id = "work-" + uuid7_hex()
                workspace = self.workspace.parent / work_id
                self.addCleanup(shutil.rmtree, workspace, True)
                workspace.mkdir(mode=0o700)
                prepared = workspace.lstat()
                binding_path = str(workspace)
                device, inode = prepared.st_dev, prepared.st_ino
                if case == "wrong-identity":
                    inode += 1
                elif case == "wrong-mode":
                    workspace.chmod(0o750)
                elif case == "not-empty":
                    (workspace / "intruder").write_text("occupied", encoding="utf-8")
                else:
                    binding_path = str(workspace.parent / "." / workspace.name)
                    binding_path = binding_path.replace(
                        "/floati-work/", "/floati-work/../floati-work/",
                    )
                refusing = CodexAppServerAdapter(self.command("complete"))
                refusing.set_prepared_workspace(binding_path, device, inode)
                item = {
                    "id": work_id,
                    "title": "Create PROOF.txt",
                    "workspace": str(workspace),
                }

                with self.assertRaises(WorkerAdapterFailure) as caught:
                    refusing.spawn(item, deadline_seconds=5)

                self.assertEqual("workspace_invalid", caught.exception.code)

    def test_prepared_workspace_symlink_replacement_refuses(self) -> None:
        """Catches path-only prepared bindings accepting a replacement symlink."""
        lawful = CodexAppServerAdapter(self.command("complete"))
        self.prepared_round_trip(lawful)

        replacement_id = "work-" + uuid7_hex()
        replacement_path = self.workspace.parent / replacement_id
        self.addCleanup(shutil.rmtree, replacement_path, True)
        replacement_path.mkdir(mode=0o700)
        original = replacement_path.lstat()
        replacement_path.rmdir()
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            target = Path(temporary) / "replacement"
            target.mkdir(mode=0o700)
            replacement_path.symlink_to(target, target_is_directory=True)
            adapter = CodexAppServerAdapter(self.command("complete"))
            adapter.set_prepared_workspace(
                str(replacement_path), original.st_dev, original.st_ino,
            )
            item = {
                "id": replacement_id,
                "title": "Create PROOF.txt",
                "workspace": str(replacement_path),
            }

            with self.assertRaises(WorkerAdapterFailure) as caught:
                adapter.spawn(item, deadline_seconds=5)

        self.assertEqual("workspace_invalid", caught.exception.code)

    def test_prepared_workspace_binding_is_one_shot(self) -> None:
        """Catches a consumed prepared identity authorizing another spawn."""
        adapter = CodexAppServerAdapter(self.command("complete"))
        item, _bindings = self.prepared_round_trip(adapter)

        with self.assertRaises(WorkerAdapterFailure) as caught:
            adapter.spawn(item, deadline_seconds=5)

        self.assertEqual("workspace_invalid", caught.exception.code)

    def test_invalid_spawn_attempt_consumes_prepared_workspace_binding(self) -> None:
        """Catches item validation leaving a prepared identity reusable."""
        self.prepared_round_trip(CodexAppServerAdapter(self.command("complete")))

        work_id = "work-" + uuid7_hex()
        workspace = self.workspace.parent / work_id
        self.addCleanup(shutil.rmtree, workspace, True)
        workspace.mkdir(mode=0o700)
        metadata = workspace.lstat()
        adapter = CodexAppServerAdapter(self.command("complete"))
        adapter.set_prepared_workspace(
            str(workspace), metadata.st_dev, metadata.st_ino,
        )

        with self.assertRaises(WorkerAdapterFailure) as first:
            adapter.spawn(
                {"id": work_id, "title": "missing workspace"},
                deadline_seconds=5,
            )
        self.assertEqual("workspace_mapping_missing", first.exception.code)

        with self.assertRaises(WorkerAdapterFailure) as second:
            adapter.spawn(
                {
                    "id": work_id,
                    "title": "must not reuse prepared identity",
                    "workspace": str(workspace),
                },
                deadline_seconds=5,
            )
        self.assertEqual("workspace_invalid", second.exception.code)

    def test_prepared_workspace_replacement_during_validation_refuses(self) -> None:
        """Catches pathname validation initializing a replacement directory."""
        self.prepared_round_trip(CodexAppServerAdapter(self.command("complete")))

        work_id = "work-" + uuid7_hex()
        workspace = self.workspace.parent / work_id
        original_path = workspace.with_name(workspace.name + "-original")
        self.addCleanup(shutil.rmtree, workspace, True)
        self.addCleanup(shutil.rmtree, original_path, True)
        workspace.mkdir(mode=0o700)
        metadata = workspace.lstat()
        adapter = CodexAppServerAdapter(self.command("complete"))
        adapter.set_prepared_workspace(
            str(workspace), metadata.st_dev, metadata.st_ino,
        )
        item = {
            "id": work_id,
            "title": "must not initialize a replacement",
            "workspace": str(workspace),
        }
        real_scandir = os.scandir
        real_listdir = os.listdir

        def replace_workspace() -> None:
            if original_path.exists():
                return
            workspace.rename(original_path)
            workspace.mkdir(mode=0o700)

        def replacing_scandir(path: object):
            replace_workspace()
            return real_scandir(path)

        def replacing_listdir(path: object):
            replace_workspace()
            return real_listdir(path)

        try:
            with (
                mock.patch.object(
                    codex_live.os, "scandir", side_effect=replacing_scandir,
                ),
                mock.patch.object(
                    codex_live.os, "listdir", side_effect=replacing_listdir,
                ),
                self.assertRaises(WorkerAdapterFailure) as caught,
            ):
                adapter.spawn(item, deadline_seconds=5)
        finally:
            adapter.cancel()

        self.assertEqual("workspace_invalid", caught.exception.code)
        self.assertFalse((workspace / ".git").exists())
        self.assertFalse((workspace / ".floati").exists())

    def test_prepared_workspace_replacement_after_acceptance_stays_on_inode(self) -> None:
        """Catches repository initialization reopening a replaced pathname."""
        self.prepared_round_trip(CodexAppServerAdapter(self.command("complete")))

        work_id = "work-" + uuid7_hex()
        workspace = self.workspace.parent / work_id
        original_path = workspace.with_name(workspace.name + "-accepted-original")
        self.addCleanup(shutil.rmtree, workspace, True)
        self.addCleanup(shutil.rmtree, original_path, True)
        workspace.mkdir(mode=0o700)
        metadata = workspace.lstat()

        class ReplaceBeforeInitialization(CodexAppServerAdapter):
            def _initialize_repository(
                self,
                initialization_workspace: Path,
                deadline: float,
                *,
                workspace_descriptor: int | None = None,
            ) -> None:
                if not original_path.exists():
                    workspace.rename(original_path)
                    workspace.mkdir(mode=0o700)
                if workspace_descriptor is None:
                    super()._initialize_repository(
                        initialization_workspace, deadline,
                    )
                else:
                    super()._initialize_repository(
                        initialization_workspace,
                        deadline,
                        workspace_descriptor=workspace_descriptor,
                    )

        adapter = ReplaceBeforeInitialization(self.command("complete"))
        adapter.set_prepared_workspace(
            str(workspace), metadata.st_dev, metadata.st_ino,
        )
        item = {
            "id": work_id,
            "title": "must not initialize post-accept replacement",
            "workspace": str(workspace),
        }
        handle = adapter.spawn(item, deadline_seconds=5)
        bindings = adapter.drive(handle, item, deadline_seconds=5)

        self.assertEqual(["PROOF.txt"], [binding["doc"] for binding in bindings])
        self.assertFalse((workspace / ".git").exists())
        self.assertFalse((workspace / ".floati").exists())

    def test_prepared_adapter_process_is_one_workspace_only(self) -> None:
        """Catches one effect-enabled adapter accepting a second workspace."""
        adapter = CodexAppServerAdapter(self.command("complete"))
        self.addCleanup(adapter.cancel)
        self.prepared_round_trip(adapter)

        second_id = "work-" + uuid7_hex()
        second = self.workspace.parent / second_id
        self.addCleanup(shutil.rmtree, second, True)
        second.mkdir(mode=0o700)
        metadata = second.lstat()
        adapter.set_prepared_workspace(str(second), metadata.st_dev, metadata.st_ino)
        item = {"id": second_id, "title": "must refuse second work", "workspace": str(second)}

        with self.assertRaises(WorkerAdapterFailure) as caught:
            adapter.spawn(item, deadline_seconds=5)

        self.assertEqual("workspace_invalid", caught.exception.code)

    def test_prepared_workspace_stays_pinned_before_evidence_creation(self) -> None:
        """Catches evidence creation reopening a post-validation replacement."""
        self.prepared_round_trip(CodexAppServerAdapter(self.command("complete")))
        work_id = "work-" + uuid7_hex()
        workspace = self.workspace.parent / work_id
        original = workspace.with_name(workspace.name + "-evidence-original")
        self.addCleanup(shutil.rmtree, workspace, True)
        self.addCleanup(shutil.rmtree, original, True)
        workspace.mkdir(mode=0o700)
        metadata = workspace.lstat()
        adapter = CodexAppServerAdapter(self.command("complete"))
        adapter.set_prepared_workspace(str(workspace), metadata.st_dev, metadata.st_ino)
        item = {"id": work_id, "title": "Create PROOF.txt", "workspace": str(workspace)}
        real_secure = codex_live._secure_directory
        replaced = False

        def replace_before_evidence(path: Path, *, create: bool) -> None:
            nonlocal replaced
            if create and path.name == ".floati" and not replaced:
                workspace.rename(original)
                workspace.mkdir(mode=0o700)
                replaced = True
            real_secure(path, create=create)

        with mock.patch.object(codex_live, "_secure_directory", replace_before_evidence):
            handle = adapter.spawn(item, deadline_seconds=5)
            try:
                adapter.drive(handle, item, deadline_seconds=5)
            except WorkerAdapterFailure as exc:
                self.fail(f"prepared lifecycle lost accepted inode: {exc.code}")

        self.assertTrue(replaced)
        self.assertFalse((workspace / ".git").exists())
        self.assertFalse((workspace / ".floati").exists())

    def test_prepared_workspace_stays_pinned_before_provider_launch(self) -> None:
        """Catches provider Popen reopening a post-validation replacement."""
        self.prepared_round_trip(CodexAppServerAdapter(self.command("complete")))
        work_id = "work-" + uuid7_hex()
        workspace = self.workspace.parent / work_id
        original = workspace.with_name(workspace.name + "-popen-original")
        self.addCleanup(shutil.rmtree, workspace, True)
        self.addCleanup(shutil.rmtree, original, True)
        workspace.mkdir(mode=0o700)
        metadata = workspace.lstat()
        adapter = CodexAppServerAdapter(self.command("complete"))
        adapter.set_prepared_workspace(str(workspace), metadata.st_dev, metadata.st_ino)
        item = {"id": work_id, "title": "Create PROOF.txt", "workspace": str(workspace)}
        real_popen = subprocess.Popen
        replaced = False

        def replace_before_provider(*args, **kwargs):
            nonlocal replaced
            command = args[0] if args else kwargs.get("args", ())
            if str(HARNESS) in command and not replaced:
                workspace.rename(original)
                workspace.mkdir(mode=0o700)
                replaced = True
            return real_popen(*args, **kwargs)

        with mock.patch.object(codex_live.subprocess, "Popen", replace_before_provider):
            handle = adapter.spawn(item, deadline_seconds=5)
            try:
                adapter.drive(handle, item, deadline_seconds=5)
            except WorkerAdapterFailure as exc:
                self.fail(f"provider did not remain on accepted inode: {exc.code}")

        self.assertTrue(replaced)
        self.assertFalse((workspace / ".git").exists())
        self.assertFalse((workspace / ".floati").exists())

    def test_prepared_workspace_restores_cwd_and_closes_directory_fds(self) -> None:
        """Catches a completed one-shot adapter leaking cwd or accepted dirfds."""
        before = os.stat(".")
        captured: list[int] = []
        real_open = codex_live.os.open

        def recording_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            if path == self.workspace or path == ".":
                captured.append(descriptor)
            return descriptor

        self.workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace.mkdir(mode=0o700)
        metadata = self.workspace.lstat()
        adapter = CodexAppServerAdapter(self.command("complete"))
        self.addCleanup(adapter.cancel)
        adapter.set_prepared_workspace(
            str(self.workspace), metadata.st_dev, metadata.st_ino,
        )
        with mock.patch.object(codex_live.os, "open", recording_open):
            handle = adapter.spawn(self.item(), deadline_seconds=5)
            pinned = os.stat(".")
            self.assertEqual(
                (metadata.st_dev, metadata.st_ino),
                (pinned.st_dev, pinned.st_ino),
            )
            for descriptor in captured:
                os.fstat(descriptor)
            adapter.drive(handle, self.item(), deadline_seconds=5)

        after = os.stat(".")
        self.assertEqual((before.st_dev, before.st_ino), (after.st_dev, after.st_ino))
        self.assertGreaterEqual(len(captured), 2)
        for descriptor in captured:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_legacy_adapter_still_creates_absent_workspace(self) -> None:
        """Catches the prepared path changing the legacy absent-path behavior."""
        adapter = CodexAppServerAdapter(self.command("complete"))
        self.assertFalse(self.workspace.exists())

        handle = adapter.spawn(self.item(), deadline_seconds=5)
        bindings = adapter.drive(handle, self.item(), deadline_seconds=5)

        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(["PROOF.txt"], [binding["doc"] for binding in bindings])

    def test_workspace_and_local_evidence_are_owner_private(self) -> None:
        adapter = CodexAppServerAdapter(self.command("complete"))

        handle = adapter.spawn(self.item(), deadline_seconds=5)
        adapter.drive(handle, self.item(), deadline_seconds=5)

        self.assertFalse(os.path.lexists(self.workspace / LEGACY_PREFIX))

        expected_modes = {
            self.workspace.parent: 0o700,
            self.workspace: 0o700,
            self.workspace / ".floati": 0o700,
            self.workspace / ".floati" / "transcript.jsonl": 0o600,
            self.workspace / ".floati" / "app-server.stderr": 0o600,
        }
        for path, expected in expected_modes.items():
            with self.subTest(path=path):
                self.assertTrue(path.exists(), f"Floati storage is missing: {path}")
                self.assertEqual(expected, stat.S_IMODE(path.stat().st_mode))

    def test_workspace_parent_rejects_symlink_and_wrong_owner(self) -> None:
        self.assertIsNotNone(codex_live)
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            base = Path(temporary)
            target = base / "target"
            target.mkdir()
            symlink = base / "parent-link"
            symlink.symlink_to(target, target_is_directory=True)
            with mock.patch.object(codex_live, "_WORKSPACE_PARENT", symlink):
                item = self.item(symlink / self.work_id)
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    CodexAppServerAdapter(self.command("complete")).spawn(
                        item, deadline_seconds=5
                    )
                self.assertEqual("workspace_invalid", caught.exception.code)

            parent = base / "owned-parent"
            parent.mkdir(mode=0o700)
            actual_uid = os.getuid()
            with (
                mock.patch.object(codex_live, "_WORKSPACE_PARENT", parent),
                mock.patch.object(codex_live.os, "getuid", return_value=actual_uid + 1),
            ):
                item = self.item(parent / self.work_id)
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    CodexAppServerAdapter(self.command("complete")).spawn(
                        item, deadline_seconds=5
                    )
                self.assertEqual("workspace_invalid", caught.exception.code)

    def test_git_finalization_uses_literal_paths_and_ignores_hooks_and_signing(self) -> None:
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            template = Path(temporary) / "template"
            hooks = template / "hooks"
            hooks.mkdir(parents=True)
            sentinel = Path(temporary) / "hook-ran"
            hook = hooks / "pre-commit"
            hook.write_text(
                f"#!/bin/sh\ntouch {sentinel}\nexit 1\n", encoding="utf-8"
            )
            hook.chmod(0o700)
            local_sentinel = Path(temporary) / "local-config-ran"
            local_filter = Path(temporary) / "local-filter"
            local_filter.write_text(
                f"#!/bin/sh\ntouch {local_sentinel}\ncat\n", encoding="utf-8"
            )
            local_filter.chmod(0o700)
            global_sentinel = Path(temporary) / "global-config-ran"
            global_monitor = Path(temporary) / "global-monitor"
            global_monitor.write_text(
                f"#!/bin/sh\ntouch {global_sentinel}\nexit 0\n", encoding="utf-8"
            )
            global_monitor.chmod(0o700)
            global_config = Path(temporary) / "global.gitconfig"
            global_config.write_text(
                f"[core]\n\tfsmonitor = {global_monitor}\n", encoding="utf-8"
            )
            adapter = CodexAppServerAdapter(self.command("complete-pathspec"))

            with mock.patch.dict(
                os.environ,
                {
                    "GIT_CONFIG_GLOBAL": str(global_config),
                    "GIT_TEMPLATE_DIR": str(template),
                },
            ):
                handle = adapter.spawn(self.item(), deadline_seconds=5)
                self.git(self.workspace, "config", "commit.gpgsign", "true")
                self.git(
                    self.workspace,
                    "config",
                    "filter.child-owned.clean",
                    str(local_filter),
                )
                (self.workspace / ".gitattributes").write_text(
                    "* filter=child-owned\n", encoding="utf-8"
                )
                bindings = adapter.drive(handle, self.item(), deadline_seconds=5)

            self.assertFalse(sentinel.exists())
            self.assertFalse(local_sentinel.exists())
            self.assertFalse(global_sentinel.exists())
            self.assertEqual(
                [".gitattributes", ":(exclude)PROOF.txt"],
                [row["doc"] for row in bindings],
            )
            self.assertEqual(
                ".gitattributes\n:(exclude)PROOF.txt",
                self.git(self.workspace, "ls-tree", "-r", "--name-only", "HEAD"),
            )
            self.assertEqual("", self.git(self.workspace, "status", "--porcelain"))

    def test_background_app_server_descendant_is_quiescent_before_git_finalization(self) -> None:
        adapter = CodexAppServerAdapter(self.command("complete-background-mutate"))

        handle = adapter.spawn(self.item(), deadline_seconds=5)
        bindings = adapter.drive(handle, self.item(), deadline_seconds=5)
        time.sleep(0.3)

        self.assertEqual(["PROOF.txt"], [row["doc"] for row in bindings])
        self.assertFalse((self.workspace / "LATE.txt").exists())
        self.assertEqual("", self.git(self.workspace, "status", "--porcelain"))

    def test_adapter_refuses_workspace_escape_missing_artifact_symlink_and_overflow(self) -> None:
        cases = (
            ("complete", Path("\x2fprivate/tmp/not-ruled") / self.work_id, "workspace_invalid"),
            ("complete-empty", self.workspace, "artifact_missing"),
            ("complete-replace-git", self.workspace, "git_finalize_failed"),
            ("complete-symlink", self.workspace, "artifact_ambiguous"),
            ("complete-many", self.workspace, "artifact_ambiguous"),
        )
        for mode, workspace, code in cases:
            with self.subTest(mode=mode):
                shutil.rmtree(self.workspace, ignore_errors=True)
                adapter = CodexAppServerAdapter(self.command(mode))
                item = self.item(workspace)
                try:
                    handle = adapter.spawn(item, deadline_seconds=5)
                    adapter.drive(handle, item, deadline_seconds=5)
                except WorkerAdapterFailure as caught:
                    self.assertEqual(code, caught.code)
                else:
                    self.fail(f"{mode} must fail closed with {code}")

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.host_paths import worker_workspace_root
from floati.ids import uuid7_hex
from floati.workers import WorkerAdapterFailure
from tests.temp_roots import REAL_TEMP_ROOT


HARNESS = Path(__file__).parent / "fixtures" / "pi-rpc" / "reference_harness.py"

# The dot-prefixed workspace name the pre-rename product wrote, built from
# the fence's own governed token rather than spelled: these fixtures drive a
# refusal (or assert an absence) whose whole mechanism is these exact bytes.
LEGACY_PREFIX = "." + RETIRED_PRODUCT_NAME


try:
    import floati.adapters.pi as pi
    from floati.adapters.pi import PiRpcAdapter, PiRpcSession
except (ImportError, ModuleNotFoundError):
    pi = None
    PiRpcAdapter = None
    PiRpcSession = None


class PiRpcSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name) / "workspace"
        self.workspace.mkdir()

    def command(self, mode: str) -> tuple[str, ...]:
        return (str(Path(sys.executable).resolve()), str(HARNESS), mode)

    def session(self, mode: str) -> object:
        self.assertIsNotNone(PiRpcSession, "pi RPC session must exist")
        return PiRpcSession(self.command(mode), self.workspace)

    def test_session_sends_prompt_with_lf_and_correlates_response(self) -> None:
        session = self.session("complete")
        session.run("Create PI-PROOF.txt", deadline_seconds=5)

        request_log = self.workspace / ".floati" / "pi-requests.raw"
        self.assertTrue(request_log.is_file(), "Floati evidence must include the request log")
        raw = request_log.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        self.assertEqual("prompt", json.loads(raw.decode("utf-8").splitlines()[0])["type"])
        self.assertEqual("FLOATI pi fixture proof\n", (self.workspace / "PI-PROOF.txt").read_text())
        self.assertFalse(os.path.lexists(self.workspace / LEGACY_PREFIX))

    def test_session_waits_for_terminal_agent_event(self) -> None:
        session = self.session("interleaved")
        session.run("Create PI-PROOF.txt", deadline_seconds=5)

        transcript_path = self.workspace / ".floati" / "transcript.jsonl"
        self.assertTrue(transcript_path.is_file(), "Floati evidence must include the transcript")
        transcript = transcript_path.read_text()
        self.assertIn('"type":"agent_end"', transcript)

    def test_session_treats_unicode_separator_as_payload(self) -> None:
        session = self.session("unicode")
        session.run("Create PI-PROOF.txt", deadline_seconds=5)

        self.assertTrue((self.workspace / "PI-PROOF.txt").is_file())

    def test_session_distinguishes_malformed_response_and_timeout(self) -> None:
        for mode, expected in (("malformed", "protocol_error"), ("timeout", "process_timeout")):
            with self.subTest(mode=mode):
                workspace = self.workspace / mode
                workspace.mkdir()
                session = PiRpcSession(self.command(mode), workspace)
                with self.assertRaises(WorkerAdapterFailure) as caught:
                    session.run("Create PI-PROOF.txt", deadline_seconds=0.1)
                self.assertEqual(expected, caught.exception.code)


class PiRpcAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.work_id = "work-" + uuid7_hex()
        self.workspace = worker_workspace_root() / self.work_id
        self.addCleanup(shutil.rmtree, self.workspace, True)

    def command(self, mode: str) -> tuple[str, ...]:
        return (str(Path(sys.executable).resolve()), str(HARNESS), mode)

    def test_adapter_finalizes_fixture_turn_through_worker_contract(self) -> None:
        self.assertIsNotNone(PiRpcAdapter, "pi RPC adapter must exist")
        adapter = PiRpcAdapter(self.command("complete"))
        item = {
            "id": self.work_id,
            "title": "Create PI-PROOF.txt",
            "workspace": str(self.workspace),
        }

        handle = adapter.spawn(item, deadline_seconds=5)
        bindings = adapter.drive(handle, item, deadline_seconds=5)

        self.assertEqual(["PI-PROOF.txt"], [binding["doc"] for binding in bindings])
        self.assertEqual("local/" + self.work_id, bindings[0]["repo"])
        git_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=self.workspace,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual("", git_status.stdout)

    def test_prepared_workspace_refuses_legacy_before_git_or_evidence_creation(self) -> None:
        self.assertIsNotNone(
            getattr(pi, "refuse_legacy_workspace_artifacts", None),
            "Pi adapter must preflight legacy workspace artifacts",
        )
        self.workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace.mkdir(mode=0o700)
        legacy = self.workspace / f"{LEGACY_PREFIX}-pi"
        contents = b"Pi legacy workspace sentinel\n"
        legacy.write_bytes(contents)
        metadata = self.workspace.lstat()
        legacy_metadata = legacy.lstat()
        item = {
            "id": self.work_id,
            "title": "Create PI-PROOF.txt",
            "workspace": str(self.workspace),
        }
        adapter = PiRpcAdapter(self.command("complete"))
        adapter.set_prepared_workspace(
            str(self.workspace), metadata.st_dev, metadata.st_ino,
        )

        with (
            mock.patch.object(
                pi.CodexAppServerAdapter,
                "_initialize_repository",
                side_effect=AssertionError("legacy refusal must precede Git initialization"),
            ),
            mock.patch.object(
                pi,
                "_open_private_file",
                side_effect=AssertionError("legacy refusal must precede evidence creation"),
            ),
            mock.patch.object(
                pi.subprocess,
                "Popen",
                side_effect=AssertionError("legacy refusal must precede provider launch"),
            ),
            self.assertRaises(ProtocolRefusal) as raised,
        ):
            adapter.spawn(item, deadline_seconds=5)

        self.assertEqual("legacy_workspace_artifacts", raised.exception.code)
        self.assertEqual(
            f"workspace refused: legacy artifact '{LEGACY_PREFIX}-pi' predates the Floati rename; nothing was read, migrated, or deleted; start a fresh root, or archive the legacy artifacts yourself and run again",
            raised.exception.detail,
        )
        current = legacy.lstat()
        self.assertTrue(os.path.lexists(legacy))
        self.assertEqual(
            (legacy_metadata.st_dev, legacy_metadata.st_ino),
            (current.st_dev, current.st_ino),
        )
        self.assertEqual(contents, legacy.read_bytes())
        self.assertFalse(os.path.lexists(self.workspace / ".floati"))
        self.assertFalse(os.path.lexists(self.workspace / ".git"))

    def test_exec_adapter_provider_inherits_the_current_process_group(self) -> None:
        """Catches exec-owned Pi sessions creating a separate provider group."""
        observed_groups: list[int] = []
        real_popen = subprocess.Popen

        def capture_provider_group(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            command = args[0] if args else kwargs.get("args", ())
            if str(HARNESS) in command:
                observed_groups.append(os.getpgid(process.pid))
            return process

        item = {
            "id": self.work_id,
            "title": "Create PI-PROOF.txt",
            "workspace": str(self.workspace),
        }
        adapter = PiRpcAdapter(
            self.command("complete"), isolate_process_group=False,
        )
        with mock.patch.object(pi.subprocess, "Popen", capture_provider_group):
            handle = adapter.spawn(item, deadline_seconds=5)
            adapter.drive(handle, item, deadline_seconds=5)

        self.assertEqual([os.getpgrp()], observed_groups)

    def test_prepared_workspace_round_trip_uses_exact_filesystem_identity(self) -> None:
        """Catches Pi bypassing the inherited prepared-workspace contract."""
        item = {
            "id": self.work_id,
            "title": "Create PI-PROOF.txt",
            "workspace": str(self.workspace),
        }
        self.workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.workspace.mkdir(mode=0o700)
        metadata = self.workspace.lstat()
        adapter = PiRpcAdapter(self.command("complete"))
        adapter.set_prepared_workspace(
            str(self.workspace), metadata.st_dev, metadata.st_ino,
        )

        handle = adapter.spawn(item, deadline_seconds=5)
        bindings = adapter.drive(handle, item, deadline_seconds=5)

        current = self.workspace.lstat()
        self.assertEqual((metadata.st_dev, metadata.st_ino), (current.st_dev, current.st_ino))
        self.assertEqual(os.getuid(), current.st_uid)
        self.assertEqual(0o700, stat.S_IMODE(current.st_mode))
        self.assertEqual(["PI-PROOF.txt"], [binding["doc"] for binding in bindings])

    def test_prepared_workspace_replacements_cannot_receive_evidence_or_provider(self) -> None:
        """Catches Pi reopening the ruled pathname for evidence or Popen."""
        for boundary in ("evidence", "popen"):
            with self.subTest(boundary=boundary):
                lawful_id = "work-" + uuid7_hex()
                lawful_workspace = self.workspace.parent / lawful_id
                self.addCleanup(shutil.rmtree, lawful_workspace, True)
                lawful_item = {"id": lawful_id, "title": "Create PI-PROOF.txt", "workspace": str(lawful_workspace)}
                lawful_workspace.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                lawful_workspace.mkdir(mode=0o700)
                lawful_metadata = lawful_workspace.lstat()
                lawful_adapter = PiRpcAdapter(self.command("complete"))
                lawful_adapter.set_prepared_workspace(str(lawful_workspace), lawful_metadata.st_dev, lawful_metadata.st_ino)
                lawful_handle = lawful_adapter.spawn(lawful_item, deadline_seconds=5)
                lawful_adapter.drive(lawful_handle, lawful_item, deadline_seconds=5)

                work_id = "work-" + uuid7_hex()
                workspace = self.workspace.parent / work_id
                original = workspace.with_name(workspace.name + "-original")
                self.addCleanup(shutil.rmtree, workspace, True)
                self.addCleanup(shutil.rmtree, original, True)
                workspace.mkdir(mode=0o700)
                metadata = workspace.lstat()
                item = {"id": work_id, "title": "Create PI-PROOF.txt", "workspace": str(workspace)}
                adapter = PiRpcAdapter(self.command("complete"))
                adapter.set_prepared_workspace(str(workspace), metadata.st_dev, metadata.st_ino)
                replaced = False
                real_secure = pi._secure_directory
                real_popen = subprocess.Popen

                def replace() -> None:
                    nonlocal replaced
                    if not replaced:
                        workspace.rename(original)
                        workspace.mkdir(mode=0o700)
                        replaced = True

                def secure(path: Path, *, create: bool) -> None:
                    if boundary == "evidence" and create and path.name == ".floati":
                        replace()
                    real_secure(path, create=create)

                def popen(*args, **kwargs):
                    command = args[0] if args else kwargs.get("args", ())
                    if boundary == "popen" and str(HARNESS) in command:
                        replace()
                    return real_popen(*args, **kwargs)

                with mock.patch.object(pi, "_secure_directory", secure), mock.patch.object(pi.subprocess, "Popen", popen):
                    handle = adapter.spawn(item, deadline_seconds=5)
                    try:
                        adapter.drive(handle, item, deadline_seconds=5)
                    except WorkerAdapterFailure as exc:
                        self.fail(f"prepared Pi lifecycle lost accepted inode: {exc.code}")

                self.assertTrue(replaced)
                self.assertFalse((workspace / ".git").exists())
                self.assertFalse((workspace / ".floati").exists())

    def test_worker_help_and_parser_name_pi_adapter(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "floati", "worker", "run", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("codex|pi", result.stdout)


if __name__ == "__main__":
    unittest.main()

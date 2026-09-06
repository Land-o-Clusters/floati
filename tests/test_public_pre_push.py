from __future__ import annotations

import importlib.machinery
import io
import json
import os
import sys
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.identity_fence import OWNER_USERNAME
from tests.temp_roots import REAL_TEMP_ROOT

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_AUTHOR_NAME": "fence-test",
            "GIT_AUTHOR_EMAIL": "fence-test@example.invalid",
            "GIT_COMMITTER_NAME": "fence-test",
            "GIT_COMMITTER_EMAIL": "fence-test@example.invalid",
        },
    )


def _run_with_path_prefix(cwd: Path, prefix: Path) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": f"{prefix}:/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_AUTHOR_NAME": "fence-test",
        "GIT_AUTHOR_EMAIL": "fence-test@example.invalid",
        "GIT_COMMITTER_NAME": "fence-test",
        "GIT_COMMITTER_EMAIL": "fence-test@example.invalid",
    }
    return subprocess.run(
        ["/usr/bin/git", "push", "origin", "main"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=environment,
    )


class PublicPrePushFenceTests(unittest.TestCase):
    """INV-3: the publication scans fence a push from a public clone."""

    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(dir=REAL_TEMP_ROOT))
        self.addCleanup(shutil.rmtree, self.temporary, True)
        self.clone = self.temporary / "clone"
        self.remote = self.temporary / "remote.git"
        self.clone.mkdir()
        _run(["git", "init", "-q", "-b", "main", str(self.clone)], self.temporary)
        _run(["git", "init", "-q", "--bare", str(self.remote)], self.temporary)
        _run(["git", "remote", "add", "origin", str(self.remote)], self.clone)
        (self.clone / "floati").mkdir()
        for name in ("__init__.py", "identity_fence.py", "scrub.py"):
            shutil.copy2(REPOSITORY_ROOT / "floati" / name, self.clone / "floati" / name)
        (self.clone / "scripts").mkdir()
        shutil.copy2(
            REPOSITORY_ROOT / "scripts/public_name_fence.py",
            self.clone / "scripts/public_name_fence.py",
        )
        hooks = self.clone / "scripts/public-hooks"
        hooks.mkdir()
        shutil.copy2(
            REPOSITORY_ROOT / "scripts/public-hooks/pre-push", hooks / "pre-push"
        )
        os.chmod(hooks / "pre-push", 0o755)
        _run(
            ["git", "config", "core.hooksPath", "scripts/public-hooks"], self.clone
        )
        (self.clone / "README.md").write_text("# a public floati clone\n")
        _run(["git", "add", "-A"], self.clone)
        _run(["git", "commit", "-q", "-m", "seed the public clone"], self.clone)

    def _push(self) -> subprocess.CompletedProcess[str]:
        return _run(["git", "push", "origin", "main"], self.clone)

    def test_clean_tree_pushes_and_reports_one_ok_artifact(self) -> None:
        completed = self._push()

        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        artifacts = [
            json.loads(line)
            for line in (completed.stderr + completed.stdout).splitlines()
            if line.startswith('{"artifact_version"')
        ]
        self.assertEqual(1, len(artifacts))
        self.assertEqual("ok", artifacts[0]["status"])
        self.assertEqual("public-pre-push", artifacts[0]["command"])

    def test_a_private_name_in_a_file_refuses_the_push(self) -> None:
        (self.clone / "README.md").write_text(f"contact {OWNER_USERNAME}\n")
        _run(["git", "add", "-A"], self.clone)
        _run(["git", "commit", "-q", "-m", "add contact"], self.clone)

        completed = self._push()

        self.assertNotEqual(0, completed.returncode)
        artifacts = [
            json.loads(line)
            for line in (completed.stderr + completed.stdout).splitlines()
            if line.startswith('{"artifact_version"')
        ]
        self.assertEqual(1, len(artifacts))
        self.assertEqual("refused", artifacts[0]["status"])
        self.assertEqual("public_name_fence_failed", artifacts[0]["evidence"]["code"])

    def test_a_private_name_in_a_commit_message_refuses_the_push(self) -> None:
        (self.clone / "NOTES.md").write_text("plain notes\n")
        _run(["git", "add", "-A"], self.clone)
        _run(["git", "commit", "-q", "-m", f"authored by {OWNER_USERNAME}"], self.clone)

        completed = self._push()

        self.assertNotEqual(0, completed.returncode)
        artifacts = [
            json.loads(line)
            for line in (completed.stderr + completed.stdout).splitlines()
            if line.startswith('{"artifact_version"')
        ]
        self.assertEqual(1, len(artifacts))
        self.assertEqual("refused", artifacts[0]["status"])
        self.assertIn("commit_message", json.dumps(artifacts[0]["evidence"]))

    def test_a_path_substituted_git_cannot_silence_the_fence(self) -> None:
        """INV-3 rework HIGH: the gate resolves git by fixed candidates, not PATH."""

        fake_bin = self.temporary / "fake-bin"
        fake_bin.mkdir()
        (fake_bin / "git").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(fake_bin / "git", 0o755)
        (self.clone / "README.md").write_text(f"contact {OWNER_USERNAME}\n")
        _run(["git", "add", "-A"], self.clone)
        _run(["git", "commit", "-q", "-m", "add contact"], self.clone)

        completed = _run_with_path_prefix(self.clone, fake_bin)

        self.assertNotEqual(0, completed.returncode)
        artifacts = [
            json.loads(line)
            for line in (completed.stderr + completed.stdout).splitlines()
            if line.startswith('{"artifact_version"')
        ]
        self.assertEqual(1, len(artifacts))
        self.assertEqual("refused", artifacts[0]["status"])
        self.assertEqual("public_name_fence_failed", artifacts[0]["evidence"]["code"])
        self.assertTrue(artifacts[0]["evidence"]["findings"])

    def test_git_absent_from_the_fixed_candidates_leaves_through_the_artifact(self) -> None:
        """INV-3 rework MEDIUM: no git at the candidates is typed, not a traceback."""

        import importlib.util
        from io import StringIO
        from unittest import mock

        loader = importlib.machinery.SourceFileLoader(
            "public_pre_push_hook",
            str(REPOSITORY_ROOT / "scripts/public-hooks/pre-push"),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        hook = importlib.util.module_from_spec(spec)
        loader.exec_module(hook)

        with mock.patch.object(hook, "GIT_CANDIDATES", ("/nonexistent/git",)):
            with mock.patch.object(sys, "stdin", StringIO("")):
                exit_code = hook.main()

        self.assertEqual(1, exit_code)

    def test_a_fence_import_failure_leaves_through_the_artifact(self) -> None:
        """INV-3 rework MEDIUM: an unloadable fence is typed, not a traceback."""

        import importlib.util
        from io import StringIO
        from unittest import mock

        loader = importlib.machinery.SourceFileLoader(
            "public_pre_push_hook",
            str(REPOSITORY_ROOT / "scripts/public-hooks/pre-push"),
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        hook = importlib.util.module_from_spec(spec)
        loader.exec_module(hook)

        captured = io.StringIO()
        with mock.patch.object(
            hook, "_load_fence", side_effect=ImportError("No module named 'scripts'")
        ):
            with mock.patch.object(sys, "stdin", StringIO("")):
                with mock.patch.object(sys, "stdout", captured):
                    exit_code = hook.main()

        self.assertEqual(1, exit_code)
        artifact = json.loads(captured.getvalue())
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("fence_unavailable", artifact["evidence"]["code"])
        self.assertNotIn("Traceback", captured.getvalue())

    def test_deleting_a_remote_ref_is_not_a_publication(self) -> None:
        self.assertEqual(0, self._push().returncode)
        _run(["git", "push", "origin", "--delete", "main"], self.clone)

        completed = _run(["git", "push", "origin", "main"], self.clone)
        self.assertEqual(0, completed.returncode)


if __name__ == "__main__":
    unittest.main()

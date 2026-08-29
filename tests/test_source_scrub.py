from __future__ import annotations

import tempfile
import unittest
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from floati.scrub import scan_generated_tree, scan_git_history_notes


APPROVED_README_RECEIPT = (
    '{"id":"ack-01a0088d18e77c9e9fa3599f20038f9d",'
    '"item_ids":["msg-01a0088c08207e31b874d517621e164b"],'
    '"kind":"ack_receipt","recipient":"lane-slipway",'
    '"schema_version":0,"tenant_id":"puddle-fleet",'
    '"timestamp":"2026-08-16T03:10:59.815Z"}'
)

APPROVED_PUBLIC_PUDDLE_REFERENCES = {
    (
        "docs/NORTH_STAR.md",
        "| V8 | Puddle as optional visual interface | **SEAM READY** | CONFLUENCE-v0 schemas + `status --json` + `graph --json` on main; consumer side deliberately out of this repo |",
    ),
}


class SourceScrubTests(unittest.TestCase):
    def test_tracked_repository_contains_no_operator_account_name(self) -> None:
        forbidden = bytes.fromhex("63687269736d656e656e64657a").lower()
        tracked = subprocess.check_output(
            ["git", "ls-files", "-z"],
            cwd=Path.cwd(),
        ).split(b"\0")
        hits = []
        for raw_path in tracked:
            if not raw_path:
                continue
            path = Path(raw_path.decode("utf-8"))
            if forbidden in path.read_bytes().lower():
                hits.append(path.as_posix())

        self.assertEqual([], hits)

    def test_managed_session_fixture_uses_no_legacy_name(self) -> None:
        forbidden = bytes.fromhex("707564646c65").decode("ascii")
        fixtures = [
            Path(__file__).with_name("test_managed.py"),
            *sorted(Path("tests/fixtures/managed/v0").glob("*.json")),
        ]
        for fixture in fixtures:
            with self.subTest(fixture=fixture.as_posix()):
                self.assertNotIn(
                    forbidden,
                    fixture.read_text(encoding="utf-8").casefold(),
                )

    def test_public_text_contains_no_private_source_name(self) -> None:
        forbidden = bytes.fromhex("707564646c65").decode("ascii")
        public_files = (
            Path("README.md"),
            Path("docs/CONFLUENCE-v0.md"),
            Path("docs/TRUTH-GUARANTEES.md"),
            Path("docs/NORTH_STAR.md"),
            Path("floati/managed.py"),
        )
        hits = []
        for path in public_files:
            for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if path == Path("README.md") and line == APPROVED_README_RECEIPT:
                    continue
                if (path.as_posix(), line) in APPROVED_PUBLIC_PUDDLE_REFERENCES:
                    continue
                if forbidden in line.casefold():
                    hits.append((path.as_posix(), line_number, line))

        self.assertEqual([], hits)

    def test_fixture_proves_scrubber_detects_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifact.txt").write_bytes(bytes.fromhex("7369676e616c6372616674"))
            self.assertEqual(["artifact.txt"], scan_generated_tree(root))

    def test_generated_repository_artifacts_are_scrubbed(self) -> None:
        self.assertEqual([], scan_generated_tree(Path.cwd()))

    def test_generated_repository_scrub_uses_only_tracked_files(self) -> None:
        """Ignored worktrees are not publication inputs, but forced tracked files are."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"],
                cwd=root,
                check=True,
            )
            (root / ".gitignore").write_text(".worktrees/\n", encoding="utf-8")
            (root / "README.md").write_text("clean\n", encoding="utf-8")
            subprocess.run(["git", "add", ".gitignore", "README.md"], cwd=root, check=True)
            ignored = root / ".worktrees/hm3i/HM3I_BRIEF.md"
            ignored.parent.mkdir(parents=True)
            ignored.write_bytes(bytes.fromhex("5369676e616c4372616674"))

            self.assertEqual([], scan_generated_tree(root))
            hostile_git_environment = {
                "GIT_DIR": str(root / "missing-git-dir"),
                "GIT_WORK_TREE": str(root / "wrong-work-tree"),
                "GIT_INDEX_FILE": str(root / "missing-index"),
            }
            with patch.dict(os.environ, hostile_git_environment):
                self.assertEqual([], scan_generated_tree(root))

            subprocess.run(
                ["git", "add", "--force", ".worktrees/hm3i/HM3I_BRIEF.md"],
                cwd=root,
                check=True,
            )
            self.assertEqual(
                [".worktrees/hm3i/HM3I_BRIEF.md"],
                scan_generated_tree(root),
            )

    def test_history_note_scanner_detects_forbidden_commit_message_without_source_literal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(
                ["git", "init", "--quiet", "--initial-branch=main"],
                cwd=root,
                check=True,
            )
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Scrub Fixture",
                "GIT_AUTHOR_EMAIL": "scrub@example.invalid",
                "GIT_COMMITTER_NAME": "Scrub Fixture",
                "GIT_COMMITTER_EMAIL": "scrub@example.invalid",
            }
            forbidden = bytes.fromhex("5369676e616c4372616674").decode("ascii")
            subprocess.run(
                ["git", "commit", "--quiet", "-m", f"remove {forbidden} source"],
                cwd=root,
                env=environment,
                check=True,
            )
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            self.assertEqual(
                [f"{sha}:commit-message"], scan_git_history_notes(root)
            )

    def test_repository_history_notes_are_scrubbed(self) -> None:
        self.assertEqual([], scan_git_history_notes(Path.cwd()))

    def test_history_note_scanner_checks_git_notes_refs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                **os.environ,
                "GIT_AUTHOR_NAME": "Scrub Fixture",
                "GIT_AUTHOR_EMAIL": "scrub@example.invalid",
                "GIT_COMMITTER_NAME": "Scrub Fixture",
                "GIT_COMMITTER_EMAIL": "scrub@example.invalid",
            }
            subprocess.run(["git", "init", "--quiet", "--initial-branch=main"], cwd=root, check=True)
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "clean subject"], cwd=root, env=environment, check=True)
            sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
            forbidden = bytes.fromhex("5369676e616c4372616674").decode("ascii")
            subprocess.run(
                ["git", "notes", "--ref=publication", "add", "-m", f"archive {forbidden}"],
                cwd=root,
                env=environment,
                check=True,
            )

            self.assertEqual(
                [f"refs/notes/publication:{sha}:note"], scan_git_history_notes(root)
            )


if __name__ == "__main__":
    unittest.main()

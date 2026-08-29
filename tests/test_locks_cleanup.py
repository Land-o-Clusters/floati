from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.locks.cleanup import CleanupInspector


class LocksCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.repository = self.base / "repository"
        self.worktree = self.base / "detached-worktree"
        self.git(self.base, "init", "--quiet", "--initial-branch=main", str(self.repository))
        self.git(self.repository, "config", "user.name", "Locks Fixture")
        self.git(self.repository, "config", "user.email", "locks@example.invalid")
        (self.repository / "base.txt").write_text("base\n", encoding="utf-8")
        self.git(self.repository, "add", "base.txt")
        self.git(self.repository, "commit", "--quiet", "-m", "base")
        self.git(
            self.repository,
            "worktree",
            "add",
            "--quiet",
            "--detach",
            str(self.worktree),
            "refs/heads/main",
        )

    @staticmethod
    def git(cwd: Path, *arguments: str) -> str:
        completed = subprocess.run(
            ["/usr/bin/git", *arguments],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        return completed.stdout.strip()

    def test_unique_worktree_commit_is_named_and_refused_until_a_ref_exists(self) -> None:
        """Catches cleanup trusting age/prunable state instead of current ref reachability."""

        (self.worktree / "unique.txt").write_text("only copy\n", encoding="utf-8")
        self.git(self.worktree, "add", "unique.txt")
        self.git(self.worktree, "commit", "--quiet", "-m", "unique detached work")
        unique_sha = self.git(self.worktree, "rev-parse", "HEAD")

        inspector = CleanupInspector(self.repository)
        assessment = inspector.assess(self.worktree)
        self.assertFalse(assessment.eligible)
        self.assertEqual((unique_sha,), assessment.unreferenced_commits)
        with self.assertRaises(ProtocolRefusal) as caught:
            inspector.require_eligible(self.worktree)
        self.assertEqual("cleanup_unreferenced_commits", caught.exception.code)
        self.assertIn(unique_sha, caught.exception.detail)

        self.git(self.repository, "branch", "rescue", unique_sha)
        rescued = inspector.assess(self.worktree)
        self.assertTrue(rescued.eligible)
        self.assertEqual((), rescued.unreferenced_commits)

    def test_noncanonical_worktree_path_refuses_before_git_observation(self) -> None:
        """Catches cleanup silently normalizing a caller-selected alias path."""

        aliased = Path(
            str(self.worktree.parent)
            + "/../"
            + self.worktree.parent.name
            + "/"
            + self.worktree.name
        )
        self.assertNotEqual(aliased, aliased.resolve())
        with self.assertRaises(ProtocolRefusal) as caught:
            CleanupInspector(self.repository).assess(aliased)
        self.assertEqual("worktree_invalid", caught.exception.code)

    def test_assessment_is_read_only_and_foreign_worktrees_refuse(self) -> None:
        """Catches observation mutating the candidate or crossing common Git directories."""

        before_head = self.git(self.worktree, "rev-parse", "HEAD")
        before_status = self.git(self.worktree, "status", "--porcelain=v1")
        assessment = CleanupInspector(self.repository).assess(self.worktree)
        self.assertTrue(assessment.eligible)
        self.assertEqual(before_head, self.git(self.worktree, "rev-parse", "HEAD"))
        self.assertEqual(before_status, self.git(self.worktree, "status", "--porcelain=v1"))

        foreign = self.base / "foreign"
        self.git(self.base, "init", "--quiet", "--initial-branch=main", str(foreign))
        with self.assertRaises(ProtocolRefusal) as caught:
            CleanupInspector(self.repository).assess(foreign)
        self.assertEqual("worktree_repository_mismatch", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

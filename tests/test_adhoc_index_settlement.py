"""Only exact remote banking permits a receipted row-index settlement."""
import subprocess
from pathlib import Path
import unittest
from unittest import mock

from tests.test_adhoc_threads import ControllerFixture


class AdhocIndexSettlementTests(ControllerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        c = self.case
        self.remote = c.base / "bank.git"
        c.git(c.base, "init", "--bare", "--quiet", str(self.remote))
        c.git(c.repository, "remote", "set-url", "origin", str(self.remote))
        self.workspace = Path(c.profile_record["workspace"])
        self.index = Path(c.profile_record["git_admin_dir"]) / "index"
        self.other_index = Path(c.git(Path(c.foreign_opened["workspace"]), "rev-parse", "--git-path", "index"))

    def pending(self):
        code, result = self.capture_run()
        self.assertEqual("adhoc_result_unbanked", code)
        self.assertIsNone(result)
        return self.case.git(self.workspace, "rev-parse", "HEAD")

    def bank_exact(self):
        c = self.case
        self.bank_result()
        c.git(c.repository, "push", "--quiet", "origin",
            "HEAD:refs/heads/main")
        sha = c.git(self.workspace, "rev-parse", "HEAD")
        c.git(c.repository, "push", "--quiet", "origin",
            sha + ":refs/heads/" + c.profile_record["branch"])

    def files(self):
        return {str(p.relative_to(self.workspace)): p.read_bytes()
            for p in self.workspace.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_unrelated_worktree_edit_refuses_without_rewriting_index_or_files(self):
        self.pending()
        self.bank_exact()
        (self.workspace / "tracked.txt").write_text("unpublished outsider edit\n")
        before, files = self.index.read_bytes(), self.files()
        code, result = self.capture_run()
        self.assertIn(code, {"adhoc_binding_changed", "adhoc_result_unbanked"})
        self.assertIsNone(result)
        self.assertEqual(before, self.index.read_bytes())
        self.assertEqual(files, self.files())

    def test_branch_cannot_change_between_settlement_check_and_index_replacement(self):
        import floati.adhoc_workspace as workspace_module
        sha = self.pending()
        self.bank_exact()
        before = self.index.read_bytes()
        attempted = []
        replace = workspace_module.os.replace

        def competing_branch_update(source, destination, *args, **kwargs):
            if source == "index.lock" and destination == "index":
                changed = subprocess.run(["/usr/bin/git", "-C", str(self.workspace), "-c",
                    "core.filesRefLockTimeout=0", "update-ref",
                    "refs/heads/" + self.case.profile_record["branch"], self.case.base_sha, sha],
                    capture_output=True, timeout=5)
                attempted.append(changed.returncode)
            return replace(source, destination, *args, **kwargs)

        with mock.patch.object(workspace_module.os, "replace", side_effect=competing_branch_update):
            code, result = self.capture_run()
        self.assertTrue(attempted, "the control must race the actual native index replacement")
        if attempted[0] == 0:
            self.assertEqual(before, self.index.read_bytes(),
                "a changed HEAD must never permit rewriting the row index")
            self.assertIn(code, {"adhoc_binding_changed", "adhoc_result_unbanked"})
        else:
            self.assertIsNone(code, "a blocked branch contender must leave lawful settlement usable")
            self.assertEqual("result_published", result["phase"])
            self.assertEqual(sha, self.case.git(self.workspace, "rev-parse", "HEAD"))

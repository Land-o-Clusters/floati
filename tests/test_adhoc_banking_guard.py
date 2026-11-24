"""Real prepared Git transactions must retain current dispatch authority."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest import mock

from floati.adhoc_workspace import BorrowedWorkspace
from floati.errors import ProtocolRefusal
from floati.planes import AuthorityGrantStore
from tests.fixtures.adhoc_stage1 import AdhocFixture


class AdhocBankingGuardTests(AdhocFixture, unittest.TestCase):
    def borrower(self):
        profile = self.profile()
        binding = {field: profile[field] for field in
                   ("lane_opening_id", "node_lease_id", "workspace")}
        binding["profile_id"] = profile["id"]
        borrower = BorrowedWorkspace(self.root, binding)
        self.addCleanup(borrower.close)
        return borrower, profile

    def snapshot(self, profile):
        return {"head": self.git(self.workspace, "rev-parse", "HEAD"),
                "index": (Path(profile["git_admin_dir"]) / "index").read_bytes(),
                "refs": self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)")}

    def assert_invalidated_authority_cannot_bank(self, invalidation):
        borrower, profile = self.borrower()
        pin = borrower.pin()
        before = self.snapshot(profile)
        (self.workspace / "result.json").write_text("guarded result\n")
        reference = "refs/heads/" + profile["branch"]
        lock = Path(profile["git_common_dir"]) / (reference + ".lock")
        real_git = borrower._git
        prepared = []
        observed = [self.now]

        def after_prepare(*arguments, **options):
            if arguments == ("symbolic-ref", "--quiet", reference):
                # This probe runs only after real Git acknowledges prepare: ok.
                self.assertTrue(lock.is_file(), "authority must change while the native ref lock is held")
                prepared.append(True)
                if invalidation == "revoke":
                    AuthorityGrantStore(self.root).revoke_exact("adhoc-work", "temporary", 1,
                        self.now + timedelta(milliseconds=1))
                else:
                    grant = AuthorityGrantStore(self.root).exact_tail("adhoc-work")
                    observed[0] = datetime.fromisoformat(grant["expires_at"].replace("Z", "+00:00"))
            return real_git(*arguments, **options)

        def guard():
            self.work._require_authority("adhoc-work", "temporary", 1, observed[0])

        code = None
        with mock.patch.object(borrower, "_git", side_effect=after_prepare):
            try:
                borrower.finalize(["result.json"], pin["expected_head"], before_commit=guard)
            except ProtocolRefusal as exc:
                code = exc.code
        self.assertTrue(prepared, "the real native ref transaction must reach preparation")
        self.assertEqual("authority_inactive", code,
                         "banking must refuse authority that ended after ref preparation")
        self.assertEqual(before, self.snapshot(profile),
                         "refusal must preserve the row head, original index, and every foreign ref")
        self.assertFalse(lock.exists(), "refusal must release the owned prepared ref lock")

    def test_current_authority_guard_runs_under_prepared_ref_lock(self):
        for case in ("expire", "current", "revoke"):
            with self.subTest(case=case):
                if case == "current":
                    self.assert_current_authority_can_bank()
                else:
                    self.assert_invalidated_authority_cannot_bank(case)
            if case == "expire":
                # A refused turn leaves its selected file untracked.
                (self.workspace / "result.json").unlink(missing_ok=True)
            elif case == "current":
                # Successful banking preserved the original row index above.
                # Synchronize only the fixture before pinning a fresh borrower.
                self.git(self.workspace, "read-tree", "HEAD")

    def assert_current_authority_can_bank(self):
        borrower, profile = self.borrower()
        pin = borrower.pin()
        before = self.snapshot(profile)
        (self.workspace / "result.json").write_text("guarded result\n")
        reference = "refs/heads/" + profile["branch"]
        lock = Path(profile["git_common_dir"]) / (reference + ".lock")
        observed = []

        def guard():
            self.assertTrue(lock.is_file(), "the guard must run before committing the held native ref lock")
            self.assertEqual(before, self.snapshot(profile))
            self.work._require_authority("adhoc-work", "temporary", 1, self.now)
            observed.append(True)

        result = borrower.finalize(["result.json"], pin["expected_head"], before_commit=guard)
        self.assertTrue(observed, "successful banking must consult current authority at its commit boundary")
        self.assertEqual(result[0]["sha"], self.git(self.workspace, "rev-parse", "HEAD"))
        self.assertEqual("guarded result", self.git(self.workspace, "show", "HEAD:result.json"))
        self.assertEqual(before["index"], self.snapshot(profile)["index"])
        self.assertFalse(lock.exists())

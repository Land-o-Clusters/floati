"""Recover exact parent banking cuts without another provider invocation."""
import json
from pathlib import Path
import unittest
from unittest import mock

from floati.adhoc_workspace import BorrowedWorkspace
from tests.test_adhoc_threads import ControllerFixture


class AdhocRecoveryTests(ControllerFixture, unittest.TestCase):
    def native_opened_ids(self):
        return [row["id"] for row in self.controller.journal.records()
                if row["phase"] == "provider_opened"]

    def test_cleanup_cut_recovers_exact_result_without_provider_relaunch(self):
        with mock.patch.object(self.controller, "_save_result", side_effect=RuntimeError("after cleanup")):
            with self.assertRaisesRegex(RuntimeError, "after cleanup"):
                self.run_dispatch()
        self.assertEqual(1, self.execution.launches)
        self.assertEqual(1, len(self.saved("process_closed", stage="cleanup")))
        opened = self.native_opened_ids()
        self.assertTrue(opened)
        code, _result = self.capture_run()
        self.assertEqual("adhoc_result_unbanked", code,
            "proved cleanup must recover dirty provider result files through the saved borrowed pin")
        self.bank_result()
        self.assertIsNone(self.capture_run()[0])
        self.assertEqual(opened, self.native_opened_ids())
        self.assertEqual(1, self.execution.launches)
        self.assertEqual(1, len(self.saved("result_published")))

    def after_cas_cut(self):
        original = BorrowedWorkspace.finalize
        committed = []
        def cut(workspace, paths, expected, **kwargs):
            result = original(workspace, paths, expected, **kwargs)
            committed.append(result[0]["sha"])
            raise RuntimeError("after parent CAS")
        with mock.patch.object(BorrowedWorkspace, "finalize", cut):
            with self.assertRaisesRegex(RuntimeError, "after parent CAS"):
                self.run_dispatch()
        self.assertEqual(1, len(committed))
        self.assertEqual([], self.saved("process_closed", stage="result"))
        self.assertEqual(1, self.execution.launches)
        return committed[0]

    def test_cas_cut_adopts_only_the_same_immutable_result_commit(self):
        sha = self.after_cas_cut()
        opened = self.native_opened_ids()
        code, _result = self.capture_run()
        self.assertEqual("adhoc_result_unbanked", code,
            "an exact already-committed banking intent must recover without a second commit")
        self.assertEqual(sha, self.case.git(Path(self.case.profile_record["workspace"]), "rev-parse", "HEAD"))
        saved = self.saved("process_closed", stage="result")
        self.assertEqual(sha, saved[0]["evidence"]["result"]["sha"])
        self.bank_result()
        self.assertIsNone(self.capture_run()[0])
        self.assertEqual(opened, self.native_opened_ids())
        self.assertEqual(1, self.execution.launches)

    def test_changed_live_result_after_cas_cannot_replace_frozen_result(self):
        sha = self.after_cas_cut()
        path = Path(self.case.profile_record["workspace"]) / self.case.dispatch_record["result_document"]
        changed = json.loads(path.read_text())
        changed["summary"] = "replacement supplied after the native process ended"
        path.write_text(json.dumps(changed) + "\n")
        code, _result = self.capture_run()
        self.assertEqual("adhoc_result_disposition_invalid", code)
        self.assertEqual(sha, self.case.git(Path(self.case.profile_record["workspace"]), "rev-parse", "HEAD"))
        self.assertEqual(changed, json.loads(path.read_text()))
        self.assertEqual(1, self.execution.launches)
        self.assertEqual(set(), set(self.acked()))
        self.assertEqual([], self.saved("result_published"))

"""Native borrowed readers and parent finalization, using scratch lane owners."""
from __future__ import annotations

from copy import deepcopy
import unittest
import shutil
from pathlib import Path
import os
import multiprocessing
import signal
import stat
import subprocess
import sys
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.records import validate_record
from floati.work import WORK_KINDS
from tests.fixtures.adhoc_stage1 import AdhocFixture


class AdhocWorkspaceRecordTests(AdhocFixture, unittest.TestCase):
    def borrowed_record(self):
        # These coordinates come only from the native profile's validated row.
        profile = self.profile()
        self.assertEqual(profile, self.profiles().get(profile["id"]))
        with self.lanes.validated_binding("temporary", profile["lane_opening_id"],
                                          "fixture", require_clean=True) as binding:
            self.assertEqual(str(binding["workspace"]), profile["workspace"])
            self.assertEqual(str(binding["git_admin_dir"]), profile["git_admin_dir"])
            self.assertEqual(self.lease["id"], profile["node_lease_id"])
            record = deepcopy(self.item)
            record.pop("car_binding")
            record.update(schema_version=2, workspace=profile["workspace"],
                adhoc_binding={"profile_id": profile["id"],
                    "lane_opening_id": profile["lane_opening_id"],
                    "node_lease_id": profile["node_lease_id"],
                    "workspace": profile["workspace"]})
        return record

    def test_native_work_ledger_retains_version_two_borrowed_binding(self):
        """Native validation, ledger and detached projection retain the binding."""
        record = self.borrowed_record()
        record["id"] = "work-" + uuid7_hex()
        saved = deepcopy(record)
        self.assertEqual(record, validate_record(record, self.root.tenant_id,
                         frozenset({"work_item"}), integrity=False))
        self.assertEqual(saved, record)
        before = self.work.consumption.records()
        self.assertEqual([self.item], before)
        append_record(self.root, self.work.relative_path, record, allowed_kinds=WORK_KINDS)
        self.assertEqual(before + [record], self.work.consumption.records())
        self.assertEqual(record["adhoc_binding"],
                         self.work.show(record["id"])[0].get("adhoc_binding"))
        projected = self.work.consumption.project([record])[record["id"]]
        self.assertEqual(record["adhoc_binding"], projected.get("adhoc_binding"))
        self.assertEqual(record["workspace"], projected["workspace"])
        projected["adhoc_binding"]["workspace"] = "changed only in detached projection"
        self.assertEqual(saved, record)
        unknown = deepcopy(record)
        unknown["adhoc_binding"]["extra_git_authority"] = record["workspace"]
        disagreeing = deepcopy(record)
        disagreeing["workspace"] = self.foreign_opened["workspace"]
        for case, candidate in (("unknown-field", unknown), ("workspace-disagreement", disagreeing)):
            with self.subTest(case=case):
                self.refuses("adhoc_binding_changed", lambda: validate_record(candidate,
                    self.root.tenant_id, frozenset({"work_item"}), integrity=False))

    def test_legacy_version_zero_and_one_records_keep_native_roundtrip(self):
        plain = self.work.add("legacy fresh-workspace binding", "temporary", [],
                              provision_workspace=True, now=self.now)
        self.assertEqual(0, plain["schema_version"])
        self.assertEqual(1, self.item["schema_version"])
        for record in (plain, self.item):
            self.assertEqual(record, validate_record(record, self.root.tenant_id,
                             frozenset({"work_item"}), integrity=False))
            projected = self.work.show(record["id"])[0]
            self.assertEqual(record.get("workspace"), projected["workspace"])
            self.assertNotIn("adhoc_binding", projected)
        self.assertEqual(self.item["car_binding"], self.work.show(self.item["id"])[0]["car_binding"])

    def test_legacy_record_cannot_adopt_existing_borrowed_row(self):
        for source in (self.item, self.work.add("plain legacy item", "temporary", [], now=self.now)):
            candidate = dict(source, workspace=str(self.workspace))
            self.refuses("workspace_invalid", lambda: validate_record(candidate,
                self.root.tenant_id, frozenset({"work_item"}), integrity=False))


class BorrowedWorkspaceTests(AdhocFixture, unittest.TestCase):
    def borrower(self):
        from floati.adhoc_workspace import BorrowedWorkspace
        profile = self.profile()
        binding = {key: profile[key] for key in
                   ("lane_opening_id", "node_lease_id", "workspace")}
        binding["profile_id"] = profile["id"]
        borrower = BorrowedWorkspace(self.root, binding)
        self.addCleanup(borrower.close)
        return borrower, profile

    def saved_git(self, profile):
        admin, common = Path(profile["git_admin_dir"]), Path(profile["git_common_dir"])
        return {"index": (admin / "index").read_bytes(),
                "config": (common / "config").read_bytes(),
                "refs": self.git(self.workspace, "for-each-ref", "--format=%(refname) %(objectname)"),
                "history": self.git(self.workspace, "rev-list", "HEAD"),
                "head": self.git(self.workspace, "rev-parse", "HEAD"),
                "marker": (admin / "floati-lane-owner.json").read_bytes()}

    def test_native_git_launcher_rejects_common_descriptor_identity_mismatch(self):
        borrower, profile = self.borrower()
        borrower.pin()
        before = self.saved_git(profile)
        real_run = subprocess.run
        observed = []

        def run(command, *args, **kwargs):
            if command[0] == os.path.realpath(sys.executable):
                command = list(command)
                command[8] = str(int(command[8]) + 1)
                result = real_run(command, *args, **kwargs)
                observed.append(result.returncode)
                return result
            return real_run(command, *args, **kwargs)

        with mock.patch("floati.adhoc_workspace.subprocess.run", side_effect=run):
            self.refuses("adhoc_binding_changed", lambda: borrower._git(
                "rev-parse", "refs/heads/" + profile["branch"], timeout=5))
        self.assertEqual([126], observed, "fresh child did not reject the actual mismatched inode")
        self.assertEqual(before, self.saved_git(profile))

    def test_borrowed_row_keeps_history_index_and_shared_refs(self):
        # Add actual ancestral history before pinning; opening base stays fixed.
        (self.workspace / "second.txt").write_text("second committed file\n")
        self.git(self.workspace, "add", "second.txt")
        self.git(self.workspace, "commit", "--quiet", "-m", "second base commit")
        borrower, profile = self.borrower()
        before = self.saved_git(profile)
        self.assertEqual(2, len(before["history"].splitlines()))
        parent_directory = os.stat(".")
        common_directory = Path(profile["git_common_dir"]).stat()
        real_popen = subprocess.Popen
        observed = []

        def launch(command, *args, **kwargs):
            if command[0] == os.path.realpath(sys.executable):
                descriptors = kwargs["pass_fds"]
                self.assertEqual(1, len(descriptors))
                identity = os.fstat(descriptors[0])
                self.assertEqual((common_directory.st_dev, common_directory.st_ino),
                                 (identity.st_dev, identity.st_ino))
                self.assertIs(True, kwargs["close_fds"])
                self.assertNotIn("preexec_fn", kwargs)
                self.assertNotIn("cwd", kwargs)
                for ambient in ("PYTHONPATH", "GIT_DIR", "GIT_WORK_TREE", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES"):
                    self.assertNotIn(ambient, kwargs["env"])
                actual_directory = os.stat(".")
                self.assertEqual((parent_directory.st_dev, parent_directory.st_ino),
                                 (actual_directory.st_dev, actual_directory.st_ino))
                observed.append(tuple(command))
            return real_popen(command, *args, **kwargs)

        with mock.patch.dict(os.environ, {
                "PYTHONPATH": "/untrusted-python-path", "GIT_DIR": "/untrusted-git-dir",
                "GIT_WORK_TREE": "/untrusted-worktree", "LD_PRELOAD": "/untrusted-loader",
                "DYLD_INSERT_LIBRARIES": "/untrusted-loader"}),\
             mock.patch("floati.adhoc_workspace.subprocess.Popen", side_effect=launch),\
             mock.patch("floati.adhoc_workspace.os.fchdir", side_effect=AssertionError("parent cwd changed")):
            pin = borrower.pin()
            self.assertEqual(before["head"], pin["expected_head"])
            (self.workspace / "result.json").write_text('{"fixture":true}\n')
            (self.workspace / "unselected.txt").write_text("leave me unbanked\n")
            bindings = borrower.finalize(["result.json"], expected_head=pin["expected_head"])
        self.assertTrue(any("rev-parse" in command for command in observed), "native read launch was not observed")
        self.assertEqual(1, sum("update-ref" in command for command in observed), "native transaction launch was not observed")
        actual_directory = os.stat(".")
        self.assertEqual((parent_directory.st_dev, parent_directory.st_ino),
                         (actual_directory.st_dev, actual_directory.st_ino))
        self.assertEqual(1, len(bindings))
        self.assertEqual("fixture", bindings[0]["repo"])
        self.assertEqual("result.json", bindings[0]["doc"])
        sha = bindings[0]["sha"]
        self.assertEqual(sha, self.git(self.workspace, "rev-parse", "HEAD"))
        self.assertEqual('{"fixture":true}', self.git(self.workspace, "show", "HEAD:result.json"))
        self.assertEqual(before["head"], self.git(self.workspace, "rev-parse", "HEAD^"))
        self.assertEqual("result.json", self.git(self.workspace, "diff-tree", "--no-commit-id", "--name-only", "-r", sha))
        self.assertEqual("retained base\n", (self.workspace / "tracked.txt").read_text())
        self.assertEqual("second committed file\n", (self.workspace / "second.txt").read_text())
        self.assertEqual("leave me unbanked\n", (self.workspace / "unselected.txt").read_text())
        after = self.saved_git(profile)
        for field in ("index", "config", "marker"):
            self.assertEqual(before[field], after[field], field)
        self.assertEqual(before["refs"].replace(before["head"], sha, 1), after["refs"])
        self.assertEqual(before["history"].splitlines(), after["history"].splitlines()[1:])
        self.assertEqual(before["head"], self.git(self.workspace, "rev-parse", "HEAD^"))
        self.assertEqual(self.base_sha, self.git(self.repository, "rev-parse", "main"))

    def test_pin_refuses_initial_dirty_tree_and_index(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                borrower, _profile = self.borrower()
                path = self.workspace / "tracked.txt"
                path.write_text("dirty before pin\n")
                if staged:
                    self.git(self.workspace, "add", "tracked.txt")
                before = self.tree_bytes(self.workspace)
                index = Path(self.git(self.workspace, "rev-parse", "--absolute-git-dir")) / "index"
                index_before = index.read_bytes()
                self.refuses("adhoc_binding_changed", borrower.pin)
                self.assertEqual(before, self.tree_bytes(self.workspace))
                self.assertEqual(index_before, index.read_bytes())
                # Fixture-only restore between the two independent variants.
                self.git(self.workspace, "restore", "--staged", "--worktree", "tracked.txt")

    def test_admin_swap_refuses_parent_finalization(self):
        borrower, profile = self.borrower()
        pin = borrower.pin()
        admin = Path(profile["git_admin_dir"])
        old_identity = admin.stat()
        retained = self.base / "retained-admin"
        admin.rename(retained)
        shutil.copytree(retained, admin)
        self.assertNotEqual((old_identity.st_dev, old_identity.st_ino),
                            (admin.stat().st_dev, admin.stat().st_ino))
        self.assertEqual((retained / "floati-lane-owner.json").read_bytes(),
                         (admin / "floati-lane-owner.json").read_bytes())
        (self.workspace / "result.json").write_text("fixture result\n")
        before = self.tree_bytes(admin)
        refs = self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)")
        self.refuses("adhoc_binding_changed", lambda: borrower.finalize(
            ["result.json"], expected_head=pin["expected_head"]))
        self.assertEqual(before, self.tree_bytes(admin))
        self.assertEqual(refs, self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)"))
        self.assertEqual("fixture result\n", (self.workspace / "result.json").read_text())

    def test_marker_and_branch_replacement_refuse_without_overwrite(self):
        borrower, profile = self.borrower()
        pin = borrower.pin()
        marker = Path(profile["git_admin_dir"]) / "floati-lane-owner.json"
        original = marker.read_bytes()
        marker.write_bytes(b"foreign marker\n")
        self.refuses("adhoc_binding_changed", lambda: borrower.finalize(
            ["tracked.txt"], expected_head=pin["expected_head"]))
        self.assertEqual(b"foreign marker\n", marker.read_bytes())
        marker.write_bytes(original)
        self.git(self.workspace, "switch", "--quiet", "-c", "codex/foreign-replacement")
        self.refuses("adhoc_binding_changed", lambda: borrower.finalize(
            ["tracked.txt"], expected_head=pin["expected_head"]))
        self.assertEqual("codex/foreign-replacement", self.git(self.workspace, "branch", "--show-current"))

    def test_changed_branch_head_refuses_expected_old_sha_without_overwrite(self):
        borrower, profile = self.borrower()
        pin = borrower.pin()
        (self.workspace / "racing.txt").write_text("another parent owns this commit\n")
        self.git(self.workspace, "add", "racing.txt")
        self.git(self.workspace, "commit", "--quiet", "-m", "branch race")
        replacement = self.git(self.workspace, "rev-parse", "HEAD")
        self.assertNotEqual(pin["expected_head"], replacement)
        before = self.saved_git(profile)
        self.refuses("adhoc_binding_changed", lambda: borrower.finalize(
            ["tracked.txt"], expected_head=pin["expected_head"]))
        self.assertEqual(before, self.saved_git(profile))

    def test_symlink_result_or_parent_refuses_and_preserves_foreign_target(self):
        borrower, profile = self.borrower()
        pin = borrower.pin()
        foreign = self.base / "foreign-result"
        foreign.mkdir()
        (foreign / "result.json").write_text("foreign bytes\n")
        (self.workspace / "alias").symlink_to(foreign, target_is_directory=True)
        (self.workspace / "result-link").symlink_to(foreign / "result.json")
        before = self.saved_git(profile)
        for path in ("alias/result.json", "result-link", "../foreign-result/result.json", ".git"):
            with self.subTest(path=path):
                self.refuses("adhoc_result_disposition_invalid", lambda: borrower.finalize(
                    [path], expected_head=pin["expected_head"]))
                self.assertEqual(before, self.saved_git(profile))
                self.assertEqual("foreign bytes\n", (foreign / "result.json").read_text())

    def test_fifo_result_refuses_in_bounded_owned_child_without_mutation(self):
        borrower, profile = self.borrower()
        pin = borrower.pin()
        fifo = self.workspace / "selected-fifo"
        os.mkfifo(fifo, 0o600)
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))
        before = self.saved_git(profile)
        common_before = self.tree_bytes(Path(profile["git_common_dir"]))
        foreign = Path(self.foreign_opened["workspace"]) / "tracked.txt"
        foreign_before = foreign.read_bytes()
        context = multiprocessing.get_context("fork")
        reader, writer = context.Pipe(duplex=False)

        def inspect_fifo():
            reader.close()
            signal.signal(signal.SIGALRM, lambda _signal, _frame: os._exit(124))
            signal.alarm(10)
            info = fifo.lstat()
            writer.send({"phase": "entered", "fifo": stat.S_ISFIFO(info.st_mode),
                         "identity": [info.st_dev, info.st_ino]})
            try:
                borrower.finalize(["selected-fifo"], expected_head=pin["expected_head"])
            except ProtocolRefusal as exc:
                writer.send({"phase": "refused", "code": exc.code})
            except BaseException as exc:
                writer.send({"phase": "error", "type": type(exc).__name__})
            else:
                writer.send({"phase": "unexpected-success"})
            finally:
                writer.close()
                signal.alarm(0)

        process = context.Process(target=inspect_fifo, name="floati-fifo-result-control")
        process.start()
        writer.close()
        try:
            process.join(12)
            self.assertFalse(process.is_alive(), "FIFO finalization exceeded owned child bound")
            self.assertEqual(0, process.exitcode, "FIFO finalization hit the child deadline")
            self.assertTrue(reader.poll(0))
            entered = reader.recv()
            self.assertEqual("entered", entered["phase"])
            self.assertTrue(entered["fifo"])
            self.assertEqual([fifo.stat().st_dev, fifo.stat().st_ino], entered["identity"])
            self.assertTrue(reader.poll(0))
            self.assertEqual({"phase": "refused", "code": "adhoc_result_disposition_invalid"}, reader.recv())
        finally:
            if process.is_alive():
                process.terminate()
                process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            reader.close()
            process.close()
        self.assertEqual(before, self.saved_git(profile))
        self.assertEqual(common_before, self.tree_bytes(Path(profile["git_common_dir"])))
        self.assertEqual(foreign_before, foreign.read_bytes())
        self.assertTrue(stat.S_ISFIFO(fifo.lstat().st_mode))

    def _final_ref_race(self, *, symbolic):
        borrower, profile = self.borrower()
        pin = borrower.pin()
        owned = "refs/heads/" + profile["branch"]
        sibling = "refs/heads/" + self.foreign_opened["record"]["branch"]
        self.assertEqual(pin["expected_head"], self.git(self.repository, "rev-parse", sibling))
        (self.workspace / "result.json").write_text("selected result\n")
        common = Path(profile["git_common_dir"])
        owned_path = common / owned
        sibling_path = common / sibling
        index = Path(profile["git_admin_dir"]) / "index"
        index_before = index.read_bytes()
        sibling_before = sibling_path.read_bytes()
        config_before = (common / "config").read_bytes()
        foreign = Path(self.foreign_opened["workspace"]) / "tracked.txt"
        foreign_before = foreign.read_bytes()
        # Produce a real competing commit without touching a branch or row index.
        replacement = self.git(self.repository, "commit-tree",
            self.git(self.repository, "rev-parse", pin["expected_head"] + "^{tree}"),
            "-p", pin["expected_head"], "-m", "competing ref-only fixture commit")
        self.assertNotEqual(pin["expected_head"], replacement)
        real_popen = subprocess.Popen
        real_git = borrower._git
        observed = {"injected": 0, "prepared_lock": False, "replacement_bytes": None, "refs": None}

        def launch(command, *args, **kwargs):
            if "update-ref" in command and not observed["injected"]:
                observed["injected"] += 1
                if symbolic:
                    self.git(self.repository, "symbolic-ref", owned, sibling)
                else:
                    self.git(self.repository, "update-ref", owned, replacement, pin["expected_head"])
                observed["replacement_bytes"] = owned_path.read_bytes()
                observed["refs"] = self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)")
                self.assertEqual(index_before, index.read_bytes(), "injection changed the original index")
            return real_popen(command, *args, **kwargs)

        def git_observer(*arguments, **kwargs):
            if arguments[:2] == ("symbolic-ref", "--quiet"):
                observed["prepared_lock"] = (common / (owned + ".lock")).is_file()
            return real_git(*arguments, **kwargs)

        with mock.patch("floati.adhoc_workspace.subprocess.Popen", side_effect=launch),\
             mock.patch.object(borrower, "_git", side_effect=git_observer):
            self.refuses("adhoc_binding_changed", lambda: borrower.finalize(
                ["result.json"], expected_head=pin["expected_head"]))
        self.assertEqual(1, observed["injected"], "actual final native CAS boundary was not reached")
        if symbolic:
            self.assertTrue(observed["prepared_lock"], "identity was not inspected under Git's prepared lock")
            self.assertEqual(sibling, self.git(self.repository, "symbolic-ref", owned))
        else:
            self.assertEqual(replacement, self.git(self.repository, "rev-parse", owned))
        self.assertEqual(observed["replacement_bytes"], owned_path.read_bytes())
        self.assertEqual(observed["refs"], self.git(self.repository, "for-each-ref", "--format=%(refname) %(objectname)"))
        self.assertEqual(sibling_before, sibling_path.read_bytes())
        self.assertEqual(pin["expected_head"], self.git(self.repository, "rev-parse", sibling))
        self.assertEqual(index_before, index.read_bytes())
        self.assertEqual(config_before, (common / "config").read_bytes())
        self.assertEqual(foreign_before, foreign.read_bytes())
        self.assertFalse((common / (owned + ".lock")).exists())
        self.assertEqual("selected result\n", (self.workspace / "result.json").read_text())

    def test_final_cas_refuses_same_oid_symbolic_replacement_and_preserves_sibling(self):
        self._final_ref_race(symbolic=True)

    def test_final_cas_refuses_ref_only_racing_sha_without_index_change(self):
        self._final_ref_race(symbolic=False)


if __name__ == "__main__":
    unittest.main()

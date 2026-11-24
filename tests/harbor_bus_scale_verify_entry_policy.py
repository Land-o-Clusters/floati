"""Am10 native verifier launches: exact inputs, no child-byte coverage claim."""
from __future__ import annotations

import copy
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import unittest
from unittest.mock import patch

from floati import cli
from tests.harbor_bus_scale_routine_lifecycle import ENTRY, fixture, load


READS = ENTRY.parent / "bus-scale-routine-reads-2026-10-15.py"


class VerifyExternalPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Genuine verification (including import, test and native cleanup)
        # authors one shared witness. Each control gets fresh mutable testimony
        # and the exact original disk bytes at the same bound coordinates.
        witness = cls()
        cls.addClassCleanup(witness.doCleanups)
        witness._prepare_native_witness()
        cls._native_witness = witness
        cls._native_tree = {}
        for path in witness.bank.rglob("*"):
            witness.assertFalse(path.is_symlink())
            metadata = path.stat()
            cls._native_tree[path.relative_to(witness.bank).as_posix()] = (
                None if path.is_dir() else path.read_bytes(), stat.S_IMODE(metadata.st_mode))

    def setUp(self):
        witness = self._native_witness
        for name in ("bank", "home", "workspace", "entry", "helper", "scratch"):
            setattr(self, name, getattr(witness, name))
        self.context = copy.deepcopy(witness.context)
        self.calls = copy.deepcopy(witness.calls)
        self._restore_native_fixture()
        self.addCleanup(self._restore_native_fixture)

    def _restore_native_fixture(self):
        for path in sorted(self.bank.rglob("*"), key=lambda value: len(value.parts), reverse=True):
            expected = self._native_tree.get(path.relative_to(self.bank).as_posix())
            if expected is None or path.is_symlink() or path.is_dir() != (expected[0] is None):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        for relative, (raw, mode) in sorted(self._native_tree.items(), key=lambda item: len(Path(item[0]).parts)):
            path = self.bank / relative
            if raw is None:
                path.mkdir(exist_ok=True)
            elif not path.exists() or path.read_bytes() != raw:
                if path.exists():
                    path.chmod(0o600)
                path.write_bytes(raw)
            path.chmod(mode)

    def _prepare_native_witness(self):
        owned = fixture("verify")
        self.bank, self.home, self.workspace = owned.__enter__()
        self.addCleanup(owned.__exit__, None, None, None)
        self.entry = load(ENTRY, "verify_external_policy_entry")
        self.helper = load(READS, "verify_external_policy_read_helper")
        self.context = self.helper.prepare("verify", self.home, self.workspace, self.bank)
        before = {path.relative_to(self.home).as_posix(): path.read_bytes()
                  for path in self.home.rglob("*") if path.is_file()}
        self.calls = []
        native = subprocess.Popen.__init__

        def record(process, argv, *args, **kwargs):
            # The native constructor and children execute unchanged. This
            # observes admission inputs, never cost, evaluator or native I/O.
            self.assertEqual((), args)
            self.calls.append((list(argv), copy.deepcopy(kwargs)))
            return native(process, argv, *args, **kwargs)

        output, error = io.StringIO(), io.StringIO()
        previous = Path.cwd()
        try:
            os.chdir(self.workspace)
            with patch.object(subprocess.Popen, "__init__", record),\
                    redirect_stdout(output), redirect_stderr(error):
                code = cli.main(self.helper.arguments("verify", self.home, self.workspace, self.context))
        finally:
            os.chdir(previous)
        self.assertEqual("", error.getvalue())
        artifact = json.loads(output.getvalue())
        self.assertEqual(0, code, artifact)
        self.assertEqual("verified_match", artifact["evidence"]["outcome"])
        self.assertEqual({"created": True, "destroyed": True},
                         {key: artifact["evidence"]["scratch"][key] for key in ("created", "destroyed")})
        self.scratch = Path(artifact["evidence"]["scratch"]["path"])
        self.assertFalse(self.scratch.exists())
        self.assertEqual({"exit_code": 0, "status": "ok", "effect": {
            "kind": "delivery_claim_verified", "count": 1}},
            self.helper.witness("verify", self.home, before, artifact, self.context, {}))
        self.assertTrue(self.calls, "the control must execute genuine native external verification work")

    def policy(self):
        result = getattr(self.entry, "verify_subprocess", None)
        self.assertTrue(callable(result),
                        "missing Am10 native verifier subprocess admission over the pinned constant claim bank")
        return result

    def test_native_verifier_observer_scope_retains_proofs_and_explicit_unmeasured_children(self):
        scope = getattr(self.entry, "verify_observer_scope", None)
        self.assertTrue(callable(scope), "missing native Am10 observer-boundary receipt")
        original = copy.deepcopy(self.context)
        expected = {
            "bus_bytes": "parent_python_application_visible",
            "ledger_flock_hold": "parent_native_flock",
            "cpu": "whole_entry_child_tree",
            "child_bytes": {"status": "UNMEASURED", "reason": "not-ledger-addressed"},
            "git_locks": {"status": "UNMEASURED", "reason": "not-ledger-addressed"},
            "verification_repo_sha": self.context["verification_repo_sha"],
            "verification_tree": copy.deepcopy(self.context["verification_tree"]),
            "verification_git_files": copy.deepcopy(self.context["verification_git_files"]),
        }
        self.assertEqual(expected, scope(self.context))
        self.assertEqual(original, self.context, "observer-scope receipt must preserve its pinned input")

    def test_actual_banked_verifier_git_import_test_and_cleanup_calls_are_admitted_exactly(self):
        policy = self.policy()
        self.entry.verify_policy().validate_inputs(self.home, self.workspace, self.context)
        vectors = [argv for argv, _kwargs in self.calls]
        self.assertIn(["python3", "-c",
            "import importlib,sys;[importlib.import_module(name) for name in sys.argv[1:]]",
            "tests.test_census_delivery"], vectors)
        self.assertIn(["python3", "-m", "unittest", "tests.test_census_delivery"], vectors)
        self.assertTrue(any("worktree" in argv and "add" in argv for argv in vectors))
        self.assertTrue(any("worktree" in argv and "remove" in argv for argv in vectors))
        self.assertTrue(any(argv[-2:] == ["rev-list", "--stdin"] for argv in vectors),
                        "actual native cleanup reachability must be included")
        for argv, kwargs in self.calls:
            with self.subTest(argv=argv):
                self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context), (argv, kwargs))
                if argv[0] == "/usr/bin/git" and kwargs.get("cwd") is not None:
                    for index in range(1, len(argv)):
                        self.assertFalse(policy(argv[:index] + argv[index + 1:], kwargs,
                                                self.home, self.workspace, self.context))
                self.assertFalse(policy(argv + ["--unreviewed"], kwargs,
                                        self.home, self.workspace, self.context))
                self.assertFalse(policy(argv, dict(kwargs, shell=True),
                                        self.home, self.workspace, self.context))
                self.assertFalse(policy(argv, dict(kwargs, executable="/bin/sh"),
                                        self.home, self.workspace, self.context))
                self.assertFalse(policy(argv, dict(kwargs, close_fds=False),
                                        self.home, self.workspace, self.context))
                self.assertFalse(policy(argv, dict(kwargs, cwd=self.home),
                                        self.home, self.workspace, self.context))
                self.assertFalse(policy(argv, dict(kwargs, stdout=1),
                                        self.home, self.workspace, self.context))
                environment = dict(kwargs["env"], GIT_DIR=str(self.home))
                self.assertFalse(policy(argv, dict(kwargs, env=environment),
                                        self.home, self.workspace, self.context))
                environment = dict(kwargs["env"], PYTHONPATH=str(self.home))
                self.assertFalse(policy(argv, dict(kwargs, env=environment),
                                        self.home, self.workspace, self.context))
        for argv in (["/bin/sh", "-c", "true"], ["/bin/cat", str(self.home / "events.jsonl")],
                     ["python3", "-m", "unittest", "tests.other_bank"], ["/bin/ps", "aux"]):
            self.assertFalse(policy(argv, {}, self.home, self.workspace, self.context))

    def test_claim_tree_and_coordinate_proof_poisoning_cannot_authorize_native_vectors(self):
        policy = self.policy()
        argv, kwargs = self.calls[0]
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))
        alterations = {
            "wrong-case": lambda value: value.update(case="sent"),
            "wrong-claim": lambda value: value["claim"].update(id="delivery-claim-" + "0" * 32),
            "wrong-note-ref": lambda value: value["claim"].update(note_ref="msg-" + "0" * 32),
            "wrong-repository": lambda value: value["claim"].update(repo_path=str(self.bank)),
            "wrong-claim-sha": lambda value: value["claim"].update(sha="0" * 40),
            "wrong-bank": lambda value: value["claim"].update(bank=["tests.other_bank"]),
            "wrong-prepared-sha": lambda value: value.update(verification_repo_sha="0" * 40),
            "wrong-tree-hash": lambda value: value["verification_tree"][0].update(sha256="0" * 64),
            "wrong-tree-blob": lambda value: value["verification_tree"][0].update(blob="0" * 40),
            "symlink-mode": lambda value: value["verification_tree"][0].update(mode="120000"),
            "gitlink-mode": lambda value: value["verification_tree"][0].update(mode="160000", kind="commit"),
            "extra-runner": lambda value: value["verification_tree"].append(
                dict(value["verification_tree"][0], path="scripts/test")),
            "foreign-config-proof": lambda value: value["verification_git_files"][".git/config"].update(sha256="0" * 64),
        }
        for label, change in alterations.items():
            with self.subTest(poison=label):
                wrong = copy.deepcopy(self.context)
                change(wrong)
                self.assertFalse(policy(argv, kwargs, self.home, self.workspace, wrong))
        self.assertFalse(policy(argv, kwargs, self.home, self.bank, self.context))
        foreign = self.bank / "foreign-scale-fleet"
        foreign.mkdir(mode=0o700)
        self.assertFalse(policy(argv, kwargs, foreign, self.workspace, self.context))

    def test_actual_source_configuration_and_reference_tampering_is_refused_read_only(self):
        policy = self.policy()
        argv, kwargs = self.calls[0]
        for relative in ("note.md", "tests/__init__.py", "tests/test_census_delivery.py",
                         ".git/config", ".git/HEAD", ".git/refs/remotes/origin/main"):
            with self.subTest(path=relative):
                path = self.workspace / relative
                original = path.read_bytes()
                poisoned = (b"0" * 40 + b"\n" if relative.endswith("origin/main")
                            else original + b"\n# unreviewed source or Git configuration\n")
                try:
                    path.write_bytes(poisoned)
                    self.assertFalse(policy(argv, kwargs, self.home, self.workspace, self.context))
                    self.assertEqual(poisoned, path.read_bytes(), "policy must never repair a refused input")
                finally:
                    path.write_bytes(original)
                self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))
        path = self.workspace / "note.md"
        original, mode = path.read_bytes(), path.stat().st_mode & 0o777
        try:
            path.chmod(0o666)
            self.assertFalse(policy(argv, kwargs, self.home, self.workspace, self.context))
            self.assertEqual(0o666, path.stat().st_mode & 0o777)
        finally:
            path.chmod(mode)
        target = self.bank / "same-bytes-foreign-coordinate"
        target.write_bytes(original)
        try:
            path.unlink()
            path.symlink_to(target)
            self.assertFalse(policy(argv, kwargs, self.home, self.workspace, self.context))
            self.assertTrue(path.is_symlink(), "policy must preserve refused aliases")
        finally:
            path.unlink()
            path.write_bytes(original)
            path.chmod(mode)

    def test_actual_pinned_commit_object_and_resolved_head_tip_are_validated_read_only(self):
        policy = self.policy()
        argv, kwargs = self.calls[0]
        sha = self.context["verification_repo_sha"]
        commit = self.workspace / ".git/objects" / sha[:2] / sha[2:]
        self.assertTrue(commit.is_file(), "owned native verifier fixture must retain its loose selected commit")
        head = (self.workspace / ".git/HEAD").read_text().strip()
        self.assertTrue(head.startswith("ref: refs/heads/"))
        branch = self.workspace / ".git" / head[len("ref: "):]
        self.assertEqual(sha, branch.read_text().strip())
        for path, poisoned in ((commit, b"not a native compressed Git object\n"),
                               (branch, b"0" * 40 + b"\n")):
            with self.subTest(path=str(path.relative_to(self.workspace))):
                original = path.read_bytes()
                original_mode = path.stat().st_mode & 0o777
                try:
                    path.chmod(0o600)
                    path.write_bytes(poisoned)
                    self.assertFalse(policy(argv, kwargs, self.home, self.workspace, self.context))
                    self.assertEqual(poisoned, path.read_bytes(), "policy must preserve refused Git evidence")
                    self.assertEqual(0o600, path.stat().st_mode & 0o777,
                                     "policy must preserve refused Git evidence permissions")
                finally:
                    try:
                        path.write_bytes(original)
                    finally:
                        path.chmod(original_mode)
                self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))

    def test_extra_valid_loose_ref_expands_native_cleanup_inputs_and_is_refused(self):
        from floati.git_process import fixed_git_command, fixed_git_environment
        policy = self.policy()
        argv, kwargs = self.calls[0]
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))
        sha = self.context["verification_repo_sha"]
        relative = "refs/remotes/origin/extra"
        extra = self.workspace / ".git" / relative
        self.assertFalse(extra.exists())
        subprocess.run(fixed_git_command("/usr/bin/git", self.workspace,
                       ("update-ref", relative, sha)),
                       env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True)
        try:
            original = extra.read_bytes()
            self.assertEqual((sha + "\n").encode(), original)
            self.assertTrue(extra.is_file() and not extra.is_symlink())
            self.assertFalse(policy(argv, kwargs, self.home, self.workspace, self.context),
                             "an additional valid loose ref changes native all-ref cleanup work")
            self.assertEqual(original, extra.read_bytes(), "admission must preserve the refused loose ref")
        finally:
            extra.unlink()
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))

    def test_extra_valid_native_packed_ref_expands_cleanup_inputs_and_is_refused(self):
        from floati.git_process import fixed_git_command, fixed_git_environment
        policy = self.policy()
        argv, kwargs = self.calls[0]
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))
        sha = self.context["verification_repo_sha"]
        relative = "refs/remotes/origin/extra"
        extra = self.workspace / ".git" / relative
        packed = self.workspace / ".git/packed-refs"
        self.assertFalse(extra.exists())
        self.assertFalse(packed.exists(), "the native fixture must begin with only its pinned loose refs")
        try:
            for query in (("update-ref", relative, sha), ("pack-refs", "--all", "--no-prune")):
                subprocess.run(fixed_git_command("/usr/bin/git", self.workspace, query),
                               env=fixed_git_environment("/usr/bin/git"), check=True, capture_output=True)
            # Keep the selected pinned loose refs byte-identical. Only this
            # extra ref is resolved from the actual native packed-ref file.
            extra.unlink()
            original = packed.read_bytes()
            self.assertIn((sha + " " + relative + "\n").encode(), original)
            resolved = subprocess.run(fixed_git_command("/usr/bin/git", self.workspace,
                                     ("show-ref", "--verify", relative)),
                                     env=fixed_git_environment("/usr/bin/git"), check=True,
                                     capture_output=True).stdout
            self.assertEqual((sha + " " + relative + "\n").encode(), resolved)
            self.assertFalse(policy(argv, kwargs, self.home, self.workspace, self.context),
                             "an additional valid packed ref changes native all-ref cleanup work")
            self.assertEqual(original, packed.read_bytes(), "admission must preserve the refused packed refs")
            self.assertFalse(extra.exists(), "admission must not unpack a refused ref")
        finally:
            if extra.exists():
                extra.unlink()
            if packed.exists():
                packed.unlink()
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))

    def test_extra_empty_ref_directory_is_refused_without_cleanup(self):
        policy = self.policy()
        argv, kwargs = self.calls[0]
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))
        for relative in ("refs/extra-empty", "refs/remotes/origin/extra-empty", "refs/tags/extra-empty"):
            with self.subTest(relative=relative):
                extra = self.workspace / ".git" / relative
                extra.mkdir(mode=0o700)
                try:
                    self.assertFalse(policy(argv, kwargs, self.home, self.workspace, self.context),
                                     "even an empty extra ref directory violates the exact native namespace")
                    self.assertTrue(extra.is_dir() and not extra.is_symlink())
                    self.assertEqual([], list(extra.iterdir()), "admission must preserve the refused directory")
                finally:
                    extra.rmdir()
                self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))

    def test_extra_ref_alias_is_refused_without_following_or_removing_it(self):
        policy = self.policy()
        argv, kwargs = self.calls[0]
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))
        extra = self.workspace / ".git/refs/remotes/origin/extra-alias"
        for target in (self.workspace / ".git/refs/heads/main", self.bank / "absent-ref-target"):
            with self.subTest(target=str(target)):
                extra.symlink_to(target)
                try:
                    self.assertFalse(policy(argv, kwargs, self.home, self.workspace, self.context),
                                     "extra ref aliases violate the exact native namespace")
                    self.assertTrue(extra.is_symlink(), "admission must preserve a refused alias")
                    self.assertEqual(str(target), os.readlink(extra))
                finally:
                    extra.unlink()
                self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))

    def test_dangling_object_alternates_alias_is_refused_without_removing_it(self):
        policy = self.policy()
        argv, kwargs = self.calls[0]
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))
        alternates = self.workspace / ".git/objects/info/alternates"
        target = self.bank / "absent-object-repository"
        self.assertFalse(os.path.lexists(alternates))
        self.assertFalse(os.path.lexists(target))
        alternates.symlink_to(target)
        try:
            self.assertTrue(alternates.is_symlink())
            self.assertFalse(alternates.exists(), "the native input must be a dangling alias")
            admitted = policy(argv, kwargs, self.home, self.workspace, self.context)
            self.assertTrue(alternates.is_symlink(), "admission must preserve the refused alias")
            self.assertEqual(str(target), os.readlink(alternates))
            self.assertFalse(admitted,
                             "a dangling object alternates alias violates the pinned native Git input")
        finally:
            alternates.unlink()
        self.assertTrue(policy(argv, kwargs, self.home, self.workspace, self.context))


if __name__ == "__main__":
    unittest.main()

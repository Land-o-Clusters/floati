"""Exact banking readback must work with Git 2.43's global option parser."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from floati import effect_reconciliation_observer as observer
from floati.effect_reconciliation_protocol import build_request, validate_result
from floati.ids import uuid7_hex
from tests.readiness import run_process
from tests.temp_roots import REAL_TEMP_ROOT


class AdhocRemoteCompatibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.environment = {
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.base),
            "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
        }

    def git(self, *arguments, data=None):
        return run_process(["/usr/bin/git", *arguments], "local Git fixture returns",
            cwd=self.base, env=self.environment, input=data, capture_output=True, check=True).stdout

    def remote(self, branch, object_format):
        remote = self.base / (branch + "-" + object_format + ".git")
        self.git("-c", "init.defaultBranch=" + branch, "init", "--bare", "--quiet",
            "--object-format=" + object_format, str(remote))
        self.assertEqual(("refs/heads/" + branch + "\n").encode(),
            self.git("--git-dir=" + str(remote), "symbolic-ref", "HEAD"))
        self.assertEqual((object_format + "\n").encode(),
            self.git("--git-dir=" + str(remote), "rev-parse", "--show-object-format"))
        tree = self.git("--git-dir=" + str(remote), "mktree", data=b"").strip().decode()
        sha = self.git("--git-dir=" + str(remote), "-c", "user.name=Floati Tests",
            "-c", "user.email=tests@floati.invalid", "commit-tree", tree,
            data=b"exact banking fixture\n").strip().decode()
        reference = "refs/heads/codex/lane/node-a/result"
        self.git("--git-dir=" + str(remote), "update-ref", reference, sha)
        self.assertEqual((sha + "\n").encode(),
            self.git("--git-dir=" + str(remote), "rev-parse", reference))
        self.assertEqual((sha + "\t" + reference + "\n").encode(),
            self.git("ls-remote", "--refs", str(remote), reference))
        return remote, reference, sha

    def request(self, remote, reference, sha):
        coordinate = str(remote)
        return build_request(operation_id="effect-op-" + uuid7_hex(),
            current_evidence_id="effect-unknown-" + uuid7_hex(), adapter="git_remote_explicit",
            target={"kind": "git_remote_ref", "coordinate": coordinate,
                "identity_digest": hashlib.sha256(coordinate.encode()).hexdigest()},
            expected_confirmation={"kind": "git_remote_ref_equals", "locator": reference,
                "expected_digest": sha}, budget_claim={"git": 1},
            local_repository_identity=None)

    @staticmethod
    def git_243_runner():
        real_run = observer._run_git

        def run(arguments, environment):
            # v2.43.0 git.c rejects this global option before dispatching
            # ls-remote. Compatible argv still reaches the real Git transport;
            # no successful ref observation is fabricated by this boundary.
            if "--no-lazy-fetch" in arguments[:arguments.index("ls-remote")]:
                return "failed", b"", b"unknown option: --no-lazy-fetch\n"
            return real_run(arguments, environment)

        return run

    def test_exact_remote_readback_survives_older_git_for_both_object_formats(self):
        for branch in ("main", "master"):
            for object_format, length in (("sha1", 40), ("sha256", 64)):
                with self.subTest(default_branch=branch, object_format=object_format):
                    remote, reference, sha = self.remote(branch, object_format)
                    self.assertEqual(length, len(sha))
                    request = self.request(remote, reference, sha)
                    with mock.patch.object(observer, "_run_git", side_effect=self.git_243_runner()):
                        result = validate_result(observer._remote_result(request, None), request)
                    self.assertEqual(("unknown", "reconciliation_inconclusive"),
                        (result.outcome, result.reason_code))
                    self.assertEqual(sha, result.observation["observed_ref_digest"])
                    self.assertRegex(result.evidence_digest, r"^[0-9a-f]{64}$")

    def test_older_git_compatibility_preserves_exact_sha_and_ref_fences(self):
        remote, reference, sha = self.remote("main", "sha1")
        for locator, expected, reason in (
            (reference, "0" * 40, "ref_digest_mismatch"),
            ("refs/heads/absent", sha, "confirmation_absent"),
        ):
            with self.subTest(locator=locator, expected=expected):
                request = self.request(remote, locator, expected)
                with mock.patch.object(observer, "_run_git", side_effect=self.git_243_runner()):
                    result = validate_result(observer._remote_result(request, None), request)
                self.assertEqual(("failed", reason), (result.outcome, result.reason_code))
                self.assertRegex(result.evidence_digest, r"^[0-9a-f]{64}$")

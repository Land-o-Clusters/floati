from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import select
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

from floati.errors import ProtocolRefusal


def copy_exact_observer_source_package(destination: Path) -> tuple[Path, Path]:
    """Copy the two descriptor-loaded sources into one private test package."""

    destination.mkdir(mode=0o700, parents=True)
    source_package = Path(__file__).parents[1] / "floati"
    protocol = destination / "effect_reconciliation_protocol.py"
    observer = destination / "effect_reconciliation_observer.py"
    shutil.copyfile(source_package / protocol.name, protocol)
    shutil.copyfile(source_package / observer.name, observer)
    return protocol, observer


class ReconciliationObserverTests(unittest.TestCase):
    """Read-only behavior at the descriptor-bound observer boundary."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = self.base / "repository"
        self.repository.mkdir()
        self.git("init", "--quiet", "--object-format=sha256", "--initial-branch=main")
        (self.repository / "README.md").write_text("observer fixture\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git(
            "-c", "user.name=Slipway Tests",
            "-c", "user.email=tests@slipway.invalid",
            "commit", "--quiet", "-m", "fixture",
        )
        self.sha = self.git("rev-parse", "HEAD").stdout.strip()

    def observer(self):
        # Kept lazy so the RED bank proves the new production module is absent.
        from floati import effect_reconciliation_observer

        return effect_reconciliation_observer

    def protocol(self):
        from floati.effect_reconciliation_protocol import (
            build_request,
            decode_result_frame,
            encode_frame,
        )

        return build_request, decode_result_frame, encode_frame

    def git(
        self, *arguments: str, cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess[str]:
        environment = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": "/var/empty",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
        return subprocess.run(
            ["/usr/bin/git", *arguments], cwd=self.repository if cwd is None else cwd,
            env=environment, check=True, capture_output=True, text=True,
        )

    @staticmethod
    def identity(path: Path) -> tuple[int, int]:
        metadata = path.stat()
        return metadata.st_dev, metadata.st_ino

    @staticmethod
    def local_identity_digest(
        coordinate: str, repository_identity: tuple[int, int],
    ) -> str:
        payload = {
            "device": repository_identity[0],
            "inode": repository_identity[1],
            "path": coordinate,
        }
        return hashlib.sha256(json.dumps(
            payload, ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()

    def request(
        self, adapter: str = "git_local", *, coordinate: Optional[str] = None,
        locator: str = "refs/heads/main", digest: Optional[str] = None,
        repository_identity: Optional[tuple[int, int]] = None,
        identity_digest: Optional[str] = None,
    ):
        build_request, _, _ = self.protocol()
        if adapter == "git_local":
            selected_coordinate = str(self.repository) if coordinate is None else coordinate
            target_kind = "git_ref"
            confirmation_kind = "git_ref_equals"
            selected_identity = self.identity(self.repository) if repository_identity is None else repository_identity
        elif adapter == "git_remote_explicit":
            selected_coordinate = str(self.repository) if coordinate is None else coordinate
            target_kind = "git_remote_ref"
            confirmation_kind = "git_remote_ref_equals"
            selected_identity = None
        elif adapter == "github_explicit":
            selected_coordinate = "owner/repository#1" if coordinate is None else coordinate
            target_kind = "github_resource"
            confirmation_kind = "github_idempotency_marker"
            selected_identity = None
            locator = "marker"
        elif adapter == "deployment_explicit":
            selected_coordinate = "deployment/production" if coordinate is None else coordinate
            target_kind = "deployment_target"
            confirmation_kind = "deployment_artifact_equals"
            selected_identity = None
            locator = "artifact"
        else:
            selected_coordinate = "workspace" if coordinate is None else coordinate
            target_kind = "shell_environment"
            confirmation_kind = "none"
            selected_identity = None
            locator = "none"
        if identity_digest is None:
            if adapter == "git_local":
                identity_digest = self.local_identity_digest(
                    selected_coordinate, selected_identity,
                )
            else:
                identity_digest = hashlib.sha256(
                    selected_coordinate.encode("utf-8")
                ).hexdigest()
        return build_request(
            operation_id="effect-op-018f7e9b3c117abc8def0123456789ab",
            current_evidence_id="effect-unknown-018f7e9b3c117abc8def0123456789ab",
            adapter=adapter,
            target={
                "kind": target_kind,
                "coordinate": selected_coordinate,
                "identity_digest": identity_digest,
            },
            expected_confirmation={
                "kind": confirmation_kind,
                "locator": locator,
                "expected_digest": self.sha if digest is None else digest,
            },
            budget_claim={"git": 1},
            local_repository_identity=selected_identity,
            request_id="1" * 32,
        )

    @staticmethod
    def _relocate(descriptor: int) -> int:
        return fcntl.fcntl(descriptor, fcntl.F_DUPFD, 20)

    def invoke(
        self, request, *, repository: Optional[Path] = None,
        hostile_descriptors: tuple[int, ...] = (), patch_observer=None,
        occupy_fixed: bool = False,
    ):
        observer = self.observer()
        _, decode_result_frame, encode_frame = self.protocol()
        occupants: list[int] = []
        if occupy_fixed:
            while True:
                descriptor = os.open(os.devnull, os.O_RDONLY)
                occupants.append(descriptor)
                if descriptor >= 6:
                    break
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        repository_descriptor = None
        if repository is not None:
            repository_descriptor = os.open(
                repository,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        pid = os.fork()
        if pid == 0:
            try:
                parent.close()
                channel_source = self._relocate(child.fileno())
                repository_source = None
                if repository_descriptor is not None:
                    repository_source = self._relocate(repository_descriptor)
                os.dup2(channel_source, 3)
                os.close(channel_source)
                if repository_source is not None:
                    os.dup2(repository_source, 6)
                    os.close(repository_source)
                if patch_observer is not None:
                    observer._observe_request = patch_observer
                status = observer.run_observer(
                    channel_fd=3,
                    repository_fd=(6 if repository_source is not None else None),
                )
            except BaseException:
                status = 97
            os._exit(status)
        child.close()
        if repository_descriptor is not None:
            os.close(repository_descriptor)
        for descriptor in occupants:
            os.close(descriptor)
        try:
            parent.sendall(encode_frame(request))
            parent.shutdown(socket.SHUT_WR)
            chunks: list[bytes] = []
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                readable, _, _ = select.select([parent], [], [], 0.1)
                if not readable:
                    continue
                chunk = parent.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            waited, status = os.waitpid(pid, 0)
            self.assertEqual(pid, waited)
            self.assertEqual(0, os.waitstatus_to_exitcode(status))
            frame = b"".join(chunks)
            return decode_result_frame(frame, request), frame
        finally:
            parent.close()

    def test_observer_closes_and_reverifies_every_unruled_actual_fd(self) -> None:
        """Catches cwd/session setup or repeated actual-FD closure being skipped."""
        leaked_read, leaked_write = os.pipe()
        self.addCleanup(self._safe_close, leaked_read)
        self.addCleanup(self._safe_close, leaked_write)
        os.set_inheritable(leaked_read, True)
        os.set_inheritable(leaked_write, True)

        proof = self.base / "observer-fd-proof.json"

        def inspect(request, repository_fd):
            proof.write_text(json.dumps({
                "cwd": os.getcwd(),
                "session_is_pid": os.getsid(0) == os.getpid(),
                "open_fds": sorted(self.observer()._open_descriptors()),
                "leak_errno": self._fstat_errno(leaked_read),
            }), encoding="utf-8")
            return self.observer().build_result(
                request, outcome="unknown", reason_code="reconciliation_inconclusive",
                observation={"adapter": "none"},
            )

        result, _ = self.invoke(
            self.request("none"), hostile_descriptors=(leaked_read, leaked_write),
            patch_observer=inspect, occupy_fixed=True,
        )
        diagnostic = json.loads(proof.read_text(encoding="utf-8"))
        self.assertEqual("/", diagnostic["cwd"])
        self.assertTrue(diagnostic["session_is_pid"])
        self.assertEqual(errno.EBADF, diagnostic["leak_errno"])
        self.assertEqual([0, 1, 2, 3], diagnostic["open_fds"])

    def test_observer_never_inherits_ledger_or_tenant_write_authority(self) -> None:
        """Catches readable ledger or tenant-write descriptors surviving pre-request closure."""
        ledger_path = self.base / "effect-ledger.jsonl"
        tenant_path = self.base / "tenant-write-target"
        secret = "ledger-secret-never-output"
        ledger_path.write_text(secret, encoding="utf-8")
        tenant_path.write_text("unchanged", encoding="utf-8")
        ledger = os.open(ledger_path, os.O_RDONLY)
        tenant = os.open(tenant_path, os.O_WRONLY | os.O_APPEND)
        self.addCleanup(self._safe_close, ledger)
        self.addCleanup(self._safe_close, tenant)
        os.set_inheritable(ledger, True)
        os.set_inheritable(tenant, True)

        proof = self.base / "observer-authority-proof.json"

        def inspect(request, repository_fd):
            proof.write_text(json.dumps({
                "ledger_errno": self._fstat_errno(ledger),
                "tenant_errno": self._fstat_errno(tenant),
            }), encoding="utf-8")
            return self.observer().build_result(
                request, outcome="unknown", reason_code="reconciliation_inconclusive",
                observation={"adapter": "none"},
            )

        result, raw = self.invoke(
            self.request("none"), hostile_descriptors=(ledger, tenant),
            patch_observer=inspect,
        )
        diagnostic = json.loads(proof.read_text(encoding="utf-8"))
        self.assertEqual(errno.EBADF, diagnostic["ledger_errno"])
        self.assertEqual(errno.EBADF, diagnostic["tenant_errno"])
        self.assertNotIn(str(ledger_path).encode(), raw)
        self.assertNotIn(str(tenant_path).encode(), raw)
        self.assertNotIn(secret.encode(), raw)
        self.assertEqual("unchanged", tenant_path.read_text(encoding="utf-8"))

    def test_observer_scrubs_python_native_loader_and_git_environment(self) -> None:
        """Catches Python/native-loader or ambient Git variables reaching observation."""
        hostile = {
            "PYTHONPATH": "/hostile/python",
            "PYTHONHOME": "/hostile/home",
            "PYTHONSTARTUP": "/hostile/startup",
            "DYLD_INSERT_LIBRARIES": "/hostile/dylib",
            "LD_PRELOAD": "/hostile/preload",
            "LIBPATH": "/hostile/lib",
            "SHLIB_PATH": "/hostile/shlib",
            "GIT_DIR": "/hostile/git-dir",
            "GIT_WORK_TREE": "/hostile/work-tree",
            "GIT_SSH_COMMAND": "hostile-helper",
        }

        proof = self.base / "observer-environment-proof.json"

        def inspect(request, repository_fd):
            proof.write_text(json.dumps({
                "remaining": sorted(key for key in hostile if key in os.environ),
            }), encoding="utf-8")
            return self.observer().build_result(
                request, outcome="unknown", reason_code="reconciliation_inconclusive",
                observation={"adapter": "none"},
            )

        with mock.patch.dict(os.environ, hostile, clear=False):
            result, _ = self.invoke(self.request("none"), patch_observer=inspect)
        diagnostic = json.loads(proof.read_text(encoding="utf-8"))
        self.assertEqual([], diagnostic["remaining"])

    def test_observer_channel_and_optional_repository_fd_contract_is_exact(self) -> None:
        """Catches non-local repository authority or missing/substituted local fd acceptance."""
        with self.assertRaises(ValueError):
            self.observer().run_observer(channel_fd=4)
        with self.assertRaises(ValueError):
            self.observer().run_observer(repository_fd=5)
        remote = self.request("git_remote_explicit")
        result, _ = self.invoke(remote, repository=self.repository)
        self.assertEqual(("unknown", "contract_invalid"), (result.outcome, result.reason_code))
        local = self.request()
        missing, _ = self.invoke(local)
        self.assertEqual(("unknown", "repository_identity_changed"), (missing.outcome, missing.reason_code))

    def test_local_git_observes_only_exact_full_ref_and_object_through_fd(self) -> None:
        """Catches pathname reopening, shorthand, or non-object evidence becoming observation."""
        request = self.request()
        alias = self.base / "repository-alias"
        alias.symlink_to(self.repository, target_is_directory=True)
        original_identity_digest = self.local_identity_digest(
            str(self.repository), self.identity(self.repository),
        )
        target_cases = (
            self.request(
                coordinate=str(alias), identity_digest=original_identity_digest,
            ),
            self.request(identity_digest="0" * 64),
            self.request(
                coordinate=str(self.repository / ".." / "repository"),
                identity_digest=original_identity_digest,
            ),
        )
        status_before = self.git("status", "--porcelain=v1").stdout
        for target_request in target_cases:
            with self.subTest(target=target_request.target):
                target_descriptor = os.open(self.repository, os.O_RDONLY)
                try:
                    target_result = self.observer()._observe_request(
                        target_request, target_descriptor,
                    )
                finally:
                    os.close(target_descriptor)
                self.assertEqual(
                    ("unknown", "repository_fence_invalid"),
                    (target_result.outcome, target_result.reason_code),
                )
                self.assertIsNone(target_result.confirmation)
        self.assertEqual(status_before, self.git("status", "--porcelain=v1").stdout)

        descriptor = os.open(self.repository, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        displaced = self.base / "opened-repository"
        replacement = self.base / "replacement"
        replacement.mkdir()
        self.git("init", "--quiet", "--object-format=sha256", "--initial-branch=main", cwd=replacement)
        (replacement / "README.md").write_text("replacement\n", encoding="utf-8")
        self.git("add", "README.md", cwd=replacement)
        self.git(
            "-c", "user.name=Slipway Tests",
            "-c", "user.email=tests@slipway.invalid",
            "commit", "--quiet", "-m", "replacement", cwd=replacement,
        )
        replacement_sha = self.git("rev-parse", "HEAD", cwd=replacement).stdout.strip()
        self.repository.rename(displaced)
        replacement.rename(self.repository)
        try:
            # /dev/fd preserves the already-open directory identity despite pathname replacement.
            result = self.observer()._observe_request(request, descriptor)
        finally:
            os.close(descriptor)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (result.outcome, result.reason_code),
        )
        self.assertEqual({"observed_ref_digest": self.sha}, result.observation)
        self.assertIsNone(result.confirmation)
        self.assertIsNone(result.measured_spend)
        self.assertNotEqual(self.sha, replacement_sha)

        calls: list[tuple[list[str], dict[str, str]]] = []

        def capture(arguments, environment):
            calls.append((arguments, environment))
            if len(calls) == 1:
                return "ok", (self.sha + "\n").encode("ascii"), b""
            return "ok", b"", b""

        held = os.open(displaced, os.O_RDONLY)
        try:
            with mock.patch.object(self.observer(), "_run_git", capture):
                captured_result = self.observer()._observe_request(request, held)
        finally:
            os.close(held)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (captured_result.outcome, captured_result.reason_code),
        )
        self.assertEqual(
            [
                [
                    "/usr/bin/git", "-c", "core.hooksPath=/dev/null",
                    "--no-lazy-fetch",
                    "--no-optional-locks", "--no-replace-objects",
                    "rev-parse", "--verify", "refs/heads/main^{object}",
                ],
                [
                    "/usr/bin/git", "-c", "core.hooksPath=/dev/null",
                    "--no-lazy-fetch",
                    "--no-optional-locks", "--no-replace-objects",
                    "cat-file", "-e", self.sha + "^{object}",
                ],
            ],
            [arguments for arguments, _environment in calls],
        )

    def test_local_git_wrong_ref_wrong_sha_and_missing_object_fail_closed(self) -> None:
        """Catches wrong refs, wrong SHA, absent objects, malformed output, or timeout becoming truth."""
        cases = (
            (self.request(locator="main"), "unknown", "contract_invalid"),
            (self.request(locator="HEAD"), "unknown", "contract_invalid"),
            (self.request(locator="refs/heads/missing"), "failed", "confirmation_absent"),
            (self.request(digest="f" * 64), "failed", "expected_object_absent"),
        )
        for request, outcome, reason in cases:
            with self.subTest(
                locator=request.expected_confirmation["locator"],
                digest=request.expected_confirmation["expected_digest"],
            ):
                result, _ = self.invoke(request, repository=self.repository)
                self.assertEqual((outcome, reason), (result.outcome, result.reason_code))
                self.assertIsNone(result.confirmation)

        def malformed(*args, **kwargs):
            return "ok", b"not-an-object-id\n", b""

        def timeout(*args, **kwargs):
            return "timeout", b"", b""

        for runner, reason in ((malformed, "evidence_malformed"), (timeout, "git_observation_timeout")):
            with self.subTest(reason=reason), mock.patch.object(self.observer(), "_run_git", runner):
                descriptor = os.open(self.repository, os.O_RDONLY)
                try:
                    result = self.observer()._observe_request(self.request(), descriptor)
                finally:
                    os.close(descriptor)
                self.assertEqual(("unknown", reason), (result.outcome, result.reason_code))

    def test_local_git_disables_hooks_config_replacements_prompts_and_pagers(self) -> None:
        """Catches hooks, replacements, prompts, pagers, ambient config, or tracking refs influencing local proof."""
        marker = self.base / "hook-ran"
        hooks = self.base / "hooks"
        hooks.mkdir()
        hook = hooks / "reference-transaction"
        hook.write_text("#!/bin/sh\ntouch '%s'\nexit 1\n" % marker, encoding="utf-8")
        hook.chmod(0o755)
        self.git("update-ref", "refs/remotes/origin/main", self.sha)
        self.git("config", "core.hooksPath", str(hooks))
        hostile = {
            "GIT_DIR": str(self.base / "missing"), "GIT_WORK_TREE": str(self.base / "missing-work"),
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.hooksPath",
            "GIT_CONFIG_VALUE_0": str(hooks), "GIT_REPLACE_REF_BASE": "refs/replace/",
            "GIT_TERMINAL_PROMPT": "1", "GIT_PAGER": "hostile-pager",
        }
        with mock.patch.dict(os.environ, hostile, clear=False):
            result, _ = self.invoke(self.request(), repository=self.repository)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (result.outcome, result.reason_code),
        )
        self.assertFalse(marker.exists())
        remote_request = self.request("git_remote_explicit", coordinate=str(self.base / "absent"))
        remote, _ = self.invoke(remote_request)
        self.assertEqual(("unknown", "destination_unqueryable"), (remote.outcome, remote.reason_code))

    def test_local_git_disables_lazy_promisor_fetch_and_preserves_object_store(self) -> None:
        """Catches a missing promisor object launching a helper or mutating local Git state."""
        self.git("config", "uploadpack.allowFilter", "true")
        blob = self.git("rev-parse", "HEAD:README.md").stdout.strip()
        promisor = self.base / "promisor-clone"
        environment = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": "/var/empty",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }
        subprocess.run(
            [
                "/usr/bin/git", "-c", "protocol.file.allow=always", "clone",
                "--quiet", "--filter=blob:none", "--no-checkout",
                "file://" + str(self.repository), str(promisor),
            ],
            cwd=self.base,
            env=environment,
            check=True,
            capture_output=True,
        )
        missing = subprocess.run(
            ["/usr/bin/git", "--no-lazy-fetch", "cat-file", "-e", blob + "^{object}"],
            cwd=promisor,
            env=environment,
            check=False,
            capture_output=True,
        )
        self.assertNotEqual(0, missing.returncode, "fixture blob must begin absent")

        marker = self.base / "promisor-helper-ran"
        helper = self.base / "hostile-upload-pack"
        helper.write_text(
            "#!/bin/sh\n"
            f"touch {str(marker)!r}\n"
            "exec /usr/bin/git-upload-pack \"$1\"\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        subprocess.run(
            [
                "/usr/bin/git", "config", "remote.origin.uploadpack",
                str(helper),
            ],
            cwd=promisor,
            env=environment,
            check=True,
            capture_output=True,
        )

        def object_store() -> dict[str, bytes]:
            root = promisor / ".git" / "objects"
            return {
                str(path.relative_to(root)): path.read_bytes()
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

        commit = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"],
            cwd=promisor,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        lawful_request = self.request(
            coordinate=str(promisor), digest=commit,
            repository_identity=self.identity(promisor),
        )
        before_control = object_store()
        lawful, _ = self.invoke(lawful_request, repository=promisor)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (lawful.outcome, lawful.reason_code),
        )
        self.assertEqual(before_control, object_store())
        self.assertFalse(marker.exists())

        hostile_request = self.request(
            coordinate=str(promisor), digest=blob,
            repository_identity=self.identity(promisor),
        )
        before_hostile = object_store()
        hostile, _ = self.invoke(hostile_request, repository=promisor)
        self.assertFalse(marker.exists())
        self.assertEqual(before_hostile, object_store())
        self.assertEqual(
            ("failed", "expected_object_absent"),
            (hostile.outcome, hostile.reason_code),
        )

    def test_local_git_repository_fd_identity_mismatch_is_unknown_without_git(self) -> None:
        """Catches a substituted repository descriptor reaching Git at all."""
        called: list[bool] = []

        def forbidden(*args, **kwargs):
            called.append(True)
            raise AssertionError("git must not run")

        wrong_identity = (self.identity(self.repository)[0], self.identity(self.repository)[1] + 1)
        request = self.request(repository_identity=wrong_identity)
        descriptor = os.open(self.repository, os.O_RDONLY)
        try:
            with mock.patch.object(self.observer(), "_run_git", forbidden):
                result = self.observer()._observe_request(request, descriptor)
        finally:
            os.close(descriptor)
        self.assertEqual(("unknown", "repository_identity_changed"), (result.outcome, result.reason_code))
        self.assertEqual([], called)

    def test_local_git_observation_restores_caller_working_directory(self) -> None:
        """Catches in-process observer controls contaminating every later relative fixture."""

        original = Path.cwd()
        descriptor = os.open(self.repository, os.O_RDONLY)
        try:
            result = self.observer()._observe_request(self.request(), descriptor)
            observed = Path.cwd()
        finally:
            os.close(descriptor)
            if Path.cwd() != original:
                os.chdir(original)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (result.outcome, result.reason_code),
        )
        self.assertEqual(original, observed)

    def test_remote_fixture_observes_exact_explicit_full_ref_without_local_fallback(self) -> None:
        """Catches shorthand, extra output, or local tracking state satisfying explicit remote observation."""
        bare = self.base / "remote-fixture.git"
        self.git("init", "--quiet", "--bare", "--object-format=sha256", bare.as_posix())
        self.git("push", "--quiet", str(bare), "refs/heads/main:refs/heads/main")
        request = self.request("git_remote_explicit", coordinate=str(bare))
        result, _ = self.invoke(request)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (result.outcome, result.reason_code),
        )
        self.assertEqual(self.sha, result.observation["observed_ref_digest"])
        self.assertEqual("filesystem_fixture", result.observation["evidence_scope"])
        self.assertIsNone(result.confirmation)
        cases = (
            (self.request("git_remote_explicit", coordinate=str(bare), locator="main"), "unknown", "contract_invalid"),
            (
                self.request(
                    "git_remote_explicit", coordinate=str(bare),
                    locator="refs/heads/missing",
                ),
                "failed", "confirmation_absent",
            ),
            (
                self.request(
                    "git_remote_explicit", coordinate=str(bare), digest="f" * 64,
                ),
                "failed", "ref_digest_mismatch",
            ),
        )

        for candidate, outcome, reason in cases:
            with self.subTest(
                locator=candidate.expected_confirmation["locator"],
                digest=candidate.expected_confirmation["expected_digest"],
            ):
                observed, _ = self.invoke(candidate)
                self.assertEqual((outcome, reason), (observed.outcome, observed.reason_code))
                self.assertIsNone(observed.confirmation)
        self.git("update-ref", "refs/remotes/origin/main", self.sha)
        missing = self.request("git_remote_explicit", coordinate=str(self.base / "missing"))
        failed, _ = self.invoke(missing)
        self.assertEqual(("unknown", "destination_unqueryable"), (failed.outcome, failed.reason_code))

        def duplicate(*args, **kwargs):
            line = (self.sha + "\trefs/heads/main\n").encode("ascii")
            return "ok", line + line, b""

        with mock.patch.object(self.observer(), "_run_git", duplicate):
            malformed = self.observer()._observe_request(request, None)
        self.assertEqual(("unknown", "evidence_malformed"), (malformed.outcome, malformed.reason_code))

    def test_exact_git_observation_does_not_manufacture_measured_spend(self) -> None:
        """Catches exact ref evidence being relabeled as complete resource measurement."""
        local_request = self.request()
        local, _ = self.invoke(local_request, repository=self.repository)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (local.outcome, local.reason_code),
        )
        self.assertEqual(
            {"observed_ref_digest": self.sha},
            local.observation,
        )
        self.assertIsNone(local.confirmation)
        self.assertEqual("unknown", local.spend_status)
        self.assertIsNone(local.measured_spend)

        bare = self.base / "measurement-remote.git"
        self.git("init", "--quiet", "--bare", "--object-format=sha256", bare.as_posix())
        self.git("push", "--quiet", str(bare), "refs/heads/main:refs/heads/main")
        remote_request = self.request("git_remote_explicit", coordinate=str(bare))
        remote, _ = self.invoke(remote_request)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (remote.outcome, remote.reason_code),
        )
        self.assertEqual(self.sha, remote.observation["observed_ref_digest"])
        self.assertEqual("filesystem_fixture", remote.observation["evidence_scope"])
        self.assertIsNone(remote.confirmation)
        self.assertEqual("unknown", remote.spend_status)
        self.assertIsNone(remote.measured_spend)

    def test_remote_coordinate_grammar_refuses_helpers_shorthand_controls_and_options(self) -> None:
        """Catches helper schemes, shorthand, controls, options, or relative paths selecting Git behavior."""
        coordinates = (
            "host:path", "user@host:path", "ext::helper", "file:///tmp/repository",
            "relative/repository", "../repository", "-uploader",
            "git://host/repository", "ftp://host/repository",
            "https://host/a/../repository.git",
            "https://host/a//repository.git",
            "https://HOST/repository.git",
            "https://host/%72epository.git",
            "https://host:443/repository.git",
            "ssh://git@HOST/repository.git",
            "ssh://git@host:22/repository.git",
        )
        for coordinate in coordinates:
            with self.subTest(coordinate=coordinate):
                request = self.request("git_remote_explicit", coordinate=coordinate)
                with mock.patch.object(
                    self.observer(), "_run_git",
                    side_effect=AssertionError("noncanonical coordinate reached Git"),
                ):
                    result = self.observer()._observe_request(request, None)
                self.assertEqual(("unknown", "remote_coordinate_unsupported"), (result.outcome, result.reason_code))
        with self.assertRaises(ProtocolRefusal):
            self.request("git_remote_explicit", coordinate="https://host/path\nnext")
        canonical_ssh = self.request(
            "git_remote_explicit",
            coordinate="ssh://git@host.example:2222/org/repository.git",
        )
        with mock.patch.object(
            self.observer(), "_run_git", return_value=("failed", b"", b""),
        ) as run_git:
            canonical_result = self.observer()._observe_request(canonical_ssh, None)
        self.assertEqual(
            ("unknown", "destination_unqueryable"),
            (canonical_result.outcome, canonical_result.reason_code),
        )
        self.assertEqual(1, run_git.call_count)

    def test_remote_git_receives_only_the_exact_environment_allowlist(self) -> None:
        """Catches unruled parent variables or unsafe allowlisted values reaching remote Git."""
        captured: dict[str, object] = {}

        def capture(arguments, environment):
            captured["arguments"] = arguments
            captured["environment"] = environment
            return "failed", b"", b""

        inherited = {
            "SSH_AUTH_SOCK": "/tmp/agent.sock", "HTTPS_PROXY": "https://proxy.invalid",
            "HTTP_PROXY": "bad\ncontrol", "ALL_PROXY": "x" * 5000,
            "SECRET_TOKEN": "must-not-cross", "GIT_SSH_COMMAND": "must-not-cross",
        }
        request = self.request("git_remote_explicit", coordinate="https://example.invalid/repository.git")
        with mock.patch.dict(os.environ, inherited, clear=True), mock.patch.object(
            self.observer(), "_run_git", capture,
        ):
            result = self.observer()._observe_request(request, None)
        self.assertEqual(("unknown", "destination_unqueryable"), (result.outcome, result.reason_code))
        environment = captured["environment"]
        self.assertEqual("/tmp/agent.sock", environment["SSH_AUTH_SOCK"])
        self.assertEqual("https://proxy.invalid", environment["HTTPS_PROXY"])
        self.assertEqual("1", environment["GIT_NO_LAZY_FETCH"])
        self.assertNotIn("HTTP_PROXY", environment)
        self.assertNotIn("ALL_PROXY", environment)
        self.assertNotIn("SECRET_TOKEN", environment)
        self.assertNotIn("GIT_SSH_COMMAND", environment)
        self.assertEqual(
            [
                "/usr/bin/git", "-c", "core.hooksPath=/dev/null",
                "--no-lazy-fetch",
                "--no-optional-locks", "--no-replace-objects", "ls-remote",
                "--refs", "https://example.invalid/repository.git", "refs/heads/main",
            ],
            captured["arguments"],
        )

    def test_unavailable_and_none_adapters_return_closed_unknown_results(self) -> None:
        """Catches unavailable or absent adapters claiming confirmation or failure."""
        cases = (
            ("github_explicit", "adapter_unavailable"),
            ("deployment_explicit", "adapter_unavailable"),
            ("none", "reconciliation_inconclusive"),
        )
        for adapter, reason in cases:
            with self.subTest(adapter=adapter):
                result, _ = self.invoke(self.request(adapter))
                self.assertEqual(("unknown", reason), (result.outcome, result.reason_code))
                self.assertIsNone(result.confirmation)
                self.assertEqual("unknown", result.spend_status)
                self.assertIsNone(result.measured_spend)

    @staticmethod
    def _safe_close(descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError:
            pass

    @staticmethod
    def _fstat_errno(descriptor: int) -> Optional[int]:
        try:
            os.fstat(descriptor)
        except OSError as exc:
            return exc.errno
        return None


if __name__ == "__main__":
    unittest.main()

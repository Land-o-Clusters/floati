from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Callable, Optional
from unittest import mock

from tests.test_effect_reconciliation_observer import (
    copy_exact_observer_source_package,
)


class EffectReconciliationExecTests(unittest.TestCase):
    """Real-exec tests for the parent-owned reconciliation observer lifecycle."""

    _MAIN = (
        'if __name__ == "__main__":\n'
        "    raise SystemExit(run_observer(repository_fd=_repository_descriptor_if_open()))\n"
    )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.protocol_path, self.observer_path = copy_exact_observer_source_package(
            self.base / "trusted" / "floati",
        )

    @staticmethod
    def exec_module():
        # Kept lazy so the required RED proves the production module is absent.
        from floati import effect_reconciliation_exec

        return effect_reconciliation_exec

    @staticmethod
    def protocol():
        from floati.effect_reconciliation_protocol import build_request

        return build_request

    def request_none(self, *, request_id: str = "1" * 32):
        return self.protocol()(
            operation_id="effect-op-018f7e9b3c117abc8def0123456789ab",
            current_evidence_id="effect-unknown-018f7e9b3c117abc8def0123456789ab",
            adapter="none",
            target={
                "kind": "shell_environment",
                "coordinate": "workspace",
                "identity_digest": hashlib.sha256(b"workspace").hexdigest(),
            },
            expected_confirmation={
                "kind": "none",
                "locator": "none",
                "expected_digest": "0" * 64,
            },
            budget_claim={},
            local_repository_identity=None,
            request_id=request_id,
        )

    def request_unavailable(self, *, request_id: str = "4" * 32):
        coordinate = "owner/repository#1"
        return self.protocol()(
            operation_id="effect-op-018f7e9b3c117abc8def0123456789ab",
            current_evidence_id="effect-unknown-018f7e9b3c117abc8def0123456789ab",
            adapter="github_explicit",
            target={
                "kind": "github_resource", "coordinate": coordinate,
                "identity_digest": hashlib.sha256(coordinate.encode("utf-8")).hexdigest(),
            },
            expected_confirmation={
                "kind": "github_idempotency_marker", "locator": "marker",
                "expected_digest": "0" * 64,
            },
            budget_claim={},
            local_repository_identity=None,
            request_id=request_id,
        )

    def make_repository(self) -> tuple[Path, str]:
        repository = self.base / "repository"
        repository.mkdir()
        environment = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": "/var/empty",
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": "/usr/bin:/bin",
        }

        def git(*arguments: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["/usr/bin/git", *arguments], cwd=repository, env=environment,
                check=True, capture_output=True, text=True,
            )

        git("init", "--quiet", "--object-format=sha256", "--initial-branch=main")
        (repository / "README.md").write_text("exec fixture\n", encoding="utf-8")
        git("add", "README.md")
        git(
            "-c", "user.name=Slipway Tests",
            "-c", "user.email=tests@slipway.invalid",
            "commit", "--quiet", "-m", "fixture",
        )
        return repository, git("rev-parse", "HEAD").stdout.strip()

    def request_local(
        self, repository: Path, digest: str, *, request_id: str = "2" * 32,
        repository_identity: Optional[tuple[int, int]] = None,
    ):
        metadata = repository.stat()
        identity = (
            (metadata.st_dev, metadata.st_ino)
            if repository_identity is None else repository_identity
        )
        coordinate = str(repository)
        identity_digest = hashlib.sha256(json.dumps(
            {"device": identity[0], "inode": identity[1], "path": coordinate},
            ensure_ascii=False, allow_nan=False, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return self.protocol()(
            operation_id="effect-op-018f7e9b3c117abc8def0123456789ab",
            current_evidence_id="effect-unknown-018f7e9b3c117abc8def0123456789ab",
            adapter="git_local",
            target={
                "kind": "git_ref", "coordinate": coordinate,
                "identity_digest": identity_digest,
            },
            expected_confirmation={
                "kind": "git_ref_equals", "locator": "refs/heads/main",
                "expected_digest": digest,
            },
            budget_claim={"git": 1},
            local_repository_identity=identity,
            request_id=request_id,
        )

    def request_remote(self, repository: Path, digest: str, *, request_id: str = "3" * 32):
        coordinate = str(repository)
        return self.protocol()(
            operation_id="effect-op-018f7e9b3c117abc8def0123456789ab",
            current_evidence_id="effect-unknown-018f7e9b3c117abc8def0123456789ab",
            adapter="git_remote_explicit",
            target={
                "kind": "git_remote_ref", "coordinate": coordinate,
                "identity_digest": hashlib.sha256(coordinate.encode("utf-8")).hexdigest(),
            },
            expected_confirmation={
                "kind": "git_remote_ref_equals", "locator": "refs/heads/main",
                "expected_digest": digest,
            },
            budget_claim={"git": 1},
            local_repository_identity=None,
            request_id=request_id,
        )

    def observe(
        self, request, *, timeout_seconds: float = 2.0,
        spawn: Optional[Callable[..., int]] = None,
    ):
        launcher = self.exec_module()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                launcher, "_source_paths",
                return_value=(self.protocol_path, self.observer_path),
            ))
            if spawn is not None:
                stack.enter_context(mock.patch.object(
                    launcher.os, "posix_spawn", side_effect=spawn,
                ))
            return launcher.observe_effect_reconciliation(
                request, timeout_seconds=timeout_seconds,
            )

    @staticmethod
    def insert_after_future(source: str, injected: str) -> str:
        future = "from __future__ import annotations\n"
        if future not in source:
            raise AssertionError("source fixture lacks future import")
        return source.replace(future, future + injected, 1)

    def replace_main(self, body: str) -> None:
        source = self.observer_path.read_text(encoding="utf-8")
        if self._MAIN not in source:
            raise AssertionError("observer fixture lacks executable main")
        indented = "".join("    " + line if line.strip() else line for line in body.splitlines(True))
        self.observer_path.write_text(
            source.replace(
                self._MAIN,
                'if __name__ == "__main__":\n' + indented,
                1,
            ),
            encoding="utf-8",
        )

    def untrusted_interpreter(self) -> Path:
        candidates = [Path(sys.executable)]
        if sys.platform == "darwin":
            candidates.extend(
                Path(candidate)
                for candidate in (
                    "/opt/homebrew/bin/python3",
                    "/opt/homebrew/opt/python@3.14/bin/python3.14",
                    "/usr/local/bin/python3",
                )
            )
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
                if resolved.stat().st_uid != 0 and os.access(resolved, os.X_OK):
                    return resolved
            except OSError:
                continue
        if sys.platform == "darwin":
            self.skipTest("no runnable non-root-owned Python interpreter")
        directory = self.base / "untrusted"
        directory.mkdir()
        interpreter = directory / "python3"
        shutil.copyfile(sys.executable, interpreter)
        interpreter.chmod(0o700)
        return interpreter

    def run_untrusted_interpreter(
        self, interpreter: Path, *arguments: str,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONNOUSERSITE"] = "1"
        return subprocess.run(
            [str(interpreter), *arguments],
            cwd=Path(__file__).resolve().parents[1],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_untrusted_interpreter_help_is_available(self) -> None:
        interpreter = self.untrusted_interpreter()
        result = self.run_untrusted_interpreter(interpreter, "-m", "floati", "--help")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertIn("NAME\n", result.stdout)
        self.assertIn(
            "floati - inspect and operate an explicit fleet root",
            result.stdout,
        )

    def test_untrusted_interpreter_exec_is_typed(self) -> None:
        interpreter = self.untrusted_interpreter()
        script = "\n".join((
            "from floati.effect_reconciliation_exec import observe_effect_reconciliation",
            "from floati.effect_reconciliation_protocol import build_request",
            "request = build_request(",
            "    operation_id='effect-op-018f7e9b3c117abc8def0123456789ab',",
            "    current_evidence_id='effect-unknown-018f7e9b3c117abc8def0123456789ab',",
            "    adapter='none',",
            "    target={'kind': 'shell_environment', 'coordinate': 'workspace', 'identity_digest': '0' * 64},",
            "    expected_confirmation={'kind': 'none', 'locator': 'none', 'expected_digest': '0' * 64},",
            "    budget_claim={},",
            "    local_repository_identity=None,",
            "    request_id='1' * 32,",
            ")",
            "result = observe_effect_reconciliation(request, timeout_seconds=1.0)",
            "print(result.outcome + ':' + result.reason_code)",
        ))
        result = self.run_untrusted_interpreter(interpreter, "-c", script)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(
            "unknown:effect_reconciliation_interpreter_untrusted\n",
            result.stdout,
        )

    @staticmethod
    def valid_result_body(*, suffix: str = "") -> str:
        return (
            "deadline = time.monotonic() + 2.0\n"
            "header = _read_exact(3, 4, deadline)\n"
            "length = struct.unpack('>I', header)[0]\n"
            "request = decode_request_frame(header + _read_exact(3, length, deadline))\n"
            "result = build_result(request, outcome='unknown', "
            "reason_code='reconciliation_inconclusive', "
            "observation={'adapter': 'none'})\n"
            "frame = encode_frame(result, request=request)\n"
            + suffix
        )

    @staticmethod
    def open_descriptors() -> set[int]:
        directory = "/proc/self/fd" if sys.platform.startswith("linux") else "/dev/fd"
        result = set()
        for name in os.listdir(directory):
            if name.isascii() and name.isdigit():
                descriptor = int(name)
                try:
                    os.fstat(descriptor)
                except OSError:
                    continue
                result.add(descriptor)
        return result

    @staticmethod
    def assert_closed(testcase: unittest.TestCase, descriptor: int) -> None:
        with testcase.assertRaises(OSError) as caught:
            os.fstat(descriptor)
        testcase.assertEqual(errno.EBADF, caught.exception.errno)

    def test_exec_uses_opened_protocol_and_observer_bytes_after_path_replacement(self) -> None:
        """Catches late pathname replacement selecting unhashed project code."""
        marker = self.base / "replacement-ran"
        replacements = self.base / "replacements"
        replacements.mkdir()
        for path in (self.protocol_path, self.observer_path):
            (replacements / path.name).write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text({path.name!r}, encoding='utf-8')\n"
                "raise SystemExit(93)\n",
                encoding="utf-8",
            )
        real_spawn = os.posix_spawn
        captured_actions: list[tuple] = []

        def replace_then_spawn(executable, argv, environment, **kwargs):
            for path in (self.protocol_path, self.observer_path):
                os.replace(replacements / path.name, path)
            captured_actions.extend(kwargs["file_actions"])
            return real_spawn(executable, argv, environment, **kwargs)

        result = self.observe(self.request_none(), spawn=replace_then_spawn)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (result.outcome, result.reason_code),
        )
        self.assertFalse(marker.exists())
        sources = [action[1] for action in captured_actions if action[0] == os.POSIX_SPAWN_DUP2]
        self.assertEqual(len(sources), len(set(sources)))
        self.assertTrue(all(descriptor >= 7 for descriptor in sources))
        closed = {
            action[1] for action in captured_actions
            if action[0] == os.POSIX_SPAWN_CLOSE
        }
        self.assertTrue({3, 4, 5}.isdisjoint(closed))

        launcher = self.exec_module()
        aliases = self.base / "source-aliases"
        aliases.mkdir()
        protocol_alias = aliases / self.protocol_path.name
        observer_alias = aliases / self.observer_path.name
        installed = Path(__file__).parents[1] / "floati"
        protocol_alias.symlink_to(installed / protocol_alias.name)
        observer_alias.symlink_to(installed / observer_alias.name)
        with mock.patch.object(
            launcher.protocol_source, "__file__", str(protocol_alias),
        ), mock.patch.object(
            launcher.observer_source, "__file__", str(observer_alias),
        ), mock.patch.object(
            launcher.os, "posix_spawn",
            side_effect=AssertionError("symlinked source reached spawn"),
        ) as forbidden_spawn:
            refused = launcher.observe_effect_reconciliation(self.request_none())
        self.assertEqual("observer_launch_failed", refused.reason_code)
        forbidden_spawn.assert_not_called()

    def test_exec_trusts_only_frozen_root_owned_canonical_interpreter(self) -> None:
        """Catches mutable or noncanonical interpreter selection reaching spawn."""
        launcher = self.exec_module()
        trusted = launcher._trusted_interpreter()
        expected = Path(os.path.realpath(sys.executable))
        self.assertEqual(expected, trusted.path)
        self.assertTrue(expected.is_absolute())
        self.assertEqual(trusted, launcher._freeze_trusted_interpreter(str(expected)))

        fake = self.base / "fake-python"
        shutil.copyfile(expected, fake)
        fake.chmod(0o700)
        candidates = (None, 1, "python3", str(fake))
        if Path(sys.executable) != expected:
            candidates += (sys.executable,)
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                with self.assertRaises((TypeError, OSError, ValueError)):
                    launcher._freeze_trusted_interpreter(candidate)

        snapshot = launcher._snapshot_interpreter(fake)
        replacement = self.base / "replacement-python"
        shutil.copyfile(expected, replacement)
        replacement.chmod(0o700)
        os.replace(replacement, fake)
        with mock.patch.object(
            launcher, "_root_trusted_interpreter_path", return_value=None,
        ), mock.patch.object(
            launcher, "_TRUSTED_INTERPRETER", snapshot,
        ), mock.patch.object(
            launcher.os, "posix_spawn",
            side_effect=AssertionError("replaced interpreter reached spawn"),
        ) as forbidden_spawn:
            refused = self.observe(self.request_none())
        self.assertEqual("effect_reconciliation_interpreter_untrusted", refused.reason_code)
        forbidden_spawn.assert_not_called()

        during_hash = self.base / "during-hash-python"
        during_hash_replacement = self.base / "during-hash-replacement-python"
        shutil.copyfile(expected, during_hash)
        shutil.copyfile(expected, during_hash_replacement)
        during_hash.chmod(0o700)
        during_hash_replacement.chmod(0o700)
        real_fstat = os.fstat
        fstat_calls = 0

        def replace_after_open(descriptor):
            nonlocal fstat_calls
            metadata = real_fstat(descriptor)
            fstat_calls += 1
            if fstat_calls == 1:
                os.replace(during_hash_replacement, during_hash)
            return metadata

        with mock.patch.object(launcher.os, "fstat", side_effect=replace_after_open):
            with self.assertRaises(OSError):
                launcher._snapshot_interpreter(during_hash)

        captured: dict[str, object] = {}
        real_spawn = os.posix_spawn

        def capture_then_spawn(executable, argv, environment, **kwargs):
            captured["executable"] = executable
            captured["argv0"] = argv[0]
            return real_spawn(executable, argv, environment, **kwargs)

        with mock.patch.object(launcher.sys, "executable", "relative-hostile-python"):
            lawful = self.observe(self.request_none(), spawn=capture_then_spawn)
        self.assertEqual("reconciliation_inconclusive", lawful.reason_code)
        self.assertEqual(str(expected), captured["executable"])
        self.assertEqual(str(expected), captured["argv0"])

    def test_exec_digest_mismatch_exits_before_any_project_code(self) -> None:
        """Catches either held source inode executing after post-hash mutation."""
        for selected_name in (
            "effect_reconciliation_protocol.py", "effect_reconciliation_observer.py",
        ):
            with self.subTest(source=selected_name):
                package = self.base / ("mutate-" + selected_name) / "floati"
                protocol_path, observer_path = copy_exact_observer_source_package(package)
                marker = package.parent / "project-code-ran"
                injected = (
                    "from pathlib import Path as _ExecMarkerPath\n"
                    f"_ExecMarkerPath({str(marker)!r}).write_text(__name__, encoding='utf-8')\n"
                )
                for path in (protocol_path, observer_path):
                    path.write_text(self.insert_after_future(
                        path.read_text(encoding="utf-8"), injected,
                    ), encoding="utf-8")
                selected = package / selected_name
                real_spawn = os.posix_spawn

                def mutate_then_spawn(executable, argv, environment, **kwargs):
                    descriptor = os.open(selected, os.O_WRONLY | os.O_TRUNC)
                    try:
                        os.write(descriptor, b"raise SystemExit(94)\n")
                    finally:
                        os.close(descriptor)
                    return real_spawn(executable, argv, environment, **kwargs)

                old_paths = self.protocol_path, self.observer_path
                self.protocol_path, self.observer_path = protocol_path, observer_path
                try:
                    result = self.observe(self.request_none(), spawn=mutate_then_spawn)
                finally:
                    self.protocol_path, self.observer_path = old_paths
                self.assertEqual("unknown", result.outcome)
                self.assertEqual("observer_child_nonzero", result.reason_code)
                self.assertEqual(126, result.observation["exit_code"])
                self.assertFalse(marker.exists())

    def test_exec_loader_uses_no_path_import_or_meta_path_finder(self) -> None:
        """Catches descriptor-loaded sources being resolved by an import finder."""
        target_proof = self.base / "target-finder-proof"
        control_proof = self.base / "control-finder-proof"
        control = self.base / "exec_positive_control.py"
        control.write_text("VALUE = 1\n", encoding="utf-8")
        impostors = self.base / "impostors"
        impostors.mkdir()
        for source in (self.protocol_path, self.observer_path):
            shutil.copyfile(source, impostors / source.name)
        injected = (
            "import importlib.machinery as _exec_machinery, sys as _exec_sys\n"
            "class _ExecFinder:\n"
            " def find_spec(self, fullname, path=None, target=None):\n"
            "  mapping = {\n"
            f"   'floati.effect_reconciliation_protocol': {str(impostors / self.protocol_path.name)!r},\n"
            f"   'floati.effect_reconciliation_observer': {str(impostors / self.observer_path.name)!r},\n"
            "  }\n"
            "  if fullname in mapping:\n"
            f"   open({str(target_proof)!r}, 'a').write(fullname + '\\n')\n"
            "   return _exec_machinery.ModuleSpec(fullname, _exec_machinery.SourceFileLoader(fullname, mapping[fullname]))\n"
            "  if fullname == 'exec_positive_control':\n"
            f"   open({str(control_proof)!r}, 'a').write(fullname + '\\n')\n"
            f"   return _exec_machinery.ModuleSpec(fullname, _exec_machinery.SourceFileLoader(fullname, {str(control)!r}))\n"
            "  return None\n"
            "_exec_sys.meta_path.insert(0, _ExecFinder())\n"
            "import exec_positive_control\n"
        )
        self.observer_path.write_text(self.insert_after_future(
            self.observer_path.read_text(encoding="utf-8"), injected,
        ), encoding="utf-8")

        result = self.observe(self.request_none())
        self.assertEqual("reconciliation_inconclusive", result.reason_code)
        self.assertTrue(control_proof.is_file())
        self.assertFalse(target_proof.exists())

    def test_exec_relocates_all_sources_before_collision_free_spawn_actions(self) -> None:
        """Catches fd 3-6 allocation cycles corrupting later spawn actions."""
        launcher = self.exec_module()
        descriptors = [os.open(os.devnull, os.O_RDONLY) for _ in range(4)]
        relocated = launcher._relocate_launch_descriptors(descriptors)
        try:
            self.assertEqual(4, len(relocated))
            self.assertEqual(4, len(set(relocated)))
            self.assertTrue(all(descriptor >= 7 for descriptor in relocated))
            for descriptor in descriptors:
                self.assert_closed(self, descriptor)
        finally:
            for descriptor in relocated:
                os.close(descriptor)

        with mock.patch.object(
            launcher.fcntl, "fcntl", side_effect=[7, 8, 9, 10],
        ) as duplicate, mock.patch.object(launcher.os, "close") as close:
            self.assertEqual(
                [7, 8, 9, 10],
                launcher._relocate_launch_descriptors([3, 6, 4, 5]),
            )
        self.assertEqual(
            [mock.call(value, fcntl.F_DUPFD_CLOEXEC, 7) for value in (3, 6, 4, 5)],
            duplicate.call_args_list,
        )
        self.assertEqual([mock.call(value) for value in (3, 6, 4, 5)], close.call_args_list)

    def test_exec_local_repository_is_the_only_optional_child_descriptor(self) -> None:
        """Catches fd 6 crossing exec for any adapter except git_local."""
        real_spawn = os.posix_spawn
        target_sets: list[set[int]] = []

        def capture_then_spawn(executable, argv, environment, **kwargs):
            target_sets.append({
                action[2] for action in kwargs["file_actions"]
                if action[0] == os.POSIX_SPAWN_DUP2
            })
            return real_spawn(executable, argv, environment, **kwargs)

        unavailable = self.observe(self.request_none(), spawn=capture_then_spawn)
        self.assertEqual("reconciliation_inconclusive", unavailable.reason_code)
        repository, digest = self.make_repository()
        observed = self.observe(
            self.request_local(repository, digest), spawn=capture_then_spawn,
        )
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (observed.outcome, observed.reason_code),
        )
        self.assertEqual([{3, 4, 5}, {3, 4, 5, 6}], target_sets)

    def test_exec_local_repository_refuses_symlinked_ancestor_and_accepts_canonical_path(self) -> None:
        """Catches ancestor symlinks bypassing final-component O_NOFOLLOW."""
        repository, digest = self.make_repository()
        canonical = self.observe(self.request_local(repository, digest))
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (canonical.outcome, canonical.reason_code),
        )

        ancestor_alias = self.base / "repository-parent-alias"
        ancestor_alias.symlink_to(repository.parent, target_is_directory=True)
        aliased_repository = ancestor_alias / repository.name
        refused = self.observe(self.request_local(aliased_repository, digest))
        self.assertEqual(
            ("unknown", "repository_identity_changed"),
            (refused.outcome, refused.reason_code),
        )
        self.assertIsNone(refused.confirmation)

    def test_exec_repository_identity_mismatch_closes_held_directory_once(self) -> None:
        """Catches a second close consuming an unrelated reused descriptor number."""
        launcher = self.exec_module()
        repository, digest = self.make_repository()
        metadata = repository.stat()
        request = self.request_local(
            repository, digest,
            repository_identity=(metadata.st_dev, metadata.st_ino + 1),
        )
        real_close = os.close
        reused: list[int] = []

        def close_and_reuse_repository_fd(descriptor: int) -> None:
            current = os.fstat(descriptor)
            real_close(descriptor)
            if not reused and (current.st_dev, current.st_ino) == (
                metadata.st_dev, metadata.st_ino,
            ):
                replacement = os.open(os.devnull, os.O_RDONLY)
                if replacement != descriptor:
                    os.dup2(replacement, descriptor)
                    real_close(replacement)
                reused.append(descriptor)

        try:
            with mock.patch.object(
                launcher.os, "close", side_effect=close_and_reuse_repository_fd,
            ):
                self.assertIsNone(launcher._open_repository(request))
            self.assertEqual(1, len(reused))
            os.fstat(reused[0])
        finally:
            for descriptor in reused:
                try:
                    real_close(descriptor)
                except OSError:
                    pass

    def test_exec_closes_every_parent_and_child_descriptor_on_every_path(self) -> None:
        """Catches launch/result failures leaking sources, channel ends, or children."""
        scenarios = ("success", "malformed", "timeout", "launch_failure")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                package = self.base / ("cleanup-" + scenario) / "floati"
                old_paths = self.protocol_path, self.observer_path
                self.protocol_path, self.observer_path = copy_exact_observer_source_package(package)
                if scenario == "malformed":
                    self.replace_main(
                        "deadline = time.monotonic() + 2.0\n"
                        "header = _read_exact(3, 4, deadline)\n"
                        "length = struct.unpack('>I', header)[0]\n"
                        "_read_exact(3, length, deadline)\n"
                        "os.write(3, b'\\x00\\x00')\n"
                        "os.close(3)\n"
                        "raise SystemExit(0)\n"
                    )
                elif scenario == "timeout":
                    self.replace_main("time.sleep(2.0)\n")
                before = self.open_descriptors()
                captured_sources: list[int] = []
                captured_pids: list[int] = []
                real_spawn = os.posix_spawn

                def capture_then_spawn(executable, argv, environment, **kwargs):
                    captured_sources.extend(
                        action[1] for action in kwargs["file_actions"]
                        if action[0] == os.POSIX_SPAWN_DUP2
                    )
                    if scenario == "launch_failure":
                        raise OSError("synthetic spawn refusal")
                    pid = real_spawn(executable, argv, environment, **kwargs)
                    captured_pids.append(pid)
                    return pid

                try:
                    result = self.observe(
                        self.request_none(), timeout_seconds=0.08,
                        spawn=capture_then_spawn,
                    )
                finally:
                    self.protocol_path, self.observer_path = old_paths
                self.assertEqual("unknown", result.outcome)
                for descriptor in captured_sources:
                    self.assert_closed(self, descriptor)
                for pid in captured_pids:
                    with self.assertRaises(ChildProcessError):
                        os.waitpid(pid, os.WNOHANG)
                self.assertEqual(before, self.open_descriptors())

    def test_exec_cleanup_failures_are_explicit_and_exact_children_are_reaped(self) -> None:
        """Catches close, decode, interaction, or reap failure bypassing cleanup."""
        launcher = self.exec_module()
        cases = ("close", "decode", "interaction", "reap", "launch_unwind")
        for case in cases:
            with self.subTest(case=case):
                package = self.base / ("cleanup-failure-" + case) / "floati"
                old_paths = self.protocol_path, self.observer_path
                self.protocol_path, self.observer_path = copy_exact_observer_source_package(package)
                if case == "reap":
                    self.replace_main("time.sleep(2.0)\n")
                captured_pids: list[int] = []
                real_spawn = os.posix_spawn

                def capture_then_spawn(executable, argv, environment, **kwargs):
                    pid = real_spawn(executable, argv, environment, **kwargs)
                    captured_pids.append(pid)
                    return pid

                contexts = []
                if case == "close":
                    real_close = launcher.ObserverChannel.close

                    def close_then_fail(channel):
                        real_close(channel)
                        raise OSError("synthetic channel close failure")

                    contexts.append(mock.patch.object(
                        launcher.ObserverChannel, "close", close_then_fail,
                    ))
                elif case == "decode":
                    contexts.append(mock.patch.object(
                        launcher, "decode_result_frame",
                        side_effect=RuntimeError("synthetic decoder failure"),
                    ))
                elif case == "interaction":
                    contexts.append(mock.patch.object(
                        launcher.ObserverChannel, "send_request",
                        side_effect=RuntimeError("synthetic interaction failure"),
                    ))
                elif case == "reap":
                    real_reap = launcher.SpawnedReconciliationObserver.terminate_and_reap

                    def reap_then_report_failure(process):
                        self.assertTrue(real_reap(process))
                        return False

                    contexts.append(mock.patch.object(
                        launcher.SpawnedReconciliationObserver,
                        "terminate_and_reap", reap_then_report_failure,
                    ))
                else:
                    real_close_descriptors = launcher._close_descriptors
                    close_calls = 0

                    def close_launch_sources_then_fail(descriptors):
                        nonlocal close_calls
                        close_calls += 1
                        real_close_descriptors(descriptors)
                        if close_calls == 2:
                            raise OSError("synthetic parent launch-source close failure")

                    real_reap = launcher.SpawnedReconciliationObserver.terminate_and_reap

                    def reap_launch_then_report_failure(process):
                        self.assertTrue(real_reap(process))
                        return False

                    contexts.extend((
                        mock.patch.object(
                            launcher, "_close_descriptors",
                            close_launch_sources_then_fail,
                        ),
                        mock.patch.object(
                            launcher.SpawnedReconciliationObserver,
                            "terminate_and_reap", reap_launch_then_report_failure,
                        ),
                    ))
                try:
                    with ExitStack() as stack:
                        for context in contexts:
                            stack.enter_context(context)
                        result = self.observe(
                            self.request_none(),
                            timeout_seconds=0.08 if case == "reap" else 0.6,
                            spawn=capture_then_spawn,
                        )
                finally:
                    self.protocol_path, self.observer_path = old_paths
                self.assertEqual("unknown", result.outcome)
                self.assertEqual("observer_cleanup_failed", result.reason_code)
                self.assertEqual(1, len(captured_pids))
                with self.assertRaises(ChildProcessError):
                    os.waitpid(captured_pids[0], os.WNOHANG)

    def test_exec_constructor_failure_retains_raw_pid_until_definitive_reap(self) -> None:
        """Catches wrapper construction losing ownership of a live spawned PID."""
        launcher = self.exec_module()
        captured_pids: list[int] = []
        real_spawn = os.posix_spawn

        def capture_then_spawn(executable, argv, environment, **kwargs):
            pid = real_spawn(executable, argv, environment, **kwargs)
            captured_pids.append(pid)
            return pid

        try:
            with mock.patch.object(
                launcher, "SpawnedReconciliationObserver",
                side_effect=MemoryError("synthetic wrapper construction failure"),
            ):
                result = self.observe(
                    self.request_none(), timeout_seconds=0.6,
                    spawn=capture_then_spawn,
                )
            self.assertEqual("observer_launch_failed", result.reason_code)
            self.assertEqual(1, len(captured_pids))
            with self.assertRaises(ChildProcessError):
                os.waitpid(captured_pids[0], os.WNOHANG)
        finally:
            for pid in captured_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except ChildProcessError:
                    pass

    def test_exec_cleanup_failure_precedes_and_chains_pending_base_exception(self) -> None:
        """Catches a pending BaseException masking close or reap cleanup failure."""
        launcher = self.exec_module()

        class InteractionBase(BaseException):
            pass

        class CloseBase(BaseException):
            pass

        interaction = InteractionBase("synthetic interaction base exception")
        real_close = launcher.ObserverChannel.close
        real_wait = launcher.SpawnedReconciliationObserver.wait_until
        captured_pids: list[int] = []
        captured_cleanup_errors: list[BaseException] = []
        interaction_calls: list[bool] = []
        real_spawn = os.posix_spawn
        real_build_cleanup_failure = launcher._build_cleanup_failure

        def capture_then_spawn(executable, argv, environment, **kwargs):
            pid = real_spawn(executable, argv, environment, **kwargs)
            captured_pids.append(pid)
            return pid

        def close_then_raise(channel):
            real_close(channel)
            raise CloseBase("synthetic close base exception")

        def raise_interaction(*_args):
            interaction_calls.append(True)
            raise interaction

        def reap_then_report_failure(process):
            process._signal(signal.SIGTERM)
            if not real_wait(process, time.monotonic() + 0.1):
                process._signal(signal.SIGKILL)
                self.assertTrue(real_wait(process, time.monotonic() + 1.0))
            return False

        def capture_cleanup_result(request, cause):
            result, cleanup_error = real_build_cleanup_failure(request, cause)
            captured_cleanup_errors.append(cleanup_error)
            return result, cleanup_error

        with mock.patch.object(
            launcher.ObserverChannel, "send_request", side_effect=raise_interaction,
        ), mock.patch.object(
            launcher.ObserverChannel, "close", close_then_raise,
        ), mock.patch.object(
            launcher.SpawnedReconciliationObserver, "wait_until", return_value=False,
        ), mock.patch.object(
            launcher.SpawnedReconciliationObserver,
            "terminate_and_reap", reap_then_report_failure,
        ), mock.patch.object(
            launcher, "_build_cleanup_failure", side_effect=capture_cleanup_result,
        ), mock.patch.object(
            self.exec_module().os, "posix_spawn", side_effect=capture_then_spawn,
        ):
            result = self.observe(self.request_none(), timeout_seconds=2.0)

        self.assertEqual(
            ("unknown", "observer_cleanup_failed"),
            (result.outcome, result.reason_code),
        )
        self.assertEqual([True], interaction_calls)
        self.assertEqual(1, len(captured_cleanup_errors))
        self.assertIs(interaction, captured_cleanup_errors[0].__cause__)
        self.assertEqual(1, len(captured_pids))
        with self.assertRaises(ChildProcessError):
            os.waitpid(captured_pids[0], os.WNOHANG)

    def test_exec_unrelated_descriptors_and_ambient_environment_do_not_cross_exec(self) -> None:
        """Catches ambient descriptors, roots, secrets, or ordinary values reaching child code."""
        original = os.open(self.base / "hostile", os.O_WRONLY | os.O_CREAT, 0o600)
        hostile = fcntl.fcntl(original, fcntl.F_DUPFD_CLOEXEC, 20)
        os.close(original)
        self.addCleanup(self._safe_close, hostile)
        os.set_inheritable(hostile, True)
        descriptor_proof = self.base / "inherited-descriptor-proof"
        environment_proof = self.base / "inherited-environment-proof"
        git_environment_proof = self.base / "git-environment-proof.json"
        hostile_environment_names = (
            "SLIPWAY_ROOT", "TENANT_ROOT", "SECRET_TOKEN", "ORDINARY_EXEC_VALUE",
        )
        injected = (
            "import os as _exec_os, pathlib as _exec_pathlib\n"
            "try:\n"
            f" _exec_os.fstat({hostile})\n"
            "except OSError:\n"
            " pass\n"
            "else:\n"
            f" _exec_pathlib.Path({str(descriptor_proof)!r}).write_text('inherited', encoding='utf-8')\n"
            f"if any(_name in _exec_os.environ for _name in {hostile_environment_names!r}):\n"
            f" _exec_pathlib.Path({str(environment_proof)!r}).write_text('inherited', encoding='utf-8')\n"
        )
        self.observer_path.write_text(self.insert_after_future(
            self.observer_path.read_text(encoding="utf-8"), injected,
        ), encoding="utf-8")
        self.replace_main(
            "original_run_git = _run_git\n"
            "def capture_run_git(arguments, environment):\n"
            f"    with open({str(git_environment_proof)!r}, 'w', encoding='utf-8') as stream:\n"
            "        json.dump(environment, stream, sort_keys=True)\n"
            "    return original_run_git(arguments, environment)\n"
            "_run_git = capture_run_git\n"
            "raise SystemExit(run_observer(repository_fd=_repository_descriptor_if_open()))\n"
        )

        captured_environments: list[dict[str, str]] = []
        real_spawn = os.posix_spawn

        def capture_then_spawn(executable, argv, environment, **kwargs):
            captured_environments.append(dict(environment))
            return real_spawn(executable, argv, environment, **kwargs)

        safe_remote_environment = {
            "SSH_AUTH_SOCK": "\x2fprivate/tmp/slipway-agent.sock",
            "SSL_CERT_FILE": "\x2fprivate/tmp/slipway-ca.pem",
            "SSL_CERT_DIR": "\x2fprivate/tmp/slipway-certs",
            "NO_PROXY": "localhost,127.0.0.1",
            "HTTPS_PROXY": "https://proxy.example.invalid",
            "HTTP_PROXY": "http://proxy.example.invalid",
            "ALL_PROXY": "socks5://proxy.example.invalid",
        }
        hostile_environment = {
            "PYTHONPATH": "/hostile/python",
            "PYTHONHOME": "/hostile/home",
            "PYTHONINSPECT": "1",
            "_PYTHON_HOST_PLATFORM": "hostile",
            "DYLD_INSERT_LIBRARIES": "/hostile/dylib",
            "LD_PRELOAD": "/hostile/preload",
            "LIBPATH": "/hostile/libpath",
            "SHLIB_PATH": "/hostile/shlib",
            "__PYVENV_LAUNCHER__": "/hostile/python",
            "SLIPWAY_ROOT": "/hostile/root",
            "TENANT_ROOT": "/hostile/tenant",
            "SECRET_TOKEN": "must-not-cross",
            "ORDINARY_EXEC_VALUE": "must-not-cross",
        }
        inherited = {**safe_remote_environment, **hostile_environment}
        with mock.patch.dict(os.environ, inherited, clear=True):
            result = self.observe(self.request_none(), spawn=capture_then_spawn)
        self.assertEqual("reconciliation_inconclusive", result.reason_code)
        self.assertFalse(descriptor_proof.exists())
        self.assertFalse(environment_proof.exists())
        self.assertEqual({}, captured_environments[0])

        repository, digest = self.make_repository()
        with mock.patch.dict(os.environ, inherited, clear=True):
            remote = self.observe(
                self.request_remote(repository, digest), spawn=capture_then_spawn,
            )
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (remote.outcome, remote.reason_code),
        )
        self.assertFalse(environment_proof.exists())
        self.assertEqual(safe_remote_environment, captured_environments[1])
        git_environment = json.loads(git_environment_proof.read_text(encoding="utf-8"))
        for name, value in safe_remote_environment.items():
            self.assertEqual(value, git_environment[name])
        for name in hostile_environment:
            self.assertNotIn(name, git_environment)

    def test_exec_accepts_one_result_bound_to_request_channel_pid_exit_and_eof(self) -> None:
        """Catches acceptance without lawful result, exact PID exit, and EOF."""
        repository, digest = self.make_repository()
        request = self.request_local(repository, digest)
        result = self.observe(request)
        self.assertEqual(
            ("unknown", "reconciliation_inconclusive"),
            (result.outcome, result.reason_code),
        )
        self.assertEqual(
            request.expected_confirmation["expected_digest"],
            result.observation["observed_ref_digest"],
        )
        self.assertIsNone(result.confirmation)
        self.assertIsNone(result.measured_spend)

        unavailable = self.observe(self.request_unavailable())
        self.assertEqual(
            ("unknown", "adapter_unavailable"),
            (unavailable.outcome, unavailable.reason_code),
        )

    def test_exec_partial_duplicate_trailing_and_no_eof_results_refuse(self) -> None:
        """Catches any response other than one canonical frame followed by EOF."""
        modes = {
            "partial_header": (
                "os.write(3, b'\\x00\\x00')\nos.close(3)\nraise SystemExit(0)\n",
                "observer_protocol_invalid",
            ),
            "partial_body": (
                "os.write(3, struct.pack('>I', 8) + b'{}')\nos.close(3)\nraise SystemExit(0)\n",
                "observer_protocol_invalid",
            ),
            "oversized": (
                "os.write(3, struct.pack('>I', 65537))\nos.close(3)\nraise SystemExit(0)\n",
                "observer_protocol_invalid",
            ),
            "noncanonical": (
                "os.write(3, struct.pack('>I', 3) + b'{ }')\nos.close(3)\nraise SystemExit(0)\n",
                "observer_protocol_invalid",
            ),
            "eof_without_result": (
                "os.close(3)\nraise SystemExit(0)\n",
                "observer_result_missing",
            ),
        }
        for mode, (suffix, reason) in modes.items():
            with self.subTest(mode=mode):
                package = self.base / ("hostile-" + mode) / "floati"
                old_paths = self.protocol_path, self.observer_path
                self.protocol_path, self.observer_path = copy_exact_observer_source_package(package)
                if mode == "eof_without_result":
                    body = (
                        "deadline = time.monotonic() + 2.0\n"
                        "header = _read_exact(3, 4, deadline)\n"
                        "length = struct.unpack('>I', header)[0]\n"
                        "_read_exact(3, length, deadline)\n" + suffix
                    )
                else:
                    body = self.valid_result_body(suffix=suffix)
                self.replace_main(body)
                try:
                    result = self.observe(self.request_none(), timeout_seconds=0.3)
                finally:
                    self.protocol_path, self.observer_path = old_paths
                self.assertEqual(("unknown", reason), (result.outcome, result.reason_code))

        for mode, suffix, reason in (
            ("duplicate", "os.write(3, frame + frame)\nos.close(3)\nraise SystemExit(0)\n", "observer_protocol_invalid"),
            ("trailing", "os.write(3, frame + b'x')\nos.close(3)\nraise SystemExit(0)\n", "observer_protocol_invalid"),
            ("no_eof", "os.write(3, frame)\ntime.sleep(2.0)\n", "observer_eof_missing"),
            (
                "wrong_request_id",
                "import dataclasses as _d, json as _j\n"
                "payload = _d.asdict(result)\npayload['request_id'] = 'f' * 32\n"
                "encoded = _j.dumps(payload, sort_keys=True, separators=(',', ':')).encode()\n"
                "os.write(3, struct.pack('>I', len(encoded)) + encoded)\nos.close(3)\nraise SystemExit(0)\n",
                "observer_result_binding_invalid",
            ),
            (
                "wrong_request_digest",
                "import dataclasses as _d, json as _j\n"
                "payload = _d.asdict(result)\npayload['request_digest'] = 'f' * 64\n"
                "encoded = _j.dumps(payload, sort_keys=True, separators=(',', ':')).encode()\n"
                "os.write(3, struct.pack('>I', len(encoded)) + encoded)\nos.close(3)\nraise SystemExit(0)\n",
                "observer_result_binding_invalid",
            ),
            (
                "wrong_evidence_digest",
                "import dataclasses as _d, json as _j\n"
                "payload = _d.asdict(result)\npayload['evidence_digest'] = 'f' * 64\n"
                "encoded = _j.dumps(payload, sort_keys=True, separators=(',', ':')).encode()\n"
                "os.write(3, struct.pack('>I', len(encoded)) + encoded)\nos.close(3)\nraise SystemExit(0)\n",
                "observer_result_binding_invalid",
            ),
        ):
            with self.subTest(mode=mode):
                package = self.base / ("hostile-" + mode) / "floati"
                old_paths = self.protocol_path, self.observer_path
                self.protocol_path, self.observer_path = copy_exact_observer_source_package(package)
                self.replace_main(self.valid_result_body(suffix=suffix))
                try:
                    result = self.observe(self.request_none(), timeout_seconds=0.6)
                finally:
                    self.protocol_path, self.observer_path = old_paths
                self.assertEqual(("unknown", reason), (result.outcome, result.reason_code))

    def test_exec_timeout_signal_and_nonzero_exit_never_return_confirmed(self) -> None:
        """Catches timeout, signal, or nonzero exit laundering child testimony."""
        cases = (
            ("timeout", "time.sleep(2.0)\n", "observer_timeout"),
            ("signal", "os.kill(os.getpid(), signal.SIGTERM)\n", "observer_child_died"),
            ("nonzero", "raise SystemExit(17)\n", "observer_child_nonzero"),
        )
        for mode, body, reason in cases:
            with self.subTest(mode=mode):
                package = self.base / ("lifecycle-" + mode) / "floati"
                old_paths = self.protocol_path, self.observer_path
                self.protocol_path, self.observer_path = copy_exact_observer_source_package(package)
                self.replace_main(body)
                try:
                    timeout_seconds = 0.1 if mode == "timeout" else 0.6
                    result = self.observe(
                        self.request_none(), timeout_seconds=timeout_seconds,
                    )
                finally:
                    self.protocol_path, self.observer_path = old_paths
                self.assertEqual("unknown", result.outcome)
                self.assertEqual(reason, result.reason_code)
                self.assertIsNone(result.confirmation)

    def test_exec_timeout_reaps_observer_and_kills_term_ignoring_descendant_group(self) -> None:
        """Catches a separately grouped Git descendant surviving parent timeout cleanup."""
        repository, digest = self.make_repository()
        descendant_pid = self.base / "term-ignoring-descendant.pid"
        helper = self.base / "hostile-git"
        helper.write_text(
            "#!/bin/sh\n"
            "trap '' TERM\n"
            f"echo $$ > {str(descendant_pid)!r}\n"
            "while :; do /bin/sleep 1; done\n",
            encoding="utf-8",
        )
        helper.chmod(0o755)
        source = self.observer_path.read_text(encoding="utf-8")
        fixed_git = '_GIT = "/usr/bin/git"'
        self.assertIn(fixed_git, source)
        self.observer_path.write_text(
            source.replace(fixed_git, "_GIT = " + repr(str(helper)), 1),
            encoding="utf-8",
        )
        captured_observers: list[int] = []
        captured_spawn_kwargs: list[dict[str, object]] = []
        real_spawn = os.posix_spawn

        def capture_then_spawn(executable, argv, environment, **kwargs):
            pid = real_spawn(executable, argv, environment, **kwargs)
            captured_observers.append(pid)
            captured_spawn_kwargs.append(dict(kwargs))
            return pid

        descendant: Optional[int] = None
        try:
            result = self.observe(
                self.request_local(repository, digest),
                timeout_seconds=0.8,
                spawn=capture_then_spawn,
            )
            deadline = time.monotonic() + 2.0
            while not descendant_pid.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(descendant_pid.exists(), "hostile descendant did not launch")
            descendant = int(descendant_pid.read_text(encoding="ascii").strip())
            while time.monotonic() < deadline:
                try:
                    os.kill(descendant, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail("SIGTERM-ignoring observer descendant survived cleanup")
            self.assertEqual(
                ("unknown", "observer_timeout"),
                (result.outcome, result.reason_code),
            )
            self.assertEqual(1, len(captured_observers))
            self.assertTrue(captured_spawn_kwargs[0].get("setsid"))
            with self.assertRaises(ChildProcessError):
                os.waitpid(captured_observers[0], os.WNOHANG)
        finally:
            if descendant is not None:
                try:
                    process_group = os.getpgid(descendant)
                except ProcessLookupError:
                    process_group = None
                if process_group is not None and process_group != os.getpgrp():
                    try:
                        os.killpg(process_group, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_exec_foreign_process_group_identity_is_never_signaled(self) -> None:
        """Catches cleanup signaling a reused or foreign process-group number."""
        launcher = self.exec_module()
        pid = os.posix_spawn(
            sys.executable,
            [
                sys.executable, "-I", "-S", "-B", "-c",
                "import time; time.sleep(20)",
            ],
            {},
            setsid=True,
        )
        process = launcher.SpawnedReconciliationObserver(pid)
        with mock.patch.object(
            launcher, "_owned_observer_process_group", return_value=None,
        ), mock.patch.object(
            launcher.os, "killpg",
            side_effect=AssertionError("foreign process group was signaled"),
        ) as kill_group:
            self.assertFalse(process.terminate_and_reap())
        kill_group.assert_not_called()
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

    def test_exec_result_binding_cannot_be_substituted_between_children(self) -> None:
        """Catches a returned PID naming a different child than the channel peer."""
        real_spawn = os.posix_spawn
        actual_children: list[int] = []

        def substitute_pid(executable, argv, environment, **kwargs):
            actual = real_spawn(executable, argv, environment, **kwargs)
            actual_children.append(actual)
            substitute = real_spawn(
                sys.executable,
                [sys.executable, "-I", "-S", "-B", "-c", "raise SystemExit(0)"],
                {},
                setsid=True,
            )
            return substitute

        result = self.observe(
            self.request_none(), timeout_seconds=0.6, spawn=substitute_pid,
        )
        self.assertEqual(
            ("unknown", "observer_channel_invalid"),
            (result.outcome, result.reason_code),
        )
        for pid in actual_children:
            waited, _status = os.waitpid(pid, 0)
            self.assertEqual(pid, waited)

    @staticmethod
    def _safe_close(descriptor: int) -> None:
        try:
            os.close(descriptor)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()

"""Real-host tests for the closed Worker filesystem-isolation backends."""

from __future__ import annotations

import dataclasses
import ctypes
import errno
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Callable, Optional
from unittest.mock import patch

from floati import worker_isolation
from floati.worker_isolation import (
    WorkerIsolationPolicy,
    apply_worker_isolation,
    cleanup_worker_isolation,
    prepare_worker_isolation,
)
from floati.worker_errors import WorkerAdapterFailure


_DENIED = {errno.EACCES, errno.EPERM}
_DENIED_LINK_OR_RENAME = _DENIED | {errno.EXDEV}


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _identity(path: Path) -> tuple[int, int]:
    metadata = os.lstat(path)
    return metadata.st_dev, metadata.st_ino


def _try_write(path: Path, *, create: bool = False, truncate: bool = False) -> Optional[int]:
    flags = os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    if truncate:
        flags |= os.O_TRUNC
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        return exc.errno
    try:
        os.write(descriptor, b"changed")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return None


def _try_open_read_write(path: Path) -> Optional[int]:
    try:
        descriptor = os.open(path, os.O_RDWR)
    except OSError as exc:
        return exc.errno
    os.close(descriptor)
    return None


def _foreign_sticky_temp_root() -> Optional[Path]:
    """A real canonical sticky directory this uid does not own -- Linux \x2ftmp's shape.

    Every Linux host ships \x2ftmp as root:root 1777, which is the whole subject of the
    cleanup predicate.  macOS spells the same directory \x2fprivate/tmp and makes \x2ftmp a
    symlink to it, which ``_canonical_path`` refuses, so the symlink is skipped here.
    """
    for candidate in (Path("\x2ftmp"), Path("\x2fprivate/tmp")):
        try:
            metadata = os.lstat(candidate)
        except OSError:
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        if not metadata.st_mode & stat.S_ISVTX:
            continue
        if metadata.st_uid == os.getuid():
            continue
        try:
            if candidate.resolve(strict=True) != candidate:
                continue
        except OSError:
            continue
        if not os.access(candidate, os.W_OK | os.X_OK):
            continue
        return candidate
    return None


def _lstat_reporting(
    target: Path, *, uid: int, sticky: bool,
) -> Callable[..., os.stat_result]:
    """Report one directory with a chosen owner and sticky bit; every other path is real.

    This fixture can prove which predicate the guard applies to the scratch's parent,
    and that st_dev/st_ino survive untouched so the identity clause still sees the real
    directory.  It cannot prove that a genuinely root-owned directory behaves the same
    way -- only ``test_cleanup_succeeds_under_a_sticky_shared_temp_root_it_does_not_own``
    does that, and only on a host that has one.
    """
    real_lstat = os.lstat
    key = os.fspath(target)

    def reporting(path: object, *args: object, **kwargs: object) -> os.stat_result:
        metadata = real_lstat(path, *args, **kwargs)
        try:
            same = os.fspath(path) == key
        except TypeError:
            same = False
        if not same:
            return metadata
        fields = list(metadata)
        fields[0] = stat.S_IFDIR | 0o777 | (stat.S_ISVTX if sticky else 0)
        fields[4] = uid
        return os.stat_result(tuple(fields))

    return reporting


def _child_result(
    policy: WorkerIsolationPolicy,
    payload: Callable[[], object],
    *,
    platform_override: Optional[str] = None,
) -> dict[str, object]:
    """Apply the irreversible policy in a real fork and return one JSON result."""

    read_descriptor, write_descriptor = os.pipe()
    process_id = os.fork()
    if process_id == 0:
        os.close(read_descriptor)
        # sandbox_init is irreversible and platform code can fail in ways that
        # prevent evidence delivery.  Bound every real-host child itself so a
        # failed backend can never strand the unittest parent.
        signal.signal(signal.SIGALRM, lambda _signum, _frame: os._exit(124))
        signal.alarm(10)
        result: dict[str, object] = {"payload_callbacks": 0}
        try:
            if platform_override is not None:
                sys.platform = platform_override
            try:
                backend = apply_worker_isolation(policy)
            except WorkerAdapterFailure as exc:
                result.update(status="refused", code=exc.code)
            else:
                result.update(status="ready", backend=backend, payload_callbacks=1)
                result["payload"] = payload()
        except BaseException as exc:  # pragma: no cover - failure evidence from child
            result.update(
                status="child_error",
                exception_type=type(exc).__name__,
                exception_text=str(exc),
            )
        data = json.dumps(result, sort_keys=True).encode("utf-8")
        try:
            os.write(write_descriptor, data)
        finally:
            os.close(write_descriptor)
        signal.alarm(0)
        os._exit(0)

    os.close(write_descriptor)
    chunks = []
    while True:
        chunk = os.read(read_descriptor, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    os.close(read_descriptor)
    waited_id, status = os.waitpid(process_id, 0)
    if waited_id != process_id or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise AssertionError(f"isolation child exited abnormally: {status}")
    if not chunks:
        raise AssertionError("isolation child returned no evidence")
    return json.loads(b"".join(chunks).decode("utf-8"))


class WorkerIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.tenant_home = self.root / "tenant"
        self.tenant_home.mkdir(mode=0o700)
        (self.tenant_home / "effects").mkdir(mode=0o700)
        self.workspaces = self.root / "workspaces"
        self.workspaces.mkdir(mode=0o700)
        self.policies: list[WorkerIsolationPolicy] = []
        self.addCleanup(self._cleanup_policies)

    def _cleanup_policies(self) -> None:
        for policy in reversed(self.policies):
            try:
                cleanup_worker_isolation(policy)
            except WorkerAdapterFailure:
                pass

    def prepare(self, suffix: str = "a", *, workspace: bool = True) -> WorkerIsolationPolicy:
        workspace_path = self.workspaces / suffix if workspace else None
        policy = prepare_worker_isolation(self.tenant_home, workspace_path, f"session-{suffix}")
        self.policies.append(policy)
        return policy

    def assert_typed_refusal(self, operation: Callable[[], object]) -> None:
        with self.assertRaises(WorkerAdapterFailure) as caught:
            operation()
        self.assertEqual(caught.exception.code, "effect_worker_isolation_unavailable")

    def assert_real_backend_result(self, result: dict[str, object], backend: str) -> None:
        if result["status"] == "refused":
            self.assertEqual(result["code"], "effect_worker_isolation_unavailable")
            self.assertEqual(result["payload_callbacks"], 0)
            return
        self.assertEqual(result["status"], "ready", result)
        if backend.endswith("-v"):
            self.assertTrue(str(result["backend"]).startswith(backend), result)
            self.assertGreaterEqual(int(str(result["backend"])[len(backend) :]), 3)
        else:
            self.assertEqual(result["backend"], backend)
        self.assertEqual(result["payload_callbacks"], 1)

    def test_prepare_creates_owned_0700_workspace_and_scratch_and_0600_probe(self) -> None:
        policy = self.prepare("prepare")

        self.assertEqual(policy.tenant_home, self.tenant_home)
        self.assertEqual(policy.workspace, self.workspaces / "prepare")
        self.assertTrue(policy.workspace.is_dir())
        self.assertEqual(tuple(policy.workspace.iterdir()), ())
        self.assertEqual(_mode(policy.workspace), 0o700)
        self.assertEqual(_mode(policy.scratch), 0o700)
        self.assertEqual(_mode(policy.write_probe), 0o600)
        self.assertEqual(policy.write_probe.parent, self.tenant_home / "effects")
        self.assertEqual(policy.write_probe.read_bytes(), b"")
        self.assertEqual(os.lstat(policy.workspace).st_uid, os.getuid())
        self.assertEqual(os.lstat(policy.scratch).st_uid, os.getuid())
        self.assertEqual(os.lstat(policy.write_probe).st_uid, os.getuid())
        self.assertEqual(policy.workspace_identity, _identity(policy.workspace))
        self.assertEqual(policy.scratch_identity, _identity(policy.scratch))
        self.assertEqual(policy.probe_identity, _identity(policy.write_probe))

    def test_prepare_refuses_symlink_nonowner_reuse_and_identity_drift(self) -> None:
        tenant_alias = self.root / "tenant-alias"
        tenant_alias.symlink_to(self.tenant_home, target_is_directory=True)
        self.assert_typed_refusal(
            lambda: prepare_worker_isolation(
                tenant_alias, self.workspaces / "symlink", "session-symlink"
            )
        )

        if os.getuid() != 0:
            self.assert_typed_refusal(
                lambda: prepare_worker_isolation(Path("/"), None, "session-nonowner")
            )

        reused_workspace = self.workspaces / "reused"
        reused_workspace.mkdir(mode=0o700)
        self.assert_typed_refusal(
            lambda: prepare_worker_isolation(
                self.tenant_home, reused_workspace, "session-reused"
            )
        )

        policy = self.prepare("drift")
        drifted = dataclasses.replace(
            policy,
            scratch_identity=(policy.scratch_identity[0], policy.scratch_identity[1] + 1),
        )
        result = _child_result(drifted, lambda: "must-not-run")
        self.assertEqual(
            result,
            {
                "code": "effect_worker_isolation_unavailable",
                "payload_callbacks": 0,
                "status": "refused",
            },
        )

    def test_prepare_failure_cleanup_preserves_replacement_probe_and_scratch(self) -> None:
        real_close = os.close
        real_mkdtemp = tempfile.mkdtemp
        real_open = os.open
        created: dict[str, object] = {}

        def tracked_mkdtemp(*args: object, **kwargs: object) -> str:
            path = real_mkdtemp(*args, **kwargs)
            created["scratch"] = Path(path).resolve()
            return path

        def tracked_open(
            path: object,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: Optional[int] = None,
        ) -> int:
            if dir_fd is None:
                descriptor = real_open(path, flags, mode)
            else:
                descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            candidate = Path(os.fsdecode(path))
            if candidate.parent == self.tenant_home / "effects" and flags & os.O_EXCL:
                created["probe"] = candidate
                created["probe_descriptor"] = descriptor
            return descriptor

        def replace_then_fail(descriptor: int) -> None:
            real_close(descriptor)
            if descriptor != created.get("probe_descriptor"):
                return
            probe = created["probe"]
            scratch = created["scratch"]
            self.assertIsInstance(probe, Path)
            self.assertIsInstance(scratch, Path)
            original_probe = probe.with_name(probe.name + "-original")
            original_scratch = scratch.with_name(scratch.name + "-original")
            probe.rename(original_probe)
            probe.write_text("replacement-probe", encoding="utf-8")
            scratch.rename(original_scratch)
            scratch.mkdir(mode=0o700)
            created["original_probe"] = original_probe
            created["original_scratch"] = original_scratch
            raise OSError(errno.EIO, "deterministic post-create failure")

        with patch("floati.worker_isolation.tempfile.mkdtemp", side_effect=tracked_mkdtemp), patch(
            "floati.worker_isolation.os.open", side_effect=tracked_open
        ), patch("floati.worker_isolation.os.close", side_effect=replace_then_fail):
            self.assert_typed_refusal(
                lambda: prepare_worker_isolation(
                    self.tenant_home,
                    self.workspaces / "replacement-failure",
                    "session-replacement-failure",
                )
            )

        probe = created["probe"]
        scratch = created["scratch"]
        original_probe = created["original_probe"]
        original_scratch = created["original_scratch"]
        self.assertIsInstance(probe, Path)
        self.assertIsInstance(scratch, Path)
        self.assertIsInstance(original_probe, Path)
        self.assertIsInstance(original_scratch, Path)
        self.addCleanup(probe.unlink, missing_ok=True)
        self.addCleanup(shutil.rmtree, scratch, True)
        self.addCleanup(original_probe.unlink, missing_ok=True)
        self.addCleanup(shutil.rmtree, original_scratch, True)
        self.assertEqual(probe.read_text(encoding="utf-8"), "replacement-probe")
        self.assertTrue(scratch.is_dir())
        self.assertEqual(tuple(scratch.iterdir()), ())
        self.assertTrue(original_probe.is_file())
        self.assertTrue(original_scratch.is_dir())

    def test_prepare_failure_reports_cleanup_failure_without_masking_original(self) -> None:
        """Catches failed preparation silently swallowing failed rollback."""
        real_mkdtemp = tempfile.mkdtemp
        real_open = os.open
        real_rmdir = os.rmdir
        created: dict[str, Path] = {}

        def tracked_mkdtemp(*args: object, **kwargs: object) -> str:
            path = real_mkdtemp(*args, **kwargs)
            created["scratch"] = Path(path).resolve()
            return path

        def fail_probe_open(
            path: object, flags: int, mode: int = 0o777,
            *, dir_fd: Optional[int] = None,
        ) -> int:
            candidate = Path(os.fsdecode(path))
            if candidate.parent == self.tenant_home / "effects":
                raise OSError(errno.EIO, "probe creation failed")
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def fail_scratch_rmdir(path: object, *args: object, **kwargs: object) -> None:
            if Path(os.fsdecode(path)) == created.get("scratch"):
                raise OSError(errno.EACCES, "scratch cleanup failed")
            real_rmdir(path, *args, **kwargs)

        with (
            patch(
                "floati.worker_isolation.tempfile.mkdtemp",
                side_effect=tracked_mkdtemp,
            ),
            patch("floati.worker_isolation.os.open", side_effect=fail_probe_open),
            patch(
                "floati.worker_isolation.os.rmdir",
                side_effect=fail_scratch_rmdir,
            ),
            self.assertRaises(WorkerAdapterFailure) as caught,
        ):
            prepare_worker_isolation(
                self.tenant_home,
                self.workspaces / "rollback-failure",
                "session-rollback-failure",
            )

        self.assertEqual("effect_worker_isolation_unavailable", caught.exception.code)
        original = caught.exception.__cause__
        self.assertIsInstance(original, OSError)
        self.assertEqual(errno.EIO, original.errno)
        cleanup = original.__context__
        self.assertIsInstance(cleanup, OSError)
        self.assertEqual(errno.EACCES, cleanup.errno)
        scratch = created["scratch"]
        self.assertTrue(scratch.is_dir())
        shutil.rmtree(scratch)

    def test_cleanup_removes_only_matching_probe_and_scratch(self) -> None:
        matching = self.prepare("matching")
        (matching.scratch / "nested").mkdir()
        (matching.scratch / "nested" / "data").write_text("owned", encoding="utf-8")
        cleanup_worker_isolation(matching)
        self.assertFalse(matching.write_probe.exists())
        self.assertFalse(matching.scratch.exists())

        probe_drift = self.prepare("probe-drift")
        # Allocate the replacement while the original probe still holds its inode:
        # ext4 hands the same inode straight back to an unlink-then-create at the same
        # path, which silently left this drift undetectable on Linux.
        foreign_probe = probe_drift.write_probe.with_name(
            probe_drift.write_probe.name + ".foreign"
        )
        foreign_probe.write_text("foreign", encoding="utf-8")
        foreign_probe.replace(probe_drift.write_probe)
        self.assertNotEqual(_identity(probe_drift.write_probe), probe_drift.probe_identity)
        self.assert_typed_refusal(lambda: cleanup_worker_isolation(probe_drift))
        self.assertEqual(probe_drift.write_probe.read_text(encoding="utf-8"), "foreign")
        self.assertFalse(probe_drift.scratch.exists())

        scratch_drift = self.prepare("scratch-drift")
        original_scratch = scratch_drift.scratch.with_name(scratch_drift.scratch.name + "-moved")
        scratch_drift.scratch.rename(original_scratch)
        scratch_drift.scratch.mkdir(mode=0o700)
        (scratch_drift.scratch / "foreign").write_text("preserve", encoding="utf-8")
        self.assert_typed_refusal(lambda: cleanup_worker_isolation(scratch_drift))
        self.assertFalse(scratch_drift.write_probe.exists())
        self.assertEqual(
            (scratch_drift.scratch / "foreign").read_text(encoding="utf-8"), "preserve"
        )
        shutil.rmtree(scratch_drift.scratch)
        shutil.rmtree(original_scratch)

    def _use_temp_root(self, root: Path) -> None:
        """Point tempfile -- and so both prepare and cleanup -- at one temp root."""
        previous = tempfile.tempdir
        tempfile.tempdir = os.fspath(root)
        self.addCleanup(setattr, tempfile, "tempdir", previous)

    def test_cleanup_succeeds_under_a_sticky_shared_temp_root_it_does_not_own(self) -> None:
        """The Linux default TMPDIR is root:root 1777; cleanup may not demand ownership."""
        shared_root = _foreign_sticky_temp_root()
        if shared_root is None:
            self.skipTest("no canonical sticky temp root owned by another uid on this host")
        self._use_temp_root(shared_root)

        policy = self.prepare("sticky-root")
        self.addCleanup(shutil.rmtree, policy.scratch, True)

        self.assertEqual(policy.scratch.parent, shared_root)
        parent_metadata = os.lstat(shared_root)
        self.assertNotEqual(parent_metadata.st_uid, os.getuid())
        self.assertTrue(parent_metadata.st_mode & stat.S_ISVTX)
        self.assertEqual(os.lstat(policy.scratch).st_uid, os.getuid())
        self.assertEqual(_mode(policy.scratch), 0o700)

        (policy.scratch / "nested").mkdir()
        (policy.scratch / "nested" / "data").write_text("owned", encoding="utf-8")

        cleanup_worker_isolation(policy)

        self.assertFalse(policy.write_probe.exists())
        self.assertFalse(policy.scratch.exists())

    def test_cleanup_refuses_a_shared_temp_root_that_is_neither_owned_nor_sticky(self) -> None:
        """Control: dropping the ownership demand may not admit a plain foreign parent."""
        temp_root = self.root / "temp-root"
        temp_root.mkdir(mode=0o700)
        self._use_temp_root(temp_root)

        policy = self.prepare("foreign-parent")
        self.assertEqual(policy.scratch.parent, temp_root)

        with patch(
            "floati.worker_isolation.os.lstat",
            side_effect=_lstat_reporting(temp_root, uid=os.getuid() + 1, sticky=False),
        ):
            self.assert_typed_refusal(lambda: cleanup_worker_isolation(policy))

        self.assertTrue(policy.scratch.is_dir())

    def test_cleanup_refuses_a_shared_temp_root_whose_identity_changed(self) -> None:
        """The parent recorded at prepare must still be the directory cleanup resolves."""
        temp_root = self.root / "swapped-root"
        temp_root.mkdir(mode=0o700)
        self._use_temp_root(temp_root)

        policy = self.prepare("swapped-parent")
        self.assertEqual(policy.scratch.parent, temp_root)

        moved_scratch = self.root / "carried-scratch"
        # Same inode-reuse trap: rmdir-then-mkdir at one path returns the SAME inode on
        # ext4, so the swap must be built from a directory allocated while the original
        # still exists, then renamed over it.
        replacement_root = self.root / "replacement-root"
        replacement_root.mkdir(mode=0o700)
        shutil.move(os.fspath(policy.scratch), os.fspath(moved_scratch))
        temp_root.rmdir()
        replacement_root.rename(temp_root)
        shutil.move(os.fspath(moved_scratch), os.fspath(policy.scratch))
        self.assertNotEqual(_identity(temp_root), policy.scratch_parent_identity)
        self.assertEqual(_identity(policy.scratch), policy.scratch_identity)

        self.assert_typed_refusal(lambda: cleanup_worker_isolation(policy))

        self.assertTrue(policy.scratch.is_dir())

    def test_cleanup_refuses_a_policy_with_no_recorded_temp_root(self) -> None:
        """Fail closed: relaxing ownership may not let an unrecorded parent through."""
        policy = self.prepare("no-recorded-parent")
        stripped = dataclasses.replace(policy, scratch_parent_identity=None)

        self.assert_typed_refusal(lambda: cleanup_worker_isolation(stripped))

        self.assertTrue(policy.scratch.is_dir())

    def test_cleanup_low_level_failure_is_typed_and_retryable(self) -> None:
        """Catches unlink failure being treated as successful cleanup."""
        policy = self.prepare("cleanup-retry")
        real_unlink = os.unlink
        failed = False

        def fail_probe_once(
            path: object, *args: object, **kwargs: object,
        ) -> None:
            nonlocal failed
            if Path(os.fsdecode(path)) == policy.write_probe and not failed:
                failed = True
                raise OSError(errno.EACCES, "probe unlink failed")
            real_unlink(path, *args, **kwargs)

        with patch("floati.worker_isolation.os.unlink", side_effect=fail_probe_once):
            self.assert_typed_refusal(lambda: cleanup_worker_isolation(policy))

        self.assertTrue(policy.write_probe.is_file())
        self.assertFalse(policy.scratch.exists())
        cleanup_worker_isolation(policy)
        self.assertFalse(policy.write_probe.exists())
        self.assertFalse(policy.scratch.exists())

    def test_unsupported_platform_refuses_without_running_payload(self) -> None:
        policy = self.prepare("unsupported")
        result = _child_result(policy, lambda: "must-not-run", platform_override="unsupported")
        self.assertEqual(
            result,
            {
                "code": "effect_worker_isolation_unavailable",
                "payload_callbacks": 0,
                "status": "refused",
            },
        )

    def test_import_without_platform_modules_refuses_unsupported_before_payload(self) -> None:
        script = r'''
import importlib.abc
import json
import sys
import tempfile
from pathlib import Path

from floati.worker_errors import WorkerAdapterFailure

class BlockPlatformModules(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in {"fcntl", "resource"}:
            raise ModuleNotFoundError(fullname)
        return None

sys.modules.pop("fcntl", None)
sys.modules.pop("resource", None)
sys.meta_path.insert(0, BlockPlatformModules())
sys.platform = "unsupported-test-platform"

from floati.worker_isolation import (
    apply_worker_isolation,
    cleanup_worker_isolation,
    prepare_worker_isolation,
)

with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary).resolve()
    tenant = root / "tenant"
    tenant.mkdir(mode=0o700)
    (tenant / "effects").mkdir(mode=0o700)
    workspace_parent = root / "workspaces"
    workspace_parent.mkdir(mode=0o700)
    policy = prepare_worker_isolation(tenant, workspace_parent / "worker", "session-import")
    callbacks = 0
    try:
        apply_worker_isolation(policy)
    except WorkerAdapterFailure as exc:
        result = {
            "code": exc.code,
            "callbacks": callbacks,
            "fcntl_loaded": "fcntl" in sys.modules,
            "resource_loaded": "resource" in sys.modules,
        }
    else:
        callbacks += 1
        result = {"code": "unexpected-success", "callbacks": callbacks}
    finally:
        cleanup_worker_isolation(policy)
    print(json.dumps(result, sort_keys=True))
'''
        completed = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            capture_output=True,
            text=True,
            cwd=Path(__file__).resolve().parent.parent,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            {
                "callbacks": 0,
                "code": "effect_worker_isolation_unavailable",
                "fcntl_loaded": False,
                "resource_loaded": False,
            },
        )

    def test_worker_isolation_failure_imports_only_worker_errors(self) -> None:
        original_workers = sys.modules.pop("floati.workers", None)
        try:
            failure = worker_isolation._failure()
            self.assertIsInstance(failure, WorkerAdapterFailure)
            self.assertEqual(failure.code, "effect_worker_isolation_unavailable")
            self.assertNotIn("floati.workers", sys.modules)
        finally:
            if original_workers is not None:
                sys.modules["floati.workers"] = original_workers

    def test_landlock_syscalls_are_closed_to_known_64_bit_architectures(self) -> None:
        for machine in ("x86_64", "aarch64", "arm64"):
            with self.subTest(machine=machine):
                self.assertEqual(
                    worker_isolation._landlock_syscall_numbers(machine),
                    (444, 445, 446),
                )

        for machine in ("", "amd64", "i686", "mips", "mips64", "ppc64le", "s390x"):
            with self.subTest(machine=machine):
                self.assert_typed_refusal(
                    lambda machine=machine: worker_isolation._landlock_syscall_numbers(
                        machine
                    )
                )

    def test_unknown_linux_architecture_refuses_before_loading_libc(self) -> None:
        with patch("floati.worker_isolation.platform.machine", return_value="mips64"), patch(
            "floati.worker_isolation.ctypes.CDLL",
            side_effect=AssertionError("libc must not load for an unknown syscall ABI"),
        ):
            self.assert_typed_refusal(lambda: worker_isolation._apply_linux(object()))

    def test_landlock_path_beneath_attr_matches_packed_uapi_layout(self) -> None:
        path_attr = worker_isolation._LandlockPathBeneathAttr
        self.assertEqual(ctypes.sizeof(path_attr), 12)
        self.assertEqual(path_attr.allowed_access.offset, 0)
        self.assertEqual(path_attr.parent_fd.offset, 8)

    def test_macos_profile_rejects_all_bidi_controls(self) -> None:
        bidi_controls = (
            "\u061c",
            "\u200e",
            "\u200f",
            "\u202a",
            "\u202b",
            "\u202c",
            "\u202d",
            "\u202e",
            "\u2066",
            "\u2067",
            "\u2068",
            "\u2069",
        )
        for control in bidi_controls:
            with self.subTest(codepoint=f"U+{ord(control):04X}"):
                with self.assertRaises(ValueError):
                    worker_isolation._profile_path(Path("\x2ftmp") / f"before{control}after")

    def test_macos_real_backend_denies_tenant_write_and_allows_scratch(self) -> None:
        if sys.platform != "darwin":
            return
        policy = self.prepare("macos")
        tenant_target = self.tenant_home / "effects" / "macos-direct-write"
        scratch_target = policy.scratch / "macos-scratch-write"
        result = _child_result(
            policy,
            lambda: {
                "tenant_errno": _try_write(tenant_target, create=True),
                "scratch_errno": _try_write(scratch_target, create=True),
                "dev_null_errno": _try_open_read_write(Path(os.devnull)),
            },
        )
        self.assert_real_backend_result(result, "macos-sandbox")
        if result["status"] == "ready":
            payload = result["payload"]
            self.assertIn(payload["tenant_errno"], _DENIED)
            self.assertIsNone(payload["scratch_errno"])
            self.assertIsNone(payload["dev_null_errno"])
            self.assertFalse(tenant_target.exists())
            self.assertEqual(scratch_target.read_bytes(), b"changed")

            escaped_policy = self.prepare('macos-quote-"\\escape')
            escaped_result = _child_result(
                escaped_policy,
                lambda: {
                    "tenant_errno": _try_write(tenant_target, create=True),
                    "scratch_errno": _try_write(
                        escaped_policy.scratch / "escaped-scratch-write", create=True
                    ),
                },
            )
            self.assertEqual(escaped_result["status"], "ready", escaped_result)
            self.assertEqual(escaped_result["backend"], "macos-sandbox")
            self.assertIn(escaped_result["payload"]["tenant_errno"], _DENIED)
            self.assertIsNone(escaped_result["payload"]["scratch_errno"])

            workspace_less = self.prepare("macos-no-workspace", workspace=False)
            workspace_less_result = _child_result(
                workspace_less,
                lambda: {
                    "tenant_errno": _try_write(tenant_target, create=True),
                    "scratch_errno": _try_write(
                        workspace_less.scratch / "workspace-less-write", create=True
                    ),
                },
            )
            self.assertEqual(workspace_less_result["status"], "ready", workspace_less_result)
            self.assertEqual(workspace_less_result["backend"], "macos-sandbox")
            self.assertIn(workspace_less_result["payload"]["tenant_errno"], _DENIED)
            self.assertIsNone(workspace_less_result["payload"]["scratch_errno"])

    def test_linux_real_backend_denies_tenant_write_and_allows_scratch(self) -> None:
        if not sys.platform.startswith("linux"):
            return
        policy = self.prepare("linux")
        tenant_target = self.tenant_home / "effects" / "linux-direct-write"
        scratch_target = policy.scratch / "linux-scratch-write"
        result = _child_result(
            policy,
            lambda: {
                "tenant_errno": _try_write(tenant_target, create=True),
                "scratch_errno": _try_write(scratch_target, create=True),
                # The clause this twin was missing.  Its macOS counterpart has asserted
                # this since the 2026-08-14 Seatbelt carve-out; adding it to only one
                # twin is what let the Linux backend go without the rule and kept every
                # instrument silent about it.  git opens /dev/null O_RDWR at startup
                # (sanitize_stdfds), so a denial here kills every git in the boundary.
                "dev_null_errno": _try_open_read_write(Path(os.devnull)),
            },
        )
        self.assert_real_backend_result(result, "linux-landlock-v")
        if result["status"] == "ready":
            payload = result["payload"]
            self.assertIn(payload["tenant_errno"], _DENIED)
            self.assertIsNone(payload["scratch_errno"])
            self.assertIsNone(payload["dev_null_errno"])
            self.assertFalse(tenant_target.exists())
            self.assertEqual(scratch_target.read_bytes(), b"changed")

            # The second clause this twin was missing.  Its macOS counterpart has run
            # a workspace-less policy since the twin was written; the Linux twin never
            # did, so `if allowed_path is None: continue` in _apply_linux -- the branch
            # deciding whether a workspace-less Worker gets a one-rule ruleset instead
            # of two -- had never executed on Linux.  Same shape as the /dev/null gap:
            # a case asserted in one twin only leaves the other backend free to be
            # wrong with no instrument to say so.
            workspace_less = self.prepare("linux-no-workspace", workspace=False)
            workspace_less_result = _child_result(
                workspace_less,
                lambda: {
                    "tenant_errno": _try_write(tenant_target, create=True),
                    "scratch_errno": _try_write(
                        workspace_less.scratch / "workspace-less-write", create=True
                    ),
                },
            )
            self.assertEqual(workspace_less_result["status"], "ready", workspace_less_result)
            workspace_less_backend = str(workspace_less_result["backend"])
            self.assertTrue(
                workspace_less_backend.startswith("linux-landlock-v"),
                workspace_less_result,
            )
            self.assertGreaterEqual(
                int(workspace_less_backend[len("linux-landlock-v") :]), 3
            )
            self.assertIn(workspace_less_result["payload"]["tenant_errno"], _DENIED)
            self.assertIsNone(workspace_less_result["payload"]["scratch_errno"])
            self.assertFalse(tenant_target.exists())

    def test_backend_policy_is_inherited_by_thread_subprocess_and_nested_fork(self) -> None:
        policy = self.prepare("inheritance")
        tenant_target = self.tenant_home / "effects" / "inherited-write"
        inherited_tenant_descriptor = os.open(policy.write_probe, os.O_RDONLY)
        self.addCleanup(os.close, inherited_tenant_descriptor)

        def payload() -> dict[str, object]:
            result: dict[str, object] = {}
            try:
                os.fstat(inherited_tenant_descriptor)
            except OSError as exc:
                result["inherited_descriptor_errno"] = exc.errno
            else:
                result["inherited_descriptor_errno"] = None

            thread_values: list[tuple[Optional[int], Optional[int]]] = []

            def thread_write() -> None:
                thread_values.append(
                    (
                        _try_write(tenant_target, create=True),
                        _try_write(policy.scratch / "thread-write", create=True),
                    )
                )

            thread = threading.Thread(target=thread_write)
            thread.start()
            thread.join()
            result["thread"] = thread_values[0]

            tenant_command = subprocess.run(
                ["/bin/sh", "-c", 'printf changed > "$1"', "sh", os.fspath(tenant_target)],
                check=False,
                capture_output=True,
            )
            scratch_command = subprocess.run(
                [
                    "/bin/sh",
                    "-c",
                    'printf changed > "$1"',
                    "sh",
                    os.fspath(policy.scratch / "subprocess-write"),
                ],
                check=False,
                capture_output=True,
            )
            result["subprocess"] = [tenant_command.returncode, scratch_command.returncode]

            read_descriptor, write_descriptor = os.pipe()
            nested_pid = os.fork()
            if nested_pid == 0:
                os.close(read_descriptor)
                nested = [
                    _try_write(tenant_target, create=True),
                    _try_write(policy.scratch / "nested-fork-write", create=True),
                ]
                os.write(write_descriptor, json.dumps(nested).encode("utf-8"))
                os.close(write_descriptor)
                os._exit(0)
            os.close(write_descriptor)
            nested_data = os.read(read_descriptor, 4096)
            os.close(read_descriptor)
            _, nested_status = os.waitpid(nested_pid, 0)
            result["nested_status"] = nested_status
            result["nested"] = json.loads(nested_data.decode("utf-8"))
            return result

        result = _child_result(policy, payload)
        expected_backend = "macos-sandbox" if sys.platform == "darwin" else "linux-landlock-v"
        self.assert_real_backend_result(result, expected_backend)
        if result["status"] == "ready":
            payload_result = result["payload"]
            self.assertEqual(payload_result["inherited_descriptor_errno"], errno.EBADF)
            self.assertIn(payload_result["thread"][0], _DENIED)
            self.assertIsNone(payload_result["thread"][1])
            self.assertNotEqual(payload_result["subprocess"][0], 0)
            self.assertEqual(payload_result["subprocess"][1], 0)
            self.assertEqual(payload_result["nested_status"], 0)
            self.assertIn(payload_result["nested"][0], _DENIED)
            self.assertIsNone(payload_result["nested"][1])
            self.assertFalse(tenant_target.exists())

    def test_alias_link_rename_and_truncate_cannot_modify_tenant_probe(self) -> None:
        policy = self.prepare("aliases")
        hard_alias = self.tenant_home / "effects" / "probe-hard-alias"
        symbolic_alias = self.tenant_home / "effects" / "probe-symbolic-alias"
        os.link(policy.write_probe, hard_alias)
        symbolic_alias.symlink_to(policy.write_probe)

        def payload() -> dict[str, object]:
            attempts: dict[str, Optional[int]] = {
                "direct_truncate": _try_write(policy.write_probe, truncate=True),
                "hard_alias_truncate": _try_write(hard_alias, truncate=True),
            }
            try:
                descriptor = os.open(symbolic_alias, os.O_WRONLY | os.O_TRUNC)
            except OSError as exc:
                attempts["symbolic_alias_truncate"] = exc.errno
            else:
                os.write(descriptor, b"changed")
                os.close(descriptor)
                attempts["symbolic_alias_truncate"] = None
            try:
                os.link(policy.write_probe, policy.scratch / "linked-probe")
            except OSError as exc:
                attempts["link"] = exc.errno
            else:
                attempts["link"] = None
            try:
                os.rename(policy.write_probe, policy.scratch / "renamed-probe")
            except OSError as exc:
                attempts["rename"] = exc.errno
            else:
                attempts["rename"] = None
            return attempts

        result = _child_result(policy, payload)
        expected_backend = "macos-sandbox" if sys.platform == "darwin" else "linux-landlock-v"
        self.assert_real_backend_result(result, expected_backend)
        if result["status"] == "ready":
            for operation, operation_errno in result["payload"].items():
                allowed_errnos = (
                    _DENIED_LINK_OR_RENAME if operation in {"link", "rename"} else _DENIED
                )
                self.assertIn(operation_errno, allowed_errnos, operation)
            self.assertEqual(policy.write_probe.read_bytes(), b"")
            self.assertEqual(_identity(policy.write_probe), policy.probe_identity)
            self.assertFalse((policy.scratch / "linked-probe").exists())
            self.assertFalse((policy.scratch / "renamed-probe").exists())


if __name__ == "__main__":
    unittest.main()

"""Closed kernel filesystem-isolation backends for effect-enabled Workers."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import platform
import shutil
import stat
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .worker_errors import WorkerAdapterFailure


if sys.platform == "darwin":
    try:
        import fcntl as _fcntl
        import resource as _resource
    except ImportError:
        _fcntl = None
        _resource = None
else:
    _fcntl = None
    _resource = None


_FAILURE_CODE = "effect_worker_isolation_unavailable"
_SCRATCH_PREFIX = "floati-effect-worker-"
_PROBE_PREFIX = ".floati-effect-worker-"
_F_GETPATH = 50

_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
# This is intentionally closed to Linux x86_64 and the arm64 names backed by
# asm-generic/unistd.h.  Other machine values refuse before libc is loaded;
# architecture-specific numbers (notably MIPS) are never guessed.
_LANDLOCK_SYSCALLS = {
    "x86_64": (444, 445, 446),
    "aarch64": (444, 445, 446),
    "arm64": (444, 445, 446),
}

_BIDI_CONTROL_CODEPOINTS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)

_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_ACCESS_FS_REFER = 1 << 13
_LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14

WRITE_RIGHTS = (
    _LANDLOCK_ACCESS_FS_WRITE_FILE
    | _LANDLOCK_ACCESS_FS_REMOVE_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_CHAR
    | _LANDLOCK_ACCESS_FS_MAKE_DIR
    | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_SOCK
    | _LANDLOCK_ACCESS_FS_MAKE_FIFO
    | _LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | _LANDLOCK_ACCESS_FS_MAKE_SYM
    | _LANDLOCK_ACCESS_FS_REFER
    | _LANDLOCK_ACCESS_FS_TRUNCATE
)


@dataclass(frozen=True)
class WorkerIsolationPolicy:
    tenant_home: Path
    workspace: Optional[Path]
    scratch: Path
    write_probe: Path
    workspace_identity: Optional[tuple[int, int]]
    scratch_identity: tuple[int, int]
    probe_identity: tuple[int, int]
    # The temp root the scratch was created in, frozen at preparation.  It defaults to
    # None so that a policy rebuilt from a bootstrap payload -- which applies isolation
    # and never cleans up -- keeps its existing seven-field shape.  Cleanup refuses a
    # policy that reaches it without one.
    scratch_parent_identity: Optional[tuple[int, int]] = None


class _LandlockRulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _LandlockPathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    ]


def _failure(cause: Optional[BaseException] = None) -> BaseException:
    failure = WorkerAdapterFailure(_FAILURE_CODE)
    if cause is not None:
        failure.__cause__ = cause
    return failure


def _identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _landlock_syscall_numbers(machine: str) -> tuple[int, int, int]:
    try:
        return _LANDLOCK_SYSCALLS[machine]
    except (KeyError, TypeError) as exc:
        raise _failure(exc)


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.fspath(path), os.fspath(root))) == os.fspath(root)
    except (OSError, TypeError, ValueError):
        return False


def _canonical_path(path: Path, *, must_exist: bool) -> Path:
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("invalid path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("path is not absolute")
    resolved = candidate.resolve(strict=must_exist)
    if candidate != resolved:
        raise ValueError("path is not canonical")
    return candidate


def _owned_metadata(path: Path, file_type: str) -> os.stat_result:
    metadata = os.lstat(path)
    if metadata.st_uid != os.getuid():
        raise PermissionError("path is not owned by the Worker uid")
    if file_type == "directory" and not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("path is not a directory")
    if file_type == "regular" and not stat.S_ISREG(metadata.st_mode):
        raise ValueError("path is not a regular file")
    return metadata


def _shared_parent_metadata(path: Path) -> os.stat_result:
    """Accept a directory we created something in but do not own the container of.

    A multi-tenant temp root -- \x2ftmp is root:root 1777 on every Linux host -- is never
    owned by the Worker uid, and demanding ownership of it refuses every default
    TMPDIR.  The sticky bit is what makes such a root safe: it is the kernel's promise
    that only an entry's own owner may remove or rename it.  A parent that is neither
    ours nor sticky carries no such promise and is still refused.  os.lstat keeps a
    symlink from passing as its target.
    """
    metadata = os.lstat(path)
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("path is not a directory")
    if metadata.st_uid != os.getuid() and not metadata.st_mode & stat.S_ISVTX:
        raise PermissionError("shared parent is neither owned by the Worker uid nor sticky")
    return metadata


def _validated_directory(
    path: Path,
    identity: tuple[int, int],
    *,
    exact_mode: Optional[int] = None,
) -> None:
    canonical = _canonical_path(path, must_exist=True)
    metadata = _owned_metadata(canonical, "directory")
    if _identity(metadata) != identity:
        raise ValueError("directory identity changed")
    if exact_mode is not None and stat.S_IMODE(metadata.st_mode) != exact_mode:
        raise PermissionError("directory mode changed")


def _validated_probe(policy: WorkerIsolationPolicy) -> None:
    canonical = _canonical_path(policy.write_probe, must_exist=True)
    if canonical.parent != policy.tenant_home / "effects":
        raise ValueError("probe escaped the tenant effects directory")
    metadata = _owned_metadata(canonical, "regular")
    if _identity(metadata) != policy.probe_identity:
        raise ValueError("probe identity changed")
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PermissionError("probe mode changed")


def _validate_policy(policy: WorkerIsolationPolicy) -> None:
    if not isinstance(policy, WorkerIsolationPolicy):
        raise TypeError("invalid isolation policy")
    tenant_home = _canonical_path(policy.tenant_home, must_exist=True)
    tenant_metadata = _owned_metadata(tenant_home, "directory")
    effects = _canonical_path(tenant_home / "effects", must_exist=True)
    _owned_metadata(effects, "directory")

    if policy.workspace is None:
        if policy.workspace_identity is not None:
            raise ValueError("workspace identity exists without a workspace")
    else:
        if policy.workspace_identity is None:
            raise ValueError("workspace identity is missing")
        _validated_directory(policy.workspace, policy.workspace_identity, exact_mode=0o700)
        if _is_beneath(policy.workspace, tenant_home):
            raise ValueError("workspace overlaps tenant truth")

    _validated_directory(policy.scratch, policy.scratch_identity, exact_mode=0o700)
    if _is_beneath(policy.scratch, tenant_home):
        raise ValueError("scratch overlaps tenant truth")
    if policy.workspace is not None and (
        _is_beneath(policy.scratch, policy.workspace)
        or _is_beneath(policy.workspace, policy.scratch)
    ):
        raise ValueError("workspace and scratch overlap")
    _validated_probe(policy)
    if tenant_metadata.st_uid != os.getuid():
        raise PermissionError("tenant owner changed")


def _remove_matching_probe(policy: WorkerIsolationPolicy) -> None:
    _remove_created_probe(
        policy.write_probe,
        policy.probe_identity,
        policy.tenant_home / "effects",
    )


def _remove_created_probe(
    path: Optional[Path],
    identity: Optional[tuple[int, int]],
    effects_path: Optional[Path],
) -> None:
    if path is None or identity is None or effects_path is None:
        return
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    try:
        canonical = _canonical_path(path, must_exist=True)
        canonical_effects = _canonical_path(effects_path, must_exist=True)
        _owned_metadata(canonical_effects, "directory")
        if canonical.parent != canonical_effects:
            return
        metadata = _owned_metadata(canonical, "regular")
        if _identity(metadata) != identity:
            raise ValueError("probe identity changed")
        os.unlink(canonical)
    except FileNotFoundError as exc:
        raise ValueError("probe changed during cleanup") from exc


def _remove_matching_scratch(policy: WorkerIsolationPolicy) -> None:
    _remove_created_directory(
        policy.scratch,
        policy.scratch_identity,
        Path(tempfile.gettempdir()).resolve(strict=True),
        name_prefix=_SCRATCH_PREFIX,
        recursive=True,
        parent_identity=policy.scratch_parent_identity,
        shared_parent=True,
    )


def _remove_created_directory(
    path: Optional[Path],
    identity: Optional[tuple[int, int]],
    parent: Optional[Path],
    *,
    name_prefix: Optional[str] = None,
    recursive: bool = False,
    parent_identity: Optional[tuple[int, int]] = None,
    shared_parent: bool = False,
) -> None:
    if path is None or identity is None or parent is None:
        return
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    try:
        canonical = _canonical_path(path, must_exist=True)
        canonical_parent = _canonical_path(parent, must_exist=True)
        if shared_parent:
            parent_metadata = _shared_parent_metadata(canonical_parent)
            if parent_identity is None:
                raise ValueError("shared parent identity was not recorded")
            if _identity(parent_metadata) != parent_identity:
                raise ValueError("shared parent identity changed")
        else:
            _owned_metadata(canonical_parent, "directory")
        if canonical.parent != canonical_parent:
            raise ValueError("created directory escaped its parent")
        if name_prefix is not None and not canonical.name.startswith(name_prefix):
            raise ValueError("created directory name changed")
        metadata = _owned_metadata(canonical, "directory")
        if _identity(metadata) != identity:
            raise ValueError("created directory identity changed")
        if recursive:
            shutil.rmtree(canonical)
        else:
            os.rmdir(canonical)
    except FileNotFoundError as exc:
        raise ValueError("created directory changed during cleanup") from exc


def _cleanup_created_paths(
    operations: tuple[Callable[[], None], ...],
) -> None:
    failure: Optional[BaseException] = None
    for operation in operations:
        try:
            operation()
        except Exception as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


def prepare_worker_isolation(
    tenant_home: Path,
    workspace: Optional[Path],
    session_id: str,
) -> WorkerIsolationPolicy:
    """Create fresh owned policy paths and freeze their filesystem identities."""

    workspace_path: Optional[Path] = None
    workspace_identity: Optional[tuple[int, int]] = None
    workspace_parent: Optional[Path] = None
    scratch_path: Optional[Path] = None
    scratch_identity: Optional[tuple[int, int]] = None
    scratch_parent_identity: Optional[tuple[int, int]] = None
    probe_path: Optional[Path] = None
    probe_identity: Optional[tuple[int, int]] = None
    effects_path: Optional[Path] = None
    try:
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
            raise ValueError("invalid Worker session id")
        session_bytes = session_id.encode("utf-8", "strict")
        tenant_path = _canonical_path(Path(tenant_home), must_exist=True)
        _owned_metadata(tenant_path, "directory")
        effects_path = _canonical_path(tenant_path / "effects", must_exist=True)
        _owned_metadata(effects_path, "directory")

        if workspace is not None:
            requested_workspace = _canonical_path(Path(workspace), must_exist=False)
            workspace_parent = _canonical_path(requested_workspace.parent, must_exist=True)
            _owned_metadata(workspace_parent, "directory")
            if _is_beneath(requested_workspace, tenant_path):
                raise ValueError("workspace overlaps tenant truth")
            os.mkdir(requested_workspace, 0o700)
            workspace_path = requested_workspace
            workspace_metadata = _owned_metadata(workspace_path, "directory")
            workspace_identity = _identity(workspace_metadata)
            _canonical_path(workspace_path, must_exist=True)
            workspace_metadata = _owned_metadata(workspace_path, "directory")
            if _identity(workspace_metadata) != workspace_identity:
                raise ValueError("workspace identity changed during preparation")
            if stat.S_IMODE(workspace_metadata.st_mode) != 0o700:
                raise PermissionError("workspace mode is not private")

        raw_scratch_path = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX))
        scratch_metadata = _owned_metadata(raw_scratch_path, "directory")
        scratch_identity = _identity(scratch_metadata)
        scratch_path = raw_scratch_path.resolve(strict=True)
        # Freeze the temp root we actually landed in, so cleanup compares against the
        # directory of record rather than whatever TMPDIR resolves to by then.
        scratch_parent_identity = _identity(os.lstat(scratch_path.parent))
        os.chmod(scratch_path, 0o700)
        scratch_metadata = _owned_metadata(scratch_path, "directory")
        if _identity(scratch_metadata) != scratch_identity:
            raise ValueError("scratch identity changed during preparation")
        if stat.S_IMODE(scratch_metadata.st_mode) != 0o700:
            raise PermissionError("scratch mode is not private")
        if _is_beneath(scratch_path, tenant_path):
            raise ValueError("scratch overlaps tenant truth")

        probe_name = _PROBE_PREFIX + hashlib.sha256(session_bytes).hexdigest() + ".probe"
        probe_path = effects_path / probe_name
        nofollow = getattr(os, "O_NOFOLLOW", None)
        if nofollow is None:
            raise OSError(errno.ENOTSUP, "O_NOFOLLOW is required")
        probe_descriptor = os.open(
            probe_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
            0o600,
        )
        try:
            probe_metadata = os.fstat(probe_descriptor)
            if probe_metadata.st_uid != os.getuid() or not stat.S_ISREG(
                probe_metadata.st_mode
            ):
                raise PermissionError("probe inode is invalid")
            if stat.S_IMODE(probe_metadata.st_mode) != 0o600:
                raise PermissionError("probe mode is not private")
            probe_identity = _identity(probe_metadata)
        finally:
            os.close(probe_descriptor)
        _canonical_path(probe_path, must_exist=True)
        probe_metadata = _owned_metadata(probe_path, "regular")
        if _identity(probe_metadata) != probe_identity:
            raise ValueError("probe identity changed during preparation")
        if stat.S_IMODE(probe_metadata.st_mode) != 0o600:
            raise PermissionError("probe mode is not private")

        return WorkerIsolationPolicy(
            tenant_home=tenant_path,
            workspace=workspace_path,
            scratch=scratch_path,
            write_probe=probe_path,
            workspace_identity=workspace_identity,
            scratch_identity=scratch_identity,
            probe_identity=probe_identity,
            scratch_parent_identity=scratch_parent_identity,
        )
    except Exception as exc:
        try:
            _cleanup_created_paths((
                lambda: _remove_created_probe(
                    probe_path, probe_identity, effects_path,
                ),
                lambda: _remove_created_directory(
                    scratch_path,
                    scratch_identity,
                    Path(tempfile.gettempdir()).resolve(strict=True),
                    name_prefix=_SCRATCH_PREFIX,
                    parent_identity=scratch_parent_identity,
                    shared_parent=True,
                ),
                lambda: _remove_created_directory(
                    workspace_path,
                    workspace_identity,
                    workspace_parent,
                ),
            ))
        except Exception as cleanup_failure:
            cleanup_failure.__context__ = None
            exc.__context__ = cleanup_failure
        raise _failure(exc)


def cleanup_worker_isolation(policy: WorkerIsolationPolicy) -> None:
    """Remove only the probe and scratch roots whose frozen identities still match."""

    if not isinstance(policy, WorkerIsolationPolicy):
        return
    try:
        _cleanup_created_paths((
            lambda: _remove_matching_probe(policy),
            lambda: _remove_matching_scratch(policy),
        ))
    except Exception as exc:
        raise _failure(exc)


def _resolved_linux_descriptors() -> dict[int, Path]:
    resolved: dict[int, Path] = {}
    for name in os.listdir("/proc/self/fd"):
        try:
            descriptor = int(name)
            target = os.readlink(f"/proc/self/fd/{descriptor}")
        except (FileNotFoundError, OSError, ValueError):
            continue
        if target.endswith(" (deleted)"):
            target = target[: -len(" (deleted)")]
        if not os.path.isabs(target):
            continue
        try:
            resolved[descriptor] = Path(target).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
    return resolved


def _resolved_macos_descriptors() -> dict[int, Path]:
    if _fcntl is None or _resource is None:
        raise OSError(errno.ENOTSUP, "macOS descriptor facilities are unavailable")
    resolved: dict[int, Path] = {}
    soft_limit = _resource.getrlimit(_resource.RLIMIT_NOFILE)[0]
    if soft_limit == _resource.RLIM_INFINITY:
        soft_limit = os.sysconf("SC_OPEN_MAX")
    for descriptor in range(int(soft_limit)):
        try:
            path_buffer = _fcntl.fcntl(descriptor, _F_GETPATH, b"\0" * 1024)
            raw_path = path_buffer.split(b"\0", 1)[0]
            if not raw_path:
                continue
            path = Path(os.fsdecode(raw_path)).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        resolved[descriptor] = path
    return resolved


def _tenant_descriptors(tenant_home: Path) -> dict[int, Path]:
    if sys.platform == "darwin":
        resolved = _resolved_macos_descriptors()
    elif sys.platform.startswith("linux"):
        resolved = _resolved_linux_descriptors()
    else:
        raise OSError(errno.ENOTSUP, "unsupported descriptor resolver")
    return {
        descriptor: path
        for descriptor, path in resolved.items()
        if _is_beneath(path, tenant_home)
    }


def _close_tenant_descriptors(tenant_home: Path) -> None:
    for descriptor in _tenant_descriptors(tenant_home):
        try:
            os.close(descriptor)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
    if _tenant_descriptors(tenant_home):
        raise PermissionError("tenant descriptor remained open")


def _profile_path(path: Path) -> str:
    raw = os.fspath(path)
    for character in raw:
        codepoint = ord(character)
        category = unicodedata.category(character)
        bidi = unicodedata.bidirectional(character)
        if (
            codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or 0xD800 <= codepoint <= 0xDFFF
            or codepoint in _BIDI_CONTROL_CODEPOINTS
            or category in {"Zl", "Zp"}
            or bidi in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI"}
        ):
            raise ValueError("path contains a profile control")
    return raw.replace("\\", "\\\\").replace('"', '\\"')


def _macos_profile(policy: WorkerIsolationPolicy) -> bytes:
    exclusions = []
    if policy.workspace is not None:
        exclusions.append(
            f'    (require-not (subpath "{_profile_path(policy.workspace)}"))'
        )
    exclusions.append(f'    (require-not (subpath "{_profile_path(policy.scratch)}"))')
    exclusions.append('    (require-not (literal "/dev/null"))')
    profile = (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*\n"
        "  (require-all\n"
        + "\n".join(exclusions)
        + "))\n"
    )
    return profile.encode("utf-8", "strict")


def _apply_macos(policy: WorkerIsolationPolicy) -> str:
    library = ctypes.CDLL("/usr/lib/libsandbox.1.dylib", use_errno=True)
    sandbox_init = library.sandbox_init
    sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_void_p)]
    sandbox_init.restype = ctypes.c_int
    sandbox_free_error = library.sandbox_free_error
    sandbox_free_error.argtypes = [ctypes.c_void_p]
    sandbox_free_error.restype = None
    error_buffer = ctypes.c_void_p()
    try:
        if sandbox_init(_macos_profile(policy), 0, ctypes.byref(error_buffer)) != 0:
            raise OSError(ctypes.get_errno(), "sandbox_init refused the literal profile")
    finally:
        if error_buffer.value:
            sandbox_free_error(error_buffer)
    return "macos-sandbox"


def _linux_syscall(library: ctypes.CDLL, number: int, *arguments: object) -> int:
    ctypes.set_errno(0)
    result = int(library.syscall(number, *arguments))
    if result < 0:
        error_number = ctypes.get_errno() or errno.EINVAL
        raise OSError(error_number, os.strerror(error_number))
    return result


def _apply_linux(policy: WorkerIsolationPolicy) -> str:
    create_ruleset_number, add_rule_number, restrict_self_number = (
        _landlock_syscall_numbers(platform.machine())
    )
    library = ctypes.CDLL(None, use_errno=True)
    library.syscall.restype = ctypes.c_long
    abi = _linux_syscall(
        library,
        create_ruleset_number,
        ctypes.c_void_p(),
        ctypes.c_size_t(0),
        ctypes.c_uint(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if abi < 3:
        raise OSError(errno.ENOTSUP, "Landlock ABI 3 is required")

    ruleset_attr = _LandlockRulesetAttr(handled_access_fs=WRITE_RIGHTS)
    ruleset_descriptor = -1
    try:
        ruleset_descriptor = _linux_syscall(
            library,
            create_ruleset_number,
            ctypes.byref(ruleset_attr),
            ctypes.sizeof(ruleset_attr),
            ctypes.c_uint(0),
        )
        for allowed_path in (policy.workspace, policy.scratch):
            if allowed_path is None:
                continue
            path_descriptor = -1
            try:
                path_descriptor = os.open(
                    allowed_path,
                    os.O_PATH | getattr(os, "O_CLOEXEC", 0),
                )
                path_attr = _LandlockPathBeneathAttr(
                    allowed_access=WRITE_RIGHTS,
                    parent_fd=path_descriptor,
                )
                _linux_syscall(
                    library,
                    add_rule_number,
                    ctypes.c_int(ruleset_descriptor),
                    ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                    ctypes.byref(path_attr),
                    ctypes.c_uint(0),
                )
            finally:
                if path_descriptor >= 0:
                    os.close(path_descriptor)

        # Every git process opens /dev/null O_RDWR at startup (git's sanitize_stdfds),
        # so without this rule the first git inside an activated boundary dies rc 128
        # and the receipt reads git_finalize_failed.  The macOS profile has carved the
        # same device out of its write-deny since 2026-08-14; this is that fix's Linux
        # half, not a new authority -- /dev/null is writable to this uid on any host and
        # discards everything written to it.  It cannot join the loop above: that passes
        # allowed_access=WRITE_RIGHTS, and Landlock refuses directory-only rights on a
        # non-directory.
        device_descriptor = -1
        try:
            device_descriptor = os.open(
                os.devnull,
                os.O_PATH | getattr(os, "O_CLOEXEC", 0),
            )
            device_attr = _LandlockPathBeneathAttr(
                allowed_access=(
                    _LANDLOCK_ACCESS_FS_WRITE_FILE | _LANDLOCK_ACCESS_FS_TRUNCATE
                ),
                parent_fd=device_descriptor,
            )
            _linux_syscall(
                library,
                add_rule_number,
                ctypes.c_int(ruleset_descriptor),
                ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH),
                ctypes.byref(device_attr),
                ctypes.c_uint(0),
            )
        finally:
            if device_descriptor >= 0:
                os.close(device_descriptor)

        library.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        library.prctl.restype = ctypes.c_int
        ctypes.set_errno(0)
        if library.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            error_number = ctypes.get_errno() or errno.EINVAL
            raise OSError(error_number, os.strerror(error_number))
        _linux_syscall(
            library,
            restrict_self_number,
            ctypes.c_int(ruleset_descriptor),
            ctypes.c_uint(0),
        )
    finally:
        if ruleset_descriptor >= 0:
            os.close(ruleset_descriptor)
    return f"linux-landlock-v{abi}"


def _post_activation_probes(policy: WorkerIsolationPolicy) -> None:
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError(errno.ENOTSUP, "O_NOFOLLOW is required")
    try:
        descriptor = os.open(policy.write_probe, os.O_WRONLY | nofollow)
    except OSError as exc:
        if exc.errno not in {errno.EACCES, errno.EPERM}:
            raise
    else:
        os.close(descriptor)
        raise PermissionError("tenant write probe unexpectedly opened")

    ready_path = policy.scratch / ".isolation-ready"
    descriptor = os.open(
        ready_path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow,
        0o600,
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.unlink(ready_path)


def apply_worker_isolation(policy: WorkerIsolationPolicy) -> str:
    """Apply the matching closed kernel backend to this process, or fail closed."""

    try:
        _validate_policy(policy)
        _close_tenant_descriptors(policy.tenant_home)
        _validate_policy(policy)
        if sys.platform == "darwin":
            backend = _apply_macos(policy)
        elif sys.platform.startswith("linux"):
            backend = _apply_linux(policy)
        else:
            raise OSError(errno.ENOTSUP, "unsupported Worker isolation platform")
        _post_activation_probes(policy)
        return backend
    except Exception as exc:
        if getattr(exc, "code", None) == _FAILURE_CODE:
            raise
        raise _failure(exc)

"""Direct exec launcher for effect-enabled Worker adapters."""

from __future__ import annotations

import fcntl
import hashlib
import os
import math
import signal
import socket
import stat
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .worker_bootstrap_protocol import BootstrapChannel
from .worker_errors import WorkerAdapterFailure


_NATIVE_LOADER_ENVIRONMENT_PREFIXES = ("DYLD_", "LD_")
_NATIVE_LOADER_ENVIRONMENT_KEYS = frozenset({"LIBPATH", "SHLIB_PATH"})
_PRELUDE_SOURCES = (
    ("floati.worker_errors", "worker_errors.py", 4),
    ("floati.worker_isolation", "worker_isolation.py", 5),
    ("floati.worker_bootstrap_protocol", "worker_bootstrap_protocol.py", 6),
    ("__main__", "worker_bootstrap.py", 7),
)
_MAX_PRELUDE_SOURCE_BYTES = 1_048_576
_BOOTSTRAP_LOADER = (
    "import hashlib,importlib.machinery,os,sys,types\n"
    "if len(sys.argv)!=6: raise SystemExit(126)\n"
    "e0,e1,e2,e3,p=sys.argv[1:6]\n"
    "if any(len(e)!=64 or any(c not in '0123456789abcdef' for c in e) for e in (e0,e1,e2,e3)): raise SystemExit(126)\n"
    "def r(fd):\n"
    " chunks=[]; total=0\n"
    " while True:\n"
    "  chunk=os.read(fd,65536)\n"
    "  if not chunk: break\n"
    "  total+=len(chunk)\n"
    "  if total>1048576: raise ValueError('oversized prelude')\n"
    "  chunks.append(chunk)\n"
    " if total==0: raise ValueError('empty prelude')\n"
    " return b''.join(chunks)\n"
    "try:\n"
    " s0,s1,s2,s3=r(4),r(5),r(6),r(7)\n"
    "except BaseException:\n"
    " for fd in (4,5,6,7):\n"
    "  try: os.close(fd)\n"
    "  except OSError: pass\n"
    " raise SystemExit(126)\n"
    "try:\n"
    " for fd in (4,5,6,7): os.close(fd)\n"
    "except OSError: raise SystemExit(126)\n"
    "if any(hashlib.sha256(s).hexdigest()!=e for s,e in ((s0,e0),(s1,e1),(s2,e2),(s3,e3))): raise SystemExit(126)\n"
    "names=('floati.worker_errors','floati.worker_isolation','floati.worker_bootstrap_protocol','__main__')\n"
    "files=(os.path.join(p,'worker_errors.py'),os.path.join(p,'worker_isolation.py'),os.path.join(p,'worker_bootstrap_protocol.py'),os.path.join(p,'worker_bootstrap.py'))\n"
    "try: codes=tuple(compile(s,f,'exec') for s,f in zip((s0,s1,s2,s3),files))\n"
    "except BaseException: raise SystemExit(126)\n"
    "pkg=types.ModuleType('floati'); pkg.__package__='floati'; pkg.__file__=os.path.join(p,'__init__.py')\n"
    "pkg.__path__=[p]; pkg.__spec__=importlib.machinery.ModuleSpec('floati',loader=None,is_package=True); pkg.__spec__.origin=pkg.__file__\n"
    "sys.modules['floati']=pkg\n"
    "mods=[]\n"
    "for n,f,c in zip(names[:3],files[:3],codes[:3]):\n"
    " m=types.ModuleType(n); m.__package__='floati'; m.__file__=f; m.__spec__=importlib.machinery.ModuleSpec(n,loader=None,origin=f); sys.modules[n]=m; mods.append(m); exec(c,m.__dict__,m.__dict__)\n"
    "preloaded=(pkg,mods[0],mods[1],mods[2])\n"
    "sys.argv=[files[3],'--fd','3']\n"
    "spec=importlib.machinery.ModuleSpec('__main__',loader=None,origin=files[3])\n"
    "namespace={'__name__':'__main__','__file__':files[3],'__package__':'floati','__spec__':spec,'_FLOATI_PRELOADED_MODULES':preloaded}\n"
    "exec(codes[3],namespace,namespace)\n"
)


@dataclass(frozen=True)
class _OpenedPreludeSource:
    module_name: str
    basename: str
    target_descriptor: int
    descriptor: int
    device: int
    inode: int
    digest: str


def _worker_environment() -> dict[str, str]:
    """Retain provider input while removing native-loader configuration."""

    return {
        name: value
        for name, value in os.environ.items()
        if name not in _NATIVE_LOADER_ENVIRONMENT_KEYS
        and not name.startswith(_NATIVE_LOADER_ENVIRONMENT_PREFIXES)
    }


def _validated_bootstrap_path(value: Path) -> Path:
    """Fail early on an invalid bootstrap selection before preparation."""

    if not isinstance(value, Path) or not value.is_absolute():
        raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
    try:
        metadata = os.lstat(value)
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise WorkerAdapterFailure("effect_worker_isolation_unavailable") from exc
    if (
        resolved != value
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
    return value


def _close_descriptors(descriptors: list[int]) -> None:
    failure: Optional[OSError] = None
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as exc:
            if failure is None:
                failure = exc
    if failure is not None:
        raise failure


def _open_validated_prelude(value: Path) -> tuple[_OpenedPreludeSource, ...]:
    """Bind the fixed pre-isolation source graph to owned regular files."""

    _validated_bootstrap_path(value)
    if value.name != _PRELUDE_SOURCES[-1][1]:
        raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
    package_path = value.parent
    package_descriptor: Optional[int] = None
    opened_records: list[_OpenedPreludeSource] = []
    try:
        package_metadata = os.lstat(package_path)
        if (
            package_path.resolve(strict=True) != package_path
            or stat.S_ISLNK(package_metadata.st_mode)
            or not stat.S_ISDIR(package_metadata.st_mode)
            or package_metadata.st_uid != os.geteuid()
            or package_metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_flags |= getattr(os, "O_NOFOLLOW", 0)
        package_descriptor = os.open(package_path, directory_flags)
        opened_package = os.fstat(package_descriptor)
        if (
            not stat.S_ISDIR(opened_package.st_mode)
            or opened_package.st_uid != os.geteuid()
            or opened_package.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or (opened_package.st_dev, opened_package.st_ino)
            != (package_metadata.st_dev, package_metadata.st_ino)
        ):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        source_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        source_flags |= getattr(os, "O_NOFOLLOW", 0)
        for module_name, basename, target_descriptor in _PRELUDE_SOURCES:
            descriptor = os.open(basename, source_flags, dir_fd=package_descriptor)
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_uid != os.geteuid()
                ):
                    raise WorkerAdapterFailure(
                        "effect_worker_isolation_unavailable"
                    )
                digest = hashlib.sha256()
                total = 0
                while True:
                    chunk = os.read(descriptor, 65_536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_PRELUDE_SOURCE_BYTES:
                        raise WorkerAdapterFailure(
                            "effect_worker_isolation_unavailable"
                        )
                    digest.update(chunk)
                if total == 0:
                    raise WorkerAdapterFailure(
                        "effect_worker_isolation_unavailable"
                    )
                os.lseek(descriptor, 0, os.SEEK_SET)
                opened_records.append(_OpenedPreludeSource(
                    module_name=module_name,
                    basename=basename,
                    target_descriptor=target_descriptor,
                    descriptor=descriptor,
                    device=metadata.st_dev,
                    inode=metadata.st_ino,
                    digest=digest.hexdigest(),
                ))
            except BaseException:
                os.close(descriptor)
                raise
        final_package = os.lstat(package_path)
        if (final_package.st_dev, final_package.st_ino) != (
            opened_package.st_dev,
            opened_package.st_ino,
        ):
            raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
        for record in opened_records:
            final_source = os.lstat(package_path / record.basename)
            if (
                stat.S_ISLNK(final_source.st_mode)
                or not stat.S_ISREG(final_source.st_mode)
                or final_source.st_uid != os.geteuid()
                or (final_source.st_dev, final_source.st_ino)
                != (record.device, record.inode)
            ):
                raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
    except BaseException as exc:
        cleanup = [record.descriptor for record in opened_records]
        if package_descriptor is not None:
            cleanup.append(package_descriptor)
        if cleanup:
            _close_descriptors(cleanup)
        if isinstance(exc, (OSError, RuntimeError)):
            raise WorkerAdapterFailure(
                "effect_worker_isolation_unavailable"
            ) from exc
        raise
    if package_descriptor is None:
        raise WorkerAdapterFailure("effect_worker_isolation_unavailable")
    try:
        os.close(package_descriptor)
    except OSError as exc:
        _close_descriptors([record.descriptor for record in opened_records])
        raise WorkerAdapterFailure(
            "effect_worker_isolation_unavailable"
        ) from exc
    return tuple(opened_records)


def _relocate_launch_descriptors(descriptors: list[int]) -> list[int]:
    """Move every spawn source outside the fixed descriptor target range."""

    relocated: list[int] = []
    try:
        for descriptor in descriptors:
            relocated.append(
                fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 8)
            )
    except BaseException:
        _close_descriptors([*relocated, *descriptors])
        raise
    try:
        _close_descriptors(descriptors)
    except BaseException:
        _close_descriptors(relocated)
        raise
    return relocated


class SpawnedWorkerProcess:
    """The exact child PID returned by one ``os.posix_spawn`` call."""

    def __init__(self, pid: int) -> None:
        if type(pid) is not int or pid <= 1:
            raise ValueError("invalid spawned Worker PID")
        self.pid = pid
        self.exitcode: Optional[int] = None
        self._parent_pid = os.getpid()
        self._waitable = True
        self._state_lock = threading.Lock()
        self._owns_process_group = False

    def confirm_process_group(self) -> bool:
        """Bind cleanup to the live bootstrap group before any wait/reap."""

        with self._state_lock:
            if self.exitcode is not None or not self._waitable:
                return False
            try:
                process_group = os.getpgid(self.pid)
            except ProcessLookupError:
                return False
            if process_group != self.pid:
                return False
            self._owns_process_group = True
            return True

    def _cache_status_locked(self, status: int) -> None:
        if os.WIFEXITED(status):
            self.exitcode = os.WEXITSTATUS(status)
        elif os.WIFSIGNALED(status):
            self.exitcode = -os.WTERMSIG(status)
        else:
            self.exitcode = 0
        self._waitable = False

    def _poll_locked(self) -> None:
        if self.exitcode is not None or not self._waitable:
            return
        try:
            waited_pid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._waitable = False
            return
        if waited_pid == self.pid:
            self._cache_status_locked(status)

    def _poll(self) -> None:
        with self._state_lock:
            self._poll_locked()

    def is_alive(self) -> bool:
        with self._state_lock:
            self._poll_locked()
            return self.exitcode is None and self._waitable

    def join(self, timeout: Optional[float] = None) -> None:
        if timeout is not None and (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or timeout < 0
        ):
            raise ValueError("invalid join timeout")
        deadline = (
            None if timeout is None else time.monotonic() + float(timeout)
        )
        first_poll = True
        while True:
            if deadline is None:
                acquired = self._state_lock.acquire()
            else:
                remaining = deadline - time.monotonic()
                if not first_poll and remaining <= 0:
                    return
                acquired = self._state_lock.acquire(
                    timeout=min(0.01, max(0.0, remaining)),
                )
            if not acquired:
                first_poll = False
                continue
            try:
                self._poll_locked()
                if self.exitcode is not None or not self._waitable:
                    return
            finally:
                self._state_lock.release()
            first_poll = False
            if deadline is None:
                time.sleep(0.01)
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.01, remaining))

    def _signal(self, signum: int) -> None:
        with self._state_lock:
            if self._owns_process_group:
                if self.exitcode is not None or not self._waitable:
                    return
                try:
                    os.killpg(self.pid, signum)
                except ProcessLookupError:
                    pass
                return
            self._poll_locked()
            if self.exitcode is not None or not self._waitable:
                return
            try:
                process_group = os.getpgid(self.pid)
            except ProcessLookupError:
                self._poll_locked()
                return
            if process_group == self.pid:
                try:
                    os.killpg(self.pid, signum)
                except ProcessLookupError:
                    self._poll_locked()
                return
            # The bootstrap calls setsid before reading launch input. If a
            # signal wins that short race, target only the exact still-waitable
            # child PID; never signal its inherited foreign process group.
            try:
                os.kill(self.pid, signum)
            except ProcessLookupError:
                self._poll_locked()

    def shutdown_process_group(self, *, grace_seconds: float = 0.1) -> bool:
        """Escalate the confirmed group before any operation can reap its leader."""

        if (
            not isinstance(grace_seconds, (int, float))
            or isinstance(grace_seconds, bool)
            or not math.isfinite(float(grace_seconds))
            or grace_seconds < 0
        ):
            raise ValueError("invalid process-group grace timeout")
        deadline = time.monotonic() + float(grace_seconds)
        with self._state_lock:
            if not self._owns_process_group:
                return False
            if self.exitcode is not None or not self._waitable:
                return True
            try:
                os.killpg(self.pid, signal.SIGTERM)
            except ProcessLookupError:
                return True
            while True:
                try:
                    os.killpg(self.pid, 0)
                except ProcessLookupError:
                    return True
                except PermissionError:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.01, remaining))
            try:
                os.killpg(self.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            return True

    def terminate(self) -> None:
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        self._signal(signal.SIGKILL)


def spawn_effect_worker(
    bootstrap_path: Path,
    launch_payload: dict[str, object],
) -> tuple[SpawnedWorkerProcess, BootstrapChannel]:
    """Spawn one isolated bootstrap and send its canonical launch document."""

    prelude = _open_validated_prelude(bootstrap_path)
    parent_socket: Optional[socket.socket] = None
    child_socket: Optional[socket.socket] = None
    source_descriptor: Optional[int] = None
    relocated: list[int] = []
    channel: Optional[BootstrapChannel] = None
    process: Optional[SpawnedWorkerProcess] = None
    try:
        digests = [record.digest for record in prelude]
        parent_socket, child_socket = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_STREAM,
        )
        parent_socket.set_inheritable(False)
        child_socket.set_inheritable(False)
        source_descriptor = child_socket.detach()
        child_socket = None
        launch_sources = [
            source_descriptor,
            *[record.descriptor for record in prelude],
        ]
        source_descriptor = None
        prelude = ()
        relocated = _relocate_launch_descriptors(launch_sources)
        file_actions = [
            (os.POSIX_SPAWN_DUP2, relocated[0], 3),
            *[
                (os.POSIX_SPAWN_DUP2, descriptor, target)
                for descriptor, target in zip(
                    relocated[1:],
                    (record[2] for record in _PRELUDE_SOURCES),
                )
            ],
            *[(os.POSIX_SPAWN_CLOSE, descriptor) for descriptor in relocated],
        ]
        pid = os.posix_spawn(
            sys.executable,
            [
                sys.executable, "-I", "-S", "-B", "-c", _BOOTSTRAP_LOADER,
                *digests,
                str(bootstrap_path.parent),
            ],
            _worker_environment(),
            file_actions=file_actions,
        )
        process = SpawnedWorkerProcess(pid)
        parent_launch_descriptors = relocated
        relocated = []
        _close_descriptors(parent_launch_descriptors)
        channel = BootstrapChannel(parent_socket.detach())
        parent_socket = None
        channel.send(("launch", launch_payload))
        return process, channel
    except BaseException:
        if channel is not None:
            channel.close()
        if process is not None:
            process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(1.0)
        raise
    finally:
        remaining_descriptors = [record.descriptor for record in prelude]
        remaining_descriptors.extend(relocated)
        if source_descriptor is not None:
            remaining_descriptors.append(source_descriptor)
        try:
            if remaining_descriptors:
                _close_descriptors(remaining_descriptors)
        finally:
            if child_socket is not None:
                child_socket.close()
            if parent_socket is not None:
                parent_socket.close()

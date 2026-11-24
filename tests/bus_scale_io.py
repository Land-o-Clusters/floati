"""Test-only application-visible I/O census; never a product fast path.

Counts bytes returned by the intercepted Python stream/read APIs, including
rereads. This is not a physical disk, buffered read-ahead, C-extension, socket
server, or subprocess census. CPU must be measured independently. Instrument
overhead is deliberately retained in the observed lock intervals.
"""
from __future__ import annotations

from contextlib import ExitStack
import builtins
import codecs
import fcntl
import io
import mmap
import os
from pathlib import Path
import stat
import subprocess
import threading
import time
from unittest.mock import patch


_ACTIVE = None


class _Stream:
    def __init__(self, probe, stream, entry, *, closefd, newline):
        self._probe = probe
        self._stream = stream
        self._entry = entry
        self._closefd = closefd
        self._newline = newline

    def __getattr__(self, name):
        if name in {"buffer", "raw", "detach"}:
            self._probe._unsupported("stream " + name + " bypasses read coverage")
        return getattr(self._stream, name)

    def __enter__(self):
        self._stream.__enter__()
        return self

    def __exit__(self, kind, value, traceback):
        self.close()
        return False

    def __iter__(self):
        iter(self._stream)
        return self

    def __next__(self):
        result = next(self._stream)
        self._returned(result)
        return result

    def _returned(self, result):
        if isinstance(result, str):
            encoding = codecs.lookup(self._stream.encoding).name
            if encoding not in {"utf-8", "ascii"} or self._stream.errors != "strict":
                self._probe._unsupported("text codec does not preserve exact returned-byte coverage")
            if self._newline is None:
                newlines = self._stream.newlines
                if newlines is not None and newlines != "\n":
                    self._probe._unsupported("universal newline translation obscures read-byte coverage")
            length = len(result.encode(encoding, "strict"))
        else:
            length = len(result)
        self._probe._count(self._entry, length)

    def read(self, *args, **kwargs):
        result = self._stream.read(*args, **kwargs)
        self._returned(result)
        return result

    def read1(self, *args, **kwargs):
        result = self._stream.read1(*args, **kwargs)
        self._returned(result)
        return result

    def readall(self, *args, **kwargs):
        result = self._stream.readall(*args, **kwargs)
        self._returned(result)
        return result

    def readline(self, *args, **kwargs):
        result = self._stream.readline(*args, **kwargs)
        self._returned(result)
        return result

    def readlines(self, *args, **kwargs):
        result = self._stream.readlines(*args, **kwargs)
        for line in result:
            self._returned(line)
        return result

    def readinto(self, *args, **kwargs):
        result = self._stream.readinto(*args, **kwargs)
        if result is not None:
            self._probe._count(self._entry, result)
        return result

    def readinto1(self, *args, **kwargs):
        result = self._stream.readinto1(*args, **kwargs)
        if result is not None:
            self._probe._count(self._entry, result)
        return result

    def peek(self, *args, **kwargs):
        self._probe._unsupported("peek does not establish consumed-byte coverage")

    def close(self):
        if self._stream.closed:
            return self._stream.close()
        before = time.monotonic_ns()
        try:
            return self._stream.close()
        finally:
            after = time.monotonic_ns()
            if self._closefd and self._stream.closed:
                self._probe._closed(self._entry, before, after)


class IOProbe:
    """Own one nonnested, process-global Python-I/O instrumentation interval.

    Public path keys are root-relative POSIX names. ``holds`` contains completed
    successful flock intervals, with shared/exclusive mode and acquisition /
    release call brackets. ``assert_complete`` means the intercepted seams
    completed without an unsupported operation or outstanding owned descriptor;
    it does not certify unknown native/C I/O paths. An explicit external child
    policy may approve a caller-owned launch outside the bus census; approved
    children must be collected before completeness can be asserted.
    """
    def __init__(self, root: Path, *, external_process_policy=None):
        self.root = Path(root)
        if (not self.root.is_absolute() or self.root.resolve(strict=True) != self.root
                or not self.root.is_dir()):
            raise ValueError("IOProbe requires an existing canonical absolute root")
        self.read_bytes_by_path = {}
        self.holds = []
        self.external_processes = []
        self._external_children = []
        self._external_process_policy = external_process_policy
        self._approved_spawn = threading.local()
        self._issues = []
        self._descriptors = {}
        self._open_holds = {}
        self._generation = 0
        self._guard = threading.RLock()
        self._stack = None
        self._entered = False
        self._finished = False
        self._creator = os.getpid()
        self._fstat = os.fstat
        self._close = os.close
        self._seed_paths = {}
        for path in (self.root, *self.root.rglob("*")):
            metadata = path.lstat()
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode):
                self._seed_paths.setdefault((metadata.st_dev, metadata.st_ino), []).append(path)

    def _unsupported(self, detail):
        with self._guard:
            self._issues.append(detail)
        raise RuntimeError("unsupported I/O coverage: " + detail)

    def _process(self):
        if os.getpid() != self._creator:
            self._unsupported("probe inherited by an uninstrumented process")

    def _relative(self, path):
        if path is None:
            return None
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return None

    def _path(self, value, dir_fd=None):
        if isinstance(value, int):
            entry = self._entry(value)
            return None if entry is None else entry["absolute"]
        path = Path(os.fsdecode(os.fspath(value)))
        if not path.is_absolute():
            if dir_fd is None:
                path = Path.cwd() / path
            else:
                parent = self._entry(dir_fd)
                if parent is None or parent["absolute"] is None:
                    self._unsupported("relative descriptor open has no bound parent path")
                path = parent["absolute"] / path
        return path.resolve(strict=False)

    def _bind(self, descriptor, path, *, fresh, opened_here):
        self._process()
        info = self._fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        with self._guard:
            prior = self._descriptors.get(descriptor)
            if not fresh and prior is not None:
                if prior["identity"] != identity:
                    self._unsupported("descriptor identity changed without an observed close")
                return prior
            if prior is not None and (descriptor, prior["generation"]) in self._open_holds:
                self._unsupported("descriptor reused while its previous flock is unresolved")
            self._generation += 1
            entry = {"fd": descriptor, "generation": self._generation,
                     "identity": identity, "absolute": path, "path": self._relative(path),
                     "ordinary": stat.S_ISREG(info.st_mode), "opened_here": opened_here}
            self._descriptors[descriptor] = entry
            return entry

    def _entry(self, descriptor):
        self._process()
        with self._guard:
            entry = self._descriptors.get(descriptor)
        info = self._fstat(descriptor)
        identity = (info.st_dev, info.st_ino)
        if entry is not None:
            if entry["identity"] != identity:
                self._unsupported("descriptor generation no longer owns its original inode")
            return entry
        candidates = self._seed_paths.get(identity, [])
        if len(candidates) > 1:
            self._unsupported("preexisting descriptor has ambiguous hard-linked paths")
        if candidates:
            return self._bind(descriptor, candidates[0], fresh=False, opened_here=False)
        return None

    def _count(self, entry, length):
        self._process()
        if entry is None or entry["path"] is None or not entry["ordinary"] or length == 0:
            return
        current = self._entry(entry["fd"])
        if current is not entry:
            self._unsupported("read belongs to a closed or reused descriptor generation")
        with self._guard:
            path = entry["path"]
            self.read_bytes_by_path[path] = self.read_bytes_by_path.get(path, 0) + length

    def _finish_hold(self, entry, before, after, release):
        key = (entry["fd"], entry["generation"])
        with self._guard:
            hold = self._open_holds.pop(key, None)
            if hold is not None:
                hold.update(release_call_before_ns=before, release_call_after_ns=after,
                            release=release,
                            seconds=(after - hold["acquire_call_after_ns"]) / 1e9)
                self.holds.append(hold)

    def _closed(self, entry, before, after):
        if entry is None:
            return
        with self._guard:
            if self._descriptors.get(entry["fd"]) is not entry:
                self._unsupported("close belongs to a replaced descriptor generation")
            self._finish_hold(entry, before, after, "close")
            del self._descriptors[entry["fd"]]

    @staticmethod
    def _argument(args, kwargs, name, index, default):
        return kwargs.get(name, args[index] if len(args) > index else default)

    def _opener(self, original):
        def opened(file, *args, **kwargs):
            # Native validation/open errors happen before any synthetic entry.
            stream = original(file, *args, **kwargs)
            if isinstance(stream, _Stream):
                return stream
            try:
                path = self._path(file)
                entry = self._bind(stream.fileno(), path, fresh=not isinstance(file, int),
                                   opened_here=not isinstance(file, int))
                if entry["path"] is None:
                    # The raw outside-root stream owns its native close. It
                    # must not leave a stale entry when that fd is later reused.
                    with self._guard:
                        if self._descriptors.get(entry["fd"]) is entry:
                            del self._descriptors[entry["fd"]]
                    return stream
                closefd = self._argument(args, kwargs, "closefd", 5, True)
                newline = self._argument(args, kwargs, "newline", 4, None)
                return _Stream(self, stream, entry, closefd=closefd, newline=newline)
            except BaseException:
                stream.close()
                raise
        return opened

    def __enter__(self):
        global _ACTIVE
        if self._entered or _ACTIVE is not None:
            raise RuntimeError("IOProbe intervals cannot be reused or nested")
        self._entered = True
        self._stack = ExitStack()
        _ACTIVE = self
        original_open = os.open
        original_read = os.read
        original_flock = fcntl.flock
        original_fcntl = fcntl.fcntl
        original_popen_init = subprocess.Popen.__init__

        def opened(path, flags, mode=0o777, *, dir_fd=None):
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            try:
                self._bind(descriptor, self._path(path, dir_fd), fresh=True, opened_here=True)
            except BaseException:
                self._close(descriptor)
                raise
            return descriptor

        def read(descriptor, size):
            result = original_read(descriptor, size)
            self._count(self._entry(descriptor), len(result))
            return result

        def close(descriptor):
            entry = self._entry(descriptor)
            before = time.monotonic_ns()
            result = self._close(descriptor)
            self._closed(entry, before, time.monotonic_ns())
            return result

        def flock(descriptor, operation):
            before = time.monotonic_ns()
            result = original_flock(descriptor, operation)
            after = time.monotonic_ns()
            entry = self._entry(descriptor)
            if entry is None or entry["path"] is None:
                return result
            if operation & fcntl.LOCK_UN:
                self._finish_hold(entry, before, after, "unlock")
                return result
            mode = "exclusive" if operation & fcntl.LOCK_EX else "shared"
            key = (descriptor, entry["generation"])
            with self._guard:
                prior = self._open_holds.get(key)
                if prior is not None:
                    if prior["mode"] != mode:
                        self._unsupported("flock mode conversion has an unresolved acquisition boundary")
                    return result
                self._open_holds[key] = {
                    "path": entry["path"], "mode": mode, "fd": descriptor,
                    "generation": entry["generation"], "device": entry["identity"][0],
                    "inode": entry["identity"][1], "acquire_call_before_ns": before,
                    "acquire_call_after_ns": after,
                }
            return result

        def forbidden(name):
            def refuse(*args, **kwargs):
                self._unsupported(name)
            return refuse

        def descriptor_control(descriptor, command, *args):
            duplicators = {fcntl.F_DUPFD}
            if hasattr(fcntl, "F_DUPFD_CLOEXEC"):
                duplicators.add(fcntl.F_DUPFD_CLOEXEC)
            if command in duplicators:
                self._unsupported("descriptor duplication has unmodeled shared lock lifetime")
            return original_fcntl(descriptor, command, *args)

        def popen_started(process, argv, *positional, **kwargs):
            # A policy sees every accepted launch option. Positional options,
            # shell strings and non-string argv entries are deliberately not
            # part of this narrow diagnostic escape hatch.
            if (positional or not isinstance(argv, (list, tuple)) or not argv
                    or not all(isinstance(item, str) for item in argv)):
                self._unsupported("subprocess spawn has unsupported argv/options")
            policy = self._external_process_policy
            if policy is None:
                self._unsupported("subprocess spawn")
            try:
                approved = policy(tuple(argv), dict(kwargs)) is True
            except Exception as error:
                self._unsupported("external subprocess policy failed: " + type(error).__name__)
            if not approved:
                self._unsupported("external subprocess policy denied spawn")
            cwd = kwargs.get("cwd")
            cwd = str(Path.cwd() if cwd is None else Path(os.fsdecode(os.fspath(cwd))).absolute())
            row = {"argv": list(argv), "cwd": cwd, "status": "launching"}
            self.external_processes.append(row)
            self._approved_spawn.active = True
            try:
                result = original_popen_init(process, argv, **kwargs)
            except BaseException:
                row["status"] = "launch_failed"
                raise
            finally:
                self._approved_spawn.active = False
            row.update(status="launched", pid=process.pid)
            self._external_children.append((process, row))
            return result

        def spawn_gate(name, original):
            def guarded(*args, **kwargs):
                # Some stdlib Popen paths use posix_spawn. Approval is confined
                # to that constructor's current thread and cannot admit a
                # separately requested fork/system/exec operation.
                if name in {"posix_spawn", "posix_spawnp"} and getattr(self._approved_spawn, "active", False):
                    return original(*args, **kwargs)
                self._unsupported("process spawn " + name)
            return guarded

        try:
            for owner, name, replacement in (
                (builtins, "open", self._opener(builtins.open)),
                (io, "open", self._opener(io.open)),
                (os, "fdopen", self._opener(os.fdopen)),
                (os, "open", opened), (os, "read", read), (os, "close", close),
                (fcntl, "flock", flock), (mmap, "mmap", forbidden("mmap")),
                (fcntl, "fcntl", descriptor_control),
                (io, "FileIO", forbidden("direct FileIO bypasses stream coverage")),
                (os, "dup", forbidden("descriptor duplication")),
                (os, "dup2", forbidden("descriptor duplication")),
                (subprocess.Popen, "__init__", popen_started),
            ):
                self._stack.enter_context(patch.object(owner, name, replacement))
            for name in ("pread", "preadv", "readv", "sendfile", "copy_file_range"):
                if hasattr(os, name):
                    self._stack.enter_context(patch.object(os, name, forbidden("uncovered read API " + name)))
            for name in ("fork", "forkpty", "posix_spawn", "posix_spawnp", "system",
                         "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve",
                         "spawnvp", "spawnvpe", "execl", "execle", "execlp", "execlpe",
                         "execv", "execve", "execvp", "execvpe"):
                if hasattr(os, name):
                    self._stack.enter_context(patch.object(os, name, spawn_gate(name, getattr(os, name))))
        except BaseException:
            self._stack.close()
            _ACTIVE = None
            raise
        return self

    def __exit__(self, kind, value, traceback):
        global _ACTIVE
        try:
            self._stack.close()
        finally:
            self._finished = True
            _ACTIVE = None
        # Do not replace a native IntegrityFailure or other original exception.
        return False

    def assert_complete(self):
        self._process()
        with self._guard:
            if not self._entered or not self._finished:
                raise RuntimeError("I/O coverage interval is unfinished")
            if self._issues:
                raise RuntimeError("unsupported I/O coverage: " + "; ".join(self._issues))
            if self._open_holds:
                raise RuntimeError("I/O coverage has unfinished successful flock intervals")
            for process, row in self._external_children:
                row["returncode"] = process.poll()
                if row["returncode"] is None:
                    raise RuntimeError("I/O coverage has an unfinished approved external subprocess")
            if any(entry["opened_here"] and entry["path"] is not None
                   for entry in self._descriptors.values()):
                raise RuntimeError("I/O coverage has unclosed owned descriptors")

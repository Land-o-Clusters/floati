"""Process-owned test scratch, including allocations made by exercised code.

Install before importing a test module. Keeping the stdlib entry points means
direct imports and TemporaryDirectory retain their normal API. The audit hook
refuses a cached, unwrapped allocator rather than silently losing ownership.
Production verbs do not install this test-only boundary. Launchers with their
own child teardown use owned_mkdtemp and cleanup_path at their safe boundary.
"""

from __future__ import annotations

import atexit
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import weakref


# The launcher imports its sibling; tests import through the scripts namespace.
# Both spellings must share one registry and one audit admission flag.
sys.modules.setdefault("fixture_finalizer", sys.modules[__name__])
sys.modules.setdefault("scripts.fixture_finalizer", sys.modules[__name__])

_native_mkdtemp = tempfile.mkdtemp
_native_mkstemp = tempfile.mkstemp
_native_mkstemp_inner = tempfile._mkstemp_inner
_native_directory_init = tempfile.TemporaryDirectory.__init__
_native_directory_cleanup = tempfile.TemporaryDirectory.cleanup
_lock = threading.RLock()
_local = threading.local()
_pid = os.getpid()
_owned = {}
_installed = False
_closing = False
_boundaries = set()
_boundary_ids = set()


@contextmanager
def owned_child(*arguments, **options):
    """Launch and collect one direct child across SIGINT and SIGTERM.

    Constructor-time signals record intent until the retained Popen object
    has adopted its PID. Exec never inherits a mask added by this helper.
    During collection, repeated signals defer their previous handlers until
    the child is reaped. No descendant discovery or forced kill is involved.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("fixture_finalizer_child_requires_main_thread")
    numbers = (signal.SIGINT, signal.SIGTERM)
    previous = {}
    pending = []
    phase = "constructing"
    forwarded = False
    failure = None
    constructor_interruption = False
    process = subprocess.Popen.__new__(subprocess.Popen)

    def remember(number, frame):
        if not any(saved == number for saved, _frame in pending):
            pending.append((number, frame))

    def prior_handler(number, frame):
        prior = previous[number]
        if callable(prior):
            prior(number, frame)
        elif prior != signal.SIG_IGN:
            if number == signal.SIGINT:
                raise KeyboardInterrupt
            # A native default SIGTERM would skip Python finalizers entirely.
            raise SystemExit(128 + number)

    def forward(number):
        nonlocal forwarded
        if getattr(process, "_child_created", False) and getattr(process, "pid", None) is not None:
            process.send_signal(number)
            forwarded = True

    def interrupted(number, frame):
        nonlocal phase
        if phase != "active":
            remember(number, frame)
            return
        phase = "delivering"
        try:
            forward(number)
            prior_handler(number, frame)
        finally:
            phase = "active"

    before = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    try:
        for number in numbers:
            previous[number] = signal.signal(number, interrupted)
    except BaseException:
        for number, original in previous.items():
            signal.signal(number, original)
        raise
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
    try:
        # Retain the object before construction: even a constructor exception
        # after fork leaves the exact native child reachable for collection.
        process.__init__(*arguments, **options)
        phase = "active"
        while pending:
            interrupted(*pending.pop(0))
        yield process
    except BaseException as error:
        failure = error
        constructor_interruption = phase == "constructing" and bool(pending)
        raise
    finally:
        phase = "collecting"
        try:
            if getattr(process, "_child_created", False) and getattr(process, "pid", None) is not None:
                if pending and not forwarded:
                    forward(pending[0][0])
                elif failure is not None and not forwarded:
                    forward(signal.SIGTERM)
                # Drain open PIPEs as well as waiting. Python 3.9's single-
                # pipe fast path closes its stream without setting the
                # communication flag; a second communicate() then reads a
                # closed file. Already-drained children need only wait().
                streams = (process.stdin, process.stdout, process.stderr)
                if process.poll() is None or any(stream is not None and not stream.closed
                                                  for stream in streams):
                    process.communicate()
                else:
                    process.wait()
        finally:
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
            try:
                # Consume only signals newly deferred by our restoration
                # mask. Preserve a caller's deliberately blocked signals.
                for number in signal.sigpending().intersection(numbers):
                    if number not in blocked:
                        signal.sigwait((number,))
                        remember(number, None)
                for number, original in previous.items():
                    signal.signal(number, original)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        deferred_error = None
        for number, frame in pending:
            try:
                prior_handler(number, frame)
            except BaseException as error:
                if deferred_error is None:
                    deferred_error = error
        if deferred_error is not None and (failure is None or constructor_interruption):
            raise deferred_error


def run_child(*arguments, timeout=None, check=False, capture_output=False,
              timeout_result=False, **options):
    """Return CompletedProcess; a timeout requests graceful SIGINT and reaps.

    SIGINT preserves native profiling and fixture finalization. Collection
    deliberately has no forced-kill deadline: only this retained child is
    signaled, and its own supervisor owns descendant teardown. As with run(),
    timeouts raise TimeoutExpired after collection unless timeout_result asks
    for the interrupted child's result (the profiling witness's contract).
    """
    if capture_output:
        if options.get("stdout") is not None or options.get("stderr") is not None:
            raise ValueError("stdout and stderr cannot be combined with capture_output")
        options.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    expired = None
    with owned_child(*arguments, **options) as process:
        try:
            output, error = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as timeout_error:
            expired = timeout_error
            process.send_signal(signal.SIGINT)
            output, error = process.communicate()
        completed = subprocess.CompletedProcess(process.args, process.returncode, output, error)
    if expired is not None and not timeout_result:
        expired.output, expired.stderr = output, error
        raise expired
    if check:
        completed.check_returncode()
    return completed


@contextmanager
def _allocation():
    """Publish ownership before a pending termination can be delivered."""
    numbers = (signal.SIGINT, signal.SIGTERM)
    before = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    try:
        with _lock:
            if _closing:
                raise RuntimeError("fixture_finalizer_closed")
            _local.admitted = getattr(_local, "admitted", 0) + 1
            try:
                yield
            finally:
                _local.admitted -= 1
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)


def _remember(path):
    path = os.path.abspath(os.fsdecode(path))
    info = os.lstat(path)
    _owned[path] = (os.getpid(), info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))


def owned_mkdtemp(*args, **kwargs):
    with _allocation():
        path = _native_mkdtemp(*args, **kwargs)
        _remember(path)
        return path


def owned_mkstemp(*args, **kwargs):
    with _allocation():
        descriptor, path = _native_mkstemp(*args, **kwargs)
        _remember(path)
        return descriptor, path


def _owned_mkstemp_inner(*args, **kwargs):
    # TemporaryFile and NamedTemporaryFile use the same audited allocator.
    # Cover them too without changing their wrapper or descriptor semantics.
    with _allocation():
        descriptor, path = _native_mkstemp_inner(*args, **kwargs)
        _remember(path)
        return descriptor, path


def _allocation_metadata(path, identity):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    observed = (os.getpid(), info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
    if observed != identity:
        raise ValueError("fixture_finalizer_replaced: " + path)
    return info


def _finalize_in_creator(path, identity, callback, args, kwargs):
    if identity[0] != os.getpid():
        return
    with _lock:
        try:
            info = _allocation_metadata(path, identity)
        except ValueError:
            # The exit sweep reports replacements at the ownership boundary.
            return
        if info is not None:
            callback(path, *args[1:], **kwargs)


def _directory_init(self, *args, **kwargs):
    with _allocation():
        _native_directory_init(self, *args, **kwargs)
        self._fixture_path = os.path.abspath(os.fsdecode(self.name))
        self._fixture_identity = _owned[self._fixture_path]
        # Preserve native arguments across supported Python versions. Merely
        # resetting our registry at fork would leave this inherited native
        # weakref callback able to delete the parent's still-live directory.
        _object, callback, arguments, keywords = self._finalizer.detach()
        self._finalizer = weakref.finalize(
            self, _finalize_in_creator, self._fixture_path, self._fixture_identity,
            callback, arguments, keywords,
        )


def _directory_cleanup(self):
    identity = getattr(self, "_fixture_identity", None)
    if identity is None:
        return _native_directory_cleanup(self)
    path = self._fixture_path
    if identity[0] != os.getpid() or os.path.abspath(os.fsdecode(self.name)) != path:
        self._finalizer.detach()
        return
    with _lock:
        if _allocation_metadata(path, identity) is not None:
            return _native_directory_cleanup(self)
        self._finalizer.detach()


def retain_path(path):
    """Relinquish deletion when a launcher's child ownership is unproved."""
    with _lock:
        _owned.pop(os.path.abspath(os.fsdecode(path)), None)


def cleanup_path(path):
    """Remove only this process's original inode; never follow a replacement."""
    path = os.path.abspath(os.fsdecode(path))
    with _lock:
        identity = _owned.get(path)
        if identity is None:
            raise ValueError("fixture_finalizer_unowned: " + path)
        if identity[0] != os.getpid():
            raise ValueError("fixture_finalizer_foreign_process: " + path)
        info = _allocation_metadata(path, identity)
        if info is None:
            return
        if stat.S_ISDIR(info.st_mode):
            # TemporaryDirectory already handles read-only Git fixture files.
            tempfile.TemporaryDirectory._rmtree(path)
        else:
            os.unlink(path)


def cleanup():
    """Drain every owned allocation, even if one cleanup refuses or fails."""
    global _closing
    if os.getpid() != _pid:
        return
    numbers = (signal.SIGINT, signal.SIGTERM)
    before = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    errors = []
    try:
        with _lock:
            _closing = True
            # An owned directory authorizes its contents. Drain only maximal
            # roots: nested transaction stages may legitimately have moved or
            # been replaced, and their parent already owns their removal. A
            # refused parent must also preserve every descendant beneath it.
            directories = {path for path, identity in _owned.items()
                           if identity[0] == os.getpid() and identity[3] == stat.S_IFDIR}
            for path in sorted(_owned):
                if any(str(parent) in directories for parent in Path(path).parents):
                    continue
                try:
                    cleanup_path(path)
                except Exception as error:
                    errors.append(str(error))
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
    if errors:
        raise RuntimeError("fixture_finalizer_cleanup_failed: " + "; ".join(errors))


def _at_exit():
    try:
        cleanup()
    except Exception as error:
        print(str(error), file=sys.stderr)
        # atexit normally discards failures; a leaking test must fail instead.
        os._exit(1)


def _after_fork():
    global _pid, _owned, _lock, _local, _closing
    _pid, _owned = os.getpid(), {}
    _lock, _local = threading.RLock(), threading.local()
    _closing = False


def _audit(event, arguments):
    if event in ("tempfile.mkdtemp", "tempfile.mkstemp"):
        if not getattr(_local, "admitted", 0):
            raise RuntimeError("fixture_finalizer_unowned_allocator: " + os.fsdecode(arguments[0]))
    elif event == "os.mkdir" and not getattr(_local, "admitted", 0):
        path, _mode, directory_fd = arguments
        name = os.fsdecode(path)
        if os.path.isabs(name) or directory_fd == -1:
            outside = os.path.realpath(os.path.dirname(os.path.abspath(name))) in _boundaries
        else:
            # Resolve only the relative parent through the supplied descriptor;
            # no /proc dependency and no scan of another process's descriptors.
            parent = os.path.dirname(name) or "."
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY, dir_fd=directory_fd)
            try:
                info = os.fstat(descriptor)
                outside = (info.st_dev, info.st_ino) in _boundary_ids
            finally:
                os.close(descriptor)
        if outside:
            # pathlib's mkdir(exist_ok=True) also emits an event for an
            # already-created owned root. It creates nothing and must retain
            # the normal stdlib API rather than looking like a new bypass.
            try:
                info = os.stat(name, dir_fd=None if directory_fd == -1 else directory_fd,
                               follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                identity = (os.getpid(), info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
                if identity in _owned.values():
                    return
            raise RuntimeError("fixture_finalizer_unowned_root: " + os.fsdecode(path))


def _interrupt(number, frame):
    # Unwind fixture contexts and child teardown first; atexit drains bare
    # allocations even when unittest catches KeyboardInterrupt to report it.
    if number == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + number)


def install(*, temp_root, platform_root=None):
    """Attach once at test bootstrap, before the suite HOME is allocated."""
    global _installed
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("fixture_finalizer_requires_main_thread")
    native_root = "\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp"
    for boundary in (temp_root, platform_root or temp_root, native_root, tempfile.gettempdir()):
        boundary = os.path.realpath(os.fsdecode(boundary))
        info = os.stat(boundary)
        _boundaries.add(boundary)
        _boundary_ids.add((info.st_dev, info.st_ino))
    if _installed:
        return
    tempfile.mkdtemp = owned_mkdtemp
    tempfile.mkstemp = owned_mkstemp
    tempfile._mkstemp_inner = _owned_mkstemp_inner
    tempfile.TemporaryDirectory.__init__ = _directory_init
    tempfile.TemporaryDirectory.cleanup = _directory_cleanup
    sys.addaudithook(_audit)
    for number in (signal.SIGINT, signal.SIGTERM):
        prior = signal.getsignal(number)
        if prior in (signal.SIG_DFL, signal.SIG_IGN, signal.default_int_handler):
            signal.signal(number, _interrupt)
        # An orchestrator's explicit handler owns its cancellation boundary;
        # its normal process exit still drains our atexit registry afterward.
    _installed = True


# Explicitly-owned launcher allocations also need normal-exit protection.
# Forked workers must never inherit authority to delete the parent's scratch.
atexit.register(_at_exit)
os.register_at_fork(after_in_child=_after_fork)

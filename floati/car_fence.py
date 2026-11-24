"""Descriptor-confined fence shared by car ownership and execution notices."""
from contextlib import ExitStack, contextmanager
import fcntl
import grp
import os
import pwd
from pathlib import Path
import shlex
import stat
import sys
import time

from .bus_epoch import LOCK_ORDER_WAKE, epoch_guard, lock_order_guard
from .errors import ProtocolRefusal


_COORDINATE = "work/car-acceptance.lock"
_LEAF = "car-acceptance.lock"
_TIMEOUT = 5.0
_NSSWITCH_PATH = Path("/etc/nsswitch.conf")
_PAM_GROUP_CONFIGURATION_PATH = Path("/etc/security/group.conf")


def _invalid(detail, *, remedy="park car mutations and have the coordinator inspect the named ownership or path boundary"):
    return ProtocolRefusal("car_acceptance_fence_invalid", detail,
        remedy=remedy)


def _changed():
    return ProtocolRefusal("car_acceptance_fence_changed",
        _COORDINATE + " or its descriptor-bound parent changed",
        remedy="park car mutations and have the coordinator reconcile the changed descriptor-bound path")


def _identity(info):
    return info.st_dev, info.st_ino


def _read_ordinary_configuration(path, *, nofollow=False):
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
    if nofollow:
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise OSError("account configuration must be an ordinary file")
        return stream.read()


def _empty_group_configuration(path):
    data = _read_ordinary_configuration(path, nofollow=True)
    # Only LF-delimited blank and comment lines testify to no group grants.
    # Unicode whitespace must not disguise an effective rule as a blank line.
    return all(not line.strip(b" \t\r") or line.lstrip(b" \t\r").startswith(b"#")
               for line in data.split(b"\n"))


def _local_account_database():
    """Enumerations testify only with the ruled Linux local-source allowlist.

    Missing, duplicate or unrecognised configuration fails closed. Optional
    initgroups sources and PAM group grants must not escape the testimony.
    NSS action clauses are not interpreted as account testimony.
    """
    if sys.platform != "linux":
        return False
    try:
        configuration = _NSSWITCH_PATH.read_bytes().decode("utf-8")
    except (OSError, UnicodeError):
        return False
    databases = set()
    # NSS consumes LF-delimited lines. Text universal-newline conversion or
    # splitlines() could turn whitespace before a remote source into a new,
    # ignored database line and falsely certify a partial enumeration.
    for line in configuration.split("\n"):
        line = line.split("#", 1)[0].strip()
        name, separator, sources = line.partition(":")
        name = name.strip()
        if name not in {"passwd", "group", "initgroups"}:
            # A colonless/otherwise malformed account entry must not hide
            # behind an earlier local definition of the same database.
            if line and line.split()[0] in {"passwd", "group", "initgroups"}:
                return False
            continue
        tokens = sources.split()
        if (not separator or name in databases or not tokens
                or any(source not in {"files", "systemd"} for source in tokens)):
            return False
        databases.add(name)
    if not {"passwd", "group"} <= databases:
        return False
    try:
        # Linux-PAM reads this default regardless of where pam_group is
        # loaded. A partial stack scan cannot establish absence of grants,
        # and a missing default cannot testify about a vendor fallback.
        return _empty_group_configuration(_PAM_GROUP_CONFIGURATION_PATH)
    except (OSError, ValueError):
        return False


def _private_group(info):
    """Require the owner's primary group and complete, sole-owner testimony.

    gr_mem omits primary-group users, so it cannot establish exclusivity
    alone. An enumeration without the resolved owner is not testimony.
    """
    if not _local_account_database():
        return False
    try:
        group = grp.getgrgid(info.st_gid)
        owner = pwd.getpwuid(info.st_uid)
        accounts = pwd.getpwall()
    except (KeyError, OSError):
        return False
    identity = (owner.pw_uid, owner.pw_name, owner.pw_gid)
    return (group.gr_gid == info.st_gid and owner.pw_uid == info.st_uid
        and any((user.pw_uid, user.pw_name, user.pw_gid) == identity for user in accounts)
        and owner.pw_gid == info.st_gid
        and all(name == owner.pw_name for name in group.gr_mem)
        and all(user.pw_uid == info.st_uid for user in accounts
                if user.pw_gid == info.st_gid))


def _owned(info, *, directory=False, tenant_home=None):
    ordinary = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if (not ordinary or info.st_uid != os.geteuid()
            or (not directory and (info.st_nlink != 1 or info.st_size != 0))):
        raise _invalid(_COORDINATE + " requires owned ordinary, non-foreign coordinates")
    if info.st_mode & 0o002 or (info.st_mode & 0o020 and not _private_group(info)):
        if tenant_home is not None:
            raise _invalid(str(tenant_home) + " requires a private tenant home",
                remedy="chmod 700 " + shlex.quote(str(tenant_home)))
        raise _invalid(_COORDINATE + " requires owned ordinary, non-foreign coordinates")


def _directory(parent, name, stack):
    descriptor = os.open(name,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
        dir_fd=parent)
    stack.callback(os.close, descriptor)
    return descriptor


def _check_chain(edges):
    for parent, name, child in edges:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or _identity(info) != _identity(os.fstat(child)):
            raise _changed()


def _check_fence(edges, parent, descriptor):
    _check_chain(edges)
    _owned(os.fstat(parent), directory=True)
    current = os.stat(_LEAF, dir_fd=parent, follow_symlinks=False)
    opened = os.fstat(descriptor)
    _owned(current)
    _owned(opened)
    if _identity(current) != _identity(opened):
        raise _changed()


def _check_absence(edges, parent, leaf):
    _check_chain(edges)
    try:
        os.stat(leaf, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise _changed()


def _acquire(descriptor, *, observe, deadline):
    from .waiter_lock_retry import current_lock_window, WaiterLockDeadline
    window = current_lock_window()
    if window is not None:
        deadline = min(deadline, time.monotonic() + window.remaining())
    operation = fcntl.LOCK_SH if observe else fcntl.LOCK_EX
    while True:
        try:
            fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
            break
        except BlockingIOError as exc:
            if window is not None:
                window.contended = True
            if time.monotonic() >= deadline or (window is not None and window.remaining() <= 0):
                failure = ProtocolRefusal("ledger_lock_timeout",
                    _COORDINATE + " lock remained contended for 5 seconds",
                    remedy={"kind": "transient", "action": "retry_once",
                            "delay_seconds": 1, "holder": "unknown"})
                if window is not None and window.remaining() <= 0:
                    raise WaiterLockDeadline(_COORDINATE) from failure
                raise failure from exc
            if window is None:
                time.sleep(min(0.01, max(0, deadline - time.monotonic())))
            else:
                window.sleep(min(0.01, window.remaining()))
    if window is not None and not window.finishing:
        if window.remaining() <= 0:
            raise WaiterLockDeadline(_COORDINATE, contended=False)
        if window.contended and window.check_authority is not None:
            window.check_authority()
        if window.remaining() <= 0:
            raise WaiterLockDeadline(_COORDINATE, contended=False)


@contextmanager
def car_acceptance_guard(root, *, observe=False):
    """Yield fence presence; observation never creates a directory or lock.

    Every claim/completion and notice uses the exclusive default before any
    recipient or ledger lock. Shared observation refuses changed absence.
    A descriptor walk and pre-/post-operation inode checks retain the same
    root, work directory and ordinary lock throughout the operation.
    """
    if type(observe) is not bool:
        raise TypeError("car fence observation mode must be boolean")
    with ExitStack() as stack:
        edges = []
        caller_error = None
        try:
            parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            stack.callback(os.close, parent)
            for component in root.tenant_home.parts[1:]:
                child = _directory(parent, component, stack)
                edges.append((parent, component, child))
                parent = child
            _owned(os.fstat(parent), directory=True, tenant_home=root.tenant_home)
            # Pin the root before epoch_guard opens it, then prove that the
            # named root still designates this descriptor after acquisition.
            with epoch_guard(root), lock_order_guard(root, LOCK_ORDER_WAKE, label="car"):
                _check_chain(edges)
                try:
                    work = _directory(parent, "work", stack)
                except FileNotFoundError:
                    if observe:
                        _check_absence(edges, parent, "work")
                        try:
                            yield False
                        except OSError as exc:
                            caller_error = exc
                            raise
                        _check_absence(edges, parent, "work")
                        return
                    try:
                        os.mkdir("work", mode=0o700, dir_fd=parent)
                    except FileExistsError:
                        pass
                    work = _directory(parent, "work", stack)
                edges.append((parent, "work", work))
                _owned(os.fstat(work), directory=True)
                # The retained work-directory descriptor is an independent
                # physical fence: a new leaf inode cannot let a cooperating
                # caller overlap an operation still holding the old leaf.
                deadline = time.monotonic() + _TIMEOUT
                with ExitStack() as lock_stack:
                    lock_stack.callback(fcntl.flock, work, fcntl.LOCK_UN)
                    _acquire(work, observe=observe, deadline=deadline)
                    _check_chain(edges)
                    _owned(os.fstat(work), directory=True)
                    flags = os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
                    flags |= os.O_RDONLY if observe else os.O_RDWR
                    try:
                        descriptor = os.open(_LEAF, flags, dir_fd=work)
                    except FileNotFoundError:
                        if observe:
                            _check_absence(edges, work, _LEAF)
                            try:
                                yield False
                            except OSError as exc:
                                caller_error = exc
                                raise
                            _check_absence(edges, work, _LEAF)
                            return
                        try:
                            descriptor = os.open(_LEAF, flags | os.O_CREAT | os.O_EXCL,
                                0o600, dir_fd=work)
                        except FileExistsError:
                            descriptor = os.open(_LEAF, flags, dir_fd=work)
                    stack.callback(os.close, descriptor)
                    _check_fence(edges, work, descriptor)
                    # Release both fences before releasing the epoch, even
                    # when authority/deadline checks refuse after acquisition.
                    lock_stack.callback(fcntl.flock, descriptor, fcntl.LOCK_UN)
                    _acquire(descriptor, observe=observe, deadline=deadline)
                    _check_fence(edges, work, descriptor)
                    try:
                        yield True
                    except OSError as exc:
                        caller_error = exc
                        raise
                    finally:
                        _check_fence(edges, work, descriptor)
        except OSError as exc:
            # The caller's operation is not fence I/O. A different failure in
            # the post-check or cleanup must still retain its typed refusal.
            if exc is caller_error:
                raise
            raise _invalid(_COORDINATE + " could not retain its ordinary descriptor coordinates") from exc

"""Private native parent anchors, held before a worker can start a child.

This is private process provenance, not work authority or a new ledger.
Every reader stays in its explicitly selected tenant. Unreadable active
testimony prevents host attribution rather than turning a job into a host.
An unfinalized anchor survives a crash or unconfirmed terminal callback: the
absence of its lock never proves that its launched children are gone.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys

from .ids import uuid7_hex
from .errors import ProtocolRefusal
from .root import validate_identifier

_RELATIVE = Path('state/worker-runner-anchors')
_FIELDS = {'schema_version', 'tenant_id', 'node_id', 'session_id', 'pid', 'birth', 'uid'}
_OPEN_ANCHORS = set()


def close_inherited_worker_anchors():
    """The forked adapter borrows no parent provenance or directory handle."""
    inherited = tuple(_OPEN_ANCHORS)
    _OPEN_ANCHORS.clear()
    for descriptor in inherited:
        os.close(descriptor)


def _directory(root, *, create=False):
    """Traverse the lexical coordinate: never resolve away a symlink."""
    path = root.tenant_home / _RELATIVE
    parent = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    current = Path('/')
    try:
        for part in path.parts[1:]:
            current /= part
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            except FileNotFoundError:
                if not create or current not in {root.tenant_home / 'state', path}:
                    raise
                os.mkdir(part, 0o700, dir_fd=parent)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
            if current in {root.tenant_home / 'state', path} and os.fstat(parent).st_uid != os.getuid():
                raise OSError('worker anchor storage is not owned by the invoking user')
        return parent
    except BaseException:
        os.close(parent)
        raise


def _valid(root, row):
    if (type(row) is not dict or set(row) != _FIELDS or row['schema_version'] != 0 or
            row['tenant_id'] != root.tenant_id or type(row['pid']) is not int or
            not 1 < row['pid'] <= 2147483647 or type(row['uid']) is not int or
            row['uid'] != os.getuid() or type(row['birth']) is not list or
            len(row['birth']) != (2 if sys.platform == 'darwin' else 1) or
            any(type(value) is not int or value < 0 for value in row['birth']) or
            row['birth'][0] <= 0 or (len(row['birth']) == 2 and row['birth'][1] >= 1000000) or
            type(row['session_id']) is not str or
            re.fullmatch(r'worker-[0-9a-f]{32}', row['session_id']) is None):
        raise OSError('worker runner anchor has malformed native identity')
    try:
        validate_identifier(row['node_id'], 'node')
    except Exception as exc:
        raise OSError('worker runner anchor has malformed node identity') from exc


@contextmanager
def worker_runner_anchor(root, node, session, *, finalized):
    """Publish and hold this parent before either fork or native dispatch."""
    from .seat_host_worker import _identity
    directory = descriptor = None
    name = uuid7_hex() + '.json'
    owned = None
    entered = False
    try:
        try:
            identity = _identity(os.getpid())
            if identity is None or identity['uid'] != os.getuid():
                raise OSError('worker parent native identity is unavailable')
            row = {'schema_version': 0, 'tenant_id': root.tenant_id,
                'node_id': node, 'session_id': session, 'pid': identity['pid'],
                'birth': list(identity['birth']), 'uid': identity['uid']}
            _valid(root, row)
            directory = _directory(root, create=True)
            _OPEN_ANCHORS.add(directory)
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                os.O_NOFOLLOW | getattr(os, 'O_CLOEXEC', 0), 0o600, dir_fd=directory)
            _OPEN_ANCHORS.add(descriptor)
            owned = os.fstat(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            encoded = (json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n').encode()
            if os.write(descriptor, encoded) != len(encoded):
                raise OSError('worker parent anchor write was incomplete')
            os.fsync(descriptor)
        except (OSError, ValueError, TypeError, ProtocolRefusal):
            # The runner already owns its claimed work and its existing
            # process_start_failed degradation, so it publishes that receipt.
            yield False
            return
        entered = True
        yield True
    finally:
        try:
            if owned is not None and (not entered or finalized()):
                try:
                    named = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    if (named.st_dev, named.st_ino) != (owned.st_dev, owned.st_ino):
                        raise OSError('worker parent anchor was replaced; preserve the replacement')
                    os.unlink(name, dir_fd=directory)
        finally:
            if descriptor is not None:
                _OPEN_ANCHORS.discard(descriptor)
                os.close(descriptor)
            if directory is not None:
                _OPEN_ANCHORS.discard(directory)
                os.close(directory)


def live_worker_runner_pids(root):
    """Match held parents; None means unresolved launch provenance remains.

    A stale/unlocked file is still an unfinalized dispatch, including a crash
    before any cleanup audit existed. Suppress host classification globally
    within this named root until that uncertainty is explicitly resolved.
    """
    from .seat_host_worker import _identity
    try:
        directory = _directory(root)
    except FileNotFoundError:
        return set()
    result = set()
    try:
        names = os.listdir(directory)
        if len(names) > 10000:
            raise OSError('worker runner anchor population exceeds its bound')
        for name in sorted(names):
            if re.fullmatch(r'[0-9a-f]{32}\.json', name) is None:
                raise OSError('worker runner anchor directory contains an unknown entry')
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                metadata = os.fstat(descriptor)
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or
                        metadata.st_nlink != 1 or metadata.st_size > 4096):
                    raise OSError('worker runner anchor is not bounded ordinary owned state')
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    return None
                raw = os.read(descriptor, 4097)
                if len(raw) > 4096:
                    raise OSError('worker runner anchor exceeds its byte bound')
                try:
                    from .recipient_mechanisms import _json
                    row = _json(raw)
                except (ValueError, UnicodeError) as exc:
                    raise OSError('active worker runner anchor is malformed') from exc
                _valid(root, row)
                current = _identity(row['pid'])
                if (current is None or current['uid'] != row['uid'] or
                        list(current['birth']) != row['birth']):
                    # A forked child can retain the held descriptor after the
                    # parent dies. That uncertainty never earns a host match.
                    return None
                result.add(row['pid'])
            finally:
                os.close(descriptor)
        return result
    finally:
        os.close(directory)

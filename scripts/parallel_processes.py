"""Bounded observation of launcher-owned descendants, never a host-wide scan.

Snapshots cannot see a fork/reparent/exit sequence entirely between observations.
Identity checks before kill are not atomic with kill on macOS. This is bounded,
identity-checked containment, not a kernel guarantee of exhaustive ownership.

A live owned process can be unreadable through the per-user identity call:
a child that execs a setuid-root binary (macOS /bin/ps, /usr/bin/top) keeps
its pid and start time but answers EPERM. EPERM is therefore not a verdict.
The kernel's unprivileged start witness decides: gone is recorded as gone
with a typed reason; alive and ours by pid + start time stays owned, is never
signaled, and is refused by pid wherever survivors are judged.

Linux captures stat/status/stat under one matching birth identity and uses
status's effective UID. A setuid transition keeps that process owned, but
signaling requires a fresh same-birth observation under the launcher's UID.
Vanished proc read sites are bounded testimony, separate from a verified
process departure; one vanished thread does not erase surviving siblings.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
from pathlib import Path
import struct
import sys
import time


class _BsdInfo(ctypes.Structure):
    # SDK sys/proc_info.h proc_bsdinfo: 136 bytes, start timeval at 120/128.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        'flags', 'status', 'xstatus', 'pid', 'ppid', 'uid', 'gid', 'ruid',
        'rgid', 'svuid', 'svgid', 'reserved')]
    _fields_ += [('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in (
        'nfiles', 'pgid', 'pjobc', 'tdev', 'tpgid')]
    _fields_ += [('nice', ctypes.c_int32), ('start_sec', ctypes.c_uint64),
                ('start_usec', ctypes.c_uint64)]


# SDK sys/sysctl.h struct kinfo_proc (LP64): 648 bytes. Offsets were checked
# against proc_bsdinfo for live, zombie and reaped processes on this host.
_KINFO_SIZE = 648
_KINFO_START, _KINFO_STAT, _KINFO_PID = 0, 36, 40
_KINFO_EUID, _KINFO_PPID = 420, 560
_SZOMB = 5

# Per remaining parent, then one named cap for the whole observe() sweep.
OBSERVATION_PASS_SECONDS = .25
OBSERVATION_SWEEP_CAP_SECONDS = 4.0
OWNED_LIVE_PROCESS_CAP = 65536


class ProcessQuery:
    """Query one retained PID or its immediate children on the current host."""
    def __init__(self):
        self.observations = []
        self._observation_keys = set()
        if sys.platform == 'darwin':
            self.lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
            self.lib.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                             ctypes.c_void_p, ctypes.c_int]
            self.lib.proc_pidinfo.restype = ctypes.c_int
            self.lib.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            self.lib.proc_listchildpids.restype = ctypes.c_int
            self.libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
            self.libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                         ctypes.c_void_p, ctypes.c_size_t]
            self.libc.sysctl.restype = ctypes.c_int
        elif sys.platform != 'linux':
            raise OSError('owned process observation unsupported on ' + sys.platform)

    def identity(self, pid):
        if sys.platform == 'darwin':
            value = _BsdInfo()
            ctypes.set_errno(0)
            count = self.lib.proc_pidinfo(pid, 3, 0, ctypes.byref(value), ctypes.sizeof(value))
            if count <= 0:
                code = ctypes.get_errno()
                if code == errno.ESRCH:
                    return None
                raise OSError(code, 'cannot read owned process identity', pid)
            if (count != ctypes.sizeof(value) or value.pid != pid
                    or not value.start_sec or value.start_usec >= 1_000_000):
                raise OSError('invalid owned process identity layout')
            return {'pid': pid, 'birth': [value.start_sec, value.start_usec],
                    'ppid': value.ppid, 'uid': value.uid, 'zombie': value.status == 5}
        path = Path('/proc') / str(pid)
        for _ in range(2):
            before = self._linux_read(path / 'stat', pid, 'stat-before')
            if before is None:
                return None
            first = self._linux_stat(before, pid)
            status = self._linux_read(path / 'status', pid, 'status')
            if status is None:
                return None
            rows = [line.split() for line in status.splitlines()
                    if line.split() and line.split()[0] == 'Uid:']
            try:
                if len(rows) != 1 or len(rows[0]) != 5:
                    raise ValueError('UID row count or length')
                uids = [int(value) for value in rows[0][1:]]
                if any(value < 0 for value in uids):
                    raise ValueError('negative UID')
            except ValueError as exc:
                raise OSError('invalid owned Linux process UID layout') from exc
            after = self._linux_read(path / 'stat', pid, 'stat-after')
            if after is None:
                return None
            second = self._linux_stat(after, pid)
            if first['birth'] == second['birth']:
                return dict(second, uid=uids[1])
        raise OSError('owned Linux process start identity changed during capture')

    def _departure_observation(self, pid, site, error, task_pid=None):
        if error.errno not in (errno.ENOENT, errno.ESRCH):
            raise error
        key = (pid, site, error.errno, task_pid)
        if key in self._observation_keys:
            return
        if len(self.observations) >= 65536:
            raise OSError('owned process observation testimony exceeded bound')
        row = {'pid': pid, 'site': site, 'errno': error.errno,
               'reason': 'proc entry disappeared'}
        if task_pid is not None:
            row['task_pid'] = task_pid
        self._observation_keys.add(key)
        self.observations.append(row)

    def _linux_read(self, path, pid, site, task_pid=None):
        try:
            return path.read_text()
        except OSError as exc:
            self._departure_observation(pid, site, exc, task_pid)
            return None

    @staticmethod
    def _linux_stat(raw, pid):
        try:
            fields = raw.rsplit(')', 1)[1].split()
            if int(raw.split('(', 1)[0].strip()) != pid:
                raise ValueError('PID mismatch')
            birth, parent = int(fields[19]), int(fields[1])
            if birth <= 0 or parent < 0 or len(fields[0]) != 1:
                raise ValueError('invalid start or parent')
            return {'pid': pid, 'birth': [birth], 'ppid': parent,
                    'zombie': fields[0] == 'Z'}
        except (ValueError, IndexError) as exc:
            raise OSError('invalid owned Linux process stat layout') from exc

    def witness(self, pid):
        """Start identity without the same-user check, or None when gone.

        Used only after identity() answered EPERM: kern.proc.pid reports
        pid, start time, parent, effective UID and zombie state for any
        process, so a pid can be matched to what we retained by its start.
        """
        if sys.platform != 'darwin':
            return self.identity(pid)
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buffer = ctypes.create_string_buffer(_KINFO_SIZE)
        size = ctypes.c_size_t(_KINFO_SIZE)
        ctypes.set_errno(0)
        if self.libc.sysctl(mib, 4, buffer, ctypes.byref(size), None, 0) != 0:
            raise OSError(ctypes.get_errno(), 'cannot witness owned process start', pid)
        if size.value == 0:
            return None
        raw = buffer.raw
        start_sec, start_usec = struct.unpack_from('=qi', raw, _KINFO_START)
        if (size.value != _KINFO_SIZE or struct.unpack_from('=i', raw, _KINFO_PID)[0] != pid
                or start_sec <= 0 or not 0 <= start_usec < 1_000_000):
            raise OSError('invalid owned process witness layout')
        return {'pid': pid, 'birth': [start_sec, start_usec],
                'ppid': struct.unpack_from('=i', raw, _KINFO_PPID)[0],
                'uid': struct.unpack_from('=I', raw, _KINFO_EUID)[0],
                'zombie': raw[_KINFO_STAT] == _SZOMB}

    def children(self, pid):
        if sys.platform == 'darwin':
            # A full buffer refuses instead of silently dropping ownership.
            values = (ctypes.c_int * 4096)()
            ctypes.set_errno(0)
            count = self.lib.proc_listchildpids(pid, values, ctypes.sizeof(values))
            if count < 0 or (count == 0 and ctypes.get_errno()):
                code = ctypes.get_errno()
                if code == errno.ESRCH:
                    return []
                raise OSError(code, 'cannot enumerate owned children', pid)
            # libproc's child wrapper returns a PID count, unlike proc_listpids.
            if count >= len(values):
                raise OSError('owned child enumeration exceeded bound')
            return [pid for pid in values[:count] if pid > 0]
        answer = set()
        try:
            tasks = list((Path('/proc') / str(pid) / 'task').iterdir())
        except OSError as exc:
            self._departure_observation(pid, 'task-directory', exc)
            return []
        for task in tasks:
            task_pid = int(task.name)
            text = self._linux_read(task / 'children', pid, 'thread-children', task_pid)
            if text is not None:
                answer.update(int(value) for value in text.split())
        if len(answer) >= 4096:
            raise OSError('owned child enumeration exceeded bound')
        return sorted(answer)


class ProcessTree:
    def __init__(self, output):
        self.query = ProcessQuery()
        self.known = {}
        self.active = set()
        self.errors = []
        self.departed = []
        self.limited_observation = None
        self.journal = (output / 'processes.jsonl').open('x')
        self._cost = dict.fromkeys((
            'observe_calls', 'observe_wall_ns', 'observe_process_cpu_ns',
            'identity_attempts', 'children_attempts', 'witness_attempts',
            'journal_write_attempts', 'journal_bytes_submitted'), 0)

    def cost(self):
        """Detached informational totals; no ownership query or verdict."""
        return dict(self._cost, schema_version=1,
                    observe_scope='Wall and launcher process CPU nanoseconds during observe calls only.',
                    query_scope='Query API attempts across this tree lifecycle; not kernel syscall or process counts.',
                    journal_scope='UTF-8 bytes submitted to journal write; not accepted or durable bytes.')

    @staticmethod
    def same(first, second):
        return (first is not None and second is not None
                and (first['pid'], first['birth'], first['uid'])
                == (second['pid'], second['birth'], second['uid']))

    @staticmethod
    def started(first, second):
        """Same process by start identity: pid and kernel start time."""
        return (first is not None and second is not None
                and first['pid'] == second['pid']
                and list(first['birth']) == list(second['birth']))

    def read(self, pid):
        """Identity or proven departure; EPERM alone requires a start witness."""
        try:
            self._cost['identity_attempts'] += 1
            return self.query.identity(pid), None
        except OSError as exc:
            if isinstance(exc, FileNotFoundError) or exc.errno in (errno.ENOENT, errno.ESRCH):
                return None, None
            if exc.errno != errno.EPERM:
                raise
            try:
                self._cost['witness_attempts'] += 1
                value = self.query.witness(pid)
            except OSError as witness_error:
                if (isinstance(witness_error, FileNotFoundError)
                        or witness_error.errno in (errno.ENOENT, errno.ESRCH)):
                    value = None
                else:
                    raise
            return (None if value is None else dict(value, unreadable=str(exc))), str(exc)

    def depart(self, item, value, reason=None):
        if reason is None:
            reason = ('exited' if value is None
                      else 'zombie' if self.started(item, value) and value['zombie']
                      else 'pid reused' if not self.started(item, value)
                      else 'identity changed')
        row = {'pid': item['pid'], 'birth': item.get('birth'),
               'root': item['root'], 'reason': reason}
        if item.get('parent') is not None:
            row['parent'] = item['parent']
        if item.get('ownership') == 'unestablished':
            row['ownership'] = 'unestablished'
        self.departed.append(row)

    def unreadable_refusals(self, survivors):
        """Name survivors that cannot safely be signaled at the verdict."""
        refusals = []
        for item in survivors:
            if item.get('unreadable'):
                refusals.append(item['root'] + ': owned process alive but unreadable: pid %d start %s euid %s: %s'
                    % (item['pid'], list(item['birth']), item.get('euid', item['uid']), item['unreadable']))
            elif item.get('foreign_uid'):
                refusals.append(item['root'] + ': owned process alive under foreign UID: pid %d start %s euid %s'
                    % (item['pid'], list(item['birth']), item['uid']))
        return refusals

    def error(self, message):
        if message not in self.errors:
            self.errors.append(message)

    def retain(self, identity, root, parent=None):
        # Callers establish direct-child or observed-parent ownership. UID
        # may change across exec; start identity retains that same process.
        key = (identity['pid'], tuple(identity['birth']))
        if key not in self.known:
            if len(self.active) >= OWNED_LIVE_PROCESS_CAP:
                # History remains in known for native receipt/ancestry proof.
                # Refresh only at capacity: unswept exits must not consume the
                # live budget, while unreadable/uncertain identities stay active.
                self.alive()
                if len(self.active) >= OWNED_LIVE_PROCESS_CAP:
                    raise OSError('owned live process observation exceeded bound')
            self.known[key] = dict(identity, root=root, parent=parent)
            if identity.get('unreadable'):
                self.known[key]['euid'] = identity['uid']
            elif identity['uid'] != os.getuid():
                self.known[key].update(euid=identity['uid'], foreign_uid=True)
            self.active.add(key)
            text = json.dumps(self.known[key], sort_keys=True) + '\n'
            self._cost['journal_write_attempts'] += 1
            self._cost['journal_bytes_submitted'] += len(text.encode('utf-8'))
            self.journal.write(text)
            self.journal.flush()

    def root(self, process, key):
        # Popen may already have reaped an exec-failed child before raising.
        # Its numeric PID then names no retained kernel child and may be reused.
        if getattr(process, 'returncode', None) is not None:
            if getattr(process, '_child_created', False):
                self.depart({'pid': process.pid, 'root': key, 'ownership': 'unestablished'},
                            None, reason='exited before leader identity read')
            return
        try:
            if not getattr(process, '_child_created', False):
                raise OSError('spawned leader lacks retained child ownership')
            identity, _ = self.read(process.pid)
            if identity is None:
                self.depart({'pid': process.pid, 'root': key, 'ownership': 'unestablished'},
                            None, reason='exited before leader identity read')
                return
            if identity['ppid'] != os.getpid():
                raise OSError('spawned leader is not this launcher\'s direct child: parent mismatch')
            self.retain(identity, key)
        except Exception as exc:
            self.error(key + ': ownership unknown: ' + str(exc))

    def current(self, item):
        key = (item['pid'], tuple(item['birth']))
        value, unreadable = self.read(item['pid'])
        if self.started(item, value) and not value['zombie']:
            if unreadable:
                # Alive and ours by start identity, but unreadable: owned
                # until a later observation proves it gone.
                item.update(unreadable=value['unreadable'], euid=value['uid'])
            else:
                item.pop('unreadable', None)
                item['uid'] = value['uid']
                if value['uid'] != os.getuid():
                    item.update(euid=value['uid'], foreign_uid=True)
                else:
                    item.pop('euid', None)
                    item.pop('foreign_uid', None)
            return value
        if key in self.active:
            self.depart(item, value)
            self.active.discard(key)
        return None

    def observe(self):
        wall_started = time.perf_counter_ns()
        cpu_started = time.process_time_ns()
        self._cost['observe_calls'] += 1
        try:
            return self._observe()
        finally:
            self._cost['observe_process_cpu_ns'] += time.process_time_ns() - cpu_started
            self._cost['observe_wall_ns'] += time.perf_counter_ns() - wall_started

    def _observe(self):
        pending = [self.known[key] for key in self.active]
        seen = set()
        started = time.monotonic()
        deadline = started + OBSERVATION_PASS_SECONDS
        cap = started + OBSERVATION_SWEEP_CAP_SECONDS
        while pending:
            if time.monotonic() >= cap:
                self.limited_observation = {
                    'reason': 'owned observation time cap reached',
                    'remaining': len(pending),
                }
                return
            parent = pending.pop()
            token = (parent['pid'], tuple(parent['birth']))
            if token in seen:
                continue
            seen.add(token)
            try:
                if time.monotonic() > deadline:
                    # Retry remaining parents inside the named sweep cap.
                    deadline = time.monotonic() + OBSERVATION_PASS_SECONDS
                before = self.current(parent)
                if before is None:
                    continue
                self._cost['children_attempts'] += 1
                candidates = [(pid, self.read(pid)[0])
                              for pid in self.query.children(parent['pid'])]
                if self.current(parent) is None:
                    # A normal exit may occur across a snapshot. Discard the
                    # unproved edge; the receipt explicitly limits observation.
                    continue
                for pid, child in candidates:
                    if child is None:
                        # Enumeration proved only a candidate edge. Never invent
                        # a birth identity or claim this PID was retained/owned.
                        self.depart({'pid': pid, 'root': parent['root'],
                                     'parent': {'pid': parent['pid'], 'birth': parent['birth']},
                                     'ownership': 'unestablished'}, None,
                                    reason='exited before identity read')
                        continue
                    if child is not None and child.get('unreadable') and child['zombie']:
                        continue
                    if child is not None and child['ppid'] == parent['pid']:
                        self.retain(child, parent['root'], {
                            'pid': parent['pid'], 'birth': parent['birth']})
                        pending.append(self.known[(child['pid'], tuple(child['birth']))])
            except Exception as exc:
                self.error(parent['root'] + ': ownership observation: ' + str(exc))

    def alive(self, root=None):
        survivors = []
        for key in list(self.active):
            item = self.known[key]
            if root is not None and item['root'] != root:
                continue
            try:
                if self.current(item) is not None:
                    survivors.append(item)
            except Exception as exc:
                self.error(item['root'] + ': ownership liveness unknown: ' + str(exc))
        return survivors

    def signal(self, number):
        for key in list(self.active):
            item = self.known[key]
            try:
                value = self.current(item)
                # Never signal an unreadable or foreign-UID process. A fresh
                # same-birth own-UID read is required before each signal.
                if (value is not None and not value.get('unreadable')
                        and value['uid'] == os.getuid()):
                    os.kill(item['pid'], number)
            except ProcessLookupError:
                pass
            except Exception as exc:
                self.error(item['root'] + ': owned signal failed: ' + str(exc))

    def receipt(self):
        row = {'scope': 'direct children and observed descendants',
               'exhaustive': False,
               'limits': 'Snapshots may miss fork/reparent/exit between observations; identity check and signal are not atomic.',
               'observed': list(self.known.values()),
               'departed': list(self.departed),
               'observations': list(getattr(self.query, 'observations', []))}
        if self.limited_observation is not None:
            row['limited_observation'] = dict(self.limited_observation)
        return row


def validate_ownership_receipt(ownership, children, platform):
    """Validate bounded native testimony; perform no fresh process observation.

    Shared by full diagnostic and offload-remainder admission. An unestablished
    exit stays unestablished; matching observed/departed rows do not turn the
    producer's explicitly nonexhaustive snapshots into complete containment.
    """
    def integer(value, minimum=0):
        return type(value) is int and value >= minimum

    if platform not in {"darwin", "linux"} or type(children) is not list or not children:
        raise ValueError("native ownership requires collected children on a supported platform")
    leaders = {}
    for child in children:
        if (type(child) is not dict or set(child) != {"key", "pid", "returncode", "log"}
                or type(child["key"]) is not str or not child["key"] or child["key"] in leaders
                or not integer(child["pid"], 1) or type(child["returncode"]) is not int
                or type(child["log"]) is not str or not child["log"]):
            raise ValueError("native collected child identity is invalid")
        leaders[child["key"]] = child["pid"]
    required = {"scope", "exhaustive", "limits", "observed", "departed", "observations"}
    if (type(ownership) is not dict or not required <= set(ownership)
            or set(ownership) - required - {"limited_observation"}
            or ownership["scope"] != "direct children and observed descendants"
            or ownership["exhaustive"] is not False
            or ownership["limits"] != "Snapshots may miss fork/reparent/exit between observations; identity check and signal are not atomic."
            or any(type(ownership[field]) is not list
                   for field in ("observed", "departed", "observations"))):
        raise ValueError("native ownership structure is invalid")

    def identity_token(row):
        birth = row["birth"]
        if (not integer(row["pid"], 1) or type(birth) is not list
                or len(birth) != (2 if platform == "darwin" else 1)
                or not integer(birth[0], 1)
                or (platform == "darwin" and not (integer(birth[1]) and birth[1] < 1_000_000))):
            raise ValueError("native process start identity is invalid")
        return row["pid"], tuple(birth)

    observed = {}
    for row in ownership["observed"]:
        required_row = {"pid", "birth", "ppid", "uid", "zombie", "root", "parent"}
        if (type(row) is not dict or not required_row <= set(row)
                or set(row) - required_row - {"unreadable", "euid", "foreign_uid"}
                or type(row["root"]) is not str or row["root"] not in leaders
                or not integer(row["ppid"]) or not integer(row["uid"])
                or type(row["zombie"]) is not bool
                or ("euid" in row and not integer(row["euid"]))
                or ("foreign_uid" in row and row["foreign_uid"] is not True)
                or ("unreadable" in row and (type(row["unreadable"]) is not str or not row["unreadable"]))):
            raise ValueError("native observed ownership row is invalid")
        token = identity_token(row)
        if token in observed:
            raise ValueError("native ownership repeats an observed process identity")
        observed[token] = row
    observed_leaders = {key: [] for key in leaders}
    parents = {}
    for token, row in observed.items():
        parent = row["parent"]
        if parent is None:
            if row["pid"] != leaders[row["root"]]:
                raise ValueError("native observed leader differs from collected child")
            observed_leaders[row["root"]].append(token)
            parents[token] = None
        else:
            if type(parent) is not dict or set(parent) != {"pid", "birth"}:
                raise ValueError("native observed parent shape is invalid")
            parent_token = identity_token(parent)
            if (parent_token not in observed or row["ppid"] != parent["pid"]
                    or observed[parent_token]["root"] != row["root"]):
                raise ValueError("native observed descendant lacks its same-root parent")
            parents[token] = parent_token
    # Every observed edge must lead to the corresponding collected leader,
    # never to a detached cycle that happens to have matching departure rows.
    resolved = set()
    for token in parents:
        path = set()
        current = token
        while current is not None and current not in resolved:
            if current in path:
                raise ValueError("native observed ancestry contains a cycle")
            path.add(current)
            current = parents[current]
        resolved.update(path)
    departed = set()
    unobserved_leaders = set()
    for row in ownership["departed"]:
        if (type(row) is not dict or not {"pid", "birth", "root", "reason"} <= set(row)
                or set(row) - {"pid", "birth", "root", "reason", "parent", "ownership"}
                or type(row["root"]) is not str or row["root"] not in leaders
                or not integer(row["pid"], 1)):
            raise ValueError("native departure ownership row is invalid")
        if row.get("ownership") == "unestablished":
            if row["birth"] is not None:
                raise ValueError("unestablished native process has invented start identity")
            parent = row.get("parent")
            if parent is None:
                if (row["pid"] != leaders[row["root"]]
                        or row["reason"] != "exited before leader identity read"):
                    raise ValueError("unobserved native leader is not the collected child")
                unobserved_leaders.add(row["root"])
            else:
                if type(parent) is not dict or set(parent) != {"pid", "birth"}:
                    raise ValueError("unobserved native descendant parent shape is invalid")
                parent_token = identity_token(parent)
                if (parent_token not in observed or observed[parent_token]["root"] != row["root"]
                        or row["reason"] != "exited before identity read"):
                    raise ValueError("unobserved native descendant lacks its same-root parent")
            continue
        token = identity_token(row)
        if (token not in observed or "ownership" in row
                or row["root"] != observed[token]["root"]
                or row["reason"] not in {"exited", "zombie", "pid reused", "identity changed"}
                or row.get("parent") != observed[token]["parent"]):
            raise ValueError("native departure differs from observed ownership")
        departed.add(token)
    if set(observed) != departed:
        raise ValueError("native observed processes lack departure proof")
    for key, tokens in observed_leaders.items():
        if len(tokens) != 1 and not (not tokens and key in unobserved_leaders):
            raise ValueError("native collected child ownership is missing or ambiguous")
    for row in ownership["observations"]:
        if (type(row) is not dict or not {"pid", "site", "errno", "reason"} <= set(row)
                or set(row) - {"pid", "site", "errno", "reason", "task_pid"}
                or not integer(row["pid"], 1) or type(row["errno"]) is not int
                or row["errno"] not in (2, 3) or row["reason"] != "proc entry disappeared"
                or row["site"] not in {"stat-before", "status", "stat-after", "task-directory", "thread-children"}
                or ("task_pid" in row and not integer(row["task_pid"], 1))):
            raise ValueError("native observation testimony is invalid")
    if "limited_observation" in ownership:
        limited = ownership["limited_observation"]
        if (type(limited) is not dict or set(limited) != {"reason", "remaining"}
                or limited["reason"] != "owned observation time cap reached"
                or not integer(limited["remaining"], 1)):
            raise ValueError("native limited observation testimony is invalid")

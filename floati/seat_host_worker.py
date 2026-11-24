"""Attribute managed hosts at a native activity cutoff.

This is a local work classification, not an adversarial binary identity
claim. Zcode additionally requires exact declared launch argv; no names, PID
files or process signalling supply attribution. Raw argv never enters receipts.
Unreadable or changing process identity earns no exemption.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
import re
import stat
import sys
from pathlib import Path

_LAUNCH_BOUND = 2 * 1024 * 1024


def codex_session_ancestry(root, node, workspace, session):
    """Revalidate the acting Stop invocation's native host ancestors.

    Codex can execute tools under a shared app-server, outside the chat's
    process tree. The session-bound Stop invocation already records that
    tree. Only its still-matching ancestor suffix is hosting, never children
    of the chat or daemon. Normal Stop return retains this testimony too.
    """
    from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
    from .errors import IntegrityFailure, ProtocolRefusal
    from .jsonl import read_records_snapshot
    from .stop_hold import StopHoldLedger
    from .waiter_invocation import _paths, _read

    def pid(value):
        return type(value) is int and 1 < value <= 2147483647

    def birth(value):
        return type(value) in {int, float} and 0 < value <= 253402300799 and math.isfinite(value)

    try:
        participant = resolve_participant(root.path, workspace, _strict=True)
        if participant is None or participant.binding.node_id != node:
            return []
        consent = CodexWaitConsentLedger(root).require_armed(participant.binding)
        ledger = CodexWaitSessionLedger(root)
        claims = ledger._matching(read_records_snapshot(root, ledger._relative(node),
            allowed_kinds=ledger._KINDS), participant.binding)
        if not claims or claims[-1]['acting_session_id'] != session:
            return []
        path = _paths(root, node, session)[1]
        row = _read(path)
        if (not isinstance(row, dict) or row.get('node_id') != node or
                row.get('session_id') != session or row.get('workspace') != str(workspace) or
                row.get('phase') not in {'running', 'exited'} or
                not pid(row.get('pid')) or not birth(row.get('process_start_epoch'))):
            return []
        holds = [item for item in StopHoldLedger(root).read(node)
                 if item['invocation_id'] == row.get('invocation_id')]
        phases = ['start', 'exit'] if row['phase'] == 'exited' else ['start']
        expected = {'node_id': node, 'acting_session_id': session, 'workspace': str(workspace),
            'harness': 'codex', 'consent_receipt_id': consent['id'],
            'holder_pid': row['pid'], 'process_start_epoch': row['process_start_epoch']}
        if ([item['phase'] for item in holds] != phases or
                any(item.get(key) != value for item in holds for key, value in expected.items())):
            return []
        ancestors = row.get('host_ancestry')
        if not isinstance(ancestors, list) or not 1 <= len(ancestors) <= 64:
            return []
        seen = {row['pid']}
        for index, ancestor in enumerate(ancestors):
            if (not isinstance(ancestor, dict) or
                    set(ancestor) != {'pid', 'process_start_epoch', 'parent_pid'} or
                    not pid(ancestor['pid']) or ancestor['pid'] in seen or
                    not birth(ancestor['process_start_epoch']) or
                    type(ancestor['parent_pid']) is not int or not 0 <= ancestor['parent_pid'] <= 2147483647):
                return []
            seen.add(ancestor['pid'])
            parent = ancestors[index + 1].get('pid') if index + 1 < len(ancestors) and isinstance(ancestors[index + 1], dict) else None
            if (index + 1 < len(ancestors) and ancestor['parent_pid'] != parent or
                    index + 1 == len(ancestors) and ancestor['parent_pid'] > 1):
                return []
        if (row.get('parent_pid') != ancestors[0]['pid'] or
                row.get('parent_process_start_epoch') != ancestors[0]['process_start_epoch']):
            return []
        # A hook shell can have exited after publishing its terminal state.
        # It cannot validate earlier ancestors, but a surviving suffix still
        # proves the chat and its launch shell with their exact native edges.
        observed, result = {}, []
        for ancestor in reversed(ancestors):
            current = _identity(ancestor['pid'])
            if (current is None or current['process_start_epoch'] != ancestor['process_start_epoch'] or
                    current['ppid'] != ancestor['parent_pid']):
                break
            observed[ancestor['pid']] = current
            if current['uid'] == os.getuid():
                result.append(ancestor['pid'])
        if row['phase'] == 'running':
            waiter = _identity(row['pid'])
            if (waiter is None or waiter['uid'] != os.getuid() or
                    waiter['process_start_epoch'] != row['process_start_epoch'] or
                    waiter['ppid'] != row['parent_pid'] or len(observed) != len(ancestors)):
                return []
            observed[row['pid']] = waiter
        # Bracket the whole classification, not just each independent read.
        if _read(path) != row or any(_identity(number) != identity for number, identity in observed.items()):
            return []
        return list(reversed(result))
    except (OSError, ValueError, TypeError, RuntimeError, IntegrityFailure, ProtocolRefusal):
        # Missing or unreadable attribution grants no process exemption.
        return []


def _invalid_json_constant(_value):
    raise ValueError('non-JSON numeric constant')


def _darwin_arguments(raw):
    """Decode only argv and HOME from KERN_PROCARGS2; never decode other env."""
    argc = int.from_bytes(raw[:4], sys.byteorder, signed=True)
    if not 1 <= argc <= 65536:
        raise ValueError('invalid argc')
    offset = raw.index(b'\0', 4) + 1  # kernel executable path
    while offset < len(raw) and raw[offset] == 0:
        offset += 1
    values = raw[offset:].split(b'\0')
    if len(values) <= argc:
        raise ValueError('incomplete argv')
    argv = tuple(os.fsdecode(value) for value in values[:argc])
    homes = [os.fsdecode(value[5:]) for value in values[argc:] if value.startswith(b'HOME=')]
    return argv, homes[0] if len(homes) == 1 else None


def _arguments(pid):
    """Bounded launch observation; callers bracket this with native identity."""
    try:
        if sys.platform == 'darwin':
            libc = ctypes.CDLL(None, use_errno=True)
            mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
            buffer = ctypes.create_string_buffer(_LAUNCH_BOUND)
            length = ctypes.c_size_t(len(buffer))
            if libc.sysctl(mib, 3, buffer, ctypes.byref(length), None, 0) != 0:
                return None
            return _darwin_arguments(buffer.raw[:length.value])
        if sys.platform.startswith('linux'):
            directory = Path('/proc') / str(pid)
            with (directory / 'cmdline').open('rb') as stream:
                raw = stream.read(_LAUNCH_BOUND + 1)
            with (directory / 'environ').open('rb') as stream:
                environment = stream.read(_LAUNCH_BOUND + 1)
            if len(raw) > _LAUNCH_BOUND or len(environment) > _LAUNCH_BOUND or not raw.endswith(b'\0'):
                return None
            homes = [os.fsdecode(value[5:]) for value in environment.split(b'\0') if value.startswith(b'HOME=')]
            return tuple(os.fsdecode(value) for value in raw[:-1].split(b'\0')), homes[0] if len(homes) == 1 else None
    except (OSError, ValueError, IndexError, OverflowError):
        return None
    return None


def _zcode_source_refusal(detail):
    from .errors import ProtocolRefusal
    return ProtocolRefusal('seat_teardown_zcode_config_unlocated', detail,
        remedy='Restore the acting runtime\'s unchanged, readable config sources or finish its blocking children before retrying teardown.')


def _ordinary_source(path, birth_before_ns, *, directory=False):
    """Require disjoint source-change and runtime-start intervals.

    Nanosecond stat fields do not promise nanosecond filesystem update clocks.
    Extend the source's latest change by a full second, covering coarse
    native update ticks even across a second boundary. The reader supplies the earliest
    possible start, including its platform's conversion uncertainty. Equality
    and overlap are unknown ordering, never testimony to loaded bytes.
    """
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('noncanonical source')
    info = path.stat()
    changed_before_ns = max(info.st_mtime_ns, info.st_ctime_ns) + 1_000_000_000
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or
            changed_before_ns >= birth_before_ns):
        raise ValueError('changed source')
    return info


def _zcode_servers(home, workspace, birth):
    """Ordinary CLI config resolution, read only command/args/enabled fields.

    ZCode 3.16.3 glm/zcode.cjs: C0/Hye, cKs/Vso/dyn, Dh/yKs (lines
    2173-2175). This primary clone is already a Git root, so discovery stops
    here. User definitions override the two project definitions. No MCP env
    override or config-path CLI option exists on this measured CLI surface.
    """
    try:
        if home is None:
            raise ValueError('runtime HOME unavailable')
        home = Path(home)
        _ordinary_source(home, birth, directory=True)
        _ordinary_source(workspace, birth, directory=True)
        sources = [workspace / 'zcode.json', workspace / '.zcode/config.json',
                   home / '.zcode/cli/config.json']
        servers = {}
        for path in sources:
            # Check existing parent directories too: deletion/creation of a
            # config after launch must not silently change its declaring source.
            for parent in reversed(path.parents):
                if parent in {home, workspace} or home in parent.parents or workspace in parent.parents:
                    if parent.exists():
                        _ordinary_source(parent, birth, directory=True)
            if not path.exists():
                if path.is_symlink():
                    raise ValueError('symlink source')
                continue
            before = _ordinary_source(path, birth)
            with path.open('rb') as stream:
                raw = stream.read(_LAUNCH_BOUND + 1)
            if len(raw) > _LAUNCH_BOUND:
                raise ValueError('oversize config')
            config = json.loads(raw, parse_constant=_invalid_json_constant)
            after = _ordinary_source(path, birth)
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            if identity(after) != identity(before) or not isinstance(config, dict):
                raise ValueError('changed/malformed config')
            validated_fields = {'$schema', 'modelStream', 'permission', 'storage', 'network',
                'features', 'memory', 'plugins', 'skills', 'skill', 'command', 'logging',
                'ui', 'toolConcurrency', 'modelAnomalyGuard', 'hooks'}
            # Hye's root schema passes unknown keys through. Do not access
            # their values (including provider credentials). Known validated
            # sections need their own full contract; conservatively refuse
            # those shapes rather than mistake a rejected config for a source.
            if set(config) & validated_fields:
                raise ValueError('unsupported validated config section')
            mcp = config.get('mcp', {})
            if not isinstance(mcp, dict):
                raise ValueError('unsupported config shape')
            definitions = mcp.get('servers', {}) if isinstance(mcp, dict) else None
            if not isinstance(definitions, dict) or len(definitions) > 10000:
                raise ValueError('malformed server declarations')
            for name, definition in definitions.items():
                # A higher-priority invalid/disabled definition shadows its
                # predecessor too. Never access env/provider/credential fields.
                servers.pop(name, None)
                # Hye validates the complete config, and Nso's stdio schema is
                # strict. Support only the credential-free subset we can
                # reproduce without accessing provider/hooks/env fields. An
                # unsupported shape is no testimony that ZCode loaded it.
                allowed = {'type', 'command', 'args', 'enabled', 'enable', 'cwd',
                           'alwaysLoad', 'protocolVersion', 'timeoutMs'}
                if not isinstance(definition, dict) or not set(definition) <= allowed:
                    raise ValueError('unsupported server shape')
                for field in ('enabled', 'enable', 'alwaysLoad'):
                    if field in definition and type(definition[field]) is not bool:
                        raise ValueError('invalid server boolean')
                if ('cwd' in definition and not isinstance(definition['cwd'], str) or
                        'protocolVersion' in definition and definition['protocolVersion'] not in {'auto', 'legacy', '2026-07-28'} or
                        'timeoutMs' in definition and (type(definition['timeoutMs']) not in (int, float) or
                                                      not 0 < definition['timeoutMs'] < float('inf'))):
                    raise ValueError('invalid server options')
                if (not isinstance(name, str) or not 1 <= len(name) <= 256 or
                        not isinstance(definition, dict) or definition.get('enabled') is False or
                        definition.get('enable') is False or definition.get('type', 'stdio') != 'stdio'):
                    continue
                command, args = definition.get('command'), definition.get('args', [])
                if (not isinstance(command, str) or not Path(command).is_absolute() or
                        not isinstance(args, list) or len(args) > 4096 or
                        any(not isinstance(arg, str) for arg in args)):
                    continue
                servers[name] = (tuple([command, *args]),
                    {'kind': 'config', 'path': str(path), 'server': name})
        return list(servers.values())
    except (OSError, ValueError, TypeError, RuntimeError):
        # No exception content: JSON diagnostics or file bytes can contain secrets.
        raise _zcode_source_refusal('The acting zcode runtime config source is absent, changed, noncanonical or unreadable.') from None


def _builtin_plugin(directory, name, birth):
    """The credential-free shipped manifest subset used by the REPL seeds."""
    try:
        manifest = directory / '.zcode-plugin/plugin.json'
        for path in (directory, manifest.parent):
            _ordinary_source(path, birth, directory=True)
        before = _ordinary_source(manifest, birth)
        with manifest.open('rb') as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            return False
        value = json.loads(raw, parse_constant=_invalid_json_constant)
        after = _ordinary_source(manifest, birth)
        if ((before.st_ino, before.st_mtime_ns, before.st_ctime_ns, before.st_size) !=
                (after.st_ino, after.st_mtime_ns, after.st_ctime_ns, after.st_size) or
                not isinstance(value, dict) or value.get('name') != name or
                not set(value) <= {'name', 'version', 'description', 'author', 'license', 'skills'} or
                not isinstance(value.get('version'), str) or
                re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value['version']) is None):
            return False
        for field in ('description', 'license'):
            if field in value and (not isinstance(value[field], str) or not value[field]):
                return False
        if 'author' in value and (not isinstance(value['author'], dict) or
                set(value['author']) != {'name'} or not isinstance(value['author']['name'], str) or
                not value['author']['name']):
            return False
        if 'skills' in value:
            if not isinstance(value['skills'], str) or not value['skills']:
                return False
            skills = directory / value['skills']
            if directory not in skills.parents:
                return False
            _ordinary_source(skills, birth, directory=True)
        return True
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def _zcode_hosts(jobs, workspace, ancestry, root, node, session, process, arguments):
    """Build match predicates from the exact acting CLI and its sources."""
    from .errors import IntegrityFailure, ProtocolRefusal
    from .wake_daemon_contract import AdapterBindingStore, DaemonCoordinate
    from .wake_daemon_adapters import _zcode_node_interpreter, adapter_contract_digest
    try:
        binding = AdapterBindingStore(root).read(DaemonCoordinate(root, node, 'zcode'))
        interpreter = _zcode_node_interpreter()
    except (ProtocolRefusal, IntegrityFailure, OSError):
        return {}
    if (binding['session_id'] != session or binding['workspace'] != str(workspace) or
            binding['adapter_digest'] != adapter_contract_digest('zcode')):
        return {}
    entry = Path(binding['executable'])
    population = {job['pid'] for job in jobs if job['cwd'] == str(workspace)}
    runtimes = []
    for pid in ancestry:
        candidate = process(pid)
        if (pid not in population or candidate is None or
                candidate['uid'] != os.getuid() or candidate['executable'] != interpreter):
            continue
        launch = arguments(pid)
        if launch is None:
            continue
        argv, home = launch
        if len(argv) < 4 or argv[1] != str(entry):
            continue
        try:
            if Path(argv[0]).resolve(strict=True) != interpreter:
                continue
        except (OSError, RuntimeError):
            continue
        options, offset = {}, 2
        while offset < len(argv):
            flag = argv[offset]
            if flag in {'--json', '--no-color', '--no-browser', '--verbose'}:
                offset += 1
            elif flag in {'--resume', '--prompt', '-p', '--cwd'} and offset + 1 < len(argv) and flag not in options:
                options[flag] = argv[offset + 1]
                offset += 2
            else:
                break
        if offset != len(argv) or options.get('--resume') != session or options.get('--cwd', str(workspace)) != str(workspace):
            continue
        runtimes.append((candidate, home))
    if len(runtimes) != 1:
        return {}
    runtime, home = runtimes[0]
    birth = runtime.get('source_start_before_ns')
    if type(birth) is not int or birth <= 0:
        return {}
    for index, pid in enumerate(ancestry[:ancestry.index(runtime['pid'])]):
        current = process(pid)
        if current is None or current['ppid'] != ancestry[index + 1]:
            return {}
    try:
        _ordinary_source(entry, birth)
        _ordinary_source(interpreter, birth)
    except (OSError, ValueError, RuntimeError):
        return {}
    servers = _zcode_servers(home, workspace, birth)
    matches = {runtime['pid']: {'kind': 'binding', 'path': str(entry)}}
    # Vri/Hri/Pli, zcode.cjs lines 14850/15462: this fixed bundled server is
    # invoked through the entry's plugin host, never by an argv word alone.
    bundle = entry.parents[3] if len(entry.parents) >= 4 else None
    plugin = entry.parent / 'packages/node-repl-host/dist/mcp/server.js'
    builtin = None
    packages = entry.parent / 'packages'
    if (entry.parts[-4:-1] == ('Contents', 'Resources', 'glm') and
            bundle is not None and bundle.suffix == '.app' and
            _builtin_plugin(packages / 'node-repl-host', 'node-repl-host', birth) and
            (_builtin_plugin(packages / 'browser-use-plugin', 'browser-use', birth) or
             _builtin_plugin(packages / 'zcode-cua-plugin', 'computer-use', birth))):
        try:
            _ordinary_source(plugin, birth)
            builtin = (str(interpreter), str(entry), '__zcode-plugin-host', str(plugin))
        except (OSError, ValueError, RuntimeError):
            pass
    for job in jobs:
        candidate = process(job['pid'])
        if (candidate is None or candidate['ppid'] != runtime['pid'] or
                candidate['uid'] != os.getuid()):
            continue
        launch = arguments(job['pid'])
        if launch is None:
            continue
        argv = tuple(launch[0])
        if builtin == argv and candidate['executable'] == interpreter:
            matches[candidate['pid']] = {'kind': 'bundle', 'path': str(bundle)}
            continue
        for declared, source in servers:
            if argv == declared and str(candidate['executable']) == declared[0]:
                matches[candidate['pid']] = source
                break
    return matches


def _identity(pid):
    try:
        return _native_identity(pid)
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        from .seat_teardown_activity import _unknown
        raise _unknown('Native process identity unreadable for PID ' + str(pid)) from exc


def _native_identity(pid):
    if sys.platform == 'darwin':
        from .codex_wait_liveness import _LIBPROC, _ProcBsdInfo
        value = _ProcBsdInfo()
        ctypes.set_errno(0)
        count = _LIBPROC.proc_pidinfo(ctypes.c_int(pid), ctypes.c_int(3),
            ctypes.c_ulonglong(0), ctypes.byref(value), ctypes.c_int(ctypes.sizeof(value)))
        if count <= 0 and ctypes.get_errno():
            raise OSError(ctypes.get_errno(), 'native process identity unavailable')
        if (count != ctypes.sizeof(value) or value.pbi_pid != pid or
                value.pbi_status == 5 or not value.pbi_start_tvsec or
                value.pbi_start_tvusec >= 1_000_000):
            return None
        return {'pid': pid, 'ppid': value.pbi_ppid, 'uid': value.pbi_uid,
            'birth': (value.pbi_start_tvsec, value.pbi_start_tvusec),
            'source_start_before_ns': value.pbi_start_tvsec * 1_000_000_000 + value.pbi_start_tvusec * 1000,
            'process_start_epoch': value.pbi_start_tvsec + value.pbi_start_tvusec / 1e6}
    if sys.platform.startswith('linux'):
        directory = Path('/proc') / str(pid)
        raw = (directory / 'stat').read_text()
        fields = raw.rpartition(') ')[2].split()
        if not raw.startswith(str(pid) + ' (') or len(fields) < 20 or fields[0] in {'Z', 'X'}:
            return None
        status = (directory / 'status').read_text()
        owner = [line.split()[2] for line in status.splitlines() if line.startswith('Uid:')]
        if len(owner) != 1:
            return None
        boot = [line.split()[1] for line in Path('/proc/stat').read_text().splitlines()
                if line.startswith('btime ')]
        ticks_per_second = os.sysconf('SC_CLK_TCK')
        if len(boot) != 1 or ticks_per_second <= 0:
            return None
        boot, ticks = int(boot[0]), int(fields[19])
        # /proc/stat btime converts boot to whole epoch seconds; /proc/PID/stat
        # starttime is in ticks. Neither its float sum nor fine stat fields
        # establish exact ordering. Retain the public timestamp, but use a
        # private lower bound with a whole second plus one tick of uncertainty.
        tick_ns = (1_000_000_000 + ticks_per_second - 1) // ticks_per_second
        return {'pid': pid, 'ppid': int(fields[1]), 'uid': int(owner[0]),
            'source_start_before_ns': (boot - 1) * 1_000_000_000 + ticks * 1_000_000_000 // ticks_per_second - tick_ns,
            'birth': (ticks,), 'process_start_epoch': boot + ticks / ticks_per_second}
    return None


def _executable(pid):
    try:
        return _native_executable(pid)
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        from .seat_teardown_activity import _unknown
        raise _unknown('Native process executable unreadable for PID ' + str(pid)) from exc


def _native_executable(pid):
    if sys.platform == 'darwin':
        from .codex_wait_liveness import _LIBPROC
        buffer = ctypes.create_string_buffer(4096)
        ctypes.set_errno(0)
        length = _LIBPROC.proc_pidpath(ctypes.c_int(pid), buffer, ctypes.c_uint32(len(buffer)))
        if length <= 0 and ctypes.get_errno():
            raise OSError(ctypes.get_errno(), 'native process executable unavailable')
        if length <= 0 or length >= len(buffer):
            return None
        value = os.fsdecode(buffer.value)
    elif sys.platform.startswith('linux'):
        value = os.readlink(Path('/proc') / str(pid) / 'exe')
    else:
        return None
    path = Path(value)
    return path.resolve(strict=True) if path.is_absolute() else None


def _process(pid):
    """Bracket executable observation by matching kernel PID/birth/parent/UID."""
    try:
        before = _identity(pid)
        if before is None:
            return None
        process_group = os.getpgid(pid)
        executable = _executable(pid)
        after = _identity(pid)
        if before != after or executable is None or os.getpgid(pid) != process_group:
            return None
        return dict(before, executable=executable, process_group=process_group)
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, OverflowError):
        return None
    except OSError as exc:
        from .seat_teardown_activity import _unknown
        raise _unknown('Native process group unreadable for PID ' + str(pid)) from exc


def _managed_cursor(executable):
    """The fixed downloaded-agent hierarchy, including custom user-data dirs.

    Cursor's shipped launcher resolves globalStorage/agent-cli's stable symlink
    into .local/share/cursor-agent/versions/BUILD/cursor-agent on Darwin/Linux.
    The invoking user's UID must own every storage component from User down.
    A dedicated bundled agent-cli/dist-package binary is also attributable.
    """
    parts = executable.parts
    suffix = ('User', 'globalStorage', 'anysphere.cursor-agent-worker',
        'agent-cli', '.local', 'share', 'cursor-agent', 'versions')
    if len(parts) >= 10 and tuple(parts[-10:-2]) == suffix and parts[-1] == 'cursor-agent':
        storage = executable
        try:
            for _ in range(10):
                if storage.stat().st_uid != os.getuid():
                    return False
                storage = storage.parent
            return stat.S_ISREG(executable.stat().st_mode) and os.access(executable, os.X_OK)
        except OSError:
            return False
    bundle_tail = ('agent-cli', 'dist-package', 'cursor-agent')
    if tuple(parts[-3:]) == bundle_tail:
        prefix = ('Cursor.app', 'Contents', 'Resources', 'app')
        if any(tuple(parts[index:index + 4]) == prefix for index in range(len(parts) - 6)):
            try:
                return (executable.stat().st_uid in {0, os.getuid()} and
                    stat.S_ISREG(executable.stat().st_mode) and os.access(executable, os.X_OK))
            except OSError:
                return False
    return False


def worker_cleanup_roots(row):
    """Closed cleanup testimony for host exclusion, not the public PID join.

    Group leaders and survivors can outlive their adapter. Their identifiers
    conservatively exclude a host match; they never authorize signalling.
    The caller has already joined the exact retained worker source.
    """
    from .recipient_mechanisms import _json
    from .records import WORKER_OUTCOME_CODES
    from .worker_bootstrap_protocol import validate_isolation_backend
    from .worker_errors import WorkerAdapterFailure
    try:
        audit = _json(row['detail'].encode('utf-8'))
        if type(audit) is not dict or set(audit) != {
                'adapter_pid', 'registered_process_groups', 'alive_after_cleanup', 'isolation_backend'}:
            return set()
        adapter = audit['adapter_pid']
        if adapter is not None and (type(adapter) is not int or not 1 < adapter <= 2147483647):
            return set()
        for field in ('registered_process_groups', 'alive_after_cleanup'):
            values = audit[field]
            if (type(values) is not list or len(values) > 100000 or
                    any(type(pid) is not int or not 1 < pid <= 2147483647 for pid in values) or
                    len(set(values)) != len(values)):
                return set()
        groups, alive = audit['registered_process_groups'], audit['alive_after_cleanup']
        roots = set(groups) | ({adapter} if adapter is not None else set())
        if groups != sorted(groups) or not set(alive) <= roots:
            return set()
        if audit['isolation_backend'] is not None:
            validate_isolation_backend(audit['isolation_backend'])
        if row['event'] == 'unknown':
            if row['code'] != 'worker_cleanup_unconfirmed' or not alive:
                return set()
        elif row['event'] != 'ended' or alive or row['code'] not in WORKER_OUTCOME_CODES | {'worker_completed_cleanup'}:
            return set()
        return roots | set(alive)
    except (ValueError, TypeError, KeyError, UnicodeError, WorkerAdapterFailure):
        return set()


def cursor_host_workers(jobs, workspace, *, floati_pids, zcode=None):
    """Name hosts only, excluding their descendants and receipt-owned work.

    floati_pids joins the named fleet's held native runner anchors and retained
    cleanup roots; it never discovers foreign roots. A managed ancestor also
    excludes a same-binary child which otherwise passes the path/cwd predicates.
    """
    result, observed, launches, owned = [], {}, {}, None

    def process(pid):
        if pid not in observed:
            observed[pid] = _process(pid)
        return observed[pid]

    def arguments(pid):
        if pid not in launches:
            before = process(pid)
            launch = _arguments(pid) if before is not None else None
            launches[pid] = launch if _process(pid) == before else None
        return launches[pid]

    matches = None
    if zcode is not None:
        root, node, session, ancestry = zcode
        matches = _zcode_hosts(jobs, workspace, ancestry, root, node, session, process, arguments)

    for job in jobs:
        if job['cwd'] != str(workspace):
            continue
        candidate = process(job['pid'])
        if (candidate is None or candidate['uid'] != os.getuid() or
                (candidate['pid'] not in matches if matches is not None else
                 not _managed_cursor(candidate['executable']))):
            continue
        if owned is None:
            roots = floati_pids()
            if roots is None:
                # An unfinalized crashed/uncertain dispatch may have orphaned
                # jobs. None of this root's native jobs earns a host exemption.
                return []
            owned = set(roots)
        current, seen, eligible = candidate, set(), True
        while current is not None:
            pid = current['pid']
            if (pid in seen or len(seen) >= 256 or pid in owned or
                    current.get('process_group') in owned):
                eligible = False
                break
            seen.add(pid)
            if matches is None and pid != candidate['pid'] and _managed_cursor(current['executable']):
                # Only the outer host itself is exempt; never a nested job,
                # even if the parent has since moved its working directory.
                eligible = False
                break
            parent = current['ppid']
            if parent in {0, 1}:
                break
            current = process(parent)
            if current is None:
                eligible = False
        if not eligible or _process(candidate['pid']) != candidate:
            continue
        testimony = {'pid': candidate['pid'],
            'process_start_epoch': candidate['process_start_epoch'],
            'executable': str(candidate['executable']),
            'reason': 'cursor_managed_agent_exact_workspace' if matches is None else 'zcode_declared_session_host'}
        if matches is not None:
            testimony['declaring_source'] = matches[candidate['pid']]
        result.append(testimony)
    if matches is not None and result:
        # The runtime anchors all child matches. Revalidate launch and native
        # identities together so PID reuse/reparenting cannot earn an exemption.
        for pid, identity in observed.items():
            if identity is not None and _process(pid) != identity:
                return []
        for pid, launch in launches.items():
            if launch is not None and (_process(pid) != observed[pid] or _arguments(pid) != launch):
                return []
    return sorted(result, key=lambda row: row['pid'])

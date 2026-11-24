"""Read-only, bounded work and native-cwd proof for one declared seat.

This is observation at a cutoff, not a lock against a later edit or process.
Identity, held claim and primary-clone declaration are the caller's gates.
"""
from __future__ import annotations

import os
import json
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from .errors import IntegrityFailure, ProtocolRefusal
from .git_process import fixed_git_command, fixed_git_environment

_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_DIRECTORIES = 10000
_MAX_ENTRIES = 100000
_MAX_DEPTH = 64
_MAX_PROCESSES = 100000
_TIMEOUT = 30
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
# ps(1)'s documented modifiers, plus H from Apple's TH_STATE_HALTED
# mapping. Its state buffer is 16 bytes including the terminating NUL.
# '?' is a native unknown state, not readable proof of absence.
_DARWIN_STATE = re.compile(r"[RSDTIUZWXH?][AELNSVWXs+<>]{0,14}\Z")
_PID_CELL = re.compile(r"[0-9]{1,10}\Z")
_DARWIN_UID_CELL = re.compile(r"(?:[0-9]{1,10}|-[1-9][0-9]{0,9})\Z")
NATIVE_UNKNOWN_REASONS = frozenset({"cwd_missing", "cwd_unreadable", "cwd_unresolvable"})


def _darwin_uid(token):
    """Normalize Apple's signed ps rendering of unsigned 32-bit ownership."""
    if token is None or _DARWIN_UID_CELL.fullmatch(token) is None:
        return None
    value = int(token)
    # ps/keyword.c uses UIDFMT "d" with UINT storage on Apple. Preserve
    # the bit pattern rather than mistaking a readable high UID for unknown.
    if value < 0:
        if value < -2147483648:
            return None
        value += 4294967296
    elif value > 4294967295:
        return None
    # XNU setreuid reserves (uid_t)-1 as no change; kauth.h explicitly
    # marks UINT32_MAX-100 as an invalid UID. Refuse their signed AND
    # unsigned representations rather than treating ambiguous owner
    # testimony as proof that a process belongs outside this UID.
    if value in {4294967295, 4294967195}:
        return None
    return value


def _unknown(detail):
    return ProtocolRefusal("seat_teardown_activity_unknown", detail,
                           remedy="Restore readable, complete Git and native process evidence for every named workspace, then retry teardown.")


def _command(argv, *, env=None):
    """Cap bytes while reading, rather than capturing unlimited child output."""
    process = subprocess.Popen(argv, cwd="/", env=env or fixed_git_environment("/usr/bin/git"),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + _TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _unknown("Activity metadata command timed out: " + repr(argv))
                for key, _ in selector.select(min(remaining, 0.25)):
                    block = os.read(key.fileobj.fileno(), 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    buffers[key.data].extend(block)
                    if sum(map(len, buffers.values())) > _MAX_OUTPUT:
                        raise _unknown("Activity metadata command exceeded its output bound: " + repr(argv))
        status = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        return subprocess.CompletedProcess(argv, status, bytes(buffers["stdout"]), bytes(buffers["stderr"]))
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        # Helpers that stayed in this session (lsof's child, a git maintenance
        # process) are not the direct pid wait() collected.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.stdout.close()
        process.stderr.close()


def _text(raw, subject):
    try:
        return raw.decode("utf-8", "strict")
    except (UnicodeError, AttributeError) as exc:
        raise _unknown("Malformed UTF-8 activity evidence for " + subject) from exc


def _git(root, *arguments):
    result = _command(fixed_git_command("/usr/bin/git", root, arguments))
    if result.returncode or result.stderr:
        raise _unknown("Git activity unavailable for " + str(root) + ": " +
                       _text(result.stderr, str(root))[:2048])
    return _text(result.stdout, str(root))


def _canonical_directory(path):
    try:
        resolved = path.resolve(strict=True)
    except RuntimeError as exc:
        raise _unknown("Cannot resolve activity directory (symlink loop): " + str(path)) from exc
    if not path.is_absolute() or resolved != path or not path.is_dir():
        raise _unknown("Not an ordinary canonical activity directory: " + str(path))
    # Resolve also rejects a symlinked ancestor without traversing it during inventory.
    return path


def _inventory(home, repository):
    blob = _git(repository, "worktree", "list", "--porcelain", "-z")
    if not blob.endswith("\0\0"):
        raise _unknown("Malformed Git worktree inventory for " + str(repository))
    selected = set()
    for block in blob[:-2].split("\0\0"):
        fields = block.split("\0")
        if not fields or not fields[0].startswith("worktree "):
            raise _unknown("Malformed Git worktree inventory for " + str(repository))
        path = Path(fields[0][9:])
        if not path.is_absolute() or path in selected:
            raise _unknown("Malformed/duplicate Git worktree coordinate: " + str(path))
        heads = [field[5:] for field in fields[1:] if field.startswith("HEAD ")]
        if len(heads) != 1 or not _SHA.fullmatch(heads[0]):
            raise _unknown("Missing/malformed HEAD in Git worktree inventory: " + str(path))
        if any(not (field.startswith(("HEAD ", "branch ", "locked ", "prunable ")) or
                    field in {"detached", "locked", "prunable"}) for field in fields[1:]):
            raise _unknown("Unknown Git worktree inventory field: " + str(path))
        selected.add(path)
    if repository not in selected:
        raise _unknown("Declared primary missing from Git worktree inventory: " + str(repository))
    pending = [(home, 0)]
    directories = entries_seen = 0
    while pending:
        directory, depth = pending.pop()
        directories += 1
        if directories > _MAX_DIRECTORIES or depth > _MAX_DEPTH:
            raise _unknown("Seat HOME inventory exceeded its directory/depth bound: " + str(directory))
        with os.scandir(directory) as entries:
            for entry in entries:
                entries_seen += 1
                if entries_seen > _MAX_ENTRIES:
                    raise _unknown("Seat HOME inventory exceeded its entry bound: " + str(directory))
                mode = entry.stat(follow_symlinks=False).st_mode
                if entry.name == ".git":
                    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                        raise _unknown("Nonordinary .git inventory marker: " + entry.path)
                    selected.add(directory)
                    # Git administration is checked by Git; it is not a workspace tree.
                    continue
                if stat.S_ISDIR(mode):
                    pending.append((Path(entry.path), depth + 1))
    return sorted(selected, key=str)


def _worktree(root):
    _canonical_directory(root)
    if _git(root, "rev-parse", "--show-toplevel").rstrip("\n") != str(root):
        raise _unknown("Inventory root is not the exact Git workspace: " + str(root))
    shallow = _git(root, "rev-parse", "--is-shallow-repository").strip()
    if shallow != "false":
        raise _unknown("Complete Git reachability is unavailable (shallow/malformed) for " + str(root))
    head = _git(root, "rev-parse", "--verify", "HEAD").strip()
    if not _SHA.fullmatch(head):
        raise _unknown("Malformed Git HEAD: " + str(root))
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none")
    tracked, untracked = [], []
    fields = status.split("\0")
    if fields.pop() != "":
        raise _unknown("Unterminated Git status: " + str(root))
    index = 0
    while index < len(fields):
        record = fields[index]
        if len(record) < 4 or record[2] != " " or not record[3:] or any(c not in " MADRCUT?!" for c in record[:2]):
            raise _unknown("Malformed Git status: " + str(root))
        if record.startswith("?? "):
            untracked.append(record[3:])
        elif record.startswith("!! "):
            raise _unknown("Unexpected ignored Git status: " + str(root))
        else:
            tracked.append(record)
        if "R" in record[:2] or "C" in record[:2]:
            index += 1
            if index >= len(fields) or not fields[index]:
                raise _unknown("Incomplete rename/copy Git status: " + str(root))
            tracked.append("source " + fields[index])
        index += 1
    commits = _git(root, "rev-list", "HEAD", "--not", "--remotes").splitlines()
    if any(not _SHA.fullmatch(value) for value in commits):
        raise _unknown("Malformed Git reachability: " + str(root))
    return {"workspace": str(root), "head": head, "tracked_changes": tracked,
            "untracked_files": untracked, "unpushed_commits": commits}


def _ancestry(parents):
    pid = os.getpid()
    result = []
    while pid:
        if pid in result or pid not in parents or len(result) >= 256:
            raise _unknown("Current teardown PID ancestry is incomplete or malformed at PID " + str(pid))
        result.append(pid)
        pid = parents[pid]
    return result


def _darwin_pid_snapshot():
    """One metadata retry; never retry cwd observation or the transaction."""
    for attempt in range(2):
        try:
            snapshot = _darwin_pid_snapshot_once()
            _, states, owners = snapshot
            if not attempt and any(states[pid] == "?" and owners[pid] == os.getuid() for pid in states):
                continue
            # A second readable ? state preserves owner/parent testimony.
            # It cannot prove exit; §23 lets readable cwd decide location.
            return snapshot
        except (OSError, subprocess.SubprocessError, ProtocolRefusal) as exc:
            if attempt:
                raise _unknown("Native PID metadata unknown after one retry: " + str(exc)) from exc


def _darwin_pid_snapshot_once():
    table = _command(["/bin/ps", "-A", "-o", "pid=,ppid=,uid=,state="])
    if table.returncode or table.stderr:
        raise _unknown("Native PID ancestry observation is unavailable")
    parents, states, owners = {}, {}, {}
    for row_number, line in enumerate(_text(table.stdout, "PID ancestry").splitlines(), 1):
        cells = line.split()
        # Never echo the whole row: extra columns and invalid tokens may
        # contain unrelated command arguments or terminal control bytes.
        fields = (cells + [None, None, None, None])[:4]
        safe_pid = [value if value is not None and _PID_CELL.fullmatch(value)
                    else "<invalid>" for value in fields[:2]]
        owner = fields[2]
        safe_owner = owner if owner is not None and _DARWIN_UID_CELL.fullmatch(owner) else "<invalid>"
        uid = _darwin_uid(owner)
        state = fields[3]
        safe_state = repr(state) if state is not None and _DARWIN_STATE.fullmatch(state) else "<invalid>"
        coordinate = ("row=" + str(row_number) + " pid=" + safe_pid[0] +
                      " ppid=" + safe_pid[1] + " uid=" + safe_owner + " state=" + safe_state)
        if len(cells) != 4 or "<invalid>" in safe_pid or uid is None or safe_state == "<invalid>":
            raise _unknown("Malformed native PID ancestry table: " + coordinate)
        pid, parent = map(int, cells[:2])
        if pid <= 0 or pid > 2147483647 or parent > 2147483647:
            raise _unknown("Invalid native PID ancestry coordinate: " + coordinate)
        if pid in parents or len(parents) >= _MAX_PROCESSES:
            raise _unknown("Duplicate/oversize native PID ancestry table: " + coordinate)
        parents[pid] = parent
        states[pid] = state[0]
        owners[pid] = uid
    if not parents:
        raise _unknown("Empty native PID ancestry table")
    return parents, states, owners


def _darwin_snapshot():
    uid = os.getuid()
    before, _, _ = _darwin_pid_snapshot()
    executable = next((path for path in ("/usr/sbin/lsof", "/usr/bin/lsof") if Path(path).is_file()), None)
    if executable is None:
        raise _unknown("Fixed native cwd observer lsof is unavailable")
    observation = _command([executable, "-n", "-P", "-a", "-u", str(uid), "-d", "cwd", "-F0pfn"])
    if observation.returncode not in (0, 1) or observation.stderr:
        raise _unknown("Native cwd observation is incomplete: " + _text(observation.stderr, "lsof")[:2048])
    rows, pid, cwd_field, seen, listed, unreadable = [], None, False, set(), set(), set()
    blob = _text(observation.stdout, "native cwd")
    if blob and not blob.endswith(("\0", "\0\n")):
        raise _unknown("Unterminated native cwd observation")
    for field in blob.split("\0"):
        field = field.lstrip("\n")
        if not field:
            continue
        if field.startswith("p") and field[1:].isdigit() and int(field[1:]) > 0:
            pid, cwd_field = int(field[1:]), False
            if pid > 2147483647 or pid in listed or len(listed) >= _MAX_PROCESSES:
                raise _unknown("Duplicate/oversize native cwd process coordinates")
            listed.add(pid)
        elif field == "fcwd" and pid is not None and not cwd_field:
            cwd_field = True
        elif field.startswith("n/") and pid is not None and cwd_field and pid not in seen and pid not in unreadable:
            rows.append({"pid": pid, "cwd": field[1:]})
            seen.add(pid)
        elif field.startswith("n") and pid is not None and cwd_field and pid not in seen and pid not in unreadable:
            # A cwd error is uncertainty for this PID, not ownership of it.
            # Never echo arbitrary lsof diagnostic bytes into the report.
            unreadable.add(pid)
        else:
            raise _unknown("Malformed native cwd field: " + repr(field[:256]))
    if len(rows) > _MAX_PROCESSES:
        raise _unknown("Incomplete/oversize native cwd observation")
    after, states, owners = _darwin_pid_snapshot()
    rows = [row for row in rows if row['pid'] in after and states[row['pid']] not in {'Z', 'X'}]
    # A ps child is reaped before lsof starts. Likewise a process may exit
    # during observation, and terminal zombies have no live cwd. Retain
    # uncertainty for every invoking-UID PID present on both sides and still
    # nonterminal. Other UIDs are outside the ruled cwd population; their
    # readable parents remain in the full computed ancestry.
    # §23 makes that uncertainty blocking only with own durable PID testimony.
    # No names or caller-provided PID lists enter.
    persistent_live = {pid for pid in before.keys() & after.keys()
                       if owners[pid] == uid and states[pid] not in {"Z", "X"}}
    unknown = [{"pid": pid, "reason": "cwd_unreadable" if pid in unreadable else "cwd_missing"}
               for pid in sorted(persistent_live - seen)]
    ancestry = _ancestry(after)
    return rows, ancestry, unknown


def _linux_snapshot():
    parents, rows, unknown = {}, [], []
    uid = os.getuid()
    with os.scandir("/proc") as entries:
        pids = sorted(int(entry.name) for entry in entries if entry.name.isdigit())
    if len(pids) > _MAX_PROCESSES:
        raise _unknown("Native /proc observation exceeded its process bound")
    for pid in pids:
        directory = Path("/proc") / str(pid)
        try:
            # Non-dumpable /proc inodes can be root-owned even when the
            # process belongs to this UID. Use actual credential metadata,
            # matching ps uid/lsof's effective-owner selection on Darwin.
            with (directory / "status").open("rb") as handle:
                owner_raw = handle.read(8193)
            if len(owner_raw) > 8192:
                raise _unknown("Oversize native /proc owner metadata for PID " + str(pid))
            owner_fields = [line[4:].split() for line in _text(owner_raw, "PID " + str(pid)).splitlines()
                            if line.startswith("Uid:")]
            if (len(owner_fields) != 1 or len(owner_fields[0]) != 4 or
                    any(not _PID_CELL.fullmatch(cell) or int(cell) > 4294967295
                        for cell in owner_fields[0])):
                raise _unknown("Malformed native /proc owner for PID " + str(pid))
            owner = int(owner_fields[0][1])
            with (directory / "stat").open("rb") as handle:
                raw = handle.read(8193)
            if len(raw) > 8192:
                raise _unknown("Oversize /proc stat for PID " + str(pid))
            text = _text(raw, str(pid))
            prefix, separator, tail = text.rpartition(") ")
            cells = tail.split()
            if not separator or not prefix.startswith(str(pid) + " (") or len(cells) < 2 or not cells[1].isdigit():
                raise _unknown("Malformed /proc ancestry for PID " + str(pid))
            parents[pid] = int(cells[1])
            if owner == uid and cells[0] not in {"Z", "X"}:
                try:
                    cwd = os.readlink(directory / "cwd")
                except (FileNotFoundError, ProcessLookupError) as exc:
                    if not isinstance(exc, ProcessLookupError) and directory.exists():
                        # Exited, unreaped zombies retain /proc/PID but no cwd.
                        from .waiter_invocation import _native_parent
                        if _native_parent(pid) is None:
                            parents.pop(pid, None)
                        else:
                            unknown.append({"pid": pid, "reason": "cwd_missing"})
                    else:
                        parents.pop(pid, None)
                except OSError:
                    unknown.append({"pid": pid, "reason": "cwd_unreadable"})
                else:
                    rows.append({"pid": pid, "cwd": cwd})
        except (FileNotFoundError, ProcessLookupError):
            parents.pop(pid, None)
        except OSError as exc:
            raise _unknown("Native /proc metadata/cwd unreadable for PID " + str(pid)) from exc
    return rows, _ancestry(parents), unknown


def _native_snapshot():
    if sys.platform == "darwin":
        return _darwin_snapshot()
    if sys.platform.startswith("linux"):
        return _linux_snapshot()
    raise _unknown("Native cwd observation unsupported on " + sys.platform)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate audit field")
        result[key] = value
    return result


def _worker_audit_pid(row):
    """Read only WorkerRunner's terminal cleanup audit, never generic detail.

    Unsupported detail is not PID testimony. Its ledger record remains
    readable history; §23 does not make it a global blocking census.
    """
    from .archive_execution_replay import worker_audit_pid
    return worker_audit_pid(row)


def _worker_pid_receipts(root, node, *, snapshot=False, all_nodes=False):
    """Join accepted worker cleanup audits; snapshot mode avoids lock creation."""
    from .seat_execution import _history
    from .snapshot import _owned_epoch_archives
    from .jsonl import read_records_snapshot
    from .workers import WorkerReceipts, WORKER_KINDS

    from .bus_epoch import observe_epoch_roll_state
    from .seat_teardown_prefix import certified_executions
    history = (certified_executions(root) if not snapshot
        and observe_epoch_roll_state(root)['classification'] == 'absent' else None)
    if history is None:
        history = _history(root, snapshot=True) if snapshot else _history(root)
        archives = _owned_epoch_archives(root)
    else:
        archives = ()
    workers = []
    for archive in reversed(archives):
        relative = archive.relative_to(root.tenant_home) / "receipts/workers.jsonl"
        if archive.is_file() and not archive.is_symlink():
            from .epoch_history import read_archived_bytes
            from .jsonl import _decode_path_records
            raw = read_archived_bytes(archive, "receipts/workers.jsonl")
            if raw:
                workers.extend(_decode_path_records(
                    archive / "receipts/workers.jsonl", root.tenant_id,
                    frozenset(WORKER_KINDS), raw))
        else:
            workers.extend(read_records_snapshot(root, relative, allowed_kinds=WORKER_KINDS))
    ledger = WorkerReceipts(root)
    workers.extend(ledger.records())
    ledger._project(workers)
    from .archive_execution_replay import _worker_pid_join
    if not all_nodes:
        return _worker_pid_join(history, workers, node)
    result = {}
    owners = {row['node_id'] for row in workers} | {
        row['node_id'] for row in history if row.get('kind') == 'seat_execution'}
    for owner in sorted(owners):
        for pid, ids in _worker_pid_join(history, workers, owner).items():
            result.setdefault(pid, []).extend(ids)
    # The public historical PID join deliberately selects only adapter_pid.
    # Host classification additionally excludes each accepted group/survivor
    # root, without changing that historical read contract.
    sources = {row['id'] for row in workers}
    from .seat_host_worker import worker_cleanup_roots
    for row in history:
        if (row.get('kind') == 'seat_execution' and row['source_kind'] == 'worker' and
                row['event'] in {'ended', 'unknown'} and row['source_receipt_id'] in sources):
            for pid in worker_cleanup_roots(row):
                result.setdefault(pid, []).append(row['id'])
    return {pid: sorted(set(ids)) for pid, ids in result.items()}


def _host_work_pids(root, node):
    from .worker_launch_anchors import live_worker_runner_pids
    parents = live_worker_runner_pids(root)
    if parents is None:
        return None
    return set(_worker_pid_receipts(root, node, all_nodes=True)) | parents


def _declared_native_scope(home, root, node):
    """Derive location and PID ownership only from validated seat records."""
    if root is None and node is None:
        return [home], {}
    from .root import FloatiRoot
    from .registry import Registry
    from .lane_workspaces import LaneWorkspaces

    if not isinstance(root, FloatiRoot) or node is None:
        raise _unknown("Native seat scope requires a validated root and node together")
    node = Registry(root).resolve_node_id(node)
    Registry(root).require_active(node)
    lanes = LaneWorkspaces(root)
    opened = lanes._open_records(node)
    roots = {home}
    if opened:
        lanes_root, repositories = lanes._declarations()
        for record in opened:
            if record["repo"] not in repositories:
                raise _unknown("Active native lease names an undeclared repository")
            roots.add(_canonical_directory(lanes_root / record["path"]))
    return sorted(roots, key=str), _worker_pid_receipts(root, node)


def inspect_seat_activity(home: Path, repository: Path, *, root=None, node=None, session=None,
                          _retiring_waiter=None) -> dict:
    """Return complete workspace/native findings, or refuse before any mutation."""
    home, repository = Path(home), Path(repository)
    unknown, worktrees = [], []
    try:
        _canonical_directory(home)
        _canonical_directory(repository)
        if repository != home / "repo":
            raise _unknown("Declared activity repository must be HOME/repo: " + str(repository))
        native_roots, owned_pids = _declared_native_scope(home, root, node)
        workspaces = sorted(set(_inventory(home, repository)) | (set(native_roots) - {home}), key=str)
    except (OSError, subprocess.SubprocessError, ProtocolRefusal, IntegrityFailure) as exc:
        raise _unknown("Workspace inventory unknown for " + str(home) + ": " + str(exc)) from exc
    for workspace in workspaces:
        try:
            worktrees.append(_worktree(workspace))
        except (OSError, subprocess.SubprocessError, ProtocolRefusal) as exc:
            unknown.append(str(workspace) + ": " + str(exc))
    jobs, ancestry, native_unknown, host_workers, population = [], [], [], [], []
    try:
        rows, ancestry, uncertain = _native_snapshot()
        if (not isinstance(ancestry, list) or not ancestry or ancestry[0] != os.getpid() or
                any(type(pid) is not int or pid <= 0 for pid in ancestry) or len(set(ancestry)) != len(ancestry)):
            raise _unknown("Malformed computed process ancestry")
        if (not isinstance(rows, list) or not isinstance(uncertain, list) or
                len(rows) + len(uncertain) > _MAX_PROCESSES):
            raise _unknown("Malformed/oversize native process rows")
        if root is not None:
            from .registry import Registry
            if Registry(root).require_active(node)['role'].casefold() == 'codex':
                from .seat_host_worker import codex_session_ancestry
                hosts = codex_session_ancestry(root, node, repository, session)
                # Only exact native host ancestors extend this exemption;
                # shared-daemon descendants and ordinary chat children do not.
                ancestry = list(dict.fromkeys([*ancestry, *hosts]))
                if len(ancestry) > 256:
                    raise _unknown('Acting session ancestry exceeds its process bound')
        seen = set()
        for row in uncertain:
            if (type(row) is not dict or set(row) != {"pid", "reason"} or
                    type(row["pid"]) is not int or not 1 <= row["pid"] <= 2147483647 or
                    row["pid"] in seen or type(row["reason"]) is not str or
                    row["reason"] not in NATIVE_UNKNOWN_REASONS):
                raise _unknown("Malformed native process uncertainty")
            seen.add(row["pid"])
            native_unknown.append({**row, "receipt_ids": owned_pids.get(row["pid"], [])})
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"pid", "cwd"} or
                    type(row["pid"]) is not int or not 1 <= row["pid"] <= 2147483647 or row["pid"] in seen or
                    not isinstance(row["cwd"], str) or not row["cwd"].startswith("/") or "\0" in row["cwd"]):
                raise _unknown("Malformed native process row: " + repr(row))
            seen.add(row["pid"])
            try:
                cwd = Path(row["cwd"]).resolve(strict=True)
                if not cwd.is_dir():
                    raise OSError("cwd is not a directory")
            except (OSError, RuntimeError):
                native_unknown.append({"pid": row["pid"], "reason": "cwd_unresolvable",
                                       "receipt_ids": owned_pids.get(row["pid"], [])})
                continue
            if any(cwd == path or path in cwd.parents for path in native_roots):
                population.append({"pid": row["pid"], "cwd": str(cwd)})
                if row["pid"] not in ancestry:
                    jobs.append({"pid": row["pid"], "cwd": str(cwd)})
        if _retiring_waiter is not None:
            # Only the read-only preflight uses this verified invocation.
            # Its caller joined the native start receipt and kernel lease.
            # Successful teardown resamples after actual exit, with no PID
            # exclusion and no saved preflight activity testimony.
            from .waiter_invocation import _same_process
            if not _same_process(_retiring_waiter['pid'], _retiring_waiter['process_start_epoch']):
                raise _unknown('retiring waiter changed during preflight')
            jobs = [row for row in jobs if row['pid'] != _retiring_waiter['pid']]
        if root is not None:
            from .registry import Registry
            harness = Registry(root).require_active(node)['role'].casefold()
            if harness in {'cursor', 'zcode'}:
                from .seat_host_worker import cursor_host_workers
                host_workers = cursor_host_workers(population if harness == 'zcode' else jobs, repository,
                    floati_pids=lambda: _host_work_pids(root, node),
                    zcode=(root, node, session, ancestry) if harness == 'zcode' else None)
                host_pids = {row['pid'] for row in host_workers}
                jobs = [row for row in jobs if row['pid'] not in host_pids]
        native_unknown.sort(key=lambda row: row["pid"])
        for row in native_unknown:
            if row["receipt_ids"]:
                unknown.append("PID " + str(row["pid"]) + " " + row["reason"] +
                               "; own native receipts " + ", ".join(row["receipt_ids"]))
    except (OSError, subprocess.SubprocessError, ProtocolRefusal) as exc:
        if isinstance(exc, ProtocolRefusal) and exc.code == 'seat_teardown_zcode_config_unlocated':
            raise
        unknown.append("native process evidence: " + str(exc))
    affected = []
    for row in worktrees:
        reasons = [key for key in ("tracked_changes", "untracked_files", "unpushed_commits") if row[key]]
        if reasons:
            affected.append(row["workspace"] + ": " + ", ".join(reasons))
    affected.extend("PID " + str(row["pid"]) + " cwd " + row["cwd"] for row in jobs)
    # Positive activity is already enough to refuse teardown. Unreadable
    # evidence elsewhere cannot turn an observed job into mere uncertainty.
    # Retain both findings so resolving the job does not hide the next gate.
    if affected:
        raise ProtocolRefusal("seat_teardown_activity_in_flight", "; ".join(affected + unknown),
                              remedy="Finish and publish or explicitly preserve the listed workspace work, and stop the listed native jobs before retrying teardown.")
    if unknown:
        raise _unknown("; ".join(unknown))
    result = {"workspace_set": [str(path) for path in workspaces], "worktrees": worktrees,
            "native_jobs": jobs, "exempt_ancestry": ancestry,
            "native_unknown": native_unknown, "native_roots": [str(path) for path in native_roots],
            "native_scope": {"population": "invoking_uid", "uid": os.getuid()}}
    if host_workers:
        result['native_host_workers'] = host_workers
    return result

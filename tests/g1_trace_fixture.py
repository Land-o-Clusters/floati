"""Fixture-private native Git testimony for the positive detachment control.

Trace2 SIDs are copied across POSIX daemonize's fork. A successful setup.c
exit records the fork parent; a second successful atexit plus completed
repack testimony records the daemon's continuation. No SID is a daemon PID.
The positive control proves detachment, independent of sampling phase. G2
keeps its separate, strict instantaneous process observer.
"""
from tests.readiness import Readiness
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time


def _rows_for(rows, sid):
    return [row for row in rows if row.get("sid") == sid]


def _command(rows, sid, name):
    return any(row.get("event") == "cmd_name" and row.get("name") == name
               for row in _rows_for(rows, sid))


def _terminals(rows, sid):
    return [row for row in _rows_for(rows, sid) if row.get("event") == "atexit"]


def _edge(rows, parent, name):
    """Join actual native start to the parent's successful child_exit PID.

    This PID is the launcher's PID. It is never used for inspection/signaling.
    A mere child_start request cannot satisfy the join.
    """
    edges = []
    for launch in _rows_for(rows, parent):
        argv = launch.get("argv", [])
        if launch.get("event") != "child_start" or len(argv) < 2 or argv[1] != name:
            continue
        ended = [row for row in _rows_for(rows, parent)
                 if row.get("event") == "child_exit"
                 and row.get("child_id") == launch.get("child_id") and row.get("code") == 0]
        for end in ended:
            for start in rows:
                sid = start.get("sid", "")
                if start.get("event") != "start" or not sid.startswith(parent + "/"):
                    continue
                segment = sid[len(parent) + 1:]
                match = re.fullmatch(r"[^/]+-P([0-9a-fA-F]+)", segment)
                if not match or int(match.group(1), 16) != end.get("pid"):
                    continue
                child_argv = start.get("argv", [])
                if len(child_argv) < 2 or child_argv[1:] != argv[1:]:
                    continue
                if _command(rows, sid, name):
                    edges.append((sid, launch))
    return edges


def detached_witness(rows):
    """Return genuine completed detachment testimony or refuse with ValueError.

    Modern Git detaches maintenance; 2.43 instead detaches its GC child.
    Recent maintenance can delegate the repack to a synchronous GC child.
    Both native POSIX paths retain the successful fork-parent setup.c exit.
    Line numbers are reported, not pinned to unpatched upstream sources.
    """
    roots = [row["sid"] for row in rows if row.get("event") == "start"
             and "commit" in row.get("argv", [])
             and any(row.get("argv", [])[index:index + 2] == ["-m", "gate trigger"]
                     for index in range(len(row.get("argv", []))))
             and "/" not in row.get("sid", "")]
    if len(roots) != 1 or not _command(rows, roots[0], "commit"):
        raise ValueError("one exact native trigger commit is required")
    root = roots[0]
    if len(_terminals(rows, root)) != 1 or _terminals(rows, root)[0].get("code") != 0:
        raise ValueError("trigger commit did not finish successfully")
    for maintenance, launch in _edge(rows, root, "maintenance"):
        if "--auto" not in launch["argv"]:
            continue
        candidates = [(maintenance, "maintenance")] if "--detach" in launch["argv"] else [
            (sid, "gc") for sid, gc_launch in _edge(rows, maintenance, "gc")
            if "--auto" in gc_launch["argv"]]
        for fork, mode in candidates:
            local = _rows_for(rows, fork)
            fork_exits = [row for row in local if row.get("event") == "exit"
                          and row.get("file") == "setup.c" and row.get("code") == 0]
            terminal = _terminals(rows, fork)
            if len(fork_exits) != 1 or len(terminal) != 2 or any(
                    row.get("code") != 0 for row in terminal):
                continue
            if mode == "maintenance":
                enter = [row for row in local if row.get("event") == "region_enter"
                         and row.get("category") == "maintenance" and row.get("label") == "detach"]
                leave = [row for row in local if row.get("event") == "region_leave"
                         and row.get("category") == "maintenance" and row.get("label") == "detach"]
                if len(enter) != 1 or len(leave) != 1 or not isinstance(enter[0].get("nesting"), int)\
                        or enter[0]["nesting"] != leave[0].get("nesting"):
                    continue
            repack_parents = [fork]
            if mode == "maintenance":
                for gc, gc_launch in _edge(rows, fork, "gc"):
                    flags = {"--auto", "--no-detach", "--skip-foreground-tasks"}
                    gc_terminals = _terminals(rows, gc)
                    if (flags.issubset(gc_launch["argv"]) and len(gc_terminals) == 1
                            and gc_terminals[0].get("code") == 0):
                        repack_parents.append(gc)
            repacks = [sid for parent in repack_parents for sid, _ in _edge(rows, parent, "repack")
                       if len(_terminals(rows, sid)) == 1
                       and _terminals(rows, sid)[0].get("code") == 0]
            if not repacks:
                continue
            return {"commit_sid": root, "maintenance_sid": maintenance,
                    "fork_sid": fork, "fork_mode": mode,
                    "fork_parent_exit": fork_exits[0], "fork_terminals": terminal,
                    "completed_repack_sids": repacks}
    raise ValueError("no completed native detached maintenance/GC fork")


def _read_trace(path):
    raw = path.read_bytes() if path.exists() else b""
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("fixture trace exceeded its bounded size")
    lines = raw.splitlines(keepends=True)
    rows = [json.loads(line) for line in lines if line.endswith(b"\n")]
    return raw, rows


def _finished(rows):
    """No launched native child is left without its exit receipt."""
    starts = [(row.get("sid"), row.get("child_id")) for row in rows
              if row.get("event") == "child_start"]
    exits = [(row.get("sid"), row.get("child_id")) for row in rows
             if row.get("event") == "child_exit"]
    return sorted(starts) == sorted(exits)


def run_positive_fixture():
    from tests import test_gate_z2_of_fl_1 as gate
    from tests.temp_roots import REAL_TEMP_ROOT
    scratch = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
    base = Path(scratch.name)
    completed_cleanup = False
    try:
        home = base / "bare-home"
        home.mkdir()
        projection = gate._projection_repository(base, home=str(home))
        trace = home / "trigger-trace.jsonl"
        environment = gate._git_environment(str(home))
        (projection / "trigger.txt").write_text("trigger\n")
        subprocess.run(["/usr/bin/git", *gate.FORCE_KNOBS, "add", "trigger.txt"],
                       cwd=projection, env=environment, check=True, capture_output=True)
        environment["GIT_TRACE2_EVENT"] = str(trace)
        # Select the GC path this control measures. Git can otherwise default
        # to geometric maintenance, which has a separate auto threshold.
        result = subprocess.run(["/usr/bin/git", *gate.FORCE_KNOBS, "-c", "maintenance.strategy=gc",
                                 "commit", "-q", "-m", "gate trigger"],
                                cwd=projection, env=environment, check=True, capture_output=True)
        return_offset = trace.stat().st_size
        warning = (b"git: warning: confstr() failed with code 5: couldn't get path of "
                   b"DARWIN_USER_TEMP_DIR; using \x2ftmp instead\n")
        if result.stderr.replace(warning, b""):
            raise AssertionError(result.stderr)
        readiness = Readiness('native detached maintenance trace; scratch preserved: ' + str(base))
        last_error = "no native trace"
        while True:
            readiness.remaining()
            raw, rows = _read_trace(trace)
            try:
                witness = detached_witness(rows)
            except ValueError as error:
                last_error = str(error)
                readiness.description = last_error + "; scratch preserved: " + str(base)
            else:
                if _finished(rows):
                    repositories = [row.get("worktree") for row in rows
                                    if row.get("event") == "def_repo"
                                    and row.get("sid") == witness["commit_sid"]]
                    if repositories != [str(projection)]:
                        raise ValueError("native trigger repository does not match its owned fixture; "
                                         "scratch preserved: " + str(base))
                    # Retain native rows in the test result before owned cleanup.
                    witness.update({"trace_rows": rows, "trace_bytes": len(raw),
                                    "return_trace_bytes": return_offset,
                                    "native_stderr": result.stderr.decode()})
                    offset = 0
                    later = []
                    for line in raw.splitlines(keepends=True):
                        if offset >= return_offset and line.endswith(b"\n"):
                            frame = json.loads(line)
                            if frame.get("sid") == witness["fork_sid"] or frame.get("sid", "").startswith(
                                    witness["fork_sid"] + "/"):
                                later.append(frame)
                        offset += len(line)
                    witness["post_python_return_frame_count"] = len(later)
                    completed_cleanup = True
                    return witness
            time.sleep(0.01)
    finally:
        if completed_cleanup:
            scratch.cleanup()
            witness["scratch_removed"] = not base.exists()
        else:
            # No process signaling or speculative deletion of a live child's tree.
            scratch._finalizer.detach()

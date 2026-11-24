"""Owned scratch-only native entries; CPU and diagnostic processes are separate."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import io
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
SESSION = "bus-scale-native-census"
_VERIFY_POLICY = None
_TEARDOWN_GUARD = None


def verify_policy():
    global _VERIFY_POLICY
    if _VERIFY_POLICY is None:
        path = ROOT / "tests/fixtures/bus_scale_census/bus-scale-verify-policy-2026-10-15.py"
        specification = importlib.util.spec_from_file_location("native_verify_census_policy", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        _VERIFY_POLICY = module
    return _VERIFY_POLICY


def verify_subprocess(argv, kwargs, fixture, workspace, context):
    return verify_policy().verify_subprocess(argv, kwargs, fixture, workspace, context)


def verify_observer_scope(context):
    return verify_policy().verify_observer_scope(context)


def routine_module(case):
    """Load only the repository-owned closed expanded workload adapters."""
    for name in ("reads", "writes", "lifecycle", "teardown"):
        path = ROOT / "tests/fixtures/bus_scale_census" / ("bus-scale-routine-" + name + "-2026-10-15.py")
        if not path.is_file():
            continue
        specification = importlib.util.spec_from_file_location("census_routine_" + name, path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        if case in module.CASES:
            return module
    raise ValueError("unknown census case or unresolved native workload: " + case)


def case_context(fixture):
    path = Path(fixture).parent / "case-context.json"
    if path.resolve(strict=True) != path or not path.is_file() or path.stat().st_uid != os.getuid():
        raise ValueError("routine case context is not the owned retained input")
    context = json.loads(path.read_text(encoding="utf-8"))
    if type(context) is not dict:
        raise ValueError("routine case context must be an exact JSON object")
    return context


def workspace_send_sha(workspace):
    """Read the prepared owned repository's fixed remote-ref input.

    The measured native send still proves commit existence and remote-ref
    containment itself. This does not substitute a test verdict for that proof.
    """
    workspace = Path(workspace)
    reference = workspace / ".git/refs/remotes/origin/main"
    if (workspace.resolve(strict=True) != workspace or workspace.is_symlink()
            or workspace.stat().st_uid != os.getuid()
            or reference.resolve(strict=True) != reference
            or any(path.is_symlink() for path in (workspace / ".git", reference.parent, reference))):
        raise ValueError("send workspace must be an owned ordinary repository")
    sha = reference.read_text(encoding="ascii").strip()
    if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        raise ValueError("send workspace remote ref is not an exact commit SHA")
    return sha


def arguments(case, fixture, workspace, context=None):
    common = ["--root", str(fixture)]
    if case == "send":
        return ["send", *common, "--from", "scale-worker", "--to", "architect",
                "--repo", "floati", "--sha", workspace_send_sha(workspace), "--doc", "note.md",
                "--note", "fixed selected census send", "--idempotency-key", "census-send"]
    if case in {"wait-start", "wait-idle"}:
        return ["wait", "--for", "fresh-work", *common, "--workspace", str(workspace),
                "--session-id", SESSION]
    if case == "status":
        return ["status", *common, "--json"]
    if case == "doctor":
        return ["doctor", *common, "--source", str(ROOT), "--ref", "HEAD", "--no-sandbox", "--json"]
    if case == "log-history":
        return ["log", *common]
    if case == "receipts-history":
        return ["receipts", "architect", *common]
    if case in {"readiness-read", "receipt-append"}:
        return None
    helper = routine_module(case)
    return helper.arguments(case, fixture, workspace,
                            case_context(fixture) if context is None else context)


def source_only_git(argv, kwargs):
    """Doctor's closed source-identity reads do not read the bus ledger.

    The producer fixes PATH and strips Git redirectors before Python startup.
    Unknown subprocesses stay unsupported; no arbitrary command is admitted.
    """
    from floati.git_process import fixed_git_command, fixed_git_environment
    if (not isinstance(argv, (tuple, list)) or not all(isinstance(a, str) for a in argv)
            or set(kwargs) - {"cwd", "env", "stdin", "stdout", "stderr", "text",
                              "encoding", "errors", "universal_newlines", "close_fds"}
            or kwargs.get("close_fds", True) is not True):
        return False
    environment = kwargs.get("env")
    shallow = fixed_git_command("/usr/bin/git", ROOT, ("rev-parse", "--is-shallow-repository"))
    if tuple(argv) == tuple(shallow):
        return (environment == fixed_git_environment("/usr/bin/git")
                and kwargs.get("cwd", ROOT) in (None, ROOT))
    for executable in ("git", "/usr/bin/git"):
        for query in (("rev-parse", "--show-toplevel"), ("ls-files", "-z")):
            if tuple(argv) == tuple(fixed_git_command(executable, ROOT, query)):
                return (environment == fixed_git_environment(executable)
                        and kwargs.get("cwd", ROOT) in (None, ROOT))
    fixed = {
        ("git", "-c", "gc.auto=0", "-c", "gc.autoDetach=false",
         "-c", "maintenance.auto=false", "status", "--porcelain=v1", "--untracked-files=all"),
        ("git", "-c", "gc.auto=0", "-c", "gc.autoDetach=false",
         "-c", "maintenance.auto=false", "rev-parse", "--verify", "HEAD^{commit}"),
    }
    if tuple(argv) not in fixed or kwargs.get("cwd") != ROOT or not isinstance(environment, dict):
        return False
    home = Path(environment.get("HOME", "/"))
    temporary = Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp")
    return (home.is_absolute() and temporary in home.parents and home.is_dir()
            and home.resolve() == home and home.stat().st_uid == os.getuid()
            and home.stat().st_mode & 0o077 == 0
            and environment.get("PATH") == "/usr/bin:/bin"
            and environment.get("GIT_ATTR_NOSYSTEM") == "1"
            and not any(key.startswith("GIT_") and key != "GIT_ATTR_NOSYSTEM"
                        or key.startswith(("DYLD_", "LD_"))
                        or key.startswith("PYTHON") and (key, environment[key]) not in {
                            ("PYTHONNOUSERSITE", "1"), ("PYTHONDONTWRITEBYTECODE", "1")}
                        for key in environment))


def node_prompt_source_git(case, argv, kwargs):
    """The native prompt writer's fixed source HEAD read, outside bus I/O."""
    import subprocess
    from floati.git_process import GIT_MAINTENANCE_OPTIONS
    from floati.lifecycle_prompts import REPOSITORY_ROOT
    return (case == "node-prompts" and REPOSITORY_ROOT == ROOT
            and isinstance(argv, (tuple, list))
            and list(argv) == ["/usr/bin/git", *GIT_MAINTENANCE_OPTIONS, "rev-parse", "HEAD"]
            and kwargs == {"cwd": str(ROOT),
                           "env": {key: value for key, value in os.environ.items()
                                   if not key.startswith("GIT_")},
                           "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True})


def scratch_git_probe(argv, kwargs, workspace):
    """Only the four native send proofs in the owned Git fixture workspace."""
    from floati.git_process import fixed_git_command
    if set(kwargs) - {"env", "cwd", "stdin", "stdout", "stderr", "text", "encoding", "errors"}:
        return False
    temporary = Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp")
    if (not isinstance(workspace, Path) or workspace.name != "workspace"
            or temporary not in workspace.parents or workspace.resolve(strict=True) != workspace
            or not workspace.is_dir() or workspace.stat().st_uid != os.getuid()
            or workspace.stat().st_mode & 0o077 or not (workspace / ".git").is_dir()
            or not workspace.parent.name.startswith("bus-scale-census-owned-")):
        return False
    try:
        sha = workspace_send_sha(workspace)
    except (OSError, ValueError, UnicodeError):
        return False
    queries = (("rev-parse", "--show-toplevel"), ("cat-file", "-e", sha + "^{commit}"),
               ("for-each-ref", "--format=%(refname)", "refs/remotes"),
               ("for-each-ref", "--format=%(refname)", "--contains", sha, "refs/remotes"))
    commands = {tuple(fixed_git_command("/usr/bin/git", workspace, query)) for query in queries}
    if tuple(argv) not in commands or kwargs.get("cwd") not in (None, workspace):
        return False
    environment = kwargs.get("env", os.environ)
    home = Path(environment.get("HOME", "/"))
    return (home.is_absolute() and temporary in home.parents and home.is_dir()
            and home.resolve() == home and home.stat().st_uid == os.getuid()
            and home.stat().st_mode & 0o077 == 0
            and environment.get("PATH") == "/usr/bin:/bin"
            and not any(key.startswith(("GIT_", "LD_", "DYLD_"))
                        or key.startswith("PYTHON") and (key, environment[key]) not in {
                            ("PYTHONNOUSERSITE", "1"), ("PYTHONDONTWRITEBYTECODE", "1")}
                        for key in environment))


def routine_subprocess(case, argv, kwargs, fixture, workspace, context):
    """Exact native commands over the retained private case coordinates.

    These Git/lsof processes operate outside the bus tree; their parent native
    command's bus I/O remains covered by IOProbe. Unknown vectors are refused.
    """
    from floati.git_process import fixed_git_command, fixed_git_environment
    import stat
    if case == "seat-teardown":
        return teardown_subprocess(argv, kwargs, fixture, workspace, context)
    if (case not in {"overlap-report", "lane-open", "lane-close", "node-prep-clear", "doctor"}
            or type(context) is not dict or case != "doctor" and context.get("case") != case
            or not isinstance(argv, (list, tuple)) or not all(type(x) is str for x in argv)
            or set(kwargs) - {"env", "cwd", "stdin", "stdout", "stderr", "text", "encoding", "errors"}
            or kwargs.get("cwd") not in (None, workspace)):
        return False
    try:
        temporary = Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp")
        bank = Path(context["bank"]) if case != "doctor" else workspace.parent
        if (fixture == workspace or fixture in workspace.parents or workspace in fixture.parents
                or bank == fixture or fixture in bank.parents):
            return False
        for path in (fixture, workspace, bank):
            metadata = path.lstat()
            if (path.resolve(strict=True) != path or temporary not in path.parents
                    or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077):
                return False
        if workspace_send_sha(workspace) is None:
            return False
        if case == "doctor":
            import subprocess
            return (list(argv) == ["/bin/ps", "-eo", "pid=,ppid=,command="]
                    and kwargs == {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True})
        commands = set()
        def admit(repository, *query):
            commands.add(tuple(fixed_git_command("/usr/bin/git", repository, query)))
        if case == "node-prep-clear":
            for query in (("rev-parse", "--show-toplevel"),
                          ("status", "--porcelain", "--untracked-files=normal"),
                          ("rev-list", "HEAD", "--not", "--remotes"), ("rev-list", "HEAD")):
                admit(workspace, *query)
        elif case == "overlap-report":
            refs = [context[key] for key in ("base_ref", "left_ref", "right_ref")]
            if any(type(value) is not str or re.fullmatch(r"[0-9a-f]{40}", value) is None for value in refs):
                return False
            base, left, right = refs
            admit(workspace, "rev-parse", "--show-toplevel")
            for ref in refs:
                admit(workspace, "rev-parse", "--verify", ref + "^{commit}")
            for ref in (left, right):
                admit(workspace, "diff", "--name-only", "-z", "--no-renames", base, ref, "--")
                admit(workspace, "diff", "--unified=0", "--no-renames", base, ref, "--", "overlap.py")
                admit(workspace, "show", ref + ":overlap.py")
        else:
            from floati.lane_workspaces import FENCE_KEYS
            from floati.root import validate_identifier
            actor = validate_identifier(context["actor"], "node")
            lane = Path(context["bank"]) / "lanes" / actor / "work" / "census-lifecycle"
            branch = "codex/lane/" + actor + "/census-lifecycle"
            for repository in (workspace, lane):
                admit(repository, "rev-parse", "--show-toplevel")
                admit(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
                admit(repository, "rev-parse", "--absolute-git-dir")
                admit(repository, "rev-parse", "--is-shallow-repository")
            admit(workspace, "rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir")
            admit(workspace, "worktree", "list", "--porcelain", "-z")
            if case == "lane-open":
                admit(workspace, "show-ref", "--verify", "--quiet", "refs/heads/" + branch)
                admit(workspace, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
                for key in FENCE_KEYS:
                    admit(workspace, "config", "--get", key)
                    admit(lane, "config", "--worktree", key, "")
                admit(workspace, "config", "--bool", "--get", "extensions.worktreeConfig")
                admit(workspace, "worktree", "add", "-b", branch, "--", str(lane), workspace_send_sha(workspace))
            else:
                admit(lane, "rev-parse", "--show-toplevel", "--path-format=absolute",
                      "--git-common-dir", "--absolute-git-dir")
                admit(lane, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching")
                admit(lane, "branch", "-r", "--contains", "HEAD")
                admit(workspace, "worktree", "remove", "--", str(lane))
                lsof = next((str(Path(name).resolve(strict=True)) for name in
                             ("/usr/sbin/lsof", "/usr/bin/lsof") if Path(name).is_file()), None)
                if tuple(argv) == (lsof, "-nP", "-F", "pn", "+D", str(lane)):
                    return kwargs.get("env") == {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
        return tuple(argv) in commands and kwargs.get("env") == fixed_git_environment("/usr/bin/git")
    except (KeyError, OSError, TypeError, ValueError):
        return False


def teardown_subprocess(argv, kwargs, fixture, workspace, context):
    """Only native teardown's exact Git/UID-census calls on its bound primary.

    Git and process-census child bytes are outside the existing parent-Python
    bus-byte observation. Their CPU remains in the collected whole child tree.
    No Git lock is reported as a bus-ledger flock.
    """
    import subprocess
    import configparser
    from floati.errors import IntegrityFailure, ProtocolRefusal
    from floati.git_process import fixed_git_command, fixed_git_environment
    try:
        helper = routine_module("seat-teardown")
        key = (str(fixture), str(workspace), json.dumps(context, sort_keys=True))
        if _TEARDOWN_GUARD is not None and _TEARDOWN_GUARD[0] == key:
            if teardown_input_identities(fixture, workspace, context) != _TEARDOWN_GUARD[1]:
                return False
        else:
            helper.coordinate(fixture, workspace, context)
        if (not isinstance(argv, (list, tuple)) or not all(type(cell) is str for cell in argv)
                or type(kwargs) is not dict):
            return False
        pipes = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
        fixed_env = fixed_git_environment("/usr/bin/git")
        activity_options = dict(pipes, cwd="/", env=fixed_env, start_new_session=True)
        activity_queries = (
            ("worktree", "list", "--porcelain", "-z"), ("rev-parse", "--show-toplevel"),
            ("rev-parse", "--is-shallow-repository"), ("rev-parse", "--verify", "HEAD"),
            ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"),
            ("rev-list", "HEAD", "--not", "--remotes"),
        )
        commands = {tuple(fixed_git_command("/usr/bin/git", workspace, query)) for query in activity_queries}
        if kwargs == activity_options:
            if tuple(argv) in commands:
                return True
            if sys.platform == "darwin":
                lsof = next((name for name in ("/usr/sbin/lsof", "/usr/bin/lsof") if Path(name).is_file()), None)
                return list(argv) == ["/bin/ps", "-A", "-o", "pid=,ppid=,uid=,state="] or (
                    lsof is not None and list(argv) == [lsof, "-n", "-P", "-a", "-u", str(os.getuid()), "-d", "cwd", "-F0pfn"])
            return False
        sha = context["sha"]
        prepared_queries = (
            ("rev-parse", "--show-toplevel"), ("status", "--porcelain", "--untracked-files=normal"),
            ("rev-list", "HEAD", "--not", "--remotes"), ("rev-list", "HEAD"),
            ("cat-file", "-t", sha + ":note.md"), ("ls-tree", sha, "--", "note.md"),
        )
        if kwargs == dict(pipes, env=fixed_env, text=True):
            return tuple(argv) in {tuple(fixed_git_command("/usr/bin/git", workspace, query)) for query in prepared_queries}
        # Existing native banked-SHA proofs inherit the entry's fixed private
        # environment; accept only those four vectors, without extra options.
        queries = (("rev-parse", "--show-toplevel"), ("cat-file", "-e", sha + "^{commit}"),
            ("for-each-ref", "--format=%(refname)", "refs/remotes"),
            ("for-each-ref", "--format=%(refname)", "--contains", sha, "refs/remotes"))
        inherited = {"HOME": os.environ.get("HOME"), "PATH": "/usr/bin:/bin",
                     "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        environment_ok = all(os.environ.get(k) == v for k, v in inherited.items())
        environment_ok = environment_ok and not any(key.startswith(("GIT_", "LD_", "DYLD_"))
            or key.startswith("PYTHON") and key not in {"PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"} for key in os.environ)
        helper.private(Path(inherited["HOME"]))
        return (environment_ok and kwargs == dict(pipes, text=True)
                and tuple(argv) in {tuple(fixed_git_command("/usr/bin/git", workspace, query)) for query in queries})
    except (KeyError, OSError, TypeError, ValueError, IntegrityFailure, ProtocolRefusal, configparser.Error):
        return False


def teardown_input_identities(fixture, workspace, context):
    """Stat-only binding of the immutable coordinate validated before IOProbe.

    Replaying the full registry/authority proof for every diagnostic subprocess
    would manufacture observer-only bus reads. Files retain exact identity and
    metadata; ancestors retain inode/mode/owner identity while native commands
    may legitimately add other files/directories beneath the bus root.
    """
    import stat
    fixture, workspace = Path(fixture), Path(workspace)
    origin = Path(context.get("origin", workspace.parent.parent / "origin.git"))
    files = [fixture / relative for relative in ("registry/entries.jsonl", "registry/governance.json",
        "codex-wait/workspaces.v0.json", "receipts/codex-wait-consent/scale-worker.jsonl",
        "receipts/codex-wait-session/scale-worker.jsonl")]
    files += [workspace / "SEAT.json", origin / "config"]
    files += [workspace / ".git" / relative for relative in
              ("config", "HEAD", "refs/heads/main", "refs/remotes/origin/main")]
    temporary = Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp")
    result = {}
    for path in files:
        for item in (path, *path.parents):
            if item == temporary:
                break
            metadata = item.lstat()
            expected_file = item == path
            if (metadata.st_uid != os.getuid() or stat.S_ISLNK(metadata.st_mode)
                    or (not stat.S_ISREG(metadata.st_mode) if expected_file else not stat.S_ISDIR(metadata.st_mode))):
                raise ValueError("teardown coordinate identity is no longer ordinary and owned")
            identity = (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid, metadata.st_gid)
            if expected_file:
                identity += (metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
            result[str(item)] = identity
    return result


@contextmanager
def prepared_stdin(case, context):
    """Supply the retained native Cursor stop event for its exact workload."""
    if case != "wake-wait":
        yield
        return
    payload = context.get("stdin")
    if (type(payload) is not dict or set(payload) != {"status", "loop_count", "conversation_id"}
            or payload["status"] != "completed" or type(payload["loop_count"]) is not int
            or payload["loop_count"] != 0 or payload["conversation_id"] != context.get("session")):
        raise ValueError("native Cursor workload requires its exact retained stop input")
    with patch.object(sys, "stdin", io.StringIO(json.dumps(payload, sort_keys=True) + "\n")):
        yield


def hold_summary(probe, *, begin=None, end=None):
    families = {name: [] for name in ("ledger", "epoch", "coordination", "lifetime", "other")}
    for row in probe.holds:
        path = row["path"]
        parts = Path(path).parts
        family = ("ledger" if path.endswith(".jsonl.lock") else
                  "epoch" if path == "." else
                  "coordination" if len(parts) == 4 and parts[:2] == ("receipts", "wake-coordination")
                  and parts[-1] == "lane.lock" else
                  "lifetime" if path == "sequencer/owner.lock" else "other")
        copy = dict(row)
        if begin is not None:
            # Clip actual acquired/released intervals to the observed idle phase.
            overlap = min(row["release_call_after_ns"], end) - max(row["acquire_call_after_ns"], begin)
            if overlap <= 0:
                continue
            copy["seconds"] = overlap / 1e9
        families[family].append(copy)
    return {name: {"count": len(rows), "maximum_seconds": max(
        (row["seconds"] for row in rows), default=0), "intervals": rows}
            for name, rows in families.items()}


def observe_idle(native_changed, watcher, changes, idle, probe, *, reconciliation=None, authorized=False,
                 deadline_ns=None, poll_interval_seconds=1.0, previous_current_time_ns=None, startup=None):
    value = native_changed(watcher)
    changes.append(list(value))
    if "end_ns" in idle:
        return value
    settled = (authorized and isinstance(reconciliation, dict)
        and reconciliation.get("state") == "caught_up"
        and reconciliation.get("wake_required") is False
        and reconciliation.get("fresh_messages") == []
        and reconciliation.get("held_total", 0) == 0)
    if list(value) != [False, False] or not settled:
        idle.clear()
    elif "begin_ns" not in idle:
        begin = time.monotonic_ns()
        if deadline_ns is None or begin + int(poll_interval_seconds * 1e9) > deadline_ns:
            return value
        idle.update(begin_ns=begin, cpu_begin_ns=time.process_time_ns(),
                    reads_begin=dict(probe.read_bytes_by_path) if probe else {})
        if startup is not None and not startup:
            child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)
            if probe and any(row.get("returncode") is None for row in probe.external_processes):
                raise RuntimeError("startup has an uncollected external child")
            startup.update(endpoint_ns=begin, self_cpu_ns=idle["cpu_begin_ns"],
                collected_child_cpu_seconds=child_usage.ru_utime + child_usage.ru_stime,
                reads=dict(idle["reads_begin"]), authorized_reconciliation=True,
                endpoint_sample_after_ns=time.monotonic_ns(),
                boundary="process birth through first settled clean poll after completed authorized caught_up reconciliation")
    else:
        # At the next native changed() call, current_time still holds the
        # prior iteration's value used by sleep(min(poll, deadline-current)).
        if (previous_current_time_ns is None or deadline_ns is None
                or previous_current_time_ns + int(poll_interval_seconds * 1e9) > deadline_ns):
            idle.clear()
            return value
        idle.update(end_ns=time.monotonic_ns(), cpu_end_ns=time.process_time_ns(),
                    reads_end=dict(probe.read_bytes_by_path) if probe else {},
                    native_sleep_seconds=poll_interval_seconds)
    return value


def main():
    if len(sys.argv) != 7 or sys.argv[1] not in {"cpu", "diagnostic"}:
        raise ValueError("expected cpu|diagnostic CASE FIXTURE WORKSPACE RESULT MARKER")
    mode, case, root_arg, workspace_arg, result_arg, marker = sys.argv[1:]
    fixture, workspace, result_path = map(Path, (root_arg, workspace_arg, result_arg))
    temporary = Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp")
    if (marker != "owned-census-v1" or fixture.name != "scale-fleet"
            or temporary not in fixture.parents or fixture.resolve(strict=True) != fixture
            or not any(p.name.startswith("bus-scale-census-owned-") for p in fixture.parents)
            or result_path.parent != fixture.parent
            or workspace.resolve(strict=True) != workspace or not workspace.is_dir()):
        raise ValueError("native census entry requires its canonical owned scratch fixture")
    if case != "seat-teardown" and (workspace.name != "workspace" or not workspace.parent.name.startswith("bus-scale-census-owned-")):
        raise ValueError("native census entry requires its canonical owned scratch fixture")
    from floati import cli
    from floati.root import FloatiRoot
    context = case_context(fixture) if case not in {"send", "status", "doctor", "log-history",
        "receipts-history", "readiness-read", "receipt-append", "wait-start", "wait-idle"} else {}
    argv = arguments(case, fixture, workspace, context)
    if case == "seat-teardown":
        # arguments() has just validated the full native coordinate, before
        # diagnostic probe installation; retain that same input's identity.
        global _TEARDOWN_GUARD
        _TEARDOWN_GUARD = ((str(fixture), str(workspace), json.dumps(context, sort_keys=True)),
                           teardown_input_identities(fixture, workspace, context))
    probe = None
    if mode == "diagnostic":
        specification = importlib.util.spec_from_file_location("bus_scale_io_native_diagnostic",
            ROOT / "tests/bus_scale_io.py")
        diagnostic = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(diagnostic)
        probe = diagnostic.IOProbe(fixture, external_process_policy=lambda argv, kwargs:
            source_only_git(argv, kwargs) or scratch_git_probe(argv, kwargs, workspace)
            or node_prompt_source_git(case, argv, kwargs)
            or routine_subprocess(case, argv, kwargs, fixture, workspace, context)
            or case == "verify" and verify_subprocess(argv, kwargs, fixture, workspace, context))
    changes, idle, startup = [], {}, {}
    if case == "wait-idle":
        from floati import codex_wait
        real_changed = codex_wait._WaiterInputsWatch.changed

    def observe(watcher):
        # Read the real waiter's completed native result; do not replace or
        # invoke its evaluator. The first hint has idle_artifact=None.
        frame = sys._getframe(1)
        if frame.f_code is not codex_wait._run_stop_waiter.__code__:
            raise RuntimeError("idle observation is outside the native waiter")
        native = frame.f_locals
        classification = native.get("classification")
        return observe_idle(real_changed, watcher, changes, idle, probe,
            reconciliation=native.get("idle_artifact"),
            authorized=getattr(classification, "state", None) == "authorized",
            deadline_ns=int(native["deadline"] * 1e9),
            poll_interval_seconds=native["poll_interval_seconds"],
            previous_current_time_ns=int(native["current_time"] * 1e9) if "current_time" in native else None,
            startup=startup)

    # Only the separate idle seam uses a passive delegated watcher. Whole CLI
    # CPU entries for wait-start and every other command install no observer.
    watcher = patch.object(codex_wait._WaiterInputsWatch, "changed", observe) if case == "wait-idle" else nullcontext()
    with (probe if probe else nullcontext()), watcher, prepared_stdin(case, context):
        if argv is not None:
            code = cli.main(argv)
        elif case == "readiness-read":
            from floati.mail_health import RecipientReadiness
            result = RecipientReadiness.observe(FloatiRoot.open_direct_home(fixture),
                "scale-worker", observed_at="2026-10-09T00:00:00.000Z").artifact()
            print(json.dumps(result, sort_keys=True))
            code = 0
        elif case == "receipt-append":
            from floati.codex_wait_contract import CodexWaitReceiptLedger
            result = CodexWaitReceiptLedger(FloatiRoot.open_direct_home(fixture)).record_exhaustion(
                node_id="scale-worker", session_digest=hashlib.sha256(SESSION.encode()).hexdigest(),
                waited_seconds=2, idempotency_key="census-receipt")
            print(json.dumps(result, sort_keys=True))
            code = 0
        else:
            code, result = routine_module(case).execute_seam(
                case, fixture, workspace, case_context(fixture))
            print(json.dumps(result, sort_keys=True))
    record = {"mode": mode, "case": case, "native_exit_code": code,
              "argv": argv, "wait_changes": changes, "idle": idle, "startup": startup}
    if case == "verify":
        record["observer_scope"] = verify_observer_scope(context)
    if probe:
        probe.assert_complete()
        holds = hold_summary(probe)
        record.update(read_bytes_by_path=dict(probe.read_bytes_by_path),
                      hold_families=holds, external_processes=probe.external_processes,
                      coverage={"bytes": True, "locks": True})
        if case == "wait-idle":
            if "end_ns" not in idle or idle["end_ns"] <= idle["begin_ns"]:
                raise RuntimeError("two consecutive authoritative unchanged ticks were not observed")
            idle["bytes_read"] = sum(idle["reads_end"].values()) - sum(idle["reads_begin"].values())
            idle["ledger_hold_seconds"] = hold_summary(
                probe, begin=idle["begin_ns"], end=idle["end_ns"])["ledger"]["maximum_seconds"]
            endpoint = startup["endpoint_ns"]
            ledger_intervals = holds["ledger"]["intervals"]
            crossing = [row for row in ledger_intervals
                        if row["acquire_call_after_ns"] < endpoint < row["release_call_after_ns"]]
            if crossing:
                raise RuntimeError("startup ledger hold crosses the observed endpoint")
            complete = [row for row in ledger_intervals if row["release_call_after_ns"] <= endpoint]
            startup.update(bytes_read=sum(startup["reads"].values()),
                ledger_hold_seconds=max((row["seconds"] for row in complete), default=0),
                ledger_intervals=complete, crossing_ledger_intervals=crossing,
                fixture_bytes_boundary="probe installation through endpoint; source/interpreter imports excluded")
    if case == "wait-idle" and "end_ns" not in idle:
        raise RuntimeError("native idle phase unavailable")
    with result_path.open("x") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

"""Fixed Floati project-profile qualification; no runtime/profile overrides.

Fresh dependency probes prove routing, not suite-worker observations. The
native parallel runner owns its workers and descendants. This controller owns
only its directly launched processes and requires their native cleanup proof.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time


# darwin is the only qualified platform. A private replacement of
# PROFILE_REGISTRY is readable on every platform. The installed coordinate is
# not opened when this platform has no entry.
_QUALIFIED_PLATFORM_REGISTRIES = {
    "darwin": Path("\x2fUsers/penguinspecz/.codex/project-job-profiles.json"),
}
PROFILE_REGISTRY = _QUALIFIED_PLATFORM_REGISTRIES["darwin"]
PROFILE_NAME = "floati-harbor"
SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
PARALLEL_LIMIT_SECONDS = 600.0
controller_clock = time.monotonic
supervisor_clock = time.monotonic
_TOOLS = {"python_import_root", "minisign_executable", "node_executable"}
_PROFILE_FIELDS = {"git_common_dirs", "interpreter", "jobs", "tools"}
_SCRIPT_BOOTSTRAP = (
    "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
    "script=sys.argv.pop(1);sys.argv[0]=script;"
    "runpy.run_path(script,run_name='__main__')"
)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate registry/receipt key: " + key)
        result[key] = value
    return result


def platform_temp_root():
    """Stable Unix ancestry across private HOME/TMPDIR startup fences."""
    return Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp").resolve(strict=True)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _ordinary(value, *, directory=False, executable=False):
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(str(path)) != str(path):
        raise ValueError("profile coordinate must be absolute and normalized")
    for component in (*reversed(path.parents), path):
        metadata = component.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("profile coordinate has a symlink component")
    metadata = path.lstat()
    if path.resolve(strict=True) != path or not (
            stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)):
        raise ValueError("profile coordinate is not canonical and ordinary")
    if executable and not os.access(path, os.X_OK):
        raise ValueError("profile executable is not executable")
    return path


def _registry_path():
    """Return the installed registry, or a caller-bound private replacement.

    An unqualified platform refuses before any profile coordinate is opened.
    """
    if PROFILE_REGISTRY not in _QUALIFIED_PLATFORM_REGISTRIES.values():
        return PROFILE_REGISTRY
    selected = _QUALIFIED_PLATFORM_REGISTRIES.get(sys.platform)
    if selected is None:
        raise ValueError("unsupported_platform")
    return selected


def _read_registry():
    path = _ordinary(_registry_path())
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(descriptor)
        if (before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_nlink != 1 or before.st_size > 262144):
            raise ValueError("fixed registry must be owned, unshared, mode0600 and bounded")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(262145)
        if (len(raw) != before.st_size or _signature(before) != _signature(os.fstat(descriptor))
                or _signature(before) != _signature(path.lstat())):
            raise ValueError("fixed registry changed during its read")
        document = json.loads(raw, object_pairs_hook=_unique)
        if (type(document) is not dict or set(document) != {"schema_version", "profiles"}
                or type(document["schema_version"]) is not int or document["schema_version"] != 1
                or type(document["profiles"]) is not dict):
            raise ValueError("unsupported fixed registry shape")
        profile = document["profiles"].get(PROFILE_NAME)
        _validate_profile(profile)
        return profile, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                         "identity": list(_signature(before))}
    finally:
        os.close(descriptor)


def _validate_profile(profile):
    if type(profile) is not dict or set(profile) != _PROFILE_FIELDS:
        raise ValueError("floati-harbor has unknown or missing profile fields")
    roots = profile["git_common_dirs"]
    if (type(roots) is not list or not 1 <= len(roots) <= 16
            or any(type(root) is not str for root in roots) or len(set(roots)) != len(roots)):
        raise ValueError("profile Git common directories are not a closed bounded list")
    for root in roots:
        _ordinary(root, directory=True)
    if type(profile["interpreter"]) is not str:
        raise ValueError("fixed profile interpreter is not text")
    _ordinary(profile["interpreter"], executable=True)
    tools = profile["tools"]
    if type(tools) is not dict or set(tools) != _TOOLS:
        raise ValueError("profile has unknown or missing tool selectors")
    for name, value in tools.items():
        if type(value) is not str:
            raise ValueError("profile tool selector is not text")
        _ordinary(value, directory=name == "python_import_root",
                  executable=name != "python_import_root")
    jobs = profile["jobs"]
    if (type(jobs) is not dict or jobs.get("unittest") != {"kind": "fixed-python"}
            or any(type(name) is not str or type(job) is not dict
                   or set(job) != {"kind"} or type(job["kind"]) is not str
                   for name, job in jobs.items())):
        raise ValueError("profile does not declare the fixed native unittest job")


def read_profile():
    """Read only the installed, fixed profile; there is no registry argument.

    A platform with no qualified profile refuses with unsupported_platform
    before any profile coordinate is opened.
    """
    return _read_registry()[0]


def _git(root, *arguments):
    env = {key: os.environ[key] for key in ("HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
           if key in os.environ}
    env.update(PATH=SAFE_PATH, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
               GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    return subprocess.check_output(["/usr/bin/git", *arguments], cwd=root, env=env,
                                   stderr=subprocess.PIPE)


def validate_repository(root, profile):
    """Admit a real primary or reciprocal linked worktree by common directory."""
    _validate_profile(profile)
    root = _ordinary(root, directory=True)
    top = Path(os.fsdecode(_git(root, "rev-parse", "--show-toplevel")).strip())
    common = Path(os.fsdecode(_git(root, "rev-parse", "--git-common-dir")).strip())
    common = (root / common).resolve(strict=True) if not common.is_absolute() else common.resolve(strict=True)
    if top.resolve(strict=True) != root or str(common) not in profile["git_common_dirs"]:
        raise ValueError("repository does not match the fixed floati-harbor common directory")
    git_dir = Path(os.fsdecode(_git(root, "rev-parse", "--absolute-git-dir")).strip())
    git_dir = _ordinary(git_dir, directory=True)
    if git_dir != common:
        if git_dir.parent != common / "worktrees":
            raise ValueError("linked worktree administration is outside its common directory")
        reciprocal = _ordinary(git_dir / "gitdir").read_text().strip()
        if Path(reciprocal).resolve(strict=True) != root / ".git":
            raise ValueError("linked worktree lacks its reciprocal Git binding")
    return {"workspace_root": str(root), "git_common_dir": str(common), "git_dir": str(git_dir)}


def make_environment(profile, inherited, home, temporary):
    """Use project-job code/tools with data paths already private at startup."""
    _validate_profile(profile)
    home, temporary = _ordinary(home, directory=True), _ordinary(temporary, directory=True)
    for path in (home, temporary):
        info = path.stat()
        if (platform_temp_root() not in path.parents
                or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError("qualification HOME and temporary root must be private and owned")
    env = {key: inherited[key] for key in ("LANG", "LC_ALL", "LC_CTYPE") if key in inherited}
    env.update(PATH=SAFE_PATH, HOME=str(home), TMPDIR=str(temporary), TMP=str(temporary),
               TEMP=str(temporary), PYTHONPATH=profile["tools"]["python_import_root"],
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    for key, child in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"),
                       ("XDG_CACHE_HOME", ".cache")):
        target = home / child
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        _ordinary(target, directory=True)
        env[key] = str(target)
    env.update(FLOATI_TEST_MINISIGN_EXECUTABLE=profile["tools"]["minisign_executable"],
               FLOATI_TEST_NODE_EXECUTABLE=profile["tools"]["node_executable"])
    return env


class _Cancellation:
    def __init__(self):
        self.number = None
        self.process = None
        self.forwarded = False
        self.committed = False
        self.delivery_errors = []

    def receive(self, number, frame):
        if self.committed:
            return
        if self.number is None:
            self.number = number
        self.forward()

    def forward(self):
        if (self.process is not None and getattr(self.process, "pid", None) is not None
                and self.number is not None and not self.forwarded):
            self.forwarded = True
            try:
                self.process.send_signal(self.number)
            except OSError as exc:
                self.delivery_errors.append({"pid": self.process.pid, "signal": self.number,
                                             "error": type(exc).__name__, "errno": exc.errno})

    @contextmanager
    def handlers(self):
        prior = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, tuple(prior))
        try:
            for number in prior:
                signal.signal(number, self.receive)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)
        try:
            yield self
        finally:
            blocked = signal.pthread_sigmask(signal.SIG_BLOCK, tuple(prior))
            try:
                for number, handler in prior.items():
                    signal.signal(number, handler)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


@contextmanager
def final_commitment(owner):
    """Settle pending cancellation before protected cleanup and publication."""
    numbers = (signal.SIGINT, signal.SIGTERM)
    prior_mask = signal.pthread_sigmask(signal.SIG_BLOCK, numbers)
    try:
        for number in numbers:
            if number in signal.sigpending():
                owner.receive(number, None)
        owner.committed = True
        yield
    finally:
        try:
            for number in signal.sigpending().intersection(numbers):
                signal.sigwait((number,))
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, prior_mask)


class _Cancelled(KeyboardInterrupt):
    def __init__(self, number):
        self.number = number


def _capture(argv, *, cwd, environment, cancellation=None, log=None, record=None):
    owner = cancellation or _Cancellation()
    scope = owner.handlers() if cancellation is None else _null_scope()
    with scope:
        if owner.number is not None:
            raise _Cancelled(owner.number)
        # The handler records cancellation during Popen construction. Once
        # adopted, the retained handle receives it and is always collected.
        proc = subprocess.Popen.__new__(subprocess.Popen)
        owner.process, owner.forwarded = proc, False
        try:
            proc.__init__(argv, cwd=cwd, env=environment, stdout=log or subprocess.PIPE,
                          stderr=subprocess.STDOUT, start_new_session=True)
            if record is not None:
                record["pid"] = proc.pid
            owner.forward()
            output = proc.communicate()[0]
        finally:
            # A signal handler never raises across this ownership boundary.
            # Native phase runners own descendant cleanup before leader exit.
            collected = False
            try:
                if getattr(proc, "pid", None) is not None:
                    owner.forward()
                    proc.wait()
                    collected = True
                    if record is not None:
                        record.update(collected=True, exit_code=proc.returncode)
            finally:
                if collected or getattr(proc, "pid", None) is None:
                    owner.process = None
        if owner.number is not None:
            raise _Cancelled(owner.number)
        return proc.returncode, b"" if output is None else output


@contextmanager
def _null_scope():
    yield


_PROBE = r'''
import importlib.metadata, json, os, signal, subprocess, sys
sys.path.insert(0, sys.argv[1])
import PIL, jsonschema
ordinal = int(sys.argv[2]); environments = json.loads(sys.argv[3])
names = ('controller', 'discovery', 'worker', 'grandchild')
def version(module, distribution):
    literal = module.__dict__.get('__version__')
    if literal is not None: return literal
    try: return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError: return None
row = {'pid': os.getpid(), 'home': os.environ['HOME'],
       'imports': {'PIL': PIL.__file__, 'jsonschema': jsonschema.__file__},
       'versions': {'PIL': version(PIL, 'Pillow'), 'jsonschema': version(jsonschema, 'jsonschema')},
       'tools': {key: os.environ[key] for key in ('FLOATI_TEST_MINISIGN_EXECUTABLE', 'FLOATI_TEST_NODE_EXECUTABLE')},
       'interpreter': sys.executable, 'flags': {'ignore_environment': sys.flags.ignore_environment, 'no_user_site': sys.flags.no_user_site}}
stages = {names[ordinal]: row}
cancelled = [None]; owned = [None]
def cancel(number, frame):
    if cancelled[0] is None: cancelled[0] = number
    if owned[0] is not None and getattr(owned[0], 'pid', None) is not None:
        try: owned[0].send_signal(number)
        except OSError: pass
for number in (signal.SIGINT, signal.SIGTERM): signal.signal(number, cancel)
if ordinal < 3 and cancelled[0] is None:
    child = subprocess.Popen.__new__(subprocess.Popen)
    owned[0] = child
    try:
        child.__init__([sys.argv[4], '-E', '-s', '-B', '-c', sys.argv[5],
            sys.argv[1], str(ordinal + 1), sys.argv[3], sys.argv[4], sys.argv[5]],
            env=environments[ordinal + 1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if cancelled[0] is not None: cancel(cancelled[0], None)
        output, error = child.communicate()
    finally:
        try:
            if getattr(child, 'pid', None) is not None: child.wait()
        finally: owned[0] = None
    if child.returncode: raise RuntimeError('nested routing probe failed: ' + error.decode(errors='replace')[-4000:])
    stages.update(json.loads(output))
if cancelled[0] is not None: raise SystemExit(128 + cancelled[0])
print(json.dumps(stages, sort_keys=True))
'''


def preflight(profile, root, environment):
    """Fresh private routing probes, explicitly separate from suite workers."""
    validate_repository(root, profile)
    private_home = _ordinary(environment["HOME"], directory=True)
    if private_home.stat().st_uid != os.getuid() or stat.S_IMODE(private_home.stat().st_mode) != 0o700:
        raise ValueError("preflight needs an already private owned HOME")
    parent = _ordinary(private_home.parent, directory=True)
    with tempfile.TemporaryDirectory(prefix="qualification-probes-", dir=parent) as temporary:
        scratch = Path(temporary)
        environments = []
        for ordinal in range(4):
            home, temp = scratch / ("h" + str(ordinal)), scratch / ("t" + str(ordinal))
            home.mkdir(mode=0o700)
            temp.mkdir(mode=0o700)
            environments.append(make_environment(profile, environment, home, temp))
        argv = [profile["interpreter"], "-E", "-s", "-B", "-c", _PROBE,
                profile["tools"]["python_import_root"], "0", json.dumps(environments),
                profile["interpreter"], _PROBE]
        code, raw = _capture(argv, cwd=root, environment=environments[0])
        if code:
            raise ValueError("profile dependency routing failed: " + raw.decode(errors="replace")[-4000:])
        stages = json.loads(raw, object_pairs_hook=_unique)
        if set(stages) != {"controller", "discovery", "worker", "grandchild"}:
            raise ValueError("dependency routing omitted a fresh boundary")
        for ordinal, name in enumerate(("controller", "discovery", "worker", "grandchild")):
            row = stages[name]
            if row["home"] != environments[ordinal]["HOME"] or row["flags"] != {
                    "ignore_environment": 1, "no_user_site": 1}:
                raise ValueError("dependency routing did not start under private fixed Python")
            for package in ("PIL", "jsonschema"):
                origin = _ordinary(row["imports"][package])
                if Path(profile["tools"]["python_import_root"]) not in origin.parents:
                    raise ValueError("dependency came from outside the fixed profile import root")
            expected_tools = {key: environments[ordinal][key] for key in (
                "FLOATI_TEST_MINISIGN_EXECUTABLE", "FLOATI_TEST_NODE_EXECUTABLE")}
            if row["tools"] != expected_tools:
                raise ValueError("dependency routing changed fixed tool selectors")
            if any(row[field] != stages["controller"][field] for field in (
                    "imports", "versions", "tools", "interpreter", "flags")):
                raise ValueError("fresh routing boundaries do not have identical code/tool identity")
            row["environment"] = environments[ordinal]
        versions = {}
        for name, arguments in (("minisign_executable", ["-v"]), ("node_executable", ["--version"])):
            tool_argv = [profile["tools"][name], *arguments]
            code, output = _capture(tool_argv, cwd=root, environment=environments[0])
            if code or len(output) > 65536:
                raise ValueError("fixed profile tool version probe failed")
            versions[name] = {"argv": tool_argv, "exit_code": code,
                              "output": output.decode(errors="strict"),
                              "identity": list(_signature(Path(tool_argv[0]).stat()))}
        return {"scope": "fresh_private_import_and_tool_routing_probes_not_suite_workers",
                "successful": True, "stages": stages, "tools": versions,
                "probe_stdout_sha256": hashlib.sha256(raw).hexdigest(),
                "probe_scratch_removed_after_return": True}


def _launcher(root):
    path = _ordinary(Path(root) / "scripts/test-parallel.py")
    prior = list(sys.path)
    try:
        specification = importlib.util.spec_from_file_location("qualification_native_launcher", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = prior


def _json(path):
    return json.loads(_ordinary(path).read_bytes(), object_pairs_hook=_unique)


def _publish(path, value):
    with path.open("x") as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def validate_output(root, output):
    """Admit an absent private-temporary bank without writing any path."""
    root, output = Path(root), Path(output)
    boundary = platform_temp_root()
    if (not output.is_absolute() or os.path.normpath(str(output)) != str(output)
            or boundary not in output.parents or output == root or root in output.parents
            or os.path.lexists(output)):
        raise ValueError("qualification output must be an absent canonical private-temporary bank outside source")
    # mkdir does not create parents; require the whole existing ancestry to be
    # canonical before permitting the first destination write.
    parent = _ordinary(output.parent, directory=True)
    for ancestor in (parent, *parent.parents):
        if ancestor == boundary:
            break
        info = ancestor.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError("qualification output ancestors must be owned and not shared-writable")
    if output.resolve(strict=False) != output:
        raise ValueError("qualification output has an aliased parent")
    return output


_COST_COUNTERS = {"observe_calls", "observe_wall_ns", "observe_process_cpu_ns",
                  "identity_attempts", "children_attempts", "witness_attempts",
                  "journal_write_attempts", "journal_bytes_submitted"}
_COST_SCOPES = {
    "observe_scope": "Wall and launcher process CPU nanoseconds during observe calls only.",
    "query_scope": "Query API attempts across this tree lifecycle; not kernel syscall or process counts.",
    "journal_scope": "UTF-8 bytes submitted to journal write; not accepted or durable bytes.",
}


def _validate_cost(final_path, report, launch, source):
    path = _ordinary(final_path.parent / "process-cost.json")
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > 262144:
        raise ValueError("native process-cost sidecar is not owned, unshared and bounded")
    raw = path.read_bytes()
    if len(raw) != info.st_size or _signature(path.stat()) != _signature(info):
        raise ValueError("native process-cost sidecar changed during read")
    row = json.loads(raw, object_pairs_hook=_unique)
    fields = {"schema_version", "kind", "launcher_pid", "source", "interpreter", "final_path",
              "request_sha256", "native_status", "availability", "cost"}
    request = _ordinary(final_path.parent / "request.json").read_bytes()
    if (type(row) is not dict or set(row) != fields
            or type(row["schema_version"]) is not int or row["schema_version"] != 1
            or row["kind"] != "parallel_process_cost" or row["availability"] != "available"
            or row["native_status"] != "complete" or row["source"] != source
            or row["interpreter"] != report["interpreter"] or row["final_path"] != str(final_path)
            or row["request_sha256"] != hashlib.sha256(request).hexdigest()
            or type(row["launcher_pid"]) is not int or row["launcher_pid"] <= 0
            or row["launcher_pid"] != launch["pid"]):
        raise ValueError("native process-cost sidecar schema or phase binding is invalid")
    cost = row["cost"]
    if (type(cost) is not dict or set(cost) != _COST_COUNTERS | set(_COST_SCOPES) | {"schema_version"}
            or type(cost["schema_version"]) is not int or cost["schema_version"] != 1
            or any(type(cost[key]) is not int or cost[key] < 0 for key in _COST_COUNTERS)
            or any(cost[key] != value for key, value in _COST_SCOPES.items())):
        raise ValueError("native process-cost counters or declared scope is invalid")
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "artifact": row}


def _diagnostic_costs(value):
    """Pin ordinary advisory bytes without admitting their population as truth."""
    path = _ordinary(value)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) & 0o022 or info.st_size > 16 * 1024 * 1024):
            raise ValueError("diagnostic costs must be owned, unshared, bounded ordinary data")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if (len(raw) != info.st_size or _signature(info) != _signature(os.fstat(descriptor))
                or _signature(info) != _signature(path.lstat())):
            raise ValueError("diagnostic costs changed during read")
        return raw, {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                     "bytes": len(raw), "identity": list(_signature(info)),
                     "scope": "advisory historical costs; never membership or qualification"}
    finally:
        os.close(descriptor)


def _admit_diagnostic(launcher, path, report, launch, source, costs, cost_bytes, interpreter):
    """Reconcile actual native artifacts, including complete unsuccessful runs."""
    if (report.get("mode") != "run" or report.get("status") != "complete"
            or report.get("qualified") is not False or report.get("workers") != 8
            or type(report.get("workers")) is not int or report.get("source") != source
            or report.get("final_path") != str(path)
            or report.get("interpreter", {}).get("executable") != str(Path(interpreter).resolve())
            or type(report.get("successful")) is not bool):
        raise ValueError("diagnostic native status, source, interpreter or worker binding is invalid")
    census, plan = report["census"], report["plan"]
    if (_json(path.parent / "request.json") != {
            "mode": "run", "source": source, "interpreter": report["interpreter"]}
            or _json(path.parent / "census.json") != census
            or _json(path.parent / "plan.json") != plan
            or census.get("source") != source or census.get("interpreter") != report["interpreter"]):
        raise ValueError("diagnostic persisted request, census or plan mismatch")
    weights, audit = launcher.profile_weights(cost_bytes, census)
    if (report.get("profile") != {"path": costs["path"], **audit}
            or launcher.make_plan(census, 8, weights=weights) != plan):
        raise ValueError("diagnostic advisory costs or native assignments disagree")
    launcher.validate_plan(census, plan)
    for result in report["results"]:
        if (result.get("source") != source or result.get("interpreter") != report["interpreter"]
                or launcher.read_worker_result(path.parent, result["key"]) != result):
            raise ValueError("diagnostic native worker journal or identity mismatch")
    aggregate = launcher.reconcile(census, plan, report["results"])
    if aggregate != report["aggregate"] or aggregate["successful"] is not report["successful"]:
        raise ValueError("diagnostic native aggregate mismatch")
    expected = {"discovery": 0, **{row["key"]: 0 if row["successful"] else 1
                                  for row in report["results"]}}
    children = report.get("children")
    if (type(children) is not list or len(children) != len(expected)
            or any(type(row) is not dict or type(row.get("pid")) is not int or row["pid"] <= 0
                   or type(row.get("returncode")) is not int for row in children)
            or {row.get("key"): row["returncode"] for row in children} != expected
            or launch["exit_code"] != (0 if report["successful"] else 1)):
        raise ValueError("diagnostic native child collection or return codes disagree")
    cleanup = report.get("cleanup", {})
    ownership = cleanup.get("ownership", {})
    if (cleanup.get("successful") is not True or cleanup.get("errors") != []
            or cleanup.get("scratch_retained") is not False
            or type(ownership) is not dict
            or ownership.get("scope") != "direct children and observed descendants"
            or ownership.get("exhaustive") is not False
            or any(type(ownership.get(field)) is not list
                   for field in ("observed", "departed", "observations"))):
        raise ValueError("diagnostic native cleanup is unproved")
    launcher.validate_ownership_receipt(ownership, children, sys.platform)
    launch["process_cost"] = _validate_cost(path, report, launch, source)


def run_diagnostic(root, output, costs):
    """Run one fixed-profile diagnostic; never qualify, recover or compare a pair."""
    profile, registry = _read_registry()
    identity = validate_repository(root, profile)
    root = Path(identity["workspace_root"])
    if _git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("diagnostic requires clean committed source")
    launcher = _launcher(root)
    source = launcher.source_identity(root)
    cost_bytes, cost_identity = _diagnostic_costs(costs)
    output = validate_output(root, output)
    output.mkdir(mode=0o700)
    receipt = {"status": "invalid", "successful": False, "qualified": False,
               "profile": "floati-harbor", "registry": registry, "repository": identity,
               "source": source, "costs": cost_identity, "launches": [],
               "scope": "parallel diagnostic only; no serial reference or qualification",
               "cleanup": {"successful": False}}
    scratch = None
    scratch_identity = None
    native_admitted = False
    owner = _Cancellation()
    with owner.handlers():
        try:
            platform_temp = platform_temp_root()
            scratch = Path(tempfile.mkdtemp(prefix="qualification-diagnostic-", dir=platform_temp)).resolve()
            info = scratch.lstat()
            scratch_identity = (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode))
            receipt["scratch_root"] = str(scratch)
            probe_home, probe_temp = scratch / "probe-home", scratch / "probe-temp"
            probe_home.mkdir(mode=0o700)
            probe_temp.mkdir(mode=0o700)
            receipt["preflight"] = preflight(profile, root,
                make_environment(profile, os.environ, probe_home, probe_temp))
            if owner.number is not None:
                raise KeyboardInterrupt()
            home, temporary = scratch / "parallel-home", scratch / "parallel-temp"
            home.mkdir(mode=0o700)
            temporary.mkdir(mode=0o700)
            environment = make_environment(profile, os.environ, home, temporary)
            argv = [profile["interpreter"], "-E", "-s", "-B", "-c", _SCRIPT_BOOTSTRAP,
                    profile["tools"]["python_import_root"], str(root / "scripts/test-parallel.py"),
                    "run", "--workers", "8", "--profile", cost_identity["path"],
                    "--output", str(output / "parallel")]
            launch = {"phase": "parallel", "argv": argv, "environment": environment, "collected": False}
            receipt["launches"].append(launch)
            with (output / "parallel.log").open("xb") as log:
                started = controller_clock()
                code, _raw = _capture(argv, cwd=root, environment=environment,
                    cancellation=owner, log=log, record=launch)
            elapsed = controller_clock() - started
            launch.update(exit_code=code, collected=True, external_wall_seconds=elapsed)
            receipt["parallel_wall_seconds"] = elapsed
            path = output / "parallel/final.json"
            native = _json(path)
            _admit_diagnostic(launcher, path, native, launch, source, cost_identity,
                              cost_bytes, receipt["preflight"]["stages"]["controller"]["interpreter"])
            native_admitted = True
            receipt.update(status="complete", successful=native["successful"],
                           native_final={"path": str(path),
                                         "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        except KeyboardInterrupt as exc:
            if owner.number is None:
                owner.number = getattr(exc, "number", signal.SIGINT)
            receipt.update(status="interrupted", error="diagnostic cancelled")
        except Exception as exc:
            receipt.update(status="invalid", successful=False, error=type(exc).__name__ + ": " + str(exc))
        finally:
            try:
                receipt["source_after"] = launcher.source_identity(root)
                receipt["registry_after"] = _read_registry()[1]
                receipt["costs_after"] = _diagnostic_costs(costs)[1]
                if (receipt["source_after"] != source or receipt["registry_after"] != registry
                        or receipt["costs_after"] != cost_identity):
                    raise ValueError("diagnostic source, fixed registry or advisory costs changed")
            except Exception as exc:
                receipt.update(status="invalid", successful=False,
                               final_verification_error=type(exc).__name__ + ": " + str(exc))
            with final_commitment(owner):
                collected = all(row["collected"] for row in receipt["launches"])
                safe = collected and (native_admitted or not receipt["launches"])
                if scratch is not None:
                    try:
                        info = _ordinary(scratch, directory=True).lstat()
                        if (info.st_dev, info.st_ino, info.st_uid, stat.S_IMODE(info.st_mode)) != scratch_identity:
                            raise ValueError("diagnostic scratch allocation identity changed")
                        if safe:
                            import shutil
                            shutil.rmtree(scratch)
                    except Exception as exc:
                        safe = False
                        receipt["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
                retained = scratch is not None and os.path.lexists(scratch)
                receipt["cleanup"] = {"successful": safe and not retained,
                                      "direct_children_collected": collected,
                                      "native_cleanup_proved": native_admitted,
                                      "scratch_retained": retained}
                if not receipt["cleanup"]["successful"]:
                    receipt.update(status="invalid", successful=False)
                if owner.number is not None:
                    receipt.update(status="interrupted", successful=False, exit_code=128 + owner.number)
                if owner.delivery_errors:
                    receipt["signal_delivery_errors"] = owner.delivery_errors
                _publish(output / "diagnostic.json", receipt)
    if not receipt["launches"] and receipt.get("error"):
        raise ValueError(receipt["error"])
    return receipt


def run_pair(root, output, *, _supervision=None):
    """Run complete serial then eight workers; qualify native parity and cost."""
    profile, registry = _read_registry()
    identity = validate_repository(root, profile)
    root = Path(identity["workspace_root"])
    if _git(root, "status", "--porcelain", "--untracked-files=all").strip():
        raise ValueError("qualification requires clean committed source")
    launcher = _launcher(root)
    source = launcher.source_identity(root)
    if _supervision is not None and (type(_supervision) is not dict or set(_supervision) != {
            "parallel_start", "parallel_collected", "post_validation_complete", "published"}
            or any(not callable(hook) for hook in _supervision.values())):
        raise ValueError("invalid private supervision hooks")
    output = validate_output(root, output)
    output.mkdir(mode=0o700)
    owner = _Cancellation()
    with owner.handlers():
        return _run_pair(root, output, profile, registry, identity, launcher, source, owner, _supervision)


def _run_pair(root, output, profile, registry, identity, launcher, source, owner, supervision):
    receipt = {"status": "invalid", "qualified": False, "successful": False,
               "profile": "floati-harbor", "registry": registry, "source": source,
               "repository": identity, "launches": [], "cleanup": {"successful": False},
               "parallel_limit_seconds": PARALLEL_LIMIT_SECONDS,
               "qualification_scope": "external_supervisor_gate_pending" if supervision is not None
               else "native_parallel_spawn_to_collection_only_excludes_controller_completion"}
    phase_reports = []
    scratch = None
    try:
        platform_temp = platform_temp_root()
        scratch = Path(tempfile.mkdtemp(prefix="qualification-pair-", dir=platform_temp)).resolve()
        receipt["scratch_root"] = str(scratch)
        with owner.handlers():
            probe_home, probe_temp = scratch / "probe-home", scratch / "probe-temp"
            probe_home.mkdir(mode=0o700)
            probe_temp.mkdir(mode=0o700)
            env = make_environment(profile, os.environ, probe_home, probe_temp)
            receipt["preflight"] = preflight(profile, root, env)
            for mode in ("serial", "run"):
                if owner.number is not None:
                    raise KeyboardInterrupt()
                phase = "serial" if mode == "serial" else "parallel"
                home, temp = scratch / (phase + "-home"), scratch / (phase + "-tmp")
                home.mkdir(mode=0o700)
                temp.mkdir(mode=0o700)
                environment = make_environment(profile, os.environ, home, temp)
                argv = [profile["interpreter"], "-E", "-s", "-B", "-c", _SCRIPT_BOOTSTRAP,
                        profile["tools"]["python_import_root"], str(root / "scripts/test-parallel.py"),
                        mode, "--output", str(output / phase)]
                if mode == "run":
                    argv.extend(["--workers", "8", "--reference", str(output / "serial/final.json")])
                launch = {"phase": phase, "argv": argv, "environment": environment, "collected": False}
                receipt["launches"].append(launch)
                with (output / (phase + ".log")).open("xb") as log:
                    if phase == "parallel" and supervision is not None:
                        supervision["parallel_start"]()
                    began = controller_clock()
                    code, _raw = _capture(argv, cwd=root, environment=environment,
                                         cancellation=owner, log=log, record=launch)
                # Native final publication and direct-child exit both precede
                # this external measurement. The controller adds no tree scan.
                elapsed = controller_clock() - began
                launch.update(exit_code=code, collected=True, external_wall_seconds=elapsed)
                if phase == "parallel":
                    receipt["parallel_wall_seconds"] = elapsed
                    if supervision is not None:
                        supervision["parallel_collected"](elapsed)
                final_path = output / phase / "final.json"
                if not final_path.is_file():
                    raise ValueError("native phase did not publish its final cleanup receipt")
                report = _json(final_path)
                phase_reports.append(report)
                if (code != 0 or report.get("status") != "complete" or not report.get("successful")
                        or not report.get("cleanup", {}).get("successful") or report.get("source") != source):
                    raise ValueError("native phase status, source or cleanup could not qualify")
                launch["process_cost"] = _validate_cost(final_path, report, launch, source)
                for result in report["results"]:
                    if launcher.read_worker_result(final_path.parent, result["key"]) != result:
                        raise ValueError("native phase worker journal differs from final report")
                if mode == "serial":
                    launcher.reference_result(final_path, source, report["interpreter"])
                elif (not report.get("qualified") or not report.get("comparison", {}).get("equivalent")
                      or report.get("workers") != 8 or report["interpreter"] != phase_reports[0]["interpreter"]
                      or report.get("profile", {}).get("path") != str(output / "serial/final.json")):
                    raise ValueError("native same-source serial/reference parity is absent")
            if _read_registry()[1] != registry or launcher.source_identity(root) != source:
                raise ValueError("fixed registry or source changed during qualification")
            if supervision is None and not 0 <= receipt["parallel_wall_seconds"] <= PARALLEL_LIMIT_SECONDS:
                raise ValueError("external parallel wall exceeded the fixed 600-second qualification bound")
            receipt.update(status="awaiting_external_gate" if supervision is not None else "complete",
                           successful=True, qualified=supervision is None)
    except KeyboardInterrupt as exc:
        if owner.number is None:
            owner.number = getattr(exc, "number", signal.SIGINT)
        receipt.update(status="interrupted", error="qualification cancelled", exit_code=128 + (owner.number or signal.SIGINT))
    except Exception as exc:
        receipt.update(error=type(exc).__name__ + ": " + str(exc))
    finally:
        try:
            receipt["source_after"] = launcher.source_identity(root)
            receipt["registry_after"] = _read_registry()[1]
            if receipt["source_after"] != source or receipt["registry_after"] != registry:
                receipt.update(qualified=False, successful=False, status="invalid")
                receipt.setdefault("error", "source or fixed registry changed at final verification")
        except Exception as exc:
            receipt.update(qualified=False, successful=False, status="invalid",
                           final_verification_error=type(exc).__name__ + ": " + str(exc))
        with final_commitment(owner):
            if scratch is not None:
                collected = all(row["collected"] for row in receipt["launches"])
                native_cleanup = (len(phase_reports) == len(receipt["launches"])
                                  and all(report.get("cleanup", {}).get("successful") for report in phase_reports))
                safe = collected and native_cleanup
                if safe:
                    import shutil
                    shutil.rmtree(scratch)
                receipt["cleanup"] = {"successful": safe and not scratch.exists(),
                                      "direct_children_collected": collected,
                                      "native_cleanup_proved": native_cleanup,
                                      "scratch_retained": scratch.exists()}
                if not receipt["cleanup"]["successful"]:
                    receipt.update(qualified=False, successful=False, status="invalid")
            if owner.number is not None:
                receipt.update(status="interrupted", qualified=False, successful=False,
                               exit_code=128 + owner.number)
            if owner.delivery_errors:
                receipt["signal_delivery_errors"] = owner.delivery_errors
            if supervision is not None:
                supervision["post_validation_complete"]()
            path = output / "qualification.json"
            publication_began = controller_clock()
            _publish(path, receipt)
            publication_seconds = controller_clock() - publication_began
            if supervision is not None:
                raw = path.read_bytes()
                supervision["published"](path, hashlib.sha256(raw).hexdigest(), len(raw), publication_seconds)
    # Failed preflight is a caller-visible admission refusal, never a skipped
    # suite. The retained qualification receipt survives that refusal.
    if not receipt["launches"] and receipt.get("error"):
        raise ValueError(receipt["error"])
    return receipt


def supervise_pair(root, output):
    """Delegate only to this engine's exact sibling external supervisor."""
    path = _ordinary(Path(__file__).parent / "parallel_supervisor.py")
    specification = importlib.util.spec_from_file_location("qualification_external_supervisor", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module.supervise_pair(root, output, engine=globals(), clock=supervisor_clock)

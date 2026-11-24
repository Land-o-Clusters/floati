"""FL-2: native effective policy plus a shipped-source coordinate census.

No live root, provider, installer, process census or detached-child witness.
The suite HOME is intentionally not evidence: each native query uses hostile
local settings and observes options extracted from the product command owner.
"""
from __future__ import annotations

import ast
import collections
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from floati import git_process
from tests.test_no_listener_fence import _literal_python_programs

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {
    "floati/checkout_liveness.py": {"_read_git": 1},
    "floati/adhoc_threads.py": {"_git": 1},
    # Task1 source composition adds this one native Git owner; the next
    # registered full confirms the expected membership and total below.
    "floati/adhoc_journal.py": {
        "_git": 1
    },
    # These run/Popen callers now launch Python, counted separately below.
    # This is the actual Git exec reached inside their literal child program.
    "floati/adhoc_workspace.py": {
        "_BORROWED_GIT_PRELUDE.<module>": 1
    },
    "floati/git_process.py": {
        "is_shallow_repository": 1
    },
    "floati/manifest.py": {
        "_git_tracked_files": 2
    },
    "floati/deploy.py": {
        "_git": 1,
        "_is_reverse_transition": 1
    },
    "floati/fleet_update.py": {
        "_require_detached_source": 3
    },
    "floati/doctor.py": {
        "_git": 1,
        "_bridge_checkout_commit": 1
    },
    "floati/overlap_radar.py": {
        "_git": 1
    },
    "floati/prep_clear.py": {
        "PrepClear._git": 1
    },
    "floati/reader_currency.py": {
        "_git": 1
    },
    "floati/release.py": {
        "_write_tag_snapshot.git": 1,
        "check_release": 2
    },
    "floati/row_tree_retire.py": {
        "_default_git_run": 1
    },
    "floati/run_manifest.py": {
        "_workspace_commit": 1
    },
    "floati/sandbox_probe.py": {
        "_git_coordinate": 1
    },
    "floati/seat_onboarding.py": {
        "_git": 1
    },
    "floati/seat_teardown_activity.py": {
        "_command": 1
    },
    "floati/lane_workspaces.py": {
        "LaneWorkspaces._git": 1
    },
    "floati/waiter_bundle.py": {
        "waiter_source_provenance.git": 1
    },
    "floati/worktree_safety.py": {
        "_run": 1
    },
    "floati/bound_record.py": {
        "_git": 1
    },
    "floati/scrub.py": {
        "_git": 1,
        "_tracked_files": 2
    },
    "floati/lifecycle_prompts.py": {
        "harbor_sha": 1
    },
    "floati/update_apply.py": {
        "_git": 1
    },
    "floati/verification.py": {
        "DeliveryVerifier._git": 1
    },
    "floati/lane_scaling.py": {
        "LaneScalingService._git_status": 1
    },
    "floati/adapters/codex_live.py": {
        "CodexAppServerAdapter._git": 1
    },
    "floati/cli.py": {
        "_require_banked_sha": 4
    },
    "floati/effect_reconciliation_observer.py": {
        "_run_git": 1
    }
}
POLICY = {"gc.auto": "0", "gc.autoDetach": "false", "maintenance.auto": "false"}
LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output"}
OS_LAUNCHERS = {"execl", "execle", "execlp", "execlpe",
                "execv", "execve", "execvp", "execvpe", "posix_spawn", "posix_spawnp",
                "spawnv", "spawnve", "spawnvp", "spawnvpe"}
GIT_NAMES = {"executable", "git_executable", "git", "candidate", "_GIT"}


def _bindings(nodes):
    result = {}
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result[node.target.id] = node.value
    return result


def _tokens(node, bindings, seen=()):
    """Resolve only command construction, never evaluate source or imports."""
    if node is None:
        return []
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, (ast.List, ast.Tuple)):
        return [token for item in node.elts for token in _tokens(item, bindings, seen)]
    if isinstance(node, ast.Starred):
        return _tokens(node.value, bindings, seen)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _tokens(node.left, bindings, seen) + _tokens(node.right, bindings, seen)
    if isinstance(node, ast.Name):
        if node.id in seen:
            return []
        if node.id in bindings:
            return _tokens(bindings[node.id], bindings, (*seen, node.id))
        if node.id == "GIT_MAINTENANCE_OPTIONS":
            return list(getattr(git_process, node.id, ()))
        return []
    if isinstance(node, ast.Call):
        name = node.func.id if isinstance(node.func, ast.Name) else ""
        if name == "fixed_git_command":
            return git_process.fixed_git_command("/usr/bin/git", Path("/fixture"), ())
        if name in {"list", "tuple", "str", "Path"} and node.args:
            return _tokens(node.args[0], bindings, seen)
    return []


def _same_expression(node, source):
    return ast.dump(node) == ast.dump(ast.parse(source, mode="eval").body)


def _borrowed_git_contract(tree):
    """Trace real Python argv -> unchanged Git argv; never assume a prefix."""
    producer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_borrowed_git_launch")
    bindings = _bindings(ast.walk(producer))
    git = bindings["git_command"]
    assert isinstance(git, ast.Call) and _same_expression(git.func, "fixed_git_command")
    assert len(git.args) == 3 and not git.keywords
    assert _same_expression(git.args[0], "'/usr/bin/git'")
    assert _same_expression(git.args[1], "Path('.')")
    assert _same_expression(git.args[2], "('--literal-pathspecs', '--git-dir=.', '--bare', *arguments)")
    launch = bindings["launch_command"]
    assert _same_expression(launch, "[os.path.realpath(sys.executable), '-I', '-S', '-B', '-c', "
        "_BORROWED_GIT_PRELUDE, str(common_descriptor), str(common_identity['dev']), "
        "str(common_identity['ino']), *git_command]")
    returns = [node.value for node in ast.walk(producer) if isinstance(node, ast.Return)]
    assert len(returns) == 1 and _same_expression(returns[0], "(launch_command, environment)")
    # No sliced/reassigned Git vector, later argv mutation, or second prefix
    # producer can silently make the audited prefix differ from the exec.
    for name in ("git_command", "launch_command"):
        stores = [node for node in ast.walk(producer) if isinstance(node, ast.Name)
                  and node.id == name and isinstance(node.ctx, ast.Store)]
        assert len(stores) == 1
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and isinstance(node.func.value, ast.Name)
                   and node.func.value.id in {"git_command", "launch_command"}
                   for node in ast.walk(producer))
    source = ast.literal_eval(_bindings(tree.body)["_BORROWED_GIT_PRELUDE"])
    prelude = ast.parse(source)
    execs = [node for node in ast.walk(prelude) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "execve"]
    assert len(execs) == 1 and _same_expression(execs[0],
        "os.execve('/usr/bin/git', sys.argv[4:], os.environ)")
    assert not any(isinstance(node, (ast.Subscript, ast.Attribute))
                   and isinstance(node.ctx, (ast.Store, ast.Del))
                   for node in ast.walk(prelude))
    allowed_calls = {"int", "ValueError", "len", "os.fstat", "stat.S_ISDIR", "os.geteuid",
                     "os.fchdir", "os.close", "os.execve", "SystemExit"}
    assert all(ast.unparse(node.func) in allowed_calls for node in ast.walk(prelude)
               if isinstance(node, ast.Call))
    calls = [ast.unparse(node) for node in ast.walk(prelude) if isinstance(node, ast.Call)]
    for required in ("os.fstat(fd)", "os.fchdir(fd)", "os.close(fd)"):
        assert required in calls
    callers = {}
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for function in (node for node in cls.body if isinstance(node, ast.FunctionDef)):
            produced = [node for node in ast.walk(function) if isinstance(node, ast.Assign)
                        and isinstance(node.value, ast.Call)
                        and _same_expression(node.value.func, "_borrowed_git_launch")]
            if not produced:
                continue
            assert len(produced) == 1 and len(produced[0].targets) == 1
            target = produced[0].targets[0]
            assert isinstance(target, ast.Tuple) and len(target.elts) == 2
            assert all(isinstance(item, ast.Name) for item in target.elts)
            assert [item.id for item in target.elts] == ["command", "environment"]
            launches = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess" and node.func.attr in LAUNCHERS]
            assert len(launches) == 1 and _same_expression(launches[0].args[0], "command")
            keywords = {item.arg: item.value for item in launches[0].keywords}
            assert _same_expression(keywords["env"], "environment")
            assert _same_expression(keywords["close_fds"], "True")
            assert _same_expression(keywords["pass_fds"], "(self._fds['common'],)")
            assert "preexec_fn" not in keywords and "cwd" not in keywords
            callers[f"{cls.name}.{function.name}"] = launches[0].func.attr
    assert callers == {"BorrowedWorkspace._git": "run", "BorrowedWorkspace._update_ref": "Popen"}
    return _tokens(git, bindings), callers


def _coordinates(path, source, *, prefix="", forwarded=None):
    tree = ast.parse(source, filename=path)
    global_bindings = _bindings(tree.body)
    module_aliases = {"subprocess"}
    imported_launchers = set()
    os_aliases = {"os"}
    imported_os_launchers = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_aliases.update(item.asname or item.name for item in node.names
                                  if item.name == "subprocess")
            os_aliases.update(item.asname or item.name for item in node.names if item.name == "os")
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            imported_launchers.update(item.asname or item.name for item in node.names
                                      if item.name in LAUNCHERS)
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            imported_os_launchers.update((item.asname or item.name, item.name)
                                         for item in node.names if item.name in OS_LAUNCHERS)
    result = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
            self.scopes = []

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self.stack.append(node.name)
            self.scopes.append(_bindings(ast.walk(node)))
            self.generic_visit(node)
            self.scopes.pop()
            self.stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            func = node.func
            module_launch = (isinstance(func, ast.Attribute)
                             and isinstance(func.value, ast.Name)
                             and func.value.id in module_aliases
                             and func.attr in LAUNCHERS)
            imported_launch = isinstance(func, ast.Name) and func.id in imported_launchers
            os_operation = (func.attr if isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name) and func.value.id in os_aliases
                            and func.attr in OS_LAUNCHERS else
                            imported_os_launchers.get(func.id) if isinstance(func, ast.Name) else None)
            if module_launch or imported_launch or os_operation:
                owner = prefix + (".".join(self.stack) or "<module>")
                bindings = dict(global_bindings)
                for scope in self.scopes:
                    bindings.update(scope)
                declared = owner in EXPECTED.get(path, {})
                if declared:
                    # Executable parameters are Git only in independently
                    # audited Git owners. Do not relabel Minisign/provider
                    # launchers merely because they use the same variable name.
                    for name in GIT_NAMES:
                        bindings.setdefault(name, ast.Constant(value="/usr/bin/git"))
                argv = node.args[0] if node.args else next(
                    (item.value for item in node.keywords if item.arg == "args"), None)
                executable = None
                if os_operation:
                    offset = 1 if os_operation.startswith("spawn") else 0
                    executable = node.args[offset] if len(node.args) > offset else None
                    argv = (ast.List(elts=node.args[1:], ctx=ast.Load())
                            if os_operation.startswith("execl") else
                            node.args[offset + 1] if len(node.args) > offset + 1 else None)
                tokens = _tokens(argv, bindings)
                if forwarded is not None and argv is not None and _same_expression(argv, "sys.argv[4:]"):
                    tokens = forwarded
                executable_tokens = _tokens(executable, bindings)
                if executable_tokens and Path(executable_tokens[0]).name == "git":
                    tokens = [executable_tokens[0], *tokens[1:]]
                # These two launchers accept other executable classes too.
                # Their Git producers are separately checked below.
                if path == "floati/seat_teardown_activity.py" and owner == "_command":
                    tokens = git_process.fixed_git_command(
                        "/usr/bin/git", Path("/fixture"), ())
                if path == "floati/effect_reconciliation_observer.py" and owner == "_run_git":
                    tokens = _tokens(global_bindings["_GIT_PREFIX"], global_bindings)
                is_git = bool(tokens and Path(tokens[0]).name == "git")
                if declared or is_git:
                    result.append((owner, node.lineno, tokens))
            self.generic_visit(node)

    Visitor().visit(tree)
    if not prefix:
        programs = _literal_python_programs(tree)
        forwarded_tokens = None
        if path == "floati/adhoc_workspace.py" and "_BORROWED_GIT_PRELUDE" in programs:
            forwarded_tokens, _callers = _borrowed_git_contract(tree)
        for label, program in programs.items():
            result.extend(_coordinates(path, program, prefix=label + ".",
                forwarded=forwarded_tokens if label == "_BORROWED_GIT_PRELUDE" else None))
    return result


def _census():
    result = {}
    manifest = ast.parse((ROOT / "floati/manifest.py").read_text(encoding="utf-8"))
    dark = ast.literal_eval(_bindings(manifest.body)["_DARK_DEPLOYMENT_PREFIXES"])
    for path in sorted((ROOT / "floati").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith(dark):
            continue
        sites = _coordinates(relative, path.read_text(encoding="utf-8"))
        if sites:
            result[relative] = sites
    return result


def _command_options(tokens):
    """Keep real prefix -c pairs; never replay a product mutation in a test."""
    result = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"rev-parse", "status", "ls-files", "for-each-ref",
                     "cat-file", "ls-remote", "config", "show", "worktree"}:
            break
        if token == "-c":
            if index + 1 >= len(tokens):
                raise AssertionError("incomplete product -c option")
            result.extend(tokens[index:index + 2])
            index += 2
        else:
            index += 1
    return result


class ProductGitMaintenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="floati-fl2-")
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.repository = Path(cls.temporary.name).resolve() / "repository"
        cls.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        cls.environment.update({"GIT_CONFIG_NOSYSTEM": "1",
                                "GIT_CONFIG_GLOBAL": "/dev/null",
                                "GIT_CONFIG_SYSTEM": "/dev/null",
                                "GIT_TERMINAL_PROMPT": "0"})
        subprocess.run(
            ["/usr/bin/git", "-c", "gc.auto=0", "-c", "gc.autoDetach=false",
             "-c", "maintenance.auto=false", "init", "--quiet", str(cls.repository)],
            env=cls.environment, check=True, capture_output=True, timeout=5)
        cls.config = cls.repository / ".git/config"
        with cls.config.open("a", encoding="utf-8") as stream:
            stream.write("\n[gc]\n\tauto = 1\n\tautoDetach = true\n"
                         "[maintenance]\n\tauto = true\n")
        cls.before = cls.config.read_bytes()
        cls.sites = _census()

    def test_every_shipped_git_coordinate_has_an_owner(self):
        observed = {
            path: dict(collections.Counter(owner for owner, _line, _argv in sites))
            for path, sites in self.sites.items()
        }
        self.assertEqual(EXPECTED, observed)
        # 41 direct Git subprocess sites (bound records and checkout liveness
        # add one each) + one actual nested Git exec. The two Python launch
        # owners are independently bound by the contract.
        self.assertEqual(42, sum(sum(rows.values()) for rows in observed.values()))
        # Generic teardown Git must still go through its explicit producer.
        source = (ROOT / "floati/seat_teardown_activity.py").read_text()
        tree = ast.parse(source)
        producer = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_git")
        self.assertTrue(any(isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name)
                            and node.func.id == "fixed_git_command"
                            for node in ast.walk(producer)))

    def test_census_detects_a_new_unprotected_git_launch(self):
        source = (ROOT / "floati/git_process.py").read_text()
        cases = (
            "\ndef unprotected():\n    subprocess.run(['git', 'status'])\n",
            "\nimport subprocess as sp\ndef unprotected():\n    sp.run(['git', 'status'])\n",
            "\nfrom subprocess import run as launch\ndef unprotected():\n    launch(['git', 'status'])\n",
            "\ndef unprotected():\n    subprocess.run([str(Path('/usr/bin/git')), 'status'])\n",
            "\nimport os\ndef unprotected():\n    os.execve('/usr/bin/git', ['/usr/bin/git', 'status'], {})\n",
            "\nimport os as host\ndef unprotected():\n    host.execv('/usr/bin/git', ['/usr/bin/git', 'status'])\n",
            "\nfrom os import execve as launch\ndef unprotected():\n    launch('/usr/bin/git', ['/usr/bin/git', 'status'], {})\n",
            "\nimport os\ndef unprotected():\n    os.posix_spawn('/usr/bin/git', ['/usr/bin/git', 'status'], {})\n",
            "\nimport os\ndef unprotected():\n    os.execl('/usr/bin/git', 'git', 'status')\n",
        )
        for extra in cases:
            with self.subTest(source=extra):
                sites = _coordinates("floati/git_process.py", source + extra)
                self.assertIn("unprotected", {owner for owner, _line, _argv in sites})
                added = next(argv for owner, _line, argv in sites if owner == "unprotected")
                self.assertEqual([], _command_options(added))

    def test_literal_child_git_exec_is_discovered_without_an_owner_entry(self):
        source = ("import subprocess,sys\n"
                  "CHILD = \"import os; os.execve('/usr/bin/git', ['/usr/bin/git', 'status'], {})\"\n"
                  "def launch():\n"
                  "    subprocess.run([sys.executable, '-I', '-S', '-B', '-c', CHILD])\n")
        sites = _coordinates("floati/new_owner.py", source)
        self.assertEqual(1, len(sites))
        self.assertEqual("CHILD.<module>", sites[0][0])
        self.assertEqual([], _command_options(sites[0][2]))
        # The native protected-prefix assertion would actually fail under the
        # same hostile config, not merely match an expected owner by hand.
        query = subprocess.run(["/usr/bin/git", *_command_options(sites[0][2]),
            "-C", str(self.repository), "config", "--get", "gc.auto"],
            env=self.environment, capture_output=True, text=True, timeout=5, check=True)
        self.assertNotEqual(POLICY["gc.auto"], query.stdout.strip())

    def test_borrowed_exec_contract_rejects_changed_forwarding_or_executable(self):
        source = (ROOT / "floati/adhoc_workspace.py").read_text(encoding="utf-8")
        _borrowed_git_contract(ast.parse(source))
        mutations = (
            ("*git_command]", "*git_command[3:]]"),
            ("fixed_git_command(\"/usr/bin/git\", Path(\".\"),", "fixed_git_command(\"/bin/echo\", Path(\".\"),"),
            ("os.execve('/usr/bin/git',sys.argv[4:],os.environ)",
             "os.execve('/usr/bin/git',['/usr/bin/git','status'],os.environ)"),
            ("os.execve('/usr/bin/git',sys.argv[4:],os.environ)",
             "os.execve('/bin/echo',sys.argv[4:],os.environ)"),
            (" os.close(fd)\\n", " pass\\n"),
        )
        for before, after in mutations:
            with self.subTest(mutation=before):
                self.assertIn(before, source)
                with self.assertRaises(AssertionError):
                    _coordinates("floati/adhoc_workspace.py", source.replace(before, after, 1))

    def test_actual_borrowed_child_git_prefix_overrides_hostile_local_config(self):
        from floati.adhoc_workspace import _borrowed_git_launch
        descriptor = os.open(self.repository / ".git", os.O_RDONLY | os.O_DIRECTORY)
        try:
            metadata = os.fstat(descriptor)
            for key, expected in POLICY.items():
                with self.subTest(key=key):
                    command, environment = _borrowed_git_launch(descriptor,
                        {"dev": metadata.st_dev, "ino": metadata.st_ino}, ("config", "--get", key))
                    query = subprocess.run(command, env=environment, close_fds=True,
                        pass_fds=(descriptor,), capture_output=True, text=True, timeout=5, check=True)
                    self.assertEqual(expected, query.stdout.strip())
        finally:
            os.close(descriptor)
        self.assertEqual(self.before, self.config.read_bytes())

    def test_census_does_not_relabel_unrelated_executable_parameters(self):
        source = ("import subprocess\n"
                  "def verify_signature(executable):\n"
                  "    subprocess.run([executable, '-Vm', 'artifact'])\n")
        self.assertEqual([], _coordinates("floati/signature.py", source))

    def test_isolated_observer_git_producers_keep_the_prefix(self):
        from floati import effect_reconciliation_observer as observer

        path = ROOT / "floati/effect_reconciliation_observer.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        globals_ = _bindings(tree.body)
        prefix = _tokens(globals_["_GIT_PREFIX"], globals_)
        self.assertEqual(1, prefix.count("--no-lazy-fetch"))
        # The Git 2.43 exception belongs only to ls-remote, which reads the
        # advertisement without resolving local objects. Resolve this one
        # reviewed producer without widening the generic token resolver.
        self.assertTrue(_same_expression(globals_["_REMOTE_GIT_PREFIX"],
            "tuple(option for option in _GIT_PREFIX if option != '--no-lazy-fetch')"))
        remote_prefix = [token for token in prefix if token != "--no-lazy-fetch"]
        globals_["_REMOTE_GIT_PREFIX"] = ast.List(
            elts=[ast.Constant(value=token) for token in remote_prefix], ctx=ast.Load())
        resolved_remote = _tokens(globals_["_REMOTE_GIT_PREFIX"], globals_)
        self.assertEqual(collections.Counter({"--no-lazy-fetch": 1}),
                         collections.Counter(prefix) - collections.Counter(resolved_remote))
        self.assertEqual(collections.Counter(),
                         collections.Counter(resolved_remote) - collections.Counter(prefix))
        self.assertEqual(remote_prefix, resolved_remote)
        self.assertEqual("1", ast.literal_eval(
            globals_["_FIXED_GIT_ENVIRONMENT"])["GIT_NO_LAZY_FETCH"])
        self.assertEqual("1", observer._git_environment(remote=True)["GIT_NO_LAZY_FETCH"])
        count = 0
        remote_count = 0
        for function in tree.body:
            if not isinstance(function, ast.FunctionDef):
                continue
            bindings = {**globals_, **_bindings(ast.walk(function))}
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "_run_git"):
                    count += 1
                    tokens = _tokens(node.args[0], bindings)
                    self.assertEqual("/usr/bin/git", tokens[0])
                    if tokens[len(remote_prefix):len(remote_prefix) + 1] == ["ls-remote"]:
                        remote_count += 1
                        self.assertEqual(remote_prefix, tokens[:len(remote_prefix)])
                        self.assertTrue(_same_expression(
                            node.args[1], "_git_environment(remote=True)"))
                    else:
                        self.assertEqual(prefix, tokens[:len(prefix)])
        self.assertEqual(3, count)
        self.assertEqual(1, remote_count)

    def test_product_git_prefix_overrides_hostile_local_config(self):
        prefixes = set()
        for path, sites in self.sites.items():
            for owner, line, argv in sites:
                with self.subTest(path=path, owner=owner, line=line):
                    self.assertTrue(argv and Path(argv[0]).name == "git",
                                    "unresolved product Git coordinate")
                    prefixes.add(tuple(_command_options(argv)))
        for prefix in sorted(prefixes):
            for key, expected in POLICY.items():
                with self.subTest(prefix=prefix, key=key):
                    query = subprocess.run(
                        ["/usr/bin/git", *prefix,
                         "-C", str(self.repository), "config", "--get", key],
                        env=self.environment, capture_output=True, text=True,
                        timeout=5, check=True)
                    self.assertEqual(expected, query.stdout.strip())
        self.assertEqual(self.before, self.config.read_bytes())


if __name__ == "__main__":
    unittest.main()

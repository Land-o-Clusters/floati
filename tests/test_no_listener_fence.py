from __future__ import annotations

import ast
import collections
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from typing import Optional

from tests.export_inventory import (
    classify_inventory,
    export_policy_is_present,
    materialise_adapted_tree,
    tracked_files,
)
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

PROJECTED_FENCE_IDS = tuple(
    "tests.test_no_listener_fence.WholeProductNoListenerFenceTests." + name
    for name in (
        "test_network_capable_imports_and_subprocesses_are_fully_counted",
        "test_subprocess_population_is_pinned",
        "test_built_argv_egress_is_enumerated_not_blind",
        "test_built_argv_egress_sites_are_pinned",
        "test_bind_and_listen_calls_exist_only_in_the_ruled_af_unix_supervisor",
        "test_mcp_module_imports_only_local_code_and_non_network_stdlib",
    )
)


@contextmanager
def _adapted_fence_projection(*, prepare_git=True):
    """Own one strict adapted tree; native sensitivity callers also need Git."""
    with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
        projection = Path(temporary) / "projection"
        projection.mkdir()
        materialise_adapted_tree(projection, root=REPOSITORY_ROOT)
        if prepare_git:
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith("GIT_")}
            for arguments in (
                ("init", "-q", "--initial-branch=main"),
                ("config", "user.name", "fixture"),
                ("config", "user.email", "fixture@example.invalid"),
                ("add", "."),
                ("commit", "-q", "-m", "projection fixture"),
            ):
                # FL-1: no detached maintenance may outlive the owned tree.
                subprocess.run(
                    ["/usr/bin/git", "-c", "gc.auto=0", "-c", "gc.autoDetach=false", *arguments],
                    cwd=projection, env=environment, check=True, capture_output=True)
        yield projection


def _run_projected_fences(projection, selectors=PROJECTED_FENCE_IDS):
    return subprocess.run(
        [sys.executable, "-m", "unittest", "-v", *selectors],
        cwd=projection, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True)


def _assert_projected_fences(case, completed):
    """A loader error, missing method or skip cannot masquerade as six passes."""
    evidence = completed.stdout + completed.stderr
    case.assertEqual(0, completed.returncode, "projected product fences failed:\n" + evidence)
    observed = []
    for method, owner in re.findall(r"^(test_\w+) \(([^)]+)\)", completed.stderr, re.MULTILINE):
        if owner.endswith("." + method):
            owner = owner[:-(len(method) + 1)]
        observed.append(owner + "." + method)
    case.assertEqual(list(PROJECTED_FENCE_IDS), observed,
                     "projected fence selection differs:\n" + evidence)
    case.assertRegex(completed.stderr, r"(?m)^Ran 6 tests in ", evidence)
    case.assertNotIn("skipped", completed.stderr.lower(), evidence)
    case.assertRegex(completed.stderr, r"(?m)^OK$", evidence)


def _rooted_fence(projection, selector):
    fence = WholeProductNoListenerFenceTests(selector.rsplit(".", 1)[1])
    fence.repository_root = projection
    return fence


def _assert_in_process_fences(case, projection, selectors=PROJECTED_FENCE_IDS):
    fences = [_rooted_fence(projection, selector) for selector in selectors]
    case.assertEqual(list(selectors), [fence.id() for fence in fences])
    result = unittest.TestResult()
    unittest.TestSuite(fences).run(result)
    case.assertEqual(len(selectors), result.testsRun)
    case.assertEqual([], result.skipped)
    case.assertTrue(result.wasSuccessful(), result.failures + result.errors)


@lru_cache(maxsize=1024)
def _parsed_source(path: Path, source: str) -> ast.AST:
    """Share read-only ASTs, keyed by path AND the freshly read content."""
    return ast.parse(source, filename=path.as_posix())


def _parse_file(path: Path) -> ast.AST:
    return _parsed_source(path, path.read_text(encoding="utf-8"))


_NETWORK_SUBPROCESS_METHODS = frozenset(
    {"Popen", "call", "check_call", "check_output", "run"}
)
_OS_EGRESS_FUNCS = frozenset(
    {
        "system", "popen",
        "exec", "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
        "spawn", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn", "posix_spawnp",
    }
)
_NETWORK_TOOL_BASENAMES = frozenset(
    {
        "curl",
        "ftp",
        "gh",
        "git-remote-ext",
        "git-remote-ftp",
        "git-remote-ftps",
        "git-remote-http",
        "git-remote-https",
        "nc",
        "ncat",
        "pip",
        "pip3",
        "rsync",
        "scp",
        "sftp",
        "socat",
        "ssh",
        "telnet",
        "wget",
    }
)
_NETWORK_GIT_VERBS = frozenset(
    {"clone", "fetch", "ls-remote", "pull", "push", "submodule"}
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_.+-]+")


def _literal_string(node: ast.AST) -> Optional[str]:
    """The string a fully-literal expression denotes, else None."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal_string(node.left), _literal_string(node.right)
        if left is not None and right is not None:
            return left + right
    return None


def _argv_string_literals(
    expression: ast.AST,
    definitions: dict[str, ast.FunctionDef],
    assignments: dict[str, ast.AST] | None = None,
) -> set[str]:
    """Expand argv builders, scope assignments, and BUILT literals.

    NET-FENCE-1 Am.1: a tool name split across concatenation and a verb
    carried inside an f-string segment, a .format template, or a
    shlex.split literal are decidable — the joined/harvested string is
    added alongside the individual parts so tokenization can decide it.
    """

    literals: set[str] = set()
    expanded: set[str] = set()
    assignments = assignments or {}

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            joined = _literal_string(node)
            if joined is not None:
                literals.add(joined)
        elif isinstance(node, ast.JoinedStr):
            literals.add("".join(
                part.value
                for part in node.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            ))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "format"
            and isinstance(node.func.value, ast.Constant)
            and isinstance(node.func.value.value, str)
        ):
            literals.add(node.func.value.value)
        if isinstance(node, ast.Name):
            key = f"name:{node.id}"
            assigned = assignments.get(node.id)
            if assigned is not None and key not in expanded:
                expanded.add(key)
                visit(assigned)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            definition = definitions.get(name)
            if definition is not None and name not in expanded:
                expanded.add(name)
                for candidate in ast.walk(definition):
                    if isinstance(candidate, ast.Return) and candidate.value is not None:
                        literals.update(_argv_string_literals(
                            candidate.value,
                            {key: value for key, value in definitions.items() if key != name},
                            {**assignments, **_scope_assignments(definition)},
                        ))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(expression)
    return literals


def _network_subprocess_signature(literals: set[str]) -> Optional[str]:
    tokens: set[str] = set()
    for value in literals:
        tokens.update(_TOKEN_PATTERN.findall(value))
    basenames = {token.rsplit("/", 1)[-1] for token in tokens}
    if {"issue", "view", "--repo", "--json"} <= literals:
        return "gh issue view"
    tools = sorted(basenames & _NETWORK_TOOL_BASENAMES)
    if tools:
        return tools[0]
    git_names = {"git", "git.exe"}
    verbs = sorted(tokens & _NETWORK_GIT_VERBS)
    if basenames & git_names and verbs:
        return "git " + verbs[0]
    return None


def _argv_expression(node: ast.Call) -> ast.AST | None:
    return node.args[0] if node.args else next(
        (
            keyword.value
            for keyword in node.keywords
            if keyword.arg == "args"
        ),
        None,
    )


def _egress_call_resolver(tree: ast.AST):
    """Resolve native calls and imported owned-child wrappers from source.

    Imports are evidence only: nothing imports or executes the named module.
    Aliased helper imports share one canonical identity, so renaming a local
    binding cannot hide an egress call or fabricate a different site digest.
    """
    modules = {}
    functions = {}
    helpers = {"owned_child", "run_child"}
    origins = {"fixture_finalizer", "scripts.fixture_finalizer"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in origins:
                    modules[alias.asname or alias.name] = "fixture_finalizer"
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module in origins:
                for alias in node.names:
                    if alias.name in helpers:
                        functions[alias.asname or alias.name] = "fixture_finalizer." + alias.name
                    elif alias.name == "*":
                        functions.update({name: "fixture_finalizer." + name for name in helpers})
            elif node.module == "scripts":
                for alias in node.names:
                    if alias.name == "fixture_finalizer":
                        modules[alias.asname or alias.name] = "fixture_finalizer"

    def resolve(node):
        if not isinstance(node, ast.Call):
            return None
        parts = []
        expression = node.func
        while isinstance(expression, ast.Attribute):
            parts.append(expression.attr)
            expression = expression.value
        if not isinstance(expression, ast.Name):
            return None
        parts.append(expression.id)
        name = ".".join(reversed(parts))
        canonical = functions.get(name)
        if canonical is None and "." in name:
            module, method = name.rsplit(".", 1)
            if module == "subprocess" and method in _NETWORK_SUBPROCESS_METHODS:
                canonical = name
            elif module == "os" and method in _OS_EGRESS_FUNCS:
                return name, list(node.args)
            elif module in modules and method in helpers:
                canonical = modules[module] + "." + method
        if canonical is None:
            return None
        argv = _argv_expression(node)
        return canonical, [] if argv is None else [argv]

    return resolve


def _scope_assignments(scope: ast.AST) -> dict[str, ast.AST]:
    """Scope names; conservatively follow tuple-return argv producers too."""

    assignments: dict[str, ast.AST] = {}
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            assignments.setdefault(node.targets[0].id, node.value)
        elif (isinstance(node, ast.Assign) and len(node.targets) == 1
              and isinstance(node.targets[0], (ast.Tuple, ast.List))):
            for index, target in enumerate(node.targets[0].elts):
                if isinstance(target, ast.Name):
                    value = (node.value.elts[index]
                             if isinstance(node.value, (ast.Tuple, ast.List))
                             and index < len(node.value.elts) else node.value)
                    assignments.setdefault(target.id, value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
        ):
            assignments.setdefault(node.target.id, node.value)
    return assignments


def _top_level_assignments(body: list[ast.stmt]) -> dict[str, ast.AST]:
    """Single-target name assignments among a module's own statements."""

    assignments: dict[str, ast.AST] = {}
    for stmt in body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        ):
            assignments.setdefault(stmt.targets[0].id, stmt.value)
        elif (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.value is not None
        ):
            assignments.setdefault(stmt.target.id, stmt.value)
    return assignments


def _literal_python_programs(tree: ast.AST) -> dict[str, str]:
    """Read closed -c programs reached through real egress argv builders.

    Parsing a literal does not execute it. Keep these execution surfaces
    separate from the enclosing file's direct subprocess population.
    """
    definitions = {node.name: node for node in tree.body
                   if isinstance(node, ast.FunctionDef)}
    globals_ = _top_level_assignments(tree.body)
    parents = {child: parent for parent in ast.walk(tree)
               for child in ast.iter_child_nodes(parent)}
    programs = {}
    resolve = _egress_call_resolver(tree)
    for node in ast.walk(tree):
        call = resolve(node)
        if call is None:
            continue
        _identity, expressions = call
        scopes = []
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append(parent)
            parent = parents.get(parent)
        assignments = dict(globals_)
        for scope in reversed(scopes):
            assignments.update(_scope_assignments(scope))
        literals = set()
        for expression in expressions:
            literals |= _argv_string_literals(expression, definitions, assignments)
        if "-c" not in literals:
            continue
        for literal in sorted(literals):
            try:
                program = ast.parse(literal)
            except (SyntaxError, ValueError):
                continue
            if not any(isinstance(part, (ast.Import, ast.ImportFrom, ast.Call))
                       for part in ast.walk(program)):
                continue
            names = sorted(name for name, value in globals_.items()
                           if _literal_string(value) == literal)
            label = names[0] if names else "literal-" + hashlib.sha256(literal.encode()).hexdigest()[:16]
            programs[label] = literal
    return programs


_BUILT_ARGV_NODES = (ast.BinOp, ast.JoinedStr)


def _is_built_argv_node(node: ast.AST) -> bool:
    """An argv piece the fence cannot read as a plain literal list element:

    BinOp (concat or path build), f-string, ``str.format``,
    ``shlex.split``, or ``str.join``. NET-FENCE-1 Am.1 named the class;
    F1 extends it to ``.join`` so a live join-built site is enumerated
    by the census even though the DETECTOR still does not decide it.
    """

    if isinstance(node, _BUILT_ARGV_NODES):
        return True
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and (
            node.func.attr == "format"
            or node.func.attr == "join"
            or (
                node.func.attr == "split"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "shlex"
            )
        )
    )


def _built_argv_tree_egress_sites(tree: ast.AST) -> set[str]:
    """Derive the built-argv site identities in one source-only AST."""
    sites = set()
    resolve = _egress_call_resolver(tree)
    for node in ast.walk(tree):
        call = resolve(node)
        if call is None:
            continue
        identity, argvs = call
        if argvs and any(_is_built_argv_node(piece)
                         for argv in argvs for piece in ast.walk(argv)):
            payload = ast.dump(ast.Module(body=list(argvs), type_ignores=[]),
                               annotate_fields=False)
            sites.add(hashlib.sha256(f"{identity}|{payload}".encode("utf-8")).hexdigest()[:16])
    return sites


def _built_argv_egress_sites(
    roots: list[Path], *, relative_to: Path | None = None,
) -> set[tuple[str, str]]:
    """NET-FENCE-1-F3: every egress call site whose argv is BUILT, per site.

    Identity is (file, site digest); the digest hashes the call's
    module-qualified callable plus its argv expression's AST. The old
    census returned FILES, so a second built-argv call landing in an
    already-pinned file was invisible to every pin that consumed it --
    two of the five files this census measures carry two sites each.
    An edited argv changes the digest (the pin reds); a reformatted
    one does not (ast.dump normalizes layout).
    """

    sites: set[tuple[str, str]] = set()
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            tree = _parse_file(path)
            relative = path if relative_to is None else path.relative_to(relative_to)
            sites.update((relative.as_posix(), digest)
                         for digest in _built_argv_tree_egress_sites(tree))
    return sites


def _built_argv_egress_files(
    roots: list[Path], *, relative_to: Path | None = None,
) -> set[str]:
    """Files holding an egress call site whose argv is a BUILT expression.

    A projection of the site census: file granularity is the blind spot
    NET-FENCE-1-F3 removed, kept only for the census's own
    documentation.
    """

    return {path for path, _ in _built_argv_egress_sites(roots, relative_to=relative_to)}


def _network_subprocess_findings(tree: ast.AST) -> set[tuple[str, str]]:
    """(function, signature) for every NETWORK-shaped egress call site.

    Covers the subprocess module's executing methods AND the os spawn
    family (system/popen/exec*/spawn*/posix_spawn). Argv literals are
    read directly, through enclosing-scope assignments (module first,
    then outermost function inward), and through module-level builder
    functions. NET-FENCE-1 keeps the whole-product consent gate
    identical while the detection widens; the population pin fails by
    name when any new egress call site lands.
    """

    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    assignments_by_scope: dict[int | None, dict[str, ast.AST]] = {
        None: _top_level_assignments(tree.body)
    }
    for function_node in (
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        assignments_by_scope[id(function_node)] = _scope_assignments(function_node)

    findings: set[tuple[str, str]] = set()
    resolve = _egress_call_resolver(tree)
    for node in ast.walk(tree):
        call = resolve(node)
        egress_args = [] if call is None else call[1]
        if not egress_args:
            continue
        functions: list[ast.AST] = []
        parent = parents.get(node)
        while parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(parent)
            parent = parents.get(parent)
        assignments = dict(assignments_by_scope[None])
        for function_node in reversed(functions):
            assignments.update(assignments_by_scope[id(function_node)])
        literals: set[str] = set()
        for expression in egress_args:
            literals |= _argv_string_literals(expression, definitions, assignments)
        signature = _network_subprocess_signature(literals)
        if signature is None:
            continue
        findings.add(
            (functions[0].name if functions else "<module>", signature)
        )
    return findings


def _subprocess_population(tree: ast.AST) -> tuple[dict[str, int], dict[str, int]]:
    """Class every subprocess-family call site: by kind and by argv shape."""

    kinds: dict[str, int] = collections.Counter()
    argv_classes: dict[str, int] = collections.Counter()
    resolve = _egress_call_resolver(tree)
    for node in ast.walk(tree):
        call = resolve(node)
        if call is None:
            continue
        kind, _expressions = call
        shell = any(
            kw.arg == "shell"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        )
        argv = _argv_expression(node)
        argv_class = "none"
        if argv is not None:
            literals = [
                n.value
                for n in ast.walk(argv)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            has_name_node = any(isinstance(n, ast.Name) for n in ast.walk(argv))
            if isinstance(argv, ast.Constant):
                argv_class = "string-const" + (" shell" if shell else "")
            elif literals and not has_name_node:
                argv_class = "all-literal-list"
            elif literals and has_name_node:
                argv_class = "mixed"
            else:
                argv_class = "dynamic"
        if kind.startswith("os."):
            kinds["os-family"] += 1
        else:
            kinds[kind.split(".", 1)[1]] += 1
        argv_classes[argv_class] += 1
    return dict(kinds), dict(argv_classes)


_FORBIDDEN_NETWORK_MODULES = {
    "aiohttp", "ftplib", "http.client", "http.server", "requests",
    "smtplib", "telnetlib", "urllib.request", "urllib3", "websockets",
}


# HARBOR-SPEED-R-C: the per-file census is a pure function of a file's
# bytes and of this module's analysis, so it is memoised per process under
# a CONTENT key: (relative path, sha256 of the bytes read on THIS call, the
# identity of every analysis callable, the forbidden-module vocabulary). A
# file edited, planted or removed between two walks is re-read every walk,
# so a changed byte is a new key and can never be served a stale census; a
# re-bound analysis function is a new key too. Entries are frozen, and each
# walk builds fresh containers, so no caller can mutate the memo. Measured
# at harbor main 1fa68e48: test_truth_pin walked floati/ eight times per
# module run (~2 s each, all ast.parse + ast.walk), 25.6 s in the module.
_FILE_CENSUS_MEMO: dict[
    tuple[object, ...], tuple[bool, frozenset[str], frozenset[tuple[str, str]]]
] = {}


def _census_analysis_identity() -> tuple[object, ...]:
    return (
        _network_subprocess_findings,
        _egress_call_resolver,
        _argv_string_literals,
        _network_subprocess_signature,
        _argv_expression,
        _scope_assignments,
        _top_level_assignments,
        _literal_python_programs,
        _literal_string,
        _TOKEN_PATTERN,
        frozenset(_NETWORK_SUBPROCESS_METHODS),
        frozenset(_OS_EGRESS_FUNCS),
        frozenset(_NETWORK_TOOL_BASENAMES),
        frozenset(_NETWORK_GIT_VERBS),
        frozenset(_FORBIDDEN_NETWORK_MODULES),
    )


def _file_census(
    relative: str, source: bytes
) -> tuple[bool, frozenset[str], frozenset[tuple[str, str]]]:
    """(imports socket, forbidden modules imported, network subprocess sites)."""

    key = (
        relative,
        hashlib.sha256(source).hexdigest(),
        _census_analysis_identity(),
    )
    cached = _FILE_CENSUS_MEMO.get(key)
    if cached is not None:
        return cached
    # Path.read_text decodes with universal newlines; do exactly that here.
    text = source.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    tree = _parsed_source(Path(relative), text)
    imports_socket = False
    network_imports: set[str] = set()
    programs = {"": tree, **{label: ast.parse(source) for label, source
                             in _literal_python_programs(tree).items()}}
    findings = set()
    for label, program in programs.items():
        for node in ast.walk(program):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "socket":
                    imports_socket = True
                if name in _FORBIDDEN_NETWORK_MODULES:
                    network_imports.add(name)
        for owner, signature in _network_subprocess_findings(program):
            findings.add((f"{label}:{owner}" if label else owner, signature))
    census = (
        imports_socket,
        frozenset(network_imports),
        frozenset(findings),
    )
    _FILE_CENSUS_MEMO[key] = census
    return census


def measure_network_surface(
    floati_root: Path,
) -> tuple[set[str], dict[str, set[str]], set[tuple[str, str, str]]]:
    """The census test_network_capable_imports_and_subprocesses_are_fully_counted walks.

    One walker. TRUTH-PIN-1 classifies this population; it does not walk again.
    Paths are repo-relative from floati_root's parent. Every call re-walks
    the tree and re-reads every file; only the per-file analysis of bytes
    already analysed is reused (see ``_FILE_CENSUS_MEMO``).
    """

    base = floati_root.resolve().parent
    seen_socket_imports: set[str] = set()
    seen_network_imports: dict[str, set[str]] = {}
    seen_network_subprocesses: set[tuple[str, str, str]] = set()
    for path in sorted(floati_root.rglob("*.py")):
        relative = path.resolve().relative_to(base).as_posix()
        imports_socket, network_imports, file_findings = _file_census(
            relative, path.read_bytes()
        )
        if imports_socket:
            seen_socket_imports.add(relative)
        if network_imports:
            seen_network_imports[relative] = set(network_imports)
        for function, signature in file_findings:
            seen_network_subprocesses.add((relative, function, signature))
    return seen_socket_imports, seen_network_imports, seen_network_subprocesses


def counted_outbound_paths(
    surface: tuple[set[str], dict[str, set[str]], set[tuple[str, str, str]]],
) -> tuple[tuple[str, str], ...]:
    """Classify the fence census into the counted outbound paths the pages claim.

    Loopback clients: adapter socket imports that are not a host.
    HTTPS: each forbidden-module import the census already named.
    Typed subprocess: each network-shaped subprocess the census already named.
    """

    sockets, net_imports, net_subs = surface
    paths: list[tuple[str, str]] = []
    for relative in sorted(sockets):
        if relative.startswith("floati/adapters/") and not relative.endswith("_host.py"):
            paths.append((relative, "target_bound_arm"))
    for relative, modules in sorted(net_imports.items()):
        for module in sorted(modules):
            paths.append((f"{relative}:{module}", "channel_bound_consent"))
    for relative, function, signature in sorted(net_subs):
        paths.append((f"{relative}:{function}:{signature}", "typed_no_receipt"))
    return tuple(paths)


class WholeProductNoListenerFenceTests(unittest.TestCase):
    """Pin the audited local-only surface named by docs/TRUTH-GUARANTEES.md."""

    repository_root = Path(".")

    def test_network_capable_imports_and_subprocesses_are_fully_counted(self) -> None:
        allowed_socket_imports = {
            "floati/adapters/herdr.py",
            "floati/adapters/herdr_host.py",
            "floati/adapters/t3.py",
            "floati/claude_inbox.py",
            "floati/cross_lan.py",
            "floati/effect_reconciliation_exec.py",
            "floati/sequencer.py",
            "floati/sequencer_scale.py",
            "floati/worker_bootstrap.py",
            "floati/worker_bootstrap_protocol.py",
            "floati/worker_exec.py",
            "floati/workers.py",
        }
        allowed_network_imports = {
            "floati/update_transport.py": {"http.client"},
        }
        allowed_network_subprocesses = {
            ("floati/gh_process.py", "read_github_issue", "gh issue view"),
        }
        seen_socket_imports, seen_network_imports, seen_network_subprocesses = (
            measure_network_surface(self.repository_root / "floati")
        )
        violations: list[str] = []
        for relative in sorted(seen_socket_imports):
            if relative not in allowed_socket_imports:
                violations.append(f"{relative}:socket")
        for relative, modules in sorted(seen_network_imports.items()):
            allowed = allowed_network_imports.get(relative, set())
            for name in sorted(modules):
                if name not in allowed:
                    violations.append(f"{relative}:{name}")
        for observation in sorted(seen_network_subprocesses):
            if observation not in allowed_network_subprocesses:
                violations.append(":".join(observation))
        self.assertEqual(allowed_socket_imports, seen_socket_imports)
        self.assertEqual(allowed_network_imports, seen_network_imports)
        self.assertEqual(allowed_network_subprocesses, seen_network_subprocesses)
        self.assertEqual([], violations)

    def test_subprocess_population_is_pinned(self) -> None:
        """NET-FENCE-1: the whole population the fence must cover, pinned.

        Measured 2026-09-05 by AST over floati/** (census:
        docs/evidence/net-fence-1-population-2026-09-05.md). A new call
        site fails this pin by name, so every egress-shaped edit crosses
        the consent gate instead of slipping past a stale census.

        LANES-1 union on 963a11f3: two local Git subprocess.run sites
        add two mixed argv calls; measured run 40 and mixed 30.
        """

        kinds: dict[str, int] = collections.Counter()
        argv_classes: dict[str, int] = collections.Counter()
        for path in sorted((self.repository_root / "floati").rglob("*.py")):
            tree = _parse_file(path)
            path_kinds, path_argv = _subprocess_population(tree)
            kinds.update(path_kinds)
            argv_classes.update(path_argv)
        self.assertEqual(
            # WD-2 Am.2 (landing F2, 2026-09-11): floati/uninstall.py gained one
            # subprocess.run site — the proven-stopped supervisor removal talks to
            # launchctl before it unlinks; measured 41 at the composition.
            # SEAT-ONBOARD-1 (2026-10-01): three seat_onboarding.run sites
            # invoke fixed local Git, a declared build script, and the explicit
            # Codex child harness. See the population doc's classified AST cut.
            # ROLLBACK-HOOK-1 (2026-10-04): floati/deploy.py
            # _is_reverse_transition gained one subprocess.run — local Git
            # merge-base --is-ancestor classifies a reverse install. Measured
            # run 44. Network allowlists unchanged: the argv is fixed Git.
            # Harbor train 14 (seat 25): floati/cursor_workers.py runs fixed /bin/ps
            # -eo pid=,ppid=,command= (F2-REMOTE-CONTROL-COEXIST-1) and
            # floati/row_tree_retire.py runs fixed /usr/bin/git with a fixed git
            # environment (F2-ROW-TREE-RETIRE-1); both local, measured run 46.
            # Network allowlists unchanged.
            # F2-SEAT-TEARDOWN-1 adds one bounded owned metadata child:
            # fixed local Git, ps and lsof; no outbound route is added.
            # ADHOC-STAGE1 Task1: adhoc_journal._git adds one fixed local
            # Git inspection site (branch/ls-tree/cat-file), through the
            # hardened fixed_git_command/environment. No network allowlist moves.
            # Task2: BorrowedWorkspace._git adds one parent-only fixed Git
            # site; source-derived count, awaiting the registered full.
            # Task2 fix1 adds a fixed parent-owned Git transaction Popen;
            # preparation/identity/commit are held by that exact child.
            # Runtime repair: these two owners now launch isolated Python,
            # then Git by exec. Direct kinds/shapes do not change; the nested
            # execution surface has its own pin below.
            # Native persistent Codex/Claude/Pi add three Popen owners;
            # ad-hoc parent finalization adds one fixed local Git run owner.
            # Parallel selftest: floati/selftest.py runs the local parallel
            # runner (scripts/test-parallel.py) when a worker count is set.
            # The argv is the interpreter, that script, and the worker/output
            # paths. No outbound route. Measured run 47.
            # User-level Cursor stop hook: floati/cursor_user_hook.py runs the
            # seat launcher named by the workspace marker. The argv is that
            # local launcher plus wake wait. No outbound route. Measured run 48.
            # FLEET-ADHOC-THREADS-1: measured merged AST census, both owners kept.
            # Claude inbox: one installed-client --version child, closed env.
            # Train 1122b: bound records add one local Git run, work handoffs one
            # observed local command, checkout liveness one Git run and the quiet
            # window two local process probes (ps, sysctl); run 56 -> 61.
            # NEEDS-YOU-OBSERVED-1: floati/needs_observed.py starts the explicitly
            # configured orchestrator CLI for one bounded read; Popen 16 -> 17.
            # Ask replay adds one fixed local Git inspection with transports disabled.
            # T3 dispatch gate adds one bounded local Floati check child.
            # Cross opt-in adds _PlatformStore._run. KeychainStore.probe
            # and SecretServiceStore.probe select security and secret-tool
            # for the ruled attended enable and disable store operations.
            {"Popen": 18, "os-family": 3, "run": 63, "run_child": 1},
            dict(kinds),
            "the subprocess-family population moved; re-measure and re-pin",
        )
        self.assertEqual(
            # WD-2 Am.2 (landing F2): floati/uninstall.py, one dynamic argv site
            # SEAT-ONBOARD-1: fixed Git and Codex argv are mixed; the declared
            # build script's argv is dynamic. Network allowlists stay unchanged.
            # ROLLBACK-HOOK-1: the reverse-transition Git argv is mixed
            # (literal merge-base flags plus name nodes); measured mixed 32.
            # Harbor train 14: the two local sites above are mixed (measured 34).
            # FL-2: scrub's two literal Git argv lists now expand the
            # transient maintenance tuple. Measured mixed36/dynamic25;
            # process kinds and network allowlists are unchanged.
            # Static composition: main's mixed36 plus Task1's one mixed
            # fixed_git_command site gives37; dynamic25 is unchanged.
            # This expectation awaits the registered full's measured census.
            # Task2's subprocess.run(command) has a Name argv, so this
            # unchanged classifier adds one dynamic site, not one mixed site.
            # Its assignment resolves to hardened fixed_git_command; no
            # network allowlist or classification predicate changes.
            # Fix1's transaction also passes the Name command as argv:
            # one more dynamic site, still fixed local Git, pending full proof.
            # Parallel selftest: the local runner argv is mixed (literal
            # mode flags plus the script and output paths). Measured mixed 37.
            # User-level Cursor stop hook: the launcher argv is a function
            # result, so the new site is dynamic. Measured dynamic 26.
            # FLEET-ADHOC-THREADS-1: measured merged AST census, no new egress.
            # Train 1122b: the work handoff argv is dynamic (31); bound
            # records, checkout liveness and the quiet window add one mixed
            # each (49); the quiet window's other argv is one literal list.
            # NEEDS-YOU-OBSERVED-1: the configured orchestrator argv is dynamic (32).
            # The ask replay site uses argv returned by fixed_git_command.
            # T3's argv selects the confirmed launcher or local recovery Python.
            # The cross store argv uses its selected helper and operation.
            {"all-literal-list": 1, "dynamic": 35, "mixed": 49},
            dict(argv_classes),
            "the argv-shape distribution moved; re-measure and re-pin",
        )

    def test_literal_python_execution_population_is_pinned_separately(self) -> None:
        observed = {}
        for path in sorted((self.repository_root / "floati").rglob("*.py")):
            tree = _parse_file(path)
            for label, source in _literal_python_programs(tree).items():
                kinds, shapes = _subprocess_population(ast.parse(source))
                if kinds:
                    observed[(path.relative_to(self.repository_root).as_posix(), label)] = (kinds, shapes)
        # The enclosing run/Popen sites are Python, already in the unchanged
        # direct population. This is the real Git exec inside their prelude.
        self.assertEqual({
            ("floati/adhoc_workspace.py", "_BORROWED_GIT_PRELUDE"):
                ({"os-family": 1}, {"string-const": 1}),
        }, observed)

    def test_built_argv_egress_is_enumerated_not_blind(self) -> None:
        """NET-FENCE-1 Am.1: built argv is a NAMED, counted class.

        The read refused the tip because argv built by concatenation or
        shlex.split was invisible to the fence AND absent from the census
        — an unstated blind spot. Measured 2026-09-05 21:1xZ by AST over
        floati/ AND scripts/: seven egress sites in five files, none
        network-shaped (five Path builds invoking the floati CLI from
        capture/export scripts, one `sha + "^{commit}"` inside a local
        `git cat-file`, one f-string commit message). A new site fails
        this pin by file name.

        Am.2: the pin is projection-aware, exactly like the H1-F1 reader
        map. The exporter's own policy excludes
        scripts/prepare_public_export.py, so a pin enumerating it failed
        inside a projection, where the file does not ship. Both the
        pinned and the derived sets are classified through the exporter
        and compared as the INCLUDED half: in the harbor the excluded
        file drops from both sides alike, and in a policy-less projection
        classification is the identity and every carried file is an
        included file, so the same comparison holds there.
        """

        measured = _built_argv_egress_files(
            [self.repository_root / "floati", self.repository_root / "scripts"],
            relative_to=self.repository_root,
        )
        pinned = {
            "floati/cli.py",
            "scripts/agent_bootstrap.py",
            "scripts/capture-demo-assets.py",
            "scripts/capture-shot1-locf1.py",
            "scripts/capture-tui-moments.py",
            # Exact local Git artifact readback in the bounded issue-16 drill.
            "scripts/measure_opencode_issue16_worker.py",
            "scripts/loaded-wallclock-abrupt-diagnostic.py",
            "scripts/loaded-wallclock-proof.py",
            'scripts/measure_zcode_conformance.py',
            'floati/release.py',
        }
        included = frozenset(
            classify_inventory(sorted(measured | pinned), root=self.repository_root)
        )

        self.assertEqual(
            {path for path in pinned if path in included},
            {path for path in measured if path in included},
            "a built-argv egress site appeared or vanished on the shipped "
            "surface; name it in this pin",
        )

    def test_exported_product_fences_run_in_a_real_adapted_projection(self) -> None:
        """PROJ-LEG-3/PROJ-PIN-1: both pins must work without export policy.

        Keep the six actual product fences on strictly adapted bytes. Raw
        source classification, the private twin and constructed controls
        remain ordinary tests; the unadapted policy-less tree is retired.
        """
        if not export_policy_is_present():
            self.skipTest("no export policy in this tree; classification is identity")
        with _adapted_fence_projection(prepare_git=False) as projection:
            relative = "tests/test_no_listener_fence.py"
            if (projection / relative).read_bytes() != (REPOSITORY_ROOT / relative).read_bytes():
                _assert_projected_fences(self, _run_projected_fences(projection))
                return
            _assert_in_process_fences(self, projection)
            # A clean source tree must not hide an unexpected import planted
            # only in the supplied projection, including after a warm census.
            planted = projection / "floati/projection_probe.py"
            planted.write_text("import socket\n", encoding="utf-8")
            try:
                with self.assertRaisesRegex(AssertionError, "projection_probe"):
                    _rooted_fence(projection, PROJECTED_FENCE_IDS[0]).debug()
            finally:
                planted.unlink()

    def test_planted_join_and_percent_sites_are_enumerated(self) -> None:
        """NET-FENCE-1-F1: a `.join`-built egress cannot land silently.

        The Am.1 read found `"".join(["cu", "rl"])` returned `[]` — the
        census enumerated BinOp and f-string builds but not `str.join`.
        Planted fixtures: the `.join` site must be enumerated (RED until
        the census learns it), and the `%`-built site must stay
        enumerated (BinOp already covers it — pinned explicitly).
        """

        import tempfile

        from tests.temp_roots import REAL_TEMP_ROOT

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            # One shape per file: a file carrying ANY enumerated shape
            # lands in the set, so co-located fixtures would mask each
            # other exactly the way the whole-file pin would want to
            # fail by shape.
            join_site = Path(temporary) / "planted_join_egress.py"
            join_site.write_text(
                "import subprocess\n"
                "def sync(url):\n"
                "    subprocess.run(''.join(['cu', 'rl', ' ', url]), shell=True)\n",
                encoding="utf-8",
            )
            percent_site = Path(temporary) / "planted_percent_egress.py"
            percent_site.write_text(
                "import subprocess\n"
                "def resync(url):\n"
                "    subprocess.run('git fetch %s' % url, shell=True)\n",
                encoding="utf-8",
            )
            enumerated = {
                Path(path).relative_to(temporary).as_posix()
                for path in _built_argv_egress_files([Path(temporary)])
            }
            self.assertEqual(
                {"planted_join_egress.py", "planted_percent_egress.py"},
                enumerated,
            )

    def test_planted_second_site_in_one_file_is_visible_per_site(self) -> None:
        """NET-FENCE-1-F3: the census must see SITES, not files.

        The file-granularity pin dedupes co-located egress sites: a second
        network-capable call landing in an already-pinned file changes
        nothing the pin compares, so it is invisible to the harbor pin
        and to the private pin alike. Two planted sites in ONE file: the
        file census reports one file (the blind spot, asserted as
        documentation), the site census reports both.
        """

        import tempfile

        from tests.temp_roots import REAL_TEMP_ROOT

        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            planted = Path(temporary) / "planted_two_sites.py"
            planted.write_text(
                "import subprocess\n"
                "def one(url):\n"
                "    subprocess.run(''.join(['cu', 'rl', ' ', url]), shell=True)\n"
                "def two(url):\n"
                "    subprocess.run(f'git fetch {url}', shell=True)\n",
                encoding="utf-8",
            )
            files = {
                Path(path).relative_to(temporary).as_posix()
                for path in _built_argv_egress_files([Path(temporary)])
            }
            sites = {
                (Path(path).relative_to(temporary).as_posix(), digest)
                for path, digest in _built_argv_egress_sites([Path(temporary)])
            }
            self.assertEqual({"planted_two_sites.py"}, files)
            self.assertEqual(2, len(sites), sites)
            self.assertEqual(
                {"planted_two_sites.py"}, {path for path, _ in sites}
            )

    def test_built_argv_egress_sites_are_pinned(self) -> None:
        """NET-FENCE-1-F3: the pin is per SITE, not per file.

        Measured 2026-09-06 at main 2045b2aa: seven built-argv egress
        sites across five files, and two of the five files carry two
        sites each -- exactly the co-location a file-granularity pin
        cannot see. Identity is (path, site digest): the digest hashes
        the call's callable and its argv expression's AST, so an edited
        argv reds this pin while a reformatted one does not, and a new
        site reds it by name. Projection-aware like Am.2: both sides are
        classified through the exporter and compared as the included
        half.
        """

        measured = _built_argv_egress_sites(
            [self.repository_root / "floati", self.repository_root / "scripts"],
            relative_to=self.repository_root,
        )
        pinned = {
            ("floati/cli.py", "4a4ea4b2f1ec4f5b"),
            # Development-only pip install of the worktree's locked wheels.
            ("scripts/agent_bootstrap.py", "4521bbb290104625"),
            ("scripts/capture-demo-assets.py", "4053d11281a3c0e1"),
            ("scripts/capture-demo-assets.py", "70cb7fa74220ea0a"),
            ("scripts/capture-shot1-locf1.py", "ebeffdefb0d69c7d"),
            ("scripts/capture-tui-moments.py", "70cb7fa74220ea0a"),
            ("scripts/measure_opencode_issue16_worker.py", "9c2c277f2661cf9c"),
            ("scripts/loaded-wallclock-abrupt-diagnostic.py", "7ab765f2a64b7158"),
            ('scripts/measure_zcode_conformance.py', '9c2c277f2661cf9c'),
            ("scripts/capture-shot1-locf1.py", "a5fc987bd4c0c4d3"),
            ('floati/release.py', '2cb46aaf07917675'),
            ('floati/release.py', '7d52939abef7e52c'),
            ("scripts/loaded-wallclock-proof.py", "c5ae537075da4343"),
        }
        included_paths = frozenset(
            classify_inventory(
                sorted({path for path, _ in measured} | {path for path, _ in pinned}),
                root=self.repository_root,
            )
        )

        self.assertEqual(
            {site for site in pinned if site[0] in included_paths},
            {site for site in measured if site[0] in included_paths},
            "a built-argv egress site appeared, vanished, or changed shape; "
            "name it in this pin",
        )

    def test_site_pin_predicts_a_raw_copy_classified_projection(self) -> None:
        """The site pin must pass on classified raw bytes, not only adapted ones.

        Classify the tracked inventory, copy those files unchanged, and
        compute the site census there. Classification reads no Git state.
        """
        if not export_policy_is_present():
            self.skipTest("no export policy in this tree; classification is identity")

        included = classify_inventory(tracked_files(REPOSITORY_ROOT), root=REPOSITORY_ROOT)
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            projection = Path(temporary) / "projection"
            projection.mkdir()
            for relative in included:
                target = projection / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPOSITORY_ROOT / relative, target)

            _assert_in_process_fences(
                self, projection,
                ("tests.test_no_listener_fence.WholeProductNoListenerFenceTests."
                 "test_built_argv_egress_sites_are_pinned",),
            )

    def test_bind_and_listen_calls_exist_only_in_the_ruled_af_unix_supervisor(self) -> None:
        calls: list[str] = []
        pattern = re.compile(r"\.(?:bind|listen)\s*\(")
        for path in sorted((self.repository_root / "floati").rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(self.repository_root).as_posix()
            if relative == "floati/adhoc_execution.py":
                # These two binds are admission/capability ledger owners,
                # not sockets. Verify the exact imports, receiver construction
                # and owning function before removing only those call sites.
                tree = _parsed_source(path, source)
                imports = {(node.module, alias.name, alias.asname)
                           for node in tree.body if isinstance(node, ast.ImportFrom)
                           for alias in node.names}
                self.assertIn(("admission", "AdmissionBinder", None), imports)
                self.assertIn(("capability_binding", "CapabilityBinder", None), imports)
                owner = next(node for node in tree.body
                             if isinstance(node, ast.FunctionDef) and node.name == "_prepare_binding")
                assignments = [node for node in ast.walk(owner)
                               if isinstance(node, ast.Assign)
                               and any(isinstance(target, ast.Name) and target.id == "binder"
                                       for target in node.targets)]
                self.assertEqual(1, len(assignments))
                self.assertIsInstance(assignments[0].value, ast.Call)
                self.assertEqual("CapabilityBinder", ast.unparse(assignments[0].value.func))
                receiver_names = {"binder", "AdmissionBinder", "CapabilityBinder"}
                receiver_stores = [node for node in ast.walk(tree)
                                   if isinstance(node, ast.Name)
                                   and node.id in receiver_names
                                   and isinstance(node.ctx, (ast.Store, ast.Del))]
                self.assertEqual(assignments[0].targets, receiver_stores,
                                 "ledger receiver must retain its reviewed owner")
                mutations = [node for node in ast.walk(tree)
                             if isinstance(node, (ast.Attribute, ast.Subscript))
                             and isinstance(node.ctx, (ast.Store, ast.Del))
                             and any(isinstance(child, ast.Name) and child.id in receiver_names
                                     for child in ast.walk(node))]
                self.assertEqual([], mutations, "ledger receiver must not be mutated")
                ledger_binds = [node for node in ast.walk(owner)
                                if isinstance(node, ast.Call)
                                and ast.unparse(node.func) in {"AdmissionBinder.bind", "binder.bind"}]
                self.assertEqual({"AdmissionBinder.bind", "binder.bind"},
                                 {ast.unparse(node.func) for node in ledger_binds})
                self.assertEqual(2, len(ledger_binds))
                lines = source.splitlines(keepends=True)
                for node in ledger_binds:
                    # Remove the method token only; any additional bind/listen
                    # anywhere in this file remains covered by the fence.
                    line = lines[node.func.end_lineno - 1]
                    lines[node.func.end_lineno - 1] = (
                        line[:node.func.end_col_offset - 4] + "LEDGER_BIND"
                        + line[node.func.end_col_offset:])
                source = "".join(lines)
            if relative == "floati/cross_lan.py":
                # X1: only the cross listener is present in this tree. Pin
                # both calls inside its owner and still scan the whole file.
                tree = _parsed_source(path, source)
                transport, = (node for node in tree.body
                              if isinstance(node, ast.ClassDef) and node.name == "LanTransport")
                listener, = (node for node in transport.body
                             if isinstance(node, ast.FunctionDef) and node.name == "listen")
                socket_calls = [ast.unparse(node) for node in ast.walk(listener)
                                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                                and node.func.attr in {"bind", "listen"}]
                self.assertCountEqual(["stream.bind(target)", "stream.listen(16)"], socket_calls)
                self.assertEqual(2, len(pattern.findall(source)))
            elif pattern.search(source):
                calls.append(relative)
        self.assertEqual(["floati/sequencer.py"], calls)
        source = (self.repository_root / "floati/sequencer.py").read_text(encoding="utf-8")
        self.assertIn("socket.AF_UNIX", source)

    def test_ledger_bind_exception_rejects_receiver_substitution(self) -> None:
        original = Path.read_text
        for substitution in (
            "AdmissionBinder = socket_object",
            "CapabilityBinder = socket_object",
            "binder: object = socket_object",
            "(binder := socket_object)",
            "binder.bind = socket_object.bind",
        ):
            def changed(path, *args, **kwargs):
                source = original(path, *args, **kwargs)
                if path.as_posix() == "floati/adhoc_execution.py":
                    source = source.replace(
                        "    run = ledger.project().run(run_id)\n    snapshot =",
                        "    " + substitution + "\n    run = ledger.project().run(run_id)\n    snapshot =")
                return source
            with self.subTest(substitution=substitution):
                with mock.patch.object(Path, "read_text", changed):
                    with self.assertRaises(AssertionError):
                        self.test_bind_and_listen_calls_exist_only_in_the_ruled_af_unix_supervisor()

    def test_mcp_module_imports_only_local_code_and_non_network_stdlib(self) -> None:
        """Catches an stdio server acquiring a listener, URL client, or dependency."""

        path = self.repository_root / "floati/mcp.py"
        tree = _parse_file(path)
        allowed_stdlib = {
            "__future__",
            "argparse",
            "contextlib",
            "io",
            "json",
            "pathlib",
            "sys",
            "time",
            "typing",
        }
        absolute_imports: set[str] = set()
        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                absolute_imports.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                absolute_imports.add(node.module.split(".")[0])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"bind", "connect", "listen", "open_connection"}:
                    violations.append(node.func.attr)
        self.assertLessEqual(absolute_imports, allowed_stdlib)
        self.assertEqual([], violations)


class LiteralPythonEgressTests(unittest.TestCase):
    @staticmethod
    def source(program):
        # This is the tuple-return producer shape used by the real launcher.
        return ("import subprocess,sys\nCHILD = " + repr(program) + "\n"
                "def command_for_child():\n"
                "    command = [sys.executable, '-I', '-S', '-B', '-c', CHILD]\n"
                "    return command, {}\n"
                "def launch():\n"
                "    command, environment = command_for_child()\n"
                "    subprocess.run(command, env=environment)\n")

    def test_nested_import_and_network_exec_cannot_hide_in_literal(self):
        program = ("import os,socket,urllib.request\n"
                   "os.execve('/usr/bin/git', ['git', 'fetch'], {})\n")
        source = self.source(program)
        self.assertEqual({"CHILD": program}, _literal_python_programs(ast.parse(source)))
        socket, imports, findings = _file_census("floati/planted_child.py", source.encode())
        self.assertTrue(socket)
        self.assertEqual({"urllib.request"}, set(imports))
        self.assertEqual({"launch", "CHILD:<module>"},
                         {owner for owner, _signature in findings})
        self.assertEqual({"git fetch"}, {signature for _owner, signature in findings})

    def test_second_nested_exec_moves_population_without_relabeling_parent(self):
        program = "import os\nos.execve('/usr/bin/git', ['git', 'status'], {})\n"
        parent = ast.parse(self.source(program))
        self.assertEqual(({"run": 1}, {"dynamic": 1}), _subprocess_population(parent))
        first = _literal_python_programs(parent)["CHILD"]
        self.assertEqual(({"os-family": 1}, {"string-const": 1}),
                         _subprocess_population(ast.parse(first)))
        second = _literal_python_programs(ast.parse(self.source(program + program)))["CHILD"]
        self.assertEqual(({"os-family": 2}, {"string-const": 2}),
                         _subprocess_population(ast.parse(second)))

    def test_unused_literal_does_not_fabricate_an_execution_site(self):
        source = "CHILD = \"import os; os.execve('/usr/bin/git', ['git'], {})\"\n"
        self.assertEqual({}, _literal_python_programs(ast.parse(source)))


class ConstructedSubprocessEgressTests(unittest.TestCase):
    """NET-FENCE-1 (issue #25): constructed egress cannot pass the fence.

    Each test drives the fence's DETECTION over a constructed module —
    never a helper with a pre-built signature — and requires the egress
    to be caught and named. The consent gate itself stays the explicit
    whole-product allowlist above.
    """

    def _findings(self, source: str) -> set[tuple[str, str]]:
        return _network_subprocess_findings(ast.parse(source))

    def test_owned_child_imports_cannot_hide_network_argv_or_nested_programs(self) -> None:
        program = "import socket\n"
        for helper in ("owned_child", "run_child"):
            forms = []
            for module in ("fixture_finalizer", "scripts.fixture_finalizer"):
                forms.extend((
                    (f"from {module} import {helper}", helper),
                    (f"from {module} import {helper} as launch", "launch"),
                    (f"import {module}", module + "." + helper),
                    (f"import {module} as owner", "owner." + helper),
                ))
            forms.append(("from scripts import fixture_finalizer as owner", "owner." + helper))
            for imported, call in forms:
                with self.subTest(imported=imported):
                    tree = ast.parse(
                        imported + "\nimport sys\nCHILD = " + repr(program) + "\n"
                        "def sync(url):\n"
                        f"    {call}(['cu' + 'rl', url])\n"
                        f"    {call}([sys.executable, '-c', CHILD])\n"
                    )
                    self.assertEqual({("sync", "curl")}, _network_subprocess_findings(tree))
                    self.assertEqual({"CHILD": program}, _literal_python_programs(tree))
                    self.assertTrue(_built_argv_tree_egress_sites(tree))
                    self.assertEqual(({helper: 2}, {"mixed": 2}), _subprocess_population(tree))

    def test_shell_string_curl_is_caught(self) -> None:
        findings = self._findings(
            "import subprocess\n"
            "def sync():\n"
            "    subprocess.run('curl -fsSL https://example.invalid/x', shell=True)\n"
        )
        self.assertEqual({("sync", "curl")}, findings)

    def test_pip_module_invocation_is_caught(self) -> None:
        findings = self._findings(
            "import subprocess, sys\n"
            "def sync():\n"
            "    subprocess.run([sys.executable, '-m', 'pip', 'install', 'x'])\n"
        )
        self.assertEqual({("sync", "pip")}, findings)

    def test_git_remote_helper_is_caught(self) -> None:
        findings = self._findings(
            "import subprocess\n"
            "def sync(url):\n"
            "    subprocess.run(['git-remote-https', 'origin', url])\n"
        )
        self.assertEqual({("sync", "git-remote-https")}, findings)

    def test_scope_variable_argv_is_caught(self) -> None:
        findings = self._findings(
            "import subprocess\n"
            "def sync(url):\n"
            "    argv = ['curl', '-fsSL', url]\n"
            "    subprocess.run(argv)\n"
        )
        self.assertEqual({("sync", "curl")}, findings)

    def test_os_system_command_is_caught(self) -> None:
        findings = self._findings(
            "import os\n"
            "def sync():\n"
            "    os.system('curl -fsSL https://example.invalid/x')\n"
        )
        self.assertEqual({("sync", "curl")}, findings)


class BuiltArgvEgressTests(unittest.TestCase):
    """NET-FENCE-1 Am.1: argv BUILT from literals cannot hide the tool.

    The read's finding: a tool name split across concatenation, or a git
    verb carried inside one longer literal (shlex.split, f-string,
    .format, shell string), decides nothing today. Literal BinOp and
    shlex.split of a literal are decidable — these tests require the
    scanner to decide them, through the findings walk, never a helper.
    """

    def _findings(self, source: str) -> set[tuple[str, str]]:
        return _network_subprocess_findings(ast.parse(source))

    def test_concatenated_tool_name_is_caught(self) -> None:
        findings = self._findings(
            "import subprocess\n"
            "def sync(url):\n"
            "    subprocess.run(['cu' + 'rl', '-fsSL', url])\n"
        )
        self.assertEqual({("sync", "curl")}, findings)

    def test_shlex_split_literal_is_caught(self) -> None:
        findings = self._findings(
            "import shlex, subprocess\n"
            "def sync():\n"
            "    subprocess.run(shlex.split('git push origin'))\n"
        )
        self.assertEqual({("sync", "git push")}, findings)

    def test_f_string_verb_is_caught(self) -> None:
        findings = self._findings(
            "import subprocess\n"
            "def sync(remote):\n"
            "    subprocess.run(f'git push {remote}', shell=True)\n"
        )
        self.assertEqual({("sync", "git push")}, findings)

    def test_format_template_verb_is_caught(self) -> None:
        findings = self._findings(
            "import subprocess\n"
            "def sync(path):\n"
            "    subprocess.run('git -C {} fetch --all'.format(path), shell=True)\n"
        )
        self.assertEqual({("sync", "git fetch")}, findings)


class ExcludedHalfPrivateTwinTests(unittest.TestCase):
    """NET-FENCE-1-F2: a projection-aware pin keeps a private_only twin.

    Am.2 compares the INCLUDED half on both sides alike, which went quiet
    on exactly the files the policy hides: a built-argv site planted in
    scripts/prepare_public_export.py was invisible to the pin. The twin
    pins the EXCLUDED half - and the Am.1 read refused the first twin
    because its pin artifact lived under tests/, where the classifier
    says INCLUDE, so the private half leaked its own census into the
    projection. The artifact lives where the classifier already says
    private_only (.github/, declared in the policy's own
    private_only_paths), and the twin typed-skips in a policy-less
    projection, where nothing is excluded.
    """

    def test_the_excluded_half_is_pinned_by_a_private_twin(self) -> None:
        from tests.export_inventory import (
            classify_inventory_excluded,
            export_policy_is_present,
        )

        if not export_policy_is_present(REPOSITORY_ROOT):
            self.skipTest(
                "export_policy_absent: a policy-less projection carries no "
                "excluded half, so the private half is a typed skip"
            )
        pin_relative = ".github/built-argv-private-pin.v0.txt"
        # The artifact itself must be private: a private pin that the
        # exporter would publish leaks the excluded half's census.
        self.assertEqual(
            [pin_relative],
            list(classify_inventory_excluded([pin_relative], root=REPOSITORY_ROOT)),
            "the private pin artifact must classify private_only, never "
            "INCLUDE - it would land in the projection and leak the census",
        )
        pin_path = REPOSITORY_ROOT / pin_relative
        self.assertTrue(
            pin_path.is_file(),
            "no private twin artifact: " + pin_relative + " is absent, so a "
            "built-argv site planted in an excluded file "
            "(scripts/prepare_public_export.py) is invisible to the fence",
        )
        measured = _built_argv_egress_files([Path("floati"), Path("scripts")])
        pinned = {
            line.strip()
            for line in pin_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        population = sorted(set(measured) | pinned)
        excluded = frozenset(
            classify_inventory_excluded(population, root=REPOSITORY_ROOT)
        )
        self.assertEqual(
            {path for path in pinned if path in excluded},
            {path for path in measured if path in excluded},
            "a built-argv egress site appeared or vanished in the EXCLUDED "
            "half; name the file in " + pin_relative,
        )


class CensusMemoIsContentKeyedTests(unittest.TestCase):
    """HARBOR-SPEED-R-C: the census memo can never serve a changed file stale."""

    def test_rewritten_planted_and_removed_files_are_re_censused(self) -> None:
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary) / "floati"
            root.mkdir()
            module = root / "module.py"
            module.write_text("import string\n", encoding="utf-8")
            self.assertEqual((set(), {}, set()), measure_network_surface(root))

            # The memo is live: an unchanged re-walk adds no entry.
            entries = len(_FILE_CENSUS_MEMO)
            self.assertEqual((set(), {}, set()), measure_network_surface(root))
            self.assertEqual(entries, len(_FILE_CENSUS_MEMO))

            # Same path, same byte length, same mtime, different bytes: a
            # stat-keyed memo would serve the old census; this one may not.
            before = module.stat()
            module.write_text("import ftplib\n", encoding="utf-8")
            os.utime(module, ns=(before.st_atime_ns, before.st_mtime_ns))
            self.assertEqual(before.st_size, module.stat().st_size)
            self.assertEqual(before.st_mtime_ns, module.stat().st_mtime_ns)
            self.assertEqual(
                (set(), {"floati/module.py": {"ftplib"}}, set()),
                measure_network_surface(root),
            )

            # A planted file is walked; a removed file leaves the census.
            planted = root / "planted.py"
            planted.write_text("import socket\n", encoding="utf-8")
            self.assertEqual(
                {"floati/planted.py"}, measure_network_surface(root)[0]
            )
            planted.unlink()
            self.assertEqual(set(), measure_network_surface(root)[0])

            # Reverting the bytes is served the original census again.
            module.write_text("import string\n", encoding="utf-8")
            self.assertEqual((set(), {}, set()), measure_network_surface(root))

    def test_callers_cannot_mutate_the_memo(self) -> None:
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            root = Path(temporary) / "floati"
            root.mkdir()
            (root / "client.py").write_text("import http.client\n", encoding="utf-8")
            first = measure_network_surface(root)
            first[1]["floati/client.py"].add("requests")
            first[0].add("floati/forged.py")
            self.assertEqual(
                (set(), {"floati/client.py": {"http.client"}}, set()),
                measure_network_surface(root),
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
import collections
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from tests.export_inventory import (
    classify_inventory,
    export_policy_is_present,
    tracked_files,
)
from tests.temp_roots import REAL_TEMP_ROOT


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


_NETWORK_SUBPROCESS_METHODS = frozenset(
    {"Popen", "call", "check_call", "check_output", "run"}
)
_OS_EGRESS_FUNCS = frozenset(
    {
        "system", "popen",
        "exec", "execv", "execve", "execvp", "execvpe",
        "spawn", "spawnv", "spawnve", "spawnvp", "spawnvpe", "posix_spawn",
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
                        visit(candidate.value)
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


def _scope_assignments(scope: ast.AST) -> dict[str, ast.AST]:
    """Single-target name assignments inside one scope (one level deep)."""

    assignments: dict[str, ast.AST] = {}
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            assignments.setdefault(node.targets[0].id, node.value)
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


def _built_argv_egress_sites(roots: list[Path]) -> set[tuple[str, str]]:
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
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                argvs: list[ast.AST] = []
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                ):
                    if (
                        node.func.value.id == "subprocess"
                        and node.func.attr in _NETWORK_SUBPROCESS_METHODS
                    ):
                        argv = _argv_expression(node)
                        if argv is not None:
                            argvs = [argv]
                    elif (
                        node.func.value.id == "os"
                        and node.func.attr in _OS_EGRESS_FUNCS
                    ):
                        argvs = list(node.args)
                if argvs and any(
                    _is_built_argv_node(piece)
                    for argv in argvs
                    for piece in ast.walk(argv)
                ):
                    call = f"{node.func.value.id}.{node.func.attr}"
                    payload = ast.dump(
                        ast.Module(body=list(argvs), type_ignores=[]),
                        annotate_fields=False,
                    )
                    digest = hashlib.sha256(
                        f"{call}|{payload}".encode("utf-8")
                    ).hexdigest()[:16]
                    sites.add((path.as_posix(), digest))
    return sites


def _built_argv_egress_files(roots: list[Path]) -> set[str]:
    """Files holding an egress call site whose argv is a BUILT expression.

    A projection of the site census: file granularity is the blind spot
    NET-FENCE-1-F3 removed, kept only for the census's own
    documentation.
    """

    return {path for path, _ in _built_argv_egress_sites(roots)}


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
    for node in ast.walk(tree):
        egress_args: list[ast.AST] = []
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            if (
                node.func.value.id == "subprocess"
                and node.func.attr in _NETWORK_SUBPROCESS_METHODS
            ):
                argv = _argv_expression(node)
                if argv is not None:
                    egress_args = [argv]
            elif (
                node.func.value.id == "os"
                and node.func.attr in _OS_EGRESS_FUNCS
            ):
                egress_args = list(node.args)
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
    for node in ast.walk(tree):
        kind = None
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            if (
                node.func.value.id == "subprocess"
                and node.func.attr in _NETWORK_SUBPROCESS_METHODS
            ):
                kind = "subprocess." + node.func.attr
            elif (
                node.func.value.id == "os"
                and node.func.attr in _OS_EGRESS_FUNCS
            ):
                kind = "os." + node.func.attr
        if kind is None:
            continue
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


class WholeProductNoListenerFenceTests(unittest.TestCase):
    """Pin the audited local-only surface named by docs/TRUTH-GUARANTEES.md."""

    def test_network_capable_imports_and_subprocesses_are_fully_counted(self) -> None:
        allowed_socket_imports = {
            "floati/adapters/herdr.py",
            "floati/adapters/herdr_host.py",
            "floati/adapters/t3.py",
            "floati/effect_reconciliation_exec.py",
            "floati/sequencer.py",
            "floati/sequencer_scale.py",
            "floati/worker_bootstrap.py",
            "floati/worker_bootstrap_protocol.py",
            "floati/worker_exec.py",
            "floati/workers.py",
        }
        forbidden_modules = {
            "aiohttp", "ftplib", "http.client", "http.server", "requests",
            "smtplib", "telnetlib", "urllib.request", "urllib3", "websockets",
        }
        allowed_network_imports = {
            "floati/update_transport.py": {"http.client"},
        }
        allowed_network_subprocesses = {
            ("floati/gh_process.py", "read_github_issue", "gh issue view"),
        }
        seen_socket_imports = set()
        seen_network_imports: dict[str, set[str]] = {}
        seen_network_subprocesses: set[tuple[str, str, str]] = set()
        violations: list[str] = []
        for path in sorted(Path("floati").rglob("*.py")):
            relative = path.as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
            file_findings = _network_subprocess_findings(tree)
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name == "socket":
                        seen_socket_imports.add(relative)
                        if relative not in allowed_socket_imports:
                            violations.append(f"{relative}:socket")
                    if (
                        name in forbidden_modules
                    ):
                        seen_network_imports.setdefault(relative, set()).add(name)
                        if name not in allowed_network_imports.get(relative, set()):
                            violations.append(f"{relative}:{name}")
            for function, signature in file_findings:
                observation = (relative, function, signature)
                seen_network_subprocesses.add(observation)
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
        for path in sorted(Path("floati").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            path_kinds, path_argv = _subprocess_population(tree)
            kinds.update(path_kinds)
            argv_classes.update(path_argv)
        self.assertEqual(
            {"run": 40, "Popen": 10, "os-family": 3},
            dict(kinds),
            "the subprocess-family population moved; re-measure and re-pin",
        )
        self.assertEqual(
            {"all-literal-list": 2, "mixed": 30, "dynamic": 21},
            dict(argv_classes),
            "the argv-shape distribution moved; re-measure and re-pin",
        )

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

        measured = _built_argv_egress_files([Path("floati"), Path("scripts")])
        pinned = {
            "floati/cli.py",
            "scripts/capture-demo-assets.py",
            "scripts/capture-shot1-locf1.py",
            "scripts/capture-tui-moments.py",
        }
        included = frozenset(
            classify_inventory(sorted(measured | pinned), root=REPOSITORY_ROOT)
        )

        self.assertEqual(
            {path for path in pinned if path in included},
            {path for path in measured if path in included},
            "a built-argv egress site appeared or vanished on the shipped "
            "surface; name it in this pin",
        )

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

        measured = _built_argv_egress_sites([Path("floati"), Path("scripts")])
        pinned = {
            ("floati/cli.py", "4a4ea4b2f1ec4f5b"),
            ("scripts/capture-demo-assets.py", "4053d11281a3c0e1"),
            ("scripts/capture-demo-assets.py", "70cb7fa74220ea0a"),
            ("scripts/capture-shot1-locf1.py", "b2f1e1598e2214e8"),
            ("scripts/capture-shot1-locf1.py", "ebeffdefb0d69c7d"),
            ("scripts/capture-tui-moments.py", "70cb7fa74220ea0a"),
        }
        included_paths = frozenset(
            classify_inventory(
                sorted({path for path, _ in measured} | {path for path, _ in pinned}),
                root=REPOSITORY_ROOT,
            )
        )

        self.assertEqual(
            {site for site in pinned if site[0] in included_paths},
            {site for site in measured if site[0] in included_paths},
            "a built-argv egress site appeared, vanished, or changed shape; "
            "name it in this pin",
        )

    def test_the_site_pin_predicts_a_real_projection(self) -> None:
        """PROJ-PIN-1: the site pin is the census a projection can actually run.

        Mirrors ``test_h1_f1.test_the_pin_predicts_a_real_projection``: build
        one real projection of the included set, ``git init`` it, and run
        the built-argv site census inside it. Classification through the
        exporter is not projection-aware once the projection deletes the
        policy — the pin must not name a ``private_only_exact`` path.
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

            def git(*arguments: str) -> None:
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("GIT_")
                }
                subprocess.run(
                    ["/usr/bin/git", *arguments],
                    cwd=projection,
                    env=environment,
                    check=True,
                    capture_output=True,
                )

            git("init", "-q", "--initial-branch=main")
            git("config", "user.name", "fixture")
            git("config", "user.email", "fixture@example.invalid")
            git("add", ".")
            git("commit", "-q", "-m", "projection fixture")

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "tests.test_no_listener_fence.WholeProductNoListenerFenceTests.test_built_argv_egress_sites_are_pinned",
                ],
                cwd=projection,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            0,
            completed.returncode,
            "the projected site census failed against the pin:\n"
            + completed.stderr[-4000:],
        )

    def test_bind_and_listen_calls_exist_only_in_the_ruled_af_unix_supervisor(self) -> None:
        calls: list[str] = []
        pattern = re.compile(r"\.(?:bind|listen)\s*\(")
        for path in sorted(Path("floati").rglob("*.py")):
            if pattern.search(path.read_text(encoding="utf-8")):
                calls.append(path.as_posix())
        self.assertEqual(["floati/sequencer.py"], calls)
        source = Path("floati/sequencer.py").read_text(encoding="utf-8")
        self.assertIn("socket.AF_UNIX", source)

    def test_mcp_module_imports_only_local_code_and_non_network_stdlib(self) -> None:
        """Catches an stdio server acquiring a listener, URL client, or dependency."""

        path = Path("floati/mcp.py")
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        allowed_stdlib = {
            "__future__",
            "argparse",
            "contextlib",
            "io",
            "json",
            "sys",
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


class ConstructedSubprocessEgressTests(unittest.TestCase):
    """NET-FENCE-1 (issue #25): constructed egress cannot pass the fence.

    Each test drives the fence's DETECTION over a constructed module —
    never a helper with a pre-built signature — and requires the egress
    to be caught and named. The consent gate itself stays the explicit
    whole-product allowlist above.
    """

    def _findings(self, source: str) -> set[tuple[str, str]]:
        return _network_subprocess_findings(ast.parse(source))

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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from typing import Optional


_NETWORK_SUBPROCESS_METHODS = frozenset(
    {"Popen", "call", "check_call", "check_output", "run"}
)
_NETWORK_TOOL_BASENAMES = frozenset(
    {
        "curl",
        "ftp",
        "gh",
        "nc",
        "ncat",
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


def _argv_string_literals(
    expression: ast.AST, definitions: dict[str, ast.FunctionDef]
) -> set[str]:
    """Expand local argv builders and return their literal string coordinates."""

    literals: set[str] = set()
    expanded: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
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
    basenames = {value.rsplit("/", 1)[-1] for value in literals}
    tools = sorted(basenames & _NETWORK_TOOL_BASENAMES)
    if tools:
        return tools[0]
    if {"issue", "view", "--repo", "--json"} <= literals:
        return "gh issue view"
    git_names = {"git", "git.exe"}
    verbs = sorted(literals & _NETWORK_GIT_VERBS)
    if basenames & git_names and verbs:
        return "git " + verbs[0]
    return None


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
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "subprocess"
                    and node.func.attr in _NETWORK_SUBPROCESS_METHODS
                ):
                    argv = node.args[0] if node.args else next(
                        (
                            keyword.value
                            for keyword in node.keywords
                            if keyword.arg == "args"
                        ),
                        None,
                    )
                    if argv is None:
                        continue
                    signature = _network_subprocess_signature(
                        _argv_string_literals(argv, definitions)
                    )
                    if signature is None:
                        continue
                    parent = parents.get(node)
                    while parent is not None and not isinstance(
                        parent, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ):
                        parent = parents.get(parent)
                    function = parent.name if parent is not None else "<module>"
                    observation = (relative, function, signature)
                    seen_network_subprocesses.add(observation)
                    if observation not in allowed_network_subprocesses:
                        violations.append(":".join(observation))
        self.assertEqual(allowed_socket_imports, seen_socket_imports)
        self.assertEqual(allowed_network_imports, seen_network_imports)
        self.assertEqual(allowed_network_subprocesses, seen_network_subprocesses)
        self.assertEqual([], violations)

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


if __name__ == "__main__":
    unittest.main()

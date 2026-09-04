"""Fence: a subprocess test must spawn the launcher, not `python3 -m floati`.

LAUNCH-1 put the product behind `scripts/floati` so the interpreter is never a
PATH lookup. Tests that still spelled `["python3", "-m", "floati", …]` never
exercised that launcher. This module is the remainder: every such argv in
`tests/` is a finding. `sys.executable -m floati` is a different shape — the
suite interpreter, not PATH `python3` — and is not this row: the launcher `cd`s
to the checkout and needs `dirname` on PATH.
"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIRECTORY = REPOSITORY_ROOT / "tests"

_INTERPRETERS = frozenset({"python3", "/usr/bin/python3", "/bin/python3"})


def test_modules(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.glob("test_*.py")))


def _constant_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _module_spawn_prefix(elts: list[ast.AST]) -> str | None:
    """Return the PATH-lookup interpreter head for a `-m floati` argv, or None."""

    if len(elts) < 3:
        return None
    dash_m = _constant_str(elts[1])
    module = _constant_str(elts[2])
    if dash_m != "-m" or module is None or not module.startswith("floati"):
        return None
    if module != "floati":
        return None
    head = elts[0]
    named = _constant_str(head)
    if named in _INTERPRETERS:
        return named
    return None


def bare_module_cli_spawns(root: Path) -> list[str]:
    """Return `file:line:head` for every PATH `python3 -m floati` argv head."""

    findings: list[str] = []
    for path in test_modules(root):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            findings.append(f"{path.name}::<unparseable>::{error.msg}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            head = _module_spawn_prefix(node.elts)
            if head is None:
                continue
            findings.append(f"{path.name}:{node.lineno}:{head}")
    return sorted(findings)


class Launch2CliSpawnFenceTests(unittest.TestCase):
    """The fence's own behaviour, proved on fixtures before the real tree."""

    def _fixture(self, stack: ExitStack, **modules: str) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name, source in modules.items():
            (root / f"{name}.py").write_text(source, encoding="utf-8")
        return root

    def test_a_python3_module_spawn_is_a_finding(self) -> None:
        """RED first: the argv LAUNCH-2 named."""

        source = (
            "import subprocess\n"
            "\n"
            "\n"
            "def run_cli():\n"
            '    subprocess.run(["python3", "-m", "floati", "status"])\n'
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_spawn=source)
            self.assertEqual(
                ["test_spawn.py:5:python3"],
                bare_module_cli_spawns(root),
            )

    def test_sys_executable_is_the_suite_interpreter_not_a_path_lookup(self) -> None:
        """sys.executable is not PATH `python3`; the launcher cds and needs dirname."""

        source = (
            "import subprocess\n"
            "import sys\n"
            "\n"
            "\n"
            "def run_cli():\n"
            "    subprocess.run([sys.executable, \"-m\", \"floati\", \"status\"])\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_spawn=source)
            self.assertEqual([], bare_module_cli_spawns(root))

    def test_the_launcher_argv_is_clean(self) -> None:
        source = (
            "import subprocess\n"
            "from tests.test_cli import LAUNCHER\n"
            "\n"
            "\n"
            "def run_cli():\n"
            "    subprocess.run([str(LAUNCHER), \"status\"])\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_spawn=source)
            self.assertEqual([], bare_module_cli_spawns(root))

    def test_floati_copy_is_not_the_cli_launcher(self) -> None:
        source = (
            "import subprocess\n"
            "\n"
            "\n"
            "def run_copy():\n"
            '    subprocess.run(["python3", "-m", "floati.copy"])\n'
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_spawn=source)
            self.assertEqual([], bare_module_cli_spawns(root))

    def test_the_real_tree_has_no_bare_module_cli_spawns(self) -> None:
        """The fence over the suite. An empty list is the claim."""

        self.assertEqual([], bare_module_cli_spawns(TESTS_DIRECTORY))


if __name__ == "__main__":
    unittest.main()

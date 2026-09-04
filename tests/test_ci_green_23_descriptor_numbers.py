"""Fence: a test must not fstat a descriptor number the test did not choose.

F9, F16 and CI-GREEN-21 each asserted about an fd NUMBER the kernel allocator
handed the parent, then read that same number in a child the launcher was
required to re-bind. A live number was not a leak; it was often the channel.
CI-GREEN-8 and CI-GREEN-22 repaired two of those instruments by OBJECT
identity. This module is the suite-wide remainder: every `fstat` of an integer
literal, and every child snippet that interpolates an fd number into `fstat`,
is either marked as a number the test placed on purpose or it is a finding.

A comment carrying `CI-GREEN-23-chosen` on the line immediately above an
integer-literal fstat is the mark. It is not a skip. The number still has to
be one the test (or the product contract) placed — channel 3 after this
test's own `dup2`, stderr, a range scan. A new `os.fstat(5)` without that
mark is a finding, which is the failure mode a two-file sweep cannot see.
"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIRECTORY = REPOSITORY_ROOT / "tests"
CHOSEN_MARK = "CI-GREEN-23-chosen"

_FSTAT_NAMES = frozenset({
    "fstat",
    "_fstat_identity",
    "_fstat_errno",
    "_descriptor_identity",
})


def test_modules(root: Path) -> tuple[Path, ...]:
    """Return every test module the loader would collect, sorted."""

    return tuple(sorted(root.glob("test_*.py")))


def _call_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _preceding_nonempty_line(lines: list[str], lineno: int) -> str:
    index = lineno - 2
    while index >= 0:
        text = lines[index].strip()
        if text:
            return text
        index -= 1
    return ""


def literal_fstat_findings(root: Path) -> list[str]:
    """Return `file:line:name(N)` for every unmarked integer-literal fstat."""

    findings: list[str] = []
    for path in test_modules(root):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            findings.append(f"{path.name}::<unparseable>::{error.msg}")
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node.func)
            if name not in _FSTAT_NAMES or not node.args:
                continue
            argument = node.args[0]
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, int):
                continue
            preceding = _preceding_nonempty_line(lines, node.lineno)
            if CHOSEN_MARK in preceding:
                continue
            findings.append(
                f"{path.name}:{node.lineno}:{name}({argument.value})"
            )
    return sorted(findings)


def interpolated_fstat_findings(root: Path) -> list[str]:
    """Return `file::function` where child source fstats a number without identity.

    The tell is the characters `fstat({` in a test function: an f-string
    interpolating some parent fd into a snippet the child will execute. If that
    function never names `st_dev` and `st_ino`, the snippet cannot be comparing
    objects and the site is number-bound.
    """

    findings: list[str] = []
    for path in test_modules(root):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            findings.append(f"{path.name}::<unparseable>::{error.msg}")
            continue
        lines = source.splitlines(keepends=True)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.end_lineno is None:
                continue
            body = "".join(lines[node.lineno - 1:node.end_lineno])
            if "fstat({" not in body:
                continue
            if "st_dev" in body and "st_ino" in body:
                continue
            findings.append(f"{path.name}::{node.name}")
    return sorted(findings)


class DescriptorNumberFenceTests(unittest.TestCase):
    """The fence's own behaviour, proved on fixtures before the real tree."""

    def _fixture(self, stack: ExitStack, **modules: str) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name, source in modules.items():
            (root / f"{name}.py").write_text(source, encoding="utf-8")
        return root

    def test_an_unmarked_integer_literal_fstat_is_a_finding(self) -> None:
        """RED first: the F9/F16 shape, on a throwaway file."""

        source = (
            "import os\n"
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_leak(self) -> None:\n"
            "        os.fstat(3)\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_number_bound=source)
            self.assertEqual(
                ["test_number_bound.py:7:fstat(3)"],
                literal_fstat_findings(root),
            )

    def test_the_same_literal_with_the_chosen_mark_is_clean(self) -> None:
        """The control for the RED: one comment, and the finding goes away."""

        source = (
            "import os\n"
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_leak(self) -> None:\n"
            "        # CI-GREEN-23-chosen: product channel after this test's dup2\n"
            "        os.fstat(3)\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_number_bound=source)
            self.assertEqual([], literal_fstat_findings(root))

    def test_fstat_of_a_held_name_is_not_a_finding(self) -> None:
        """A descriptor the test opened and still holds is identity-safe."""

        source = (
            "import os\n"
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_held(self) -> None:\n"
            "        descriptor = os.open(os.devnull, os.O_RDONLY)\n"
            "        try:\n"
            "            os.fstat(descriptor)\n"
            "        finally:\n"
            "            os.close(descriptor)\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_held=source)
            self.assertEqual([], literal_fstat_findings(root))

    def test_an_interpolated_fstat_without_identity_is_a_finding(self) -> None:
        """RED first: a child snippet that fstats the parent's number only."""

        # Built in pieces so THIS file does not itself contain the tell.
        injected = (
            "try:\\n"
            " _os.fstat(" + "{hostile})\\n"
            "except OSError:\\n"
            " pass\\n"
        )
        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_cross_exec(self) -> None:\n"
            "        injected = (\n"
            f"            \"{injected}\"\n"
            "        )\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_injected=source)
            self.assertEqual(
                ["test_injected.py::test_cross_exec"],
                interpolated_fstat_findings(root),
            )

    def test_an_interpolated_fstat_that_compares_objects_is_clean(self) -> None:
        """The control: the same snippet, with (st_dev, st_ino) beside it."""

        injected = (
            "status = _os.fstat(" + "{hostile})\\n"
            "if (status.st_dev, status.st_ino) == expected:\\n"
            " leak()\\n"
        )
        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_cross_exec(self) -> None:\n"
            "        injected = (\n"
            f"            \"{injected}\"\n"
            "        )\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_injected=source)
            self.assertEqual([], interpolated_fstat_findings(root))

    def test_the_real_tree_has_no_unmarked_literal_fstat(self) -> None:
        """The fence over the suite. An empty list is the claim."""

        self.assertEqual([], literal_fstat_findings(TESTS_DIRECTORY))

    def test_the_real_tree_has_no_number_bound_interpolated_fstat(self) -> None:
        """The fence over injected child snippets. An empty list is the claim."""

        self.assertEqual([], interpolated_fstat_findings(TESTS_DIRECTORY))


if __name__ == "__main__":
    unittest.main()

"""PIN-DRAFT-1: a pin whose positive branch no fixture reaches is a draft.

BUNDLE-1's rider census collected `_workspace_layout_finding` into a list,
allowed that list to be empty (`assertIn(len(...), (0, 1))`), and asserted
inside `if workspace_projections:`. On the seat's tree the function did not
exist, so the inner pin never ran. Composition reached it and it reded —
`str(row["path"])` as typed vs `ast.unparse`'s single quotes.

This module is the remainder of that shape: a test that builds a list
comprehension, then branches on the list's truthiness to assert, without a
presence pin the same function must satisfy. One fixture where the thing IS
present per site; an `if` that can skip the assert is a finding.
"""

from __future__ import annotations

import ast
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIRECTORY = REPOSITORY_ROOT / "tests"


def test_modules(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.glob("test_*.py")))


def _is_assert_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    return isinstance(func, ast.Attribute) and (
        func.attr.startswith("assert") or func.attr == "fail"
    )


def _len_of_name(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == name
    )


def _is_presence_assert(node: ast.AST, name: str) -> bool:
    """True when this statement requires `name` to be non-empty."""

    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    call = node.value
    if not isinstance(call.func, ast.Attribute):
        return False
    method = call.func.attr
    args = call.args
    if method == "assertTrue" and args:
        target = args[0]
        if isinstance(target, ast.Name) and target.id == name:
            return True
        if _len_of_name(target, name):
            return True
    if method in {"assertGreater", "assertGreaterEqual"} and len(args) >= 2:
        if _len_of_name(args[0], name) and isinstance(args[1], ast.Constant):
            if method == "assertGreater" and args[1].value == 0:
                return True
            if method == "assertGreaterEqual" and args[1].value >= 1:
                return True
    if method == "assertEqual" and len(args) >= 2:
        for left, right in ((args[0], args[1]), (args[1], args[0])):
            if isinstance(left, ast.Constant) and isinstance(left.value, int):
                if left.value >= 1 and _len_of_name(right, name):
                    return True
    return False


def _listcomp_names(function: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if isinstance(node.value, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            names.add(target.id)
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "list"
        ):
            names.add(target.id)
    return names


def unreachable_positive_branch_findings(root: Path) -> list[str]:
    """Return `file:line:function:name` for draft if-present assert branches."""

    findings: list[str] = []
    for path in test_modules(root):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            findings.append(f"{path.name}::<unparseable>::{error.msg}")
            continue
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not function.name.startswith("test_"):
                continue
            collections = _listcomp_names(function)
            if not collections:
                continue
            presence = {
                name
                for node in function.body
                for name in collections
                if _is_presence_assert(node, name)
            }
            for node in ast.walk(function):
                if not isinstance(node, ast.If) or node.orelse:
                    continue
                if not isinstance(node.test, ast.Name):
                    continue
                name = node.test.id
                if name not in collections or name in presence:
                    continue
                if not any(_is_assert_call(child) for child in ast.walk(node)):
                    continue
                findings.append(
                    f"{path.name}:{node.lineno}:{function.name}:{name}"
                )
    return sorted(findings)


class PinDraft1ReachableBranchTests(unittest.TestCase):
    """The fence's own behaviour, proved on fixtures before the real tree."""

    def _fixture(self, stack: ExitStack, **modules: str) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name, source in modules.items():
            (root / f"{name}.py").write_text(source, encoding="utf-8")
        return root

    def test_a_listcomp_if_without_a_presence_pin_is_a_finding(self) -> None:
        """RED first: the BUNDLE-1 rider shape, on a throwaway file."""

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_census(self) -> None:\n"
            "        rows = [node for node in tree if True]\n"
            "        if rows:\n"
            "            self.assertEqual(1, len(rows))\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_draft_pin=source)
            self.assertEqual(
                ["test_draft_pin.py:7:test_census:rows"],
                unreachable_positive_branch_findings(root),
            )

    def test_the_same_branch_with_a_presence_pin_is_clean(self) -> None:
        """Control: one fixture where the thing IS present, then the if is not a draft."""

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_census(self) -> None:\n"
            "        rows = [node for node in tree if True]\n"
            "        self.assertEqual(1, len(rows))\n"
            "        if rows:\n"
            "            self.assertEqual(1, len(rows))\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_present_pin=source)
            self.assertEqual([], unreachable_positive_branch_findings(root))

    def test_live_tests_have_no_draft_positive_branches(self) -> None:
        self.assertEqual([], unreachable_positive_branch_findings(TESTS_DIRECTORY))


if __name__ == "__main__":
    unittest.main()

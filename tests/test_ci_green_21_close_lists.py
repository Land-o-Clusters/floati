"""Fence: a test must not require an exact os.close() argument list.

CI-GREEN-21 saw extras 14 and 18 on an exact ``close.call_args_list``
equality. Those numbers were not the descriptors the test passed into
``_relocate_launch_descriptors``; they were other closes the patched
``os.close`` recorded in the same process. An fd-number probe of the
close log is sound only when the process is (CI-GREEN-7 F9/F16 class).

The named row's remedy is set-based membership over the descriptors the
test opened. This module is the remainder: every ``assertEqual`` whose
other operand is a mock named ``close``'s ``call_args_list`` and whose
expected value is a list of ``mock.call(...)`` is a finding. Filtering
that log to the opened set, then comparing sets, is not a finding.
"""

from __future__ import annotations

import ast
from pathlib import Path
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIRECTORY = REPOSITORY_ROOT / "tests"


def test_modules(root: Path) -> tuple[Path, ...]:
    """Return every test module the loader would collect, sorted."""

    return tuple(sorted(root.glob("test_*.py")))


def _is_close_call_args_list(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "call_args_list"
        and isinstance(node.value, ast.Name)
        and node.value.id == "close"
    )


def _is_mock_call_list(node: ast.AST) -> bool:
    if not isinstance(node, (ast.List, ast.ListComp)):
        return False
    if isinstance(node, ast.List):
        elements = node.elts
        if not elements:
            return False
        return all(_is_mock_call(element) for element in elements)
    return _is_mock_call(node.elt)


def _is_mock_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "call"
        and isinstance(func.value, ast.Name)
        and func.value.id == "mock"
    )


def exact_close_list_findings(root: Path) -> list[str]:
    """Return ``file:line`` for every exact close() call_args_list equality."""

    findings: list[str] = []
    for path in test_modules(root):
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as error:
            findings.append(f"{path.name}::<unparseable>::{error.msg}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func
            if isinstance(name, ast.Attribute):
                called = name.attr
            elif isinstance(name, ast.Name):
                called = name.id
            else:
                continue
            if called != "assertEqual" or len(node.args) < 2:
                continue
            left, right = node.args[0], node.args[1]
            if (
                _is_close_call_args_list(left) and _is_mock_call_list(right)
            ) or (
                _is_close_call_args_list(right) and _is_mock_call_list(left)
            ):
                findings.append(f"{path.name}:{node.lineno}")
    return findings


class CloseListFenceTests(unittest.TestCase):
    def test_suite_has_no_exact_close_call_args_list_equality(self) -> None:
        self.assertEqual([], exact_close_list_findings(TESTS_DIRECTORY))

    def test_control_list_comp_against_close_log_is_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "test_control.py").write_text(
                "from unittest import mock\n"
                "self.assertEqual(\n"
                "    [mock.call(descriptor) for descriptor in descriptors],\n"
                "    close.call_args_list,\n"
                ")\n",
                encoding="utf-8",
            )
            self.assertEqual(
                ["test_control.py:2"],
                exact_close_list_findings(root),
            )

    def test_set_filtered_close_log_is_not_a_finding(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "test_ok.py").write_text(
                "opened = set(descriptors)\n"
                "closed = [\n"
                "    call.args[0]\n"
                "    for call in close.call_args_list\n"
                "    if call.args[0] in opened\n"
                "]\n"
                "self.assertEqual(opened, set(closed))\n"
                "self.assertEqual(len(descriptors), len(closed))\n",
                encoding="utf-8",
            )
            self.assertEqual([], exact_close_list_findings(root))

    def test_train_extras_14_and_18_do_not_fail_membership_over_opened(self) -> None:
        from unittest import mock as unittest_mock

        descriptors = [3, 7, 4, 6, 5]
        opened = set(descriptors)
        recorded = [
            unittest_mock.call(3),
            unittest_mock.call(14),
            unittest_mock.call(7),
            unittest_mock.call(18),
            unittest_mock.call(4),
            unittest_mock.call(6),
            unittest_mock.call(5),
        ]
        closed = [
            call.args[0] for call in recorded if call.args[0] in opened
        ]
        self.assertEqual(opened, set(closed))
        self.assertEqual(len(descriptors), len(closed))


if __name__ == "__main__":
    unittest.main()

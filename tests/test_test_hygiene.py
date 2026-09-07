"""A test defined twice is a test that never ran, and no count can see it.

Four `test_` methods in one class of `tests/test_wake_daemon_adapters.py` were
written twice. Python binds a class body top to bottom, so the second `def`
REPLACED the first: four assertions sat in the file, were read in review, were
counted by every instrument that counts tests -- and had never executed once.
Re-bound and run, they errored.

⇒ **A DUPLICATE DEFINITION IS INVISIBLE TO EVERY INSTRUMENT THAT COUNTS.**
A test-count pin cannot see it: the count was always right, because the
duplicate never contributed a test to count. Coverage cannot see it: the file
is imported and the surviving method runs. A green suite cannot see it: nothing
failed, because nothing ran. Only reading the SOURCE for two definitions of one
name finds it, which is what this module does.

Scope, stated so the fence is read for what it is:

* **File-scoped as of TEST-DUP-WAKE-DAEMON (2026-09-07); class-scoped before.**
  This fence deliberately allowed one test name bound by two classes in one
  file -- `tests/test_wake_daemon.py` did exactly that, twice, and the
  allowance named the file. The deliberate example is what decayed: the two
  generations of `test_first_wake_verdict_leaves_proven_and_absent_bindings_alone`
  drifted one input apart inside one file while every instrument reported both
  as running. Both bodies still run (distinct classes, distinct ids -- nothing
  is shadowed), but one name bound twice in one file is two generations of one
  test waiting to drift, so the file-scoped finder now names it with the line
  numbers of every binding. The class-scoped finder stays: it names the
  SHADOWING shape, where the second binding deletes the first.
* **Direct body members, not `ast.walk`.** A method nested inside another
  function, or inside a nested class, is a different binding in a different
  scope and cannot shadow or collide with a file sibling.
* **Class names, same rule.** Two `class AdapterTests` in one module is the
  worse deletion: the second body replaces the first, and every `test_` on
  the first class is gone. The fence names `file::Class` for that.
* **What this cannot see:** a name defined once here and once by a base class or
  a mixin, which is legitimate override and indistinguishable from a mistake
  without knowing the author's intent; and a module-level `test_` function
  shadowed by another, which unittest's default loader does not collect from
  these files.
"""

from __future__ import annotations

import ast
import collections
import tempfile
import unittest
from pathlib import Path
from typing import Iterator


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIRECTORY = REPOSITORY_ROOT / "tests"


def test_modules(root: Path) -> tuple[Path, ...]:
    """Return every test module the loader would collect, sorted."""

    return tuple(sorted(root.glob("test_*.py")))


def _class_definitions(tree: ast.AST) -> Iterator[ast.ClassDef]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            yield node


def _test_method_names(body: ast.ClassDef) -> list[str]:
    """Return the `test_` names this class body binds, in source order.

    Direct members only. A `def` inside a helper function or a nested class
    binds a different name in a different scope and cannot shadow this one.
    """

    return [
        statement.name
        for statement in body.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name.startswith("test_")
    ]


def duplicate_test_methods(root: Path) -> list[str]:
    """Return `file::Class::name` for every test name a class body binds twice."""

    offenders: list[str] = []
    for path in test_modules(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:  # a file that cannot be parsed is its own finding
            offenders.append(f"{path.name}::<unparseable>::{error.msg}")
            continue
        for definition in _class_definitions(tree):
            counts = collections.Counter(_test_method_names(definition))
            for name, seen in sorted(counts.items()):
                if seen > 1:
                    offenders.append(
                        f"{path.name}::{definition.name}::{name} (defined {seen} times)"
                    )
    return sorted(offenders)


def file_scoped_duplicate_test_names(root: Path) -> list[str]:
    """Return `file::name (lines a, b)` for every test name a module binds twice.

    The TEST-DUP-WAKE-DAEMON fence: one name bound twice in one file, by any
    two bodies -- two classes, a class and the module, two module functions --
    is two generations of one test waiting to drift. Derived by walking every
    `tests/*.py` blob's AST; never a hand list. Direct members only: a `def`
    nested inside a helper binds a different name in a different scope.
    """

    offenders: list[str] = []
    for path in test_modules(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:  # a file that cannot be parsed is its own finding
            offenders.append(f"{path.name}::<unparseable>::{error.msg}")
            continue
        names: dict[str, list[int]] = collections.defaultdict(list)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                members: list[ast.stmt] = list(node.body)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                members = [node]
            else:
                continue
            for statement in members:
                if (
                    isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and statement.name.startswith("test_")
                ):
                    names[statement.name].append(statement.lineno)
        for name, lines in sorted(names.items()):
            if len(lines) > 1:
                rendered = ", ".join(str(line) for line in sorted(lines))
                offenders.append(f"{path.name}::{name} (lines {rendered})")
    return sorted(offenders)


def duplicate_test_classes(root: Path) -> list[str]:
    """Return `file::Class` for every class name a module body binds twice.

    Direct module members only. A nested class is a different binding.
    Python binds a module body top to bottom, so the second `class`
    REPLACES the first: every `test_` on the first body is gone.
    """

    offenders: list[str] = []
    for path in test_modules(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            offenders.append(f"{path.name}::<unparseable>::{error.msg}")
            continue
        names = [
            statement.name
            for statement in tree.body
            if isinstance(statement, ast.ClassDef)
        ]
        counts = collections.Counter(names)
        for name, seen in sorted(counts.items()):
            if seen > 1:
                offenders.append(f"{path.name}::{name} (defined {seen} times)")
    return sorted(offenders)


class DuplicateTestNameFenceTests(unittest.TestCase):
    """The fence's own behaviour, proved on fixtures before the real tree."""

    def _fixture(self, stack, **modules: str) -> Path:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        for name, source in modules.items():
            (root / f"{name}.py").write_text(source, encoding="utf-8")
        return root

    def test_a_name_bound_twice_in_one_class_is_a_finding(self) -> None:
        """RED first: the exact shape that shipped, on a throwaway file."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_one(self) -> None:\n"
            "        self.assertTrue(False)\n"
            "\n"
            "    def test_two(self) -> None:\n"
            "        self.assertTrue(True)\n"
            "\n"
            "    def test_one(self) -> None:\n"
            "        self.assertTrue(True)\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_duplicate_fixture=source)

            self.assertEqual(
                [
                    "test_duplicate_fixture.py::ExampleTests::test_one "
                    "(defined 2 times)"
                ],
                duplicate_test_methods(root),
            )

    def test_the_same_file_without_the_duplicate_is_clean(self) -> None:
        """The control for the RED: one edit, and the finding goes away."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_one(self) -> None:\n"
            "        self.assertTrue(False)\n"
            "\n"
            "    def test_two(self) -> None:\n"
            "        self.assertTrue(True)\n"
            "\n"
            "    def test_three(self) -> None:\n"
            "        self.assertTrue(True)\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_duplicate_fixture=source)

            self.assertEqual([], duplicate_test_methods(root))

    def test_four_duplicates_in_one_class_are_all_named(self) -> None:
        """The shipped defect's exact multiplicity: it reports all four, not one."""

        from contextlib import ExitStack

        body = "".join(
            f"    def test_{index}(self) -> None:\n        pass\n\n"
            for index in range(4)
        ) * 2
        source = "import unittest\n\n\nclass AdapterTests(unittest.TestCase):\n" + body
        with ExitStack() as stack:
            root = self._fixture(stack, test_shipped_shape=source)

            self.assertEqual(
                [
                    f"test_shipped_shape.py::AdapterTests::test_{index} "
                    "(defined 2 times)"
                    for index in range(4)
                ],
                duplicate_test_methods(root),
            )

    def test_the_same_name_in_two_classes_is_a_file_scoped_finding(self) -> None:
        """The scope, amended: two classes, one name, both run -- and named.

        The class-scoped finder still reports nothing, because nothing is
        shadowed; the file-scoped finder names the collision with both line
        numbers, because two generations of one test in one file drift.
        """

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class GreenTests(unittest.TestCase):\n"
            "    def test_shared_name(self) -> None:\n"
            "        pass\n"
            "\n"
            "\n"
            "class NoticeTests(unittest.TestCase):\n"
            "    def test_shared_name(self) -> None:\n"
            "        pass\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_two_classes=source)

            self.assertEqual([], duplicate_test_methods(root))
            self.assertEqual(
                ["test_two_classes.py::test_shared_name (lines 5, 10)"],
                file_scoped_duplicate_test_names(root),
            )

    def test_a_file_scoped_clean_module_is_clean(self) -> None:
        """The control for the RED: distinct names, one binding each."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class GreenTests(unittest.TestCase):\n"
            "    def test_one(self) -> None:\n"
            "        pass\n"
            "\n"
            "\n"
            "class NoticeTests(unittest.TestCase):\n"
            "    def test_two(self) -> None:\n"
            "        pass\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_two_classes=source)

            self.assertEqual([], file_scoped_duplicate_test_names(root))

    def test_a_module_function_and_a_class_method_sharing_a_name_is_a_finding(self) -> None:
        """The file is the scope: a module-level `test_` collides too."""

        from contextlib import ExitStack

        source = (
            "def test_shared_name() -> None:\n"
            "    pass\n"
            "\n"
            "\n"
            "class NoticeTests(unittest.TestCase):\n"
            "    def test_shared_name(self) -> None:\n"
            "        pass\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_module_and_class=source)

            self.assertEqual(
                ["test_module_and_class.py::test_shared_name (lines 1, 6)"],
                file_scoped_duplicate_test_names(root),
            )

    def test_a_nested_definition_is_not_a_file_scoped_finding(self) -> None:
        """A `def` inside a helper is a different scope, here as in the class walk."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_one(self) -> None:\n"
            "        def test_one() -> None:\n"
            "            pass\n"
            "\n"
            "        test_one()\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_nested=source)

            self.assertEqual([], file_scoped_duplicate_test_names(root))

    def test_a_nested_definition_does_not_shadow_the_class_member(self) -> None:
        """A `def` inside a helper is a different scope and must not be counted."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    def test_one(self) -> None:\n"
            "        def test_one() -> None:\n"
            "            pass\n"
            "\n"
            "        test_one()\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_nested=source)

            self.assertEqual([], duplicate_test_methods(root))

    def test_an_async_definition_counts(self) -> None:
        """`async def` binds the same name the same way."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.IsolatedAsyncioTestCase):\n"
            "    async def test_one(self) -> None:\n"
            "        pass\n"
            "\n"
            "    async def test_one(self) -> None:\n"
            "        pass\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_async=source)

            self.assertEqual(
                ["test_async.py::ExampleTests::test_one (defined 2 times)"],
                duplicate_test_methods(root),
            )

    def test_a_decorated_definition_counts(self) -> None:
        """A decorator does not change which name the body binds."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class ExampleTests(unittest.TestCase):\n"
            "    @unittest.skipIf(False, 'never')\n"
            "    def test_one(self) -> None:\n"
            "        pass\n"
            "\n"
            "    def test_one(self) -> None:\n"
            "        pass\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_decorated=source)

            self.assertEqual(
                ["test_decorated.py::ExampleTests::test_one (defined 2 times)"],
                duplicate_test_methods(root),
            )

    def test_a_class_name_bound_twice_in_one_module_is_a_finding(self) -> None:
        """RED: two class bodies with one name; the first class's tests never run."""

        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class AdapterTests(unittest.TestCase):\n"
            "    def test_first(self) -> None:\n"
            "        pass\n"
            "\n"
            "\n"
            "class AdapterTests(unittest.TestCase):\n"
            "    def test_second(self) -> None:\n"
            "        pass\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_shadowed_class=source)
            self.assertEqual(
                ["test_shadowed_class.py::AdapterTests (defined 2 times)"],
                duplicate_test_classes(root),
            )

    def test_two_different_class_names_in_one_module_are_not_a_finding(self) -> None:
        from contextlib import ExitStack

        source = (
            "import unittest\n"
            "\n"
            "\n"
            "class GreenTests(unittest.TestCase):\n"
            "    def test_one(self) -> None:\n"
            "        pass\n"
            "\n"
            "\n"
            "class NoticeTests(unittest.TestCase):\n"
            "    def test_one(self) -> None:\n"
            "        pass\n"
        )
        with ExitStack() as stack:
            root = self._fixture(stack, test_two_classes=source)
            self.assertEqual([], duplicate_test_classes(root))


class RepositoryTestHygieneTests(unittest.TestCase):
    def test_the_fence_has_a_population(self) -> None:
        """A sweep over nothing passes. Control every zero."""

        modules = test_modules(TESTS_DIRECTORY)
        self.assertGreater(len(modules), 100)
        self.assertIn(
            "test_test_hygiene.py", {path.name for path in modules}
        )

    def test_no_test_class_defines_the_same_test_name_twice(self) -> None:
        """The fence over the real tree.

        Four methods shipped this way and none of them had ever run. The
        offenders are listed as `file::Class::name` so a failure names the
        binding to delete rather than the file to go looking in.
        """

        self.assertEqual([], duplicate_test_methods(TESTS_DIRECTORY))

    def test_no_test_module_defines_the_same_class_name_twice(self) -> None:
        """A shadowed class deletes every test the first body bound."""

        self.assertEqual([], duplicate_test_classes(TESTS_DIRECTORY))

    def test_no_test_module_binds_the_same_test_name_twice(self) -> None:
        """The TEST-DUP-WAKE-DAEMON fence over the real tree.

        `tests/test_wake_daemon.py` carried two generations of one test name
        in two classes -- both ran, nothing was shadowed, and the generations
        still drifted one input apart. The finding names the file, the name,
        and every binding line, so the resolution is a decision about named
        bodies rather than a hunt.
        """

        self.assertEqual([], file_scoped_duplicate_test_names(TESTS_DIRECTORY))


if __name__ == "__main__":
    unittest.main()

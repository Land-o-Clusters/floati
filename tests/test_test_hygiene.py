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

* **Class-scoped, by design.** Two classes in one file may both define
  `test_first_wake_of_an_unproven_binding_flips_it_proven` -- that is two tests
  with one name and both of them run. `tests/test_wake_daemon.py` does exactly
  this, twice, deliberately, and a fence that flagged it would be reporting the
  English language. The defect is one name bound twice in ONE body.
* **Direct body members, not `ast.walk`.** A method nested inside another
  function, or inside a nested class, is a different binding in a different
  scope. Only the class body's own statements can shadow each other.
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


def cross_class_test_names(root: Path) -> dict[str, list[str]]:
    """Return `file::name` -> the classes sharing it, for names in 2+ classes.

    The fence's scope, made visible. Every entry here is ALLOWED: each of these
    definitions is bound and runs. If this fence ever grows a file-scoped mode,
    this is the list it would wrongly accuse.
    """

    seen: dict[str, list[str]] = collections.defaultdict(list)
    for path in test_modules(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError:
            continue
        for definition in _class_definitions(tree):
            for name in set(_test_method_names(definition)):
                seen[f"{path.name}::{name}"].append(definition.name)
    return {
        key: sorted(classes) for key, classes in seen.items() if len(classes) > 1
    }


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

    def test_the_same_name_in_two_classes_is_not_a_finding(self) -> None:
        """The scope, asserted: two classes, one name, both bound, both run."""

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
                {"test_two_classes.py::test_shared_name": ["GreenTests", "NoticeTests"]},
                cross_class_test_names(root),
            )

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

    def test_the_allowed_cross_class_names_are_real_and_are_not_findings(self) -> None:
        """The allowance is exercised by the REAL tree, not only by a fixture.

        If this ever drops to zero the fence still passes, and its scope would
        then be untested against real data -- so the existence of the allowance
        is asserted here, beside the assertion that it is not a finding.
        """

        allowed = cross_class_test_names(TESTS_DIRECTORY)

        self.assertTrue(allowed)
        offenders = set(duplicate_test_methods(TESTS_DIRECTORY))
        for key, classes in allowed.items():
            with self.subTest(name=key):
                self.assertGreater(len(classes), 1)
                self.assertFalse(
                    any(offender.startswith(key.split("::")[0]) and
                        offender.endswith(f"::{key.split('::')[1]} (defined 2 times)")
                        for offender in offenders)
                )


if __name__ == "__main__":
    unittest.main()

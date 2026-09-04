"""LAUNCH-2-F1: observer child spec.parent must match __package__.

Box Python 3.9.25 emits ``DeprecationWarning: __package__ != __spec__.parent``
when the isolated observer is exec'd with ``__package__='floati'`` and a
``ModuleSpec`` named ``__main__`` (parent ``''``). Mac 3.9.6 is silent. After
LAUNCH-2 the CLI goes through the launcher's ``/usr/bin/python3``, so the box
leg sees the warning on stderr and
``test_effect_cli.artifact``'s empty-stderr pin fails.

runpy's ``python -m`` convention names the spec ``{package}.__main__`` so
``spec.parent == __package__``. This fence reads that construction from the
loader source — the comparison CPython 3.9.25 makes — and does not depend on
this host's micro version.
"""

from __future__ import annotations

import ast
import importlib.machinery
import unittest
import warnings

from floati.effect_reconciliation_exec import _LOADER


def observer_exec_spec_name(source: str) -> str:
    """Return the ModuleSpec name assigned to ``spec`` in the observer loader."""

    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != "spec":
            continue
        call = node.value
        if not isinstance(call, ast.Call) or not call.args:
            raise AssertionError("observer loader spec is not a ModuleSpec call")
        arg0 = call.args[0]
        if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
            return arg0.value
        raise AssertionError("observer loader spec name is not a string constant")
    raise AssertionError("observer loader does not assign spec")


def observer_exec_package(source: str) -> str:
    """Return ``__package__`` from the observer exec namespace literal."""

    tree = ast.parse(source)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if node.targets[0].id != "namespace":
            continue
        if not isinstance(node.value, ast.Dict):
            raise AssertionError("observer loader namespace is not a dict")
        for key, value in zip(node.value.keys, node.value.values):
            if (
                isinstance(key, ast.Constant)
                and key.value == "__package__"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                return value.value
        raise AssertionError("observer loader namespace has no __package__ string")
    raise AssertionError("observer loader does not assign namespace")


class Launch2F1ObserverRunpyTests(unittest.TestCase):
    def test_observer_spec_parent_matches_package_under_deprecation_errors(self) -> None:
        """RED-first: the 3.9.25 runpy check, as an assertion, under -W error."""

        name = observer_exec_spec_name(_LOADER)
        package = observer_exec_package(_LOADER)
        spec = importlib.machinery.ModuleSpec(
            name, loader=None, origin="effect_reconciliation_observer.py"
        )
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            if package != spec.parent:
                warnings.warn(
                    f"{package!r} != {spec.parent!r}",
                    DeprecationWarning,
                    stacklevel=1,
                )
        self.assertEqual(package, spec.parent)

    def test_observer_spec_uses_runpy_package_main_name(self) -> None:
        self.assertEqual("floati.__main__", observer_exec_spec_name(_LOADER))
        self.assertEqual("floati", observer_exec_package(_LOADER))


if __name__ == "__main__":
    unittest.main()

"""Fail-closed coverage of the native executable command contract."""

from __future__ import annotations

import argparse
import copy
from functools import lru_cache
import importlib
from pathlib import Path
import unittest
from unittest import mock

from floati.cli import _parser
from floati.command_contract import describe_parser


try:
    _commands = importlib.import_module("tests.bus_scale_commands")
except ModuleNotFoundError as exc:
    if exc.name != "tests.bus_scale_commands":
        raise
    _commands = None


# Exact reviewed exclusions; neither a prefix nor help visibility is authority
# to exempt another command. Their argument contracts also remain bound.
_ADMINISTRATION_PATHS = frozenset({
    "init", "setup", "register", "retire", "node add", "node spawn", "node retire",
    "node switch", "node role", "node role-clear", "node person set", "node person clear", "seat add", "seat remove", "seat migrate", "seat retire",
    "hook install", "hook uninstall", "backup", "wake daemon consent", "wake daemon bind",
    "wake daemon install", "wake daemon start", "wake daemon stop",
    "wake daemon remove", "wake daemon revoke", "sequencer serve",
    "net route add", "net route remove", "sequencer direct", "grant", "grant revoke", "epoch roll", "journal checkpoint", "journal verify",
    "repair quarantine", "signature sign", "signature verify", "install",
    "update", "update consent", "update revoke", "update status",
    "update check", "update apply", "update rollback", "update fleet preview",
    "update fleet apply", "update prepare", "uninstall", "purge", "chart add-root",
    "chart remove-root", "survey", "snapshot", "context policy set",
    "context policy clear", "presence set", "role transfer-architect", "role new",
    "role import", "role edit", "cross enable", "cross disable",
})


def _must_not_execute(*args: object, **kwargs: object) -> None:
    raise AssertionError("command census must not execute a native handler")


def _subcommands(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    actions = [
        action for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    ]
    if len(actions) != 1:
        raise AssertionError("fixture expected exactly one subcommand registry")
    return actions[0]


def _at(parser: argparse.ArgumentParser, path: str) -> argparse.ArgumentParser:
    for part in path.split():
        parser = _subcommands(parser).choices[part]
    return parser


def _executable_paths(parser: argparse.ArgumentParser) -> set[str]:
    """Independently walk both native dispatch mechanisms, including parents."""
    paths: set[str] = set()

    def visit(current: argparse.ArgumentParser, prefix: tuple[str, ...]) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, child in action.choices.items():
                path = (*prefix, name)
                if "handler" in child._defaults or "direct_handler" in child._defaults:
                    paths.add(" ".join(path))
                visit(child, path)

    visit(parser, ())
    return paths


@lru_cache(maxsize=1)
def _command_template():
    return _commands.executable_contract(_parser())


class BusScaleCommandCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            _commands,
            "missing census helper tests.bus_scale_commands: implement "
            "executable_contract, classify_contract and require_coverage",
        )
        for name in ("executable_contract", "classify_contract", "require_coverage"):
            self.assertTrue(
                callable(getattr(_commands, name, None)),
                f"missing census helper function tests.bus_scale_commands.{name}",
            )
        self._fixture_parser = None
        self.rows = copy.deepcopy(_command_template())

    @property
    def parser(self):
        # Only parser controls construct a parser; each mutation stays local.
        if self._fixture_parser is None:
            self._fixture_parser = _parser()
        return self._fixture_parser

    def _measured(self, rows: dict | None = None) -> dict:
        rows = self.rows if rows is None else rows
        classified = _commands.classify_contract(rows)
        return {path: copy.deepcopy(rows[path]) for path in classified["routine"]}

    def _assert_unknown(self, parser: argparse.ArgumentParser, paths: set[str]) -> None:
        rows = _commands.executable_contract(parser)
        classified = _commands.classify_contract(rows)
        self.assertTrue(paths <= set(rows))
        self.assertTrue(paths <= set(classified["unknown"]))
        # Supplying a measurement for an unreviewed path cannot authorize it.
        measured = self._measured()
        measured.update({path: copy.deepcopy(rows[path]) for path in paths})
        with self.assertRaises(ValueError):
            _commands.require_coverage(rows, measured)

    def test_executable_rows_are_a_full_native_contract_bijection(self) -> None:
        expected = {
            " ".join(row["path"]): row
            for row in describe_parser(self.parser)["commands"]
            if row["executable"]
        }
        self.assertEqual(_executable_paths(self.parser), set(self.rows))
        self.assertEqual(expected, self.rows)
        self.assertNotIn("wake", self.rows)
        self.assertNotIn("context", self.rows)
        # The explicit census and its dry plan use this exact scope. Walking
        # every native parser path is enumeration only: neither a handler nor
        # a child may start while checking its measured/excluded disposition.
        from tests.bus_scale_workloads import scope_contract, _SEAM_NATIVE_COMMANDS
        with mock.patch("floati.cli.main", side_effect=_must_not_execute),\
                mock.patch("subprocess.Popen", side_effect=_must_not_execute):
            plan = scope_contract(self.parser)
        commands = {row["command"] for row in plan["cases"].values()
                    if not row["command"].startswith("seam:")}
        commands.update(_SEAM_NATIVE_COMMANDS.values())
        self.assertFalse(commands & set(plan["excluded"]))
        self.assertEqual(set(expected), commands | set(plan["excluded"]))

    def test_hidden_delegated_and_executable_parent_commands_are_retained(self) -> None:
        for path in (
            "wake-evaluate", "wake-record", "wake-callback", "wake daemon serve",
            "update consent", "update rollback", "epoch roll", "context reading record",
            "chart", "grant", "update",
        ):
            with self.subTest(path=path):
                self.assertIn(path, self.rows)
                self.assertTrue(self.rows[path]["executable"])
        self.assertFalse(self.rows["wake daemon serve"]["public"])
        self.assertFalse(self.rows["update consent"]["public"])

    def test_reviewed_classification_is_an_exact_disjoint_native_partition(self) -> None:
        classified = _commands.classify_contract(self.rows)
        routine = classified["routine"]
        administration = classified["administration"]
        self.assertEqual(64, len(_ADMINISTRATION_PATHS))
        self.assertEqual(_ADMINISTRATION_PATHS, set(administration))
        self.assertEqual([], classified["unknown"])
        self.assertEqual(len(routine), len(set(routine)))
        self.assertFalse(set(routine) & set(administration))
        self.assertEqual(set(self.rows) - _ADMINISTRATION_PATHS, set(routine))
        for path, reason in administration.items():
            with self.subTest(path=path):
                self.assertIsInstance(reason, str)
                self.assertTrue(reason.strip())
        # Output-proportional history reads are not silently exempted.
        self.assertTrue({"log", "receipts", "wait", "send", "doctor", "status"} <= set(routine))
        self.assertIn("wake daemon serve", routine)

    def test_exact_native_routine_contract_coverage_is_accepted(self) -> None:
        _commands.require_coverage(self.rows, self._measured())
        # Warming the shared reviewed fixture must not grant callers ownership
        # of its nested rows or make later changed source bytes admissible.
        baseline = _commands._baseline_contract()
        baseline["send"]["path"].append("unreviewed")
        self.assertEqual(self.rows["send"], _commands._baseline_contract()["send"])
        measured = self._measured()
        with mock.patch.object(_commands, "_FIXTURE") as fixture:
            fixture.read_bytes.return_value = b"changed reviewed fixture bytes\n"
            with self.assertRaises(ValueError):
                _commands.require_coverage(self.rows, measured)

    def test_missing_routine_measurement_fails(self) -> None:
        measured = self._measured()
        del measured["send"]
        with self.assertRaises(ValueError):
            _commands.require_coverage(self.rows, measured)

    def test_administration_measurement_cannot_overlap_the_routine_population(self) -> None:
        measured = self._measured()
        measured["epoch roll"] = copy.deepcopy(self.rows["epoch roll"])
        with self.assertRaises(ValueError):
            _commands.require_coverage(self.rows, measured)

    def test_stale_or_invented_measurement_path_fails(self) -> None:
        measured = self._measured()
        row = copy.deepcopy(self.rows["send"])
        row["path"] = ["scale-probe"]
        measured["scale-probe"] = row
        with self.assertRaises(ValueError):
            _commands.require_coverage(self.rows, measured)

    def test_new_public_leaf_is_unknown_even_with_a_measurement(self) -> None:
        child = _subcommands(self.parser).add_parser("scale-probe")
        child.set_defaults(handler=_must_not_execute)
        self._assert_unknown(self.parser, {"scale-probe"})

    def test_new_hidden_direct_handler_is_unknown_even_with_a_measurement(self) -> None:
        child = _subcommands(self.parser).add_parser("scale-probe", floati_public=False)
        child.set_defaults(direct_handler=_must_not_execute)
        self._assert_unknown(self.parser, {"scale-probe"})

    def test_new_child_under_an_administration_prefix_is_not_exempted(self) -> None:
        child = _subcommands(_at(self.parser, "update")).add_parser("scale-probe")
        child.set_defaults(handler=_must_not_execute)
        self._assert_unknown(self.parser, {"update scale-probe"})

    def test_new_executable_parent_and_its_leaf_are_both_unknown(self) -> None:
        parent = _subcommands(self.parser).add_parser("scale-probe")
        parent.set_defaults(handler=_must_not_execute)
        child = parent.add_subparsers(dest="scale_operation").add_parser("read")
        child.set_defaults(direct_handler=_must_not_execute)
        self._assert_unknown(self.parser, {"scale-probe", "scale-probe read"})

    def test_new_nonexecutable_container_does_not_hide_its_executable_leaf(self) -> None:
        parent = _subcommands(self.parser).add_parser("scale-probe")
        child = parent.add_subparsers(dest="scale_operation").add_parser("read")
        child.set_defaults(handler=_must_not_execute)
        rows = _commands.executable_contract(self.parser)
        self.assertNotIn("scale-probe", rows)
        self._assert_unknown(self.parser, {"scale-probe read"})

    def test_new_routine_argument_invalidates_old_measurement_contract(self) -> None:
        measured = self._measured()
        _at(self.parser, "send").add_argument("--scale-mode", choices=("one", "two"))
        changed = _commands.executable_contract(self.parser)
        self.assertNotEqual(self.rows["send"], changed["send"])
        with self.assertRaises(ValueError):
            _commands.require_coverage(changed, measured)

    def test_argument_shape_drift_in_a_measurement_is_not_path_coverage(self) -> None:
        for field, replacement in (
            ("choices", ["unreviewed-mode"]),
            ("required", False),
            ("type", "integer"),
            ("nargs", "*"),
            ("default", "unreviewed-default"),
        ):
            with self.subTest(field=field):
                measured = self._measured()
                argument = next(
                    row for row in measured["send"]["arguments"] if row["name"] == "sender"
                )
                self.assertNotEqual(replacement, argument[field])
                argument[field] = replacement
                with self.assertRaises(ValueError):
                    _commands.require_coverage(self.rows, measured)

    def test_administration_argument_addition_invalidates_its_exclusion(self) -> None:
        measured = self._measured()
        _at(self.parser, "epoch roll").add_argument("--scale-mode", choices=("one", "two"))
        changed = _commands.executable_contract(self.parser)
        self.assertNotEqual(self.rows["epoch roll"], changed["epoch roll"])
        with self.assertRaises(ValueError):
            _commands.require_coverage(changed, measured)

    def test_only_native_temporary_example_tokens_adapt_for_linux(self) -> None:
        moved = copy.deepcopy(self.rows)
        tokens = {"\x2fprivate/tmp/floati-contract": "\x2ftmp/floati-contract",
                  "\x2fprivate/tmp/floati-contract.json": "\x2ftmp/floati-contract.json"}
        # Build the expected native Linux presentation independently of helper code.
        for row in moved.values():
            row["example_argv"] = [tokens.get(value, value) for value in row["example_argv"]]
        with mock.patch.object(_commands, "worker_workspace_root", return_value=Path("\x2ftmp/floati-work")):
            classified = _commands.classify_contract(moved)
            _commands.require_coverage(moved, {path: copy.deepcopy(moved[path])
                                               for path in classified["routine"]})
            arbitrary = copy.deepcopy(moved)
            arbitrary["send"]["example_argv"].append("\x2ftmp/arbitrary-contract")
            self.assertIn("send", _commands.classify_contract(arbitrary)["unknown"])
            changed_default = copy.deepcopy(moved)
            changed_default["send"]["arguments"][0]["default"] = "\x2ftmp/floati-contract"
            self.assertIn("send", _commands.classify_contract(changed_default)["unknown"])

    def test_administration_new_choice_invalidates_its_exclusion(self) -> None:
        measured = self._measured()
        action = next(
            action for action in _at(self.parser, "node add")._actions
            if action.dest == "lifetime"
        )
        action.choices = (*action.choices, "scale-probe")
        changed = _commands.executable_contract(self.parser)
        self.assertNotEqual(self.rows["node add"], changed["node add"])
        with self.assertRaises(ValueError):
            _commands.require_coverage(changed, measured)


if __name__ == "__main__":
    unittest.main()

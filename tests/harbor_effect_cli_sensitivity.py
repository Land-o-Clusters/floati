"""Opt-in real effect CLI parity and assertion-routing evidence."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

from floati import cli
from tests import test_effect_cli as subject


METHOD = "test_cli_never_accepts_raw_record_adapter_or_confirmation_override"


def native_provenance(testcase, report):
    case_type = subject.EffectCliTests
    native_cli, native_popen = case_type.run_cli, subprocess.Popen
    doctor = case_type(METHOD)
    doctor.setUp()
    doctor_children = []

    def observe_doctor(*arguments, **keywords):
        child = native_popen(*arguments, **keywords)
        doctor_children.append(child)
        return child

    try:
        with mock.patch.object(subprocess, "Popen", observe_doctor):
            actual = native_cli(doctor, "doctor", "--root", str(doctor.home),
                                "--source", str(subject.REPOSITORY_ROOT), "--json")
        report["native_doctor"] = {"returncode": actual.returncode, "stdout": actual.stdout,
            "stderr": actual.stderr, "children": [{"pid": child.pid, "returncode": child.returncode}
                                                  for child in doctor_children]}
        findings = json.loads(actual.stdout)["evidence"]["findings"]
        finding = next(item for item in findings if item["code"] == "launcher_interpreter")
        report["native_interpreter_finding"] = finding
        testcase.assertTrue(finding["detail"].endswith(
            "It is running " + report["direct_interpreter_resolved"] + "."), finding)
        testcase.assertTrue(finding["detail"].startswith(("candidate:", "declared:")))
        testcase.assertTrue(all(child.returncode is not None for child in doctor_children))
    finally:
        doctor.doCleanups()
        report["doctor_fixture_removed"] = not Path(doctor.temp.name).exists()
        testcase.assertTrue(report["doctor_fixture_removed"])


class NativeEffectCliProvenance(unittest.TestCase):
    def test_actual_native_launcher_interpreter(self):
        paths = ("tests/test_effect_cli.py", "floati/cli.py", "scripts/floati", "floati/__main__.py")
        before = {name: hashlib.sha256((subject.REPOSITORY_ROOT / name).read_bytes()).hexdigest()
                  for name in paths}
        report = {"scope": "targeted actual native doctor provenance; no matrix rerun",
                  "source_before": before, "direct_interpreter": sys.executable,
                  "direct_interpreter_resolved": str(Path(sys.executable).resolve()),
                  "direct_version": sys.version, "direct_package": str(Path(cli.__file__).resolve()),
                  "native_launcher": str(subject.LAUNCHER.resolve()),
                  "FLOATI_PYTHON": os.environ.get("FLOATI_PYTHON", "")}
        self.assertEqual(subject.REPOSITORY_ROOT / "floati/cli.py", Path(cli.__file__).resolve())
        self.assertEqual(subject.REPOSITORY_ROOT / "scripts/floati", subject.LAUNCHER.resolve())
        try:
            native_provenance(self, report)
        finally:
            report["source_after"] = {name: hashlib.sha256((subject.REPOSITORY_ROOT / name).read_bytes()).hexdigest()
                                      for name in paths}
            print("effect CLI native provenance", json.dumps(report, sort_keys=True))
        self.assertEqual(before, report["source_after"])


class ObservedResult(unittest.TestResult):
    def __init__(self):
        super().__init__()
        self.coordinates = []

    def addSubTest(self, test, subtest, error):
        self.coordinates.append({"params": dict(subtest.params), "failed": error is not None})
        super().addSubTest(test, subtest, error)


class EffectCliSensitivity(unittest.TestCase):
    def test_actual_matrix_text_parity_and_four_targeted_predicates(self):
        case_type = subject.EffectCliTests
        native_cli, direct_main = case_type.run_cli, case_type.run_main
        native_popen = subprocess.Popen
        original_parser, original_emit = cli._parser, cli._emit
        source_paths = ("tests/test_effect_cli.py", "floati/cli.py", "scripts/floati", "floati/__main__.py")
        before = {name: hashlib.sha256((subject.REPOSITORY_ROOT / name).read_bytes()).hexdigest()
                  for name in source_paths}
        report = {"scope": "decoded-text parity and real-path assertion routing; no raw-byte claim",
                  "source_before": before, "direct_interpreter": sys.executable,
                  "direct_interpreter_resolved": str(Path(sys.executable).resolve()),
                  "direct_version": sys.version, "direct_package": str(Path(cli.__file__).resolve()),
                  "native_launcher": str(subject.LAUNCHER.resolve()),
                  "FLOATI_PYTHON": os.environ.get("FLOATI_PYTHON", ""),
                  "runs": []}
        self.assertEqual(subject.REPOSITORY_ROOT / "floati/cli.py", Path(cli.__file__).resolve())
        self.assertEqual(subject.REPOSITORY_ROOT / "scripts/floati", subject.LAUNCHER.resolve())
        expected_coordinates = None

        def target(arguments):
            return arguments[:2] == ("effect", "compensate") and arguments[-3:] == ("--preview", "--target", "hostile")

        def accept_target():
            parser = original_parser()
            root_commands = next(action for action in parser._actions
                                 if isinstance(action, argparse._SubParsersAction))
            effect_commands = next(action for action in root_commands.choices["effect"]._actions
                                   if isinstance(action, argparse._SubParsersAction))
            effect_commands.choices["compensate"].add_argument("--target")
            return parser

        def emit_stderr(*arguments, **keywords):
            with redirect_stdout(sys.stderr):
                return original_emit(*arguments, **keywords)

        def emit_twice(*arguments, **keywords):
            original_emit(*arguments, **keywords)
            return original_emit(*arguments, **keywords)

        predicates = {
            "accepted_option": "AssertionError: 'arguments_invalid' != 'effect_compensation_plan_unavailable'",
            "return_status": "self.assertEqual(20, status)",
            "stderr": 'self.assertEqual("", stderr)',
            "line_count": "self.assertEqual(1, len(stdout.splitlines()))",
        }
        try:
            for label in ("parity_pristine", *predicates, "restored_pristine"):
                calls, parity, children = [], [], []
                case, result = case_type(METHOD), ObservedResult()

                def observed_popen(*arguments, **keywords):
                    child = native_popen(*arguments, **keywords)
                    children.append(child)
                    return child

                def observe_native(fixture, *arguments):
                    actual = native_cli(fixture, *arguments)
                    calls.append({"kind": "native", "argv": arguments,
                                  "status": actual.returncode, "stdout": actual.stdout, "stderr": actual.stderr})
                    if label == "parity_pristine":
                        direct = direct_main(fixture, *arguments)
                        parity.append({"argv": arguments, "native": [actual.returncode, actual.stdout, actual.stderr],
                                       "direct": list(direct)})
                    return actual

                def observe_direct(fixture, *arguments):
                    with ExitStack() as patches:
                        if target(arguments):
                            if label == "accepted_option":
                                patches.enter_context(mock.patch.object(cli, "_parser", accept_target))
                            elif label == "return_status":
                                patches.enter_context(mock.patch.object(cli, "CONFIGURATION_REFUSED", 0))
                            elif label == "stderr":
                                patches.enter_context(mock.patch.object(cli, "_emit", emit_stderr))
                            elif label == "line_count":
                                patches.enter_context(mock.patch.object(cli, "_emit", emit_twice))
                        actual = direct_main(fixture, *arguments)
                    calls.append({"kind": "direct", "argv": arguments,
                                  "status": actual[0], "stdout": actual[1], "stderr": actual[2]})
                    if label == "parity_pristine":
                        native = native_cli(fixture, *arguments)
                        parity.append({"argv": arguments, "native": [native.returncode, native.stdout, native.stderr],
                                       "direct": list(actual)})
                    return actual

                with mock.patch.object(subprocess, "Popen", observed_popen),\
                        mock.patch.object(case_type, "run_cli", observe_native),\
                        mock.patch.object(case_type, "run_main", observe_direct):
                    case.run(result)
                row = {"label": label, "calls": calls, "parity": parity,
                       "coordinates": result.coordinates, "tests_run": result.testsRun,
                       "failures": [(str(test), trace) for test, trace in result.failures],
                       "errors": [(str(test), trace) for test, trace in result.errors],
                       "skipped": result.skipped,
                       "children": [{"pid": child.pid, "args": child.args,
                                     "returncode": child.returncode} for child in children],
                       "fixture_removed": not Path(case.temp.name).exists()}
                report["runs"].append(row)
                self.assertEqual(1, result.testsRun)
                self.assertEqual([], result.errors)
                self.assertEqual([], result.skipped)
                self.assertEqual(44, len(calls))
                self.assertEqual(8, sum(call["kind"] == "native" for call in calls))
                self.assertEqual(36, sum(call["kind"] == "direct" for call in calls))
                self.assertEqual(44, len(result.coordinates))
                coordinates = [item["params"] for item in result.coordinates]
                if expected_coordinates is None:
                    expected_coordinates = coordinates
                self.assertEqual(expected_coordinates, coordinates)
                self.assertEqual(44 if parity else 8, len(children))
                self.assertTrue(all(child.returncode == 20 for child in children))
                self.assertTrue(row["fixture_removed"])
                if label in predicates:
                    self.assertEqual(1, len(result.failures), result.failures)
                    failed, trace = result.failures[0]
                    self.assertEqual({"command": ("effect", "compensate"), "option": "--target"}, dict(failed.params))
                    self.assertIn(predicates[label], trace)
                    actual = next(call for call in calls if target(call["argv"]))
                    if label == "accepted_option":
                        self.assertEqual(20, actual["status"])
                        self.assertEqual("", actual["stderr"])
                        self.assertEqual(1, len(actual["stdout"].splitlines()))
                        self.assertEqual("effect_compensation_plan_unavailable", json.loads(actual["stdout"])["evidence"]["code"])
                else:
                    self.assertEqual([], result.failures)
                if parity:
                    self.assertEqual(44, len(parity))
                    for pair in parity:
                        self.assertEqual(pair["native"], pair["direct"], pair["argv"])
            # A separate real launcher command exposes its selected shim and the
            # actual running executable. It is not one of the 44 parity pairs.
            native_provenance(self, report)
        finally:
            report["source_after"] = {name: hashlib.sha256((subject.REPOSITORY_ROOT / name).read_bytes()).hexdigest()
                                      for name in source_paths}
            print("effect CLI parity sensitivity", json.dumps(report, sort_keys=True))
        self.assertEqual(before, report["source_after"])

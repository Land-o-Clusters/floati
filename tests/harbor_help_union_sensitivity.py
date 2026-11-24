"""Opt-in native producer faults for the transferred effect-help predicates."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests import test_effect_cli as subject


class HelpUnionSensitivity(unittest.TestCase):
    def test_four_real_producer_faults_reach_transferred_assertions(self):
        source = subject.REPOSITORY_ROOT
        paths = ("tests/test_cli.py", "tests/test_effect_cli.py",
                 "tests/harbor_slice_c_sensitivity.py", "floati/generated_help.py",
                 "floati/__main__.py", "floati/cli.py", "scripts/floati")
        before = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in paths}
        help_source = (source / "floati/generated_help.py").read_bytes()
        entry_source = (source / "floati/__main__.py").read_bytes()
        native_cli, native_popen = subject.EffectCliTests.run_cli, subprocess.Popen
        report = {"scope": "actual copied producer and native launcher; decoded text, no fabricated result",
                  "source_before": before, "runs": []}
        expected_topics = (("effects",), ("effect",), ("effect", "show"),
                           ("effect", "reconcile"), ("effect", "compensate"))

        def replace_page(page, needle, replacement):
            tree = ast.parse(help_source)
            assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                              and any(isinstance(target, ast.Name) and target.id == "PAGES"
                                      for target in node.targets))
            self.assertIsInstance(assignment.value, ast.Dict)
            values = [value for key, value in zip(assignment.value.keys, assignment.value.values)
                      if ast.literal_eval(key) == page]
            self.assertEqual(1, len(values))
            value = values[0]
            lines = help_source.splitlines(keepends=True)
            start = sum(map(len, lines[:value.lineno - 1])) + value.col_offset
            end = sum(map(len, lines[:value.end_lineno - 1])) + value.end_col_offset
            segment = help_source[start:end]
            self.assertEqual(1, segment.count(needle))
            changed = segment.replace(needle, replacement, 1)
            return help_source[:start] + changed + help_source[end:]

        faults = {
            "title": ("effect show", b"floati effect show - ", b"floati effect - "),
            "description": ("effects", b"DESCRIPTION\\n", b"\\n"),
            "examples": ("effects", b"EXAMPLES\\n", b"\\n"),
        }
        predicates = {
            "title": 'self.assertIn("floati " + " ".join(topic) + " - ", result.stdout)',
            "description": 'self.assertIn("DESCRIPTION\\n", result.stdout)',
            "examples": 'self.assertIn("EXAMPLES\\n", result.stdout)',
            "stderr": 'self.assertEqual("", result.stderr)',
        }
        pristine = None
        temporary_path = None
        try:
            with tempfile.TemporaryDirectory(prefix="help-union-") as temporary:
                private = Path(temporary)
                temporary_path = private
                report["private_source"] = str(private)
                shutil.copytree(source / "floati", private / "floati",
                                ignore=shutil.ignore_patterns("__pycache__"))
                (private / "scripts").mkdir()
                shutil.copy2(source / "scripts/floati", private / "scripts/floati")
                help_target, entry_target = private / "floati/generated_help.py", private / "floati/__main__.py"
                for label in ("pristine", "title", "description", "examples", "stderr", "restored"):
                    self.assertEqual(help_source, help_target.read_bytes())
                    self.assertEqual(entry_source, entry_target.read_bytes())
                    if label in faults:
                        help_target.write_bytes(replace_page(*faults[label]))
                    if label == "stderr":
                        needle = b"sys.exit(main())"
                        self.assertEqual(1, entry_source.count(needle))
                        entry_target.write_bytes(entry_source.replace(needle,
                            b'if sys.argv[1:] == ["effects", "--help"]:\n'
                            b'    print("help-union diagnostic", file=sys.stderr)\n'
                            b'sys.exit(main())', 1))
                    calls, children = [], []
                    case = subject.EffectCliTests(
                        "test_static_help_covers_every_effect_command_without_import_side_effects")
                    result = unittest.TestResult()

                    def observe_popen(*arguments, **keywords):
                        child = native_popen(*arguments, **keywords)
                        children.append(child)
                        return child

                    def observe_cli(fixture, *arguments):
                        actual = native_cli(fixture, *arguments)
                        calls.append({"argv": list(arguments), "returncode": actual.returncode,
                                      "stdout": actual.stdout, "stderr": actual.stderr})
                        return actual

                    producer = {"help_sha256": hashlib.sha256(help_target.read_bytes()).hexdigest(),
                                "entry_sha256": hashlib.sha256(entry_target.read_bytes()).hexdigest()}
                    try:
                        with mock.patch.object(subject, "LAUNCHER", private / "scripts/floati"),\
                                mock.patch.object(subject.EffectCliTests, "run_cli", observe_cli),\
                                mock.patch.object(subprocess, "Popen", observe_popen):
                            case.run(result)
                    finally:
                        help_target.write_bytes(help_source)
                        entry_target.write_bytes(entry_source)
                    row = {"label": label, "producer": producer, "calls": calls,
                           "children": [{"pid": child.pid, "args": child.args,
                                         "returncode": child.returncode} for child in children],
                           "failures": [(str(test), trace) for test, trace in result.failures],
                           "errors": [(str(test), trace) for test, trace in result.errors],
                           "skipped": result.skipped, "tests_run": result.testsRun,
                           "fixture_removed": not Path(case.temp.name).exists(),
                           "producer_restored": help_target.read_bytes() == help_source
                                                and entry_target.read_bytes() == entry_source}
                    report["runs"].append(row)
                    self.assertEqual(1, result.testsRun)
                    self.assertEqual([], result.errors)
                    self.assertEqual([], result.skipped)
                    self.assertEqual([(*topic, "--help") for topic in expected_topics],
                                     [tuple(call["argv"]) for call in calls])
                    self.assertEqual(5, len(children))
                    self.assertTrue(all(child.returncode == 0 for child in children))
                    self.assertTrue(all(call["returncode"] == 0 for call in calls))
                    self.assertTrue(row["fixture_removed"])
                    self.assertTrue(row["producer_restored"])
                    if label in predicates:
                        self.assertEqual(1, len(result.failures), result.failures)
                        failed, trace = result.failures[0]
                        topic = ("effect", "show") if label == "title" else ("effects",)
                        self.assertEqual({"topic": topic}, dict(failed.params))
                        self.assertIn(predicates[label], trace)
                        for call, baseline in zip(calls, pristine):
                            if tuple(call["argv"]) != (*topic, "--help"):
                                self.assertEqual(baseline, call)
                            elif label == "stderr":
                                self.assertEqual(baseline["stdout"], call["stdout"])
                                self.assertEqual("help-union diagnostic\n", call["stderr"])
                            else:
                                self.assertEqual("", call["stderr"])
                                self.assertNotEqual(baseline["stdout"], call["stdout"])
                    else:
                        self.assertEqual([], result.failures)
                        self.assertTrue(all(call["stderr"] == "" for call in calls))
                        if pristine is None:
                            pristine = calls
                        else:
                            self.assertEqual(pristine, calls)
        finally:
            report["private_source_removed"] = temporary_path is not None and not temporary_path.exists()
            report["source_after"] = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in paths}
            print("help union sensitivity", json.dumps(report, sort_keys=True))
        self.assertTrue(report["private_source_removed"])
        self.assertEqual(before, report["source_after"])

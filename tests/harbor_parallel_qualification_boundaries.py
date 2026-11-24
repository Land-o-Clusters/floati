"""Qualification must admit actual sidecars and charge controller completion.

The tiny owned repositories and import/tool routing fixtures are composed from
the existing qualification controls. They are not host performance evidence.
Only a collected phase's sidecar or a forbidden destination mkdir is
intercepted; native discovery, tests, journals, process collection and cleanup
stay real. Publication-inclusive timing belongs to the separately ruled
external supervisor and awaits that parent's API design.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

from tests import harbor_parallel_qualification as qualification_tests
from tests.temp_roots import REAL_TEMP_ROOT


COUNTERS = (
    "observe_calls", "observe_wall_ns", "observe_process_cpu_ns",
    "identity_attempts", "children_attempts", "witness_attempts",
    "journal_write_attempts", "journal_bytes_submitted",
)


def _allocation_identity(path):
    info = path.lstat()
    if (path != path.resolve(strict=True) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise AssertionError("owned qualification allocation is not ordinary private data: " + str(path))
    return info.st_dev, info.st_ino, info.st_uid, info.st_mode


class _OwnedPair:
    """Real producer and separately observed cleanup authority, never a receipt path."""

    def __init__(self, case, register_cleanup):
        self.case = case
        self.bank = Path(tempfile.mkdtemp(prefix="qualification-boundary-", dir=REAL_TEMP_ROOT))
        self.identity = _allocation_identity(self.bank)
        self.allocations = []
        self.capture_entries = []
        self.attempts = []
        self.snapshots = {}
        register_cleanup(self.cleanup)  # Before population or producer assertions.
        self.owner = qualification_tests.ParallelQualificationTests("runTest")
        self.owner.fixture(bank=self.bank)
        text = "import unittest\n"
        for ordinal in range(8):
            text += ("class Case%d(unittest.TestCase):\n"
                     " def test_one(self): pass\n") % ordinal
        (self.owner.root / "test_cases.py").write_text(text)
        self.owner.commit()
        self.helper = self.owner.helper()
        self.output = self.bank / "pair"
        self.source = copy.deepcopy(self.helper._launcher(self.owner.root).source_identity(self.owner.root))
        self.registry_bytes = self.owner.registry.read_bytes()
        self.intercepted = []

    def cleanup(self):
        # Both paths originate in actual allocators. No returned scratch_root
        # can add removal authority. Missing native proof retains every bank.
        safe = (all(entry.get("returned") and entry.get("exit_code") == 0
                    for entry in self.capture_entries)
                and all(row.get("collected") and row.get("exit_code") == 0
                   and row["phase"] in self.snapshots
                   and self.snapshots[row["phase"]]["report"]["cleanup"]["successful"]
                   for row in self.attempts))
        if not safe:
            raise AssertionError("retained unproved native fixture: " + str(self.bank))
        for path, identity in self.allocations:
            if os.path.lexists(path):
                if _allocation_identity(path) != identity:
                    raise AssertionError("retained changed scratch allocation: " + str(path))
                shutil.rmtree(path)
            if os.path.lexists(path):
                raise AssertionError("owned scratch cleanup did not complete: " + str(path))
        if os.path.lexists(self.bank):
            if _allocation_identity(self.bank) != self.identity:
                raise AssertionError("retained changed fixture allocation: " + str(self.bank))
            shutil.rmtree(self.bank)

    def _freeze_phase(self, launch):
        case = self.case
        phase = launch["phase"]
        final = self.output / phase / "final.json"
        raw = final.read_bytes()
        report = json.loads(raw)
        request_raw = (final.parent / "request.json").read_bytes()
        request = json.loads(request_raw)
        case.assertTrue(launch["collected"])
        case.assertEqual(0, launch["exit_code"])
        case.assertEqual("complete", report["status"])
        case.assertTrue(report["successful"])
        case.assertTrue(report["cleanup"]["successful"])
        case.assertEqual(8, report["aggregate"]["tests_run"])
        case.assertEqual(self.source, report["source"])
        case.assertEqual(self.owner.profile["interpreter"], launch["argv"][0])
        # macOS /usr/bin/python3 can be a dispatch stub rather than the native
        # executable. The actual unmodified routing probe supplies that identity.
        case.assertEqual(str(Path(self.preflight["stages"]["controller"]["interpreter"]).resolve()),
                         report["interpreter"]["executable"])
        case.assertEqual({"mode": "serial" if phase == "serial" else "run",
                          "source": self.source, "interpreter": report["interpreter"]}, request)
        if phase == "parallel":
            case.assertEqual(8, report["workers"])
            case.assertTrue(report["qualified"])
            case.assertTrue(report["comparison"]["equivalent"])
        cost = self.helper._validate_cost(final, report, launch, self.source)
        cost_path = final.parent / "process-cost.json"
        info = cost_path.lstat()
        sidecar_raw = cost_path.read_bytes()
        case.assertEqual(hashlib.sha256(sidecar_raw).hexdigest(), cost["sha256"])
        self.snapshots[phase] = {
            "report": copy.deepcopy(report), "launch": copy.deepcopy(launch),
            "source": copy.deepcopy(self.source), "final": final, "final_raw": raw,
            "request_raw": request_raw, "raw": sidecar_raw, "cost": copy.deepcopy(cost),
            "identity": (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink),
        }

    def run(self, interception=None):
        capture = self.helper._capture
        allocate = self.helper.tempfile.mkdtemp
        preflight = self.helper.preflight

        def observed_preflight(*args, **kwargs):
            result = preflight(*args, **kwargs)
            self.preflight = copy.deepcopy(result)
            return result

        def allocated(*args, **kwargs):
            value = allocate(*args, **kwargs)
            if kwargs.get("prefix") == "qualification-pair-":
                path = Path(value)
                self.allocations.append((path, _allocation_identity(path)))
            return value

        def captured(argv, **keywords):
            record = keywords.get("record")
            entry = {"phase": None if record is None else record["phase"], "returned": False}
            self.capture_entries.append(entry)
            if record is not None:
                self.attempts.append(record)
            # No-record routing/tool probes also launch real children. Only a
            # normal return from the native collector proves their collection;
            # an exception or interrupted call retains the bank fail-closed.
            result = capture(argv, **keywords)
            entry.update(returned=True, exit_code=result[0])
            if record is not None:
                self._freeze_phase(record)
                # Freeze collection before either corruption or controller edits.
                self.attempts[-1] = copy.deepcopy(record)
                if interception is not None and record["phase"] == interception[0]:
                    snapshot = self.snapshots[record["phase"]]
                    path = snapshot["final"].parent / "process-cost.json"
                    interception[1](path, json.loads(snapshot["raw"]), snapshot["raw"])
                    self.intercepted.append(path)
            return result

        with (self.owner.registry_patch(self.helper),
              mock.patch.object(self.helper.tempfile, "mkdtemp", allocated),
              mock.patch.object(self.helper, "preflight", observed_preflight),
              mock.patch.object(self.helper, "_capture", captured)):
            receipt = self.helper.run_pair(self.owner.root, self.output)
        self.case.assertEqual(1, len(self.allocations))
        self.case.assertFalse(os.path.lexists(self.allocations[0][0]),
                              "native controller retained scratch: " + str(receipt.get("error")))
        self.case.assertTrue(receipt["cleanup"]["successful"])
        self.case.assertEqual(self.registry_bytes, self.owner.registry.read_bytes())
        self.case.assertEqual(self.source, self.helper._launcher(self.owner.root).source_identity(self.owner.root))
        self.case.assertEqual(self.source, receipt["source_after"])
        self.case.assertEqual(receipt["registry"], receipt["registry_after"])
        print("native boundary launches", [(row["phase"], row["pid"], row["collected"], row["exit_code"])
                                           for row in self.attempts])
        return receipt

    def pristine(self, phase):
        snapshot = self.snapshots[phase]
        final = snapshot["final"]
        path = final.parent / "process-cost.json"
        self.case.assertEqual(snapshot["raw"], path.read_bytes())
        info = path.lstat()
        self.case.assertEqual(snapshot["identity"],
                              (info.st_dev, info.st_ino, info.st_uid, info.st_mode, info.st_nlink))
        self.case.assertEqual(snapshot["final_raw"], final.read_bytes())
        self.case.assertEqual(snapshot["request_raw"], (final.parent / "request.json").read_bytes())
        self.case.assertEqual(snapshot["cost"], self.helper._validate_cost(
            final, copy.deepcopy(snapshot["report"]), copy.deepcopy(snapshot["launch"]),
            copy.deepcopy(snapshot["source"])))
        return path, snapshot


class ParallelQualificationBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = None
        cls.poisoned = None
        cls.seed_lock = threading.RLock()

    def native_seed(self):
        cls = type(self)
        self.assertIsNone(cls.poisoned, "native data seed is poisoned: " + str(cls.poisoned))
        if cls.seed is None:
            try:
                seed = _OwnedPair(self, cls.addClassCleanup)
                receipt = seed.run()
                self.assertEqual("complete", receipt["status"])
                self.assertTrue(receipt["successful"])
                self.assertTrue(receipt["qualified"])
                self.assertEqual(["serial", "parallel"], [row["phase"] for row in seed.attempts])
                for phase in ("serial", "parallel"):
                    seed.pristine(phase)
                cls.seed = seed
            except BaseException as error:
                cls.poisoned = repr(error)
                raise
        # The current case owns assertion diagnostics; stored evidence is independent.
        cls.seed.case = self
        return cls.seed

    def fixture(self):
        # Composition prevents unittest from collecting the existing ten tests
        # a second time through an imported or inherited TestCase class.
        owner = qualification_tests.ParallelQualificationTests("runTest")
        self.addCleanup(owner.doCleanups)
        owner.fixture()
        text = "import unittest\n"
        for ordinal in range(8):
            text += ("class Case%d(unittest.TestCase):\n"
                     " def test_one(self): pass\n") % ordinal
        (owner.root / "test_cases.py").write_text(text)
        owner.commit()
        return owner, owner.helper()

    def refused_sidecar(self, phase, mutation, *, error=ValueError,
                        reason="native process-cost sidecar schema or phase binding is invalid",
                        missing=False):
        with type(self).seed_lock:
            seed = self.native_seed()
            try:
                path, snapshot = seed.pristine(phase)
            except BaseException as failure:
                type(self).poisoned = repr(failure)
                raise
            backup = path.with_name("process-cost.saved")
            self.assertFalse(os.path.lexists(backup))
            try:
                if missing:
                    path.rename(backup)
                else:
                    mutation(path, json.loads(snapshot["raw"]), snapshot["raw"])
                with self.assertRaisesRegex(error, reason):
                    seed.helper._validate_cost(snapshot["final"], copy.deepcopy(snapshot["report"]),
                                               copy.deepcopy(snapshot["launch"]), copy.deepcopy(snapshot["source"]))
                print("native-backed rejection", str(self._subtest or self), phase,
                      error.__name__, reason)
            finally:
                try:
                    if missing:
                        backup.rename(path)
                    else:
                        path.write_bytes(snapshot["raw"])
                    seed.pristine(phase)
                except BaseException as failure:
                    type(self).poisoned = repr(failure)
                    raise

    def controller_rejects(self, phase, mutation, reason):
        producer = _OwnedPair(self, self.addCleanup)
        receipt = producer.run((phase, mutation))
        self.assertEqual([producer.output / phase / "process-cost.json"], producer.intercepted)
        expected = ["serial"] if phase == "serial" else ["serial", "parallel"]
        self.assertEqual(expected, [row["phase"] for row in producer.attempts])
        self.assertEqual(expected, [row["phase"] for row in receipt["launches"]])
        if phase == "serial":
            self.assertFalse((producer.output / "parallel").exists())
        for label, document in (("returned", receipt), ("persisted", json.loads(
                (producer.output / "qualification.json").read_bytes()))):
            self.assertEqual("invalid", document["status"], label + " cost refusal status")
            self.assertFalse(document["successful"], label + " invalid cost succeeded")
            self.assertFalse(document["qualified"], label + " invalid cost qualified")
            self.assertIn(reason, document["error"], label + " wrong cost refusal")
        print("controller rejection", phase, reason, "persisted invalid/unqualified")

    @staticmethod
    def replace_field(field, value):
        def replace(path, document, raw):
            document[field] = value(document) if callable(value) else value
            path.write_text(json.dumps(document) + "\n")
        return replace

    def test_absent_phase_cost_cannot_qualify_completed_native_work(self):
        for phase in ("serial", "parallel"):
            with self.subTest(phase=phase):
                self.refused_sidecar(phase, None, error=FileNotFoundError,
                                     reason="process-cost.json", missing=True)
        self.controller_rejects("serial", lambda path, document, raw: path.unlink(), "FileNotFoundError")

    def test_malformed_and_duplicate_key_cost_cannot_qualify(self):
        variants = (
            ("invalid-json", lambda path, document, raw: path.write_bytes(b"{not-json\n")),
            ("duplicate-key", lambda path, document, raw: path.write_bytes(
                b'{"availability":"available",' + raw.lstrip()[1:])),
            ("non-object", lambda path, document, raw: path.write_bytes(b"[]\n")),
        )
        for label, mutation in variants:
            with self.subTest(variant=label):
                error, reason = {
                    "invalid-json": (json.JSONDecodeError, "Expecting property name"),
                    "duplicate-key": (ValueError, "duplicate registry/receipt key: availability"),
                    "non-object": (ValueError, "native process-cost sidecar schema or phase binding is invalid"),
                }[label]
                self.refused_sidecar("parallel", mutation, error=error, reason=reason)

    def test_source_interpreter_request_and_final_path_bindings_are_required(self):
        variants = (
            ("source", self.replace_field("source", lambda row: {
                **row["source"], "digest": "0" * 64})),
            ("interpreter", self.replace_field("interpreter", lambda row: {
                **row["interpreter"], "executable": "/unselected/python"})),
            ("request", self.replace_field("request_sha256", "0" * 64)),
            ("final-path", self.replace_field("final_path", "/unselected/final.json")),
        )
        for label, mutation in variants:
            with self.subTest(binding=label):
                self.refused_sidecar("parallel", mutation)
        self.controller_rejects("parallel", self.replace_field("request_sha256", "0" * 64),
                                "native process-cost sidecar schema or phase binding is invalid")

    def test_launcher_pid_must_be_strict_integer_matching_collected_process(self):
        for label, value in (("different-pid", lambda row: row["launcher_pid"] + 1),
                             ("boolean", True), ("negative", -1), ("string", "123")):
            with self.subTest(variant=label):
                self.refused_sidecar("parallel", self.replace_field("launcher_pid", value))

    def test_each_cost_counter_requires_non_boolean_nonnegative_integer(self):
        for counter in COUNTERS:
            with self.subTest(counter=counter, variant="boolean"):
                self.refused_sidecar("parallel", self.replace_field("cost", lambda row: {
                    **row["cost"], counter: True}), reason="native process-cost counters or declared scope is invalid")
        for value in (-1, 1.5, "1"):
            with self.subTest(counter="observe_calls", variant=value):
                self.refused_sidecar("parallel", self.replace_field("cost", lambda row: {
                    **row["cost"], "observe_calls": value}), reason="native process-cost counters or declared scope is invalid")

    def test_unavailable_incomplete_or_missing_cost_evidence_cannot_qualify(self):
        variants = (
            ("unavailable", self.replace_field("availability", "unavailable")),
            ("native-incomplete", self.replace_field("native_status", "invalid")),
            ("missing-cost", self.replace_field("cost", None)),
        )
        for label, mutation in variants:
            with self.subTest(variant=label):
                reason = ("native process-cost counters or declared scope is invalid" if label == "missing-cost"
                          else "native process-cost sidecar schema or phase binding is invalid")
                self.refused_sidecar("parallel", mutation, reason=reason)

    def test_cost_artifact_and_metrics_schema_versions_are_required(self):
        variants = (
            ("artifact-version", self.replace_field("schema_version", 2)),
            ("boolean-version", self.replace_field("schema_version", True)),
            ("kind", self.replace_field("kind", "unrelated_cost")),
            ("metrics-version", self.replace_field("cost", lambda row: {
                **row["cost"], "schema_version": 2})),
        )
        for label, mutation in variants:
            with self.subTest(variant=label):
                reason = ("native process-cost counters or declared scope is invalid" if label == "metrics-version"
                          else "native process-cost sidecar schema or phase binding is invalid")
                self.refused_sidecar("parallel", mutation, reason=reason)

    def test_interpreter_is_derived_from_immutable_profile_not_a_literal_host_path(self):
        owner, helper = self.fixture()
        owner.profile["interpreter"] = str(Path(sys.executable).resolve(strict=True))
        owner.write_registry()
        with owner.registry_patch(helper):
            try:
                profile = helper.read_profile()
            except ValueError as error:
                self.fail("canonical immutable-profile interpreter was refused: " + str(error))
        self.assertEqual(owner.profile["interpreter"], profile["interpreter"])

    def test_non_private_output_is_refused_before_any_destination_write(self):
        owner, helper = self.fixture()
        # This coordinate is outside OS private temp and outside the fixture
        # source. An interception prevents a broken admission path writing to
        # the actual checkout's parent while still exposing the wrong branch.
        outside = qualification_tests.SOURCE.parent / ("qualification-forbidden-" + owner.bank.name)
        self.assertFalse(outside.exists())
        attempted = []
        mkdir = Path.mkdir

        def guard(path, *arguments, **keywords):
            if path == outside or outside in path.parents:
                attempted.append(path)
                raise ValueError("test intercepted forbidden destination write")
            return mkdir(path, *arguments, **keywords)

        with owner.registry_patch(helper), mock.patch.object(Path, "mkdir", guard):
            with self.assertRaises(ValueError):
                helper.run_pair(owner.root, outside)
        self.assertEqual([], attempted, "output admission attempted a non-private destination write")
        self.assertFalse(outside.exists())

if __name__ == "__main__":
    unittest.main()

"""NET-LATENCY-1: measured warm ask receipts, using scratch fleets only."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from tests.banked_sha_fixture import banked_send_repository
from tests.readiness import run_process
from tests.temp_roots import REAL_TEMP_ROOT


SOURCE = Path(__file__).resolve().parents[1]
SCRIPT = SOURCE / "scripts/measure-ask-latency.py"


def _load_instrument():
    if not SCRIPT.is_file():
        raise AssertionError("warm ask latency must have a runnable receipt instrument")
    spec = importlib.util.spec_from_file_location("measure_ask_latency", SCRIPT)
    instrument = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(instrument)
    return instrument


class _LatencyFleet:
    """One scratch fleet for read-only refusals or the explicit provider self-test."""

    def __init__(self, base):
        self.base = base
        self.home = self.base / "home"
        self.home.mkdir()
        self.fleet = self.base / "fleet"
        self.repository, self.sha = banked_send_repository(self.base)
        self.environment = dict(os.environ, HOME=str(self.home),
                                PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(SOURCE))
        for argv in (("init",), ("node", "add", "--node", "caller", "--harness", "Codex",
                                "--lifetime", "permanent"),
                     ("node", "add", "--node", "served", "--harness", "Codex",
                      "--lifetime", "permanent")):
            result = subprocess.run(
                ["/usr/bin/python3", "-B", "-m", "floati", *argv, "--root", str(self.fleet)],
                cwd=self.repository, env=self.environment, capture_output=True, text=True)
            if result.returncode != 0:
                raise AssertionError(result.stdout + result.stderr)


def _latency_argv(fixture, out):
    return ["--root", str(fixture.fleet), "--from", "caller", "--to", "served",
            "--repository", str(fixture.repository), "--repo", "fixture", "--doc", "note.md",
            "--session", "fixture-caller", "--harness", "fake-python", "--model", "fixed-delay",
            "--out", str(out), "--n", "3", "--wait-seconds", "3"]


class NetLatencyPureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instrument = _load_instrument()

    def test_constructed_statistics_pin_median_and_nearest_rank_p95(self):
        stats = self.instrument.statistics_for(list(range(20, 0, -1)))
        self.assertEqual({"n": 20, "median_seconds": 10.5, "p95_seconds": 19,
                          "min_seconds": 1, "max_seconds": 20}, stats)
        self.assertEqual(2, self.instrument.statistics_for([2])["p95_seconds"])

    def test_invalid_samples_cannot_be_published_as_measurements(self):
        for samples in ([], [-1], [float("nan")], [float("inf")]):
            with self.subTest(samples=samples), self.assertRaises(self.instrument.LatencyRefusal):
                self.instrument.statistics_for(samples)

    def test_git_redirectors_cannot_change_the_reported_source(self):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as temporary:
            repository, sha = banked_send_repository(Path(temporary))
            with mock.patch.dict(os.environ, {"GIT_DIR": str(repository / ".git"),
                                              "GIT_WORK_TREE": str(repository)}):
                self.assertNotEqual(sha, self.instrument.git(SOURCE, "rev-parse", "HEAD"))

    def test_default_population_is_twenty(self):
        args = self.instrument.parser().parse_args([
            "--root", "unused-fleet", "--from", "caller", "--to", "served",
            "--session", "fixture", "--harness", "fake", "--model", "delay",
            "--out", "unused-output"])
        self.assertEqual(20, args.n)


class NetLatencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        cls.addClassCleanup(temporary.cleanup)
        cls.fixture = _LatencyFleet(Path(temporary.name))
        cls.instrument = _load_instrument()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.out = self.base / "receipts"
        self.out.mkdir()

    def arguments(self, *extra):
        return self.instrument.parser().parse_args([*self._argv_for_script(), *extra])

    def test_cli_unserved_refusal_is_typed_and_leaves_no_speed_receipt(self):
        result = run_process([
            "/usr/bin/python3", "-B", str(SCRIPT), *self._argv_for_script(),
            "--wait-seconds", "0"], "ask latency CLI returns its refusal",
            cwd=self.fixture.repository, env=self.fixture.environment, capture_output=True, text=True)
        self.assertEqual(20, result.returncode, result.stderr)
        artifact = json.loads(result.stdout)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("latency_node_not_served", artifact["evidence"]["code"])
        self.assertEqual([], list(self.out.iterdir()))

    def test_warmup_is_discarded_and_every_timed_answer_is_named(self):
        answers = [{"outcome": "answered", "seconds": seconds, "question_id": "q" + str(i),
                    "answer_id": "a" + str(i)} for i, seconds in enumerate([9, 1, 2, 3])]
        with mock.patch.object(self.instrument, "ask_once", side_effect=answers) as ask:
            receipt = self.instrument.measure(self.arguments(),
                load=mock.Mock(side_effect=[(1, 2, 3), (4, 5, 6)]))
        self.assertEqual(4, ask.call_count)
        self.assertEqual(2, receipt["median_seconds"])
        self.assertEqual(3, receipt["p95_seconds"])
        self.assertEqual([1, 2, 3], [row["seconds"] for row in receipt["samples"]])
        self.assertEqual("q0", receipt["warmup"]["question_id"])
        self.assertEqual("a3", receipt["samples"][-1]["answer_id"])
        self.assertEqual((1, 4), (receipt["load1_start"], receipt["load1_end"]))
        self.assertEqual(("fake-python", "fixed-delay", "served"),
                         (receipt["harness"], receipt["model"], receipt["served_node"]))
        self.assertRegex(receipt["source_sha"], r"^[0-9a-f]{40}$")

    def assert_refusal(self, code, args, *, answers=None):
        with mock.patch.object(self.instrument, "ask_once", side_effect=answers) as ask:
            with self.assertRaises(self.instrument.LatencyRefusal) as caught:
                self.instrument.measure(args)
        self.assertEqual(code, caught.exception.code)
        self.assertEqual([], list(self.out.iterdir()))
        return ask

    def test_unregistered_node_refuses_before_any_ask(self):
        ask = self.assert_refusal("latency_node_not_served", self.arguments("--to", "absent"))
        ask.assert_not_called()

    def test_symlink_root_is_not_the_named_root(self):
        alias = self.base / "alias"
        alias.symlink_to(self.fixture.fleet, target_is_directory=True)
        ask = self.assert_refusal("latency_root_not_named", self.arguments("--root", str(alias)))
        ask.assert_not_called()

    def test_failed_warmup_is_not_a_speed_receipt(self):
        for evidence, code in (({"outcome": "UNKNOWN"}, "latency_node_not_served"),
                               ({"outcome": "declined"}, "latency_node_not_warm"),
                               ({"outcome": "answered"}, "latency_node_not_warm")):
            with self.subTest(evidence=evidence):
                self.assert_refusal(code, self.arguments(), answers=[evidence])

    def test_lost_warmth_aborts_without_success_receipt(self):
        self.assert_refusal("latency_node_not_warm", self.arguments(), answers=[
            {"outcome": "answered", "question_id": "q", "answer_id": "a"},
            {"outcome": "UNKNOWN"}])

    def _argv_for_script(self):
        return _latency_argv(self.fixture, self.out)


class ClaudeHookLatencyTests(unittest.TestCase):
    """The hook comparison consumes explicit session observations, not flushes."""

    SCRIPT = SOURCE / "scripts/measure-claude-hook-latency.py"

    def invoke(self, *arguments):
        result = run_process(
            ["/usr/bin/python3", "-B", str(self.SCRIPT), *arguments],
            "hook latency instrument returns its artifact",
            cwd=SOURCE, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            capture_output=True, text=True)
        self.assertTrue(result.stdout, result.stderr)
        return result.returncode, json.loads(result.stdout)

    @staticmethod
    def observations():
        rows = []
        for arm, latency in (("hook", 300_000_000), ("stop", 800_000_000)):
            common = {"schema_version": 1, "run_id": "fixture-run",
                      "clock_id": "fixture-host-monotonic", "source_sha": "a" * 40,
                      "arm": arm, "session_id": "fixture-" + arm}
            message = {"item_id": "message-" + arm,
                       "delivery_receipt_id": "delivery-" + arm}
            rows.extend([
                dict(common, kind="sent", item_id=message["item_id"], monotonic_ns=1_000_000_000),
                dict(common, **message, kind="presentation_flushed", monotonic_ns=1_100_000_000),
                dict(common, **message, kind="session_observed",
                     basis="session_model_action", monotonic_ns=1_000_000_000 + latency),
                dict(common, kind="idle_cpu", interval_id="idle-1", phase="start",
                     monotonic_ns=2_000_000_000, cpu_source="explicit-observer-receipt.json",
                     processes=[{"pid": 101, "identity": "fixture-process-start",
                                 "user_seconds": 1.0, "system_seconds": 0.5}]),
                dict(common, kind="idle_cpu", interval_id="idle-1", phase="end",
                     monotonic_ns=12_000_000_000, cpu_source="explicit-observer-receipt.json",
                     processes=[{"pid": 101, "identity": "fixture-process-start",
                                 "user_seconds": 1.2, "system_seconds": 0.6}]),
            ])
        return rows

    def analyze(self, rows):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as directory:
            trace = Path(directory) / "observations.jsonl"
            trace.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            return self.invoke("--observations", str(trace))

    def test_dry_run_explains_both_arms_without_claiming_live_measurement(self):
        code, artifact = self.invoke("--dry-run")
        self.assertEqual(0, code)
        self.assertEqual("ok", artifact["status"])
        evidence = artifact["evidence"]
        self.assertEqual("dry_run", evidence["mode"])
        self.assertFalse(evidence["live_measurement_performed"])
        self.assertEqual(["hook", "doorbell", "stop"], evidence["arms"])
        self.assertEqual({"sent", "doorbell_sent", "presentation_flushed", "session_observed", "idle_cpu"},
                         {row["kind"] for row in evidence["example_observations"]})

    def test_doorbell_latency_requires_the_correlated_presentation_receipt(self):
        for variant in ("complete", "all_arms", "missing_doorbell", "early_doorbell",
                        "late_doorbell", "foreign_item", "wrong_receipt",
                        "missing_presentation", "missing_idle_cpu"):
            with self.subTest(variant=variant):
                rows = self.observations()
                for row in rows:
                    if row["arm"] == "hook":
                        row["arm"] = "doorbell"
                bell = dict(rows[0], kind="doorbell_sent", monotonic_ns=1_020_000_000)
                if variant != "missing_doorbell":
                    rows.append(bell)
                if variant == "all_arms":
                    rows.extend(row for row in self.observations() if row["arm"] == "hook")
                elif variant == "early_doorbell":
                    bell["monotonic_ns"] = 900_000_000
                elif variant == "late_doorbell":
                    bell["monotonic_ns"] = 1_200_000_000
                elif variant == "foreign_item":
                    bell["item_id"] = "another-message"
                elif variant == "wrong_receipt":
                    next(row for row in rows if row["arm"] == "doorbell"
                         and row["kind"] == "session_observed")["delivery_receipt_id"] = "another-receipt"
                elif variant == "missing_presentation":
                    rows = [row for row in rows if not (
                        row["arm"] == "doorbell" and row["kind"] == "presentation_flushed")]
                elif variant == "missing_idle_cpu":
                    rows = [row for row in rows if not (
                        row["arm"] == "doorbell" and row["kind"] == "idle_cpu")]
                code, artifact = self.analyze(rows)
                if variant not in ("complete", "all_arms"):
                    self.assertEqual(20, code)
                    self.assertEqual("refused", artifact["status"])
                    continue
                self.assertEqual(0, code)
                result = artifact["evidence"]["arms"]["doorbell"]
                self.assertEqual({"hook", "doorbell", "stop"} if variant == "all_arms"
                                 else {"doorbell", "stop"}, set(artifact["evidence"]["arms"]))
                self.assertAlmostEqual(0.08, result["doorbell_presentation_latency"]["median_seconds"])
                self.assertAlmostEqual(0.3, result["session_latency"]["median_seconds"])
                self.assertAlmostEqual(0.03, result["idle_cpu"]["mean_cores"])

    def test_session_latency_and_idle_cpu_derive_from_correlated_external_observations(self):
        code, artifact = self.analyze(self.observations())
        self.assertEqual(0, code)
        evidence = artifact["evidence"]
        self.assertEqual("architect_observations", evidence["measurement_basis"])
        for arm, latency in (("hook", 0.3), ("stop", 0.8)):
            with self.subTest(arm=arm):
                result = evidence["arms"][arm]
                self.assertEqual(1, result["session_latency"]["n"])
                self.assertAlmostEqual(latency, result["session_latency"]["median_seconds"])
                self.assertAlmostEqual(0.1, result["presentation_latency"]["median_seconds"])
                self.assertAlmostEqual(0.3, result["idle_cpu"]["cpu_seconds"])
                self.assertAlmostEqual(10, result["idle_cpu"]["wall_seconds"])
                self.assertAlmostEqual(0.03, result["idle_cpu"]["mean_cores"])

    def test_flush_without_session_observation_cannot_be_published_as_delivery_latency(self):
        rows = [row for row in self.observations() if row["kind"] != "session_observed"]
        code, artifact = self.analyze(rows)
        self.assertEqual(20, code)
        self.assertEqual("refused", artifact["status"])
        self.assertEqual("claude_latency_observation_incomplete", artifact["evidence"]["code"])

    def test_mixed_clocks_nonfinite_values_and_unproved_cpu_population_are_refused(self):
        for variant in ("clock", "timestamp", "cpu", "population", "cpu_source", "session"):
            with self.subTest(variant=variant):
                rows = self.observations()
                if variant == "clock":
                    rows[2]["clock_id"] = "another-host"
                elif variant == "timestamp":
                    rows[2]["monotonic_ns"] = float("nan")
                elif variant == "cpu":
                    rows[4]["processes"][0]["user_seconds"] = float("inf")
                elif variant == "population":
                    rows[4]["processes"][0]["identity"] = "different-process-start"
                elif variant == "cpu_source":
                    rows[4]["cpu_source"] = ""
                else:
                    rows[2]["session_id"] = "foreign-session"
                code, artifact = self.analyze(rows)
                self.assertEqual(20, code)
                self.assertEqual("refused", artifact["status"])


if __name__ == "__main__":
    unittest.main()

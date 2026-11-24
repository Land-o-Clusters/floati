"""Landing budgets use exact executed IDs and measured case seconds."""

import copy
from contextlib import redirect_stderr, redirect_stdout
from functools import lru_cache
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.parallel_plan import make_plan, reconcile

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/test-cost-gate.py"


class TestCostGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.root = Path(cls.scratch.name)
        spec = importlib.util.spec_from_file_location("scripts.test_cost_gate", SCRIPT)
        cls.gate = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.gate)
        # All queried refs are immutable commit IDs in this shared Git fixture.
        # Reuse their real Git answers across property variants.
        cls.gate.git = lru_cache(maxsize=None)(cls.gate.git)
        cls.git("init", "-q")
        cls.git("config", "user.email", "fixture@example.invalid")
        cls.git("config", "user.name", "Fixture")
        (cls.root / "scripts").mkdir()
        cls.registry = cls.root / "scripts/test-cost-registry.csv"
        cls.registry.write_text("id,seconds,reason,owner\n")
        (cls.root / "scripts/test-cost-allowance.json").write_text(
            '{"train_seconds": 0}\n')
        cls.git("add", "scripts")
        cls.git("commit", "-qm", "Empty budget policy")
        cls.base = cls.git("rev-parse", "HEAD")
        cls.registry.write_text("id,seconds,reason,owner\nslow,6,Required integration,Maintainer\n")
        cls.git("commit", "-qam", "Register integration cost")
        cls.registered = cls.git("rev-parse", "HEAD")
        (cls.root / "scripts/test-cost-allowance.json").write_text(
            '{"train_seconds": 2}\n')
        cls.git("commit", "-qam", "Allow measured train cost")
        cls.allowed = cls.git("rev-parse", "HEAD")
        (cls.root / "scripts/test-cost-allowance.json").write_text(
            '{"train_seconds": 1000}\n')

    @classmethod
    def git(cls, *args):
        return subprocess.check_output(["/usr/bin/git", *args], cwd=cls.root,
                                       stderr=subprocess.PIPE, text=True).strip()

    def receipt(self, times, ref=None):
        ids = list(times)
        digest = hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()
        census = {"ids": ids, "population_digest": digest,
                  "units": [{"key": "fixture", "ids": ids,
                             "classes": ["fixture.Case"], "serial_reason": None}],
                  "membership": {i: {"class": "fixture.Case", "module": "fixture",
                                     "origin": "fixture"} for i in ids}}
        result = {"status": "complete", "selected_ids": ids,
                             "started_ids": ids, "stopped_ids": ids,
                             "fixture_blocked_ids": [], "tests_run": len(ids),
                             "source": {"head": ref or self.base},
                             "test_durations": times, "key": "worker-1",
                             "population_digest": digest, "duration_seconds": sum(times.values()),
                             "successful": True,
                             "events": [{"kind": "success", "test_id": i} for i in ids]}
        plan = make_plan(census, 1)
        return {"status": "complete", "successful": True,
                "load_before": [4, 5, 6], "load_after": [5, 6, 7],
                "source": {"head": ref or self.base}, "census": census,
                "plan": plan, "results": [result], "aggregate": reconcile(census, plan, [result])}

    def run_gate(self, mode, baseline, current, *extra):
        with tempfile.TemporaryDirectory() as folder:
            before, after = (Path(folder) / name for name in ("base.json", "car.json"))
            before.write_text(json.dumps(baseline))
            after.write_text(json.dumps(current))
            args = [mode, "--baseline", str(before), "--timing", str(after),
                    "--policy-ref", current["source"]["head"]]
            if mode == "car":
                args += ["--base-ref", baseline["source"]["head"],
                         "--car-ref", current["source"]["head"]]
            else:
                args += ["--car", "example", str(before), str(after)]
            previous = Path.cwd()
            output = io.StringIO()
            info = io.StringIO()
            try:
                os.chdir(self.root)
                with redirect_stdout(output), redirect_stderr(info):
                    code = self.gate.main([*args, *extra])
            finally:
                os.chdir(previous)
            self.last_info = info.getvalue()
            self.assertIn(code, (0, 1, 2))
            return code, json.loads(output.getvalue())

    def test_unregistered_offenders_are_counted_and_named(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                times = {"kept": 1, **{"slow" + str(i): 6 for i in range(count)}}
                code, report = self.run_gate("car", self.receipt({"kept": 1}),
                                             self.receipt(times))
                self.assertEqual(bool(count), bool(code))
                self.assertEqual(["slow" + str(i) for i in range(count)],
                                 [row["id"] for row in report["offending_tests"]])
                self.assertEqual(count, report["offending_count"])

    def test_committed_registration_passes_and_working_copy_cannot_grant_it(self):
        for ref, expected in ((self.base, 1), (self.registered, 0)):
            with self.subTest(ref=ref):
                code, report = self.run_gate("car", self.receipt({"kept": 1}),
                    self.receipt({"kept": 1, "slow": 6}, ref))
                self.assertEqual(expected, code)
                self.assertEqual([{"id": "slow", "seconds": 6}], report["added"])

    def test_car_total_refuses_61_seconds_even_with_small_cases(self):
        times = {"new" + str(i): 1 for i in range(61)}
        code, report = self.run_gate("car", self.receipt({"kept": 1}),
                                     self.receipt({"kept": 1, **times}))
        self.assertEqual(1, code)
        self.assertEqual(61, report["added_seconds"])
        self.assertEqual(0, report["offending_count"])
        self.assertIn("car_budget_exceeded", report["reasons"])
        self.assertEqual(set(times), {row["id"] for row in report["added"]})

    def test_id_diff_catches_equal_count_replacement_and_exact_boundaries(self):
        code, report = self.run_gate("car", self.receipt({"old": 1}),
                                     self.receipt({"new": 5}))
        self.assertEqual(0, code)
        self.assertEqual(["old"], report["removed_ids"])
        self.assertEqual([{"id": "new", "seconds": 5}], report["added"])
        code, report = self.run_gate("car", self.receipt({"kept": 1}),
                                     self.receipt({"kept": 1, **{str(i): 5 for i in range(12)}}))
        self.assertEqual(0, code)
        self.assertEqual(60, report["added_seconds"])

    def test_train_rise_refuses_by_values_and_reports_each_car(self):
        for seconds, expected in ((0, 0), (1, 0), (2, 1)):
            with self.subTest(seconds=seconds):
                code, report = self.run_gate("train", self.receipt({"kept": 1}),
                                             self.receipt({"new": seconds}))
                self.assertEqual(expected, code)
                self.assertEqual(seconds - 1, report["delta_seconds"])
                self.assertEqual(0, report["allowance_seconds"])
                self.assertEqual(1, report["cars"][0]["added_count"])
                self.assertEqual(seconds, report["cars"][0]["added_seconds"])
                self.assertEqual(1, report["cars"][0]["removed_seconds"])
                self.assertEqual(1, report["added_count"])
                self.assertEqual(1, report["removed_count"])

    def test_committed_allowance_is_used_at_its_exact_boundary(self):
        for seconds, expected in ((3, 0), (4, 1)):
            with self.subTest(seconds=seconds):
                code, report = self.run_gate("train", self.receipt({"kept": 1}),
                    self.receipt({"kept": seconds}, self.allowed))
                self.assertEqual(expected, code)
                self.assertEqual(2, report["allowance_seconds"])

    def test_train_comparison_requires_both_receipts_quiet_at_both_ends(self):
        cases = [("quiet boundary", None, None, 1, "refused", "train_budget_exceeded")]
        for side in ("baseline", "timing"):
            for endpoint in ("load_before", "load_after"):
                cases.append((side + endpoint, side, (endpoint, [1, 2, 8.01]),
                              2, "not_comparable_load", "load_not_quiet"))
                cases.append((side + endpoint + " missing", side, (endpoint, None),
                              2, "not_comparable_load", "load_unrecorded"))
        for invalid in ([1, 2], [1, 2, True], [1, -1, 2], [1, 2, float("nan")],
                        [1, 2, 10**400]):
            cases.append((str(invalid), "timing", ("load_after", invalid),
                          2, "not_comparable_load", "load_invalid"))
        for name, side, mutation, expected, status, reason in cases:
            with self.subTest(name=name):
                baseline, current = self.receipt({"kept": 1}), self.receipt({"kept": 201})
                for receipt in (baseline, current):
                    # Only load15 controls admissibility; load1 and load5 are testimony.
                    receipt.update(load_before=[20, 30, 8], load_after=[40, 50, 8])
                if mutation:
                    target = baseline if side == "baseline" else current
                    endpoint, value = mutation
                    if value is None:
                        del target[endpoint]
                    else:
                        target[endpoint] = value
                code, report = self.run_gate("train", baseline, current)
                self.assertEqual(expected, code)
                self.assertEqual(status, report["status"])
                self.assertEqual([reason], report["reasons"])
                self.assertEqual(200, report["delta_seconds"])
                self.assertIn("load", report["baseline"])
                self.assertIn("load", report["timing"])
        baseline, current = self.receipt({"kept": 1}), self.receipt({"kept": 1, "new": 5})
        for receipt in (baseline, current):
            del receipt["load_before"], receipt["load_after"]
        code, report = self.run_gate("car", baseline, current)
        self.assertEqual((0, "ok"), (code, report["status"]))
        current["load_after"] = [1, 2, 10**400]
        code, report = self.run_gate("car", baseline, current)
        self.assertEqual((0, "ok"), (code, report["status"]))

    def test_existing_increases_are_always_info_ranked_and_limited_to_twenty(self):
        old = {"case%02d" % i: 1 for i in range(22)}
        new = {test_id: i + 2 for i, test_id in enumerate(old)}
        old.update(removed=500, faster=50, unchanged=1)
        new.update(added=900, faster=1, unchanged=1)
        for state, expected in (("quiet", "refused"), ("loaded", "not_comparable_load"),
                                ("unrecorded", "not_comparable_load"), ("pass", "ok")):
            with self.subTest(state=state):
                baseline = self.receipt({**old, "removed": 5000} if state == "pass" else old)
                current = self.receipt(new)
                if state == "loaded":
                    current["load_after"][2] = 9
                if state == "unrecorded":
                    del baseline["load_before"], baseline["load_after"]
                _, report = self.run_gate("train", baseline, current)
                self.assertEqual(expected, report["status"])
                info = report["info"]
                self.assertEqual("INFO", info["level"])
                self.assertEqual(22, info["increased_count"])
                self.assertEqual(["case%02d" % i for i in range(21, 1, -1)],
                                 [row["id"] for row in info["tests"]])
                self.assertEqual({"id": "case21", "baseline_seconds": 1,
                                  "timing_seconds": 23, "delta_seconds": 22}, info["tests"][0])
                self.assertEqual(report["baseline"]["load"], info["baseline_load"])
                self.assertEqual(report["timing"]["load"], info["timing_load"])
                self.assertEqual("INFO " + json.dumps(info, sort_keys=True) + "\n", self.last_info)

    def test_incomplete_invalid_or_missing_measurements_refuse(self):
        mutations = [
            lambda r: r.update(status="interrupted"),
            lambda r: r["results"][0]["test_durations"].clear(),
            lambda r: r["results"][0]["test_durations"].update(new=float("nan")),
            lambda r: r["results"][0]["test_durations"].update(new=True),
            lambda r: r["results"][0]["test_durations"].update(new=-1),
            lambda r: r["results"][0]["test_durations"].update(foreign=1),
            lambda r: r["results"][0]["stopped_ids"].clear(),
            lambda r: r["results"].append(copy.deepcopy(r["results"][0])),
            lambda r: r["census"]["ids"].append("unexecuted"),
            lambda r: r["results"][0]["source"].update(head=self.registered),
            lambda r: r.update(results=[None]),
            lambda r: r["aggregate"].update(tests_run=999),
            lambda r: r["results"][0]["events"].clear(),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                current = self.receipt({"new": 1})
                mutate(current)
                with tempfile.TemporaryDirectory() as folder:
                    path = Path(folder) / "invalid.json"
                    path.write_text(json.dumps(current))
                    with self.assertRaises((ValueError, KeyError, TypeError)):
                        self.gate.timing(path)
        current = self.receipt({"new": 1})
        current["results"] = [None]
        code, report = self.run_gate("car", self.receipt({"kept": 1}), current)
        self.assertEqual(1, code)
        self.assertEqual("invalid_evidence", report["reasons"][0])

    def test_foreign_ref_and_fixture_blocked_tests_refuse_by_name(self):
        code, report = self.run_gate("car", self.receipt({"kept": 1}),
            self.receipt({"new": 1}), "--car-ref", self.registered)
        self.assertEqual(1, code)
        self.assertIn("source_ref_mismatch", report["detail"])
        current = self.receipt({"new": 1})
        current["census"]["ids"].append("blocked")
        current["results"][0]["selected_ids"] = ["new", "blocked"]
        current["results"][0]["fixture_blocked_ids"] = ["blocked"]
        code, report = self.run_gate("train", self.receipt({"kept": 1}), current)
        self.assertEqual(1, code)
        self.assertIn("blocked", report["detail"])

    def test_failed_native_tests_do_not_hide_budget_evidence(self):
        current = self.receipt({"kept": 1})
        current["successful"] = False
        current["results"][0]["successful"] = False
        current["results"][0]["events"][0]["kind"] = "failure"
        current["aggregate"] = reconcile(current["census"], current["plan"], current["results"])
        code, report = self.run_gate("train", current, current)
        self.assertEqual(0, code)
        self.assertFalse(report["native_successful"])
        self.assertEqual(0, report["delta_seconds"])

    def test_registration_is_a_ceiling_not_a_blanket_exemption(self):
        code, report = self.run_gate("car", self.receipt({"kept": 1}),
                                     self.receipt({"kept": 1, "slow": 7}, self.registered))
        self.assertEqual(1, code)
        self.assertEqual(["slow"], [row["id"] for row in report["offending_tests"]])

    def test_malformed_policy_and_json_cannot_grant_admission(self):
        gate = self.gate
        for row in ("slow,nan,reason,owner", "slow,-1,reason,owner",
                    "slow,6,,owner", "slow,6,reason,", "slow,6,reason,owner\nslow,7,reason,owner"):
            with self.subTest(row=row), self.assertRaises(ValueError):
                gate.registry("id,seconds,reason,owner\n" + row)
        for text in ('{"x": 1, "x": 2}',):
            with self.assertRaises(ValueError):
                gate.json_value(text)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "invalid.json"
            for value in (None, [], {"status": "complete", "successful": True, "source": None}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    gate.timing(path)

    def test_historical_workers_do_not_need_the_readers_cpu_capacity(self):
        current = self.receipt({str(i): 1 for i in range(8)})
        census = current["census"]
        census["units"] = []
        current["plan"]["assignments"] = []
        prototype = current["results"][0]
        current["results"] = []
        for i in range(8):
            test_id, cls = str(i), "fixture.Case" + str(i)
            census["units"].append({"key": test_id, "ids": [test_id],
                                    "classes": [cls], "serial_reason": None})
            census["membership"][test_id]["class"] = cls
            key = "worker-" + str(i + 1)
            current["plan"]["assignments"].append({"key": key, "phase": "parallel",
                "ids": [test_id], "unit_keys": [test_id]})
            current["results"].append({**prototype, "key": key, "selected_ids": [test_id],
                "started_ids": [test_id], "stopped_ids": [test_id], "tests_run": 1,
                "duration_seconds": 1, "test_durations": {test_id: 1},
                "events": [{"kind": "success", "test_id": test_id}]})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "eight-workers.json"
            path.write_text(json.dumps(current))
            with patch("os.cpu_count", return_value=1):
                self.assertEqual(8, self.gate.timing(path)["case_seconds"])
                with self.assertRaises(ValueError):
                    make_plan(census, 8)


if __name__ == "__main__":
    unittest.main()

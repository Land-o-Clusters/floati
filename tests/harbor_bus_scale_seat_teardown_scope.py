"""New routine teardown at actual parser scope and metric admission consumers.

The matrix inputs are explicitly synthetic correctness fixtures, never native
workload or performance evidence. No seat/session/provider action is executed.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import unittest

from floati.cli import _parser
from tests import bus_scale_commands, bus_scale_workloads
from tests import harbor_bus_scale_attention_proof as admission_fixtures


ROOT=Path(__file__).resolve().parents[1]


class BusScaleSeatTeardownScopeTests(unittest.TestCase):
    def current_scope(self):
        parser=_parser()
        rows=bus_scale_commands.executable_contract(parser)
        self.assertIn("seat teardown",rows,"native product must expose the exact routine session teardown")
        native=rows["seat teardown"]
        try:
            scope=bus_scale_workloads.scope_contract(parser)
        except ValueError as error:
            # Preserve the actual typed closure refusal and complete newly
            # declared native row. The assertion names the missing routine
            # behavior; no guessed API/import or population-count failure.
            evidence={"refusal_type":type(error).__name__,"refusal_detail":str(error),
                      "new_routine_path":"seat teardown","native_contract":native,
                      "unclassified_paths":bus_scale_commands.classify_contract(rows)["unknown"]}
            self.fail("routine seat teardown is not admitted by the actual parser scope: "+json.dumps(evidence,sort_keys=True))
        return scope,native

    def test_current_parser_scope_measures_exact_seat_teardown_contract_once(self):
        scope,native=self.current_scope()
        bindings=[case for case,row in scope["cases"].items() if row["command"]=="seat teardown"]
        self.assertEqual(1,len(bindings),"routine seat teardown needs one actual measured case")
        self.assertNotIn("seat teardown",scope["excluded"],"a routine completed-session action cannot be excluded as administration")
        self.assertEqual(native,scope["inventory"]["seat teardown"],"scope must bind the full current native contract")
        required={argument["option_strings"][0] for argument in native["arguments"]
                  if argument.get("required") and argument.get("option_strings")}
        self.assertEqual({"--root","--as","--home","--session","--repo","--doc","--idempotency-key"},required)

    def test_seat_teardown_current_output_never_exempts_above_budget_cpu_bytes_or_holds(self):
        scope,native=self.current_scope()
        cases=[case for case,row in scope["cases"].items() if row["command"]=="seat teardown"]
        self.assertEqual(1,len(cases),"metric consumer needs the uniquely admitted teardown case")
        case=cases[0]
        fixture_scope,observations=admission_fixtures.population()
        self.assertEqual(scope,fixture_scope)
        matrix={}
        for family in ("events","registry","wait-exhaustion","deliveries"):
            rows=copy.deepcopy(observations)
            for observation in rows.values():observation["family"]=family
            rows[case]["arms"]["1x"]["metrics"]={"cpu_seconds":1.0,"bytes_read":100,"max_ledger_lock_seconds":0.01}
            rows[case]["arms"]["10x"]["metrics"]={"cpu_seconds":2.0,"bytes_read":200,"max_ledger_lock_seconds":0.02}
            matrix[family]=[{"order":order,"observations":copy.deepcopy(rows)}
                            for order in ([1,10],[10,1])]
        path=ROOT/"tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py"
        specification=importlib.util.spec_from_file_location("seat_teardown_scope_consumer",path)
        coordinator=importlib.util.module_from_spec(specification)
        specification.loader.exec_module(coordinator)
        result=coordinator.validate_matrix(scope,matrix,first_pass=True)
        compared=[row for row in result["comparisons"] if row["case"]==case]
        self.assertEqual({"events","registry","wait-exhaustion","deliveries"},{row["family"] for row in compared})
        self.assertEqual(4,len(compared),"each family must classify its own teardown case once")
        for comparison in compared:
            for metric in ("cpu_seconds","read_bytes","ledger_hold_seconds"):
                with self.subTest(family=comparison["family"],metric=metric):
                    self.assertEqual([2.0,2.0],comparison["metrics"][metric]["pair_ratios"])
                    self.assertEqual("red",comparison["metrics"][metric]["classification"],
                                     "bounded completed-session output is fully gated, not history INFO")
        red_metrics={row["metric"] for row in result["red_map"] if row["subcommand"]=="seat teardown"}
        self.assertEqual({"cpu_seconds","read_bytes","ledger_hold_seconds"},red_metrics)


if __name__=="__main__":unittest.main()

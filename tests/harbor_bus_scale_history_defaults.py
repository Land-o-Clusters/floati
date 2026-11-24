"""Default-output exceptions at the real census metric consumer.

The literals follow native help/projection contracts, independently of scope
flags: cumulative entity history earns CPU/byte INFO; current summaries do
not. These are synthetic admission inputs, never performance evidence.
"""
from __future__ import annotations

import unittest

from tests import harbor_bus_scale_matrix as matrix_fixtures


class BusScaleHistoryDefaultTests(unittest.TestCase):
    def setUp(self):
        fixture=matrix_fixtures.BusScaleMatrixTests()
        fixture.setUp()
        self.subject,self.scope=fixture.subject,fixture.scope
        self.matrix={family:pairs[:2] for family,pairs in fixture.matrix.items()}
        # Both independent balanced pairs have ratio 2, above the ruled 1.5
        # threshold. Nothing about the exception can hide behind a PASS.
        for pairs in self.matrix.values():
            for pair in pairs:
                for case in ("sent","effects","presence-show","work-show",
                             "quota-show","chart-timings","intake-show"):
                    arms=pair["observations"][case]["arms"]
                    arms["1x"]["metrics"]={"cpu_seconds":1.0,"bytes_read":100,"max_ledger_lock_seconds":0.01}
                    arms["10x"]["metrics"]={"cpu_seconds":2.0,"bytes_read":200,"max_ledger_lock_seconds":0.02}
        self.result=self.subject.validate_matrix(self.scope,self.matrix,first_pass=True)

    def assert_metric(self,case,command,metric,want):
        matches=[row for row in self.result["comparisons"] if row["case"]==case]
        self.assertEqual({"events","registry","wait-exhaustion","deliveries"},
                         {row["family"] for row in matches})
        for comparison in matches:
            value=comparison["metrics"][metric]
            self.assertEqual([2.0,2.0],value["pair_ratios"])
            self.assertEqual(2.0,value["ratio"])
            self.assertEqual(want,value["classification"],
                             case+" must classify its documented default output at the consumer")
        native=[row for row in self.result["native_metric_rows"]
                if row["subcommand"]==command and row["metric"]==metric]
        self.assertEqual(1,len(native))
        self.assertEqual(want,native[0]["classification"])
        reds={(row["subcommand"],row["metric"]) for row in self.result["red_map"]}
        self.assertEqual(want=="red",(command,metric) in reds)

    def test_default_current_summaries_cannot_hide_above_budget_cpu_as_history(self):
        # Catches wrongly exempting latest/current/aggregate output, or
        # removing the exemption from cumulative historical entity lists.
        for case,command,want in (
            ("sent","sent","info"),
            ("effects","effects","info"),
            ("work-show","work show","info"),
            ("intake-show","intake show","info"),
            ("presence-show","presence show","red"),
            ("quota-show","quota show","red"),
            ("chart-timings","chart timings","red"),
        ):
            with self.subTest(case=case):self.assert_metric(case,command,"cpu_seconds",want)

    def test_default_current_summaries_cannot_hide_above_budget_bytes_as_history(self):
        for case,command,want in (
            ("sent","sent","info"),
            ("effects","effects","info"),
            ("work-show","work show","info"),
            ("intake-show","intake show","info"),
            ("presence-show","presence show","red"),
            ("quota-show","quota show","red"),
            ("chart-timings","chart timings","red"),
        ):
            with self.subTest(case=case):self.assert_metric(case,command,"read_bytes",want)

    def test_history_default_never_exempts_above_budget_ledger_holds(self):
        for case,command in (
            ("sent","sent"),("effects","effects"),("work-show","work show"),
            ("intake-show","intake show"),("presence-show","presence show"),
            ("quota-show","quota show"),("chart-timings","chart timings"),
        ):
            with self.subTest(case=case):self.assert_metric(case,command,"ledger_hold_seconds","red")


if __name__=="__main__":unittest.main()

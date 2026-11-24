"""Native exhaustion-family construction and real append/effect controls.

Tiny 2/20 fixtures exercise both instrumentation modes. The default profile
runs the genuine entry in-process; the explicit full profile retains owned
native children. Neither profile supplies a five-pair performance verdict.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from tests.bus_scale_test_profile import StableSourceFixture
from unittest.mock import patch

from floati.codex_wait_contract import CodexWaitReceiptLedger
from floati.jsonl import read_records
from floati.root import FloatiRoot
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_test_profile import full_scale_enabled, load_fixture_module
from tests.temp_roots import REAL_TEMP_ROOT
import tests.bus_scale_census as native_census

RELATIVE="receipts/codex-wait-exhaustion/scale-worker.jsonl"


class NativeExhaustionFamilyTests(StableSourceFixture, unittest.TestCase):
    def setUp(self):
        if full_scale_enabled():
            self.census = native_census
        else:
            from tests.bus_scale_bounded_transport import bounded_module
            # Share only compiled code: patches and instrumentation wrappers
            # belong to this test's fresh module globals.
            self.census = bounded_module(load_fixture_module(
                native_census.__file__, "bounded_exhaustion_family_instrument"))
        self.base=Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-exhaustion-control-",
                                      dir=REAL_TEMP_ROOT)).resolve()
        self.owners=[]
        self.addCleanup(self.remove_collected_scratch)

    def remove_collected_scratch(self):
        if any(owner.homes for owner in self.owners):
            print("UNcollected exhaustion control child; retained "+str(self.base),flush=True)
            return
        shutil.rmtree(self.base)

    def pair(self):
        census = self.census
        factory=getattr(census,"native_exhaustion_seeds",None)
        self.assertTrue(callable(factory),"native exhaustion-family seed producer is missing")
        return factory(self.base/"seeds",small_count=2,deadline=time.monotonic()+60)

    def coverage(self, seeds):
        census = self.census
        guard=getattr(census,"original_family_coverage",None)
        self.assertTrue(callable(guard),"premeasurement declared-family admission is missing")
        return guard("receipt-append",seeds)

    def test_native_receipts_not_event_padding_form_the_exact_declared_family(self):
        census = self.census
        observed=[]
        native=CodexWaitReceiptLedger.record_exhaustion
        def record(plane,**kwargs):
            row=native(plane,**kwargs)
            observed.append(row)
            return row
        with patch.object(CodexWaitReceiptLedger,"record_exhaustion",new=record):
            pair=self.pair()
        self.assertEqual(20,len(observed))
        self.assertEqual(20,len({row["id"] for row in observed}))
        self.assertEqual(20,len({row["session_digest"] for row in observed}))
        plain={}
        for scale,count in (("1",2),("10",20)):
            home=pair[scale]
            rows=read_records(FloatiRoot.open_direct_home(home),RELATIVE,
                allowed_kinds=set(CodexWaitReceiptLedger._KINDS))
            self.assertEqual(observed[:count],rows,"fixture contains unauthored/copy-padded frames")
            self.assertTrue(all(row["node_id"]=="scale-worker" and row["waited_seconds"]==2
                and row["outcome"]=="consent_reopened" and row["idempotency_key"]!="census-receipt" for row in rows))
            self.assertTrue(all(len(row["session_digest"])==64 for row in rows))
            files=census._inventory(home)
            self.assertNotIn("events.jsonl",files,"unrelated events cannot substitute for exhaustion history")
            plain[scale]={path:value for path,value in files.items() if path!=RELATIVE}
        self.assertEqual(plain["1"],plain["10"],"native history changed ancillary inputs")
        self.assertTrue((pair["10"]/RELATIVE).read_bytes().startswith((pair["1"]/RELATIVE).read_bytes()))
        proof=self.coverage({scale:pair[scale] for scale in ("1","10")})
        self.assertEqual("wait-exhaustion",proof["declared_family"])
        self.assertEqual(RELATIVE,proof["ledger_relative"])
        self.assertEqual({"1":2,"10":20},proof["physical_records"])

    def test_genuine_global_event_history_cannot_qualify_exhaustion_family(self):
        census = self.census
        guard=getattr(census,"original_family_coverage",None)
        self.assertTrue(callable(guard),"premeasurement declared-family admission is missing")
        wrong=census.native_seeds(self.base/"global-event-probe",small_count=2,
                                  deadline=time.monotonic()+60)
        with self.assertRaisesRegex(RuntimeError,"declared wait-exhaustion family"):
            guard("receipt-append",wrong)

    def test_real_cpu_and_diagnostic_children_append_one_fresh_receipt_after_each_native_history(self):
        census = self.census
        pair=self.pair()
        repository,_=banked_send_repository(self.base)
        workspace=self.base/"workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        producer=census._load(census.ROOT/"tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py",
                              "exhaustion_family_native_effects")
        owner=census._ContinuationOwner(time.monotonic()+120)
        self.owners.append(owner)
        self.coverage({scale:pair[scale] for scale in ("1","10")})
        for scale,count in (("1",2),("10",20)):
            seed=pair[scale]
            frozen=census._inventory(seed)
            history=read_records(FloatiRoot.open_direct_home(seed),RELATIVE,
                allowed_kinds=set(CodexWaitReceiptLedger._KINDS))
            values,records=census._original_observation("receipt-append",seed,workspace,
                self.base/"observed"/scale,owner,producer)
            for mode in ("cpu","diagnostic"):
                record=records[mode]
                collection=record["collection"]
                self.assertTrue(collection["child_collected"])
                self.assertEqual(0,collection["exit_code"])
                self.assertTrue(collection["home_cleanup"]["home_removed"])
                home=self.base/"observed"/scale/mode/"scale-fleet"
                actual=read_records(FloatiRoot.open_direct_home(home),RELATIVE,
                    allowed_kinds=set(CodexWaitReceiptLedger._KINDS))
                self.assertEqual(count+1,len(actual))
                self.assertEqual(history,actual[:-1])
                self.assertEqual(record["artifact"],actual[-1])
                self.assertEqual("census-receipt",actual[-1]["idempotency_key"])
                self.assertEqual(hashlib.sha256(b"bus-scale-native-census").hexdigest(),actual[-1]["session_digest"])
                self.assertEqual({"exit_code":0,"status":"consent_reopened",
                    "effect":{"kind":"exhaustion_receipt_appended","count":1}},record["effect"])
            self.assertEqual(records["cpu"]["equivalence"]["delta"],records["diagnostic"]["equivalence"]["delta"])
            self.assertGreater(values["read_bytes"],0)
            self.assertEqual(frozen,census._inventory(seed))
        self.assertEqual({},owner.homes,"native control retained an uncollected child")

    def test_original_continuation_routes_receipt_case_through_family_admission_before_measurement(self):
        census = self.census
        native=getattr(census,"native_exhaustion_seeds",None)
        self.assertTrue(callable(native),"native exhaustion-family seed producer is missing")
        class MeasurementBoundary(RuntimeError): pass
        calls=[]
        def tiny(base,*,deadline):
            return native(base,small_count=2,deadline=deadline)
        def observe(case,seed,*args):
            self.assertEqual("receipt-append",case)
            rows=read_records(FloatiRoot.open_direct_home(seed),RELATIVE,
                allowed_kinds=set(CodexWaitReceiptLedger._KINDS))
            self.assertEqual(2,len(rows))
            calls.append(case)
            raise MeasurementBoundary("stopped after real preparation, before native measurement")
        bank=self.base/"case-evidence"
        bank.mkdir(mode=0o700)
        with patch.object(census,"native_exhaustion_seeds",side_effect=tiny),\
                patch.object(census,"native_seeds",side_effect=AssertionError("unrelated global seed route selected")),\
                patch.object(census,"_original_observation",side_effect=observe):
            with self.assertRaises(MeasurementBoundary):
                census._remaining_case("receipt-append",bank)
        self.assertEqual(["receipt-append"],calls)
        retained=json.loads((bank/"case.json").read_text())
        self.assertEqual("wait-exhaustion",retained["family_coverage"]["declared_family"])
        self.assertEqual({"1":2,"10":20},retained["family_coverage"]["physical_records"])
        self.assertFalse(retained["native_complete"],"boundary control cannot claim a native measurement")
        self.assertTrue(retained["scratch_removed"])

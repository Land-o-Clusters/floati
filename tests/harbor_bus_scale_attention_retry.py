"""Actual first-pass retry routing with synthetic admission data only.

No observations below are native executions or performance measurements.
Only expensive/native preparation and observation boundaries are replaced;
real scope, attention admission, matrix classification, archive and cleanup
remain on the actual coordinator main path.
"""
from __future__ import annotations

import copy
from tests.bus_scale_test_profile import clone_fixture
from functools import lru_cache
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests import bus_scale_seeds
from tests import harbor_bus_scale_attention_proof as attention_fixtures
from tests import harbor_bus_scale_integration as phase_fixtures
from tests.bus_scale_attention import AttentionCategoryMismatch
from tests.temp_roots import REAL_TEMP_ROOT


ROOT=Path(__file__).resolve().parents[1]
COORDINATOR=ROOT/"tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py"


def subject():
    from tests.bus_scale_test_profile import load_fixture_module
    return load_fixture_module(COORDINATOR,"attention_retry_main_controls")


def objects(value):
    if isinstance(value,dict):
        yield value
        for child in value.values():yield from objects(child)
    elif isinstance(value,list):
        for child in value:yield from objects(child)


@lru_cache(maxsize=1)
def _phase_details_template():
    phase = phase_fixtures.BusScaleWaitStartQualificationTests()
    phase.setUp()
    return phase.details


class SyntheticObservationBoundaries:
    """Closed data substitution; no fake processes or measured-cost claims."""
    def __init__(self,test,*,persistent,arm_failure=None):
        self.test,self.persistent=test,persistent
        test.assertIn(arm_failure,(None,"typed","generic","malformed"))
        self.arm_failure=arm_failure
        self.scope,self.observations=attention_fixtures.population()
        self.phase_details=clone_fixture(_phase_details_template())
        self.calls=[]
        self.base=None

    def seed_pair(self,base):
        self.base=Path(base).parent
        seeds={}
        for family in ("events","registry","exhaustion","deliveries"):
            canonical="wait-exhaustion" if family=="exhaustion" else family
            pair={"family":canonical,"ledger_relative":"synthetic-admission.txt"}
            for scale in (1,10):
                home=Path(base)/family/str(scale)/"scale-fleet"
                home.mkdir(parents=True,mode=0o700)
                (home/pair["ledger_relative"]).write_bytes(b"synthetic admission input; not a ledger\n"*(1000*scale))
                pair[str(scale)]=home
            seeds[family]=pair
        return seeds

    def workload_seed(self,seeds,family,case,workspace,base,entry,cache):
        # Native case preparation/consent is outside this routing regression.
        return seeds[family],workspace

    def original(self,owner,case,home,relative,workspace,place):
        return self.observe(case,Path(home).parent.name,Path(home),relative,Path(place))

    def expanded(self,owner,case,pair,scale,workspace,place,entry):
        return self.observe(case,scale,pair[scale],pair["ledger_relative"],Path(place))

    def observe(self,case,scale,home,relative,place):
        scale=int(scale)
        parts=place.relative_to(self.base/"runs").parts
        family=parts[0]
        call={"case":case,"family":family,"scale":scale,"raw_coordinate":place.relative_to(self.base).as_posix()}
        targets=[row for row in self.calls if row["family"]=="events" and row["case"]=="supervise"]
        target=family=="events" and case=="supervise"
        if target and self.persistent and len(targets)>=4:
            self.test.fail("persistent category mismatch exceeded the exact one-retry/two-attempt budget")
        initial_failed_arm=target and scale==10 and len(targets)==1 and self.arm_failure is not None
        invalid=target and (self.persistent or len(targets)<2) and self.arm_failure in (None,"typed")
        arm=clone_fixture(self.observations[case]["arms"][str(scale)+"x"])
        raw=(Path(home)/relative).read_bytes()
        arm.update(seed_sha256=hashlib.sha256(raw).hexdigest(),clone_sha256=hashlib.sha256(raw).hexdigest(),
                   records=1000*scale,bytes=len(raw))
        if invalid and scale==10:
            arm["attention_proof"]["nodes"]["architect"]["seat_attention"]=attention_fixtures.seat_category(pending=True,overdue=True)
            # An invalid outlier must survive in the retained attempt, not be
            # admitted/classified or silently replaced by the successful retry.
            arm["metrics"]["cpu_seconds"]=8.0
        if initial_failed_arm and self.arm_failure=="malformed":
            arm["coverage"]["locks"]=False
        place.mkdir(parents=True,exist_ok=True)
        payload={"synthetic_admission_only":True,"case":case,"family":family,"scale":scale,
                 "arm":clone_fixture(arm)}
        if initial_failed_arm and self.arm_failure in ("typed","generic"):
            payload["boundary_error"]={
                "type":"AttentionCategoryMismatch" if self.arm_failure=="typed" else "ValueError",
                "code":"bus_scale_attention_category_mismatch" if self.arm_failure=="typed" else None,
                "detail":"synthetic arm-local category mismatch" if self.arm_failure=="typed" else
                         "synthetic generic observation refusal; no attention retry",
            }
        path=place/"synthetic-observation.json"
        path.write_text(json.dumps(payload,sort_keys=True)+"\n")
        arm["raw_sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
        call.update(raw_sha256=arm["raw_sha256"],invalid=invalid,arm=clone_fixture(arm),
                    returned=not (initial_failed_arm and self.arm_failure in ("typed","generic")))
        self.calls.append(call)
        if initial_failed_arm and self.arm_failure=="typed":
            raise AttentionCategoryMismatch("synthetic arm-local category mismatch")
        if initial_failed_arm and self.arm_failure=="generic":
            raise ValueError("synthetic generic observation refusal; no attention retry")
        if case=="wait-idle":
            for mode in ("cpu","diagnostic"):
                detail=place/mode/"detail.json"
                detail.parent.mkdir(mode=0o700)
                detail.write_text(json.dumps(self.phase_details[str(scale)+"x"][mode],sort_keys=True)+"\n")
        return arm


class BusScaleAttentionRetryTests(unittest.TestCase):
    def assert_complete_comparisons_equal(self,recomputed,archived):
        # JSON sorts the scope dictionary keys; comparison list order is not
        # an admission fact. Preserve every complete row at its unique key.
        for rows in (recomputed,archived):
            self.assertEqual(280,len(rows),"complete four-family/70-case comparison population")
        recomputed_by_key={(row["family"],row["case"]):row for row in recomputed}
        archived_by_key={(row["family"],row["case"]):row for row in archived}
        self.assertEqual(len(recomputed),len(recomputed_by_key),"duplicate recomputed comparison coordinate")
        self.assertEqual(len(archived),len(archived_by_key),"duplicate archived comparison coordinate")
        self.assertEqual(recomputed_by_key,archived_by_key)

    def run_firstpass(self,persistent,arm_failure=None):
        coordinator=subject()
        temporary=tempfile.TemporaryDirectory(prefix="bus-scale-census-2026-10-09-attention-routing-",dir=coordinator.TEMP)
        self.addCleanup(temporary.cleanup)
        output=Path(temporary.name).resolve()
        boundaries=SyntheticObservationBoundaries(self,persistent=persistent,arm_failure=arm_failure)
        failure=None
        code=None
        # The actual main still creates its tiny owned Git workspace and
        # private scratch, imports real scope/admission, qualifies independent
        # waiter phase fixtures, archives every raw file and removes scratch.
        with mock.patch.object(bus_scale_seeds,"make_seed_pair",boundaries.seed_pair),\
                mock.patch("tests.bus_scale_workloads.mcp_tool_bindings",return_value={}),\
                mock.patch.object(coordinator,"prepare",return_value=None),\
                mock.patch.object(coordinator,"workload_seed",boundaries.workload_seed),\
                mock.patch.object(coordinator,"observation",boundaries.original),\
                mock.patch.object(coordinator,"expanded_adapter",return_value=SimpleNamespace(expanded_observation=boundaries.expanded)),\
                mock.patch("sys.argv",[str(COORDINATOR),"first-pass",str(output)]),\
                redirect_stdout(io.StringIO()):
            try:code=coordinator.main()
            except ValueError as error:failure=error
        record=json.loads((output/"census.json").read_bytes())
        self.assertTrue(record["scratch_removed"],"actual main cleanup must complete")
        self.assertFalse(boundaries.base.exists(),"owned main scratch leaked")
        with tarfile.open(output/"census-raw.tar.gz","r:gz") as archive:
            raw={member.name:archive.extractfile(member).read() for member in archive
                 if member.isfile() and member.name.endswith("synthetic-observation.json")}
        for call in boundaries.calls:
            coordinate=call["raw_coordinate"]+"/synthetic-observation.json"
            self.assertIn(coordinate,raw,"main dropped a raw native-boundary routing fixture")
            self.assertEqual(call["raw_sha256"],hashlib.sha256(raw[coordinate]).hexdigest())
        retained=Path(tempfile.mkdtemp(prefix="bus-scale-attention-retry-routing-",dir=REAL_TEMP_ROOT))
        shutil.copytree(output,retained/"output",copy_function=shutil.copy2)
        sources=("tests/harbor_bus_scale_attention_retry.py","tests/bus_scale_attention.py",
                 "tests/bus_scale_workloads.py","tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py")
        (retained/"routing-control.json").write_text(json.dumps({
            "synthetic_admission_only":True,"persistent":persistent,"arm_failure":arm_failure,"code":code,
            "failure_type":None if failure is None else type(failure).__name__,
            "failure_code":None if failure is None else getattr(failure,"code",None),
            "calls":boundaries.calls,"original_output":str(output),
            "sources":{path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in sources},
        },sort_keys=True)+"\n")
        print("synthetic first-pass retry routing bank retained: "+str(retained),flush=True)
        return coordinator,boundaries,record,code,failure,raw

    def invalid_rows(self,record):
        result=[]
        for row in objects(record):
            arms=row.get("arms")
            if type(arms) is not dict or set(arms)!={"1x","10x"}:continue
            proofs=[arms[scale].get("attention_proof") for scale in ("1x","10x")]
            if (all(type(proof) is dict and proof.get("case")=="supervise" for proof in proofs)
                    and proofs[0]["nodes"]!=proofs[1]["nodes"]):
                result.append(row)
        return result

    def test_firstpass_retries_both_scale_arms_and_retains_invalid_pair_before_admission(self):
        coordinator,boundaries,record,code,failure,raw=self.run_firstpass(False)
        calls=[row for row in boundaries.calls if row["family"]=="events" and row["case"]=="supervise"]
        self.assertEqual([1,10,1,10,10,1],[row["scale"] for row in calls],
                         "invalid first pair must rerun both scales once, preserving balanced pair order")
        self.assertIsNone(failure)
        self.assertEqual(0,code)
        self.assertEqual("first_pass_complete",record["status"])
        rejected=self.invalid_rows(record)
        self.assertEqual(1,len(rejected),"retain exactly the invalid pair, outside admitted matrix")
        self.assertEqual(8.0,rejected[0]["arms"]["10x"]["metrics"]["cpu_seconds"])
        admitted=record["matrix"]["events"][0]["observations"]["supervise"]
        self.assertEqual([calls[2]["raw_sha256"],calls[3]["raw_sha256"]],
                         [admitted["arms"][scale]["raw_sha256"] for scale in ("1x","10x")])
        self.assertEqual(admitted["arms"]["1x"]["attention_proof"]["nodes"],
                         admitted["arms"]["10x"]["attention_proof"]["nodes"])
        # The real full consumer must classify only the valid admitted matrix.
        self.assert_complete_comparisons_equal(
            coordinator.validate_matrix(record["scope"],record["matrix"],first_pass=True)["comparisons"],
            record["comparisons"])
        metric=next(row for row in record["comparisons"] if row["family"]=="events" and row["case"]=="supervise")
        self.assertEqual([1.0,1.0],metric["metrics"]["cpu_seconds"]["pair_ratios"])

    def test_firstpass_persistent_category_mismatch_stops_after_one_retry_with_all_invalid_raw(self):
        coordinator,boundaries,record,code,failure,raw=self.run_firstpass(True)
        calls=[row for row in boundaries.calls if row["family"]=="events" and row["case"]=="supervise"]
        self.assertEqual([1,10,1,10],[row["scale"] for row in calls],
                         "exactly one full-pair retry must precede typed incomplete on persistent mismatch")
        self.assertEqual("incomplete",record["status"])
        self.assertEqual(2,len(self.invalid_rows(record)),"both invalid attempts and their outliers must be retained")
        self.assertNotEqual(0,code)
        self.assertTrue((failure is not None and failure.code=="bus_scale_attention_category_mismatch")
                        or any(value.get("code")=="bus_scale_attention_category_mismatch" for value in objects(record)),
                        "persistent incomplete needs the named category mismatch, not a generic or hidden refusal")
        self.assertNotIn("comparisons",record,"invalid pair must never be classified")

    def test_firstpass_arm_local_typed_mismatch_retains_partial_raw_and_retries_both_scales(self):
        coordinator,boundaries,record,code,failure,raw=self.run_firstpass(False,arm_failure="typed")
        calls=[row for row in boundaries.calls if row["family"]=="events" and row["case"]=="supervise"]
        self.assertEqual([1,10,1,10,10,1],[row["scale"] for row in calls],
                         "a typed failure inside an arm must retry the entire pair, never just the failed scale")
        self.assertIsNone(failure)
        self.assertEqual(0,code)
        self.assertEqual("first_pass_complete",record["status"])
        self.assertFalse(calls[1]["returned"],"fixture must fail before the original 10x arm returns")
        failed_raw=json.loads(raw[calls[1]["raw_coordinate"]+"/synthetic-observation.json"])
        self.assertEqual("bus_scale_attention_category_mismatch",failed_raw["boundary_error"]["code"])
        self.assertEqual(8.0,failed_raw["arm"]["metrics"]["cpu_seconds"],"partial raw outlier was discarded")
        partials=[row for row in objects(record) if type(row.get("arms")) is dict
                  and set(row["arms"])=={"1x"}
                  and row["arms"]["1x"].get("attention_proof",{}).get("case")=="supervise"]
        self.assertEqual(1,len(partials),"retain the invalid partial attempt outside the admitted matrix")
        self.assertEqual(calls[0]["raw_sha256"],partials[0]["arms"]["1x"]["raw_sha256"])
        admitted=record["matrix"]["events"][0]["observations"]["supervise"]
        self.assertEqual([calls[2]["raw_sha256"],calls[3]["raw_sha256"]],
                         [admitted["arms"][scale]["raw_sha256"] for scale in ("1x","10x")])
        self.assert_complete_comparisons_equal(
            coordinator.validate_matrix(record["scope"],record["matrix"],first_pass=True)["comparisons"],
            record["comparisons"])

    def test_firstpass_generic_or_malformed_admission_never_retries_as_attention(self):
        for failure_mode in ("generic","malformed"):
            with self.subTest(failure_mode=failure_mode):
                coordinator,boundaries,record,code,failure,raw=self.run_firstpass(False,arm_failure=failure_mode)
                calls=[row for row in boundaries.calls if row["family"]=="events" and row["case"]=="supervise"]
                self.assertEqual([1,10],[row["scale"] for row in calls],
                                 "generic or malformed admission must not receive an attention retry")
                self.assertEqual("incomplete",record["status"])
                self.assertNotEqual(0,code)
                self.assertTrue((isinstance(failure,ValueError) and not isinstance(failure,AttentionCategoryMismatch))
                                or str(record.get("error","")).startswith("ValueError:"))
                self.assertNotIn("bus_scale_attention_category_mismatch",str(record.get("error","")))
                self.assertNotIn("comparisons",record,"malformed arms must never be classified")
                failed_raw=json.loads(raw[calls[1]["raw_coordinate"]+"/synthetic-observation.json"])
                if failure_mode=="generic":
                    self.assertFalse(calls[1]["returned"])
                    self.assertEqual("ValueError",failed_raw["boundary_error"]["type"])
                    self.assertIsNone(failed_raw["boundary_error"]["code"])
                else:
                    self.assertTrue(calls[1]["returned"])
                    self.assertFalse(failed_raw["arm"]["coverage"]["locks"],
                                     "actual admission must see the malformed coverage, not a mocked refusal")


if __name__=="__main__":unittest.main()

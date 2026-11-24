"""Raw native arm clocks must preserve the intended selected-mail category.

This executes the actual expanded adapter/entry. No measured clock is mocked
and no ledger is rewritten: native send authors the deliberately older mail
before the real preparation helper and ordinary immutable history builder.
"""
from __future__ import annotations

import copy
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from floati.events import EventLog
from floati.root import FloatiRoot
from tests.bus_scale_test_profile import banked_send_repository
from tests.bus_scale_case_seeds import FixtureSendCache,inventory,make_case_seed_pair
from tests.bus_scale_test_profile import seed_counts
from tests.temp_roots import REAL_TEMP_ROOT


ROOT=Path(__file__).resolve().parents[1]
ADAPTER=ROOT/"tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py"
ENTRY=ROOT/"tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"
SOURCE_PATHS=("tests/harbor_bus_scale_arm_clock.py","tests/bus_scale_case_seeds.py",
              "tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py",
              "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py",
              "tests/fixtures/bus_scale_census/bus-scale-routine-reads-2026-10-15.py",
              "floati/supervisor.py","floati/wake_health.py")


def load(path,name):
    from tests.bus_scale_test_profile import load_fixture_module
    module=load_fixture_module(path,name)
    from tests.bus_scale_bounded_transport import bounded_module
    return bounded_module(module)


def sources():
    return {path:{"bytes":(ROOT/path).stat().st_size,
                  "sha256":hashlib.sha256((ROOT/path).read_bytes()).hexdigest()}
            for path in SOURCE_PATHS}


class EarlierNativeMailPreparation:
    def __init__(self,helper,sha):
        self.helper,self.sha=helper,sha
        self.message=None

    def prepare(self,case,home,workspace,bank):
        root=FloatiRoot.open_direct_home(home,create=False)
        cache_owner=FixtureSendCache(Path(bank).parent,root.path)
        actual_authored_time=datetime.now(timezone.utc)-timedelta(seconds=960)
        self.message=EventLog(root).send("scale-worker","architect","floati",self.sha,
            "note.md","native arm-clock selected mail",idempotency_key="native-arm-clock-selected",
            now=actual_authored_time)["message"]
        context=self.helper.prepare(case,home,workspace,bank)
        # Immutable seeds retain native ledger testimony, not timestamp-bound
        # cache sidecars. Ownership was captured before this fixture's send.
        self.cache_cleanup_receipt=cache_owner.remove([self.message["id"]])
        # This is the intended scenario's comparison clock: age zero at its
        # real native authored time. A measured arm must materialize that
        # relative condition; normalizing raw overdue state cannot prove it.
        context["canonical_time"]=self.message["timestamp"]
        return context


class CapturingNativeOwner:
    def __init__(self,delegate,helper,context,bank):
        self.delegate,self.helper,self.context,self.bank=delegate,helper,context,bank
        self.running={}
        self.records=[]

    def launch_owned_child(self,argv,**kwargs):
        home=Path(argv[5])
        record={"argv":list(argv),"home":home,"mode":argv[3],"before":inventory(home),
                "stdout":Path(kwargs["stdout"].name),"stderr":Path(kwargs["stderr"].name),
                "detail":Path(argv[7]),"source_before_launch":sources()}
        child=self.delegate.launch_owned_child(argv,**kwargs)
        self.running[child.pid]=record
        return child

    def collect_owned_child(self,child,**kwargs):
        collected=self.delegate.collect_owned_child(child,**kwargs)
        record=self.running.pop(child.pid)
        record["collection"]=copy.deepcopy(collected)
        record["source_after_collection"]=sources()
        self.records.append(record)
        if collected.get("child_collected") is not True:
            return collected
        capture=self.bank/record["mode"]
        capture.mkdir(mode=0o700)
        shutil.copytree(record["home"],capture/"scale-fleet",copy_function=shutil.copy2)
        for key,name in (("stdout","stdout.txt"),("stderr","stderr.txt"),("detail","detail.json")):
            shutil.copy2(record[key],capture/name)
        record["artifact"]=json.loads((capture/"stdout.txt").read_bytes())
        detail=json.loads((capture/"detail.json").read_bytes())
        captured=inventory(record["home"])
        # Witness the complete raw artifact independently of normalization,
        # while its actual native source is still at the collected coordinate.
        record["native_witness"]=self.helper.witness("supervise",record["home"],record["before"],
            record["artifact"],self.context,detail)
        if inventory(record["home"])!=captured:
            raise AssertionError("independent native witnessing changed the collected root")
        (capture/"collection.json").write_text(json.dumps({
            "pid":child.pid,"collection":collected,"native_witness":record["native_witness"],
            "source_before_launch":record["source_before_launch"],
            "source_after_collection":record["source_after_collection"],
            "stdout_sha256":hashlib.sha256((capture/"stdout.txt").read_bytes()).hexdigest(),
        },sort_keys=True)+"\n")
        return collected


class BusScaleArmClockTests(unittest.TestCase):
    def test_supervise_arm_materializes_fresh_selected_mail_before_native_measurement(self):
        # Catches exact restoration of an old seed clock that silently turns
        # the intended fresh scenario into overdue/unknown native work.
        with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-arm-clock-",dir=REAL_TEMP_ROOT) as temporary:
            base=Path(temporary).resolve()
            repository,sha=banked_send_repository(base)
            workspace=base/"workspace"
            repository.rename(workspace)
            workspace.chmod(0o700)
            seed_base=base/"seeds"
            seed_base.mkdir(mode=0o700)
            entry=load(ENTRY,"arm_clock_entry")
            helper=entry.routine_module("supervise")
            preparation=EarlierNativeMailPreparation(helper,sha)
            initial_sources=sources()
            pair=make_case_seed_pair(seed_base,"registry","supervise",preparation,workspace,
                                     counts=seed_counts())
            templates={scale:inventory(pair[scale]) for scale in ("1","10")}
            original_context=copy.deepcopy(pair["context"])
            place=base/"observation"
            place.mkdir(mode=0o700)
            captures=base/"raw-native-captures"
            captures.mkdir(mode=0o700)
            delegate=load(ROOT/"tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py","arm_clock_owner")
            owner=CapturingNativeOwner(delegate,helper,pair["context"],captures)
            adapter=load(ADAPTER,"arm_clock_adapter")
            normalization_refusal=None
            try:
                try:
                    adapter.expanded_observation(owner,"supervise",pair,"1",workspace,place,ENTRY)
                except ValueError as error:
                    if not str(error).startswith("native normalization would change unreviewed testimony: "):
                        raise
                    normalization_refusal=str(error)
                self.assertTrue(owner.records,"raw native child output must exist before the category assertion")
                self.assertFalse(owner.running,"every owned child must be collected")
                for scale in ("1","10"):
                    self.assertEqual(templates[scale],inventory(pair[scale]),"immutable native template changed")
                self.assertEqual(original_context,pair["context"],"authored scenario clock/context changed")
                for source in ("tests/bus_scale_case_seeds.py",ADAPTER.relative_to(ROOT).as_posix()):
                    self.assertEqual(initial_sources[source],sources()[source],"arm seed implementation changed during control")
                attention_rows=[]
                if normalization_refusal is None:
                    self.assertEqual(2,len(owner.records),"both unmodified native modes must be collected")
                for record in owner.records:
                    self.assertEqual({"status":"complete","exit_code":0,"child_collected":True,"interrupted":False},record["collection"])
                    self.assertEqual({"exit_code":0,"status":"ok","effect":{"kind":"supervision_returned","count":1}},record["native_witness"])
                    root=FloatiRoot.open_direct_home(captures/record["mode"]/"scale-fleet",create=False)
                    selected=[row for row in EventLog(root).records()
                              if row["sender"]=="scale-worker" and row["recipient"]=="architect"]
                    self.assertEqual([preparation.message["id"]],[row["id"] for row in selected])
                    self.assertEqual("native arm-clock selected mail",selected[0]["note"])
                    for plane in ("deliveries","acks"):
                        for path in (root.path/"receipts"/plane).glob("*.jsonl"):
                            self.assertEqual(b"",path.read_bytes(),"measurement authored delivery/ACK")
                    node=next(row for row in record["artifact"]["evidence"]["nodes"] if row["node_id"]=="architect")
                    attention=node["seat_attention"]
                    self.assertEqual(1,node["inbox_depth"])
                    self.assertEqual(1,attention["undelivered_count"])
                    self.assertEqual({"delivered":0,"acknowledged":0,"consumed":0},node["message_progress"])
                    self.assertIn(preparation.message["id"],[row["record_id"] for row in attention["sources"]])
                    attention_rows.append(attention)
                # Establish selected identity/prose/count/receipt conditions
                # for every collected mode before the intended RED assertion.
                if len(attention_rows)==2:
                    for field in ("state","reason","detail","remedy","undelivered_count",
                                  "hold_evidence_state","last_hold_started_at"):
                        self.assertEqual(attention_rows[0][field],attention_rows[1][field],field)
                for attention in attention_rows:
                    self.assertEqual("not_overdue",attention["state"],
                        "raw native arm must preserve intended fresh mail; normalization refusal is not the RED proof")
                    self.assertEqual("fresh_waiting_mail",attention["reason"])
                    self.assertEqual("",attention["detail"])
                    self.assertEqual("",attention["remedy"])
                    self.assertLess(attention["oldest_waiting_age_seconds"],900)
            except Exception as error:
                retained=Path(tempfile.mkdtemp(prefix="bus-scale-arm-clock-red-",dir=REAL_TEMP_ROOT))
                shutil.copytree(base,retained/"case",copy_function=shutil.copy2)
                (retained/"source-bindings.json").write_text(json.dumps({
                    "head":subprocess.check_output(["/usr/bin/git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
                    "initial":initial_sources,"final":sources(),"normalization_refusal":normalization_refusal,
                    "collections":[record["collection"] for record in owner.records],
                    "error_type":type(error).__name__,"error":str(error),
                    "template_canonical_time":pair["context"]["canonical_time"],
                    "selected_id":preparation.message["id"],"original_coordinate":str(base),
                },sort_keys=True)+"\n")
                print("raw native arm-clock RED retained: "+str(retained),flush=True)
                raise


if __name__=="__main__":unittest.main()

"""Native controls for expanded observations, never a scale/performance verdict.

Each case uses the real ruled seed builder and owned child entry. The mode
comparison is at one fixed scale; 1x/10x source anchors are never equated.
"""
from __future__ import annotations

import copy
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from tests.bus_scale_test_profile import StableSourceFixture
from unittest.mock import patch

from tests.bus_scale_case_seeds import make_case_seed_pair,inventory
from tests.bus_scale_test_profile import (full_scale_enabled, seed_counts, load_fixture_module,
                                          banked_send_repository, register_fixture_cleanup)
from tests.temp_roots import REAL_TEMP_ROOT
from tests import bus_scale_observation_fixture as observation_fixture
from tests.bus_scale_observation_fixture import (
    ROOT,
    ADAPTER,
    ENTRY,
    load,
    RecordingOwner,
    UnknownCollectionOwner,
    ForbiddenLaunchOwner,
    AuxiliaryMutationOwner,
    LaneIndexCorruptionOwner,
    VerificationAuxiliaryCorruptionOwner,
)


class ExpandedObservationTests(StableSourceFixture, unittest.TestCase):
    _completed_observations = {}

    @classmethod
    def cleanup_observations(cls):
        retained=list(cls._completed_observations.values())
        cls._completed_observations.clear()
        for owner,_value,_identity in retained:
            if not owner.doCleanups():
                raise AssertionError("completed expanded-observer fixture cleanup failed")

    @staticmethod
    def _observation_identity(base):
        if not base.exists():
            return None
        values=[]
        for path in (base,*sorted(base.rglob("*"))):
            info=path.lstat()
            directory=stat.S_ISDIR(info.st_mode)
            if (info.st_uid!=os.getuid() or not (directory or stat.S_ISREG(info.st_mode))
                    or not directory and info.st_nlink!=1):
                return None
            values.append((path.relative_to(base).as_posix(),info.st_dev,info.st_ino,
                info.st_mode,info.st_uid,info.st_gid,info.st_nlink,info.st_size,
                info.st_mtime_ns,info.st_ctime_ns,None if directory else path.read_bytes()))
        return tuple(values)

    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-expanded-controls-",dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve()
        repository,_=banked_send_repository(self.base)
        self.workspace=self.base/"workspace"
        repository.rename(self.workspace)
        self.workspace.chmod(0o700)
        self.delegate=load(ROOT/"tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py","expanded_owner")
        self.entry=load(ENTRY,"expanded_entry")

    def pair(self,case):
        family="events" if case=="sent" else "registry"
        seed_base=self.base/("seed-"+case)
        seed_base.mkdir(mode=0o700)
        return make_case_seed_pair(seed_base,family,case,self.entry.routine_module(case),self.workspace,
                                   counts=seed_counts())

    def adapter(self):
        result=load(ADAPTER,"expanded_observation_adapter")
        self.assertIsNotNone(result,"missing expanded native observation adapter")
        self.assertTrue(callable(getattr(result,"expanded_observation",None)),"missing closed expanded observation API")
        return result

    def place(self):
        place=self.base/"observation"
        place.mkdir(mode=0o700)
        return place

    def observe(self,case):
        if full_scale_enabled():
            return self._observe_fresh(case)
        from floati import send_prefix
        source=(send_prefix._revision(),tuple((str(path),hashlib.sha256(path.read_bytes()).hexdigest())
            for path in (Path(__file__).resolve(),Path(observation_fixture.__file__).resolve(),ADAPTER,ENTRY)))
        config=(self.workspace/".git/config").read_bytes()
        key=case,source,config
        cache=type(self)._completed_observations
        retained=cache.get(key)
        if retained is not None and self._observation_identity(retained[0].base)!=retained[2]:
            cache.pop(key)
            if not retained[0].doCleanups():
                raise AssertionError("changed expanded-observer fixture cleanup failed")
            retained=None
        if retained is None:
            # Retain this exact genuinely prepared workspace, including any
            # caller-owned prerequisite. Transfer its cleanup stack, rather
            # than inventing a second setup or losing that prerequisite.
            builder=type(self)()
            for name in ("base","workspace","entry","delegate"):
                setattr(builder,name,getattr(self,name))
            builder._cleanups,self._cleanups=self._cleanups,[]
            try:
                value=builder._observe_fresh(case)
                identity=self._observation_identity(builder.base)
                if identity is None:
                    raise AssertionError("completed observer fixture is not ordinary owned storage")
                retained=builder,value,identity
                cache[key]=retained
            except BaseException:
                self._cleanups.extend(builder._cleanups)
                builder._cleanups=[]
                raise
        register_fixture_cleanup(type(self),type(self).cleanup_observations)
        builder,value,_identity=retained
        for name in ("base","workspace","entry","delegate"):
            setattr(self,name,getattr(builder,name))

        def release_if_changed():
            if cache.get(key) is retained and self._observation_identity(builder.base)!=retained[2]:
                cache.pop(key)
                if not builder.doCleanups():
                    raise AssertionError("mutated expanded-observer fixture cleanup failed")

        self.addCleanup(release_if_changed)
        arm,pair,owner,place=value
        recording=RecordingOwner(owner.delegate,copy.deepcopy(owner.expected_seed))
        for name,record in vars(owner).items():
            if name!="delegate":
                setattr(recording,name,copy.deepcopy(record))
        return copy.deepcopy(arm),copy.deepcopy(pair),recording,place

    def _observe_fresh(self,case):
        pair=self.pair(case)
        seed=inventory(pair["1"])
        adapter=self.adapter()
        place=self.place()
        attention=case in {"supervise","watch"}
        owner=RecordingOwner(self.delegate,seed,arm_bank=place if attention else None,
                             scale="1" if attention else None,expected_context=pair["context"] if attention else None)
        if case=="verify" and not full_scale_enabled():
            prove=adapter.auxiliary_proof
            owner.verification_auxiliary_controls=[]

            def controls(case,home,workspace,context,baseline,manifest,states,artifact,normalized,begin,end,**clock):
                positive=prove(case,home,workspace,context,baseline,manifest,states,artifact,normalized,begin,end,**clock)
                self.assertEqual("native_verified_scratch_git_directory_clock",positive["auxiliaries"][1]["kind"])
                genuine=copy.deepcopy(states)
                identity=self._observation_identity(Path(home))
                for mutation in ("payload","population","clock"):
                    changed=copy.deepcopy(states)
                    entries=changed[1]["entries"]
                    if mutation=="payload":
                        entries[".git/description"]["sha256"]="0"*64
                    elif mutation=="population":
                        entries[".git/foreign-empty-directory"]=copy.deepcopy(entries[".git"])
                    else:
                        entries[".git"]["mtime_ns"]=0
                    with self.assertRaisesRegex(RuntimeError,
                            "unrelated retained entry|entry population|outside the collected invocation") as refusal:
                        prove(case,home,workspace,context,baseline,manifest,changed,artifact,normalized,begin,end,**clock)
                    self.assertEqual(genuine,states,"negative proof mutated genuine retained post-state")
                    self.assertEqual(identity,self._observation_identity(Path(home)),
                                     "negative auxiliary proof changed native source identity or bytes")
                    owner.verification_auxiliary_controls.append(dict(mutation=mutation,
                        exception_type=type(refusal.exception).__name__,detail=str(refusal.exception),
                        genuine_post_state_unchanged=True,source_identity_unchanged=True))
                return positive

            with patch.object(adapter,"auxiliary_proof",controls):
                arm=adapter.expanded_observation(owner,case,pair,"1",self.workspace,place,ENTRY)
        else:
            arm=adapter.expanded_observation(owner,case,pair,"1",self.workspace,place,ENTRY)
        fields={"records","bytes","seed_sha256","clone_sha256","child_collected",
                "raw_sha256","native","coverage","metrics"}
        self.assertEqual(fields|({"attention_proof"} if attention else set()),set(arm))
        self.assertTrue(arm["child_collected"])
        self.assertEqual({"cpu":True,"bytes":True,"locks":True},arm["coverage"])
        expected_input=owner.materialized_seed if attention else seed
        primary=expected_input[pair["ledger_relative"]]
        self.assertEqual(primary.count(b"\n"),arm["records"])
        self.assertEqual(len(primary),arm["bytes"])
        self.assertEqual(hashlib.sha256(primary).hexdigest(),arm["seed_sha256"])
        self.assertEqual(arm["seed_sha256"],arm["clone_sha256"])
        self.assertEqual(2,len(owner.launches))
        self.assertEqual(["cpu","diagnostic"],[argv[3] for argv,_,_ in owner.launches])
        self.assertEqual({pair["prepared_home"]},{home for _,_,home in owner.launches},
                         "consent must execute at the same native root in both modes")
        self.assertEqual(seed,inventory(pair["1"]),"immutable source seed changed")
        for mode in ("cpu","diagnostic"):
            bank=place/mode
            self.assertTrue((bank/"scale-fleet").is_dir(),"mode root was not archived before next restore")
            for name in ("stdout.txt","stderr.txt","detail.json","native-delta.json","equivalence.json","collection.json"):
                self.assertTrue((bank/name).is_file(),"missing retained "+mode+" "+name)
            restore=json.loads((place/(mode+"-restore.json")).read_text())
            self.assertEqual(True,restore["seed_unchanged"])
            self.assertEqual(True,restore["restored_exact"])
            self.assertEqual(str(pair["prepared_home"]),restore["root"])
            self.assertEqual(owner.admitted_inputs[("cpu","diagnostic").index(mode)], {p:base64.b64decode(v) for p,v in
                json.loads((bank/"before-raw.json").read_text()).items()})
            collection=json.loads((bank/"collection.json").read_text())
            self.assertTrue(collection["collection"]["child_collected"])
            self.assertEqual(0,collection["collection"]["exit_code"])
            self.assertGreater(collection["cpu_seconds"],0)
        self.assertEqual(hashlib.sha256((place/"collection.json").read_bytes()).hexdigest(),arm["raw_sha256"])
        final=json.loads((place/"final-restore.json").read_bytes())
        self.assertTrue(final["restored_exact"])
        self.assertEqual(final["inventory"],adapter.tree(Path(pair["prepared_home"])),
                         "completed observer did not leave its exact final restored root")
        return arm,pair,owner,place

    def test_ordinary_read_is_a_real_two_mode_native_observation(self):
        arm,*_=self.observe("sent")
        self.assertEqual("sender_receipts_returned",arm["native"]["effect"]["kind"])

    def test_bundle_auxiliary_projects_only_proved_roster_age_leaves_and_retains_raw_bytes(self):
        from floati.root import FloatiRoot
        arm,pair,_owner,place=self.observe("confluence-bundle")
        self.assertEqual("confluence_bundle_written",arm["native"]["effect"]["kind"])
        collected=json.loads((place/"collection.json").read_bytes())["cpu"]
        archived=place/"cpu/auxiliary/2"
        raw=archived.read_bytes()
        raw_inventory=collected["auxiliary_post_state"][2]["inventory"]
        self.assertEqual(len(raw),raw_inventory["entries"]["."]["bytes"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),raw_inventory["entries"]["."]["sha256"])
        comparison=collected["auxiliary_equivalence"]["auxiliaries"][2]["after"]
        self.assertEqual("complete_native_bundle_clock_projection",comparison["content_projection"]["kind"])
        self.assertTrue(comparison["content_projection"]["normalizations"])
        self.assertNotEqual(raw_inventory["entries"]["."]["sha256"],comparison["entries"]["."]["sha256"])
        replay=self.base/"bundle-proof-replay"
        replay.mkdir(mode=0o700)
        home=replay/"scale-fleet"
        shutil.copytree(place/"cpu/scale-fleet",home)
        publication=replay/"routine-bundle.json"
        shutil.copy2(archived,publication)
        root=FloatiRoot.open_direct_home(home,create=False)
        adapter=self.adapter()
        begin=datetime.fromisoformat(collected["wall_begin"])
        end=datetime.fromisoformat(collected["wall_end"])
        artifact=json.loads((place/"cpu/stdout.txt").read_bytes())
        normalized=collected["equivalence"]

        def prove(value):
            return adapter.native_publication("confluence-bundle",root,publication,
                adapter.tree(publication),artifact,pair["context"],begin,end,normalized=value,
                filesystem_clock=collected["filesystem_clock"])

        self.assertEqual(comparison,prove(normalized))
        for fault in ("missing_audit","duplicate_audit","nonage_leaf","age_value"):
            with self.subTest(fault=fault):
                forged=copy.deepcopy(normalized)
                age_audit=next(row for row in forged["normalizations"] if "/roster/" in row["pointer"])
                if fault=="missing_audit":
                    forged["normalizations"].remove(age_audit)
                elif fault=="duplicate_audit":
                    forged["normalizations"].append(copy.deepcopy(age_audit))
                elif fault=="nonage_leaf":
                    forged["delta"]["@publication/routine-bundle.json"]["value"]["roster"][0]["owner"]["value"]="invented owner"
                else:
                    forged["delta"]["@publication/routine-bundle.json"]["value"]["roster"][0]["harness"]["age_seconds"]="<unproved-age>"
                with self.assertRaises(adapter.UnresolvedObservation):
                    prove(forged)
                self.assertEqual(raw,publication.read_bytes())
        publication.write_bytes(raw+b" ")
        with self.assertRaises(adapter.UnresolvedObservation):
            prove(normalized)
        self.assertEqual(raw,archived.read_bytes(),"auxiliary projection changed archived native bytes")

    def native_hint_touch_fixture(self,*,foreign=False):
        from floati.events import EVENT_KINDS,EventLog
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from floati.wake_hints import publish_recipient_hint
        adapter=self.adapter()
        home=self.base/"native-hint-touch"
        root=FloatiRoot.open_direct_home(home,create=True)
        Registry(root).register("scale-worker","codex")
        Registry(root).register("architect","codex")
        EventLog(root).send("scale-worker","architect","floati","a"*40,"note.md",
            "native before hint",idempotency_key="before-hint")
        if foreign:
            self.assertTrue(publish_recipient_hint(root,"unrelated"))
        before,before_raw=adapter.tree(home),adapter.raw_files(home)
        captured=adapter.capture_retained_hint_input(home,before,before_raw)
        begin,filesystem_clock=adapter.capture_filesystem_clock()
        EventLog(root).send("scale-worker","architect","floati","a"*40,"note.md",
            "native after hint",idempotency_key="after-hint")
        if foreign:
            os.utime(home/"state/wake-hints/unrelated/generation",None)
        end=datetime.now(timezone.utc)
        after,after_raw=adapter.tree(home),adapter.raw_files(home)
        return adapter,home,before,after,before_raw,after_raw,begin,end,captured,{"events.jsonl":EVENT_KINDS},filesystem_clock

    def test_retained_hint_touches_require_prechild_identity_and_bounded_native_routing(self):
        adapter,home,before,after,before_raw,after_raw,begin,end,captured,kinds,filesystem_clock=self.native_hint_touch_fixture()
        hint="state/wake-hints/architect/generation"
        self.assertNotEqual(before["entries"][hint]["mtime_ns"],after["entries"][hint]["mtime_ns"])

        def prove(identity,first=begin,last=end):
            return adapter.retained_hint_touches(home,before,after,before_raw,after_raw,
                first,last,identity,ledger_kinds=kinds,filesystem_clock=filesystem_clock)

        proof=prove(captured)
        self.assertEqual({"operation":"touch_empty_hint","kind":"native_wake_hint"},proof[hint])
        retained=adapter.tree(home)
        for fault in ("missing","missing_leaf","substituted_inode","outside_invocation"):
            with self.subTest(fault=fault):
                forged=copy.deepcopy(captured)
                if fault=="missing":
                    forged=None
                elif fault=="missing_leaf":
                    forged["hints"].pop(hint)
                elif fault=="substituted_inode":
                    forged["hints"][hint]["lineage"][-1]["identity"][1]+=1
                with self.assertRaises(adapter.UnresolvedObservation):
                    prove(forged,begin+timedelta(days=1),end+timedelta(days=1)) if fault=="outside_invocation" else prove(forged)
                self.assertEqual(retained,adapter.tree(home),"hint metadata proof changed retained input")

    def test_retained_hint_touch_cannot_borrow_another_recipients_native_append(self):
        adapter,home,before,after,before_raw,after_raw,begin,end,captured,kinds,filesystem_clock=self.native_hint_touch_fixture(foreign=True)
        retained=adapter.tree(home)
        with self.assertRaisesRegex(adapter.UnresolvedObservation,"unreviewed retained hint touch"):
            adapter.retained_hint_touches(home,before,after,before_raw,after_raw,
                begin,end,captured,ledger_kinds=kinds,filesystem_clock=filesystem_clock)
        self.assertEqual(retained,adapter.tree(home))

    def test_seat_board_proves_its_empty_native_inbox_and_lock_before_both_modes(self):
        arm,pair,owner,_place=self.observe("seat-board")
        self.assertEqual(2,len(owner.collections))
        self.assertTrue(all(row["child_collected"] for row in owner.collections))
        self.assertFalse((pair["1"]/"events.jsonl").exists())
        for before in owner.admitted_inputs:
            self.assertEqual(b"",before["events.jsonl.lock"])
            self.assertIn(".inbox-prefix/checkpoint.json",before)

    def test_empty_inbox_warm_input_rejects_foreign_lock_or_sibling_before_launch(self):
        from unittest.mock import patch
        from floati.inbox_prefix import warm
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from tests.test_bus_epoch_roll import _MutationProbe
        for fault in ("lock_bytes","lock_mode","lock_link","sibling"):
            with self.subTest(fault=fault):
                root=FloatiRoot.open_direct_home(self.base/("empty-"+fault),create=True)
                Registry(root).register("builder","Codex")
                expected=inventory(root.path)
                self.assertNotIn("events.jsonl",expected)
                self.assertIsNotNone(warm(root))
                owner=RecordingOwner(self.delegate,expected)
                self.assertEqual(inventory(root.path),owner.verify_inbox_clone(root.path,expected))
                lock=root.path/"events.jsonl.lock"
                if fault=="lock_bytes": lock.write_bytes(b"not an empty native lock")
                if fault=="lock_mode": lock.chmod(0o777)
                if fault=="lock_link":
                    target=self.base/("foreign-"+fault)
                    target.write_bytes(b"")
                    lock.unlink()
                    os.link(target,lock)
                if fault=="sibling":
                    sibling=root.path/".inbox-prefix-foreign"
                    sibling.mkdir(mode=0o700)
                    (sibling/"sentinel").write_bytes(b"not a ruled artifact")
                # Retain hostile link/mode inputs without admitting them as a
                # legal seed. The ordinary seed inventory correctly refuses
                # a shared file before the owner even reaches child launch.
                before=_MutationProbe.snapshot(root)
                with patch.object(self.delegate,"launch_owned_child") as launch:
                    with self.assertRaises((AssertionError,ValueError)):
                        owner.launch_owned_child(["/usr/bin/python3","-B",str(ENTRY),
                            "cpu","seat-board",str(root.path)])
                    launch.assert_not_called()
                self.assertEqual(before,_MutationProbe.snapshot(root),"refusal changed the fixture")

    def test_attention_pair_reuses_known_clock_context_across_both_native_scales(self):
        from tests.bus_scale_workloads import _require_arm
        source_paths=(ADAPTER,ENTRY,ROOT/"tests/bus_scale_arm_clock.py",
                      ROOT/"tests/fixtures/bus_scale_census/bus-scale-routine-reads-2026-10-15.py")
        source_before={str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
        for case in ("supervise","watch"):
            with self.subTest(case=case):
                pair=self.pair(case)
                templates={scale:inventory(pair[scale]) for scale in ("1","10")}
                original_context=copy.deepcopy(pair["context"])
                adapter=self.adapter()
                published=[]
                retained=[]
                proofs=[]
                for scale in ("1","10"):
                    place=self.base/(case+"-scale-"+scale)
                    place.mkdir(mode=0o700)
                    owner=RecordingOwner(self.delegate,templates[scale],arm_bank=place,scale=scale,
                                         expected_context=original_context)
                    arm=adapter.expanded_observation(owner,case,pair,scale,self.workspace,place,ENTRY)
                    _require_arm(case,arm)
                    self.assertEqual(2,len(owner.launches))
                    self.assertEqual(["cpu","diagnostic"],[argv[3] for argv,_,_ in owner.launches])
                    self.assertTrue(all(row["child_collected"] for row in owner.collections))
                    self.assertEqual(original_context,pair["context"],"cached template context must stay immutable")
                    self.assertEqual(templates,{key:inventory(pair[key]) for key in ("1","10")})
                    publication=pair["_clock_publication"]
                    reference=publication["receipt"]
                    self.assertEqual(str(place/"seed-clock.json"),reference["path"])
                    raw=(place/"seed-clock.json").read_bytes()
                    self.assertEqual(hashlib.sha256(raw).hexdigest(),reference["sha256"])
                    self.assertEqual(owner.clock_receipt,raw)
                    context_path=Path(pair["prepared_home"]).parent/"case-context.json"
                    self.assertEqual(publication["context"],json.loads(context_path.read_bytes()))
                    self.assertEqual({key:value for key,value in original_context.items() if key!="canonical_time"},
                        {key:value for key,value in publication["context"].items() if key!="canonical_time"})
                    published.append(publication["context"]["canonical_time"])
                    retained.append((place/"seed-clock.json",raw))
                    proofs.append(arm["attention_proof"]["nodes"])
                    for mode in ("cpu","diagnostic"):
                        artifact=json.loads((place/mode/"stdout.txt").read_bytes())
                        snapshot=artifact["evidence"] if case=="supervise" else artifact["evidence"]["delta"]["snapshot"]
                        attention=next(row["seat_attention"] for row in snapshot["nodes"] if row["node_id"]=="architect")
                        self.assertEqual(("not_overdue","fresh_waiting_mail",1,"",""),
                            tuple(attention[key] for key in ("state","reason","undelivered_count","detail","remedy")))
                        self.assertLess(attention["oldest_waiting_age_seconds"],900)
                        self.assertEqual(owner.admitted_inputs[("cpu","diagnostic").index(mode)],{key:base64.b64decode(value) for key,value in
                            json.loads((place/mode/"before-raw.json").read_bytes()).items()})
                    for path,expected_raw in retained:
                        self.assertEqual(expected_raw,path.read_bytes(),"later arm changed a published clock receipt")
                self.assertNotEqual(published[0],published[1],"second arm reused the first arm's clock")
                self.assertEqual(proofs[0],proofs[1],"real native scale arms crossed attention categories")
        self.assertEqual(source_before,{str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths})

    def test_durable_write_is_restored_and_archived_before_second_native_append(self):
        arm,pair,owner,place=self.observe("presence-report")
        self.assertEqual("presence_report_appended",arm["native"]["effect"]["kind"])
        before=json.loads((place/"diagnostic/before-raw.json").read_text())
        self.assertNotIn("liveness-presence/architect.jsonl",before,
                         "diagnostic inherited CPU's append rather than exact seed")

    def test_native_daemon_consent_executes_at_its_original_canonical_coordinate(self):
        arm,pair,owner,place=self.observe("wake-daemon-serve")
        self.assertEqual("daemon_poll_completed",arm["native"]["effect"]["kind"])
        for mode in ("cpu","diagnostic"):
            artifact=json.loads((place/mode/"stdout.txt").read_text())
            self.assertEqual(pair["context"]["coordinate_digest"],artifact["evidence"]["runtime"]["coordinate_digest"])
            self.assertEqual(1,artifact["evidence"]["completed_native_iterations"])
            self.assertTrue(artifact["evidence"]["owner_reacquired_after"])

    def test_lane_close_restores_native_linked_worktree_and_git_admin_before_each_mode(self):
        arm,pair,owner,place=self.observe("lane-close")
        self.assertEqual("lane_workspace_closed",arm["native"]["effect"]["kind"])
        opened=pair["context"]["opened"]
        original=Path(opened["workspace"])
        self.assertTrue(original.is_dir(),"final exact auxiliary restore lost the original native lane")
        for mode in ("cpu","diagnostic"):
            artifact=json.loads((place/mode/"stdout.txt").read_text())
            self.assertEqual(str(original),artifact["evidence"]["removed"])
            receipt=json.loads((place/(mode+"-restore.json")).read_text())
            self.assertEqual(2,len(receipt["auxiliaries"]))
            self.assertTrue(all(row["restored_exact"] for row in receipt["auxiliaries"]))

    def test_prep_clear_compares_complete_native_readiness_pair_in_both_modes(self):
        for arguments in (("remote","add","origin",str(self.workspace)),
                          ("config","branch.main.remote","origin"),
                          ("config","branch.main.merge","refs/heads/main")):
            subprocess.run(["/usr/bin/git",*arguments],cwd=self.workspace,check=True,
                           stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
        arm,pair,owner,place=self.observe("node-prep-clear")
        self.assertEqual("prep_clear_receipted",arm["native"]["effect"]["kind"])
        for mode in ("cpu","diagnostic"):
            value=json.loads((place/mode/"equivalence.json").read_text())
            readiness=value["delta"]["events.jsonl"]["frames"][1]
            self.assertEqual("<native-invocation-clock>",readiness["observed_at"])
            self.assertEqual(readiness["observed_at"],readiness["readiness"]["observed_at"])

    def test_owner_accepts_only_complete_native_warm_rebuild_of_exact_seed(self):
        from types import SimpleNamespace
        from floati.events import EventLog
        from floati.registry import Registry
        from floati.root import FloatiRoot
        from floati.send_prefix import warm

        seed=self.base/"warm-seed"/"scale-fleet"
        seed.mkdir(mode=0o700,parents=True)
        root=FloatiRoot.open_direct_home(seed,create=True)
        for node in ("scale-worker","architect"):
            Registry(root).register(node,"codex")
        EventLog(root).send("scale-worker","architect","floati","a"*40,"note.md",
            "native warm admission input",idempotency_key="warm-admission-input")
        expected=inventory(seed)
        home=self.base/"warm-clone"/"scale-fleet"
        shutil.copytree(seed,home)
        self.assertIsNotNone(warm(FloatiRoot.open_direct_home(home)))
        actual=inventory(home)
        self.assertNotEqual(expected[".send-prefix/checkpoint.json"],actual[".send-prefix/checkpoint.json"])

        class AdmissionDelegate:
            def __init__(self): self.calls=[]
            def launch_owned_child(self,argv,**kwargs):
                self.calls.append(list(argv))
                return SimpleNamespace(pid=12345)

        delegate=AdmissionDelegate()
        owner=RecordingOwner(delegate,expected)
        argv=["/usr/bin/python3","-B",str(ENTRY),"cpu","node-prep-clear",str(home)]
        owner.launch_owned_child(argv)
        self.assertEqual(1,len(delegate.calls),"valid native warm rebuild never reached admission")
        self.assertEqual(actual,inventory(home),"warm admission proof changed retained input")

        # A correct content-address does not establish reachability or native
        # provenance. The positive admission above prevents an always-refusing
        # owner from satisfying this malformed warmed-clone control.
        extra=b'{"unreviewed":"extra-warmed-clone-node"}'
        target=home/".send-prefix/nodes"/hashlib.sha256(extra).hexdigest()
        target.write_bytes(extra)
        target.chmod(0o600)
        forged=inventory(home)
        with self.assertRaisesRegex((AssertionError,ValueError),"unreachable|unexplained"):
            owner.launch_owned_child(argv)
        self.assertEqual(1,len(delegate.calls),"unexplained warm node reached the delegate")
        self.assertEqual(forged,inventory(home),"warm rejection repaired the forged input")

    def test_verify_binds_exact_git_directory_clock_to_successful_native_scratch_cleanup(self):
        try:
            arm,pair,owner,place=self.observe("verify")
        except Exception:
            retained=Path(tempfile.mkdtemp(prefix="bus-scale-expanded-verify-red-",dir=REAL_TEMP_ROOT))
            shutil.copytree(self.base,retained/"case",copy_function=shutil.copy2)
            print("expanded verify native equivalence RED retained: "+str(retained),flush=True)
            raise
        self.assertEqual("delivery_claim_verified",arm["native"]["effect"]["kind"])
        for mode in ("cpu","diagnostic"):
            proof=json.loads((place/mode/"auxiliary-proof.json").read_text())["proof"]["auxiliaries"][1]
            self.assertEqual("native_verified_scratch_git_directory_clock",proof["kind"])
            self.assertEqual("verified_match",proof["receipt"]["outcome"])
            self.assertTrue(proof["receipt"]["scratch"]["created"])
            self.assertTrue(proof["receipt"]["scratch"]["destroyed"])
            artifact=json.loads((place/mode/"stdout.txt").read_text())
            helper=self.entry.routine_module("verify")
            raw_hash=artifact["evidence"]["output_sha256"]
            bounded=helper._verify_output_proof(raw_hash,artifact["evidence"]["wall_time_seconds"])
            canonical_hash=bounded["canonical_output_sha256"]
            self.assertEqual(canonical_hash,proof["receipt"]["output_sha256"])
            self.assertEqual(canonical_hash,proof["receipt"]["measurement"]["output_sha256"])
            collection=json.loads((place/mode/"collection.json").read_text())
            pointers={"/evidence/output_sha256","/evidence/measurement/output_sha256",
                      "/delta/receipts~1verifications.jsonl/frames/0/output_sha256",
                      "/delta/receipts~1verifications.jsonl/frames/0/measurement/output_sha256"}
            audits={row["pointer"]:row for row in collection["equivalence"]["normalizations"]
                    if row["pointer"] in pointers}
            self.assertEqual(pointers,set(audits))
            for audit in audits.values():
                self.assertEqual(raw_hash,audit["before"])
                self.assertEqual(canonical_hash,audit["after"])
                self.assertEqual("exact bounded stock unittest transcript preimage validated: "+helper._json(bounded),
                                 audit["reason"])
            self.assertEqual(pair["context"]["verification_repo_sha"],collection["observer_scope"]["verification_repo_sha"])

    def test_verify_refuses_unrelated_git_payload_population_and_unbounded_clock_after_native_success(self):
        if not full_scale_enabled():
            return self._bounded_verification_auxiliary_controls()
        for mutation in ("payload","population","clock"):
            with self.subTest(mutation=mutation):
                # Each negative owns an independent exact native preparation.
                child=self.base/("bus-scale-census-owned-verification-negative-"+mutation)
                child.mkdir(mode=0o700)
                repository,_=banked_send_repository(child)
                workspace=child/"workspace"
                repository.rename(workspace)
                workspace.chmod(0o700)
                seed=child/"seeds"
                seed.mkdir(mode=0o700)
                pair=make_case_seed_pair(seed,"registry","verify",self.entry.routine_module("verify"),workspace,
                                         counts=seed_counts())
                place=child/"observation"
                place.mkdir(mode=0o700)
                owner=VerificationAuxiliaryCorruptionOwner(self.delegate,inventory(pair["1"]),workspace,mutation)
                with self.assertRaisesRegex(RuntimeError,"unrelated retained entry|entry population|outside the collected invocation"):
                    self.adapter().expanded_observation(owner,"verify",pair,"1",workspace,place,ENTRY)
                self.assertEqual(1,len(owner.collections))
                proof=json.loads((place/"cpu/auxiliary-proof.json").read_text())
                self.assertEqual("unresolved",proof["status"])
                self.assertTrue(proof["post_state_archived"])
                self.assertTrue((place/"cpu/auxiliary/1/.git").is_dir())

    def _bounded_verification_auxiliary_controls(self):
        """Read actual independent poison results captured before final restore.

        The genuine proof and all three copied-state refusals execute at the
        live native proof boundary during observe(). No proof is replayed at
        the later restored root, whose receipt/inode identity has changed.
        """
        arm,_pair,owner,place=self.observe("verify")
        self.assertEqual("delivery_claim_verified",arm["native"]["effect"]["kind"])
        controls=owner.verification_auxiliary_controls
        self.assertEqual(["payload","population","clock"]*2,[row["mutation"] for row in controls])
        for row in controls:
            with self.subTest(mutation=row["mutation"]):
                self.assertRegex(row["detail"],
                    "unrelated retained entry|entry population|outside the collected invocation")
                self.assertTrue(row["genuine_post_state_unchanged"])
                self.assertTrue(row["source_identity_unchanged"])
        self.assertEqual(2,len(owner.collections))
        for mode in ("cpu","diagnostic"):
            proof=json.loads((place/mode/"auxiliary-proof.json").read_bytes())
            self.assertEqual("validated",proof["status"])
            self.assertTrue((place/mode/"auxiliary/1/.git").is_dir())

    def test_auxiliary_clocks_admit_only_recorded_platform_bounds(self):
        from tests import bus_scale_filesystem_clock as clocks
        adapter=self.adapter()
        realtime=1793230441768300931
        coarse=1793230441767141750
        for platform in ("linux","darwin"):
            with self.subTest(platform=platform),patch.object(clocks.sys,"platform",platform),\
                    patch.object(clocks.time,"time_ns",return_value=realtime),\
                    patch.object(clocks.time,"clock_gettime_ns",return_value=coarse,create=True):
                begin,receipt=clocks.capture_filesystem_clock()
                end=begin+timedelta(seconds=1)
                lower=coarse if platform=="linux" else clocks.epoch_ns(begin)
                upper=clocks.epoch_ns(end)
                for stamp in (lower,upper):
                    adapter.moment_ns(stamp,begin,end,filesystem_clock=receipt)
                for stamp in (lower-1,upper+1,True):
                    with self.assertRaises(adapter.UnresolvedObservation):
                        adapter.moment_ns(stamp,begin,end,filesystem_clock=receipt)
                for fault in ("clock","platform","begin","population","sample"):
                    forged=dict(receipt)
                    if fault=="clock": forged["clock"]="unreviewed clock"
                    elif fault=="platform": forged["platform"]="foreign"
                    elif fault=="begin": forged["wall_begin"]=(begin-timedelta(seconds=1)).isoformat()
                    elif fault=="population": forged["extra"]=True
                    else: forged["realtime_ns"]+=1000
                    with self.assertRaises(adapter.UnresolvedObservation):
                        adapter.moment_ns(lower,begin,end,filesystem_clock=forged)
                if platform=="linux":
                    with self.assertRaises(adapter.UnresolvedObservation):
                        adapter.moment_ns(coarse,begin,end)
                    with patch.object(clocks.time,"clock_gettime_ns",side_effect=OSError("unavailable")):
                        with self.assertRaisesRegex(ValueError,"CLOCK_REALTIME_COARSE"):
                            clocks.capture_filesystem_clock()

    def test_lane_auxiliary_admits_only_two_closed_git_refs_layouts(self):
        adapter=self.adapter()
        begin=datetime(2026,10,28,tzinfo=timezone.utc)
        end=begin+timedelta(seconds=1)
        from tests.bus_scale_filesystem_clock import epoch_ns
        directory={"kind":"directory","mode":0o755,"mtime_ns":epoch_ns(begin)}
        prefix=".git/worktrees/census-lifecycle"
        head=prefix+"/HEAD"
        refs=prefix+"/refs"
        raw=b"ref: refs/heads/owned\n"
        files={head:{"raw":raw,"mode":0o644}}
        before={"kind":"directory","entries":{".":dict(directory)}}
        for has_refs in (False,True):
            with self.subTest(per_worktree_refs=has_refs):
                after=copy.deepcopy(before)
                for path in (".git",".git/worktrees",prefix):
                    after["entries"][path]=dict(directory)
                after["entries"][head]={"kind":"file","mode":0o644,"mtime_ns":epoch_ns(begin),
                    "bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}
                if has_refs: after["entries"][refs]=dict(directory)
                def prove(candidate):
                    return adapter.native_lane_admin_additions(before,candidate,files,prefix,
                        directory_mode=0o755,begin=begin,end=end)
                retained=copy.deepcopy(after)
                prove(after)
                self.assertEqual(retained,after,"proof changed its retained input")
                for fault in ("unknown","missing_file","nonempty_refs","refs_mode","refs_kind","clock"):
                    forged=copy.deepcopy(after)
                    if fault=="unknown": forged["entries"][prefix+"/foreign"]=dict(directory)
                    elif fault=="missing_file": forged["entries"].pop(head)
                    elif fault=="clock": forged["entries"][head]["mtime_ns"]=epoch_ns(begin)-1
                    else:
                        forged["entries"][refs]=dict(directory)
                        if fault=="nonempty_refs": forged["entries"][refs+"/foreign"]=dict(directory)
                        elif fault=="refs_mode": forged["entries"][refs]["mode"]=0o777
                        else: forged["entries"][refs]=dict(forged["entries"][head])
                    with self.assertRaises(adapter.UnresolvedObservation): prove(forged)

    def test_lane_open_proves_generated_checkout_index_and_reflog_auxiliaries(self):
        try:
            arm,pair,owner,place=self.observe("lane-open")
            self.assertEqual("lane_workspace_opened",arm["native"]["effect"]["kind"])
            for mode in ("cpu","diagnostic"):
                proof=json.loads((place/mode/"auxiliary-proof.json").read_text())
                self.assertEqual("validated",proof["status"])
        except Exception:
            retained=Path(tempfile.mkdtemp(prefix="bus-scale-expanded-lane-open-red-",dir=REAL_TEMP_ROOT))
            shutil.copytree(self.base,retained/"case",copy_function=shutil.copy2)
            print("expanded lane-open native auxiliary RED retained: "+str(retained),flush=True)
            raise

    def test_lane_open_rejects_unrelated_auxiliary_file_after_real_native_success(self):
        pair=self.pair("lane-open")
        adapter=self.adapter()
        place=self.place()
        owner=AuxiliaryMutationOwner(self.delegate,inventory(pair["1"]),self.workspace)
        with self.assertRaisesRegex(RuntimeError,"auxiliary entry population"):
            adapter.expanded_observation(owner,"lane-open",pair,"1",self.workspace,place,ENTRY)
        self.assertEqual(1,len(owner.collections))
        self.assertTrue((place/"cpu/auxiliary/1/unrelated-native-read-mutation.txt").is_file())

    def test_lane_open_rejects_corrupt_index_after_real_native_success(self):
        pair=self.pair("lane-open")
        adapter=self.adapter()
        place=self.place()
        owner=LaneIndexCorruptionOwner(self.delegate,inventory(pair["1"]),self.workspace)
        with self.assertRaisesRegex(RuntimeError,"index.*checksum"):
            adapter.expanded_observation(owner,"lane-open",pair,"1",self.workspace,place,ENTRY)
        self.assertEqual(1,len(owner.collections))
        self.assertTrue((place/"cpu/auxiliary/1/.git/worktrees/census-lifecycle/index").is_file())

    def test_reclaim_uses_restored_actual_declared_age_and_preserves_private_payload(self):
        arm,pair,owner,place=self.observe("reclaim")
        self.assertEqual("reclaim_plan_returned",arm["native"]["effect"]["kind"])
        candidate=Path(pair["context"]["candidate"])
        payload=candidate/"payload.bin"
        self.assertEqual(pair["context"]["payload_sha256"],hashlib.sha256(payload.read_bytes()).hexdigest())
        mtimes=[]
        for mode in ("cpu","diagnostic"):
            artifact=json.loads((place/mode/"stdout.txt").read_text())["evidence"]
            self.assertTrue(artifact["dry_run"])
            self.assertEqual([],artifact["deleted"])
            self.assertEqual(0,artifact["df_moved_bytes"])
            self.assertIn(str(candidate),[row["path"] for row in artifact["listed"]])
            receipt=json.loads((place/(mode+"-restore.json")).read_text())
            relative=payload.relative_to(pair["preparation_bank"]).as_posix()
            mtimes.append(receipt["auxiliaries"][0]["inventory"]["entries"][relative]["mtime_ns"])
        self.assertEqual(mtimes[0],mtimes[1])
        self.assertEqual(mtimes[0],payload.stat().st_mtime_ns)

    def test_same_native_case_pair_can_reuse_its_stable_root_for_next_scale(self):
        arm,pair,owner,first=self.observe("presence-report")
        second=self.base/"observation-second-scale"
        second.mkdir(mode=0o700)
        adapter=self.adapter()
        next_owner=RecordingOwner(self.delegate,inventory(pair["10"]))
        next_arm=adapter.expanded_observation(next_owner,"presence-report",pair,"10",self.workspace,second,ENTRY)
        self.assertEqual(seed_counts()[1] * arm["records"], seed_counts()[0] * next_arm["records"])
        self.assertEqual(arm["native"],next_arm["native"])
        self.assertEqual(2,len(next_owner.launches))
        self.assertEqual({pair["prepared_home"]},{home for _,_,home in next_owner.launches})
        self.assertTrue((first/"cpu/stdout.txt").is_file(),"next restore erased the first archived native output")
        self.assertTrue((second/"diagnostic/collection.json").is_file())

    def test_forged_existing_restore_receipt_cannot_authorize_a_child(self):
        pair=self.pair("sent")
        adapter=self.adapter()
        place=self.place()
        forged=place/"cpu-restore.json"
        raw=b'{"restored_exact":true,"seed_unchanged":true,"forged":true}\n'
        forged.write_bytes(raw)
        owner=RecordingOwner(self.delegate,inventory(pair["1"]))
        with self.assertRaises((ValueError,RuntimeError,FileExistsError)):
            adapter.expanded_observation(owner,"sent",pair,"1",self.workspace,place,ENTRY)
        self.assertEqual([],owner.launches)
        self.assertEqual(raw,forged.read_bytes())

    def test_uncollected_receipt_stops_before_restore_or_second_child(self):
        pair=self.pair("sent")
        adapter=self.adapter()
        place=self.place()
        owner=UnknownCollectionOwner(self.delegate,inventory(pair["1"]))
        with self.assertRaises(RuntimeError) as caught:
            adapter.expanded_observation(owner,"sent",pair,"1",self.workspace,place,ENTRY)
        self.assertTrue(getattr(caught.exception,"uncollected_child",False))
        self.assertEqual(1,len(owner.launches))
        self.assertFalse((place/"diagnostic-restore.json").exists())
        self.assertTrue(pair["prepared_home"].is_dir(),"unknown child ownership cannot permit root removal")
        receipt=json.loads((place/"cpu-collection.json").read_text())
        self.assertIs(receipt["collection"]["child_collected"],False)

    def test_observation_bank_must_be_disjoint_from_every_restore_and_seed_root(self):
        adapter=self.adapter()
        for coordinate in ("prepared_home","1","preparation_bank","workspace"):
            with self.subTest(coordinate=coordinate):
                seed_base=self.base/("boundary-seed-"+coordinate)
                seed_base.mkdir(mode=0o700)
                pair=make_case_seed_pair(seed_base,"events","sent",self.entry.routine_module("sent"),self.workspace,
                                         counts=seed_counts())
                selected=self.workspace if coordinate=="workspace" else pair[coordinate]
                place=selected/"unsafe-observation"
                place.mkdir(mode=0o700)
                owner=ForbiddenLaunchOwner()
                caught=None
                try:
                    adapter.expanded_observation(owner,"sent",pair,"1",self.workspace,place,ENTRY)
                except Exception as error:
                    caught=error
                self.assertEqual([],owner.launches,"overlapping restore bank reached child launch")
                self.assertIsInstance(caught,RuntimeError,"overlap needs a typed pre-mutation refusal")
                self.assertRegex(str(caught),"overlap|disjoint|outside","refusal must identify the unsafe bank boundary")

    def test_read_rejects_unrelated_auxiliary_mutation_after_real_owned_collection(self):
        if not full_scale_enabled():
            return self._bounded_unrelated_auxiliary_control()
        pair=self.pair("sent")
        adapter=self.adapter()
        place=self.place()
        owner=AuxiliaryMutationOwner(self.delegate,inventory(pair["1"]),self.workspace)
        caught=None
        try:
            adapter.expanded_observation(owner,"sent",pair,"1",self.workspace,place,ENTRY)
        except RuntimeError as error:
            caught=error
        retained=Path(tempfile.mkdtemp(prefix="bus-scale-expanded-aux-negative-",dir=REAL_TEMP_ROOT))
        shutil.copytree(place,retained/"observation",copy_function=shutil.copy2)
        print("expanded auxiliary negative evidence: "+str(retained),flush=True)
        self.assertTrue(owner.collections and owner.collections[0]["child_collected"])
        self.assertTrue((place/"cpu/auxiliary/1/unrelated-native-read-mutation.txt").is_file(),
                        "refusal must retain the actual unexpected post-state before restoration")
        self.assertIsInstance(caught,RuntimeError,"native read accepted an unrelated workspace mutation")

    def _bounded_unrelated_auxiliary_control(self):
        """Exercise exact auxiliary archive/proof on a tiny native preparation."""
        adapter=self.adapter()
        pair=self.pair("sent")
        baseline,manifest=adapter.baseline("sent",pair,self.workspace)
        states=[adapter.tree(Path(row["root"])) for row in manifest["roots"]]
        begin=datetime.now(timezone.utc)
        positive=adapter.auxiliary_proof("sent",pair["prepared_home"],self.workspace,pair["context"],
            baseline,manifest,states,{}, {},begin,datetime.now(timezone.utc))
        self.assertTrue(all(row["kind"]=="unchanged" for row in positive["auxiliaries"]))
        mutation=self.workspace/"unrelated-native-read-mutation.txt"
        mutation.write_text("bounded post-state mutation, not native child testimony\n")
        states=[adapter.tree(Path(row["root"])) for row in manifest["roots"]]
        place=self.place()/"cpu"/"auxiliary"
        place.mkdir(parents=True,mode=0o700)
        for index,row in enumerate(manifest["roots"]):
            adapter.copy_exact(Path(row["root"]),place/str(index),states[index])
        self.assertTrue((place/"1/unrelated-native-read-mutation.txt").is_file(),
                        "unexpected post-state must remain in the exact auxiliary archive")
        saved=copy.deepcopy(states)
        with self.assertRaisesRegex(RuntimeError,"unrelated auxiliary root"):
            adapter.auxiliary_proof("sent",pair["prepared_home"],self.workspace,pair["context"],
                baseline,manifest,states,{}, {},begin,datetime.now(timezone.utc))
        self.assertEqual(saved,[adapter.tree(Path(row["root"])) for row in manifest["roots"]],
                         "auxiliary refusal changed the retained post-state")

    def test_native_teardown_admits_only_its_exact_private_empty_waiter_lock(self):
        if not full_scale_enabled():
            return self._bounded_teardown_waiter_lock_controls()
        from tests.bus_scale_workloads import _require_arm
        adapter=self.adapter()
        helper=self.entry.routine_module("seat-teardown")
        census=load(ROOT/"tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py","waiter_lock_native_census")
        pair,workspace=census.workload_seed({},"registry","seat-teardown",self.workspace,self.base,self.entry,{},
                                           counts=seed_counts())
        seed=inventory(pair["1"])
        digest=hashlib.sha256(helper.SESSION.encode()).hexdigest()
        exact="state/waiter-invocations/"+helper.ACTOR+"/"+digest+".lock"
        retained=Path(tempfile.mkdtemp(prefix="bus-scale-teardown-waiter-lock-effects-",dir=REAL_TEMP_ROOT))
        print("native teardown waiter-lock effects retained: "+str(retained),flush=True)
        positive=self.base/"waiter-lock-positive"
        positive.mkdir(mode=0o700)
        owner=RecordingOwner(self.delegate,seed)
        arm=adapter.expanded_observation(owner,"seat-teardown",pair,"1",workspace,positive,ENTRY)
        _require_arm("seat-teardown",arm)
        self.assertEqual(2,len(owner.collections))
        for mode in ("cpu","diagnostic"):
            self.assertTrue((positive/mode/"prefix-effect-proof.json").is_file(),
                            "positive must retain its complete pre-child native prefix proof")
            effect=json.loads((positive/mode/"equivalence.json").read_bytes())
            self.assertEqual({"operation":"create_lock"},effect["delta"][exact])
        shutil.copytree(positive,retained/"positive",copy_function=shutil.copy2)

        class LockMutationOwner(RecordingOwner):
            def __init__(self,delegate,expected_seed,kind):
                super().__init__(delegate,expected_seed)
                self.kind=kind
                self.injections=[]

            def collect_owned_child(self,child,**kwargs):
                result=super().collect_owned_child(child,**kwargs)
                if result.get("child_collected") is not True or result.get("exit_code")!=0:
                    raise AssertionError("lock mutation requires a genuinely completed native teardown")
                argv,_pid,home=next(row for row in self.launches if row[1]==child.pid)
                raw=json.loads((home.parent/("expanded-"+argv[3]+"-stdout.txt")).read_bytes())
                if raw.get("status")!="ok" or raw.get("evidence",{}).get("step")!="completed":
                    raise AssertionError("lock mutation requires a native completed artifact")
                native=home/exact
                if native.read_bytes()!=b"" or stat.S_IMODE(native.lstat().st_mode)!=0o600:
                    raise AssertionError("mutation lost the exact native private empty waiter lock")
                relative=exact
                if self.kind=="foreign-node":
                    relative="state/waiter-invocations/other-node/"+digest+".lock"
                elif self.kind=="foreign-session":
                    relative="state/waiter-invocations/"+helper.ACTOR+"/"+hashlib.sha256(b"other-session").hexdigest()+".lock"
                target=home/relative
                target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                target.write_bytes(b"unreviewed waiter state" if self.kind=="nonempty" else b"")
                target.chmod(0o644 if self.kind=="public-mode" else 0o600)
                self.injections.append({"kind":self.kind,"relative":relative,
                    "synthetic_post_collection_variant":True,"native_execution":False,
                    "performance_evidence":False,"child_pid":child.pid})
                return result

        reasons={"foreign-node":"unreviewed or nonempty teardown lock",
                 "foreign-session":"unreviewed or nonempty teardown lock",
                 "nonempty":"unreviewed teardown ledger append",
                 "public-mode":"teardown waiter invocation lock must be private"}
        for kind,reason in reasons.items():
            with self.subTest(kind=kind):
                place=self.base/("waiter-lock-"+kind)
                place.mkdir(mode=0o700)
                owner=LockMutationOwner(self.delegate,seed,kind)
                with self.assertRaises(ValueError) as refusal:
                    adapter.expanded_observation(owner,"seat-teardown",pair,"1",workspace,place,ENTRY)
                self.assertEqual("bus_scale_teardown_unresolved",getattr(refusal.exception,"code",None))
                self.assertIn(reason,str(refusal.exception),"negative must reach the lock-specific proof")
                self.assertEqual(1,len(owner.injections))
                self.assertTrue(owner.collections[0]["child_collected"])
                self.assertEqual(0,owner.collections[0]["exit_code"])
                self.assertEqual(seed,inventory(pair["1"]),"negative changed the immutable native input")
                shutil.copytree(place,retained/kind,copy_function=shutil.copy2)
                (retained/kind/"injection.json").write_text(json.dumps({"injections":owner.injections,
                    "refusal":str(refusal.exception),"collection":owner.collections},sort_keys=True)+"\n")
                # Failed observers preserve their outputs. This test owns the
                # collected child and its exact files; retain verified copies
                # before clearing only those outputs for the next variant.
                self.assertEqual(1,len(owner.launches))
                argv,_pid,home=owner.launches[0]
                for suffix,archived in (("stdout.txt","stdout.txt"),
                                        ("stderr.txt","stderr.txt"),
                                        ("detail.json","detail.json")):
                    output=home.parent/("expanded-"+argv[3]+"-"+suffix)
                    info=output.lstat()
                    self.assertTrue(stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
                                    and info.st_nlink==1,"only exact owned ordinary child outputs may be removed")
                    self.assertEqual((retained/kind/argv[3]/archived).read_bytes(),output.read_bytes(),
                                     "negative output must be archived exactly before reuse")
                    output.unlink()

    def _bounded_teardown_waiter_lock_controls(self):
        """Reuse one live completed operation for every lock-only poison."""
        from tests.bus_scale_workloads import _require_arm
        from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import raw_candidate_delta
        adapter=self.adapter()
        native=adapter.load_entry(ENTRY)
        helper=native.routine_module("seat-teardown")
        census=load(ROOT/"tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py","bounded_waiter_lock_census")
        pair,workspace=census.workload_seed({},"registry","seat-teardown",self.workspace,self.base,self.entry,{},
                                           counts=seed_counts())
        seed=inventory(pair["1"])
        place=self.place()
        owner=RecordingOwner(self.delegate,seed)
        normalize=helper.normalize
        digest=hashlib.sha256(helper.SESSION.encode()).hexdigest()
        exact="state/waiter-invocations/"+helper.ACTOR+"/"+digest+".lock"
        reasons={"foreign-node":"unreviewed or nonempty teardown lock",
                 "foreign-session":"unreviewed or nonempty teardown lock",
                 "nonempty":"unreviewed teardown ledger append",
                 "public-mode":"teardown waiter invocation lock must be private"}
        visited=[]

        def identities(home):
            values={}
            for path in home.rglob("*"):
                if path.is_file() and not path.name.endswith(".lock"):
                    info=path.lstat()
                    values[path.relative_to(home).as_posix()]=(
                        info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_size,
                        info.st_mtime_ns,info.st_ctime_ns,path.read_bytes())
            return values

        def controls(case,delta,artifact,context,**bounds):
            positive=normalize(case,delta,artifact,context,**bounds)
            if visited:
                return positive
            home=Path(bounds["home"])
            original_tree=adapter.tree(home)
            source_identities=identities(home)
            native_lock=home/exact
            self.assertEqual(b"",native_lock.read_bytes())
            self.assertEqual(0o600,stat.S_IMODE(native_lock.lstat().st_mode))
            for kind,reason in reasons.items():
                with self.subTest(kind=kind):
                    relative=exact
                    if kind=="foreign-node":
                        relative="state/waiter-invocations/other-node/"+digest+".lock"
                    elif kind=="foreign-session":
                        relative="state/waiter-invocations/"+helper.ACTOR+"/"+hashlib.sha256(b"other-session").hexdigest()+".lock"
                    target=home/relative
                    old=target.read_bytes() if target.exists() else None
                    old_info=target.lstat() if target.exists() else None
                    parents={path:path.lstat() for path in target.parents if path==home or home in path.parents
                             if path.exists()}
                    missing=[path for path in target.parents if path!=home and home in path.parents and not path.exists()]
                    try:
                        target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                        target.write_bytes(b"unreviewed waiter state" if kind=="nonempty" else b"")
                        target.chmod(0o644 if kind=="public-mode" else 0o600)
                        after=helper.snapshot(home)
                        changed=raw_candidate_delta(bounds["before_raw_files"],after)
                        with self.assertRaisesRegex(ValueError,reason) as refusal:
                            normalize(case,changed,artifact,context,**bounds)
                        self.assertEqual("bus_scale_teardown_unresolved",getattr(refusal.exception,"code",None))
                        self.assertEqual(after,helper.snapshot(home),"lock-specific refusal must remain read-only")
                        visited.append(kind)
                    finally:
                        if old is None:
                            target.unlink()
                        else:
                            if target.read_bytes()!=old:
                                target.write_bytes(old)
                            target.chmod(stat.S_IMODE(old_info.st_mode))
                            os.utime(target,ns=(old_info.st_atime_ns,old_info.st_mtime_ns))
                        for path in missing:
                            path.rmdir()
                        for path,info in parents.items():
                            os.utime(path,ns=(info.st_atime_ns,info.st_mtime_ns))
                    self.assertEqual(source_identities,identities(home),
                                     "lock-only replay changed an authoritative source identity")
                    self.assertEqual(original_tree,adapter.tree(home),
                                     "lock-only replay failed to restore exact bytes, modes and mtimes")
            return positive

        with patch.object(adapter,"load_entry",return_value=native),\
                patch.object(native,"routine_module",return_value=helper),\
                patch.object(helper,"normalize",controls):
            arm=adapter.expanded_observation(owner,"seat-teardown",pair,"1",workspace,place,ENTRY)
        _require_arm("seat-teardown",arm)
        self.assertEqual(list(reasons),visited)
        self.assertEqual(2,len(owner.collections))
        self.assertTrue(all(row["child_collected"] and row["exit_code"]==0 for row in owner.collections))
        self.assertEqual(seed,inventory(pair["1"]),"lock-only replays changed immutable native input")
        for mode in ("cpu","diagnostic"):
            effect=json.loads((place/mode/"equivalence.json").read_bytes())
            self.assertEqual({"operation":"create_lock"},effect["delta"][exact])

    def test_bounded_native_selection_covers_every_harness_execution_path(self):
        from floati.cli import _parser
        from tests.bus_scale_workloads import scope_contract
        expected=set(scope_contract(_parser())["cases"])-set(self.adapter().OLD_CASES)
        classes,selected=self._bounded_representatives(expected)
        self.assertEqual(expected,set().union(*classes.values()))
        self.assertGreater(len(classes),1)
        self.assertLess(len(selected),len(expected),
                        "bounded controls must represent harness paths, not enumerate native commands")
        self.assertEqual({min(cases) for cases in classes.values()},set(selected))
        for signature,cases in classes.items():
            with self.subTest(signature=signature):
                self.assertEqual(1,len(cases&set(selected)))
                with self.assertRaisesRegex(ValueError,"uncovered.*execution path"):
                    self._require_bounded_paths(classes,set(selected)-cases)
        from tests.bus_scale_execution_paths import execution_path_classes
        original_read=Path.read_text
        source=original_read(ADAPTER)
        marker='    """Return the established arm schema after two fully archived native modes."""\n'
        self.assertEqual(1,source.count(marker))
        unsupported={
            "method_guard": '    if case.startswith("role-show"):\n        pass\n',
            "reversed_comparison": '    if "role-show" == case:\n        pass\n',
            "match_guard": '    match case:\n        case "role-show":\n            pass\n',
            "assigned_alias": '    selected_case=case\n    if selected_case == "role-show":\n        pass\n',
            "unresolved_helper": '    if _coverage_missing_route(case):\n        pass\n',
        }
        for mutation,guard in unsupported.items():
            changed=source.replace(marker,marker+guard)
            def mutated_read(path,*args,_changed=changed,**kwargs):
                return _changed if path==ADAPTER else original_read(path,*args,**kwargs)
            with self.subTest(routing=mutation),patch.object(Path,"read_text",mutated_read):
                with self.assertRaisesRegex(ValueError,"routing"):
                    execution_path_classes(expected)
        keyword_helper=('\ndef _coverage_keyword_route(kind):\n'
                        '    if kind == "role-show":\n        return True\n    return False\n')
        changed=source.replace(marker,marker+'    if _coverage_keyword_route(kind=case):\n        pass\n')+keyword_helper
        def keyword_helper_read(path,*args,**kwargs):
            return changed if path==ADAPTER else original_read(path,*args,**kwargs)
        with self.subTest(routing="renamed_keyword_parameter"),patch.object(Path,"read_text",keyword_helper_read):
            with self.assertRaisesRegex(ValueError,"routing"):
                execution_path_classes(expected)
        helper=('\ndef _coverage_added_route(case):\n'
                '    if case == "role-show":\n        return True\n    return False\n')
        changed=source.replace(marker,marker+'    if _coverage_added_route(case):\n        pass\n')+helper
        def added_helper_read(path,*args,**kwargs):
            return changed if path==ADAPTER else original_read(path,*args,**kwargs)
        with patch.object(Path,"read_text",added_helper_read):
            discovered=execution_path_classes(expected)
        show=next(signature for signature,cases in discovered.items() if "role-show" in cases)
        listing=next(signature for signature,cases in discovered.items() if "role-list" in cases)
        self.assertNotEqual(show,listing,"a newly called observer helper needs its own represented path")

    @staticmethod
    def _bounded_representatives(expected):
        from tests.bus_scale_execution_paths import execution_path_classes,representative_cases
        classes=execution_path_classes(expected)
        return classes,representative_cases()

    @staticmethod
    def _require_bounded_paths(classes,selected):
        from tests.bus_scale_execution_paths import require_execution_paths
        require_execution_paths(classes,selected)

    def test_all_expanded_cases_complete_real_native_same_scale_modes(self):
        from floati.cli import _parser
        from tests.bus_scale_workloads import scope_contract,_require_arm
        adapter=self.adapter()
        expected=set(scope_contract(_parser())["cases"])-set(adapter.OLD_CASES)
        self.assertEqual(62,len(expected),"every expanded case, including verify and teardown, must qualify")
        if not full_scale_enabled():
            return self._bounded_all_expanded_cases(expected)
        # Vector 5 restores every command at both original scales with genuine
        # owned-child transport. Defaults represent the harness paths below.
        selected = expected
        self.assertTrue(selected <= expected)
        completed=[]
        for case,scale in ((case,scale) for case in sorted(selected) for scale in ("1","10")):
            with self.subTest(case=case,scale=scale):
                case_root=self.base/("bus-scale-census-owned-case-"+case+"-"+scale)
                case_root.mkdir(mode=0o700)
                repository,_=banked_send_repository(case_root)
                workspace=case_root/"workspace"
                repository.rename(workspace)
                workspace.chmod(0o700)
                for arguments in (("remote","add","origin",str(workspace)),
                                  ("config","branch.main.remote","origin"),
                                  ("config","branch.main.merge","refs/heads/main")):
                    subprocess.run(["/usr/bin/git",*arguments],cwd=workspace,check=True,
                                   stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=30)
                seed_base=case_root/"seeds"
                seed_base.mkdir(mode=0o700)
                place=case_root/"observation"
                place.mkdir(mode=0o700)
                try:
                    helper=self.entry.routine_module(case)
                    if case=="seat-teardown":
                        census=load(ROOT/"tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py","expanded_all_native_census")
                        pair,workspace=census.workload_seed({},"registry",case,workspace,case_root,self.entry,{},
                                                           counts=seed_counts())
                    else:
                        pair=make_case_seed_pair(seed_base,"registry",case,helper,workspace,counts=seed_counts())
                    attention=case in {"supervise","watch"}
                    owner=RecordingOwner(self.delegate,inventory(pair[scale]),
                        arm_bank=place if attention else None,scale=scale if attention else None,
                        expected_context=pair["context"] if attention else None)
                    arm=adapter.expanded_observation(owner,case,pair,scale,workspace,place,ENTRY)
                    _require_arm(case,arm)
                    self.assertEqual(2,len(owner.collections))
                    self.assertTrue(all(row["child_collected"] for row in owner.collections))
                    completed.append((case,scale))
                    print("expanded native correctness complete: "+case,flush=True)
                except Exception as error:
                    retained=Path(tempfile.mkdtemp(prefix="bus-scale-expanded-native-red-"+case+"-",dir=REAL_TEMP_ROOT))
                    shutil.copytree(case_root,retained/"case",copy_function=shutil.copy2)
                    (retained/"failure.json").write_text(json.dumps({"case":case,"type":type(error).__name__,
                        "detail":str(error),"original_coordinate":str(case_root)},sort_keys=True)+"\n")
                    print("expanded native correctness RED retained: "+str(retained),flush=True)
                    raise
        self.assertEqual([(case,scale) for case in sorted(selected) for scale in ("1","10")],completed,
                         "expanded native qualification cannot omit unresolved cases or scales")

    def _bounded_all_expanded_cases(self,expected):
        """Run one deterministic representative through each observer path.

        Every representative executes the same restoration, native entry,
        collection and complete raw/auxiliary proofs as the explicit census.
        Both observer modes remain real; only scale/host timing is explicit.
        """
        from tests.bus_scale_workloads import _require_arm
        classes,selected=self._bounded_representatives(expected)
        self._require_bounded_paths(classes,selected)
        completed=[]
        for case in sorted(selected):
            with self.subTest(case=case):
                if case=="seat-teardown":
                    census=load(ROOT/"tests/fixtures/bus_scale_census/census-bus-scale-2026-10-09.py","bounded_expanded_teardown")
                    pair,workspace=census.workload_seed({},"registry",case,self.workspace,self.base,self.entry,{},
                                                       counts=seed_counts())
                    place=self.base/"bounded-teardown-observation"
                    place.mkdir(mode=0o700)
                    owner=RecordingOwner(self.delegate,inventory(pair["1"]))
                    arm=self.adapter().expanded_observation(owner,case,pair,"1",workspace,place,ENTRY)
                    _require_arm(case,arm)
                    self.assertEqual(2,len(owner.collections))
                    self.assertTrue(all(row["child_collected"] and row["exit_code"]==0 for row in owner.collections))
                else:
                    control=type(self)()
                    try:
                        control.setUp()
                        arm,_pair,owner,_place=control.observe(case)
                        _require_arm(case,arm)
                        self.assertEqual(2,len(owner.collections))
                        self.assertTrue(all(row["child_collected"] and row["exit_code"]==0
                                            for row in owner.collections))
                    finally:
                        self.assertTrue(control.doCleanups())
                completed.append(case)
        self.assertEqual(sorted(selected),completed,"bounded native successors cannot omit a harness path")


if __name__=="__main__": unittest.main()

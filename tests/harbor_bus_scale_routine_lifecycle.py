"""Native private-fixture correctness controls for lifecycle/runtime census cases."""
from __future__ import annotations

import importlib.util
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timezone
import base64
import copy
from tests.bus_scale_test_profile import clone_fixture
import hashlib
import io
import json
import os
from pathlib import Path
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_filesystem_clock import capture_filesystem_clock
from unittest.mock import patch

from floati import cli
from floati.registry import Registry
from floati.root import FloatiRoot

from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import full_scale_enabled, load_fixture_module, bounded_cli_parser, stable_fixture_sources
from tests.bus_scale_lifecycle_fixture import (
    ROOT,
    HELPER,
    ENTRY,
    CASES,
    load,
    raw_delta,
    owned_git,
    fixture,
    execute,
    _fixture_identity,
)


class BusScaleRoutineLifecycleContractTests(StableSourceFixture, unittest.TestCase):
    _completed_pairs = {}

    @classmethod
    def tearDownClass(cls):
        for temporary,_record,_identity in cls._completed_pairs.values():
            temporary.cleanup()
        cls._completed_pairs.clear()

    @contextmanager
    def completed_pair(self,case):
        """Two actual starts, shared only while the completed live roots agree."""
        source=tuple((str(path),hashlib.sha256(path.read_bytes()).hexdigest())
                     for path in (HELPER,ENTRY))
        key=full_scale_enabled(),case,source
        cache=type(self)._completed_pairs
        retained=cache.get(key)
        if retained is not None and _fixture_identity(retained[1]["base"])!=retained[2]:
            cache.pop(key)
            retained[0].cleanup()
            retained=None
        if retained is None:
            temporary=tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-lifecycle-pair-",dir=REAL_TEMP_ROOT)
            try:
                record=self._make_completed_pair(Path(temporary.name).resolve(),case)
                retained=temporary,record,_fixture_identity(record["base"])
                cache[key]=retained
            except BaseException:
                temporary.cleanup()
                raise
        try:
            yield copy.deepcopy(retained[1]) if full_scale_enabled() else clone_fixture(retained[1])
        finally:
            if _fixture_identity(retained[1]["base"])!=retained[2]:
                cache.pop(key,None)
                retained[0].cleanup()

    @stable_fixture_sources()
    def _make_completed_pair(self,base,case):
        from tests.bus_scale_census import _warm_fixture
        helper=load(HELPER,"lifecycle_completed_pair_helper")
        entry=load(ENTRY,"lifecycle_completed_pair_entry")
        adapter=load(ROOT/"tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py",
                     "lifecycle_completed_pair_auxiliary")
        workspace=base/"workspace"
        workspace.mkdir(mode=0o700)
        owned_git(workspace)
        if case=="wake-callback":
            root=FloatiRoot.open(base/"callback-root","scale-fleet")
            home=root.tenant_home
            home.chmod(0o700)
        else:
            home=base/"scale-fleet"
            home.mkdir(mode=0o700)
            root=FloatiRoot.open_direct_home(home)
        for node in ("scale-worker","architect"):
            Registry(root).register(node,"codex")
        bank=base/"native-preparation"
        bank.mkdir(mode=0o700)
        context=helper.prepare(case,home,workspace,bank)
        seed=base/"immutable-seed"/"scale-fleet"
        seed.parent.mkdir(mode=0o700)
        shutil.copytree(home,seed)
        pair=dict(prepared_home=home,preparation_bank=bank,context=context)
        seed_tree=adapter.tree(seed)
        baseline,manifest=adapter.baseline(case,pair,workspace)
        observations=[]
        for mode in ("cpu","diagnostic"):
            adapter.restore(pair,seed,seed_tree,baseline,manifest,base/(mode+"-restore.json"))
            _warm_fixture(home)
            observed={}
            execute(self,helper,case,home,workspace,context,entry=entry,observation=observed)
            states=[adapter.tree(Path(row["root"])) for row in manifest["roots"]]
            observed["auxiliary_proof"]=adapter.auxiliary_proof(case,home,workspace,context,
                baseline,manifest,states,observed["artifact"],observed["normal"],observed["begin"],observed["end"],
                filesystem_clock=observed["filesystem_clock"])
            observed["mode"]=mode
            observations.append(observed)
            self.assertEqual(seed_tree,adapter.tree(seed),"actual start changed its immutable native seed")
        self.assertEqual(observations[0]["normal"]["artifact"],observations[1]["normal"]["artifact"])
        self.assertEqual(observations[0]["normal"]["delta"],observations[1]["normal"]["delta"])
        self.assertEqual(observations[0]["auxiliary_proof"],observations[1]["auxiliary_proof"])
        return dict(base=base,workspace=workspace,bank=bank,seed=seed,context=context,
                    observations=observations,primary=observations[-1])

    def test_lifecycle_helper_has_the_closed_native_interface(self):
        helper = load(HELPER, "bus_scale_lifecycle_contract")
        self.assertIsNotNone(helper, "missing native lifecycle/runtime census helper")
        self.assertEqual(CASES, helper.CASES)
        for name in ("prepare", "arguments", "execute_seam", "witness", "normalize"):
            self.assertTrue(callable(getattr(helper, name, None)), name)

    def test_current_entry_routes_new_native_lifecycle_and_runtime_cases(self):
        entry = load(ENTRY, "bus_scale_lifecycle_entry_contract")
        self.assertIsNotNone(entry)
        helper=load(HELPER,"bus_scale_lifecycle_route_helper")
        self.assertIsNotNone(helper)
        if not full_scale_enabled():
            from tests.bus_scale_execution_paths import representative_cases
            for case in sorted(representative_cases(CASES)):
                with self.subTest(case=case),self.completed_pair(case) as record:
                    for observation in record["observations"]:
                        argv=observation["argv"]
                        if case=="wake-daemon-serve":
                            self.assertIsNone(argv,"daemon uses the full native one-iteration serve seam")
                        else:
                            self.assertIsInstance(argv,list)
                            self.assertTrue(argv)
            return
        for case in sorted(CASES):
            with self.subTest(case=case),fixture(case) as (bank,home,workspace):
                context=helper.prepare(case,home,workspace,bank)
                argv=entry.arguments(case,home,workspace,context)
                if case=="wake-daemon-serve":
                    self.assertIsNone(argv,"daemon uses the full native one-iteration serve seam")
                else:
                    self.assertIsInstance(argv,list)
                    self.assertTrue(argv)

    def test_native_lifecycle_cases_prove_positive_work_and_retain_all_raw_evidence(self):
        helper=load(HELPER,"bus_scale_lifecycle_native_helper")
        self.assertIsNotNone(helper)
        if not full_scale_enabled():
            for case in sorted(CASES):
                with self.subTest(case=case),self.completed_pair(case) as record:
                    context=record["context"]
                    json.dumps(context,allow_nan=False)
                    observation=record["primary"]
                    self.assertEqual({"artifact","delta","normalizations"},set(observation["normal"]))
                    witness=observation["witness"]
                    self.assertEqual((0,"ok",1),(witness["exit_code"],witness["status"],witness["effect"]["count"]))
                    bad=copy.deepcopy(observation["artifact"])
                    if case=="wake-wait":
                        body=bad.get("evidence",bad)
                        body["followup_message"]="fabricated notification"
                    else:
                        bad["status"]="refused"
                    with self.assertRaises((ValueError,KeyError)):
                        helper.normalize(case,observation["delta"],bad,context,**observation["bounds"])
            return
        for case in sorted(CASES):
            with self.subTest(case=case),fixture(case) as (bank,home,workspace):
                context=helper.prepare(case,home,workspace,bank)
                json.dumps(context,allow_nan=False)
                normalized,before,delta,artifact,bounds=execute(self,helper,case,home,workspace,context)
                bad=copy.deepcopy(artifact)
                if case=="wake-wait":
                    body=bad.get("evidence",bad)
                    body["followup_message"]="fabricated notification"
                else:
                    bad["status"]="refused"
                with self.assertRaises((ValueError,KeyError)):
                    helper.normalize(case,delta,bad,context,**bounds)

    def test_generated_identity_and_raw_delta_tampering_are_refused(self):
        helper=load(HELPER,"bus_scale_lifecycle_negative_helper")
        with fixture("wake-pause") as (bank,home,workspace):
            context=helper.prepare("wake-pause",home,workspace,bank)
            normalized,before,delta,artifact,bounds=execute(self,helper,"wake-pause",home,workspace,context)
            path="receipts/wake-control/scale-worker.jsonl"
            changed=copy.deepcopy(delta)
            changed[path]["before_sha256"]="f"*64
            with self.assertRaises(ValueError): helper.normalize("wake-pause",changed,artifact,context,**bounds)
            changed=copy.deepcopy(artifact)
            changed["evidence"]["receipt"]["id"]="wake-control-"+"0"*32
            with self.assertRaises(ValueError): helper.normalize("wake-pause",delta,changed,context,**bounds)
            missing=copy.deepcopy(delta)
            missing.pop(path)
            with self.assertRaises(ValueError): helper.normalize("wake-pause",missing,artifact,context,**bounds)

    def test_resume_requires_actual_retained_deleted_marker_bytes(self):
        helper=load(HELPER,"bus_scale_lifecycle_resume_helper")
        with fixture("wake-resume") as (bank,home,workspace):
            context=helper.prepare("wake-resume",home,workspace,bank)
            normalized,before,delta,artifact,bounds=execute(self,helper,"wake-resume",home,workspace,context)
            self.assertEqual(1,sum(row["operation"]=="delete" for row in delta.values()))
            incomplete=dict(bounds)
            incomplete.pop("before_raw_files")
            with self.assertRaises(ValueError): helper.normalize("wake-resume",delta,artifact,context,**incomplete)

    def test_prep_clear_cannot_normalize_forged_readiness_observation_clocks(self):
        from floati.errors import IntegrityFailure
        from floati.framing import decode_frames,encode_frame
        helper=load(HELPER,"bus_scale_lifecycle_readiness_clock_helper")
        with fixture("node-prep-clear") as (bank,home,workspace):
            context=helper.prepare("node-prep-clear",home,workspace,bank)
            normalized,before,delta,artifact,bounds=execute(self,helper,"node-prep-clear",home,workspace,context)
            path=home/"events.jsonl"
            raw=path.read_bytes()
            for nested in (False,True):
                with self.subTest(nested=nested):
                    frames=decode_frames(raw)
                    readiness=next(row for row in frames if row["kind"]=="send_readiness_observation")
                    target=readiness["readiness"] if nested else readiness
                    target["observed_at"]="1970-01-01T00:00:00.000Z"
                    path.write_bytes(b"".join(encode_frame(row) for row in frames))
                    forged=raw_delta(before,helper.snapshot(home))
                    with self.assertRaises((ValueError,IntegrityFailure)):
                        helper.normalize("node-prep-clear",forged,artifact,context,**bounds)
                    path.write_bytes(raw)

    def test_daemon_foreign_copy_cannot_reuse_native_root_bound_consent(self):
        helper=load(HELPER,"bus_scale_lifecycle_daemon_coordinate_helper")
        with fixture("wake-daemon-serve") as (bank,home,workspace):
            context=helper.prepare("wake-daemon-serve",home,workspace,bank)
            copied=bank/"foreign"/"scale-fleet"
            shutil.copytree(home,copied)
            before=helper.snapshot(copied)
            with self.assertRaisesRegex(ValueError,"foreign coordinate"):
                helper.execute_seam("wake-daemon-serve",copied,workspace,context)
            self.assertEqual(before,helper.snapshot(copied))

    def test_independent_native_clones_normalize_complete_simple_lifecycle_effects(self):
        helper=load(HELPER,"bus_scale_lifecycle_equivalence_helper")
        cases=("wake-pause","wake-resume","wake-arm","seat-board","context-reading-record",
               "node-state-flush","node-boot","node-teardown","node-explain","node-prompts")
        if not full_scale_enabled():
            from tests.bus_scale_execution_paths import representative_cases
            for case in sorted(representative_cases(cases)):
                with self.subTest(case=case),self.completed_pair(case) as record:
                    results=[row["normal"] for row in record["observations"]]
                    self.assertEqual(results[0]["artifact"],results[1]["artifact"])
                    self.assertEqual(results[0]["delta"],results[1]["delta"])
            return
        for case in cases:
            with self.subTest(case=case),fixture(case) as (bank,seed,workspace):
                context=helper.prepare(case,seed,workspace,bank)
                results=[]
                for mode in ("cpu","diagnostic"):
                    home=bank/mode/"scale-fleet"
                    shutil.copytree(seed,home)
                    result,*_=execute(self,helper,case,home,workspace,context)
                    results.append(result)
                self.assertEqual(results[0]["artifact"],results[1]["artifact"])
                self.assertEqual(results[0]["delta"],results[1]["delta"])

    def test_boarding_index_requires_complete_native_proof_and_preserves_other_path_fence(self):
        from tests.fixtures.bus_scale_census.bus_scale_inbox_prefix_effects import SUMMARY
        helper=load(HELPER,"bus_scale_lifecycle_inbox_proof_helper")
        with fixture("seat-board") as (bank,home,workspace):
            context=helper.prepare("seat-board",home,workspace,bank)
            normalized,before,delta,artifact,bounds=execute(self,helper,"seat-board",home,workspace,context)
            self.assertIn(SUMMARY,normalized["delta"])
            missing=dict(bounds)
            missing.pop("inbox_prefix_input")
            with self.assertRaises(ValueError):
                helper.normalize("seat-board",delta,artifact,context,**missing)
            foreign=dict(bounds)
            foreign["inbox_prefix_input"]=dict(bounds["inbox_prefix_input"],root=str(bank/"foreign"))
            with self.assertRaises(ValueError):
                helper.normalize("seat-board",delta,artifact,context,**foreign)
            sibling=home/".inbox-prefix-foreign"
            sibling.write_bytes(b"unreviewed sibling prefix\n")
            with self.assertRaises(ValueError):
                helper.normalize("seat-board",raw_delta(before,helper.snapshot(home)),artifact,context,**bounds)
            sibling.unlink()
            bucket=next((home/".inbox-prefix/buckets").iterdir(),None)
            if bucket is None:
                # A quiet event source has no positive buckets. An invented
                # bucket remains outside the complete empty native map.
                bucket=home/".inbox-prefix/buckets/0000"
            bucket.write_bytes(b"forged membership page\n")
            bucket.chmod(0o600)
            with self.assertRaises(ValueError):
                helper.normalize("seat-board",raw_delta(before,helper.snapshot(home)),artifact,context,**bounds)

    def test_helper_has_no_tests_import_or_global_home_repin(self):
        source=HELPER.read_text(encoding="utf-8")
        self.assertNotIn("from tests",source)
        self.assertNotIn("import tests",source)
        self.assertNotIn('os.environ["HOME"]',source)
        with tempfile.TemporaryDirectory(prefix="bus-scale-native-import-home-",dir=REAL_TEMP_ROOT) as temporary:
            environment={key:value for key,value in os.environ.items()
                         if key not in {"PYTHONHOME","PYTHONPATH"}}
            environment.update(HOME=str(Path(temporary).resolve()),PYTHONDONTWRITEBYTECODE="1")
            completed=subprocess.run(["/usr/bin/python3","-B",str(HELPER.with_name("verify-native-imports.py"))],
                cwd=ROOT,env=environment,capture_output=True,text=True,timeout=30)
            self.assertEqual(0,completed.returncode,completed.stdout+completed.stderr)
            self.assertEqual({"home_preserved":True,"tests_imported":False,"adapters":4,
                              "proof_modules":8,"foreign_namespace_refused":True},
                             json.loads(completed.stdout))

    def test_waiter_invocation_cannot_hide_foreign_identity_or_unbound_terminal_state(self):
        helper=load(HELPER,"bus_scale_lifecycle_invocation_negative_helper")
        with fixture("wake-wait") as (bank,home,workspace):
            context=helper.prepare("wake-wait",home,workspace,bank)
            _,before,delta,artifact,bounds=execute(self,helper,"wake-wait",home,workspace,context)
            relative="state/waiter-invocations/"+helper.ACTOR+"/"+hashlib.sha256(helper.SESSION.encode()).hexdigest()+".json"
            path=home/relative
            original=path.read_bytes()
            value=json.loads(original)
            self.assertIsNone(value["invocation_id"],"immediate native delivery returns before an empty-wait hold")
            from tests.fixtures.bus_scale_census.bus_scale_waiter_invocation_effects import verify_waiter_invocation
            with self.assertRaisesRegex(ValueError,"exact new start/exit pair"):
                verify_waiter_invocation(FloatiRoot.open_direct_home(home),helper.ACTOR,helper.SESSION,
                    workspace,before,helper.snapshot(home),child_pid=bounds["child_pid"],
                    wall_begin=bounds["wall_begin"],wall_end=bounds["wall_end"])
            for field,replacement in (("node_id","foreign"),("session_id","foreign-session"),
                    ("workspace",str(bank)),("pid",os.getpid()+100000),("phase","running"),
                    ("invocation_id","stop-hold-"+"0"*32),("token","0"*32),
                    ("process_start_epoch",value["parent_process_start_epoch"]-1)):
                with self.subTest(field=field):
                    forged=dict(value,**{field:replacement})
                    path.write_bytes((json.dumps(forged,sort_keys=True,separators=(",",":"))+"\n").encode())
                    with self.assertRaises(ValueError):
                        helper.normalize("wake-wait",raw_delta(before,helper.snapshot(home)),artifact,context,**bounds)
                    path.write_bytes(original)
            foreign=path.with_name("f"*64+".json")
            foreign.write_bytes(original)
            try:
                with self.assertRaises(ValueError):
                    helper.normalize("wake-wait",raw_delta(before,helper.snapshot(home)),artifact,context,**bounds)
            finally:
                foreign.unlink()

    def test_shared_lifecycle_prompt_files_require_their_exact_native_contents(self):
        helper=load(HELPER,"bus_scale_lifecycle_shared_prompt_negative_helper")
        with fixture("node-prompts") as (bank,home,workspace):
            context=helper.prepare("node-prompts",home,workspace,bank)
            _,before,delta,artifact,bounds=execute(self,helper,"node-prompts",home,workspace,context)
            names={row["path"] for row in artifact["evidence"]["files"]}
            self.assertTrue({"boot-seat.md","teardown-seat.md"}<=names)
            path=home.parent/"lifecycle-prompts/boot-seat.md"
            path.write_text("forged lifecycle command\n")
            forged=copy.deepcopy(artifact)
            row=next(row for row in forged["evidence"]["files"] if row["path"]=="boot-seat.md")
            row["sha256"]=hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaises(ValueError):
                helper.normalize("node-prompts",delta,forged,context,**bounds)


if __name__ == "__main__":
    unittest.main()

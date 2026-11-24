"""Expanded observer fixtures shared by default product-cache tests and the census suite."""
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


ROOT=Path(__file__).resolve().parents[1]
ADAPTER=ROOT/"tests/fixtures/bus_scale_census/bus-scale-expanded-observation-2026-10-15.py"
ENTRY=ROOT/"tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"


def load(path,name):
    if not path.is_file(): return None
    result=load_fixture_module(path,name)
    from tests.bus_scale_bounded_transport import bounded_module
    return bounded_module(result)


class RecordingOwner:
    def __init__(self,delegate,expected_seed,*,arm_bank=None,scale=None,expected_context=None):
        self.delegate=delegate
        self.expected_seed=expected_seed
        self.launches=[]
        self.admitted_inputs=[]
        self.collections=[]
        self.arm_bank=arm_bank
        self.scale=scale
        self.expected_context=expected_context
        self.materialized_seed=None
        self.clock_receipt=None

    @staticmethod
    def cache_projection(expected, actual, names):
        """Compose only named cache classes whose complete proofs also run."""
        def selected(path):
            return any(path==name or path.startswith(name+"/") for name in names)
        result={path:raw for path,raw in expected.items() if not selected(path)}
        result.update({path:raw for path,raw in actual.items() if selected(path)})
        return result

    @staticmethod
    def registry_changed(expected, actual):
        return any((path==".registry-prefix" or path.startswith(".registry-prefix/"))
                   and expected.get(path)!=actual.get(path) for path in set(expected)|set(actual))

    def verify_send_clone(self, home, expected):
        from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import verify_warmed_clone
        from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import verify_warmed_registry_clone
        actual=inventory(home)
        send_expected=expected
        if self.registry_changed(expected,actual):
            registry_expected=self.cache_projection(expected,actual,{".send-prefix"})
            # The send proof below independently admits this sole native new
            # empty lock, including its mode, owner and retained mode witness.
            if "events.jsonl.lock" not in expected and actual.get("events.jsonl.lock")==b"":
                registry_expected["events.jsonl.lock"]=b""
            verify_warmed_registry_clone(home,registry_expected)
            send_expected=self.cache_projection(expected,actual,{".registry-prefix"})
        verify_warmed_clone(home,send_expected)
        if inventory(home)!=actual:
            raise AssertionError("composed send/registry warm proof changed retained input")

    def verify_cursor_clone(self, home, expected, *, node="architect"):
        """Prove a complete native rebuild while retaining frozen noncache bytes."""
        from tests.fixtures.bus_scale_census import bus_scale_cursor_prefix_effects as proof
        from floati.root import FloatiRoot
        root = FloatiRoot.open_direct_home(home)
        actual = inventory(home)
        captured = proof.capture_cursor_prefix_input(home, actual, node=node)
        ack, delivery = captured["ack_relative"], captured["delivery_relative"]
        names = {".send-prefix", proof.cache_name(ack), proof.cache_name(delivery)}
        registry_expected=self.cache_projection(expected,actual,names)
        added_locks={}
        native_lock = home / "registry/entries.jsonl.lock"
        for path in ("events.jsonl.lock", ack + ".lock", delivery + ".lock",
                     "receipts/wake-coordination/" + node + "/lane.lock"):
            if path not in expected and actual.get(path)==b"":
                info=(home/path).lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_nlink!=1
                        or stat.S_IMODE(info.st_mode)!=stat.S_IMODE(native_lock.lstat().st_mode)):
                    raise AssertionError("cursor warm clone created a foreign, shared or nonnative lock")
                added_locks[path]=b""
        registry_expected.update(added_locks)
        if self.registry_changed(expected,actual):
            from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import verify_warmed_registry_clone
            verify_warmed_registry_clone(home,registry_expected)
            names.add(".registry-prefix")

        def is_cache(path):
            return any(path == name or path.startswith(name + "/") for name in names)

        old_plain = {path: raw for path, raw in expected.items() if not is_cache(path)}
        new_plain = {path: raw for path, raw in actual.items() if not is_cache(path)}
        old_plain.update(added_locks)
        if old_plain != new_plain:
            raise AssertionError("cursor warm clone changed frozen authoritative or unrelated bytes")
        for name in names:
            old_key = expected.get(name + "/seal.key")
            if old_key is not None and actual.get(name + "/seal.key") != old_key:
                raise AssertionError("cursor warm clone changed a retained sealing key")
        # Authenticate every historical ACK entry and its complete native
        # folds, in addition to the event and delivery join above.
        proof.verify_complete_cursor_banks(home, actual, node=node)
        if inventory(home) != actual:
            raise AssertionError("cursor warm clone changed during complete read-only proof")

    def verify_inbox_clone(self, home, expected):
        """Project this sole artifact only after its complete native map proof."""
        from tests.fixtures.bus_scale_census import bus_scale_inbox_prefix_effects as proof
        actual=inventory(home)
        captured=proof.capture_inbox_prefix_input(home,actual)
        proof.verify_inbox_prefix_effect(home,actual,actual,{},captured)
        old_key=expected.get(proof.CACHE+"/seal.key")
        if old_key is not None and actual.get(proof.CACHE+"/seal.key")!=old_key:
            raise AssertionError("inbox warm clone changed its retained sealing key")
        projected=self.cache_projection(expected,actual,{proof.CACHE})
        # With no event source yet, native warming creates its one empty
        # canonical ledger lock. Prove its exact coordinate and physical
        # metadata; this grants no other noncache change.
        lock="events.jsonl.lock"
        if lock not in expected and actual.get(lock)==b"":
            info=(home/lock).lstat()
            native=(home/"registry/entries.jsonl.lock").lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid()
                    or info.st_nlink!=1 or info.st_dev!=home.stat().st_dev
                    or stat.S_IMODE(info.st_mode)!=stat.S_IMODE(native.st_mode)):
                raise AssertionError("inbox warm clone created a nonnative lock")
            projected[lock]=b""
        if inventory(home)!=actual:
            raise AssertionError("complete inbox warm proof changed its input")
        return projected

    def attention_seed(self,argv):
        """Independently bind reviewed clock leaves to the original bytes."""
        adapter=load(ADAPTER,"recording_owner_clock_proof")
        bank=adapter.ordinary(self.arm_bank,directory=True)
        path=adapter.ordinary(bank/"seed-clock.json",directory=False)
        raw=path.read_bytes()
        receipt=json.loads(raw)
        if raw!=(adapter.canonical(receipt)+"\n").encode():
            raise AssertionError("arm clock receipt is not canonical")
        if (set(receipt)!={"version","case","real_anchor","source_canonical_time","canonical_time",
                          "offset_ms","original_primary","original_prefix_exact","scales"}
                or type(receipt["version"]) is not int or receipt["version"]!=1
                or receipt["case"]!=argv[4] or receipt["case"] not in {"supervise","watch"}
                or type(receipt["offset_ms"]) is not int or receipt["original_prefix_exact"] is not True
                or set(receipt["scales"])!={"1","10"} or self.scale not in {"1","10"}):
            raise AssertionError("arm clock receipt has an unreviewed shape or coordinate")
        proof=receipt["scales"][self.scale]
        template=adapter.ordinary(proof["template"],directory=True)
        source=adapter.ordinary(proof["materialized"],directory=True)
        home=adapter.ordinary(argv[5],directory=True)
        if (any(stat.S_IMODE(path.stat().st_mode)&0o077 for path in (bank,template,source,home))
                or source.parent.name!=self.scale or template.parent.name!=self.scale):
            raise AssertionError("clock input must occupy the known private scale coordinate")
        if (source==home or source in home.parents or home in source.parents
                or bank==source or bank in source.parents or source in bank.parents):
            raise AssertionError("materialized input overlaps measured root or observation bank")
        origin=datetime.fromisoformat(receipt["source_canonical_time"].replace("Z","+00:00"))
        target=datetime.fromisoformat(receipt["canonical_time"].replace("Z","+00:00"))
        anchor=datetime.fromisoformat(receipt["real_anchor"].replace("Z","+00:00"))
        if target-origin!=timedelta(milliseconds=receipt["offset_ms"]) or not timedelta(0)<=anchor-target<timedelta(milliseconds=1):
            raise AssertionError("clock origin and real arm anchor do not bind the uniform recipe")
        if inventory(template)!=self.expected_seed:
            raise AssertionError("clock receipt lost the exact original native template")
        expected_inventory={relative:{"bytes":len(data),"sha256":hashlib.sha256(data).hexdigest(),
                                      "mode":stat.S_IMODE((template/relative).stat().st_mode)}
                            for relative,data in self.expected_seed.items()}
        if proof["template_inventory"]!=expected_inventory or proof["inverse_inventory"]!=expected_inventory:
            raise AssertionError("clock template/inverse proof does not bind the original bytes and modes")
        actual=inventory(source)
        if adapter.file_inventory(source)!=proof["materialized_inventory"]:
            raise AssertionError("declared detached input differs from its receipt")
        from floati.framing import decode_frames,encode_frame
        original=decode_frames(self.expected_seed["events.jsonl"])
        rows=decode_frames(actual["events.jsonl"])
        if len(rows)!=len(original):
            raise AssertionError("clock materialization changed native event population")
        expected_leaves=[]
        for ordinal,(row,old) in enumerate(zip(rows,original)):
            if row["id"]!=old["id"] or row["kind"]!=old["kind"]:
                raise AssertionError("clock materialization changed native identity")
            pointers=["/timestamp"] if row["kind"]=="message_envelope" else [
                "/timestamp","/observed_at","/readiness/observed_at"]
            for pointer in pointers:
                parts=pointer.strip("/").split("/")
                before,after=old,row
                for part in parts:
                    before,after=before[part],after[part]
                if (datetime.fromisoformat(after.replace("Z","+00:00"))
                        -datetime.fromisoformat(before.replace("Z","+00:00"))
                        !=timedelta(milliseconds=receipt["offset_ms"])):
                    raise AssertionError("native leaf differs from exact uniform clock recipe")
                expected_leaves.append({"path":"events.jsonl","ordinal":ordinal,"kind":row["kind"],
                    "record_id":row["id"],"pointer":pointer,"before":before,"after":after})
        if proof["changed_leaves"]!=expected_leaves:
            raise AssertionError("clock receipt omitted or expanded reviewed native leaves")
        inverse=copy.deepcopy(rows)
        for leaf in expected_leaves:
            target=inverse[leaf["ordinal"]]
            parts=leaf["pointer"].strip("/").split("/")
            for part in parts[:-1]: target=target[part]
            target[parts[-1]]=leaf["before"]
        inverse_raw=b"".join(encode_frame(row) for row in inverse)
        if dict(actual,**{"events.jsonl":inverse_raw})!=self.expected_seed:
            raise AssertionError("native input does not inverse exactly to the original seed")
        context_path=home.parent/"case-context.json"
        context=json.loads(adapter.ordinary(context_path,directory=False).read_bytes())
        if context["seed_home"]!=str(home) or context["canonical_time"]!=receipt["canonical_time"]:
            raise AssertionError("native child context is not bound to materialized clock/root")
        if self.expected_context is not None:
            if (receipt["source_canonical_time"]!=self.expected_context["canonical_time"]
                    or {key:value for key,value in context.items() if key!="canonical_time"}
                    !={key:value for key,value in self.expected_context.items() if key!="canonical_time"}):
                raise AssertionError("native child context changed an unreviewed field")
        admitted=inventory(home)
        admitted_inventory=adapter.file_inventory(home)
        expected_admitted_inventory=copy.deepcopy(proof["materialized_inventory"])
        if admitted!=actual:
            from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import verify_warmed_registry_clone
            registry_proof=verify_warmed_registry_clone(home,actual)
            # Every projected metadata entry belongs to the exact changed
            # artifact already proved complete; all other entries stay exact.
            for relative in registry_proof["changed_files"]:
                if relative in admitted_inventory:
                    expected_admitted_inventory[relative]=admitted_inventory[relative]
                else:
                    expected_admitted_inventory.pop(relative,None)
        if admitted_inventory!=expected_admitted_inventory:
            raise AssertionError("prepared root is not the exact materialized input plus its proved registry warm rebuild")
        if inventory(home)!=admitted or adapter.file_inventory(home)!=admitted_inventory:
            raise AssertionError("attention warm admission changed retained input")
        if self.clock_receipt is not None and self.clock_receipt!=raw:
            raise AssertionError("CPU/diagnostic clock recipe changed")
        self.clock_receipt=raw
        self.materialized_seed=actual
        return admitted

    def launch_owned_child(self,argv,**kwargs):
        home=Path(argv[5])
        expected=self.attention_seed(argv) if self.arm_bank is not None else self.expected_seed
        admitted=inventory(home)
        if admitted!=expected:
            if self.arm_bank is not None:
                raise AssertionError("owned child launched without exact restored seed bytes")
            if (argv[4] in {"consume","work-complete","inbox","node-drain","seat-board","wake-wait"}
                    or (argv[4] in {"node-prep-clear","seat-teardown"}
                        and any(path.startswith(".inbox-prefix/") for path in admitted))):
                expected=self.verify_inbox_clone(home,expected)
            if admitted!=expected:
                if argv[4] in {"ack", "consume", "work-complete", "inbox", "node-drain", "wake-wait"}:
                    self.verify_cursor_clone(home, expected, node="scale-worker" if argv[4] == "wake-wait" else "architect")
                elif argv[4] in {"node-prep-clear", "seat-teardown"}:
                    self.verify_send_clone(home,expected)
                else:
                    from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import verify_warmed_registry_clone
                    verify_warmed_registry_clone(home,expected)
            if inventory(home)!=admitted:
                raise AssertionError("warm input changed during its read-only admission proof")
        child=self.delegate.launch_owned_child(argv,**kwargs)
        self.launches.append((list(argv),child.pid,home))
        self.admitted_inputs.append(admitted)
        return child

    def collect_owned_child(self,child,**kwargs):
        if kwargs!={"deadline_seconds":120,"collection_seconds":10}:
            raise AssertionError("expanded child uses the closed ownership/collection budgets")
        result=self.delegate.collect_owned_child(child,**kwargs)
        self.collections.append(copy.deepcopy(result))
        return result


class UnknownCollectionOwner(RecordingOwner):
    def collect_owned_child(self,child,**kwargs):
        # Collect the actual native child first: this negative control must
        # never leak a live process merely to test a pessimistic receipt.
        real=super().collect_owned_child(child,**kwargs)
        if real.get("child_collected") is not True:
            raise AssertionError("negative control's actual child was not collected")
        return dict(status="uncollected",exit_code=None,child_collected=False,interrupted=True)


class ForbiddenLaunchOwner:
    """Boundary negatives never launch a native child or reach archiving."""
    def __init__(self): self.launches=[]

    def launch_owned_child(self,argv,**kwargs):
        self.launches.append(list(argv))
        raise AssertionError("unsafe observation bank reached child launch")


class AuxiliaryMutationOwner(RecordingOwner):
    def __init__(self,delegate,expected_seed,workspace):
        super().__init__(delegate,expected_seed)
        self.workspace=workspace

    def collect_owned_child(self,child,**kwargs):
        result=super().collect_owned_child(child,**kwargs)
        if len(self.collections)==1 and result.get("child_collected") is True:
            (self.workspace/"unrelated-native-read-mutation.txt").write_text("unreviewed auxiliary mutation\n",encoding="utf-8")
        return result


class LaneIndexCorruptionOwner(RecordingOwner):
    def __init__(self,delegate,expected_seed,workspace):
        super().__init__(delegate,expected_seed)
        self.workspace=workspace

    def collect_owned_child(self,child,**kwargs):
        result=super().collect_owned_child(child,**kwargs)
        if len(self.collections)==1 and result.get("child_collected") is True:
            index=self.workspace/".git/worktrees/census-lifecycle/index"
            raw=index.read_bytes()
            index.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        return result


class VerificationAuxiliaryCorruptionOwner(RecordingOwner):
    def __init__(self,delegate,expected_seed,workspace,mutation):
        super().__init__(delegate,expected_seed)
        self.workspace,self.mutation=workspace,mutation

    def collect_owned_child(self,child,**kwargs):
        result=super().collect_owned_child(child,**kwargs)
        if len(self.collections)==1 and result.get("child_collected") is True:
            if self.mutation=="payload":
                path=self.workspace/".git/description"
                path.write_bytes(path.read_bytes()+b"unrelated private mutation\n")
            elif self.mutation=="population":
                (self.workspace/".git/foreign-empty-directory").mkdir()
            elif self.mutation=="clock":
                os.utime(self.workspace/".git",ns=(0,0))
            else:
                raise AssertionError("unknown closed verification mutation")
        return result

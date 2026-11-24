"""Owned expanded-case adapter; original nine-case observations stay separate.

Am8 restores one native consent coordinate outside child measurement. Every
completed mode is validated and archived before any subsequent restore. This
module derives observations, never a performance or budget verdict.
"""
from __future__ import annotations

import importlib.util as _fixture_proof_util
from pathlib import Path as _FixtureProofPath

_fixture_proof_specification = _fixture_proof_util.spec_from_file_location(
    "_bus_scale_fixture_proof_bootstrap", _FixtureProofPath(__file__).with_name("_proof_namespace.py"))
_fixture_proof_bootstrap = _fixture_proof_util.module_from_spec(_fixture_proof_specification)
_fixture_proof_specification.loader.exec_module(_fixture_proof_bootstrap)
_fixture_proof_bootstrap.bind()


import base64
import configparser
import copy
from datetime import datetime,timedelta,timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import resource
import re
import shutil
import stat
import struct
import subprocess
import sys
import time

from tests.bus_scale_attention import (ATTENTION_CASES, AttentionCategoryMismatch,
                                       attention_categories, validate_attention_proof)
from tests.bus_scale_arm_clock import materialize_attention_pair, publish_attention_context
from tests.bus_scale_filesystem_clock import capture_filesystem_clock, filesystem_bounds


ROOT=Path(__file__).resolve().parents[3]
ENTRY=ROOT/"tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"
OLD_CASES=frozenset({"send","status","doctor","wait-start","wait-idle","log-history",
                     "receipts-history","readiness-read","receipt-append"})
TEMP=Path("\x2fprivate/tmp" if sys.platform=="darwin" else "\x2ftmp")


class UnresolvedObservation(RuntimeError):
    code="bus_scale_expanded_observation_unresolved"


class UncollectedChild(UnresolvedObservation):
    uncollected_child=True


def require(value,detail):
    if not value: raise UnresolvedObservation(detail)


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(",",":"))


def digest(raw): return hashlib.sha256(raw).hexdigest()


def ordinary(path,*,exists=True,directory=None):
    path=Path(path)
    require(path.is_absolute() and TEMP in path.parents and path.resolve(strict=exists)==path,
            "observation coordinate must be canonical below the native temporary parent")
    for part in (path,*path.parents):
        if part==TEMP: break
        if part.exists() or part.is_symlink():
            info=part.lstat()
            require(not stat.S_ISLNK(info.st_mode) and info.st_uid==os.getuid(),
                    "observation coordinate contains an alias or foreign owner")
    if exists:
        info=path.lstat()
        require(stat.S_ISDIR(info.st_mode) if directory is True else
                stat.S_ISREG(info.st_mode) if directory is False else
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode),"observation coordinate is not ordinary")
    return path


def tree(path):
    """Exact bytes, modes and mtimes; atime/inodes are not clone semantics."""
    path=ordinary(path,exists=False)
    if not path.exists(): return {"kind":"absent","entries":{}}
    result={}
    paths=[path]+(sorted(path.rglob("*")) if path.is_dir() else [])
    for item in paths:
        info=item.lstat()
        require(item.resolve(strict=True)==item and info.st_uid==os.getuid()
                and (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)),
                "snapshot contains an alias, special file or foreign owner")
        row={"kind":"directory" if stat.S_ISDIR(info.st_mode) else "file",
             "mode":stat.S_IMODE(info.st_mode),"mtime_ns":info.st_mtime_ns}
        if row["kind"]=="file":
            require(info.st_nlink==1,"snapshot contains a shared hardlink")
            raw=item.read_bytes()
            row.update(bytes=len(raw),sha256=digest(raw))
        result["." if item==path else item.relative_to(path).as_posix()]=row
    return {"kind":"directory" if path.is_dir() else "file","entries":result}


def raw_files(home):
    result={}
    for relative,row in tree(home)["entries"].items():
        if row["kind"]=="file": result[relative]=(Path(home)/relative).read_bytes()
    return result


def file_inventory(home):
    return {path:{"sha256":row["sha256"],"bytes":row["bytes"],"mode":row["mode"]}
            for path,row in tree(home)["entries"].items() if row["kind"]=="file"}


def write_json(path,value):
    raw=(canonical(value)+"\n").encode()
    with Path(path).open("xb") as stream: stream.write(raw)
    require(Path(path).read_bytes()==raw,"observation receipt readback differs")
    return digest(raw)


def copy_exact(source,destination,expected):
    source,destination=Path(source),Path(destination)
    require(not destination.exists() and not destination.is_symlink(),"snapshot destination already exists")
    if expected["kind"]=="absent": return
    if expected["kind"]=="directory": shutil.copytree(source,destination,copy_function=shutil.copy2)
    else: shutil.copy2(source,destination)
    require(tree(destination)==expected,"snapshot copy differs in bytes/mode/mtime")


def restore_exact(source,destination,expected):
    destination=ordinary(destination,exists=False)
    # Inspect every existing leaf before removal; never follow a replacement
    # alias or remove shared/foreign filesystem objects on a receipt alone.
    current=tree(destination)
    if current["kind"]=="directory": shutil.rmtree(destination)
    elif current["kind"]=="file": destination.unlink()
    copy_exact(source,destination,expected)
    require(tree(destination)==expected,"restored bytes/mode/mtime differ from the retained snapshot")


def load_entry(entry):
    entry=Path(entry)
    require(entry==ENTRY and entry.resolve(strict=True)==entry and entry.is_file(),
            "expanded observation requires the fixed repository native entry")
    spec=importlib.util.spec_from_file_location("expanded_observation_native_entry",entry)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def auxiliary_roots(case,pair,workspace):
    home,bank=Path(pair["prepared_home"]),Path(pair["preparation_bank"])
    result=[bank,Path(workspace)]
    if case=="seat-teardown":
        result=[bank,Path(workspace).parent,Path(pair["context"].get("origin",Path(workspace).parent.parent/"origin.git"))]
    if case=="node-prompts": result.append(home.parent/"lifecycle-prompts")
    if case in {"node-boot","node-teardown","node-explain"}: result.append(home.parent/"lifecycle-declared-roots.json")
    if case=="confluence-bundle": result.append(home.parent/"routine-bundle.json")
    require(len(set(result))==len(result),"auxiliary coordinates overlap")
    for selected in result:
        require(selected!=home and home not in selected.parents and selected not in home.parents,
                "auxiliary root overlaps the measured bus root")
        for other in result:
            require(selected==other or (other not in selected.parents and selected not in other.parents),
                    "auxiliary roots must be disjoint")
    return result


def disjoint_bank(case,pair,workspace,place):
    roots=auxiliary_roots(case,pair,workspace)
    roots.extend(Path(pair[key]) for key in ("prepared_home","1","10"))
    key=digest(canonical({"case":case,"context":pair["context"],"roots":list(map(str,auxiliary_roots(case,pair,workspace)))}).encode())
    roots.append(Path(pair["preparation_bank"]).parent/("expanded-auxiliary-baseline-"+key[:16]))
    require(all(place!=root and root not in place.parents and place not in root.parents for root in roots),
            "observation bank must be disjoint from every seed, restore root, auxiliary and baseline")


def baseline(case,pair,workspace):
    context=pair["context"]
    roots=auxiliary_roots(case,pair,workspace)
    scope=ordinary(Path(os.path.commonpath([str(pair["prepared_home"]),str(workspace),
                                          str(pair["preparation_bank"])])),directory=True)
    require(stat.S_IMODE(scope.stat().st_mode)&0o077==0,"auxiliary common scope must be private")
    for path in roots:
        ordinary(path,exists=False)
        require(scope in path.parents,"auxiliary root is outside the closed owned scope")
    key=digest(canonical({"case":case,"context":context,"roots":list(map(str,roots))}).encode())
    place=Path(pair["preparation_bank"]).parent/("expanded-auxiliary-baseline-"+key[:16])
    manifest_path=place/"manifest.json"
    if place.exists():
        ordinary(place,directory=True)
        manifest=json.loads(manifest_path.read_bytes())
        require(set(manifest)=={"case","context_sha256","scope","roots","prepared_original"}
                and manifest["case"]==case and manifest["context_sha256"]==key
                and manifest["scope"]==str(scope)
                and [row["root"] for row in manifest["roots"]]==list(map(str,roots)),
                "retained auxiliary baseline has a foreign identity")
        for row in manifest["roots"]:
            require(tree(place/row["snapshot"])==row["inventory"],"retained auxiliary snapshot is corrupt")
        require(tree(place/"prepared-original")==manifest["prepared_original"],"retained native preparation root is corrupt")
        return place,manifest
    place.mkdir(mode=0o700)
    manifest={"case":case,"context_sha256":key,"scope":str(scope),"roots":[],
              "prepared_original":tree(pair["prepared_home"])}
    copy_exact(pair["prepared_home"],place/"prepared-original",manifest["prepared_original"])
    for index,path in enumerate(roots):
        before=tree(path)
        snapshot="auxiliary-"+str(index)
        copy_exact(path,place/snapshot,before)
        require(tree(path)==before,"source changed during auxiliary snapshot")
        manifest["roots"].append({"root":str(path),"snapshot":snapshot,"inventory":before})
    write_json(manifest_path,manifest)
    return place,manifest


def moment_ns(value,begin,end,*,path="<unspecified>",filesystem_clock=None):
    try:
        lower,upper=filesystem_bounds(begin,end,filesystem_clock)
    except ValueError as exc:
        raise UnresolvedObservation("native filesystem clock evidence is invalid: "+str(exc)) from exc
    require(type(value) is int and lower<=value<=upper,
            "native auxiliary mutation clock is outside the collected invocation: "+canonical({
                "path":str(path),"mtime_ns":value,"begin":begin.isoformat(),"end":end.isoformat(),
                "filesystem_clock":filesystem_clock,"lower_ns":lower,"upper_ns":upper}))


def exact_survivors(before,after,*,removed=(),written=(),directory_clocks=(),begin,end,filesystem_clock=None):
    """Only proved native leaves/subtrees may differ; all else stays exact."""
    old,new=before["entries"],after["entries"]
    removed=set(removed)
    written=set(written)
    expected=(set(old)-removed)|written
    require(set(new)==expected,"native auxiliary entry population contains an unexplained mutation: "+canonical({
        "missing":sorted(expected-set(new)),"extra":sorted(set(new)-expected)}))
    projected=copy.deepcopy(after)
    for path,row in new.items():
        if path in written:
            moment_ns(row["mtime_ns"],begin,end,path=path,filesystem_clock=filesystem_clock)
            projected["entries"][path]["mtime_ns"]="<native-publication-time>"
        elif path in directory_clocks:
            require(row["kind"]==old[path]["kind"]=="directory" and row["mode"]==old[path]["mode"],
                    "native auxiliary changed a surviving directory mode/type")
            if row["mtime_ns"]!=old[path]["mtime_ns"]:
                moment_ns(row["mtime_ns"],begin,end,path=path,filesystem_clock=filesystem_clock)
            projected["entries"][path]["mtime_ns"]="<native-directory-change-time>"
        else:
            require(row==old[path],"native auxiliary changed an unrelated retained entry: "+path)
    return projected


def native_publication(case,root,path,inventory,artifact,context,begin,end,*,normalized=None,filesystem_clock=None):
    """Complete native payload proof precedes explicit creation-time projection."""
    require(inventory["kind"] in {"file","directory"},"native publication is missing")
    entries=inventory["entries"]
    if case=="node-prompts":
        expected={row["path"]:row["sha256"] for row in artifact["evidence"]["files"]}
        require(set(entries)=={".",*expected} and entries["."]["kind"]=="directory",
                "native prompt publication contains foreign entries")
        for name,sha256 in expected.items():
            require(entries[name]["kind"]=="file" and entries[name]["sha256"]==sha256
                    and entries[name]["mode"]&0o700==0o600 and not entries[name]["mode"]&0o111,
                    "native prompt publication differs from its exact witnessed content/mode")
    elif case in {"node-boot","node-teardown","node-explain"}:
        expected={"schema_version":0,"roots":[{"bus_id":"census","root":str(root.tenant_home),
                   "architect_node":"architect","downstream":[]}]}
        require(entries["."]["kind"]=="file" and entries["."]["mode"]==0o600
                and path.read_bytes()==(canonical(expected)+"\n").encode(),
                "native declared-root publication differs from the exact prepared shape")
    elif case=="confluence-bundle":
        # Retain every published byte in the archive. Comparison additionally
        # projects only proved publication clocks and measured ages.
        require(entries["."]["kind"]=="file" and entries["."]["mode"]&0o700==0o600
                and not entries["."]["mode"]&0o111,"native source bundle has a foreign file type/mode")
        helper=load_entry(ENTRY).routine_module(case)
        document=helper._bundle(root,context,wall_begin=begin,wall_end=end)
        raw=(json.dumps(document,indent=2)+"\n").encode("utf-8")
        require(path.read_bytes()==raw and entries["."]["bytes"]==len(raw)
                and entries["."]["sha256"]==digest(raw),
                "native source bundle bytes differ from its complete exact publication")
        projected_document=copy.deepcopy(document)
        clock_audits=[]
        for parts, _expected, replacement in helper.bundle_clock_leaves(document):
            target=projected_document
            for part in parts[:-1]:
                target=target[part]
            target[parts[-1]]=replacement
            clock_audits.append({"pointer":"/delta/@publication~1routine-bundle.json/value/"
                +"/".join(map(str,parts)),
                "reason":"complete native routine effect, exact source binding and invocation bounds validated"})
        require(type(normalized) is dict and type(normalized.get("normalizations")) is list
                and all(type(audit) is dict and set(audit)=={"pointer","reason"}
                        and type(audit["pointer"]) is str for audit in normalized["normalizations"]),
                "native bundle publication lacks its complete scalar age audits")
        publication=normalized.get("delta",{}).get("@publication/routine-bundle.json")
        require(publication=={"operation":"publish","value":projected_document}
                and [audit for audit in normalized["normalizations"]
                     if audit["pointer"].startswith("/delta/@publication~1routine-bundle.json/")]==clock_audits,
                "native bundle publication projection changes an unproved leaf or omits a clock audit")
    else:
        raise UnresolvedObservation("unreviewed native sibling publication")
    projected=copy.deepcopy(inventory)
    for relative,row in projected["entries"].items():
        moment_ns(row["mtime_ns"],begin,end,path=path/relative,filesystem_clock=filesystem_clock)
        row["mtime_ns"]="<native-publication-time>"
    if case=="confluence-bundle":
        comparison=(json.dumps(projected_document,indent=2)+"\n").encode("utf-8")
        projected["entries"]["."].update(bytes=len(comparison),sha256=digest(comparison))
        projected["content_projection"]={"kind":"complete_native_bundle_clock_projection",
                                         "normalizations":clock_audits}
    return projected


def lane_close_auxiliary(root,workspace,context,artifact,manifest,states,begin,end,*,filesystem_clock=None):
    opened=context["opened"]
    original=Path(opened["workspace"])
    opening=opened["record"]
    record=artifact["evidence"]["record"]
    expected=dict(opening,id=record["id"],timestamp=record["timestamp"],state="closed",
                  closed_at=record["timestamp"],closed_by="scale-worker",opened_record_id=opening["id"],why=None)
    require(record==expected and artifact["evidence"]["removed"]==str(original) and not original.exists(),
            "native close did not remove exactly its retained opening")
    prep=Path(manifest["roots"][0]["root"])
    before_prep=manifest["roots"][0]["inventory"]
    prefix=original.relative_to(prep).as_posix()
    baseline=Path(context["_auxiliary_baseline"])
    git_file=(baseline/manifest["roots"][0]["snapshot"]/prefix/".git").read_text()
    require(git_file.startswith("gitdir: ") and git_file.endswith("\n"),"native opening lacks its exact Git admin pointer")
    admin=Path(git_file[len("gitdir: "):-1])
    admin_prefix=admin.relative_to(workspace).as_posix()
    require(admin.parent==workspace/".git/worktrees", "native opening points outside its private Git admin plane")
    before_workspace=manifest["roots"][1]["inventory"]
    source_admin=baseline/manifest["roots"][1]["snapshot"]/admin_prefix
    from floati.lane_workspaces import LaneWorkspaces
    require((source_admin/"floati-lane-owner.json").read_bytes()==LaneWorkspaces._marker_bytes(opening["id"])
            and (source_admin/"gitdir").read_text()==str(original/".git")+"\n"
            and (source_admin/"HEAD").read_text()=="ref: refs/heads/"+opening["branch"]+"\n",
            "native lane opening's retained admin identity differs")
    proofs=[]
    for before,after,target in ((before_prep,states[0],prefix),(before_workspace,states[1],admin_prefix)):
        removed={p for p in before["entries"] if p==target or p.startswith(target+"/")}
        require(removed and all(p not in after["entries"] for p in removed),"native close left part of its exact recorded subtree")
        ancestors={p.as_posix() for p in Path(target).parents if p.as_posix() in before["entries"]}
        # Git may prune the now-empty worktrees directory; permit only an
        # empty ancestor whose entire former descendant population is removed.
        if target==admin_prefix:
            for candidate in sorted(ancestors,key=lambda p:len(Path(p).parts),reverse=True):
                if candidate!="." and candidate not in after["entries"]:
                    require(all(p in removed for p in before["entries"] if p.startswith(candidate+"/")),
                            "native close removed an unselected Git administration subtree")
                    removed.add(candidate)
        projected=exact_survivors(before,after,removed=removed,directory_clocks=ancestors,begin=begin,end=end,
                                  filesystem_clock=filesystem_clock)
        proofs.append({"removed":sorted(removed),"after":projected})
    return {"kind":"exact_native_lane_close","opening":opening,"auxiliaries":proofs}


def native_git_index(data,note,note_bytes,begin,end,*,filesystem_clock=None):
    """The ruled fixture has one ordinary note, native SHA1 index v2/TREE."""
    require(len(data)>=12+64+20 and data[:12]==b"DIRC\0\0\0\2\0\0\0\1"
            and hashlib.sha1(data[:-20]).digest()==data[-20:],"native Git index header/count/checksum differs")
    fields=struct.unpack(">10I20sH",data[12:74])
    path_end=data.find(b"\0",74)
    require(path_end>=74 and data[74:path_end]==b"note.md" and fields[11]==7,
            "native Git index has an unselected path, stage or flag")
    offset=12+((path_end+1-12+7)//8)*8
    require(data[path_end:offset]==b"\0"*(offset-path_end),"native Git index has noncanonical entry padding")
    blob=hashlib.sha1(b"blob "+str(len(note_bytes)).encode()+b"\0"+note_bytes).digest()
    tree_bytes=b"100644 note.md\0"+blob
    tree_oid=hashlib.sha1(b"tree "+str(len(tree_bytes)).encode()+b"\0"+tree_bytes).digest()
    cache_tree=b"\0"+b"1 0\n"+tree_oid
    require(data[offset:-20]==b"TREE"+struct.pack(">I",len(cache_tree))+cache_tree,
            "native Git index carries an unknown or inconsistent extension")
    info=note.stat()
    require(stat.S_ISREG(info.st_mode) and note.read_bytes()==note_bytes and fields[10]==blob
            and fields[6]==0o100644 and fields[7]==info.st_uid and fields[8]==info.st_gid
            and fields[9]==len(note_bytes) and fields[4]==info.st_dev&0xffffffff
            and fields[5]==info.st_ino&0xffffffff,"native Git index differs from its actual checked-out note/stat/blob")
    require(fields[0]*10**9+fields[1]==info.st_ctime_ns
            and fields[2]*10**9+fields[3]==info.st_mtime_ns,"native Git index stat cache is not the actual checkout")
    moment_ns(info.st_ctime_ns,begin,end,path=str(note)+" (ctime)",filesystem_clock=filesystem_clock)
    moment_ns(info.st_mtime_ns,begin,end,path=note,filesystem_clock=filesystem_clock)
    return {"version":2,"entries":[{"path":"note.md","flags":7,"mode":fields[6],"uid":fields[7],
             "gid":fields[8],"bytes":fields[9],"blob_sha1":blob.hex(),"device":fields[4],
             "inode":"<validated-native-checkout-inode>","ctime":"<bounded-native-checkout-time>",
             "mtime":"<bounded-native-checkout-time>"}],"tree_sha1":tree_oid.hex(),
             "checksum":"<validated-complete-native-index-checksum>"}


def native_reflog(raw,transitions,name,email,begin,end):
    lines=raw.decode("utf-8").splitlines(keepends=True)
    require(len(lines)==len(transitions),"native Git reflog has an unexpected record population")
    result=[]
    for line,(old,new,message) in zip(lines,transitions):
        pattern=re.escape(old+" "+new+" "+name+" <"+email+"> ")+r"([0-9]+) ([+-][0-9]{4})"
        if message is not None: pattern+=re.escape("\t"+message)
        match=re.fullmatch(pattern+r"\n",line)
        require(match is not None,"native Git reflog actor/ref/message differs from its exact operation")
        stamp=int(match.group(1))
        require(begin.timestamp()-1<=stamp<=end.timestamp(),"native Git reflog clock is outside its collected invocation")
        zone=match.group(2)
        require(int(zone[1:3])<=14 and int(zone[3:])<60,"native Git reflog offset is invalid")
        result.append({"old":old,"new":new,"name":name,"email":email,
                       "timestamp":"<bounded-native-reflog-second>","offset":zone,"message":message})
    return result


def native_additions(before,after,files,*,directory_mode,begin,end,empty_directories=(),filesystem_clock=None):
    """Exact generated files, declared empty directories and necessary parents."""
    old=before["entries"]
    empty_directories=set(empty_directories)
    require(not empty_directories.intersection(old) and not empty_directories.intersection(files),
            "native empty-directory proof overlaps retained evidence")
    written=set(files)|empty_directories
    parents=set()
    for path in tuple(written):
        for parent in Path(path).parents:
            name=parent.as_posix()
            if name in old:
                parents.add(name)
                break
            written.add(name)
    projected=exact_survivors(before,after,written=written,directory_clocks=parents,begin=begin,end=end,
                              filesystem_clock=filesystem_clock)
    for path in written-set(files):
        require(after["entries"][path]["kind"]=="directory" and after["entries"][path]["mode"]==directory_mode,
                "native Git created a foreign directory type/mode")
    for path in empty_directories:
        require(not any(name.startswith(path+"/") for name in after["entries"]),
                "native declared empty Git directory contains foreign evidence")
    for path,expected in files.items():
        row=after["entries"][path]
        require(row["kind"]=="file" and row["mode"]==expected["mode"]
                and row["sha256"]==digest(expected["raw"]),"native Git generated file differs: "+path)
        if "projection" in expected:
            # The entire exact parser result was validated above; retain raw
            # bytes in the archive and bind this comparison checksum to that
            # complete projected value, never an arbitrary file scrub rule.
            projected["entries"][path]["sha256"]=digest(canonical(expected["projection"]).encode())
    return projected


def native_lane_admin_additions(before,after,files,prefix,*,directory_mode,begin,end,filesystem_clock=None):
    # Git 2.43's files backend has no per-worktree refs directory; the newer
    # backend creates it even when empty. These are two closed layouts, not
    # permission to accept arbitrary observed directories. A present refs
    # directory must still have its exact mode, clock and empty population.
    refs=prefix+"/refs"
    empty_refs={refs} if refs in after["entries"] else set()
    return native_additions(before,after,files,directory_mode=directory_mode,begin=begin,end=end,
                            empty_directories=empty_refs,filesystem_clock=filesystem_clock)


def lane_open_auxiliary(root,workspace,context,artifact,normalized,baseline_path,manifest,states,begin,end,*,filesystem_clock=None):
    from floati.lane_workspaces import LaneWorkspaces
    record=artifact["evidence"]["record"]
    require(record["state"]=="open" and record["node_id"]=="scale-worker"
            and record["row"]=="census-lifecycle" and record["repo"]=="census"
            and record["branch"]=="codex/lane/scale-worker/census-lifecycle"
            and record["path"]=="scale-worker/work/census-lifecycle", "native lane opening has a foreign coordinate")
    prep=Path(manifest["roots"][0]["root"])
    lane=prep/"lanes"/record["path"]
    require(artifact["evidence"]["workspace"]==str(lane),"native lane stdout differs from the exact owned checkout")
    prior=baseline_path/manifest["roots"][1]["snapshot"]
    before_workspace=manifest["roots"][1]["inventory"]
    baseline_files={p for p,row in before_workspace["entries"].items()
                    if row["kind"]=="file" and p!=".git" and not p.startswith(".git/")}
    require(baseline_files=={"note.md"},"lane fixture requires the closed one-note native repository")
    sha=record["base_sha"]
    require((prior/".git/refs/remotes/origin/main").read_bytes()==(sha+"\n").encode(),
            "native lane base differs from its owned banked remote ref")
    note_bytes=(prior/"note.md").read_bytes()
    admin=workspace/".git/worktrees/census-lifecycle"
    prefix=admin.relative_to(workspace).as_posix()
    require((lane/".git").read_bytes()==("gitdir: "+str(admin)+"\n").encode(),"native checkout points at foreign Git admin")
    config=configparser.RawConfigParser()
    config.read_string((prior/".git/config").read_text())
    name,email=config["user"]["name"],config["user"]["email"]
    index=(admin/"index").read_bytes()
    index_projection=native_git_index(index,lane/"note.md",note_bytes,begin,end,filesystem_clock=filesystem_clock)
    head_log=(admin/"logs/HEAD").read_bytes()
    zero="0"*40
    head_projection=native_reflog(head_log,[(zero,sha,None),(sha,sha,"reset: moving to HEAD")],name,email,begin,end)
    branch_path=".git/refs/heads/"+record["branch"]
    branch_log_path=".git/logs/refs/heads/"+record["branch"]
    branch_log=(workspace/branch_log_path).read_bytes()
    branch_projection=native_reflog(branch_log,[(zero,sha,"branch: Created from "+sha)],name,email,begin,end)
    normalized_record=normalized["artifact"]["evidence"]["record"]
    owner_marker=LaneWorkspaces._marker_bytes(record["id"])
    normalized_marker={"schema_version":0,"opened_record_id":normalized_record["id"]}
    def mode(path): return before_workspace["entries"][path]["mode"]
    admin_files={
        "HEAD":{"raw":("ref: refs/heads/"+record["branch"]+"\n").encode(),"mode":mode(".git/HEAD")},
        "ORIG_HEAD":{"raw":(sha+"\n").encode(),"mode":mode(".git/HEAD")},
        "commondir":{"raw":b"../..\n","mode":mode(".git/HEAD")},
        "gitdir":{"raw":(str(lane/".git")+"\n").encode(),"mode":mode(".git/HEAD")},
        "index":{"raw":index,"mode":mode(".git/index"),"projection":index_projection},
        "logs/HEAD":{"raw":head_log,"mode":mode(".git/logs/HEAD"),"projection":head_projection},
        "floati-lane-owner.json":{"raw":owner_marker,"mode":mode(".git/HEAD"),"projection":normalized_marker},
    }
    files={prefix+"/"+name:row for name,row in admin_files.items()}
    files[branch_path]={"raw":(sha+"\n").encode(),"mode":mode(".git/refs/heads/main")}
    files[branch_log_path]={"raw":branch_log,"mode":mode(".git/logs/refs/heads/main"),"projection":branch_projection}
    for path,row in files.items():
        require((workspace/path).read_bytes()==row["raw"],"native lane generated a foreign Git file payload: "+path)
    directory_mode=mode(".git/refs/heads")
    projected_workspace=native_lane_admin_additions(before_workspace,states[1],files,prefix,
                            directory_mode=directory_mode,begin=begin,end=end,filesystem_clock=filesystem_clock)
    lane_prefix=lane.relative_to(prep).as_posix()
    prep_files={lane_prefix+"/note.md":{"raw":note_bytes,"mode":mode("note.md")},
                lane_prefix+"/.git":{"raw":("gitdir: "+str(admin)+"\n").encode(),"mode":mode(".git/HEAD")}}
    projected_prep=native_additions(manifest["roots"][0]["inventory"],states[0],prep_files,
                                   directory_mode=directory_mode,begin=begin,end=end,filesystem_clock=filesystem_clock)
    return {"kind":"exact_native_lane_open","record":normalized_record,"git_index":index_projection,
            "head_reflog":head_projection,"branch_reflog":branch_projection,
            "auxiliaries":[projected_prep,projected_workspace]}


def auxiliary_proof(case,home,workspace,context,baseline_path,manifest,states,artifact,normalized,begin,end,*,filesystem_clock=None):
    from floati.root import FloatiRoot
    root=FloatiRoot.open_direct_home(home,create=False) if case!="wake-callback" else FloatiRoot.open(home.parent.parent,home.name)
    if case=="seat-teardown":
        require(len(states)==3 and all(row["inventory"]==states[index] for index,row in enumerate(manifest["roots"])),
                "native teardown changed its preparation bank, primary seat home or owned bare origin")
        return {"kind":"complete_auxiliary_proof","auxiliaries":[{"kind":"unchanged","inventory":state} for state in states]}
    if case=="lane-open":
        return lane_open_auxiliary(root,workspace,context,artifact,normalized,baseline_path,manifest,states,begin,end,
                                   filesystem_clock=filesystem_clock)
    if case=="lane-close":
        selected=dict(context,_auxiliary_baseline=str(baseline_path))
        return lane_close_auxiliary(root,workspace,selected,artifact,manifest,states,begin,end,filesystem_clock=filesystem_clock)
    proofs=[]
    for index,row in enumerate(manifest["roots"]):
        before,after=row["inventory"],states[index]
        if index<2:
            if case=="seat-board" and index==1:
                from floati.seat_declaration import SeatDeclaration,COORDINATOR_AUTHORITIES,OWNER_TIERS
                expected=SeatDeclaration(1,root.tenant_id,str(root.path),"scale-worker","star","architect",
                                         tuple(COORDINATOR_AUTHORITIES),tuple(OWNER_TIERS))
                require(SeatDeclaration.load(workspace)==expected,"native boarding published a foreign seat declaration")
                require(after["entries"]["SEAT.json"]["mode"]==0o600,"native seat marker mode differs")
                projected=exact_survivors(before,after,written={"SEAT.json"},directory_clocks={"."},begin=begin,end=end,
                                          filesystem_clock=filesystem_clock)
                proofs.append({"kind":"native_seat_declaration","declaration":expected.artifact(),"after":projected})
            elif case=="verify" and index==1:
                from floati.jsonl import read_records_snapshot
                from floati.verification import DeliveryVerifier
                evidence=copy.deepcopy(artifact["evidence"])
                evidence.pop("scope",None)
                claim=context["claim"]
                scratch=home/"scratch/verifications"/claim["id"]
                require(artifact["command"]=="verify" and artifact["status"]=="ok"
                        and evidence["kind"]=="verification_receipt" and evidence["outcome"]=="verified_match"
                        and evidence["claim"]==claim and DeliveryVerifier(root)._claim(claim["id"])==claim
                        and evidence["repo_path"]==str(workspace) and claim["sha"]==context["verification_repo_sha"]
                        and evidence["scratch"]=={"path":str(scratch),"created":True,"destroyed":True}
                        and not scratch.exists() and not scratch.is_symlink(),
                        "native Git directory clock lacks its exact successful verified/cleaned scratch proof")
                relative=scratch.relative_to(home).as_posix()
                require(not any(path==relative or path.startswith(relative+"/")
                                for path in manifest["prepared_original"]["entries"]),
                        "native verification scratch existed before the measured invocation")
                receipts=read_records_snapshot(root,"receipts/verifications.jsonl",allowed_kinds={"verification_receipt"})
                require(receipts and receipts[-1]==evidence,"native Git directory clock differs from its authored verification receipt")
                projected=exact_survivors(before,after,directory_clocks={".git"},begin=begin,end=end,filesystem_clock=filesystem_clock)
                proofs.append({"kind":"native_verified_scratch_git_directory_clock",
                               "receipt":normalized["artifact"]["evidence"],"after":projected})
            else:
                require(before==after,"native operation changed an unrelated auxiliary root: "+row["root"])
                proofs.append({"kind":"unchanged","inventory":after})
        else:
            require(before["kind"]=="absent","native publication case requires its once-captured absent baseline")
            path=Path(row["root"])
            projected=native_publication(case,root,path,after,artifact,context,begin,end,normalized=normalized,
                                         filesystem_clock=filesystem_clock)
            proofs.append({"kind":"complete_native_publication","after":projected})
    return {"kind":"complete_auxiliary_proof","auxiliaries":proofs}


def restore(pair,seed,seed_tree,baseline_path,manifest,receipt_path):
    started=datetime.now(timezone.utc)
    monotonic=time.monotonic()
    require(not Path(receipt_path).exists(),"restore receipt cannot preexist")
    for row in manifest["roots"]:
        require(tree(baseline_path/row["snapshot"])==row["inventory"],"auxiliary snapshot changed before restoration")
        restore_exact(baseline_path/row["snapshot"],Path(row["root"]),row["inventory"])
    require(tree(seed)==seed_tree,"immutable seed changed before restoration")
    restore_exact(seed,Path(pair["prepared_home"]),seed_tree)
    require(tree(seed)==seed_tree,"immutable seed changed during restoration")
    receipt={"root":str(pair["prepared_home"]),"seed":str(seed),"seed_unchanged":True,
             "restored_exact":True,"inventory":seed_tree,"inventory_sha256":digest(canonical(seed_tree).encode()),
             "auxiliaries":[{"root":row["root"],"restored_exact":True,"inventory":row["inventory"]}
                            for row in manifest["roots"]],
             "outside_child_measurement":True,"started_at":started.isoformat(),
             "completed_at":datetime.now(timezone.utc).isoformat(),"seconds":time.monotonic()-monotonic}
    write_json(receipt_path,receipt)
    return receipt


def native_delta(before,after,*,home=None,cursor_prefix_input=None,delivery_prefix_input=None,prefix_input=None,
                 registry_prefix_input=None,inbox_prefix_input=None,consumed_prefix_input=None,
                 inbox_append_input=None,cut_input=None):
    cache_names=set()
    candidate={}
    if cursor_prefix_input is not None or delivery_prefix_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import cache_name,raw_candidate_delta
        require(home is not None,"cursor cache transport lacks its exact live root")
        if cursor_prefix_input is not None:
            cache_names.add(cache_name(cursor_prefix_input["ack_relative"]))
        if delivery_prefix_input is not None:
            cache_names.add(cache_name(delivery_prefix_input["delivery_relative"]))
        candidate=raw_candidate_delta(before,after)
    if prefix_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
        require(home is not None,"send cache transport lacks its exact live root")
        cache_names.add(".send-prefix")
        candidate=raw_candidate_delta(before,after)
    registry_changed=any((path==".registry-prefix" or path.startswith(".registry-prefix/"))
                         and before.get(path)!=after.get(path) for path in set(before)|set(after))
    if registry_changed:
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
        require(home is not None,"registry cache transport lacks its exact live root")
        cache_names.add(".registry-prefix")
        candidate=raw_candidate_delta(before,after)
    extra_proofs=[]
    if cut_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import CACHE as CUT_CACHE,verify_cut_prefix_effect
        extra_proofs.append((CUT_CACHE,verify_cut_prefix_effect,cut_input))
    inbox_storage=None
    require(inbox_prefix_input is None or inbox_append_input is None,
            "inbox transport must declare either an unchanged source or a native append")
    if inbox_prefix_input is not None or inbox_append_input is not None:
        from floati.inbox_prefix import storage_declaration
        inbox_storage=storage_declaration()
        captured=inbox_prefix_input if inbox_prefix_input is not None else inbox_append_input
        require(type(captured) is dict and captured.get("storage_declaration")==inbox_storage,
                "native index input differs from its writer storage declaration")
        require(inbox_storage["source"]["role"]=="retained_append_only"
                and inbox_storage["index"]["role"]=="derived_rebuildable",
                "native inbox writer storage roles are unsupported")
    if inbox_prefix_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import verify_inbox_prefix_effect
        extra_proofs.append((inbox_storage["index"]["path"],verify_inbox_prefix_effect,inbox_prefix_input))
    if inbox_append_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import verify_inbox_prefix_append_effect
        extra_proofs.append((inbox_storage["index"]["path"],verify_inbox_prefix_append_effect,inbox_append_input))
    if consumed_prefix_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_consumed_prefix_effects import cache_name,verify_consumed_prefix_effect
        extra_proofs.append((cache_name(consumed_prefix_input["relative"]),
            verify_consumed_prefix_effect,consumed_prefix_input))
    for name,verify,captured in extra_proofs:
        declared_append=(captured is inbox_append_input and inbox_append_input is not None
                         and before.get(inbox_storage["source"]["path"])!=after.get(inbox_storage["source"]["path"]))
        if declared_append or any((path==name or path.startswith(name+"/")) and before.get(path)!=after.get(path)
               for path in set(before)|set(after)):
            from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import raw_candidate_delta
            require(home is not None,"native cache transport lacks its exact live root")
            cache_names.add(name)
            candidate=raw_candidate_delta(before,after)
    result={}
    for path in sorted(set(before)|set(after)):
        old,new=before.get(path,b""),after.get(path)
        if before.get(path)==new: continue
        if any(path==name or path.startswith(name+"/") for name in cache_names):
            result[path]=candidate[path]
            continue
        if new is None: operation,payload="delete",b""
        elif not new and path.endswith(".lock"): operation,payload="create_lock",b""
        elif path.endswith(".json") and not (inbox_storage is not None
                and path==inbox_storage["source"]["path"]): operation,payload="replace",new
        else:
            require(new.startswith(old),"native append rewrote retained prefix: "+path)
            operation,payload="append",new[len(old):]
        result[path]={"operation":operation,"before_sha256":digest(old),"after_sha256":digest(new or b""),
                      "before_bytes":len(old),"after_bytes":len(new or b""),"payload_base64":base64.b64encode(payload).decode()}
    # Binary cache replacements are provisional until their complete native
    # transcripts, maps, private metadata and exact insertion delta prove them.
    if cursor_prefix_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import verify_cursor_prefix_effect
        verify_cursor_prefix_effect(home,before,after,result,cursor_prefix_input,
            node=cursor_prefix_input["node"],worker_session_id=cursor_prefix_input["worker_session_id"],
            delivery_input=delivery_prefix_input)
    elif delivery_prefix_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import verify_delivery_prefix_effect
        verify_delivery_prefix_effect(home,before,after,result,delivery_prefix_input,
            node=delivery_prefix_input["node"],worker_session_id=delivery_prefix_input["worker_session_id"])
    if prefix_input is not None:
        from _bus_scale_fixture_proofs.bus_scale_prefix_effects import verify_prefix_effect
        verify_prefix_effect(home,before,after,result,prefix_input)
    if registry_changed:
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import verify_registry_prefix_effect
        verify_registry_prefix_effect(home,before,after,result,registry_prefix_input)
    for name,verify,captured in extra_proofs:
        if name in cache_names:
            coordinates=({"node":captured["node"],"worker_session_id":captured["worker_session_id"]}
                if name.startswith(".consumed-prefix-") else {})
            verify(home,before,after,result,captured,**coordinates)
    return result


def validate_detail(detail,case,mode,code,context):
    expected={"mode","case","native_exit_code","argv","wait_changes","idle","startup"}
    if mode=="diagnostic": expected|={"read_bytes_by_path","hold_families","external_processes","coverage"}
    if case=="verify": expected.add("observer_scope")
    require(type(detail) is dict and set(detail)==expected and detail["mode"]==mode and detail["case"]==case
            and type(detail["native_exit_code"]) is int and detail["native_exit_code"]==code,
            "native entry detail is missing, incomplete or belongs to another invocation")
    require(detail["wait_changes"]==[] and detail["idle"]=={} and detail["startup"]=={},
            "expanded observation cannot inherit an old waiter phase")
    if case=="verify":
        anchors={"verification_repo_sha","verification_tree","verification_git_files"}
        require(type(context) is dict and anchors<=set(context),"verification observer lacks its prepared source anchors")
        scope={"bus_bytes":"parent_python_application_visible","ledger_flock_hold":"parent_native_flock",
               "cpu":"whole_entry_child_tree",
               "child_bytes":{"status":"UNMEASURED","reason":"not-ledger-addressed"},
               "git_locks":{"status":"UNMEASURED","reason":"not-ledger-addressed"},
               **{key:copy.deepcopy(context[key]) for key in anchors}}
        require(canonical(detail["observer_scope"])==canonical(scope),
                "verification observer scope differs from its exact ruled boundary/source anchors")
    if mode=="diagnostic":
        require(detail["coverage"]=={"bytes":True,"locks":True},"native diagnostic coverage is incomplete")
        require(type(detail["read_bytes_by_path"]) is dict and all(type(p) is str and type(n) is int and n>=0
                for p,n in detail["read_bytes_by_path"].items()),"native read-byte testimony is malformed")
        require(type(detail["external_processes"]) is list and all(type(row) is dict and type(row.get("returncode")) is int
                for row in detail["external_processes"]),"diagnostic has an uncollected external child")
        require(set(detail["hold_families"])=={"ledger","epoch","coordination","lifetime","other"},
                "native lock testimony lacks a whole family")
        for row in detail["hold_families"].values():
            require(set(row)=={"count","maximum_seconds","intervals"} and type(row["count"]) is int
                    and row["count"]==len(row["intervals"]) and type(row["maximum_seconds"]) in (int,float)
                    and math.isfinite(row["maximum_seconds"]) and row["maximum_seconds"]>=0,
                    "native lock-family testimony is malformed")


def attention_clock_pair(case,template,place):
    """Publish one proved arm clock while retaining the immutable scenario."""
    original=template["context"]
    previous=template.get("_clock_publication")
    published=original
    if previous is not None:
        require(type(previous) is dict and set(previous)=={"context","receipt"},
                "previous attention context publication is malformed")
        published=previous["context"]
        reference=previous["receipt"]
        require(type(reference) is dict and set(reference)=={"path","sha256"},
                "previous attention clock reference is malformed")
        path=ordinary(reference["path"],directory=False)
        raw=path.read_bytes()
        require(digest(raw)==reference["sha256"],"previous attention clock receipt changed")
        receipt=json.loads(raw)
        require(raw==(canonical(receipt)+"\n").encode()
                and receipt["case"]==case and type(receipt["version"]) is int and receipt["version"]==1
                and receipt["source_canonical_time"]==original["canonical_time"]
                and receipt["canonical_time"]==published["canonical_time"]
                and set(published)==set(original)
                and {key:value for key,value in published.items() if key!="canonical_time"}
                    =={key:value for key,value in original.items() if key!="canonical_time"},
                "previous attention clock receipt does not bind its published context")
        for scale in ("1","10"):
            source=ordinary(template[scale],directory=True)
            require(receipt["scales"][scale]["template"]==str(source)
                    and receipt["scales"][scale]["template_inventory"]==file_inventory(source),
                    "previous attention clock receipt lost its immutable template binding")
    detached=materialize_attention_pair(template,case,place)
    context_path=Path(template["prepared_home"]).parent/"case-context.json"
    publish_attention_context(context_path,published,detached["context"])
    template["_clock_publication"]={"context":copy.deepcopy(detached["context"]),
                                    "receipt":copy.deepcopy(detached["clock_materialization"])}
    return detached


def teardown_product_proof():
    """Outside measured children: prove the actual complete d66 product tree."""
    from floati.git_process import fixed_git_command, fixed_git_environment
    product="d66a94daa357bd392b3040c55cbe21e20ac884a3"
    paths=("floati","schemas","bundle-manifest.v0.json")
    testimony=[]
    def git(*query):
        argv=fixed_git_command("/usr/bin/git",ROOT,query)
        result=subprocess.run(argv,cwd=ROOT,env=fixed_git_environment("/usr/bin/git"),
                              capture_output=True,timeout=30)
        require(len(result.stdout)+len(result.stderr)<=8*1024*1024,"product proof output exceeds bound")
        testimony.append({"argv":argv,"exit_code":result.returncode,
                          "stdout":result.stdout.decode("utf-8"),"stderr":result.stderr.decode("utf-8")})
        require(result.returncode==0 and not result.stderr,"fixed Git product proof is incomplete")
        return result.stdout
    require(git("status","--porcelain=v1","-z","--untracked-files=all","--",*paths)==b"",
            "functional refusal product contains modified or untracked nonignored source")
    require(git("diff","--exit-code",product,"--",*paths)==b"", "functional refusal product differs from ruled d66")
    entries=git("ls-tree","-r","-z",product,"--",*paths).split(b"\0")
    require(entries and entries[-1]==b"" and len(entries)<=10000,"product proof file population is incomplete or unbounded")
    files={}
    total=0
    for entry in entries[:-1]:
        header,name=entry.split(b"\t",1)
        mode,kind,object_id=header.decode("ascii").split(" ")
        require(kind=="blob" and mode in {"100644","100755"}
                and re.fullmatch(r"[0-9a-f]{40}",object_id),"pinned product tree has an unreviewed object kind/mode/identity")
        relative=name.decode("utf-8")
        path=ROOT/relative
        info=path.lstat()
        require(relative not in files and not Path(relative).is_absolute() and ".." not in Path(relative).parts
                and (relative==paths[2] or relative.startswith(("floati/","schemas/")))
                and path.resolve(strict=True)==path and stat.S_ISREG(info.st_mode) and info.st_nlink==1,
                "product proof contains an alias, shared/special file or foreign path")
        raw=path.read_bytes()
        total+=len(raw)
        require(total<=64*1024*1024,"product source proof exceeds bounded bytes")
        actual_blob=hashlib.sha1(b"blob "+str(len(raw)).encode("ascii")+b"\0"+raw).hexdigest()
        require(actual_blob==object_id and stat.S_IMODE(info.st_mode)==(0o755 if mode=="100755" else 0o644),
                "actual product bytes or regular/executable mode differ from pinned d66 blob: "+relative)
        files[relative]={"bytes":len(raw),"sha256":digest(raw),"mode":stat.S_IMODE(info.st_mode),
                         "git_mode":mode,"pinned_git_blob":object_id,"actual_git_blob":actual_blob}
    return {"product_sha":product,"files":files,"fixed_git":testimony}


def capture_retained_hint_input(home,before,before_raw):
    """Bind existing empty hint identities before the native child launches."""
    from _bus_scale_fixture_proofs.bus_scale_wake_hint_effects import _family,_metadata
    empty=digest(b"")
    hints={}
    for path,row in before["entries"].items():
        if not _family(path) or row["kind"]!="file":
            continue
        require(row["mode"]==0o600 and row["bytes"]==0 and row["sha256"]==empty
                and before_raw.get(path)==b"","retained hint input is not an exact private empty native file")
        metadata=_metadata(home,path)
        require(metadata["mtime_ns"]==row["mtime_ns"],"hint metadata changed before native invocation")
        hints[path]=metadata
    return {"version":1,"root":str(home),"hints":hints}


def retained_hint_touches(home,before,after,before_raw,after_raw,begin,end,hint_input,*,ledger_kinds,filesystem_clock=None):
    """Prove only retained empty-file touches routed by actual native appends."""
    from _bus_scale_fixture_proofs.bus_scale_wake_hint_effects import _family,_metadata,_permitted
    from floati.root import FloatiRoot
    old,new=before["entries"],after["entries"]
    touched={path for path in set(old)&set(new) if _family(path)
             and old[path]["kind"]=="file" and old[path]!=new[path]}
    if not touched:
        return {}
    require(home is not None and type(before_raw) is dict and type(after_raw) is dict
            and type(hint_input) is dict and set(hint_input)=={"version","root","hints"}
            and type(hint_input["version"]) is int and hint_input["version"]==1
            and hint_input["root"]==str(home) and type(hint_input["hints"]) is dict,
            "retained hint touches lack pre-child identity testimony")
    permitted=_permitted(FloatiRoot.open_direct_home(home,create=False),before_raw,after_raw,ledger_kinds)
    empty=digest(b"")
    proofs={}
    for path in sorted(touched):
        require(path in permitted,"unreviewed retained hint touch: "+path)
        prior,row=old[path],new[path]
        expected={"kind":"file","mode":0o600,"bytes":0,"sha256":empty}
        require({key:value for key,value in prior.items() if key!="mtime_ns"}==expected
                and {key:value for key,value in row.items() if key!="mtime_ns"}==expected
                and before_raw.get(path)==after_raw.get(path)==b"",
                "retained hint touch changed private empty native content or mode")
        captured=hint_input["hints"].get(path)
        require(type(captured) is dict and set(captured)=={"root","lineage","mtime_ns"}
                and captured["mtime_ns"]==prior["mtime_ns"],
                "retained hint touch has no exact pre-child identity")
        current=_metadata(home,path)
        require(current["root"]==captured["root"] and current["lineage"]==captured["lineage"]
                and current["mtime_ns"]==row["mtime_ns"],
                "retained hint touch replaced its captured ordinary owned lineage")
        moment_ns(row["mtime_ns"],begin,end,path=home/path,filesystem_clock=filesystem_clock)
        proofs[path]={"operation":"touch_empty_hint","kind":"native_wake_hint"}
    return proofs


def prepare_teardown_inbox(home):
    """Prove the exact cache-only preflight, including its possible root clock."""
    from floati.inbox_prefix import warm
    from floati.root import FloatiRoot
    before=tree(home)
    identity=(home.stat().st_dev,home.stat().st_ino)
    started,filesystem_clock=capture_filesystem_clock()
    require(warm(FloatiRoot.open_direct_home(home)) is not None,"teardown inbox preflight failed")
    ended=datetime.now(timezone.utc)
    after=tree(home)
    require(identity==(home.stat().st_dev,home.stat().st_ino),"inbox preflight replaced its bus root")
    def plain(value):
        entries=copy.deepcopy({p:row for p,row in value["entries"].items()
            if p!=".inbox-prefix" and not p.startswith(".inbox-prefix/")})
        entries["."].pop("mtime_ns")
        return entries
    require(plain(before)==plain(after),"inbox preflight changed unrelated bytes or metadata")
    old,new=before["entries"]["."],after["entries"]["."]
    if old==new:
        return None
    require(".inbox-prefix" not in before["entries"] and ".inbox-prefix" in after["entries"],
            "inbox preflight touched root without creating its exact cache")
    moment_ns(new["mtime_ns"],started,ended,path=home,filesystem_clock=filesystem_clock)
    return {"root":str(home),"root_identity":list(identity),"root_before":old,"root_after":new,
            "noncache":plain(after),"started_at":started.isoformat(),"completed_at":ended.isoformat(),
            "filesystem_clock":filesystem_clock}


def retained_registry_tree(home,before,after,before_raw,after_raw,captured):
    """Project one unchanged bank only after its complete native registry proof."""
    from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import CACHE,verify_registry_prefix_effect
    def selected(entries):
        return {p:row for p,row in entries.items() if p==CACHE or p.startswith(CACHE+"/")}
    old,new=selected(before["entries"]),selected(after["entries"])
    require(bool(old) and old==new,"retained registry bank changed bytes, population or metadata")
    require(selected(before_raw)==selected(after_raw),"retained registry bank changed raw bytes")
    proof=verify_registry_prefix_effect(home,before_raw,after_raw,{},captured)
    require(not proof["changed_files"],"retained registry proof contains a mutation")
    return {"kind":"complete_validated_retained_registry_prefix",
            **{key:proof[key] for key in ("source_sha256","head","registry_records","complete_map_entries")}}


CURSOR_PREPARE_CASES=frozenset({"ack","consume","work-complete","inbox","node-drain","wake-wait"})


def prepare_cursor_input(home,node,*,worker_session_id=None,cursor_preflights=()):
    """Reuse only current invocation-local complete input, otherwise warm natively."""
    from _bus_scale_fixture_proofs import bus_scale_cursor_prefix_effects as keys
    if cursor_preflights:
        testimony=keys.reuse_cursor_preflight(home,raw_files(home),node=node,
            worker_session_id=worker_session_id,cursor_preflights=cursor_preflights)
        if testimony is not None:
            return testimony["native_result"]
    from floati.cursor_prefix import warm
    from floati.root import FloatiRoot
    return warm(FloatiRoot.open_direct_home(home),node,worker_session_id=worker_session_id)


def capture_mail_certificates(home,raw,*,cursor_preflights=()):
    """Validate complete native banks on the live pre-child root."""
    from floati.root import FloatiRoot
    from floati import send_prefix
    from _bus_scale_fixture_proofs import bus_scale_cursor_prefix_effects as keys
    from _bus_scale_fixture_proofs import bus_scale_receipt_prefix_effects as receipts
    from _bus_scale_fixture_proofs import bus_scale_consumed_prefix_effects as consumed
    root=FloatiRoot.open_direct_home(home)
    certificates={}
    for name in sorted({Path(path).parts[0] for path in raw}):
        if not name.startswith((".cursor-prefix-",".receipt-prefix-",".consumed-prefix-")) or name in certificates:
            continue
        checkpoint=raw.get(name+"/checkpoint.json")
        require(checkpoint is not None,"retained mail bank lacks checkpoint")
        payload=json.loads(checkpoint)["payload"]
        relative=payload["binding"]["relative"]
        if name.startswith(".cursor-prefix-"):
            node=payload["binding"]["node"]
            plane=payload["binding"]["worker_session_id"]
            testimony=keys.reuse_cursor_preflight(home,raw,node=node,
                worker_session_id=plane,cursor_preflights=cursor_preflights)
            if testimony is None:
                testimony=keys.verify_complete_cursor_banks(home,raw,node=node,worker_session_id=plane)
            certificates.update(testimony["certificates"])
        elif name.startswith(".receipt-prefix-"):
            node=Path(relative).stem
            require(name==receipts.cache_name(node) and relative==receipts.ledger_relative(node),
                "retained receipt bank has a foreign coordinate")
            cache=receipts._cache(raw,name)
            receipts._check_metadata(receipts._metadata(home,name),cache,name,bucketed=True)
            source=raw.get(relative,b"")
            rows=receipts._transcript(root,relative,source)
            receipts._checkpoint(root,name,relative,cache,source,rows,send_prefix._identity(home/relative))
            certificates[name]=keys._content_certificate(raw,name,relative)
        else:
            parts=Path(relative).parts
            require(len(parts) in (3,4) and parts[:2]==("receipts","consumed")
                and Path(relative).as_posix()==relative,"retained consumed bank has a foreign coordinate")
            node=Path(relative).stem if len(parts)==3 else parts[2]
            rows=keys._transcript(root,relative,consumed.KINDS,raw.get(relative,b""))
            if len(parts)==3:
                plane=None
            elif rows:
                plane=rows[0]["worker_session_id"]
            else:
                events,_known=keys._events._transcript(root,raw.get("events.jsonl",b""))
                candidates={row["worker_session_id"] for row in events
                    if row["kind"]=="message_envelope" and row["recipient"]==node
                    and row.get("worker_session_id") is not None}
                matches=[candidate for candidate in candidates
                    if consumed._coordinates(root,node,candidate)[0]==relative]
                require(len(matches)==1,"empty retained consumed worker plane is underivable")
                plane=matches[0]
            require(consumed._coordinates(root,node,plane)[0]==relative
                and consumed.cache_name(relative)==name,"retained consumed bank has a foreign plane")
            consumed.verify_complete_consumed_bank(home,raw,node=node,worker_session_id=plane)
            certificates[name]=keys._content_certificate(raw,name,relative)
    return certificates


def retained_mail_trees(home,before,after,before_raw,after_raw,certificates):
    """Retain exact within-arm identity before excluding rebuild entropy."""
    result={}
    for name,certificate in certificates.items():
        def selected(entries):
            return {path:value for path,value in entries.items() if path==name or path.startswith(name+"/")}
        require(bool(selected(before["entries"])) and selected(before["entries"])==selected(after["entries"]),
            "retained mail bank changed bytes, population or metadata")
        require(selected(before_raw)==selected(after_raw),"retained mail bank changed raw bytes")
        from _bus_scale_fixture_proofs import bus_scale_cursor_prefix_effects as keys
        payload=json.loads(before_raw[name+"/checkpoint.json"])["payload"]
        require(certificate==keys._content_certificate(before_raw,name,payload["binding"]["relative"]),
            "retained mail certificate differs from its validated pre-child content")
        result[name]=copy.deepcopy(certificate)
    return result


def teardown_bus_tree(before,after,normalized,begin,end,prefix_proof=None,*,home=None,
                      before_raw=None,after_raw=None,hint_input=None,inbox_append_proof=None,inbox_preflight=None,
                      registry_input=None,filesystem_clock=None,mail_certificates=None,cut_proof=None):
    """Admit exact native files and their ancestors, including empty dirs."""
    require(before["kind"]==after["kind"]=="directory","teardown bus root changed kind")
    old,new=before["entries"],after["entries"]
    if inbox_preflight is not None:
        require(type(inbox_preflight) is dict and set(inbox_preflight)=={
            "root","root_identity","root_before","root_after","noncache","started_at","completed_at","filesystem_clock"}
            and inbox_preflight["root"]==str(home) and old["."]==inbox_preflight["root_after"]
            and inbox_preflight["root_identity"]==[home.stat().st_dev,home.stat().st_ino],
            "inbox preflight root clock lacks its exact before-tree binding")
        started=datetime.fromisoformat(inbox_preflight["started_at"])
        ended=datetime.fromisoformat(inbox_preflight["completed_at"])
        require(started.tzinfo is not None and ended.tzinfo is not None and started<=ended<=begin,
                "inbox preflight clock is outside its preparation interval")
        moment_ns(old["."]["mtime_ns"],started,ended,path=home,filesystem_clock=inbox_preflight["filesystem_clock"])
        root_before,root_after=copy.deepcopy(inbox_preflight["root_before"]),copy.deepcopy(old["."])
        require(root_before.pop("mtime_ns")!=root_after.pop("mtime_ns") and root_before==root_after,
                "inbox preflight changed its root kind or mode")
        plain=copy.deepcopy({p:row for p,row in old.items()
            if p!=".inbox-prefix" and not p.startswith(".inbox-prefix/")})
        plain["."].pop("mtime_ns")
        require(plain==inbox_preflight["noncache"],"inbox preflight noncache tree differs")
    cache_projection=None
    semantic=normalized["delta"]
    from _bus_scale_fixture_proofs.bus_scale_prefix_effects import CACHE,SUMMARY,project_prefix_tree
    if SUMMARY in semantic:
        require(type(prefix_proof) is dict,"teardown cache tree lacks its complete raw-byte proof")
        require(semantic[SUMMARY]==prefix_proof["semantic"],"teardown cache semantic proof differs")
        cache_projection=project_prefix_tree(before,after,prefix_proof,begin,end,filesystem_clock=filesystem_clock)
        old={path:row for path,row in old.items() if path!=CACHE and not path.startswith(CACHE+"/")}
        new={path:row for path,row in new.items() if path!=CACHE and not path.startswith(CACHE+"/")}
        semantic={path:row for path,row in semantic.items() if path!=SUMMARY}
    inbox_projection=None
    from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import CACHE as INBOX_CACHE, SUMMARY as INBOX_SUMMARY, project_inbox_prefix_tree
    if INBOX_SUMMARY in semantic:
        require(type(inbox_append_proof) is dict and semantic[INBOX_SUMMARY]==inbox_append_proof["semantic"],
                "teardown inbox tree lacks its complete append proof")
        inbox_projection=project_inbox_prefix_tree(before,after,inbox_append_proof,begin,end,filesystem_clock=filesystem_clock)
        old={path:row for path,row in old.items() if path!=INBOX_CACHE and not path.startswith(INBOX_CACHE+"/")}
        new={path:row for path,row in new.items() if path!=INBOX_CACHE and not path.startswith(INBOX_CACHE+"/")}
        semantic={path:row for path,row in semantic.items() if path!=INBOX_SUMMARY}
    cut_projection=None
    from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import CACHE as CUT_CACHE, SUMMARY as CUT_SUMMARY, project_cut_prefix_tree
    if CUT_SUMMARY in semantic:
        require(type(cut_proof) is dict and semantic[CUT_SUMMARY]==cut_proof["semantic"],
                "teardown cut tree lacks its complete append proof")
        cut_projection=project_cut_prefix_tree(before,after,cut_proof,begin,end,filesystem_clock=filesystem_clock)
        old={path:row for path,row in old.items() if path!=CUT_CACHE and not path.startswith(CUT_CACHE+"/")}
        new={path:row for path,row in new.items() if path!=CUT_CACHE and not path.startswith(CUT_CACHE+"/")}
        semantic={path:row for path,row in semantic.items() if path!=CUT_SUMMARY}
    registry_projection=None
    from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import CACHE as REGISTRY_CACHE, SUMMARY as REGISTRY_SUMMARY
    if any(path==REGISTRY_CACHE or path.startswith(REGISTRY_CACHE+"/") for path in set(old)|set(new)):
        require(REGISTRY_SUMMARY not in semantic,"retained registry projection cannot admit a native cache mutation")
        registry_projection=retained_registry_tree(home,before,after,before_raw,after_raw,registry_input)
        old={path:row for path,row in old.items() if path!=REGISTRY_CACHE and not path.startswith(REGISTRY_CACHE+"/")}
        new={path:row for path,row in new.items() if path!=REGISTRY_CACHE and not path.startswith(REGISTRY_CACHE+"/")}
    mail_projection=retained_mail_trees(home,before,after,before_raw,after_raw,mail_certificates or {})
    for name in mail_projection:
        old={path:row for path,row in old.items() if path!=name and not path.startswith(name+"/")}
        new={path:row for path,row in new.items() if path!=name and not path.startswith(name+"/")}
    require(set(old)<=set(new),"teardown dropped a retained file or directory")
    require(set(semantic)<=set(new),"teardown semantic delta names no actual tree entry")
    added_files={path for path in set(new)-set(old) if new[path]["kind"]=="file"}
    require(added_files<=set(semantic),"teardown added an unreviewed native file")
    permitted_dirs={"."}
    for path in semantic:
        parent=Path(path).parent
        while parent.as_posix()!=".":
            permitted_dirs.add(parent.as_posix())
            parent=parent.parent
    added_dirs={path for path in set(new)-set(old) if new[path]["kind"]=="directory"}
    require(added_dirs==permitted_dirs-set(old)-{"."},"teardown added an unreviewed empty directory or omitted a native ancestor")
    default_directory=old["registry"]["mode"]
    default_lock=old["registry/entries.jsonl.lock"]["mode"]
    teardown_helper=load_entry(ENTRY).routine_module("seat-teardown")
    invocation_node="state/waiter-invocations/"+teardown_helper.ACTOR
    invocation_lock=invocation_node+"/"+hashlib.sha256(teardown_helper.SESSION.encode()).hexdigest()+".lock"
    hint_touches=retained_hint_touches(home,before,after,before_raw,after_raw,begin,end,hint_input,
                                     ledger_kinds=teardown_helper.KINDS,filesystem_clock=filesystem_clock)
    result={"kind":"complete_native_teardown_bus_tree","entries":{}}
    for path,row in new.items():
        prior=old.get(path)
        if prior is not None:
            require(row["kind"]==prior["kind"] and row["mode"]==prior["mode"],
                    "teardown changed a retained entry kind or mode: "+path)
        value=copy.deepcopy(row)
        if row["kind"]=="directory":
            if path=="." and inbox_preflight is not None and prior==row:
                value["mtime_ns"]="<bounded-native-inbox-preflight-clock>"
            if prior is None:
                mode=0o700 if path in {"state/wake-control","state/wake-control/scale-worker",invocation_node} else default_directory
                require(row["mode"]==mode,"teardown generated a directory with an unreviewed mode: "+path)
            if prior is None or row["mtime_ns"]!=prior["mtime_ns"]:
                require(path in permitted_dirs,"teardown touched an unrelated directory: "+path)
                moment_ns(row["mtime_ns"],begin,end,path=home/path,filesystem_clock=filesystem_clock)
                value["mtime_ns"]="<bounded-native-directory-change>"
        elif path in semantic:
            expected_mode=default_lock if semantic[path]["operation"]=="create_lock" else 0o600
            if path==invocation_lock and semantic[path]["operation"]=="create_lock":
                expected_mode=0o600
            require(row["mode"]==expected_mode,"teardown generated a file with an unreviewed mode: "+path)
            moment_ns(row["mtime_ns"],begin,end,path=home/path,filesystem_clock=filesystem_clock)
            # Raw bytes/size/hash are retained in complete before/after tree
            # receipts; complete native semantic payload replaces no opaque
            # content in the equivalence projection.
            value={"kind":"file","mode":row["mode"],"mtime_ns":"<bounded-native-file-change>",
                   "before":copy.deepcopy(prior),"native_delta":copy.deepcopy(semantic[path])}
        elif path in hint_touches:
            value={"kind":"file","mode":row["mode"],"mtime_ns":"<bounded-native-hint-touch>",
                   "before":copy.deepcopy(prior),"native_delta":hint_touches[path]}
        else:
            require(prior==row,"teardown changed an unreviewed file or metadata: "+path)
        result["entries"][path]=value
    if cache_projection is not None:
        result["entries"][CACHE]=cache_projection
    if inbox_projection is not None:
        result["entries"][INBOX_CACHE]=inbox_projection
    if cut_projection is not None:
        result["entries"][CUT_CACHE]=cut_projection
    if registry_projection is not None:
        result["entries"][REGISTRY_CACHE]=registry_projection
    result["entries"].update(mail_projection)
    return result


def teardown_observation(owner,seed_pair,scale,workspace,place,entry):
    """Collect complete positive work or the one ruled native functional RED."""
    from tests.bus_scale_native_refusal import validate_functional_refusal,bind_functional_refusal
    case="seat-teardown"
    require(scale in {"1","10"} and type(seed_pair) is dict
            and {"context","prepared_home","preparation_bank","1","10","ledger_relative"}<=set(seed_pair),
            "teardown observation lacks its closed seed pair")
    workspace,place=ordinary(workspace,directory=True),ordinary(place,directory=True)
    disjoint_bank(case,seed_pair,workspace,place)
    require(not any(place.iterdir()),"teardown observation bank must be empty")
    native=load_entry(entry)
    helper=native.routine_module(case)
    context=copy.deepcopy(seed_pair["context"])
    require(context["case"]==case,"teardown context belongs to another case")
    home,seed=ordinary(seed_pair["prepared_home"],directory=True),ordinary(seed_pair[scale],directory=True)
    require(home!=seed and home not in seed.parents and seed not in home.parents,"teardown immutable seed overlaps restored root")
    seed_tree=tree(seed)
    primary=Path(seed_pair["ledger_relative"])
    require(not primary.is_absolute() and ".." not in primary.parts and primary.as_posix() in raw_files(seed),
            "teardown seed lacks its exact primary ledger")
    raw_primary=(seed/primary).read_bytes()
    refusal_cell=primary.as_posix()=="events.jsonl" and scale=="10" and raw_primary.count(b"\n")==10000
    # A 10k seed is not proof of the historical d66 refusal. Current product
    # versions can complete it; authenticate d66 only for an actual refusal.
    product=None
    source_paths=(Path(entry),Path(helper.__file__),Path(__file__))
    source_pins={str(path):digest(path.read_bytes()) for path in source_paths}
    entry_hash=source_pins[str(Path(entry))]
    baseline_path,manifest=baseline(case,seed_pair,workspace)
    context_path=home.parent/"case-context.json"
    context_raw=(canonical(context)+"\n").encode()
    if context_path.exists(): require(context_path.read_bytes()==context_raw,"stable teardown context changed")
    else: write_json(context_path,context)
    records={}
    for mode in ("cpu","diagnostic"):
        restore(seed_pair,seed,seed_tree,baseline_path,manifest,place/(mode+"-restore.json"))
        from floati.send_prefix import warm
        from floati.root import FloatiRoot
        require(warm(FloatiRoot.open_direct_home(home)) is not None,"teardown prefix preflight failed")
        from floati.cut_prefix import warm as warm_cut
        from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import capture_cut_prefix_input,verify_cut_prefix_effect,verify_warmed_cut_clone
        cut_seed=raw_files(home)
        require(warm_cut(FloatiRoot.open_direct_home(home)) is not None,"teardown cut preflight failed")
        verify_warmed_cut_clone(home,cut_seed)
        inbox_preflight=prepare_teardown_inbox(home)
        before_tree=tree(home)
        before=raw_files(home)
        hint_input=capture_retained_hint_input(home,before_tree,before)
        from _bus_scale_fixture_proofs.bus_scale_prefix_effects import capture_prefix_input
        prefix_input=capture_prefix_input(home,before)
        cut_input=capture_cut_prefix_input(home,before)
        from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import capture_inbox_prefix_append_input,verify_inbox_prefix_append_effect
        inbox_append_input=capture_inbox_prefix_append_input(home,before)
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import capture_registry_prefix_input
        registry_input=capture_registry_prefix_input(home,before)
        mail_certificates=capture_mail_certificates(home,before,
            cursor_preflights=getattr(owner,"cursor_preflights",()))
        output,error,detail_path=(home.parent/("expanded-"+mode+"-"+name)
                                  for name in ("stdout.txt","stderr.txt","detail.json"))
        require(not any(path.exists() or path.is_symlink() for path in (output,error,detail_path)),
                "teardown native output already exists")
        environment={key:value for key,value in os.environ.items()
                     if not key.startswith(("GIT_","FLOATI_","PYTHON","LD_","DYLD_"))}
        environment.update(PATH="/usr/bin:/bin",PYTHONNOUSERSITE="1",PYTHONDONTWRITEBYTECODE="1")
        usage_before=resource.getrusage(resource.RUSAGE_CHILDREN)
        wall_begin,filesystem_clock=capture_filesystem_clock()
        with output.open("xb") as out,error.open("xb") as err:
            child=owner.launch_owned_child(["/usr/bin/python3","-B",str(entry),mode,case,str(home),
                str(workspace),str(detail_path),"owned-census-v1"],cwd=workspace,env=environment,stdout=out,stderr=err)
            collected=owner.collect_owned_child(child,deadline_seconds=120,collection_seconds=10)
        wall_end=datetime.now(timezone.utc)
        usage_after=resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu=usage_after.ru_utime+usage_after.ru_stime-usage_before.ru_utime-usage_before.ru_stime
        collection={"collection":collected,"child_pid":child.pid,"wall_begin":wall_begin.isoformat(),
            "wall_end":wall_end.isoformat(),"cpu_seconds":cpu,"entry_sha256":entry_hash,
            "stdout_sha256":digest(output.read_bytes()),"stderr_sha256":digest(error.read_bytes()),"source_pins":source_pins}
        collection["inbox_preflight"]=inbox_preflight
        collection["filesystem_clock"]=filesystem_clock
        write_json(place/(mode+"-collection.json"),collection)
        if type(collected) is not dict or collected.get("child_collected") is not True:
            raise UncollectedChild("teardown child collection is unknown; preserve root and evidence")
        # Retain complete collected post-state BEFORE any proof can refuse.
        after_tree,after=tree(home),raw_files(home)
        bank=place/mode
        bank.mkdir(mode=0o700)
        copy_exact(home,bank/"scale-fleet",after_tree)
        for source,name in ((output,"stdout.txt"),(error,"stderr.txt"),(detail_path,"detail.json")):
            if source.exists():
                shutil.copy2(source,bank/name)
                require((bank/name).read_bytes()==source.read_bytes(),"teardown output archive differs")
        write_json(bank/"before-raw.json",{p:base64.b64encode(raw).decode() for p,raw in before.items()})
        write_json(bank/"before-tree.json",before_tree)
        write_json(bank/"after-tree.json",after_tree)
        write_json(bank/"hint-lineage-input.json",hint_input)
        auxiliaries=[]
        (bank/"auxiliary").mkdir(mode=0o700)
        for index,row in enumerate(manifest["roots"]):
            actual=Path(row["root"])
            state=tree(actual)
            copy_exact(actual,bank/"auxiliary"/str(index),state)
            auxiliaries.append({"root":str(actual),"inventory":state})
        collection.update(archived_root=after_tree,auxiliary_post_state=auxiliaries,
                          restore_sha256=digest((place/(mode+"-restore.json")).read_bytes()))
        try:
            require(collected.get("status")=="complete" and collected.get("interrupted") is False
                    and type(collected.get("exit_code")) is int and detail_path.is_file(),
                    "teardown native child is incomplete")
            require(type(cpu) in (int,float) and math.isfinite(cpu) and cpu>0,"teardown collected CPU is invalid")
            code=collected["exit_code"]
            detail=json.loads(detail_path.read_bytes())
            validate_detail(detail,case,mode,code,context)
            artifact=json.loads(output.read_bytes())
            delta=native_delta(before,after,home=home,prefix_input=prefix_input,inbox_append_input=inbox_append_input,cut_input=cut_input)
            write_json(bank/"native-delta.json",{"before_tree":before_tree,"after_tree":after_tree,"native_delta":delta})
            bounds=dict(home=home,workspace=workspace,before_raw_files=before,wall_begin=wall_begin,
                        wall_end=wall_end,child_pid=child.pid,child_cpu_seconds=cpu,prefix_input=prefix_input,
                        inbox_append_input=inbox_append_input,cut_input=cut_input)
            prefix_proof=None
            inbox_append_proof=None
            cut_proof=None
            if code==0:
                witness=helper.witness(case,home,before,artifact,context,detail)
                normalized=helper.normalize(case,delta,artifact,context,**bounds)
                from _bus_scale_fixture_proofs.bus_scale_prefix_effects import verify_prefix_effect
                prefix_proof=verify_prefix_effect(home,before,after,delta,prefix_input)
                write_json(bank/"prefix-effect-proof.json",prefix_proof)
                inbox_append_proof=verify_inbox_prefix_append_effect(home,before,after,delta,inbox_append_input)
                write_json(bank/"inbox-append-effect-proof.json",inbox_append_proof)
                cut_proof=verify_cut_prefix_effect(home,before,after,delta,cut_input)
                write_json(bank/"cut-effect-proof.json",cut_proof)
                collection["native_effect"]=witness
                collection["disposition"]="completed"
            elif code==20 and refusal_cell:
                current_product=teardown_product_proof()
                require(product is None or product==current_product,
                        "actual ruled product source changed between refusal modes")
                product=current_product
                witness=helper.refusal_witness(case,home,before,artifact,context,detail)
                normalized=helper.normalize_refusal(case,delta,artifact,context,**bounds)
                collection["native_refusal"]=witness
                collection["disposition"]="functional_red"
            else:
                raise UnresolvedObservation("teardown returned an unruled native code/artifact; raw retained")
            require(witness["exit_code"]==code and set(normalized)=={"artifact","delta","normalizations"},
                    "teardown witness or complete semantic projection differs")
            bus_proof=teardown_bus_tree(before_tree,after_tree,normalized,wall_begin,wall_end,prefix_proof,
                home=home,before_raw=before,after_raw=after,hint_input=hint_input,
                inbox_append_proof=inbox_append_proof,inbox_preflight=inbox_preflight,registry_input=registry_input,
                filesystem_clock=filesystem_clock,mail_certificates=mail_certificates,cut_proof=cut_proof)
            write_json(bank/"bus-tree-proof.json",{"status":"validated","proof":bus_proof})
            write_json(bank/"equivalence.json",normalized)
            auxiliary=auxiliary_proof(case,home,workspace,context,baseline_path,manifest,
                [row["inventory"] for row in auxiliaries],artifact,normalized,wall_begin,wall_end,
                filesystem_clock=filesystem_clock)
            write_json(bank/"auxiliary-proof.json",{"status":"validated","proof":auxiliary})
            require(tree(home)==after_tree and raw_files(home)==after,"teardown proof changed collected root")
            require(tree(seed)==seed_tree and all(digest(path.read_bytes())==source_pins[str(path)] for path in source_paths),
                    "teardown source or immutable seed changed")
            collection.update(detail_sha256=digest(detail_path.read_bytes()),native_delta=delta,equivalence=normalized,
                              bus_equivalence=bus_proof,auxiliary_equivalence=auxiliary)
            write_json(bank/"collection.json",collection)
        except Exception as exc:
            write_json(bank/"unresolved-proof.json",{"status":"unresolved","code":getattr(exc,"code",type(exc).__name__),
                "detail":str(exc),"post_state_archived":True,"before_tree":before_tree,"after_tree":after_tree})
            if not (bank/"collection.json").exists(): write_json(bank/"collection.json",collection)
            raise
        for source,name in ((output,"stdout.txt"),(error,"stderr.txt"),(detail_path,"detail.json")):
            require(tree(source)==tree(bank/name),"teardown output changed after archive/proof")
            source.unlink()
        records[mode]=collection
    require(records["cpu"]["disposition"]==records["diagnostic"]["disposition"],
            "native CPU/diagnostic teardown outcomes differ; no pair admitted")
    for field in ("bus_equivalence","auxiliary_equivalence"):
        require(canonical(records["cpu"][field])==canonical(records["diagnostic"][field]),
                "diagnostics changed complete teardown "+field)
    refused=records["cpu"]["disposition"]=="functional_red"
    if refused:
        require(refusal_cell and records["cpu"]["native_refusal"]==records["diagnostic"]["native_refusal"],
                "teardown refusal category, unchanged inputs or no-effect proof differs")
        require(teardown_product_proof()==product,"actual ruled product source changed during refusal observation")
    else:
        require(records["cpu"]["native_effect"]==records["diagnostic"]["native_effect"],"diagnostics changed completed teardown effect")
        for field in ("artifact","delta"):
            require(canonical(records["cpu"]["equivalence"][field])==canonical(records["diagnostic"]["equivalence"][field]),
                    "diagnostics changed complete native teardown "+field)
    restore(seed_pair,seed,seed_tree,baseline_path,manifest,place/"final-restore.json")
    receipt={**records,"cpu_boundary":"whole native entry child including bootstrap, imports, native command and cleanup",
        "restore_boundary":"outside native children; exact seed and complete auxiliary bytes/modes/mtimes",
        "final_restore_sha256":digest((place/"final-restore.json").read_bytes()),"product_proof":product}
    raw_sha=write_json(place/"collection.json",receipt)
    diagnostic=json.loads((place/"diagnostic/detail.json").read_bytes())
    common={"records":raw_primary.count(b"\n"),"bytes":len(raw_primary),"seed_sha256":digest(raw_primary),
            "clone_sha256":digest(raw_primary),"child_collected":True,"raw_sha256":raw_sha}
    metrics={"cpu_seconds":records["cpu"]["cpu_seconds"],"bytes_read":sum(diagnostic["read_bytes_by_path"].values()),
             "max_ledger_lock_seconds":diagnostic["hold_families"]["ledger"]["maximum_seconds"]}
    if refused:
        result={**common,"disposition":"functional_red","row":"F2-SEAT-TEARDOWN-SCALE-1",
            "native":{"exit_code":20,"artifact":records["cpu"]["equivalence"]["artifact"],
                      "diagnostic_artifact":records["diagnostic"]["equivalence"]["artifact"]},
            "available_metrics":metrics,"source":{"product_sha":product["product_sha"],"entry_sha256":entry_hash,
                "stdout_sha256":{mode:records[mode]["stdout_sha256"] for mode in ("cpu","diagnostic")}}}
        validate_functional_refusal(case,"events","10x",result)
        bind_functional_refusal(case,"events","10x",result,place,entry)
        return result
    return {**common,"native":records["cpu"]["native_effect"],"coverage":{"cpu":True,"bytes":True,"locks":True},"metrics":metrics}


def expanded_observation(owner,case,seed_pair,scale,workspace,place,entry):
    """Return the established arm schema after two fully archived native modes."""
    if case=="seat-teardown":
        return teardown_observation(owner,seed_pair,scale,workspace,place,entry)
    require(case not in OLD_CASES,"original nine cases retain their original observation implementation")
    require(scale in {"1","10"},"expanded scale must be an exact seed-pair key")
    require(type(seed_pair) is dict and {"context","prepared_home","preparation_bank","1","10","ledger_relative"}<=set(seed_pair),
            "expanded observation requires its prepared native seed pair")
    workspace=ordinary(workspace,directory=True)
    place=ordinary(place,directory=True)
    disjoint_bank(case,seed_pair,workspace,place)
    require(not any(place.iterdir()),"observation bank must be empty; restore receipts cannot preexist")
    time_policy=seed_pair.get("attention_time_policy","translated")
    require(time_policy in {"translated","native_preserved"},"unknown attention time policy")
    native_time=case in ATTENTION_CASES and time_policy=="native_preserved"
    require(time_policy!="native_preserved" or case in ATTENTION_CASES,
            "native time preservation is only qualified for the attention cases")
    require(not native_time or seed_pair["ledger_relative"]=="events.jsonl",
            "native attention time proof requires its exact event history")
    translated_clock=case in ATTENTION_CASES and not native_time
    if translated_clock:
        seed_pair=attention_clock_pair(case,seed_pair,place)
        disjoint_bank(case,seed_pair,workspace,place)
        clock_path=ordinary(seed_pair["clock_materialization"]["path"],directory=False)
        clock_tree=tree(clock_path)
        require(digest(clock_path.read_bytes())==seed_pair["clock_materialization"]["sha256"],
                "attention clock receipt does not bind its exact bytes")
    native=load_entry(entry)
    helper=native.routine_module(case)
    context=copy.deepcopy(seed_pair["context"])
    require(context["case"]==case,"native preparation context belongs to another case")
    home=ordinary(seed_pair["prepared_home"],directory=True)
    seed=ordinary(seed_pair[scale],directory=True)
    require(home!=seed and home not in seed.parents and seed not in home.parents,"immutable seed overlaps restored root")
    seed_tree=tree(seed)
    primary=Path(seed_pair["ledger_relative"])
    require(not primary.is_absolute() and ".." not in primary.parts and primary.as_posix() in raw_files(seed),
            "native seed lacks its exact primary ledger")
    raw_primary=(seed/primary).read_bytes()
    baseline_path,manifest=baseline(case,seed_pair,workspace)
    context_path=home.parent/"case-context.json"
    context_bytes=(canonical(context)+"\n").encode()
    if context_path.exists(): require(context_path.read_bytes()==context_bytes,"stable native context changed")
    else: write_json(context_path,context)
    entry_hash=digest(Path(entry).read_bytes())
    records={}
    for mode in ("cpu","diagnostic"):
        if translated_clock:
            require(tree(clock_path)==clock_tree,"attention clock receipt changed before native observation")
        restore_receipt=restore(seed_pair,seed,seed_tree,baseline_path,manifest,place/(mode+"-restore.json"))
        prefix_input=None
        cursor_input=None
        delivery_input=None
        cursor_preflight=None
        inbox_input=None
        inbox_preflight=None
        consumed_input=None
        inbox_append_input=None
        if case in {"consume","work-complete","inbox","node-drain","seat-board","wake-wait","node-prep-clear"}:
            from floati.inbox_prefix import warm as warm_inbox
            from floati.root import FloatiRoot
            authoritative={p.relative_to(home).as_posix():p.read_bytes() for p in home.rglob("*.jsonl")}
            require(warm_inbox(FloatiRoot.open_direct_home(home)) is not None,
                    "native inbox prefix preflight failed")
            require(authoritative=={p.relative_to(home).as_posix():p.read_bytes() for p in home.rglob("*.jsonl")},
                    "inbox preflight changed authoritative seed ledgers")
        if case in CURSOR_PREPARE_CASES:
            authoritative={p.relative_to(home).as_posix():p.read_bytes() for p in home.rglob("*.jsonl")}
            cursor_preflight=prepare_cursor_input(home,helper.ACTOR,
                cursor_preflights=getattr(owner,"cursor_preflights",()))
            require(cursor_preflight is not None,"native ACK prefix preflight failed")
            require(authoritative=={p.relative_to(home).as_posix():p.read_bytes() for p in home.rglob("*.jsonl")},
                    "ACK prefix preflight changed authoritative seed ledgers")
        if case=="node-prep-clear":
            from floati.send_prefix import warm
            from floati.root import FloatiRoot
            require(warm(FloatiRoot.open_direct_home(home)) is not None,"lifecycle prefix preflight failed")
        before=raw_files(home)
        from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import capture_cut_prefix_input
        cut_input=capture_cut_prefix_input(home,before)
        from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import capture_registry_prefix_input
        registry_input=capture_registry_prefix_input(home,before)
        if case in {"consume","work-complete","inbox","node-drain","seat-board","wake-wait"}:
            from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import capture_inbox_prefix_input,verify_inbox_prefix_effect
            inbox_input=capture_inbox_prefix_input(home,before)
            inbox_preflight=verify_inbox_prefix_effect(home,before,before,{},inbox_input)
        if case in {"consume","work-complete"}:
            from _bus_scale_fixture_proofs.bus_scale_consumed_prefix_effects import capture_consumed_prefix_input
            consumed_input=capture_consumed_prefix_input(home,before,node=helper.ACTOR)
        if case in {"ack","inbox","node-drain"}:
            from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import capture_cursor_prefix_input, capture_delivery_prefix_input
            cursor_input=capture_cursor_prefix_input(home,before,node=helper.ACTOR)
            if case in {"inbox","node-drain"}:
                delivery_input=capture_delivery_prefix_input(home,before,node=helper.ACTOR)
        if case=="wake-wait":
            from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import capture_delivery_prefix_input
            delivery_input=capture_delivery_prefix_input(home,before,node=helper.ACTOR)
        if case=="node-prep-clear":
            from _bus_scale_fixture_proofs.bus_scale_prefix_effects import capture_prefix_input
            prefix_input=capture_prefix_input(home,before)
            from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import capture_inbox_prefix_append_input
            inbox_append_input=capture_inbox_prefix_append_input(home,before)
        before_inventory=file_inventory(home)
        output,error,detail_path=(home.parent/("expanded-"+mode+"-"+name)
                                  for name in ("stdout.txt","stderr.txt","detail.json"))
        require(not any(path.exists() or path.is_symlink() for path in (output,error,detail_path)),
                "native mode output already exists")
        environment={key:value for key,value in os.environ.items()
                     if not key.startswith(("GIT_","FLOATI_","PYTHON","LD_","DYLD_"))}
        environment.update(PATH="/usr/bin:/bin",PYTHONNOUSERSITE="1",PYTHONDONTWRITEBYTECODE="1")
        usage_before=resource.getrusage(resource.RUSAGE_CHILDREN)
        wall_begin,filesystem_clock=capture_filesystem_clock()
        with output.open("xb") as stdout,error.open("xb") as stderr:
            child=owner.launch_owned_child(["/usr/bin/python3","-B",str(entry),mode,case,str(home),
                                           str(workspace),str(detail_path),"owned-census-v1"],
                                          cwd=workspace,env=environment,stdout=stdout,stderr=stderr)
            collected=owner.collect_owned_child(child,deadline_seconds=120,collection_seconds=10)
        wall_end=datetime.now(timezone.utc)
        usage_after=resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu=usage_after.ru_utime+usage_after.ru_stime-usage_before.ru_utime-usage_before.ru_stime
        collection={"collection":collected,"child_pid":child.pid,"wall_begin":wall_begin.isoformat(),
                    "wall_end":wall_end.isoformat(),"cpu_seconds":cpu,"entry_sha256":entry_hash,
                    "stdout_sha256":digest(output.read_bytes()),"stderr_sha256":digest(error.read_bytes()),
                    "filesystem_clock":filesystem_clock}
        if inbox_preflight is not None:
            collection["inbox_prefix_preflight"]={"outside_child_measurement":True,
                "complete_native_proof":inbox_preflight}
        if translated_clock:
            collection["clock_materialization"]=copy.deepcopy(seed_pair["clock_materialization"])
        write_json(place/(mode+"-collection.json"),collection)
        if type(collected) is not dict or collected.get("child_collected") is not True:
            raise UncollectedChild("owned expanded child collection is unknown; preserve stable root and evidence")
        require(collected.get("status")=="complete" and collected.get("interrupted") is False
                and type(collected.get("exit_code")) is int and collected["exit_code"]==0
                and detail_path.is_file(),"native expanded child is incomplete or refused")
        require(type(cpu) in (int,float) and math.isfinite(cpu) and cpu>0,"collected native child CPU is invalid")
        detail=json.loads(detail_path.read_bytes())
        validate_detail(detail,case,mode,collected["exit_code"],context)
        if case=="verify": collection["observer_scope"]=copy.deepcopy(detail["observer_scope"])
        artifact=json.loads(output.read_bytes())
        after=raw_files(home)
        after_inventory=file_inventory(home)
        require(all(before_inventory[p]["mode"]==after_inventory[p]["mode"] for p in set(before_inventory)&set(after_inventory)),
                "native operation changed a retained file mode")
        delta=native_delta(before,after,home=home,cursor_prefix_input=cursor_input,
            delivery_prefix_input=delivery_input,prefix_input=prefix_input,registry_prefix_input=registry_input,
            inbox_prefix_input=inbox_input,consumed_prefix_input=consumed_input,inbox_append_input=inbox_append_input,cut_input=cut_input)
        cursor_bounds={"cursor_prefix_input":cursor_input} if cursor_input is not None else {}
        cursor_bounds["registry_prefix_input"]=registry_input
        if "lifecycle" in Path(helper.__file__).name:
            cursor_bounds["cut_input"]=cut_input
        if inbox_input is not None:
            cursor_bounds["inbox_prefix_input"]=inbox_input
        if inbox_append_input is not None:
            cursor_bounds["inbox_append_input"]=inbox_append_input
        if consumed_input is not None:
            cursor_bounds["consumed_prefix_input"]=consumed_input
        if delivery_input is not None:
            cursor_bounds["delivery_prefix_input"]=delivery_input
        witness=helper.witness(case,home,before,artifact,context,detail,**cursor_bounds)
        require(type(witness) is dict and witness.get("exit_code")==collected["exit_code"],
                "native effect does not bind collected child status")
        if case in ATTENTION_CASES:
            # Generation follows the complete native source witness above;
            # hash syntax alone cannot authenticate this categorical map.
            collection["attention_categories"]=attention_categories(case,artifact)
        bounds=dict(wall_begin=wall_begin,wall_end=wall_end,child_pid=child.pid,home=home,
                    workspace=workspace,child_cpu_seconds=cpu,before_raw_files=before,
                    registry_prefix_input=registry_input)
        if any((path==".registry-prefix" or path.startswith(".registry-prefix/")) for path in delta):
            from _bus_scale_fixture_proofs.bus_scale_registry_prefix_effects import verify_registry_prefix_effect
            registry_proof=verify_registry_prefix_effect(home,before,after,delta,registry_input)
            write_json(place/(mode+"-registry-prefix-proof.json"),registry_proof)
            collection["registry_prefix_effect"]=registry_proof
        if "lifecycle" in Path(helper.__file__).name or case in {"verify","seat-teardown"}:
            bounds["before_raw_files"]=before
        if prefix_input is not None:
            bounds["prefix_input"]=prefix_input
        if "lifecycle" in Path(helper.__file__).name:
            bounds["cut_input"]=cut_input
        if cursor_input is not None:
            bounds.update(cursor_prefix_input=cursor_input,before_raw_files=before)
        if delivery_input is not None:
            bounds["delivery_prefix_input"]=delivery_input
        if inbox_input is not None:
            bounds["inbox_prefix_input"]=inbox_input
        if inbox_append_input is not None:
            bounds["inbox_append_input"]=inbox_append_input
        if consumed_input is not None:
            bounds["consumed_prefix_input"]=consumed_input
        normalized=helper.normalize(case,delta,artifact,context,**bounds)
        require(set(normalized)=={"artifact","delta","normalizations"},"native equivalence projection is incomplete")
        if native_time:
            # Full native source witnessing and own-clock normalization above
            # precede this proof; no event or context timestamp is translated.
            own=helper._projection_time(case,artifact["evidence"])
            require(isinstance(own,datetime) and own.tzinfo is not None
                    and wall_begin-timedelta(seconds=1)<=own<=wall_end,
                    "native attention own clock is outside its collected invocation")
            require(before.get("events.jsonl")==raw_primary==after.get("events.jsonl"),
                    "native attention observation changed its authored event times or bytes")
            collection["native_time_proof"]={
                "schema_version":1,"policy":"native_preserved","case":case,"scale":scale,
                "source_events_sha256":digest(raw_primary),"context_sha256":digest(context_bytes),
                "native_own_clock":own.isoformat(),"wall_begin":wall_begin.isoformat(),
                "wall_end":wall_end.isoformat(),"raw_event_times_preserved":True}
        require(raw_files(home)==after and file_inventory(home)==after_inventory,
                "native post-collection proof changed measured root bytes or modes")
        require(digest(Path(entry).read_bytes())==entry_hash and tree(seed)==seed_tree,
                "entry source or immutable seed changed during native observation")
        if translated_clock:
            require(tree(clock_path)==clock_tree,"attention clock receipt changed during native observation")
        bank=place/mode
        bank.mkdir(mode=0o700)
        archive_tree=tree(home)
        copy_exact(home,bank/"scale-fleet",archive_tree)
        for source,name in ((output,"stdout.txt"),(error,"stderr.txt"),(detail_path,"detail.json")):
            shutil.copy2(source,bank/name)
            require((bank/name).read_bytes()==source.read_bytes(),"native output archive differs")
        write_json(bank/"before-raw.json",{p:base64.b64encode(raw).decode() for p,raw in before.items()})
        write_json(bank/"native-delta.json",{"inventory_before":before_inventory,"inventory_after":after_inventory,"native_delta":delta})
        write_json(bank/"equivalence.json",normalized)
        from _bus_scale_fixture_proofs.bus_scale_cut_prefix_effects import CACHE as CUT_CACHE,verify_cut_prefix_effect
        if any(path==CUT_CACHE or path.startswith(CUT_CACHE+"/") for path in delta):
            cut_proof=verify_cut_prefix_effect(home,before,after,delta,cut_input)
            write_json(bank/"cut-effect-proof.json",cut_proof)
            collection["cut_prefix_effect"]=cut_proof
        if prefix_input is not None:
            from _bus_scale_fixture_proofs.bus_scale_prefix_effects import verify_prefix_effect
            write_json(bank/"prefix-effect-proof.json",verify_prefix_effect(home,before,after,delta,prefix_input))
        if inbox_input is not None:
            from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import verify_inbox_prefix_effect
            inbox_proof=verify_inbox_prefix_effect(home,before,after,delta,inbox_input)
            write_json(bank/"inbox-prefix-effect-proof.json",inbox_proof)
            collection["inbox_prefix_effect"]=inbox_proof
        if inbox_append_input is not None:
            from _bus_scale_fixture_proofs.bus_scale_inbox_prefix_effects import verify_inbox_prefix_append_effect
            inbox_append_proof=verify_inbox_prefix_append_effect(home,before,after,delta,inbox_append_input)
            write_json(bank/"inbox-append-effect-proof.json",inbox_append_proof)
            collection["inbox_append_effect"]=inbox_append_proof
        if consumed_input is not None:
            from _bus_scale_fixture_proofs.bus_scale_consumed_prefix_effects import verify_consumed_prefix_effect
            consumed_proof=verify_consumed_prefix_effect(home,before,after,delta,consumed_input,node=helper.ACTOR)
            write_json(bank/"consumed-prefix-effect-proof.json",consumed_proof)
            collection["consumed_prefix_effect"]=consumed_proof
        if cursor_input is not None:
            from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import verify_cursor_prefix_effect
            cursor_proof=verify_cursor_prefix_effect(home,before,after,delta,cursor_input,node=helper.ACTOR,
                delivery_input=delivery_input)
            write_json(bank/"cursor-prefix-effect-proof.json",cursor_proof)
            collection["cursor_prefix_preflight"]={"outside_child_measurement":True,"native_result":cursor_preflight}
            collection["cursor_prefix_effect"]=cursor_proof
        elif delivery_input is not None:
            from _bus_scale_fixture_proofs.bus_scale_cursor_prefix_effects import verify_delivery_prefix_effect
            delivery_proof=verify_delivery_prefix_effect(home,before,after,delta,delivery_input,node=helper.ACTOR)
            write_json(bank/"delivery-prefix-effect-proof.json",delivery_proof)
            collection["cursor_prefix_preflight"]={"outside_child_measurement":True,"native_result":cursor_preflight}
            collection["delivery_prefix_effect"]=delivery_proof
        auxiliary_bank=bank/"auxiliary"
        auxiliary_bank.mkdir(mode=0o700)
        auxiliaries=[]
        for index,row in enumerate(manifest["roots"]):
            actual=Path(row["root"])
            state=tree(actual)
            copy_exact(actual,auxiliary_bank/str(index),state)
            auxiliaries.append({"root":str(actual),"inventory":state})
        collection.update(detail_sha256=digest(detail_path.read_bytes()),inventory_before=before_inventory,
            inventory_after=after_inventory,native_delta=delta,native_effect=witness,
            equivalence=normalized,restore_sha256=digest((place/(mode+"-restore.json")).read_bytes()),
            archived_root=archive_tree,auxiliary_post_state=auxiliaries)
        write_json(bank/"collection.json",collection)
        require(tree(home)==archive_tree,"measured root changed before its archive completed")
        try:
            proof=auxiliary_proof(case,home,workspace,context,baseline_path,manifest,
                [row["inventory"] for row in auxiliaries],artifact,normalized,wall_begin,wall_end,
                filesystem_clock=filesystem_clock)
        except Exception as error:
            write_json(bank/"auxiliary-proof.json",{"status":"unresolved","code":getattr(error,"code",type(error).__name__),
                "detail":str(error),"post_state_archived":True})
            raise
        write_json(bank/"auxiliary-proof.json",{"status":"validated","proof":proof})
        collection["auxiliary_equivalence"]=proof
        for source,name in ((output,"stdout.txt"),(error,"stderr.txt"),(detail_path,"detail.json")):
            ordinary(source,directory=False)
            require(tree(source)==tree(bank/name),"owned output changed after its complete archive")
            source.unlink()
        records[mode]=collection
    attention_proof=None
    if case in ATTENTION_CASES:
        if records["cpu"]["attention_categories"]!=records["diagnostic"]["attention_categories"]:
            raise AttentionCategoryMismatch(case+" attention category mismatch between native CPU and diagnostic")
        attention_proof={"schema_version":1,"case":case,
                         "nodes":copy.deepcopy(records["cpu"]["attention_categories"]),
                         "sources":{mode:records[mode]["stdout_sha256"] for mode in ("cpu","diagnostic")}}
        validate_attention_proof(case,attention_proof)
    require(canonical(records["cpu"]["native_effect"])==canonical(records["diagnostic"]["native_effect"]),
            "diagnostics changed native selected outcome")
    for field in ("artifact","delta"):
        require(canonical(records["cpu"]["equivalence"][field])==canonical(records["diagnostic"]["equivalence"][field]),
                "diagnostics changed complete native "+field+" testimony")
    require(canonical(records["cpu"]["auxiliary_equivalence"])==canonical(records["diagnostic"]["auxiliary_equivalence"]),
            "diagnostics changed complete proved native auxiliary testimony")
    # All native/current-root validation is complete and archived above.
    # Releasing this serialized case restores external private Git/admin and
    # declared build payload state too; bus restoration alone is insufficient.
    restore(seed_pair,seed,seed_tree,baseline_path,manifest,place/"final-restore.json")
    receipt={**records,"cpu_boundary":"whole native entry child including bootstrap, imports, native command and cleanup",
             "restore_boundary":"outside every native child; exact seed and closed auxiliary bytes/modes/mtimes",
             "final_restore_sha256":digest((place/"final-restore.json").read_bytes())}
    if attention_proof is not None:
        receipt["attention_proof"]=copy.deepcopy(attention_proof)
        if translated_clock:
            require(tree(clock_path)==clock_tree,"attention clock receipt changed before final binding")
            receipt["clock_materialization"]=copy.deepcopy(seed_pair["clock_materialization"])
        else:
            receipt["attention_time_policy"]="native_preserved"
            receipt["native_time_proofs"]={mode:copy.deepcopy(records[mode]["native_time_proof"])
                                          for mode in ("cpu","diagnostic")}
    raw_sha=write_json(place/"collection.json",receipt)
    diagnostic=json.loads((place/"diagnostic/detail.json").read_bytes())
    arm={"records":raw_primary.count(b"\n"),"bytes":len(raw_primary),"seed_sha256":digest(raw_primary),
            "clone_sha256":digest(raw_primary),"child_collected":True,"raw_sha256":raw_sha,
            "native":records["cpu"]["native_effect"],"coverage":{"cpu":True,"bytes":True,"locks":True},
            "metrics":{"cpu_seconds":records["cpu"]["cpu_seconds"],
                       "bytes_read":sum(diagnostic["read_bytes_by_path"].values()),
                       "max_ledger_lock_seconds":diagnostic["hold_families"]["ledger"]["maximum_seconds"]}}
    if attention_proof is not None:
        arm["attention_proof"]=copy.deepcopy(attention_proof)
    return arm

"""Native lifecycle workload fixtures shared by default product-cache tests and the census suite."""
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


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tests/fixtures/bus_scale_census/bus-scale-routine-lifecycle-2026-10-15.py"
ENTRY = ROOT / "tests/fixtures/bus_scale_census/bus-scale-native-entry-2026-10-09.py"
CASES = frozenset([
    "reclaim",
    "lane-open",
    "lane-close",
    "wake-evaluate",
    "wake-record",
    "node-boot",
    "node-teardown",
    "node-explain",
    "node-prep-clear",
    "node-state-flush",
    "node-prompts",
    "seat-board",
    "wake-pause",
    "wake-resume",
    "wake-arm",
    "wake-wait",
    "context-reading-record",
    "wake-callback",
    "wake-daemon-serve"
])

# Every case retains two actual bounded starts. The explicit profile additionally
# retains fresh setup and its original native transport/population controls.
_GIT_TEMPLATE = None


def load(path, name):
    if not path.exists():
        return None
    return load_fixture_module(path, name)


def raw_delta(before, after):
    from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import raw_candidate_delta
    return raw_candidate_delta(before, after)


def _fresh_owned_git(workspace):
    """Author the original real reachable repository and its self-origin."""
    def git(*arguments):
        return subprocess.run(["/usr/bin/git",*arguments],cwd=workspace,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,check=True,timeout=30).stdout.strip()
    git("init","-b","main")
    git("config","user.name","Census Fixture")
    git("config","user.email","census@example.invalid")
    (workspace/"note.md").write_text("Owned native lifecycle fixture.\n",encoding="utf-8")
    git("add","--","note.md")
    git("-c","user.name=Census Fixture","-c","user.email=census@example.invalid",
        "-c","commit.gpgsign=false","commit","-m","Owned lifecycle fixture")
    sha=git("rev-parse","HEAD")
    git("remote","add","origin",str(workspace))
    git("update-ref","refs/remotes/origin/main",sha)
    git("config","branch.main.remote","origin")
    git("config","branch.main.merge","refs/heads/main")
    return sha


def owned_git(workspace):
    """Copy immutable genuinely authored Git bytes into an independent root.

    The copy has its own object/index/ref inodes. Only its origin coordinate is
    rebound, through Git itself; the template is never a linked worktree or a
    borrowed object directory. Vector 5 retains the original fresh authoring.
    """
    global _GIT_TEMPLATE
    if full_scale_enabled():
        return _fresh_owned_git(workspace)
    if any(workspace.iterdir()):
        raise AssertionError("owned lifecycle Git fixture needs an empty workspace")
    if _GIT_TEMPLATE is None:
        temporary=tempfile.TemporaryDirectory(prefix="bus-scale-lifecycle-git-template-",dir=REAL_TEMP_ROOT)
        template=Path(temporary.name).resolve()/"workspace"
        template.mkdir(mode=0o700)
        try:
            sha=_fresh_owned_git(template)
            entries=tuple((path.relative_to(template).as_posix(),path.is_dir(),
                           stat.S_IMODE(path.stat().st_mode),path.stat().st_mtime_ns,
                           None if path.is_dir() else path.read_bytes())
                          for path in sorted(template.rglob("*")))
        except BaseException:
            temporary.cleanup()
            raise
        temporary.cleanup()
        _GIT_TEMPLATE=(sha,entries)
    sha,entries=_GIT_TEMPLATE
    for relative,directory,mode,_mtime_ns,raw in entries:
        path=workspace/relative
        if directory:
            path.mkdir(mode=mode,parents=True,exist_ok=True)
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(raw)
            path.chmod(mode)
    for relative,_directory,_mode,mtime_ns,_raw in reversed(entries):
        path=workspace/relative
        os.utime(path,ns=(path.stat().st_atime_ns,mtime_ns))
    subprocess.run(["/usr/bin/git","remote","set-url","origin",str(workspace)],cwd=workspace,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,check=True,timeout=30)
    return sha


@contextmanager
def fixture(case):
    with tempfile.TemporaryDirectory(prefix="bus-scale-census-owned-lifecycle-",dir=REAL_TEMP_ROOT) as temporary:
        bank=Path(temporary).resolve()
        workspace=bank/"workspace"
        workspace.mkdir(mode=0o700)
        owned_git(workspace)
        if case=="wake-callback":
            root=FloatiRoot.open(bank/"callback-root","scale-fleet")
            home=root.tenant_home
            home.chmod(0o700)
        else:
            home=bank/"scale-fleet"
            home.mkdir(mode=0o700)
            root=FloatiRoot.open_direct_home(home)
        for node in ("scale-worker","architect"): Registry(root).register(node,"codex")
        yield bank,home,workspace


def execute(test, helper, case, home, workspace, context, *, entry=None, observation=None):
    begin,filesystem_clock=capture_filesystem_clock()
    argv=(entry.arguments(case,home,workspace,context) if entry is not None else
          helper.arguments(case,home,workspace,context))
    prefix_input=None
    delivery_input=None
    inbox_input=None
    inbox_append_input=None
    if case=="node-prep-clear":
        from floati.send_prefix import warm
        from floati.inbox_prefix import warm as warm_inbox
        from tests.fixtures.bus_scale_census.bus_scale_prefix_effects import capture_prefix_input
        warm(FloatiRoot.open_direct_home(home))
        test.assertIsNotNone(warm_inbox(FloatiRoot.open_direct_home(home)))
    if case=="wake-wait":
        from floati.cursor_prefix import warm
        from tests.fixtures.bus_scale_census.bus_scale_cursor_prefix_effects import capture_delivery_prefix_input
        ledgers={p.relative_to(home).as_posix():p.read_bytes() for p in home.rglob("*.jsonl")}
        test.assertIsNotNone(warm(FloatiRoot.open_direct_home(home),helper.ACTOR))
        test.assertEqual(ledgers,{p.relative_to(home).as_posix():p.read_bytes() for p in home.rglob("*.jsonl")})
    before=helper.snapshot(home)
    from tests.fixtures.bus_scale_census.bus_scale_cut_prefix_effects import CACHE as CUT_CACHE, capture_cut_prefix_input
    cut_input = capture_cut_prefix_input(home, before)
    from tests.fixtures.bus_scale_census.bus_scale_registry_prefix_effects import capture_registry_prefix_input
    registry_input=capture_registry_prefix_input(home,before)
    if case in {"seat-board","wake-wait"}:
        from tests.fixtures.bus_scale_census.bus_scale_inbox_prefix_effects import capture_inbox_prefix_input
        inbox_input=capture_inbox_prefix_input(home,before)
    if case=="node-prep-clear":
        prefix_input=capture_prefix_input(home,before)
        from tests.fixtures.bus_scale_census.bus_scale_inbox_prefix_effects import capture_inbox_prefix_append_input
        inbox_append_input=capture_inbox_prefix_append_input(home,before)
    if case=="wake-wait":
        delivery_input=capture_delivery_prefix_input(home,before,node=helper.ACTOR)
    output=io.StringIO()
    previous=Path.cwd()
    cpu_begin=time.process_time()
    child_begin=resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        os.chdir(workspace)
        if argv is None:
            code,artifact=helper.execute_seam(case,home,workspace,context)
        else:
            payload=io.StringIO(json.dumps(context.get("stdin",{})))
            with redirect_stdout(output),patch.object(sys,"stdin",payload),bounded_cli_parser():
                code=cli.main(argv)
            artifact=json.loads(output.getvalue())
    finally:
        os.chdir(previous)
    child_end=resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu=time.process_time()-cpu_begin+child_end.ru_utime+child_end.ru_stime-child_begin.ru_utime-child_begin.ru_stime
    end=datetime.now(timezone.utc)
    test.assertEqual(0,code,artifact)
    witnessed=helper.witness(case,home,before,artifact,context,{},
        registry_prefix_input=registry_input,
        inbox_prefix_input=inbox_input,
        inbox_append_input=inbox_append_input,
        cut_input=cut_input,
        **({"delivery_prefix_input":delivery_input} if delivery_input is not None else {}))
    test.assertEqual((0,"ok",1),(witnessed["exit_code"],witnessed["status"],witnessed["effect"]["count"]))
    after=helper.snapshot(home)
    delta=raw_delta(before,after)
    bounds=dict(wall_begin=begin,wall_end=end,child_pid=os.getpid(),home=home,
                workspace=workspace,child_cpu_seconds=cpu,before_raw_files=before,
                registry_prefix_input=registry_input,inbox_prefix_input=inbox_input)
    if prefix_input is not None: bounds["prefix_input"]=prefix_input
    if inbox_append_input is not None: bounds["inbox_append_input"]=inbox_append_input
    if delivery_input is not None: bounds["delivery_prefix_input"]=delivery_input
    if cut_input is not None: bounds["cut_input"]=cut_input
    normalized=helper.normalize(case,delta,artifact,context,**bounds)
    test.assertEqual({"artifact","delta","normalizations"},set(normalized))
    test.assertEqual(after,helper.snapshot(home),"native proof changed retained evidence")
    if observation is not None:
        observation.update(home=home,before=before,after=after,begin=begin,end=end,code=code,
            artifact=artifact,delta=delta,normal=normalized,bounds=bounds,witness=witnessed,argv=argv,
            filesystem_clock=filesystem_clock)
    return normalized,before,delta,artifact,bounds


def _fixture_identity(base):
    if not base.exists():
        return None
    return tuple((path.relative_to(base).as_posix(),info.st_dev,info.st_ino,info.st_mode,
                  info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
                 for path in (base,*sorted(base.rglob("*"))) for info in (path.lstat(),))

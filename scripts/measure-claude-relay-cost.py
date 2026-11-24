#!/usr/bin/env python3
"""Measure local Claude relay invocation cost on owned scratch fixtures only.

This explicit instrument is outside the default test suite. It measures the
relay Python process, including startup, rather than a live Claude seat's CPU.
No existing root/session is accepted and no provider or Stop waiter is launched.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import resource
import re
import signal
import statistics
import subprocess
import sys
import tempfile
import time

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from floati import fixture_ids  # noqa: E402
from floati.errors import IntegrityFailure, ProtocolRefusal  # noqa: E402
from floati.events import EventLog  # noqa: E402
from floati.registry import Registry  # noqa: E402
from floati.root import FloatiRoot  # noqa: E402
from floati.waiter_consent import arm_waiter_consent  # noqa: E402


class CostRefusal(ValueError):
    def __init__(self, detail, code="claude_relay_cost_unavailable"):
        super().__init__(detail)
        self.code = code


def run_child(argv, workspace, environment, payload=""):
    usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic_ns()
    child = subprocess.Popen(argv, cwd=workspace, env=environment, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             start_new_session=True)
    try:
        output, error = child.communicate(payload)
    except BaseException:
        # Only this instrument's retained child is signalled and reaped.
        if child.poll() is None:
            child.send_signal(signal.SIGINT)
        child.communicate()
        raise
    finished = time.monotonic_ns()
    usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    if child.returncode != 0:
        raise CostRefusal("owned relay child exited {0}".format(child.returncode))
    user = usage_after.ru_utime - usage_before.ru_utime
    system = usage_after.ru_stime - usage_before.ru_stime
    return {"wall_seconds": (finished - started) / 1e9, "cpu_seconds": user + system,
            "user_seconds": user, "system_seconds": system,
            "stdout_bytes": len(output.encode("utf-8")), "child_exit_code": child.returncode}, output, error


def invoke(root, workspace, node, session, event, environment, *, profile=False, entrypoint="launcher"):
    if entrypoint == "launcher":
        if profile:
            raise CostRefusal("import profiling requires --entrypoint module",
                              "claude_relay_cost_profile_requires_module")
        argv = [str(SOURCE / "scripts/floati")]
    else:
        argv = ["/usr/bin/python3", "-B"]
        if profile:
            argv.extend(["-X", "importtime"])
        argv.extend(["-m", "floati"])
    argv.extend(["hook", "relay", "--harness", "claude", "--root", str(root.path),
                 "--as", node, "--runtime", str(SOURCE)])
    payload = json.dumps({"cwd": str(workspace), "session_id": session,
                          "hook_event_name": event})
    return run_child(argv, workspace, environment, payload)


def import_profile(error):
    """Keep only Python's integer import timings and safe module identifiers."""
    rows = []
    pattern = re.compile(r"^import time:\s*(\d+)\s*\|\s*(\d+)\s*\|\s*([A-Za-z0-9_.]+)\s*$")
    for line in error.splitlines():
        match = pattern.fullmatch(line)
        if match:
            own, cumulative, module = match.groups()
            rows.append({"module": module, "self_wall_microseconds": int(own),
                         "cumulative_wall_microseconds": int(cumulative)})
    if not rows:
        raise CostRefusal("profiled child produced no import timing observations")
    return {"clock": "CPython importtime wall microseconds; not CPU time",
            "module_count": len(rows),
            "sum_self_wall_microseconds": sum(row["self_wall_microseconds"] for row in rows),
            "largest_self": sorted(rows, key=lambda row: row["self_wall_microseconds"], reverse=True)[:20],
            "largest_cumulative": sorted(rows, key=lambda row: row["cumulative_wall_microseconds"], reverse=True)[:20]}


def summarize(samples):
    return {"n": len(samples), "cpu_seconds_median": statistics.median(row["cpu_seconds"] for row in samples),
            "cpu_seconds_max": max(row["cpu_seconds"] for row in samples),
            "wall_seconds_median": statistics.median(row["wall_seconds"] for row in samples),
            "wall_seconds_max": max(row["wall_seconds"] for row in samples), "samples": samples}


def fixture(directory):
    directory.mkdir(mode=0o700)
    root = FloatiRoot.open_direct_home(directory / "fleet", create=True)
    workspace = directory / "workspace"
    workspace.mkdir(mode=0o700)
    node, sender = fixture_ids.builder("a"), fixture_ids.builder("b")
    Registry(root).register(node, "claude")
    Registry(root).register(sender, "claude")
    arm_waiter_consent(root.path, workspace, node, "claude", 10, 2)
    return root, workspace, node, sender


def measure(count, *, profile_imports=False, entrypoint="launcher"):
    if not 1 <= count <= 100:
        raise CostRefusal("sample count must be 1..100")
    if profile_imports and entrypoint != "module":
        raise CostRefusal("import profiling requires --entrypoint module",
                          "claude_relay_cost_profile_requires_module")
    with tempfile.TemporaryDirectory(prefix="floati-claude-relay-cost-", dir="\x2fprivate/tmp") as temporary:
        bank = Path(temporary).resolve()
        scratch_home = bank / "home"
        scratch_home.mkdir(mode=0o700)
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("FLOATI_", "CLAUDE_CODE_", "GIT_"))}
        environment.update(HOME=str(scratch_home), PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(SOURCE))
        prior_home = os.environ.get("HOME")
        os.environ["HOME"] = str(scratch_home)
        gated, full, baseline = [], [], []
        profile = None
        try:
            for index in range(count):
                directory = bank / ("sample-" + str(index))
                directory.mkdir(mode=0o700)
                baseline_sample, baseline_output, _ = run_child(
                    ["/usr/bin/python3", "-B", "-c", "pass"], directory, environment)
                if baseline_output:
                    raise CostRefusal("interpreter baseline unexpectedly produced stdout")
                baseline.append(baseline_sample)
                root, workspace, node, _ = fixture(directory / "empty")
                _, warm_output, _ = invoke(root, workspace, node, "fixture-empty", "SessionStart",
                                           environment, entrypoint=entrypoint)
                if warm_output:
                    raise CostRefusal("empty warmup unexpectedly presented context")
                sample, output, _ = invoke(root, workspace, node, "fixture-empty", "PostToolUse",
                                          environment, entrypoint=entrypoint)
                if output:
                    raise CostRefusal("unchanged gate unexpectedly presented context")
                gated.append(sample)
                if profile_imports and index == 0:
                    sample, output, error = invoke(root, workspace, node, "fixture-empty",
                                                   "PostToolUse", environment, profile=True,
                                                   entrypoint=entrypoint)
                    if output:
                        raise CostRefusal("profiled unchanged gate unexpectedly presented context")
                    profile = {"invocation": sample, **import_profile(error)}
                root, workspace, node, sender = fixture(directory / "pending")
                message = EventLog(root).send(sender, node, "floati", "a" * 40,
                    "docs/CONFLUENCE-v0.md", "owned fixture cost measurement",
                    idempotency_key="fixture-cost-message")
                # A separate pending-mail fixture has no acting predecessor
                # or watermark; no state is reset and no sleep is required.
                sample, output, _ = invoke(root, workspace, node, "fixture-full", "SessionStart",
                                          environment, entrypoint=entrypoint)
                try:
                    context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
                except (ValueError, KeyError, TypeError):
                    raise CostRefusal("full-read fixture did not emit hook context")
                if message["id"] not in context or len(context) > 10000:
                    raise CostRefusal("full-read context did not carry the bounded fixture envelope")
                full.append(sample)
        finally:
            if prior_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = prior_home
    return {"measurement_scope": "owned_scratch_relay_process_including_python_startup",
            "cpu_source": "RUSAGE_CHILDREN deltas around one awaited relay process",
            "wall_source": "monotonic_ns immediately before launch through child reaping",
            "entrypoint": entrypoint,
            "entrypoint_path": str(SOURCE / "scripts/floati") if entrypoint == "launcher" else "-m floati",
            "interpreter": "/usr/bin/python3 -I -S -B via launcher" if entrypoint == "launcher" else "/usr/bin/python3 -B",
            "baseline_interpreter": "/usr/bin/python3 -B -c pass",
            "fixture_root_basename": "fleet",
            "fixture_messages_per_full_read": 1, "scratch_home": True,
            "live_session_measurement": False, "idle_claude_cpu_measurement": False,
            "gated_cpu_target_seconds": 0.05,
            "gated_cpu_target_met_for_every_sample": all(row["cpu_seconds"] <= 0.05 for row in gated),
            "gated_noop": summarize(gated), "full_read": summarize(full),
            "interpreter_baseline": summarize(baseline), "import_profile": profile}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--entrypoint", choices=("launcher", "module"), default="launcher",
                        help="measure the installed launcher by default; module keeps the source comparison")
    parser.add_argument("--profile-imports", action="store_true",
                        help="module only: profile one extra unchanged gate outside ordinary samples")
    args = parser.parse_args(argv)
    try:
        artifact = {"artifact_version": 0, "command": "measure-claude-relay-cost",
                    "status": "ok", "evidence": measure(args.samples, profile_imports=args.profile_imports,
                                                         entrypoint=args.entrypoint)}
        code = 0
    except (CostRefusal, ProtocolRefusal, IntegrityFailure, OSError, ValueError) as exc:
        artifact = {"artifact_version": 0, "command": "measure-claude-relay-cost",
                    "status": "refused", "evidence": {
                        "code": getattr(exc, "code", "claude_relay_cost_unavailable"),
                        "detail": str(exc) if isinstance(exc, CostRefusal) else "fixture operation unavailable"}}
        code = 20
    print(json.dumps(artifact, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/python3
"""Measure one explicitly selected unittest worker, never discover the full suite.

The native ownership journal is unchanged. A separate offset journal attributes
new retained identities to synchronized test boundaries. Audit events identify
direct worker spawn callsites without recording arguments or environment values.
This is a bounded sample diagnostic, not runner qualification.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import traceback

from parallel_processes import ProcessTree, validate_ownership_receipt


ROOT = Path(__file__).resolve().parents[1]


def worker(args):
    import unittest
    sys.path.insert(0, str(ROOT))
    from parallel_unittest import _census, run_suite
    from parallel_plan import make_plan

    boundary = os.fdopen(args.events_fd, "w", buffering=1)
    acknowledgments = os.fdopen(args.ack_fd, "rb", buffering=0)
    current = [None]
    with (args.output / "spawn-calls.jsonl").open("x") as audit:
        def record_spawn(event, values):
            if event != "subprocess.Popen":
                return
            frames = []
            for frame in traceback.extract_stack():
                try:
                    relative = Path(frame.filename).relative_to(ROOT).as_posix()
                except ValueError:
                    continue
                if relative.startswith(("tests/", "floati/")):
                    frames.append({"file": relative, "line": frame.lineno, "function": frame.name})
            audit.write(json.dumps({"test_id": current[0],
                                    "executable": os.path.basename(os.fsdecode(values[0])),
                                    "callsites": frames}, sort_keys=True) + "\n")
            audit.flush()

        sys.addaudithook(record_spawn)

        def emit(event):
            if event["kind"] not in {"start", "stop"}:
                return
            if event["kind"] == "start":
                current[0] = event["test_id"]
            boundary.write(json.dumps(event, sort_keys=True) + "\n")
            if acknowledgments.read(1) != b"+":
                raise RuntimeError("measurement parent closed boundary channel")
            if event["kind"] == "stop":
                current[0] = None

        suite = unittest.defaultTestLoader.loadTestsFromNames(args.modules)
        census = _census(suite)
        # One explicit worker, in native order, including serial-only fixtures.
        for unit in census["units"]:
            unit["serial_reason"] = "single bounded measurement worker"
        assignment = make_plan(census, 1)["assignments"][0]
        result = run_suite(suite, assignment, census["population_digest"], sys.stderr, event_sink=emit)
        (args.output / "native-result.json").write_text(json.dumps(result, sort_keys=True) + "\n")
    boundary.close()
    acknowledgments.close()
    return 0 if result["successful"] else 1


def measure(args):
    args.output.mkdir(mode=0o700)
    tree = ProcessTree(args.output)
    current = [None]
    counts = Counter()
    peak_pending = 0
    offsets = (args.output / "retention-offsets.jsonl").open("x")
    process = None
    received, send_events = os.pipe()
    read_ack, acknowledge = os.pipe()
    interrupted = []

    def cancel(number, frame):
        interrupted.append(number)
        if process is not None and process.poll() is None:
            process.send_signal(number)

    prior = {number: signal.signal(number, cancel) for number in (signal.SIGINT, signal.SIGTERM)}
    began = time.monotonic()
    try:
        with (args.output / "worker.log").open("x") as log:
            original_retain = tree.retain

            def retain(identity, root, parent=None):
                nonlocal peak_pending
                before = len(tree.known)
                original_retain(identity, root, parent)
                peak_pending = max(peak_pending, len(tree.active))
                if len(tree.known) != before:
                    counts[current[0]] += 1
                    offsets.write(json.dumps({"journal_row": before, "test_id": current[0],
                                              "pending_identities": len(tree.active)}) + "\n")
                    offsets.flush()

            tree.retain = retain
            process = subprocess.Popen([
                "/usr/bin/python3", "-B", str(Path(__file__).resolve()),
                "--worker", "--events-fd", str(send_events), "--ack-fd", str(read_ack),
                "--output", str(args.output), *args.modules,
            ], cwd=ROOT, stdout=log, stderr=log, pass_fds=(send_events, read_ack), start_new_session=True)
            os.close(send_events)
            os.close(read_ack)
            tree.root(process, "sample-worker")
            with os.fdopen(received, "rb", buffering=0) as events:
                buffer = b""
                while process.poll() is None:
                    tree.observe()
                    if select.select([events], [], [], .02)[0]:
                        data = events.read(65536)
                        if not data:
                            process.wait()
                            break
                        buffer += data
                        while b"\n" in buffer:
                            raw, buffer = buffer.split(b"\n", 1)
                            event = json.loads(raw)
                            # Worker is blocked here: all work through stop is
                            # attributed before acknowledging the next boundary.
                            tree.observe()
                            current[0] = event["test_id"] if event["kind"] == "start" else None
                            os.write(acknowledge, b"+")
            process.wait()
    finally:
        os.close(acknowledge)
        if process is not None:
            if process.poll() is None:
                tree.signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    tree.signal(signal.SIGKILL)
                    process.wait(timeout=5)
            deadline = time.monotonic() + 2
            while tree.alive() and time.monotonic() < deadline:
                tree.observe()
                time.sleep(.02)
            if tree.alive():
                tree.signal(signal.SIGINT)
                time.sleep(.1)
                tree.signal(signal.SIGKILL)
                tree.alive()
        tree.journal.close()
        offsets.close()
        for number, handler in prior.items():
            signal.signal(number, handler)

    receipt = tree.receipt()
    (args.output / "ownership.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
    children = [{"key": "sample-worker", "pid": process.pid,
                 "returncode": process.returncode, "log": "worker.log"}]
    validate_ownership_receipt(receipt, children, sys.platform)
    result = json.loads((args.output / "native-result.json").read_text())
    module_counts = Counter()
    for test_id in result["selected_ids"]:
        module_counts[test_id.rsplit(".", 2)[0]] += counts[test_id]
    summary = {"scope": "one explicitly selected worker; not full-suite or cross-platform qualification",
               "platform": sys.platform, "modules": args.modules,
               "tests_run": result["tests_run"], "skip_count": sum(
                   row["kind"] in {"skip", "fixture_skip"} for row in result["events"]),
               "worker_exit_code": process.returncode, "child_collected": process.returncode is not None,
               "successful": result["successful"] and not tree.errors and not tree.active,
               "observer_errors": tree.errors, "wall_seconds": time.monotonic() - began,
               "retained": len(receipt["observed"]), "departed": len(receipt["departed"]),
               "peak_pending_identities": peak_pending,
               "unassigned_retained": counts[None],
               "per_test": {key: counts[key] for key in result["selected_ids"]},
               "module_ranking": module_counts.most_common(),
               "journal_sha256": hashlib.sha256((args.output / "processes.jsonl").read_bytes()).hexdigest(),
               "ownership_validator": "accepted", "cost": tree.cost(), "interrupted": interrupted,
               "interpreter": {"executable": sys.executable, "version": sys.version},
               "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                   for name in ["scripts/parallel_processes.py", "scripts/measure-observe-count.py",
                                *(name.replace(".", "/") + ".py" for name in args.modules)]}}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({key: value for key, value in summary.items()
                      if key not in {"per_test", "module_ranking", "cost"}}, sort_keys=True))
    return 0 if summary["successful"] and process.returncode == 0 else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--events-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--ack-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("modules", nargs="+")
    args = parser.parse_args()
    if not all(name.startswith("tests.test_") and name.count(".") == 1 for name in args.modules):
        parser.error("explicit test modules required; discovery is not supported")
    if not args.output.is_absolute() or args.output.resolve() != args.output:
        parser.error("output must be an absolute ordinary path")
    if not os.environ.get("HOME", "").startswith("\x2fprivate/tmp/hw-home."):
        parser.error("a fresh scratch HOME is required")
    return worker(args) if args.worker else measure(args)


if __name__ == "__main__":
    sys.exit(main())

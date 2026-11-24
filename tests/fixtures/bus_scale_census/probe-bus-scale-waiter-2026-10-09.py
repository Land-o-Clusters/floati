"""Initial native startup CPU RED; not the complete command/metric census."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
SELF = Path(__file__).resolve()
TEMP = Path("\x2fprivate/tmp") if sys.platform == "darwin" else Path(tempfile.gettempdir()).resolve()
SESSION = "bus-scale-native-baseline"


class UncollectedNativeChild(RuntimeError):
    uncollected_child = True


def native_child(root_path, workspace):
    if (root_path.name != "demo-fleet" or TEMP not in root_path.parents
            or not any(p.name.startswith("bus-scale-native-") for p in root_path.parents)):
        raise ValueError("only this probe's owned temporary fixture may run")
    sys.path.insert(0, str(ROOT))
    from floati import codex_wait
    from unittest import mock
    real_changed = codex_wait._WaiterInputsWatch.changed
    changes, sleeps = [], []
    clock = [0.0]
    idle_begin = [None]
    output, error = io.StringIO(), io.StringIO()
    began = time.process_time_ns()

    class ProbeComplete(BaseException):
        pass

    def changed(watcher):
        value = real_changed(watcher)
        changes.append(list(value))
        return value

    def sleep(seconds):
        now = time.process_time_ns()
        sleeps.append({"cpu_since_native_call_ns": now - began,
                       "changes": len(changes), "seconds": seconds})
        if changes and changes[-1] == [False, False]:
            if idle_begin[0] is not None:
                raise ProbeComplete()
            idle_begin[0] = now
        else:
            idle_begin[0] = None
        if len(sleeps) >= 6:
            raise RuntimeError("no pair of authoritative unchanged idle ticks")
        clock[0] += seconds

    try:
        with mock.patch.object(codex_wait._WaiterInputsWatch, "changed", changed):
            codex_wait.run_stop_waiter(bus_home=root_path,
                hook_payload={"cwd": str(workspace), "session_id": SESSION},
                stdout=output, stderr=error, monotonic=lambda: clock[0],
                sleep=sleep, poll_interval_seconds=0.1)
    except ProbeComplete:
        pass
    if idle_begin[0] is None or not sleeps or output.getvalue() or error.getvalue():
        raise RuntimeError("native idle phase not proven: " + error.getvalue() + output.getvalue())
    print(json.dumps({"phase": "native_waiter", "initial_reconciliation_cpu_ns":
                      sleeps[0]["cpu_since_native_call_ns"],
                      "one_unchanged_idle_tick_cpu_ns": sleeps[-1]["cpu_since_native_call_ns"]
                      - (idle_begin[0] - began),
                      "watch_changes": changes, "sleeps": sleeps,
                      "stdout_empty": True, "stderr_empty": True,
                      "boundary": "native run_stop_waiter; sleep ends after one proven unchanged tick"}, sort_keys=True))
    return 0


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--native-child":
        return native_child(Path(sys.argv[2]), Path(sys.argv[3]))
    if len(sys.argv) != 2 or sys.argv[1] != "run":
        raise ValueError("expected run")
    # This producer runs only as the closed runner's already-private child.
    private_home = Path(os.environ["HOME"])
    if TEMP not in private_home.parents or not private_home.name.startswith("suite-home-"):
        raise ValueError("the closed runner must install private HOME before startup")
    sys.path.insert(0, str(ROOT))
    from floati import fixture_ids
    from floati.codex_wait_contract import CodexWaitConsentLedger
    from floati.events import EventLog, validate_event_records
    from floati.framing import encode_frame
    from floati.ids import uuid7_hex
    from tests.test_codex_wait import CodexWaitRuntimeTests

    f = CodexWaitRuntimeTests()
    base = Path(tempfile.mkdtemp(prefix="bus-scale-native-", dir=TEMP))
    record = {"expected_source": "f971841e48b008903277b86d089892bbde42f963", "pairs": [],
              "measurement": "supplemental native waiter CPU RED, not full CLI or I/O/lock coverage"}
    active = None
    collected = True
    try:
        specification = importlib.util.spec_from_file_location(
            "bus_scale_owned_runner", ROOT / "tests/fixtures/bus_scale_census/run-bus-scale-census-2026-10-09.py")
        owner = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(owner)
        f.setUp()
        CodexWaitConsentLedger(f.root).arm(f.participant.binding,
            hook_timeout_seconds=300, wait_deadline_seconds=240,
            idempotency_key="bus-scale-native-budget")
        f.arm_session(SESSION)
        log = EventLog(f.root)
        log.send(fixture_ids.builder("floati"), "architect", "floati", "a" * 40,
                 "docs/evidence/ping.md", "unselected history", idempotency_key="scale-template")
        template = next(r for r in log.event_records() if r["kind"] == "message_envelope")
        frames, rows = [], []
        for index in range(10000):
            row = dict(template, id="msg-" + uuid7_hex(),
                       idempotency_key=f"scale-{index:06d}", note="")
            padding = 1024 - len(encode_frame(row))
            if not 0 <= padding <= 4096:
                raise ValueError("native envelope cannot fit fixed frame width")
            row["note"] = "x" * padding
            frame = encode_frame(row)
            if len(frame) != 1024:
                raise ValueError("frame width changed")
            rows.append(row)
            frames.append(frame)
        validate_event_records(rows)
        seeds = {}
        for scale, count in ((1, 1000), (10, 10000)):
            seed = base / ("seed-" + str(scale)) / "demo-fleet"
            shutil.copytree(f.bus_home, seed)
            raw = b"".join(frames[:count])
            (seed / "events.jsonl").write_bytes(raw)
            seeds[scale] = seed
            record["seed_" + str(scale)] = {"frames": count, "bytes": len(raw),
                                           "sha256": hashlib.sha256(raw).hexdigest()}
        record["prefix_exact"] = (seeds[10] / "events.jsonl").read_bytes().startswith(
            (seeds[1] / "events.jsonl").read_bytes())
        for pair in range(5):
            result = {"pair": pair, "order": [1, 10] if pair % 2 == 0 else [10, 1]}
            for scale in result["order"]:
                fixture = base / (f"pair-{pair}-{scale}") / "demo-fleet"
                shutil.copytree(seeds[scale], fixture)
                before = resource.getrusage(resource.RUSAGE_CHILDREN)
                out_path, err_path = fixture.parent / "stdout.json", fixture.parent / "stderr.txt"
                with out_path.open("xb") as out_stream, err_path.open("xb") as err_stream:
                    active = owner.launch_owned_child(
                        ["/usr/bin/python3", "-B", str(SELF), "--native-child",
                         str(fixture), str(f.workspace)], cwd=ROOT, env=dict(os.environ),
                        stdout=out_stream, stderr=err_stream)
                    collected = False
                    collection = owner.collect_owned_child(
                        active, deadline_seconds=120, collection_seconds=10)
                    collected = collection["child_collected"]
                if not collected:
                    record["uncollected_child"] = {"pid": active.pid, "fixture": str(fixture)}
                    raise UncollectedNativeChild("native child did not collect within its cancellation budget")
                stdout, stderr = out_path.read_bytes(), err_path.read_bytes()
                after = resource.getrusage(resource.RUSAGE_CHILDREN)
                observation = {"child_pid": active.pid, "child_collected": active.returncode is not None,
                               "exit_code": active.returncode, "stdout": stdout.decode(),
                               "stderr": stderr.decode(), "cpu_seconds":
                               after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime}
                result[str(scale)] = observation
                observation["collection"] = collection
                if collection["status"] != "complete" or active.returncode != 0 or stderr:
                    record["pairs"].append(result)
                    raise RuntimeError("native probe incomplete")
                observation["native"] = json.loads(stdout)
            record["pairs"].append(result)
        small = sum(p["1"]["cpu_seconds"] for p in record["pairs"])
        large = sum(p["10"]["cpu_seconds"] for p in record["pairs"])
        record.update(cpu_sum_1=small, cpu_sum_10=large, cpu_ratio=large / small,
                      native_cpu_scale_red=large > 1.5 * small,
                      status="complete", byte_and_lock_verdict="not_measured")
    except BaseException as exc:
        record.update(status="incomplete", error=type(exc).__name__ + ": " + str(exc))
        raise
    finally:
        if collected:
            f.doCleanups()
            shutil.rmtree(base)
        record["child_collected"] = collected
        record["scratch_removed"] = not base.exists()
        print(json.dumps(record, sort_keys=True), flush=True)
    return 1 if record["native_cpu_scale_red"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

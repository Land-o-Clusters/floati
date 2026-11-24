#!/usr/bin/env python3
"""Compare Claude hook/doorbell/Stop delivery from architect observations.

--dry-run opens no root and prints the recording contract and examples. The
architect supplies JSONL from one host monotonic clock to --observations. This
instrument does not send mail, change hooks, start a provider, inspect processes,
or infer that a hook flush means the model received its envelope.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import statistics
import sys


class LatencyRefusal(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def refuse(detail, code="claude_latency_evidence_invalid"):
    raise LatencyRefusal(code, detail)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    mode = result.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--observations", help="exact canonical JSONL file recorded by the architect")
    return result


def examples():
    rows = []
    for arm, observed in (("hook", 1_300_000_000), ("doorbell", 1_300_000_000),
                          ("stop", 1_800_000_000)):
        common = {"schema_version": 1, "run_id": "example-only", "arm": arm,
                  "session_id": "example-session-" + arm,
                  "clock_id": "example-host-monotonic-boot-id", "source_sha": "a" * 40}
        message = {"item_id": "example-message-" + arm,
                   "delivery_receipt_id": "example-delivery-" + arm}
        rows.extend([
            dict(common, kind="sent", item_id=message["item_id"], monotonic_ns=1_000_000_000),
            dict(common, **message, kind="presentation_flushed", monotonic_ns=1_100_000_000),
            dict(common, **message, kind="session_observed", monotonic_ns=observed,
                 basis="session_model_action"),
        ])
        if arm == "doorbell":
            rows.append(dict(common, kind="doorbell_sent", item_id=message["item_id"],
                             monotonic_ns=1_020_000_000))
        for phase, timestamp, user, system in (("start", 2_000_000_000, 1.0, 0.5),
                                                ("end", 12_000_000_000, 1.2, 0.6)):
            rows.append(dict(common, kind="idle_cpu", phase=phase, interval_id="idle-1",
                             monotonic_ns=timestamp, cpu_source="example-observer-receipt.json",
                             processes=[{"pid": 101, "identity": "example-process-start-id",
                                         "user_seconds": user, "system_seconds": system}]))
    return rows


def dry_run():
    return {
        "mode": "dry_run", "live_measurement_performed": False,
        "arms": ["hook", "doorbell", "stop"],
        "schema_version": 1,
        "recording_procedure": [
            "Architect selects explicit scratch sessions and records the same source commit for every arm.",
            "Compare hook or doorbell delivery with existing Stop delivery, or record all three arms.",
            "Timestamp sent at envelope publication using the observer host's monotonic_ns clock.",
            "For the doorbell arm, record doorbell_sent when the fixed mail-free socket line is sent; correlate the item only in observer evidence.",
            "Record presentation_flushed only after complete hook/Stop output; copy its delivery receipt ID.",
            "The doorbell arm needs that same presentation receipt; socket write completion or EOF proves no delivery.",
            "Record session_observed through a receiving-session model action naming that exact message and receipt.",
            "Run successful tool calls during the hook arm; record their cadence externally.",
            "For each idle arm, an external observer records cumulative user/system CPU before and after a declared idle interval.",
            "Include the seat and every waiter/helper in the CPU population; preserve process start identities and observer evidence.",
            "Use a single host/boot monotonic clock ID; do not mix clocks or subtract wall-clock timestamps.",
            "Analyze with --observations /absolute/canonical/observations.jsonl; capture the JSON artifact as evidence.",
        ],
        "boundaries": {
            "flush_is_model_receipt": False,
            "idle_state_and_cpu_population": "external observer evidence, not independently discovered",
            "cpu_source": "nonempty evidence coordinate for each cumulative CPU snapshot",
            "source_sha_and_clock_identity": "declared by the architect's observer",
            "explicit_ack": "seat action; this instrument does not acknowledge mail",
            "doorbell_confirmation": "correlated Floati presentation receipt, never socket EOF",
            "idle_wake_without_stop_waiter": "architect must observe waiter absence during the doorbell idle arm",
            "messaging_credentials": "never record socket or token fields",
        },
        "example_observations": examples(),
    }


def text_field(value):
    return (isinstance(value, str) and 0 < len(value) <= 1024
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def nonnegative(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def stats(values):
    ordered = sorted(values)
    return {"n": len(ordered), "median_seconds": statistics.median(ordered),
            "p95_seconds": ordered[math.ceil(0.95 * len(ordered)) - 1],
            "min_seconds": ordered[0], "max_seconds": ordered[-1]}


def validate_row(row):
    common = {"schema_version", "run_id", "arm", "session_id", "clock_id", "source_sha",
              "kind", "monotonic_ns"}
    extra = {
        "sent": {"item_id"},
        "doorbell_sent": {"item_id"},
        "presentation_flushed": {"item_id", "delivery_receipt_id"},
        "session_observed": {"item_id", "delivery_receipt_id", "basis"},
        "idle_cpu": {"interval_id", "phase", "cpu_source", "processes"},
    }
    if (not isinstance(row, dict) or not isinstance(row.get("kind"), str)
            or row["kind"] not in extra):
        refuse("each observation must name a supported recording event")
    if set(row) != common | extra[row["kind"]]:
        refuse("observation fields must match the recording contract; never include credentials")
    if type(row["schema_version"]) is not int or row["schema_version"] != 1:
        refuse("unsupported observation schema")
    for name in ("run_id", "session_id", "clock_id"):
        if not text_field(row[name]):
            refuse("run, session and clock coordinates must be explicit")
    if row["arm"] not in ("hook", "doorbell", "stop"):
        refuse("arm must be hook, doorbell or stop")
    if row["kind"] == "doorbell_sent" and row["arm"] != "doorbell":
        refuse("only the doorbell arm can record a socket doorbell")
    if not isinstance(row["source_sha"], str) or not re.fullmatch(r"[0-9a-f]{40}", row["source_sha"]):
        refuse("source_sha must be an exact commit")
    if type(row["monotonic_ns"]) is not int or not 0 <= row["monotonic_ns"] <= 2**63 - 1:
        refuse("timestamps must be bounded nonnegative integer monotonic nanoseconds")
    if row["kind"] == "idle_cpu":
        if (row["phase"] not in ("start", "end") or not text_field(row["interval_id"])
                or not text_field(row["cpu_source"])):
            refuse("idle CPU needs an interval, phase and external source evidence")
        processes = row["processes"]
        if not isinstance(processes, list) or not 1 <= len(processes) <= 64:
            refuse("idle CPU must name one to 64 observed processes")
        identities = set()
        for process in processes:
            if (not isinstance(process, dict)
                    or set(process) != {"pid", "identity", "user_seconds", "system_seconds"}
                    or type(process["pid"]) is not int or not 1 <= process["pid"] <= 2147483647
                    or not text_field(process["identity"])
                    or not nonnegative(process["user_seconds"])
                    or not nonnegative(process["system_seconds"])):
                refuse("CPU processes need exact identities and finite cumulative counters")
            identity = (process["pid"], process["identity"])
            if identity in identities:
                refuse("CPU process population contains duplicates")
            identities.add(identity)
    else:
        if not text_field(row["item_id"]):
            refuse("each delivery observation must name its message")
        if (row["kind"] in ("presentation_flushed", "session_observed")
                and not text_field(row["delivery_receipt_id"])):
            refuse("presentation and model observation must name their delivery receipt")
        if row["kind"] == "session_observed" and row["basis"] != "session_model_action":
            refuse("session observation must come from an explicit receiving-model action")


def analyze(rows):
    if not rows or len(rows) > 100000:
        refuse("supply a bounded, nonempty observation population")
    messages, idle, sessions = {}, {}, {}
    coordinates = None
    for row in rows:
        validate_row(row)
        coordinate = (row["run_id"], row["clock_id"], row["source_sha"])
        if coordinates is not None and coordinates != coordinate:
            refuse("comparison must use one run, host clock and source commit")
        coordinates = coordinate
        arm, session = row["arm"], row["session_id"]
        if arm in sessions and sessions[arm] != session:
            refuse("each arm must belong to one exact session")
        sessions[arm] = session
        if row["kind"] == "idle_cpu":
            target = idle.setdefault((arm, row["interval_id"]), {})
            slot = row["phase"]
        else:
            target = messages.setdefault((arm, row["item_id"]), {})
            slot = row["kind"]
        if slot in target:
            refuse("duplicate recording event")
        target[slot] = row
    if "stop" not in sessions or not ({"hook", "doorbell"} & sessions.keys()):
        refuse("compare at least one hook or doorbell arm against the Stop arm",
               "claude_latency_observation_incomplete")
    results = {arm: {"session_id": sessions[arm], "samples": [], "idle_intervals": []}
               for arm in ("hook", "doorbell", "stop") if arm in sessions}
    for (arm, item), events in messages.items():
        required = {"sent", "presentation_flushed", "session_observed"}
        if arm == "doorbell":
            required.add("doorbell_sent")
        if set(events) != required:
            refuse("every message needs its presentation receipt, model observation and any doorbell event",
                   "claude_latency_observation_incomplete")
        sent, flushed, observed = (events[key] for key in
                                   ("sent", "presentation_flushed", "session_observed"))
        if (not sent["monotonic_ns"] <= flushed["monotonic_ns"] <= observed["monotonic_ns"]
                or flushed["delivery_receipt_id"] != observed["delivery_receipt_id"]):
            refuse("delivery chronology or receipt correlation is inconsistent")
        sample = {
            "item_id": item, "delivery_receipt_id": observed["delivery_receipt_id"],
            "presentation_seconds": (flushed["monotonic_ns"] - sent["monotonic_ns"]) / 1e9,
            "session_seconds": (observed["monotonic_ns"] - sent["monotonic_ns"]) / 1e9,
        }
        if arm == "doorbell":
            bell = events["doorbell_sent"]
            if not sent["monotonic_ns"] <= bell["monotonic_ns"] <= flushed["monotonic_ns"]:
                refuse("doorbell must follow publication and precede its presentation receipt")
            sample["doorbell_presentation_seconds"] = (
                flushed["monotonic_ns"] - bell["monotonic_ns"]) / 1e9
        results[arm]["samples"].append(sample)
    for (arm, interval), phases in idle.items():
        if set(phases) != {"start", "end"}:
            refuse("each idle CPU interval needs both cumulative snapshots",
                   "claude_latency_observation_incomplete")
        start, end = phases["start"], phases["end"]
        before = {(p["pid"], p["identity"]): p for p in start["processes"]}
        after = {(p["pid"], p["identity"]): p for p in end["processes"]}
        wall = (end["monotonic_ns"] - start["monotonic_ns"]) / 1e9
        if before.keys() != after.keys() or wall <= 0:
            refuse("idle interval must retain the exact process population and advance its clock")
        cpu = 0.0
        for identity in before:
            for counter in ("user_seconds", "system_seconds"):
                delta = after[identity][counter] - before[identity][counter]
                if delta < 0:
                    refuse("cumulative CPU counters moved backwards")
                cpu += delta
        if not math.isfinite(cpu):
            refuse("CPU interval aggregate must remain finite")
        results[arm]["idle_intervals"].append({
            "interval_id": interval, "wall_seconds": wall, "cpu_seconds": cpu,
            "mean_cores": cpu / wall, "process_identities": [list(key) for key in sorted(before)],
            "cpu_sources": [start["cpu_source"], end["cpu_source"]],
        })
    for arm, result in results.items():
        if not result["samples"] or not result["idle_intervals"]:
            refuse("every compared arm needs complete delivery samples and idle CPU intervals",
                   "claude_latency_observation_incomplete")
        result["presentation_latency"] = stats([sample["presentation_seconds"] for sample in result["samples"]])
        result["session_latency"] = stats([sample["session_seconds"] for sample in result["samples"]])
        if arm == "doorbell":
            result["doorbell_presentation_latency"] = stats([
                sample["doorbell_presentation_seconds"] for sample in result["samples"]])
        cpu = sum(interval["cpu_seconds"] for interval in result["idle_intervals"])
        wall = sum(interval["wall_seconds"] for interval in result["idle_intervals"])
        if not math.isfinite(cpu) or not math.isfinite(wall):
            refuse("CPU population aggregate must remain finite")
        result["idle_cpu"] = {"cpu_seconds": cpu, "wall_seconds": wall, "mean_cores": cpu / wall}
    return {"mode": "observation_analysis", "measurement_basis": "architect_observations",
            "live_measurement_performed": False, "run_id": coordinates[0], "clock_id": coordinates[1],
            "source_sha": coordinates[2], "coordinates_basis": "observer_declared",
            "idle_population_basis": "external_observer_declared", "arms": results}


def read_observations(value):
    path = Path(value)
    if (not path.is_absolute() or path.is_symlink() or path.resolve() != path
            or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024):
        refuse("supply an existing canonical observation file no larger than 32 MiB")
    def invalid_constant(_):
        refuse("nonfinite JSON values are not observations")
    try:
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line, parse_constant=invalid_constant) for line in stream if line.strip()]
    except (json.JSONDecodeError, UnicodeError):
        refuse("observations must be valid UTF-8 JSONL")


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        evidence = dry_run() if args.dry_run else analyze(read_observations(args.observations))
        artifact = {"artifact_version": 0, "command": "measure-claude-hook-latency",
                    "status": "ok", "evidence": evidence}
        exit_code = 0
    except (LatencyRefusal, OSError, ValueError) as exc:
        artifact = {"artifact_version": 0, "command": "measure-claude-hook-latency",
                    "status": "refused", "evidence": {
                        "code": getattr(exc, "code", "claude_latency_evidence_unavailable"),
                        "detail": str(exc) if isinstance(exc, LatencyRefusal) else "observation file unavailable"}}
        exit_code = 20
    print(json.dumps(artifact, sort_keys=True, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

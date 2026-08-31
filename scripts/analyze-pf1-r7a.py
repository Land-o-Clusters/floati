#!/usr/bin/env python3
"""Reproduce the PF-R7a watcher-instance attribution from frozen JSONL."""

from __future__ import annotations

import argparse
import hashlib
import os
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


# The journal path is an INPUT, not a property of this file: an instrument that
# hardcodes one machine's path is not reproducible anywhere else, and the path
# carries an operator identity and a private fleet coordinate.  Pass --journal,
# or set FLOATI_WATCH_JOURNAL.  EXPECTED_SHA256 below is what actually pins the
# input, and it travels with the receipt.
_ENV_JOURNAL = os.environ.get("FLOATI_WATCH_JOURNAL")
DEFAULT_JOURNAL = Path(_ENV_JOURNAL) if _ENV_JOURNAL else None
EXPECTED_SHA256 = "fd9b95f606aea6cf11d6daf5b831cce68b322582d22cb5b5bca2ce6832a0a0bc"


def timestamp(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def pearson(left: list[float], right: list[float]) -> float:
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator


def tied_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][1] == ordered[start][1]:
            end += 1
        rank = (start + 1 + end) / 2
        for index, _ in ordered[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def slope(left: list[float], right: list[float]) -> float:
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    return sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right)) / sum(
        (x - left_mean) ** 2 for x in left
    )


def load(path: Path) -> tuple[bytes, list[dict[str, object]]]:
    if path is None:
        raise SystemExit(
            "usage: analyze-pf1-r7a.py <watcher-journal.jsonl>  "
            "(or set FLOATI_WATCH_JOURNAL)"
        )
    frozen = path.read_bytes()
    digest = hashlib.sha256(frozen).hexdigest()
    if digest != EXPECTED_SHA256:
        raise SystemExit(f"frozen digest mismatch: expected {EXPECTED_SHA256}, got {digest}")
    rows = [json.loads(line) for line in frozen.splitlines() if line]
    return frozen, rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("journal", nargs="?", type=Path, default=DEFAULT_JOURNAL)
    args = parser.parse_args()
    frozen, rows = load(args.journal)

    by_pid: dict[int, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        by_pid[int(row["pid"])].append(row)

    retained: list[tuple[int, int, float, float]] = []
    pid_raw_intervals: dict[int, tuple[float, float, int]] = {}
    for pid, pid_rows in by_pid.items():
        times = [timestamp(str(row["ts"])) for row in pid_rows]
        duration_minutes = (max(times) - min(times)) / 60
        instances = sum(row.get("event") == "init" for row in pid_rows)
        exit_empty = sum(row.get("event") == "exit_empty" for row in pid_rows)
        pid_raw_intervals[pid] = (min(times), max(times), instances)
        if duration_minutes >= 5:
            retained.append((instances, len(pid_rows), exit_empty, duration_minutes))

    instances = [float(row[0]) for row in retained]
    all_rates = [row[1] / row[3] for row in retained]
    empty_rates = [row[2] / row[3] for row in retained]
    print(f"path={args.journal}")
    print(f"sha256={hashlib.sha256(frozen).hexdigest()}")
    print(f"bytes={len(frozen)} rows={len(rows)} pids={len(by_pid)}")
    print(
        "process_rate "
        f"retained={len(retained)} rows={sum(row[1] for row in retained)} "
        f"median_instances={statistics.median(instances):g} "
        f"mean_instances={statistics.fmean(instances):.1f}"
    )
    print(
        "process_rate all "
        f"pearson={pearson(instances, all_rates):.4f} "
        f"spearman={pearson(tied_ranks(instances), tied_ranks(all_rates)):.4f}"
    )
    print(
        "process_rate exit_empty "
        f"pearson={pearson(instances, empty_rates):.4f} "
        f"spearman={pearson(tied_ranks(instances), tied_ranks(empty_rates)):.4f}"
    )

    pid_intervals = {
        pid: (
            math.floor(first / 60),
            math.floor(last / 60),
            instances,
        )
        for pid, (first, last, instances) in pid_raw_intervals.items()
    }
    per_minute_appends: Counter[int] = Counter()
    per_minute_empty: Counter[int] = Counter()
    for row in rows:
        minute = math.floor(timestamp(str(row["ts"])) / 60)
        per_minute_appends[minute] += 1
        if row.get("event") == "exit_empty":
            per_minute_empty[minute] += 1

    per_minute_instances: Counter[int] = Counter()
    for first, last, count in pid_intervals.values():
        for minute in range(first, last + 1):
            per_minute_instances[minute] += count

    for width in (1, 5, 15, 60):
        grouped: dict[int, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
        for minute, appends in per_minute_appends.items():
            bucket = minute // width
            grouped[bucket][0] += appends
            grouped[bucket][1] += per_minute_empty[minute]
        for minute, count in per_minute_instances.items():
            bucket = minute // width
            grouped[bucket][2] += count
        active = [values for values in grouped.values() if values[0] and values[2]]
        all_appends = [values[0] for values in active]
        empty_appends = [values[1] for values in active]
        instance_minutes = [values[2] for values in active]
        print(
            f"bucket={width}m active={len(active)} "
            f"instance_minutes_all_r={pearson(instance_minutes, all_appends):.4f} "
            f"instance_minutes_all_slope={slope(instance_minutes, all_appends):.4f} "
            f"instance_minutes_empty_r={pearson(instance_minutes, empty_appends):.4f} "
            f"instance_minutes_empty_slope={slope(instance_minutes, empty_appends):.4f}"
        )


if __name__ == "__main__":
    main()

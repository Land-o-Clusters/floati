#!/usr/bin/python3 -B
"""Measure physical epoch barriers during the existing twelve-writer torture.

Run with /usr/bin/python3 -B, a caller-owned scratch HOME, and
PYTHONDONTWRITEBYTECODE=1. No product deadline or test operation is changed.
Per-process summaries stay in memory until the process finishes; diagnostic
files are written only after the worker's test operation has returned.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import multiprocessing
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock


REPOSITORY = Path(__file__).resolve().parent.parent
TEST_NAME = (
    "tests.test_gauntlet_concurrency.ConcurrentWriterGauntletTests."
    "test_twelve_process_send_register_ack_and_claim_torture_has_no_double_effects"
)


def output_path(value: str) -> Path:
    selected = Path(value)
    if not selected.is_absolute():
        if ".." in selected.parts:
            raise ValueError("output must not traverse outside the repository")
        selected = REPOSITORY / selected
    resolved = selected.resolve(strict=False)
    temporary = Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp").resolve()
    if not any(root in resolved.parents for root in (REPOSITORY, temporary)):
        raise ValueError("output must be below the repository or platform temporary root")
    if selected != resolved or selected.is_symlink():
        raise ValueError("output must have a canonical path without symlinks")
    if resolved.exists():
        raise ValueError("output already exists")
    if not resolved.parent.is_dir():
        raise ValueError("output parent must already exist")
    return resolved


def validate_runtime() -> None:
    if not sys.dont_write_bytecode or os.environ.get("PYTHONDONTWRITEBYTECODE") != "1":
        raise ValueError("-B and PYTHONDONTWRITEBYTECODE=1 are required")
    home = Path(os.environ.get("HOME", ""))
    temporary = Path("\x2fprivate/tmp" if sys.platform == "darwin" else "\x2ftmp").resolve()
    if not home.is_absolute() or temporary not in home.resolve().parents:
        raise ValueError("HOME must be a scratch directory below the platform temporary root")
    info = home.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("scratch HOME must be an ordinary directory owned by this uid")


class EpochMeasurements:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.phase = "setup"
        self.pid = os.getpid()
        self.rows = {}
        self.held = {}

    def local(self) -> None:
        if self.pid != os.getpid():
            self.pid = os.getpid()
            self.rows = {}
            self.held = {}

    @staticmethod
    def scope() -> tuple[str, ...]:
        frames = []
        frame = sys._getframe(2)
        try:
            while frame is not None and len(frames) < 12:
                filename = Path(frame.f_code.co_filename)
                if filename.is_absolute() and REPOSITORY in filename.parents:
                    relative = filename.relative_to(REPOSITORY).as_posix()
                    if relative.startswith("floati/"):
                        frames.append(f"{relative}:{frame.f_code.co_name}:{frame.f_lineno}")
                frame = frame.f_back
        finally:
            del frame
        return tuple(frames)

    def row(self, mode: str, scope: tuple[str, ...]) -> dict:
        key = (self.phase, mode, scope)
        if key not in self.rows:
            self.rows[key] = {
                "phase": self.phase, "mode": mode, "scope": list(scope),
                "acquired": 0, "refusals": {}, "wait_seconds_total": 0.0,
                "wait_seconds_max": 0.0, "hold_seconds_total": 0.0,
                "hold_seconds_max": 0.0, "released": 0,
            }
        return self.rows[key]

    @staticmethod
    def wait(row: dict, duration: float) -> None:
        row["wait_seconds_total"] += duration
        row["wait_seconds_max"] = max(row["wait_seconds_max"], duration)

    def acquire(self, original, descriptor: int, *, exclusive: bool) -> None:
        self.local()
        row = self.row("EX" if exclusive else "SH", self.scope())
        started = time.monotonic()
        try:
            original(descriptor, exclusive=exclusive)
        except BaseException as failure:
            self.wait(row, time.monotonic() - started)
            code = getattr(failure, "code", type(failure).__name__)
            row["refusals"][code] = row["refusals"].get(code, 0) + 1
            raise
        acquired = time.monotonic()
        self.wait(row, acquired - started)
        row["acquired"] += 1
        self.held[descriptor] = (row, acquired)

    def flock(self, original, descriptor: int, operation: int):
        self.local()
        result = original(descriptor, operation)
        if operation == fcntl.LOCK_UN and descriptor in self.held:
            row, acquired = self.held.pop(descriptor)
            duration = time.monotonic() - acquired
            row["hold_seconds_total"] += duration
            row["hold_seconds_max"] = max(row["hold_seconds_max"], duration)
            row["released"] += 1
        return result

    def flush(self) -> None:
        self.local()
        payload = {"pid": self.pid, "rows": list(self.rows.values()),
                   "unreleased": len(self.held)}
        destination = self.directory / f"{self.pid}.json"
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True)


def aggregate(directory: Path) -> dict:
    grouped = {}
    process_count = 0
    unreleased = 0
    for path in sorted(directory.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        process_count += 1
        unreleased += payload["unreleased"]
        for row in payload["rows"]:
            key = (row["phase"], row["mode"], tuple(row["scope"]))
            if key not in grouped:
                grouped[key] = row
                continue
            total = grouped[key]
            for field in ("acquired", "released", "wait_seconds_total", "hold_seconds_total"):
                total[field] += row[field]
            for field in ("wait_seconds_max", "hold_seconds_max"):
                total[field] = max(total[field], row[field])
            for code, count in row["refusals"].items():
                total["refusals"][code] = total["refusals"].get(code, 0) + count
    return {"process_summaries": process_count, "unreleased": unreleased,
            "rows": sorted(grouped.values(), key=lambda row: (row["phase"], row["mode"], row["scope"]))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    validate_runtime()
    destination = output_path(args.output)
    sys.path.insert(0, str(REPOSITORY))
    from floati import bus_epoch
    from tests.test_gauntlet_concurrency import ConcurrentWriterGauntletTests

    original_acquire = bus_epoch._acquire_epoch_descriptor
    original_flock = fcntl.flock
    original_hammer = ConcurrentWriterGauntletTests.run_hammer
    baseline = {process.pid for process in multiprocessing.active_children()}
    with tempfile.TemporaryDirectory(prefix="epoch-measurement-") as temporary:
        measurements = EpochMeasurements(Path(temporary))

        def acquire(descriptor, *, exclusive):
            return measurements.acquire(original_acquire, descriptor, exclusive=exclusive)

        def flock(descriptor, operation):
            return measurements.flock(original_flock, descriptor, operation)

        def hammer(case, target, args_for_index, **kwargs):
            previous = measurements.phase
            measurements.phase = target.__name__

            def measured_target(*target_args):
                measurements.local()
                try:
                    return target(*target_args)
                finally:
                    measurements.flush()

            try:
                return original_hammer(case, measured_target, args_for_index, **kwargs)
            finally:
                measurements.phase = previous

        started = time.monotonic()
        interrupted = None
        result = None
        cleaned_children = []
        try:
            with mock.patch.object(bus_epoch, "_acquire_epoch_descriptor", acquire),\
                    mock.patch.object(fcntl, "flock", flock),\
                    mock.patch.object(ConcurrentWriterGauntletTests, "run_hammer", hammer):
                suite = unittest.defaultTestLoader.loadTestsFromName(TEST_NAME)
                result = unittest.TextTestRunner(verbosity=2).run(suite)
        except KeyboardInterrupt:
            interrupted = "KeyboardInterrupt"
        finally:
            for process in multiprocessing.active_children():
                if process.pid not in baseline:
                    cleaned_children.append(process.pid)
                    process.terminate()
                    process.join(5)
                    if process.is_alive():
                        process.kill()
                        process.join(5)
            measurements.flush()
        payload = {
            "test": TEST_NAME, "tests_run": 0 if result is None else result.testsRun,
            "successful": result is not None and result.wasSuccessful() and interrupted is None,
            "failures": [] if result is None else [text for _test, text in result.failures],
            "errors": [] if result is None else [text for _test, text in result.errors],
            "interrupted": interrupted, "cleaned_children": cleaned_children,
            "elapsed_seconds": time.monotonic() - started,
            "sys_executable": sys.executable,
            "epoch_timeout_seconds": bus_epoch.EPOCH_LOCK_TIMEOUT_SECONDS,
            "instrumentation": "physical acquisition and descriptor-specific unlock; no timeout overrides",
            "measurements": aggregate(Path(temporary)),
        }
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        print(json.dumps({"output": str(destination), "successful": payload["successful"],
                          "tests_run": payload["tests_run"]}, sort_keys=True))
        return 130 if interrupted else (0 if payload["successful"] else 1)


if __name__ == "__main__":
    raise SystemExit(main())

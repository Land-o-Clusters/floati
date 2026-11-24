"""Explicit nine-pair native send observation; excluded from discovery.

Run with scratch HOME through the suite slot. This retains all native effects
and uncensored timings, including per-pair host load. It does not change the
existing five-pair census acceptance contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

from tests import bus_scale_census as census
from tests.banked_sha_fixture import banked_send_repository
from tests.temp_roots import REAL_TEMP_ROOT


def _host():
    return {"utc": datetime.now(timezone.utc).isoformat(),
            "load_average": list(os.getloadavg()), "logical_cpus": os.cpu_count()}


def _collected_sample(seed, workspace, place, deadline):
    """Keep native sample metrics while using the existing sealed HOME wrapper."""
    native_collect = census._collect

    def collect(argv, child_place, **kwargs):
        if (argv[:3] != ["/usr/bin/python3", "-B", str(census.ENTRY)]
                or argv[4] != "send" or argv[-1] != "owned-census-v1"):
            raise RuntimeError("follow-up requested a different native command")
        cleanup = child_place / "native-home-cleanup.json"
        collected = native_collect(census._native_argv(argv[3:], cleanup), child_place, **kwargs)
        home = json.loads(cleanup.read_text())
        if home.get("home_removed") is not True or Path(home["native_home"]).exists():
            raise RuntimeError("native send child did not remove its pinned HOME")
        return dict(collected, home_cleanup=home, home_cleanup_receipt=str(cleanup))

    with patch.object(census, "_collect", side_effect=collect):
        return census._native_sample("send", seed, workspace, place, deadline)


def run_followup():
    bank = Path(tempfile.mkdtemp(prefix="bus-scale3-send-", dir=REAL_TEMP_ROOT)).resolve()
    scratch = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-send-followup-", dir=REAL_TEMP_ROOT)).resolve()
    started = time.monotonic()
    deadline = started + 1100
    report = {"status": "incomplete", "pairs": [], "host": [], "raw": [],
              "source_before": census._source_witness(), "scratch_removed": False,
              "observer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    print("BUS_SCALE3_SEND_BANK " + str(bank), flush=True)
    try:
        report["quiet_start"] = {"maximum_one_minute_load": 2 * (os.cpu_count() or 1),
                                 "maximum_wait_seconds": 900, "observations": []}
        quiet_deadline = time.monotonic() + 900
        while True:
            host = _host()
            report["quiet_start"]["observations"].append(host)
            census._write_json(bank / "result.json", report)
            print("BUS_SCALE3_SEND_QUIET " + json.dumps(host, sort_keys=True), flush=True)
            if host["load_average"][0] <= report["quiet_start"]["maximum_one_minute_load"]:
                break
            if time.monotonic() >= quiet_deadline:
                raise RuntimeError("quiet-start load budget exhausted before native measurement")
            time.sleep(30)
        deadline = time.monotonic() + 1100
        repository, _ = banked_send_repository(scratch)
        workspace = scratch / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        seeds = census.native_seeds(scratch, deadline=deadline)
        report["seed_ledgers"] = {scale: {
            p.relative_to(seed).as_posix(): {"bytes": p.stat().st_size,
                "frames": len(p.read_bytes().splitlines())}
            for p in seed.rglob("*.jsonl")} for scale, seed in seeds.items()}
        for index in range(9):
            pair = {"order": [1, 10] if index % 2 == 0 else [10, 1]}
            host = {"before": _host()}
            raw = {}
            for scale in pair["order"]:
                pair[str(scale)], raw[str(scale)] = _collected_sample(
                    seeds[str(scale)], workspace,
                    scratch / "measure" / str(index) / str(scale), deadline)
            host["after"] = _host()
            report["pairs"].append(pair)
            report["raw"].append(raw)
            report["host"].append(host)
            census._write_json(bank / "result.json", report)
            print("BUS_SCALE3_SEND_PAIR " + json.dumps({"index": index + 1,
                "host": host, "metrics": pair}, sort_keys=True), flush=True)
        report["metrics"] = {}
        for metric in census.METRICS:
            ratios = [p["10"][metric] / p["1"][metric]
                      if p["1"][metric] else (1.0 if not p["10"][metric] else None)
                      for p in report["pairs"]]
            if None in ratios:
                raise RuntimeError("zero-to-positive ratio remains unresolved")
            report["metrics"][metric] = {"pair_ratios": ratios,
                "median_ratio": statistics.median(ratios),
                "minimum_ratio": min(ratios), "maximum_ratio": max(ratios),
                "quartiles": statistics.quantiles(ratios, n=4, method="inclusive"),
                "passes": sum(r <= 1.5 for r in ratios), "threshold": 1.5}
        report["status"] = "observation_complete"
    except BaseException as error:
        report["error"] = {"type": type(error).__name__, "detail": str(error)}
        raise
    finally:
        # Native collectors reap children before returning; retain inputs on
        # unexpected failure rather than guessing whether a child still uses them.
        if report["status"] == "observation_complete":
            archive = bank / "raw.tar.gz"
            with tarfile.open(archive, "x:gz") as stream:
                stream.add(scratch, arcname="scratch")
            report["raw_archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
            shutil.rmtree(scratch)
            report["scratch_removed"] = not scratch.exists()
        else:
            report["retained_scratch"] = str(scratch)
        report["source_after"] = census._source_witness()
        report["source_unchanged"] = report["source_before"] == report["source_after"]
        report["wall_seconds"] = time.monotonic() - started
        census._write_json(bank / "result.json", report)
        print("BUS_SCALE3_SEND_RESULT " + str(bank / "result.json"), flush=True)
    if not report["source_unchanged"]:
        raise RuntimeError("source changed during follow-up")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([unittest.FunctionTestCase(run_followup,
        description="nine alternating native send pairs with host load")])

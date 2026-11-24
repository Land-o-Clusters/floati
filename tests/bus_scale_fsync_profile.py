"""Opt-in real-send fsync mechanism probe; never a five-pair scale verdict.

Invoke explicitly with system Python:
    python3 -B -m unittest -v tests.bus_scale_fsync_profile

Ordinary test discovery does not select this filename. Four owned native
children run: CPU and diagnostic at 1x and 10x, with genuine 32/320 acknowledged
history. Every fsync delegates the original call. Profiling overhead remains
inside collected child CPU and lock intervals; no metric is corrected for it.
Full private fixtures and raw testimony remain in the printed temporary bank.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SELF = Path(__file__).resolve()
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from tests import bus_scale_census as census
from tests.temp_roots import REAL_TEMP_ROOT


def _sha_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metadata(value):
    return {"dev": value.st_dev, "ino": value.st_ino, "mode": value.st_mode,
            "size": value.st_size}


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def _profile_child(entry_args, cleanup_path, profile_path):
    """Observe current-process Python fsync calls without replacing results."""
    if (len(entry_args) != 6 or entry_args[0] not in {"cpu", "diagnostic"}
            or entry_args[1] != "send" or entry_args[-1] != "owned-census-v1"):
        raise ValueError("fsync profiler requires the exact native send entry")
    fixture, output = Path(entry_args[2]), Path(entry_args[4])
    cleanup_path, profile_path = Path(cleanup_path), Path(profile_path)
    if (cleanup_path.parent != output.parent or profile_path.parent != output.parent
            or fixture.parent != output.parent or profile_path.name != "fsync.json"
            or cleanup_path.exists() or profile_path.exists()):
        raise ValueError("fsync sidecars must be absent siblings of the native detail")
    original_fsync = os.fsync
    calls = []
    spans = []
    native_exit = None
    native_exception = None

    def observe(descriptor):
        row = {"ordinal": len(calls)}
        try:
            row["fd_metadata"] = _metadata(os.fstat(descriptor))
        except OSError as error:
            # Metadata observation must not replace fsync's own exception.
            row["fd_metadata_error"] = type(error).__name__
        row["monotonic_before_ns"] = time.monotonic_ns()
        row["process_cpu_before_ns"] = time.process_time_ns()
        try:
            result = original_fsync(descriptor)
            row["outcome"] = "returned"
            return result
        except BaseException as error:
            row.update(outcome="raised", exception_type=type(error).__name__)
            raise
        finally:
            row["process_cpu_after_ns"] = time.process_time_ns()
            row["monotonic_after_ns"] = time.monotonic_ns()
            calls.append(row)

    from floati import send_prefix
    originals = [(send_prefix.Prefix, name, getattr(send_prefix.Prefix, name))
                 for name in ("appended", "publish", "_put", "_node")]
    originals.append((send_prefix, "_atomic", send_prefix._atomic))

    def timed(original, name):
        def invoke(*args, **kwargs):
            row = {"ordinal": len(spans), "operation": name,
                   "monotonic_before_ns": time.monotonic_ns(),
                   "process_cpu_before_ns": time.process_time_ns()}
            if name == "_atomic":
                path = Path(args[0])
                row["target"] = "proof_node" if path.parent.name == "nodes" else path.name
            spans.append(row)
            try:
                result = original(*args, **kwargs)
                row["outcome"] = "returned"
                return result
            except BaseException as error:
                row.update(outcome="raised", exception_type=type(error).__name__)
                raise
            finally:
                row["process_cpu_after_ns"] = time.process_time_ns()
                row["monotonic_after_ns"] = time.monotonic_ns()
        return invoke

    os.fsync = observe
    for owner, name, original in originals:
        setattr(owner, name, timed(original, name))
    try:
        census._native_child(entry_args, cleanup_path)
        native_exit = 0
    except SystemExit as error:
        # Re-raise the original native exit, including its exact code.
        native_exit = error.code
        raise
    except BaseException as error:
        native_exception = type(error).__name__
        raise
    finally:
        os.fsync = original_fsync
        for owner, name, original in originals:
            setattr(owner, name, original)
        # Native work, native IOProbe and pinned-HOME cleanup have completed.
        # A sidecar-write problem cannot replace the native result/exception;
        # the parent separately requires a complete readable profile receipt.
        try:
            _write(profile_path, {
                "schema_version": 1, "pid": os.getpid(), "mode": entry_args[0],
                "case": "send", "native_exit": native_exit,
                "native_exception_type": native_exception,
                "wrapper_sha256": _sha_file(SELF),
                "clock": "time.monotonic_ns; time.process_time_ns",
                "scope": "delegated os.fsync calls in this Python process only",
                "written_after_native_work_and_home_cleanup": True, "calls": calls,
                "cache_operation_spans": spans})
        except Exception as error:
            print("fsync profile sidecar unavailable: " + type(error).__name__, file=sys.stderr)


def _fixture_identities(fixture):
    """Map inode identities after collection using metadata, never payloads."""
    result = {}
    for path in [fixture, *fixture.rglob("*")]:
        info = path.lstat()
        if (path.is_symlink() or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or info.st_uid != os.getuid()):
            raise RuntimeError("profile fixture contains an unreviewed filesystem object")
        relative = "." if path == fixture else path.relative_to(fixture).as_posix()
        result.setdefault((info.st_dev, info.st_ino), []).append(relative)
    return result


def _mapped_calls(profile, fixture):
    identities = _fixture_identities(fixture)
    result = []
    for row in profile["calls"]:
        metadata = row.get("fd_metadata")
        paths = [] if metadata is None else identities.get((metadata["dev"], metadata["ino"]), [])
        if "events.jsonl" in paths:
            category = "authoritative_events"
        elif ".send-prefix/checkpoint.json" in paths:
            category = "prefix_checkpoint_file"
        elif ".send-prefix" in paths:
            category = "prefix_fixed_parent"
        elif ".send-prefix/nodes" in paths:
            category = "prefix_node_bank_directory"
        else:
            category = "other_fixture_path" if paths else "unmapped_identity"
        result.append(dict(row, fixture_paths=paths, category=category,
            wall_seconds=(row["monotonic_after_ns"] - row["monotonic_before_ns"]) / 1e9,
            process_cpu_seconds=(row["process_cpu_after_ns"] - row["process_cpu_before_ns"]) / 1e9))
    return result


def _lock_sync_overlap(detail, calls):
    """Retain ordinary native intervals and overlap their certainly-held core."""
    result = []
    for family, group in detail["hold_families"].items():
        for interval in group["intervals"]:
            begin = interval["acquire_call_after_ns"]
            end = interval["release_call_before_ns"]
            overlap = []
            for call in calls:
                duration = max(0, min(end, call["monotonic_after_ns"])
                               - max(begin, call["monotonic_before_ns"]))
                if duration:
                    overlap.append({"fsync_ordinal": call["ordinal"], "category": call["category"],
                                    "overlap_seconds": duration / 1e9})
            result.append({"family": family, "native_interval": interval,
                           "overlap_boundary": "acquire_call_after_ns..release_call_before_ns",
                           "fsync_overlaps": overlap})
    return result


def _child_summary(place, record):
    profile_path = place / "fsync.json"
    cleanup_path = place / "child-home.json"
    profile = json.loads(profile_path.read_text())
    cleanup = json.loads(cleanup_path.read_text())
    fixture = place / "scale-fleet"
    if (profile.get("schema_version") != 1 or profile.get("pid") != record["collection"]["pid"]
            or profile.get("mode") != place.name or profile.get("case") != "send"
            or profile.get("native_exit") != 0 or profile.get("native_exception_type") is not None
            or profile.get("wrapper_sha256") != _sha_file(SELF)
            or profile.get("written_after_native_work_and_home_cleanup") is not True):
        raise RuntimeError("fsync receipt does not bind the collected successful native child")
    removed_home = Path(cleanup["native_home"])
    if (cleanup.get("home_removed") is not True or removed_home.exists()
            or removed_home.parent != Path(REAL_TEMP_ROOT)
            or not removed_home.name.startswith("suite-home-")):
        raise RuntimeError("profile child omitted its exact pinned-HOME cleanup")
    calls = _mapped_calls(profile, fixture)
    totals = {}
    for call in calls:
        aggregate = totals.setdefault(call["category"], {"calls": 0, "wall_seconds": 0.0,
                                                       "process_cpu_seconds": 0.0})
        aggregate["calls"] += 1
        aggregate["wall_seconds"] += call["wall_seconds"]
        aggregate["process_cpu_seconds"] += call["process_cpu_seconds"]
    result = {"pid": profile["pid"], "child_collected": record["collection"]["child_collected"],
        "native_exit": profile["native_exit"], "home_cleanup": cleanup,
        "fsync_receipt": {"path": str(profile_path), "sha256": _sha_file(profile_path)},
        "calls": calls, "totals_by_category": totals}
    spans = []
    span_totals = {}
    for span in profile["cache_operation_spans"]:
        item = dict(span,
            wall_seconds=(span["monotonic_after_ns"] - span["monotonic_before_ns"]) / 1e9,
            process_cpu_seconds=(span["process_cpu_after_ns"] - span["process_cpu_before_ns"]) / 1e9)
        label = span["operation"] + (":" + span["target"] if "target" in span else "")
        total = span_totals.setdefault(label, {"calls": 0, "wall_seconds": 0.0, "process_cpu_seconds": 0.0})
        total["calls"] += 1
        total["wall_seconds"] += item["wall_seconds"]
        total["process_cpu_seconds"] += item["process_cpu_seconds"]
        spans.append(item)
    result.update(cache_operation_spans=spans, inclusive_cache_operation_totals=span_totals,
                  span_note="nested delegated call durations overlap; totals must not be summed across operations")
    if profile["mode"] == "diagnostic":
        result["ordinary_lock_intervals"] = _lock_sync_overlap(record["detail"], calls)
    return result


def run_profile():
    """Run native sample validation unchanged, retaining all private evidence."""
    from tests.bus_scale_test_profile import banked_send_repository
    bank = Path(tempfile.mkdtemp(prefix="bus-scale-census-owned-fsync-profile-", dir=REAL_TEMP_ROOT)).resolve()
    print("BUS_SCALE_FSYNC_PROFILE " + str(bank), flush=True)
    started = time.monotonic()
    deadline = started + 570
    source_before = census._source_witness()
    _write(bank / "source-before.json", source_before)
    summary = {"schema_version": 1, "status": "incomplete", "bank": str(bank),
        "purpose": "one 1x/10x mechanism probe, not a five-pair performance verdict",
        "history_items": {"1": 32, "10": 320}, "children_expected": 4,
        "child_budget_seconds": 70, "overall_budget_seconds": 570,
        "source_before": {"commit": source_before["commit"],
            "path": str(bank / "source-before.json"),
            "sha256": _sha_file(bank / "source-before.json")},
        "wrapper_sha256": _sha_file(SELF), "samples": {}, "children": {},
        "raw_artifacts_retained": True, "extra_fixture_settling_fsync_performed": False,
        "copied_history_durability": {
            "baseline": "unchanged census._native_sample: copytree, inventory equality, _warm_fixture",
            "static_observation": "copy/warm performs no explicit authoritative ledger fsync before the child",
            "disk_dirty_state": "not measured; no copied-prefix durability proof asserted",
            "profile_scope": "child Python os.fsync only; no preflight or subprocess syscall trace"},
        "instrumentation_limits": [
            "delegates real os.fsync; does not measure subprocess or native/C fsync syscalls",
            "profiler overhead remains in native collected CPU and ordinary lock intervals",
            "postcommand inode mapping may leave replaced/deleted identities unresolved",
            "one pair cannot classify reproducibility or the five-pair scale threshold"]}
    original_collect = census._collect
    launches = []

    def collect(argv, place, *, workspace, home, deadline):
        if (len(argv) != 9 or argv[:3] != ["/usr/bin/python3", "-B", str(census.ENTRY)]
                or argv[3] not in {"cpu", "diagnostic"} or argv[4] != "send"):
            raise RuntimeError("profile may substitute only an exact native send child argv")
        cleanup, sidecar = place / "child-home.json", place / "fsync.json"
        fixture = Path(argv[5])
        before = _metadata((fixture / "events.jsonl").stat())
        replacement = [argv[0], argv[1], str(SELF), "native-child", *argv[3:],
                       str(cleanup), str(sidecar)]
        launches.append({"mode": argv[3], "fixture": str(fixture),
                         "event_file_before_child": before,
                         "entry_args": list(argv[3:]), "cleanup_receipt": str(cleanup),
                         "profile_receipt": str(sidecar)})
        # This is the sole parent interception: delegate unchanged ownership,
        # deadlines, CPU accounting and native child collection.
        return original_collect(replacement, place, workspace=workspace, home=home, deadline=deadline)

    try:
        repository, _ = banked_send_repository(bank)
        workspace = bank / "workspace"
        repository.rename(workspace)
        workspace.chmod(0o700)
        seeds = census.native_seeds(bank, small_count=32, deadline=deadline)
        census._collect = collect
        for scale in ("1", "10"):
            place = bank / "measure" / scale
            values, evidence = census._native_sample("send", seeds[scale], workspace, place, deadline)
            raw_path = place / "sample.json"
            _write(raw_path, {"metrics": values, "native_evidence": evidence})
            summary["samples"][scale] = {"metrics": values,
                "raw_evidence": {"path": str(raw_path), "sha256": _sha_file(raw_path)},
                "native_cpu_diagnostic_effect_equal": evidence["cpu"]["effect"] == evidence["diagnostic"]["effect"]}
            summary["children"][scale] = {
                mode: _child_summary(place / mode, evidence[mode]) for mode in ("cpu", "diagnostic")}
        if len(launches) != 4:
            raise RuntimeError("mechanism profile did not collect its four native children")
        summary["status"] = "mechanism_probe_complete"
    except BaseException as error:
        summary["error"] = {"type": type(error).__name__, "detail": str(error)}
        raise
    finally:
        census._collect = original_collect
        summary["launches"] = launches
        source_after = census._source_witness()
        _write(bank / "source-after.json", source_after)
        summary["source_after"] = {"commit": source_after["commit"],
            "path": str(bank / "source-after.json"),
            "sha256": _sha_file(bank / "source-after.json")}
        summary["source_unchanged"] = source_before == source_after
        if not summary["source_unchanged"]:
            summary["status"] = "incomplete"
        summary["wall_seconds"] = time.monotonic() - started
        _write(bank / "summary.json", summary)
        print("BUS_SCALE_FSYNC_SUMMARY " + str(bank / "summary.json"), flush=True)
    if not summary["source_unchanged"]:
        raise RuntimeError("profile source changed during native work")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite([unittest.FunctionTestCase(
        run_profile, description="four-child native send fsync mechanism profile")])


if __name__ == "__main__":
    if len(sys.argv) == 10 and sys.argv[1] == "native-child":
        _profile_child(sys.argv[2:8], sys.argv[8], sys.argv[9])
    else:
        raise SystemExit("invoke with python3 -B -m unittest tests.bus_scale_fsync_profile")

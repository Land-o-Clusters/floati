#!/usr/bin/env python3
"""Check Harbor landing budgets from existing test-parallel final.json receipts.

No test discovery or execution occurs. Car membership comes from the exact ID
diff between two native receipts, bound to the supplied Git refs. Both receipts
must cover their entire recorded population. Policy is read from a committed
--policy-ref (default HEAD), never from dirty registry or allowance files.

Native case timings exclude module/class fixtures. A budget pass does not say
that the native suite passed, that removals are authorized, or that the quiet-box
wall budget is met. Those remain landing decisions.

Train comparisons require each receipt's own load_before and load_after triples
(load1, load5, load15), with load15 <= 8 at both endpoints. Missing historical
load is named, never inferred. Exit 2 means not_comparable_load, distinct from
budget pass (0) and refusal (1). INFO diagnostics also go to stderr, leaving
stdout as one JSON result.
"""

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys

# Reuse native reconciliation in a private module instance. A receipt reader
# starts no workers: today's CPU count must not reject yesterday's partition.
# The launcher's original module and its capacity admission remain untouched.
_spec = importlib.util.spec_from_file_location(
    "_test_cost_receipt_validation", Path(__file__).with_name("parallel_plan.py"))
_validation = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_validation)
_validation.worker_limit = lambda: sys.maxsize
reconcile = _validation.reconcile

REGISTRY = "scripts/test-cost-registry.csv"
ALLOWANCE = "scripts/test-cost-allowance.json"
QUIET_LOAD15 = 8


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def json_value(data):
    return json.loads(data, object_pairs_hook=unique_object)


def number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid nonnegative seconds: " + label)
    return value


def ids(value, label):
    if (not isinstance(value, list)
            or any(not isinstance(item, str) or not item.strip() for item in value)
            or len(set(value)) != len(value)):
        raise ValueError("invalid or duplicate IDs: " + label)
    return value


def git(*args):
    result = subprocess.run(["/usr/bin/git", *args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError("git input unavailable: " + " ".join(args))
    return result.stdout.strip()


def commit(ref):
    return git("rev-parse", "--verify", "--end-of-options", ref + "^{commit}")


def policy_text(ref, path):
    return git("show", ref + ":" + path)


def registry(text):
    reader = csv.DictReader(io.StringIO(text), strict=True)
    if reader.fieldnames != ["id", "seconds", "reason", "owner"]:
        raise ValueError("registry requires id,seconds,reason,owner columns")
    rows = {}
    for row in reader:
        test_id = row.get("id")
        if (None in row or any(not isinstance(v, str) or not v.strip() for v in row.values())
                or test_id in rows):
            raise ValueError("malformed or duplicate registry entry: " + str(test_id))
        rows[test_id] = {**row, "seconds": number(float(row["seconds"]), test_id)}
    return rows


def receipt_load(document):
    """Retain valid endpoint testimony and name absent or unusable samples."""
    record = {"start": None, "end": None, "missing_fields": [], "invalid_fields": []}
    for field, endpoint in (("load_before", "start"), ("load_after", "end")):
        sample = document.get(field)
        try:
            valid = (isinstance(sample, list) and len(sample) == 3
                     and all(type(value) in (int, float) and math.isfinite(value) and value >= 0
                             for value in sample))
        except OverflowError:
            valid = False
        if sample is None:
            record["missing_fields"].append(field)
        elif not valid:
            record["invalid_fields"].append(field)
        else:
            record[endpoint] = sample
    record["status"] = ("load_unrecorded" if record["missing_fields"] else
                        "load_invalid" if record["invalid_fields"] else "recorded")
    return record


def timing(path):
    data = Path(path).read_bytes()
    document = json_value(data)
    if not isinstance(document, dict):
        raise ValueError("timing receipt must be an object")
    if document.get("status") != "complete" or type(document.get("successful")) is not bool:
        raise ValueError("incomplete native timing receipt: " + str(path))
    source = document["source"]
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    head = source["head"]
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid source head: " + str(path))
    population = ids(document["census"]["ids"], "census")
    if not population or not isinstance(document["results"], list) or not document["results"]:
        raise ValueError("empty timing population")
    durations, selected_all = {}, []
    for result in document["results"]:
        if not isinstance(result, dict):
            raise ValueError("worker receipt must be an object")
        if result.get("status") != "complete" or result.get("source") != source:
            raise ValueError("incomplete or foreign worker source")
        selected = ids(result["selected_ids"], "selected")
        started = ids(result["started_ids"], "started")
        stopped = ids(result["stopped_ids"], "stopped")
        blocked = ids(result["fixture_blocked_ids"], "fixture blocked")
        if blocked:
            raise ValueError("unmeasured fixture-blocked IDs: " + ", ".join(blocked))
        if (selected != started or started != stopped
                or type(result["tests_run"]) is not int or result["tests_run"] != len(started)):
            raise ValueError("incomplete start/stop timing coverage: " + ", ".join(selected))
        measured = result["test_durations"]
        if not isinstance(measured, dict) or set(measured) != set(started):
            missing = set(started) - set(measured) if isinstance(measured, dict) else set(started)
            raise ValueError("missing or foreign duration IDs: " + ", ".join(sorted(missing or set(measured))))
        if set(durations).intersection(measured):
            raise ValueError("duplicate measured IDs across workers")
        durations.update((test_id, number(value, test_id)) for test_id, value in measured.items())
        selected_all.extend(selected)
    if len(selected_all) != len(set(selected_all)) or set(selected_all) != set(population):
        raise ValueError("census/selected IDs mismatch: " + ", ".join(sorted(set(population) ^ set(selected_all))))
    aggregate = reconcile(document["census"], document["plan"], document["results"])
    if aggregate != document["aggregate"]:
        raise ValueError("native aggregate mismatch")
    if document["successful"] and not aggregate["successful"]:
        raise ValueError("native success disagrees with worker outcomes")
    return {"path": str(Path(path).resolve()), "sha256": hashlib.sha256(data).hexdigest(),
            "head": head, "native_successful": document["successful"],
            "load": receipt_load(document),
            "durations": durations, "case_seconds": math.fsum(durations.values())}


def difference(before, after):
    old, new = before["durations"], after["durations"]
    added = [{"id": test_id, "seconds": new[test_id]} for test_id in sorted(new.keys() - old.keys())]
    removed = sorted(old.keys() - new.keys())
    return {"base_ref": before["head"], "car_ref": after["head"],
            "added": added, "added_count": len(added),
            "added_seconds": math.fsum(row["seconds"] for row in added),
            "removed_ids": removed, "removed_count": len(removed),
            "removed_seconds": math.fsum(old[test_id] for test_id in removed),
            "delta_seconds": after["case_seconds"] - before["case_seconds"]}


def car_check(before, after, exemptions):
    report = difference(before, after)
    offenders = [row for row in report["added"] if row["seconds"] > 5 and
                 (row["id"] not in exemptions or row["seconds"] > exemptions[row["id"]]["seconds"])]
    reasons = []
    if offenders:
        reasons.append("unregistered_test_budget_exceeded")
    if report["added_seconds"] > 60:
        reasons.append("car_budget_exceeded")
    return {**report, "offending_tests": offenders, "offending_count": len(offenders),
            "reasons": reasons, "test_limit_seconds": 5, "car_limit_seconds": 60}


def public_receipt(receipt):
    return {key: value for key, value in receipt.items() if key != "durations"}


def comparison_load_reasons(before, after):
    loads = [before["load"], after["load"]]
    reasons = []
    if any(load["missing_fields"] for load in loads):
        reasons.append("load_unrecorded")
    if any(load["invalid_fields"] for load in loads):
        reasons.append("load_invalid")
    if any(load[endpoint] is not None and load[endpoint][2] > QUIET_LOAD15
           for load in loads for endpoint in ("start", "end")):
        reasons.append("load_not_quiet")
    return reasons


def increases_info(before, after):
    old, new = before["durations"], after["durations"]
    increased = [{"id": test_id, "baseline_seconds": old[test_id],
                  "timing_seconds": new[test_id], "delta_seconds": new[test_id] - old[test_id]}
                 for test_id in old.keys() & new.keys() if new[test_id] > old[test_id]]
    increased.sort(key=lambda row: (-row["delta_seconds"], row["id"]))
    return {"level": "INFO", "code": "existing_test_increases",
            "increased_count": len(increased), "tests": increased[:20],
            "baseline_load": before["load"], "timing_load": after["load"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="mode", required=True)
    for mode in ("car", "train"):
        sub = subs.add_parser(mode)
        sub.add_argument("--baseline", required=True, help="existing base final.json")
        sub.add_argument("--timing", required=True, help="existing car/train final.json")
        sub.add_argument("--policy-ref", default="HEAD", help="committed landing policy ref")
        if mode == "car":
            sub.add_argument("--base-ref", required=True)
            sub.add_argument("--car-ref", required=True)
        else:
            sub.add_argument("--car", nargs=3, action="append", required=True,
                             metavar=("NAME", "BASE_FINAL", "CAR_FINAL"))
    args = parser.parse_args(argv)
    try:
        resolved = {}

        def resolve(ref):
            if ref not in resolved:
                resolved[ref] = commit(ref)
            return resolved[ref]

        policy_ref = resolve(args.policy_ref)
        cache = {}

        def read(path):
            key = str(Path(path).resolve())
            if key not in cache:
                cache[key] = timing(key)
                if resolve(cache[key]["head"]) != cache[key]["head"]:
                    raise ValueError("source_ref_mismatch: " + key)
            return cache[key]

        before, after = read(args.baseline), read(args.timing)
        if args.mode == "car":
            if resolve(args.base_ref) != before["head"] or resolve(args.car_ref) != after["head"]:
                raise ValueError("source_ref_mismatch: timing does not name the supplied refs")
            report = car_check(before, after, registry(policy_text(policy_ref, REGISTRY)))
        else:
            allowance = json_value(policy_text(policy_ref, ALLOWANCE))
            ceiling = number(allowance["train_seconds"], "train allowance")
            cars = []
            names = set()
            for name, base, car in args.car:
                if not name.strip() or name in names:
                    raise ValueError("empty or duplicate car name: " + name)
                names.add(name)
                car_base, car_tip = read(base), read(car)
                cars.append({"name": name, **difference(car_base, car_tip),
                             "baseline": public_receipt(car_base),
                             "timing": public_receipt(car_tip)})
            report = {**difference(before, after), "baseline_seconds": before["case_seconds"],
                      "train_seconds": after["case_seconds"], "allowance_seconds": ceiling,
                      "cars": cars, "quiet_load15_limit": QUIET_LOAD15,
                      "reasons": comparison_load_reasons(before, after)}
            if report["reasons"]:
                report["status"] = "not_comparable_load"
            elif report["delta_seconds"] > ceiling:
                report["reasons"].append("train_budget_exceeded")
        report.update(mode=args.mode,
                      status=report.get("status", "refused" if report["reasons"] else "ok"),
                      policy_ref=policy_ref, native_successful=after["native_successful"],
                      baseline=public_receipt(before), timing=public_receipt(after),
                      info=increases_info(before, after))
    except (ValueError, KeyError, TypeError, OSError, csv.Error, OverflowError) as exc:
        report = {"mode": args.mode, "status": "refused", "reasons": ["invalid_evidence"],
                  "detail": str(exc)}
    if "info" in report:
        print("INFO " + json.dumps(report["info"], sort_keys=True, allow_nan=False), file=sys.stderr)
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return {"ok": 0, "refused": 1, "not_comparable_load": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Measure local warm ask round trips; never launch a provider or discover a root.

The operator supplies a node already serving questions. One discarded correlated
answer proves responsive service and completes warmup; registration alone proves
neither. Harness/model are operator testimony, not provider observations. Timing
runs from completed question publication to correlated answer observation,
including the real ask path's polling interval.
No speed claim is made for a failed warmup or an incomplete sample population.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
from types import SimpleNamespace
import uuid

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))

from floati.cli import _ask  # noqa: E402
from floati.errors import IntegrityFailure, ProtocolRefusal  # noqa: E402
from floati.registry import Registry  # noqa: E402
from floati.root import resolve_command_root  # noqa: E402


RECEIPT_CONSTANTS = {
    "receipt_version": 1,
    "status": "MEASURED",
    "harness_model_basis": "operator_supplied",
    "service_warmth_basis": "discarded_correlated_answer_before_samples",
    "clock": "monotonic_ns",
    "timing_scope": "question_published_to_correlated_answer_observed",
    "percentile_method": "nearest_rank",
}
LABEL_FIELDS = ("root", "served_node", "sender", "harness", "model")
NUMBER_FIELDS = ("load1_start", "load1_end", "median_seconds", "p95_seconds",
                 "min_seconds", "max_seconds")
CORRELATION_FIELDS = ("question_id", "answer_id")


class LatencyRefusal(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def parser():
    result = argparse.ArgumentParser(
        description=__doc__, epilog="To check a saved receipt offline, use --validate-receipt PATH by itself.")
    result.add_argument("--root", required=True, help="exact canonical fleet directory; no ambient fallback")
    result.add_argument("--from", dest="sender", required=True)
    result.add_argument("--to", dest="recipient", required=True, help="node already serving asks")
    result.add_argument("--session", required=True)
    result.add_argument("--harness", required=True, help="operator-supplied serving harness")
    result.add_argument("--model", required=True, help="operator-supplied serving model")
    result.add_argument("--out", required=True, help="existing canonical receipt directory")
    result.add_argument("--n", type=int, default=20)
    result.add_argument("--wait-seconds", type=int, default=60)
    result.add_argument("--repository", default=str(SOURCE))
    result.add_argument("--repo", default="floati")
    result.add_argument("--doc", default="AGENTS.md")
    result.add_argument("--note", default="Latency probe: please answer briefly with ready.")
    return result


def statistics_for(samples):
    if not samples or any(not math.isfinite(value) or value < 0 for value in samples):
        raise LatencyRefusal("latency_samples_invalid", "samples must be finite nonnegative seconds")
    ordered = sorted(samples)
    return {"n": len(ordered), "median_seconds": statistics.median(ordered),
            "p95_seconds": ordered[math.ceil(0.95 * len(ordered)) - 1],
            "min_seconds": ordered[0], "max_seconds": ordered[-1]}


def validate_receipt(receipt):
    """Check the version 1 shape, sample statistics, and distinct receipt IDs."""
    def require(valid, detail):
        if not valid:
            raise LatencyRefusal("latency_receipt_invalid", detail)

    def named(value):
        return isinstance(value, str) and bool(value.strip())

    def nonnegative(value):
        return type(value) in (int, float) and 0 <= value <= sys.float_info.max

    fields = {*RECEIPT_CONSTANTS, *LABEL_FIELDS, *NUMBER_FIELDS,
              "run_id", "source_sha", "source_dirty", "question_sha", "started_at",
              "ended_at", "timing_includes", "warmup", "samples", "n"}
    require(isinstance(receipt, dict) and receipt.keys() == fields, "receipt fields must match version 1")
    require(type(receipt["receipt_version"]) is int and receipt["timing_includes"] == ["answer_polling"] and all(
        receipt[field] == expected for field, expected in RECEIPT_CONSTANTS.items()),
        "receipt version, measurement status or method is invalid")
    require(all(named(receipt[field]) for field in LABEL_FIELDS) and Path(receipt["root"]).is_absolute(),
            "an absolute root, sender, served node, harness and model must be named")
    for field, pattern in (("run_id", r"[0-9a-f]{32}"),
                           ("source_sha", r"[0-9a-f]{40}(?:[0-9a-f]{24})?"),
                           ("question_sha", r"[0-9a-f]{40}(?:[0-9a-f]{24})?"),
                           ("started_at", r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00"),
                           ("ended_at", r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?\+00:00")):
        require(named(receipt[field]) and re.fullmatch(pattern, receipt[field]), f"{field} is invalid")
    for field in ("started_at", "ended_at"):
        try:
            datetime.fromisoformat(receipt[field])
        except ValueError as exc:
            raise LatencyRefusal("latency_receipt_invalid", f"{field} is invalid") from exc
    require(type(receipt["source_dirty"]) is bool, "source_dirty must be a boolean")
    require(all(nonnegative(receipt[field]) for field in NUMBER_FIELDS),
            "host load and timing summaries must be finite nonnegative numbers")
    n, samples = receipt["n"], receipt["samples"]
    require(type(n) is int and 1 <= n <= 10000 and isinstance(samples, list) and len(samples) == n,
            "sample population must contain exactly N rows (1..10000)")

    seen = set()
    for index, row in enumerate([receipt["warmup"], *samples]):
        fields = set(CORRELATION_FIELDS) if index == 0 else {*CORRELATION_FIELDS, "index", "seconds"}
        require(isinstance(row, dict) and row.keys() == fields, f"probe {index} has invalid fields")
        if index:
            require(type(row["index"]) is int and row["index"] == index and nonnegative(row["seconds"]),
                    f"sample {index} has an invalid index or elapsed time")
        for field in CORRELATION_FIELDS:
            identifier = row[field]
            require(named(identifier) and identifier not in seen, f"probe {index} has a missing or reused {field}")
            seen.add(identifier)

    expected = statistics_for([row["seconds"] for row in samples])
    require(all(receipt[field] == value for field, value in expected.items()),
            "median, p95 and range must agree with the recorded samples")


def unique_json_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise LatencyRefusal("latency_receipt_invalid", "receipt JSON repeats an object key")
        result[name] = value
    return result


def canonical_directory(value, code):
    named = Path(value)
    if (not named.is_absolute() or not named.is_dir() or named.is_symlink()
            or str(named) != value or named.resolve() != named):
        raise LatencyRefusal(code, "supply the exact canonical existing directory")
    return named


def identity(path):
    info = path.stat()
    return info.st_dev, info.st_ino


def git(repository, *argv):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    result = subprocess.run(["/usr/bin/git", *argv], cwd=repository,
                            env=environment, capture_output=True, text=True, check=False)
    if result.returncode:
        raise LatencyRefusal("latency_source_unavailable", "cannot resolve the explicit Git source")
    return result.stdout.strip()


def ask_once(root, args, key):
    """Use the product CLI handler, including banked-SHA and seat checks."""
    timing = {}
    status, evidence, exit_code = _ask(SimpleNamespace(
        root=str(root), sender=args.sender, recipient=args.recipient,
        repo=args.repo, sha=args.question_sha, repository=args.repository,
        doc=args.doc, note=args.note, session=args.session, idempotency_key=key,
        wait_seconds=args.wait_seconds, reply_to=None, disposition=None, reason=None), timing=timing)
    result = dict(evidence, ask_status=status, ask_exit_code=exit_code)
    if answered(result):
        result["seconds"] = (timing["answer_received_ns"] - timing["question_sent_ns"]) / 1_000_000_000
    return result


def answered(evidence):
    return (evidence.get("outcome") == "answered"
            and all(isinstance(evidence.get(name), str) and evidence[name]
                    for name in ("question_id", "answer_id")))


def measure(args, *, load=None):
    if load is None:
        load = getattr(os, "getloadavg", None)
        if load is None:
            raise LatencyRefusal("latency_load_unsupported", "this platform cannot report one-minute host load")
    if not 1 <= args.n <= 10000 or not 0 <= args.wait_seconds <= 86400:
        raise LatencyRefusal("latency_arguments_invalid", "N must be 1..10000 and wait 0..86400 seconds")
    if any(not getattr(args, name).strip() for name in ("harness", "model", "session", "note")):
        raise LatencyRefusal("latency_arguments_invalid", "harness, model, session and note must be named")
    root_path = canonical_directory(args.root, "latency_root_not_named")
    canonical_directory(args.out, "latency_output_invalid")
    root_identity = identity(root_path)
    root = resolve_command_root(str(root_path))
    registry = Registry(root)
    registry.require_active(args.sender)
    try:
        registry.require_active(args.recipient)
    except ProtocolRefusal as exc:
        raise LatencyRefusal("latency_node_not_served", "the named recipient is not active on this root") from exc
    source_sha = git(SOURCE, "rev-parse", "HEAD")
    source_dirty = bool(git(SOURCE, "status", "--porcelain"))
    args.question_sha = git(args.repository, "rev-parse", "HEAD")
    run_id = uuid.uuid4().hex

    def check_root():
        canonical_directory(args.root, "latency_root_not_named")
        if identity(root_path) != root_identity:
            raise LatencyRefusal("latency_root_not_named", "the named root was replaced during measurement")

    def probe(index):
        check_root()
        evidence = ask_once(root_path, args, "latency-" + run_id + "-" + str(index))
        check_root()
        return evidence

    # Observe load across the whole operation, including the discarded warmup.
    started_at = datetime.now(timezone.utc).isoformat()
    load_start = load()[0]
    warmup = probe(0)
    if not answered(warmup):
        code = "latency_node_not_served" if warmup.get("outcome") == "UNKNOWN" else "latency_node_not_warm"
        raise LatencyRefusal(code, "discarded warmup did not return a correlated answer: "
                             + str(warmup.get("code", warmup.get("outcome"))))
    samples = []
    for index in range(1, args.n + 1):
        evidence = probe(index)
        if not answered(evidence):
            raise LatencyRefusal("latency_node_not_warm", "sample {0} did not return a correlated answer: {1}".format(
                index, evidence.get("code", evidence.get("outcome"))))
        samples.append({"index": index, "seconds": evidence["seconds"],
                        "question_id": evidence["question_id"], "answer_id": evidence["answer_id"]})
    load_end = load()[0]
    if git(SOURCE, "rev-parse", "HEAD") != source_sha:
        raise LatencyRefusal("latency_source_changed", "source commit changed during measurement")
    receipt = {**RECEIPT_CONSTANTS, "run_id": run_id,
            "root": str(root_path), "served_node": args.recipient, "sender": args.sender,
            "harness": args.harness, "model": args.model,
            "warmup": {name: warmup[name] for name in ("question_id", "answer_id")},
            "source_sha": source_sha, "source_dirty": source_dirty,
            "question_sha": args.question_sha,
            "started_at": started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
            "timing_includes": ["answer_polling"], "load1_start": load_start, "load1_end": load_end,
            "samples": samples, **statistics_for([row["seconds"] for row in samples])}
    validate_receipt(receipt)
    return receipt


def check_saved_receipt(argv):
    check = argparse.ArgumentParser(description="Check ask latency receipt structure and sample consistency offline.")
    check.add_argument("--validate-receipt", required=True, type=Path)
    path = check.parse_args(argv).validate_receipt
    try:
        if not path.is_file():
            raise LatencyRefusal("latency_receipt_invalid", "supply a regular receipt file")
        receipt = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
    except (OSError, ValueError, RecursionError) as exc:
        raise LatencyRefusal("latency_receipt_invalid", "cannot read a valid receipt JSON object") from exc
    validate_receipt(receipt)
    print(json.dumps({"artifact_version": 0, "command": "measure-ask-latency", "status": "ok",
                      "evidence": {"receipt": str(path), "n": receipt["n"], "run_id": receipt["run_id"]}}))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0].split("=", 1)[0] == "--validate-receipt":
            return check_saved_receipt(argv)
        args = parser().parse_args(argv)
        receipt = measure(args)
        output = canonical_directory(args.out, "latency_output_invalid") / (
            "ask-latency-" + receipt["run_id"] + ".json")
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, indent=2, allow_nan=False) + "\n")
        print("MEASURED N={n} median={median_seconds:.6f}s p95={p95_seconds:.6f}s receipt={path}".format(
            **receipt, path=output))
        return 0
    except (LatencyRefusal, ProtocolRefusal, IntegrityFailure, OSError, ValueError) as exc:
        print(json.dumps({"artifact_version": 0, "command": "measure-ask-latency", "status": "refused",
                          "evidence": {"code": getattr(exc, "code", "latency_unavailable"), "detail": str(exc)}}))
        return 20


if __name__ == "__main__":
    raise SystemExit(main())

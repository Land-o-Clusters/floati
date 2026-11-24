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


class LatencyRefusal(Exception):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def parser():
    result = argparse.ArgumentParser(description=__doc__)
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
    return {"receipt_version": 1, "status": "MEASURED", "run_id": run_id,
            "root": str(root_path), "served_node": args.recipient, "sender": args.sender,
            "harness": args.harness, "model": args.model,
            "harness_model_basis": "operator_supplied",
            "service_warmth_basis": "discarded_correlated_answer_before_samples",
            "warmup": {name: warmup[name] for name in ("question_id", "answer_id")},
            "source_sha": source_sha, "source_dirty": source_dirty,
            "question_sha": args.question_sha,
            "started_at": started_at, "ended_at": datetime.now(timezone.utc).isoformat(),
            "clock": "monotonic_ns", "timing_scope": "question_published_to_correlated_answer_observed",
            "timing_includes": ["answer_polling"],
            "percentile_method": "nearest_rank", "load1_start": load_start, "load1_end": load_end,
            "samples": samples, **statistics_for([row["seconds"] for row in samples])}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
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

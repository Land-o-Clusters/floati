"""Scratch-only fixed-delay responder for the latency instrument integration test."""

import argparse
from pathlib import Path
import subprocess
import time

from floati.events import EVENT_KINDS
from floati.errors import IntegrityFailure
from floati.jsonl import read_records
from floati.root import resolve_command_root
from tests.readiness import Readiness


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "repository", "sha", "ready"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--delay", type=float, required=True)
    args = parser.parse_args()
    root = resolve_command_root(args.root)
    answered = set()
    readiness = Readiness("scratch latency provider questions", timeout=120)
    Path(args.ready).write_text("ready\n")
    while len(answered) < args.count:
        readiness.remaining()
        try:
            rows = read_records(root, "events.jsonl", allowed_kinds=set(EVENT_KINDS))
        except IntegrityFailure:
            time.sleep(0.005)
            continue
        for row in rows:
            if row["kind"] != "ask_question" or row["id"] in answered:
                continue
            time.sleep(args.delay)
            result = subprocess.run([
                "/usr/bin/python3", "-B", "-m", "floati", "ask", "--root", args.root,
                "--from", row["recipient"], "--to", row["sender"], "--reply-to", row["id"],
                "--disposition", "answer", "--repo", "fixture", "--sha", args.sha,
                "--repository", args.repository, "--doc", "note.md", "--note", "ready",
                "--session", "fake-provider", "--idempotency-key", "reply-" + row["id"]],
                cwd=args.repository, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            answered.add(row["id"])
        time.sleep(0.005)
    return 0 if len(answered) == args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())

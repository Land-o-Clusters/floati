#!/usr/bin/env python3
"""Observe one CI-shaped selftest without writing into its source checkout."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from fixture_finalizer import install, owned_child


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--test", action="append", default=[],
                        help="focused unittest selector; omit for the eight-worker CI run")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(mode=0o700)
    install(temp_root="\x2fprivate/tmp")
    home = Path(tempfile.mkdtemp(prefix="hw-home.", dir="\x2fprivate/tmp"))
    environment = dict(os.environ, HOME=str(home), PYTHONDONTWRITEBYTECODE="1",
                       PYTHONUSERBASE=str(Path.home() / "Library/Python/3.9"))
    command = ["/usr/bin/nice", "-n", "10", "/usr/bin/python3", "-B", "-m"]
    if args.test:
        command.extend(["unittest", "-v", *args.test])
    else:
        command.extend(["floati.selftest", "--workers", "8", "--output", str(output / "runner")])
    git_dir = Path(subprocess.check_output(
        ["/usr/bin/git", "rev-parse", "--absolute-git-dir"], cwd=source, text=True).strip())
    started = time.monotonic()
    receipt = {"command": command, "source": str(source), "home": str(home),
               "environment": {name: environment[name] for name in
                               ("HOME", "PYTHONDONTWRITEBYTECODE", "PYTHONUSERBASE")},
               "load_start": os.getloadavg(), "interval_seconds": 0.25}
    (output / "invocation.json").write_text(json.dumps(receipt, indent=2) + "\n")
    offsets = {}
    active = {}
    samples = 0
    dirty_samples = 0
    # The child shares this terminal's process group: Ctrl-C reaches both it
    # and the observer. Always reap the exact child before returning.
    with (output / "selftest.log").open("x") as log,\
            (output / "samples.jsonl").open("x") as sampler,\
            (output / "observed-events.jsonl").open("x") as observed,\
            owned_child(command, cwd=source, env=environment,
                        stdout=log, stderr=subprocess.STDOUT) as child:
        try:
            while True:
                tick = time.monotonic()
                timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
                for journal in sorted((output / "runner").glob("*.events.jsonl")):
                    with journal.open() as stream:
                        stream.seek(offsets.get(journal.name, 0))
                        while True:
                            position = stream.tell()
                            line = stream.readline()
                            if not line.endswith("\n"):
                                stream.seek(position)
                                break
                            event = json.loads(line)
                            record = event["record"]
                            key = journal.name
                            if record.get("kind") == "start":
                                active[key] = record["test_id"]
                            elif record.get("kind") in ("stop", "footer"):
                                active.pop(key, None)
                            observed.write(json.dumps({"observed_at": timestamp,
                                                       "journal": key, **event}) + "\n")
                        offsets[journal.name] = stream.tell()
                status = subprocess.check_output(
                    ["/usr/bin/git", "--no-optional-locks", "status", "--porcelain",
                     "--untracked-files=all"], cwd=source, text=True)
                index = git_dir / "index"
                row = {"at": timestamp, "elapsed_seconds": time.monotonic() - started,
                       "status": status, "active": dict(active),
                       "index_sha256": hashlib.sha256(index.read_bytes()).hexdigest(),
                       "locks": sorted(p.name for p in git_dir.glob("*.lock"))}
                sampler.write(json.dumps(row, sort_keys=True) + "\n")
                sampler.flush()
                observed.flush()
                samples += 1
                dirty_samples += bool(status)
                if child.poll() is not None:
                    break
                time.sleep(max(0, 0.25 - (time.monotonic() - tick)))
        finally:
            # The shared owner forwards interruption to the exact child.
            receipt["exit_code"] = child.wait()
            receipt.update(load_end=os.getloadavg(), samples=samples,
                           dirty_samples=dirty_samples,
                           wall_seconds=time.monotonic() - started)
            (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            print(json.dumps(receipt, sort_keys=True), flush=True)
    return receipt["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Derive only NODE-SPINUP-CLI-1's two reviewed parser-closure amendments.

The historical capture and its digest remain untouched. This is parser
coverage, never workload, scale or ledger-contract ratification evidence.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    from floati.cli import _parser
    from tests import bus_scale_commands as owner

    historical = owner._FIXTURE.read_bytes()
    if hashlib.sha256(historical).hexdigest() != owner.BASELINE_FIXTURE_SHA256:
        raise ValueError("historical command capture changed")
    previous = owner._from_commands(json.loads(historical)["commands"])
    current = owner.executable_contract(_parser())
    selected = ("hook install", "node role")
    # These are the complete reviewed changes. Never capture another row's
    # drift merely because it happens to be present in the caller's tree.
    changed = {path for path in current if owner._signature(current[path]) !=
        owner._signature(previous.get(path))}
    if changed != set(selected) or set(current) != set(previous):
        raise ValueError("node spinup command amendment has unrelated parser drift")
    document = {"schema_version": 0,
        "predecessors": {path: owner._signature(previous[path]) for path in selected},
        "commands": [current[path] for path in selected]}
    encoded = json.dumps(document, indent=2, sort_keys=True) + "\n"
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    helper = ROOT / "tests/bus_scale_commands.py"
    before = helper.read_text()
    after, count = re.subn(r'^_NODE_SPINUP_FIXTURE_SHA256 = "[a-f0-9]*"$',
        '_NODE_SPINUP_FIXTURE_SHA256 = "' + digest + '"', before, flags=re.MULTILINE)
    if count != 1:
        raise ValueError("node spinup command amendment pin is ambiguous")
    target = owner._NODE_SPINUP_FIXTURE
    drift = not target.exists() or target.read_text() != encoded or before != after
    if drift and not args.write:
        print("DRIFT node spinup command amendment; re-run with --write")
        return 1
    if args.write:
        target.write_text(encoded)
        helper.write_text(after)
    print(("WROTE" if args.write else "UNCHANGED") + " node spinup command amendment: 2 rows " + digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

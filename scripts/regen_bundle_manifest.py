#!/usr/bin/env python3
"""Regenerate `bundle-manifest.v0.json` — the second derived artifact nobody owned.

`floati.manifest.verify_manifest` is the VERIFIER and the pre-commit hook is the GUARD;
neither can produce the artifact, so every integrator has hand-resolved digest conflicts
in a 1,500-line JSON file. Two cars on one train each touched `floati/records.py`, each
moved its digest, and the merge presented that as a choice. **A conflict on a derived
value is not a choice: regenerate.** Choosing a side here means picking one car's digest
for a file that now contains both cars' edits — a manifest that verifies against neither
tree.

It derives nothing of its own: the deployable path set comes from
`floati.manifest._deployable_paths` (the same function the verifier compares against) and
the digests are plain SHA-256 of the bytes on disk. Every top-level field other than
`files` is carried through untouched, because those are declarations and not measurements.

    --check   compare only; exit 1 and name what drifted. **The default**, because a tool
              that writes by default gets run by accident.
    --write   rewrite the manifest in place.

Run `--check` at the tip you are about to push, never only at the union you composed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

MANIFEST_NAME = "bundle-manifest.v0.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def derived_files(root: Path) -> list[dict]:
    sys.path.insert(0, str(root))
    from floati.manifest import _deployable_paths

    return [
        {"path": relative, "sha256": digest(root / relative)}
        for relative in sorted(_deployable_paths(root))
    ]


def render(current: dict, files: list[dict]) -> str:
    document = dict(current)
    document["files"] = files
    return json.dumps(document, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    parser.add_argument("--write", action="store_true", help="rewrite the manifest in place")
    arguments = parser.parse_args()

    root = Path(arguments.root).resolve()
    path = root / MANIFEST_NAME
    current_text = path.read_text(encoding="utf-8")
    current = json.loads(current_text)
    wanted_text = render(current, derived_files(root))

    if wanted_text == current_text:
        print("UNCHANGED %s  %d files" % (MANIFEST_NAME, len(current["files"])))
        return 0

    was = {entry["path"]: entry["sha256"] for entry in current.get("files", [])}
    now = {entry["path"]: entry["sha256"] for entry in json.loads(wanted_text)["files"]}
    for relative in sorted(set(was) | set(now)):
        if relative not in now:
            print("  REMOVED %s" % relative)
        elif relative not in was:
            print("  ADDED   %s" % relative)
        elif was[relative] != now[relative]:
            print("  DIGEST  %s" % relative)
    if arguments.write:
        path.write_text(wanted_text, encoding="utf-8")
        print("WROTE     %s  %d files" % (MANIFEST_NAME, len(now)))
        return 0
    print("DRIFT     %s  %d files derived. Re-run with --write." % (MANIFEST_NAME, len(now)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

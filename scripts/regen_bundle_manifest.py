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

    (no flag) compare only; exit 1 and name what drifted. **The default**, because a tool
              that writes by default gets run by accident.
    --write   rewrite the manifest in place.

Run without flags at the tip you are about to push, never only at the union you composed.
"""
from __future__ import annotations

import argparse
import ast
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


def reviewed_schema_pin(root: Path, reviewed: list[str]) -> str:
    """Advance only explicitly reviewed membership; reuse the verifier's census."""
    sys.path.insert(0, str(root))
    from tests import test_manifest as owner

    target = root / "tests/test_manifest.py"
    text = target.read_text(encoding="utf-8")
    assignments = {node.targets[0].id: node for node in ast.parse(text).body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)}
    names = ("_FLOATI_GOVERNED_PROTOCOL_ASSET_COUNT", "_FLOATI_GOVERNED_PROTOCOL_PATHS_SHA256",
        "_FLOATI_GOVERNED_PROTOCOL_COMMON_KEY_COUNT", "_FLOATI_GOVERNED_PROTOCOL_COMMON_KEYS_SHA256")
    pinned = tuple(ast.literal_eval(assignments[name].value) for name in names)
    paths = owner._protocol_json_paths(owner._GOVERNED_PROTOCOL_ASSET_ROOTS, "governed protocol", root=root)
    reviewed_paths = {Path(value) for value in reviewed}
    if (len(reviewed_paths) != len(reviewed) or not reviewed_paths
        or any(path.is_absolute() or path.as_posix() != value or ".." in path.parts
            for value, path in zip(reviewed, map(Path, reviewed)))
        or not reviewed_paths <= set(paths)):
        raise ValueError("reviewed_schema_selection_invalid")
    observed = owner._governed_protocol_snapshot(root=root)
    if observed[2:] != pinned[2:]:
        raise ValueError("reviewed_schema_shared_shape_changed")
    frozen = ("_FLOATI_FROZEN_PROTOCOL_ASSET_COUNT", "_FLOATI_FROZEN_PROTOCOL_PATHS_SHA256",
        "_FLOATI_FROZEN_PROTOCOL_SNAPSHOT_SHA256")
    if owner._frozen_protocol_snapshot(root=root) != tuple(
        ast.literal_eval(assignments[name].value) for name in frozen):
        raise ValueError("reviewed_schema_frozen_bytes_changed")
    if observed == pinned:
        return text
    previous = [path for path in paths if path not in reviewed_paths]
    if (len(previous), owner._protocol_paths_sha256(previous)) != pinned[:2]:
        raise ValueError("reviewed_schema_previous_membership_mismatch")
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements = []
    for name, value in zip(names[:2], observed[:2]):
        node = assignments[name].value
        replacements.append((offsets[node.lineno - 1] + node.col_offset,
            offsets[node.end_lineno - 1] + node.end_col_offset, repr(value)))
    for start, end, value in sorted(replacements, reverse=True):
        text = text[:start] + value + text[end:]
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="repository root (default: cwd)")
    parser.add_argument("--write", action="store_true", help="rewrite the manifest in place")
    parser.add_argument("--reviewed-schema", action="append", default=[],
        help="explicit governed schema addition; refuses any other membership or shape drift")
    arguments = parser.parse_args()

    root = Path(arguments.root).resolve()
    if arguments.reviewed_schema:
        schema_pin = root / "tests/test_manifest.py"
        wanted_pin = reviewed_schema_pin(root, arguments.reviewed_schema)
        if wanted_pin != schema_pin.read_text(encoding="utf-8"):
            if not arguments.write:
                print("DRIFT     reviewed governed-schema membership pin; re-run with --write")
                return 1
            schema_pin.write_text(wanted_pin, encoding="utf-8")
            print("WROTE     tests/test_manifest.py reviewed governed-schema membership pin")
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

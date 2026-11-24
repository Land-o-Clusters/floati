#!/usr/bin/env python3
"""Derive only the capture checkout's reviewed public built-command pin.

Reuse the existing source-only scanner and literal writer. Any delta outside
the one reviewed capture script refuses, and unrelated pin bytes are retained.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()
    path = Path(__file__).with_name("regen-loaded-wallclock-fence-pins.py")
    spec = importlib.util.spec_from_file_location("capture_checkout_pin_writer", path)
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    owner = "scripts/capture-shot1-locf1.py"
    helper.OWNERS = frozenset({owner})
    root = path.resolve().parents[1]
    sys.path.insert(0, str(root))
    from tests.export_inventory import classify_inventory
    from tests.test_no_listener_fence import _built_argv_egress_sites

    target = root / "tests/test_no_listener_fence.py"
    original = target.read_bytes()
    assignment, pinned = helper.pin_assignment(ast.parse(original), helper.METHODS["sites"])
    measured = _built_argv_egress_sites([root / "floati", root / "scripts"], relative_to=root)
    included = set(classify_inventory(sorted({name for name, _ in measured | pinned}), root=root))
    old = {site for site in pinned if site[0] in included}
    actual = {site for site in measured if site[0] in included}
    if any(name != owner for name, _ in old ^ actual):
        raise ValueError("built-command drift outside the reviewed capture script")
    wanted = pinned - old | actual
    replacement = helper.owned_literal(original, assignment, wanted)
    rendered = original
    if replacement is not None:
        start, end, literal = replacement
        rendered = original[:start] + literal + original[end:]
    if helper.pin_assignment(ast.parse(rendered), helper.METHODS["sites"])[1] != wanted:
        raise ValueError("generated pin readback differs")
    if target.read_bytes() != original:
        raise ValueError("pin changed during derivation")
    changed = rendered != original
    if arguments.write and changed:
        target.write_bytes(rendered)
    print(json.dumps({"status": "written" if arguments.write and changed else
                     "drift" if changed else "unchanged", "reviewed_path": owner,
                     "added": sorted(actual - old), "removed": sorted(old - actual)}, sort_keys=True))
    return 0 if arguments.write or not changed else 1


if __name__ == "__main__":
    raise SystemExit(main())

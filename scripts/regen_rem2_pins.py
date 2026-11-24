#!/usr/bin/python3
"""Regenerate REM-2 declarations without permitting unbound census growth."""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COUNT_FIELDS = {
    "PINNED_PROTOCOL_COUNT": "protocol",
    "PINNED_KIND_NONE_COUNT": "kind_none",
    "PINNED_BOUND_COUNT": "bound",
    "PINNED_UNBOUND_COUNT": "unbound",
    "PINNED_MIXED_COUNT": "mixed",
    "PINNED_INTEGRITY_COUNT": "integrity",
    "PINNED_DURABILITY_COUNT": "durability",
    "PINNED_OTHER_COUNT": "other",
}
DIGEST_FIELDS = {
    "PINNED_PROTOCOL_DIGEST": "protocol",
    "PINNED_KIND_NONE_DIGEST": "kind_none",
    "PINNED_OTHER_DIGEST": "other",
    "PINNED_INTEGRITY_DIGEST": "integrity",
    "PINNED_DURABILITY_DIGEST": "durability",
}
SET_FIELDS = {
    "PINNED_ROOT_SHARED": "root_shared",
    "PINNED_INTEGRITY_DURABILITY": "integrity_durability",
    "PINNED_ROLE_INVALID_OUTSIDE": "role_invalid_outside",
    "PINNED_BOUND": "bound",
    "PINNED_MIXED": "mixed",
}
PIN_NAMES = frozenset(COUNT_FIELDS) | frozenset(DIGEST_FIELDS) | frozenset(SET_FIELDS) | {
    "PINNED_UNRESOLVED_PROTOCOL"}


def ordinary_bytes(path):
    metadata = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1):
        raise ValueError("pin source must be an ordinary repository file")
    return path.read_bytes()


def replacement(name, value):
    if isinstance(value, frozenset):
        body = "frozenset({\n" + "".join("    " + repr(item) + ",\n" for item in sorted(value)) + "})"
    else:
        body = repr(value)
    return (name + " = " + body).encode("utf-8")


def declarations(source, values):
    tree = ast.parse(source, filename="tests/test_rem_2.py")
    nodes = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            for name in names:
                if name not in PIN_NAMES:
                    continue
                if len(node.targets) != 1 or name in nodes:
                    raise ValueError("pin declaration must be one exact top-level assignment")
                nodes[name] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in PIN_NAMES:
            raise ValueError("annotated pin declarations are outside this producer contract")
    if set(nodes) != PIN_NAMES or set(values) != PIN_NAMES:
        raise ValueError("REM-2 pin declaration set changed")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    for name, node in nodes.items():
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        edits.append((start, end, replacement(name, values[name])))
    result = source
    for start, end, body in sorted(edits, reverse=True):
        result = result[:start] + body + result[end:]
    # The replacement ranges are declarations only; separately prove that the
    # executable census and all test methods retain their exact syntax trees.
    def retained(parsed):
        return [ast.dump(node, include_attributes=False) for node in parsed.body
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id in PIN_NAMES)]
    if retained(tree) != retained(ast.parse(result)):
        raise ValueError("regeneration changed logic outside pinned declarations")
    return result


def atomic_write(path, before, after):
    if ordinary_bytes(path) != before:
        raise ValueError("pin source changed during regeneration")
    mode = stat.S_IMODE(path.stat().st_mode)
    descriptor, staged = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staged, mode)
        if ordinary_bytes(path) != before:
            raise ValueError("pin source changed before publication")
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--write", action="store_true")
    modes.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    if Path.cwd() != ROOT:
        parser.error("invoke this producer from its repository root")
    try:
        from tests import test_rem_2 as owner

        target = ROOT / "tests/test_rem_2.py"
        pin = ROOT / "tests/rem_2_unbound_pin.txt"
        source, old_pin = ordinary_bytes(target), ordinary_bytes(pin)
        derived = owner.census(ROOT)
        old_kind_none = owner.PINNED_UNBOUND | owner.PINNED_MIXED
        new_unbound = derived["unbound"] - owner.PINNED_UNBOUND
        new_kind_none = derived["kind_none"] - old_kind_none
        if (new_unbound or new_kind_none
                or len(derived["kind_none"]) > owner.PINNED_KIND_NONE_COUNT
                or len(derived["unbound"]) > owner.PINNED_UNBOUND_COUNT):
            print(json.dumps({"status": "refused", "reason": "rem2_ratchet_growth",
                "new_unbound": sorted(new_unbound), "new_kind_none": sorted(new_kind_none)}, sort_keys=True))
            return 2
        values = {name: len(derived[key]) for name, key in COUNT_FIELDS.items()}
        values.update({name: owner._set_digest(derived[key]) for name, key in DIGEST_FIELDS.items()})
        values.update({name: derived[key] for name, key in SET_FIELDS.items()})
        values["PINNED_UNRESOLVED_PROTOCOL"] = derived["unresolved_protocol"]
        updated = declarations(source, values)
        # Preserve the existing pin bytes when membership did not shrink.
        updated_pin = old_pin if derived["unbound"] == owner.PINNED_UNBOUND else (
            "\n".join(sorted(derived["unbound"])) + "\n").encode("utf-8")
        changed = [(path, before, after) for path, before, after in (
            (target, source, updated), (pin, old_pin, updated_pin)) if before != after]
        if args.write:
            for path, before, after in changed:
                atomic_write(path, before, after)
        print(json.dumps({"status": "updated" if args.write and changed else "drift" if changed else "current",
            "files": [path.relative_to(ROOT).as_posix() for path, _, _ in changed],
            "counts": {key: len(value) if isinstance(value, frozenset) else value
                for key, value in derived.items()}}, sort_keys=True))
        return 1 if args.check and changed else 0
    except (OSError, SyntaxError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "refused", "reason": "rem2_source_invalid", "detail": str(exc)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

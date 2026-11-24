"""Regenerate only the release checker's reviewed local Git fence pins."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
OWNER = "floati/release.py"
TARGET = "tests/test_no_listener_fence.py"
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true", help="compare only (the default)")
    arguments = parser.parse_args()
    from tests.test_no_listener_fence import (
        _subprocess_population, _built_argv_egress_files, _built_argv_egress_sites,
    )
    from tests.export_inventory import classify_inventory

    kinds, shapes = Counter(), Counter()
    for path in sorted((ROOT / "floati").rglob("*.py")):
        found_kinds, found_shapes = _subprocess_population(ast.parse(path.read_text()))
        kinds.update(found_kinds)
        shapes.update(found_shapes)
        if path == ROOT / OWNER:
            if found_kinds != {"run": 3} or found_shapes != {"mixed": 3}:
                raise ValueError("reviewed release Git sites changed")
    source = (ROOT / TARGET).read_text()
    owner, = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
              and node.name == "WholeProductNoListenerFenceTests"]
    methods = {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)}
    population = methods["test_subprocess_population_is_pinned"]
    pins = [node.value.args[0] for node in population.body if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "assertEqual"]
    if len(pins) != 2 or any(not isinstance(node, ast.Dict) for node in pins):
        raise ValueError("subprocess pin shape changed")
    observed = (dict(kinds), dict(shapes))
    replacements = []
    for node, wanted, delta in zip(pins, observed, ({"run": 3}, {"mixed": 3})):
        current = ast.literal_eval(node)
        if Counter(current) not in (Counter(wanted), Counter(wanted) - Counter(delta)):
            raise ValueError("subprocess drift outside reviewed release Git sites")
        if current != wanted:
            replacements.append((node, json.dumps(wanted, sort_keys=True)))

    roots = [ROOT / "floati", ROOT / "scripts"]
    files = _built_argv_egress_files(roots, relative_to=ROOT)
    sites = _built_argv_egress_sites(roots, relative_to=ROOT)
    for method, measured, site_pin in (
        ("test_built_argv_egress_is_enumerated_not_blind", files, False),
        ("test_built_argv_egress_sites_are_pinned", sites, True),
    ):
        node, = [node.value for node in methods[method].body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "pinned"
                         for target in node.targets)]
        current = ast.literal_eval(node)
        path_of = (lambda row: row[0]) if site_pin else (lambda row: row)
        included = set(classify_inventory(sorted({path_of(row) for row in measured | current}), root=ROOT))
        kept = {row for row in current if path_of(row) in included and path_of(row) != OWNER}
        found = {row for row in measured if path_of(row) in included and path_of(row) != OWNER}
        if kept != found:
            raise ValueError("built argv drift outside reviewed release Git sites")
        own = {row for row in measured if path_of(row) == OWNER}
        if len(own) != (2 if site_pin else 1):
            raise ValueError("reviewed release built argv shape changed")
        previous = ({(OWNER, "3d1ca01c3fa2cb98"), (OWNER, "913efa66a60383a8")}
                    if site_pin else {OWNER})
        current_own = {row for row in current if path_of(row) == OWNER}
        if current_own not in (previous, own):
            raise ValueError("unreviewed release pin predecessor")
        wanted = (current - current_own) | own
        if wanted != current:
            literal = ast.get_source_segment(source, node)
            prefix, closing = literal.rsplit("\n", 1)
            indent = closing[:len(closing) - len(closing.lstrip())] + "    "
            for row in sorted(current_own - own):
                line = indent + repr(row) + ","
                if prefix.count(line) != 1:
                    raise ValueError("reviewed release pin line shape changed")
                prefix = "\n".join(part for part in prefix.split("\n") if part != line)
            additions = "".join(indent + repr(row) + ",\n" for row in sorted(own - current))
            replacements.append((node, prefix + "\n" + additions + closing))

    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    for node, value in sorted(replacements, key=lambda pair: pair[0].lineno, reverse=True):
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        source = source[:start] + value + source[end:]
    if replacements and arguments.write:
        (ROOT / TARGET).write_text(source)
    print(json.dumps({"status": "written" if replacements and arguments.write else
                      "drift" if replacements else "unchanged",
                      "kinds": observed[0], "argv_shapes": observed[1],
                      "release_built_argv_sites": sorted(row for row in sites if row[0] == OWNER)},
                     sort_keys=True))
    return 1 if replacements and not arguments.write else 0


if __name__ == "__main__":
    raise SystemExit(main())

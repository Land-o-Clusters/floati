"""Derive the reviewed ZCode artifact-readback and terminal-resolver pins.

The canonical census and export classifier remain unchanged. Any unrelated
drift or removal refuses. No native measurement or test is executed here.
"""

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.export_inventory import classify_inventory
from tests.test_no_listener_fence import _built_argv_egress_sites, _subprocess_population


OWNER = "scripts/measure_zcode_conformance.py"
TARGET = ROOT / "tests/test_no_listener_fence.py"
METHODS = {"test_built_argv_egress_is_enumerated_not_blind": "files",
           "test_built_argv_egress_sites_are_pinned": "sites"}
RESOLVER_OWNER = "floati/adapters/zcode.py"


def assignments(tree):
    owner, = [node for node in tree.body if isinstance(node, ast.ClassDef)
              and node.name == "WholeProductNoListenerFenceTests"]
    result = {}
    for method, kind in METHODS.items():
        function, = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == method]
        assignment, = [node for node in function.body if isinstance(node, ast.Assign)
                      and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                      and node.targets[0].id == "pinned"]
        if not isinstance(assignment.value, ast.Set):
            raise ValueError("pin must remain a literal set")
        result[kind] = assignment.value
    return result


def population_pins(source):
    owner, = [node for node in ast.parse(source).body if isinstance(node, ast.ClassDef)
              and node.name == "WholeProductNoListenerFenceTests"]
    method, = [node for node in owner.body if isinstance(node, ast.FunctionDef)
               and node.name == "test_subprocess_population_is_pinned"]
    pins = [node.value.args[0] for node in method.body if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute)
            and node.value.func.attr == "assertEqual"]
    if len(pins) != 2 or any(not isinstance(node, ast.Dict) for node in pins):
        raise ValueError("subprocess population pin shape changed")
    return pins


def resolver_population(source):
    kinds, shapes = Counter(), Counter()
    own = _subprocess_population(ast.parse((ROOT / RESOLVER_OWNER).read_text()))
    if own != ({"run": 1}, {"mixed": 1}):
        raise ValueError("reviewed metadata-only Node resolver site changed")
    for path in sorted((ROOT / "floati").rglob("*.py")):
        found_kinds, found_shapes = _subprocess_population(ast.parse(path.read_text()))
        kinds.update(found_kinds)
        shapes.update(found_shapes)
    observed = (dict(kinds), dict(shapes))
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    edits = []
    for node, actual, delta in zip(population_pins(source), observed, own):
        current = ast.literal_eval(node)
        if Counter(current) not in (Counter(actual), Counter(actual) - Counter(delta)):
            raise ValueError("subprocess population drift outside the reviewed ZCode resolver")
        if current != actual:
            edits.append((offsets[node.lineno - 1] + node.col_offset,
                          offsets[node.end_lineno - 1] + node.end_col_offset,
                          json.dumps(actual, sort_keys=True)))
    for start, end, literal in sorted(edits, reverse=True):
        source = source[:start] + literal + source[end:]
    if tuple(ast.literal_eval(node) for node in population_pins(source)) != observed:
        raise ValueError("generated subprocess population readback differs")
    return source, observed


def regenerate(write=False):
    original = TARGET.read_text()
    nodes = assignments(ast.parse(original))
    sites = {(Path(path).relative_to(ROOT).as_posix(), digest)
             for path, digest in _built_argv_egress_sites([ROOT / "floati", ROOT / "scripts"])}
    measured = {"sites": sites, "files": {path for path, _ in sites}}
    path_of = lambda entry: entry if isinstance(entry, str) else entry[0]
    edits, additions = [], {}
    lines = original.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for kind, node in nodes.items():
        pinned = ast.literal_eval(node)
        population = {path_of(entry) for entry in pinned | measured[kind]}
        included = set(classify_inventory(sorted(population), root=ROOT))
        old = {entry for entry in pinned if path_of(entry) in included}
        actual = {entry for entry in measured[kind] if path_of(entry) in included}
        added, removed = actual - old, old - actual
        if removed or any(path_of(entry) != OWNER for entry in added) or OWNER not in included:
            raise ValueError("census drift outside this row's additive artifact-readback sites")
        additions[kind] = sorted(added)
        if added:
            closing = lines[node.end_lineno - 1]
            if closing.strip() != "}":
                raise ValueError("pin closing delimiter moved")
            indent = closing[:len(closing) - len(closing.lstrip())] + "    "
            insertion = "".join(indent + repr(entry) + ",\n" for entry in sorted(added))
            edits.append((offsets[node.end_lineno - 1], insertion))
    wanted = original
    for offset, insertion in sorted(edits, reverse=True):
        wanted = wanted[:offset] + insertion + wanted[offset:]
    reread = assignments(ast.parse(wanted))
    for kind, node in reread.items():
        if ast.literal_eval(node) != ast.literal_eval(nodes[kind]) | set(map(tuple, additions[kind]) if kind == "sites" else additions[kind]):
            raise ValueError("generated pin readback differs")
    wanted, population = resolver_population(wanted)
    changed = wanted != original
    if TARGET.read_text() != original:
        raise ValueError("pin changed during observation")
    if write and wanted != original:
        TARGET.write_text(wanted)
    print(json.dumps({"status": "written" if write and changed else "drift" if changed else "unchanged",
                      "reviewed_path": OWNER, "added": additions,
                      "resolver_owner": RESOLVER_OWNER, "population": population}, sort_keys=True))
    return 0 if write or not changed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    raise SystemExit(regenerate(args.write))

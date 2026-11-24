#!/usr/bin/env python3
"""Regenerate the selected reviewed row's public egress pin literals.

Check is the default; --write changes only the two `pinned` assignments in
tests/test_no_listener_fence.py, plus its direct population literals for the
explicit fixture-finalizer row. The canonical census and export classifier
are loaded from their source AST without importing the tests package, whose
initializer writes a scratch HOME. No tests, Git, probes or children run.

Any included delta outside the selected row's closed path set refuses before
writing. Existing excluded and unrelated entries, comments and source bytes
are retained. Row-owned argv shapes are derived, not hard-coded. The default
loaded-wallclock row retains its proof-runner/abrupt-diagnostic scope.
"""
from __future__ import annotations

import argparse
import ast
import collections
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from types import ModuleType, SimpleNamespace
from typing import Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
OWNER = "scripts/loaded-wallclock-proof.py"
OWNERS = frozenset({OWNER, "scripts/loaded-wallclock-abrupt-diagnostic.py"})
ROW_OWNERS = {
    "loaded-wallclock": OWNERS,
    "fixture-finalizer": frozenset({
        "floati/selftest.py",
        "scripts/fixture_finalizer.py",
        "scripts/test-zcode-headless-entry.py",
        "scripts/diagnose-checkout-cleanliness.py",
        "scripts/loaded-wallclock-isolation-diagnostic.py",
        "scripts/loaded-wallclock-proof.py",
        "scripts/loaded-wallclock-abrupt-diagnostic.py",
        "scripts/check-harbor-parallel-r2.py",
        "scripts/probe_bus_auto_rotate_live.py",
    }),
}
TARGET = "tests/test_no_listener_fence.py"
CLASSIFIER = "tests/export_inventory.py"
# The exporter re-exports these definitions from their canonical owner.
EXPORTER = "floati/public_export_policy.py"
METHODS = {
    "files": "test_built_argv_egress_is_enumerated_not_blind",
    "sites": "test_built_argv_egress_sites_are_pinned",
}


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def ordinary(relative):
    path = ROOT
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlinked source/destination: " + relative)
    return path


def extract(path, functions=(), classes=(), assignments=(), initial=None):
    """Execute exact named definitions, never a module's import/top-level code."""
    source = path.read_bytes()
    tree = ast.parse(source.decode("utf-8"), filename=str(path))
    selected = []
    found = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            selected.append(node)
            found.append(node.name)
        elif isinstance(node, ast.ClassDef) and node.name in classes:
            selected.append(node)
            found.append(node.name)
        elif isinstance(node, ast.Assign):
            names = [target.id for target in node.targets if isinstance(target, ast.Name)]
            matching = set(names) & set(assignments)
            if matching:
                selected.append(node)
                found.extend(sorted(matching))
    required = set(functions) | set(classes) | set(assignments)
    if set(found) != required or len(found) != len(required):
        raise ValueError("canonical definition set moved: " + str(path.relative_to(ROOT)))
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    program = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    # dataclass resolves postponed annotations through this temporary in-memory
    # module. Its functions retain their namespace after registration is removed.
    name = "_loaded_wallclock_pin_" + path.stem.replace("-", "_")
    module = ModuleType(name)
    module.__dict__.update(initial or {})
    module.__dict__.update(__file__=str(path), __name__=name)
    if name in sys.modules:
        raise ValueError("source reader module name already registered")
    sys.modules[name] = module
    try:
        exec(compile(program, str(path), "exec"), module.__dict__)
    finally:
        del sys.modules[name]
    return module, source, tree


def readers():
    fence, target, tree = extract(
        ordinary(TARGET),
        functions=("_parsed_source", "_parse_file", "_argv_expression", "_egress_call_resolver",
                   "_is_built_argv_node", "_built_argv_tree_egress_sites", "_built_argv_egress_sites",
                   "_built_argv_egress_files", "_subprocess_population"),
        assignments=("_NETWORK_SUBPROCESS_METHODS", "_OS_EGRESS_FUNCS", "_BUILT_ARGV_NODES"),
        initial={"ast": ast, "hashlib": hashlib, "Path": Path,
                 "lru_cache": lru_cache, "collections": collections},
    )
    exporter, _, _ = extract(
        ordinary(EXPORTER),
        functions=("_validate_relative", "classify_path"),
        classes=("ExportRefusal", "Classification", "ExportPolicy"),
        assignments=("POLICY_KEYS",),
        initial={"dataclass": dataclass, "json": json, "Path": Path,
                 "PurePosixPath": PurePosixPath, "Mapping": Mapping},
    )

    def source_import(name):
        if name != "scripts.export_public":
            raise ValueError("unreviewed canonical classifier import: " + name)
        return exporter

    classifier, _, _ = extract(
        ordinary(CLASSIFIER),
        functions=("declared_public_root", "assert_baseline_current", "published_baseline",
                   "export_policy_is_present", "classify_inventory", "classify_inventory_excluded"),
        assignments=("REPOSITORY_ROOT", "EXPORTER_RELATIVE", "POLICY_RELATIVE",
                     "BASELINE_RELATIVE", "REGENERATOR_MODULE", "PUBLIC_ROOT_ENV"),
        initial={"Path": Path, "os": os, "json": json,
                 "importlib": SimpleNamespace(import_module=source_import),
                 "Iterable": Iterable, "Sequence": Sequence},
    )
    if classifier.declared_public_root() is not None:
        raise ValueError("declared public-root freshness requires an external Git observation; this row-only source regenerator never launches it")
    if not classifier.export_policy_is_present(ROOT):
        raise ValueError("export policy absent: row-only regeneration requires the harbor classifier")
    return fence, classifier, target, tree


def pin_assignment(tree, method):
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "WholeProductNoListenerFenceTests"]
    if len(classes) != 1:
        raise ValueError("public fence class must have one exact owner")
    methods = [node for node in classes[0].body if isinstance(node, ast.FunctionDef)
               and node.name == method]
    if len(methods) != 1:
        raise ValueError("public fence method must have one exact owner: " + method)
    assignments = [node for node in methods[0].body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == "pinned"]
    if len(assignments) != 1 or not isinstance(assignments[0].value, ast.Set):
        raise ValueError("public pin must have one literal set assignment: " + method)
    assignment = assignments[0]
    values = ast.literal_eval(assignment.value)
    if len(values) != len(assignment.value.elts):
        raise ValueError("duplicate public pin entries: " + method)
    return assignment, values


def validate_pin(kind, values):
    for value in values:
        path = value if kind == "files" else value[0] if isinstance(value, tuple) and len(value) == 2 else None
        if (not isinstance(path, str) or PurePosixPath(path).is_absolute()
                or ".." in PurePosixPath(path).parts or PurePosixPath(path).as_posix() != path):
            raise ValueError("noncanonical public pin path")
        if kind == "sites" and (not isinstance(value[1], str) or not re.fullmatch(r"[0-9a-f]{16}", value[1])):
            raise ValueError("noncanonical public site digest")


def entry_path(value):
    return value if isinstance(value, str) else value[0]


def owned_literal(source, assignment, wanted, owners=None):
    """Retain all non-owner bytes; change only this row's literal elements."""
    if owners is None:
        owners = OWNERS
    node = assignment.value
    current = ast.literal_eval(node)
    if current == wanted:
        return None
    if {value for value in current if entry_path(value) not in owners} != {
            value for value in wanted if entry_path(value) not in owners}:
        raise ValueError("refusing non-owner pin replacement")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    start = offsets[node.lineno - 1] + node.col_offset
    end = offsets[node.end_lineno - 1] + node.end_col_offset
    literal = source[start:end].splitlines(keepends=True)
    if not literal or not re.fullmatch(rb"[ \t]*}", literal[-1]):
        raise ValueError("public literal closing delimiter moved")
    remove = set()
    for element in node.elts:
        value = ast.literal_eval(element)
        if entry_path(value) not in owners or value in wanted:
            continue
        if element.lineno != element.end_lineno:
            raise ValueError("reviewed row pin is not a single-line element")
        whole = lines[element.lineno - 1]
        if whole[:element.col_offset].strip() or whole[element.end_col_offset:].strip() not in (b"", b","):
            raise ValueError("reviewed row pin shares source with another value/comment")
        remove.add(element.lineno - node.lineno)
    kept = [line for index, line in enumerate(literal[:-1]) if index not in remove]
    newline = b"\r\n" if b"\r\n" in source else b"\n"
    indent = b" " * (assignment.col_offset + 4)
    for value in sorted(wanted - current):
        rendered = json.dumps(value) if isinstance(value, str) else "(" + ", ".join(json.dumps(part) for part in value) + ")"
        kept.append(indent + rendered.encode("utf-8") + b"," + newline)
    return start, end, b"".join([*kept, literal[-1]])


def population_literals(tree):
    """Locate only the two direct-population expectation dictionaries."""
    methods = [node for owner in tree.body if isinstance(owner, ast.ClassDef)
               and owner.name == "WholeProductNoListenerFenceTests" for node in owner.body
               if isinstance(node, ast.FunctionDef) and node.name == "test_subprocess_population_is_pinned"]
    if len(methods) != 1:
        raise ValueError("direct population pin method moved")
    calls = [statement.value for statement in methods[0].body if isinstance(statement, ast.Expr)
             and isinstance(statement.value, ast.Call)]
    values = [call.args[0] for call in calls
              if isinstance(call.func, ast.Attribute) and call.func.attr == "assertEqual"
              and call.args and isinstance(call.args[0], ast.Dict)]
    if len(values) != 2:
        raise ValueError("direct population pin dictionaries moved")
    result = dict(zip(("kinds", "argv"), values))
    for value in result.values():
        literal = ast.literal_eval(value)
        if (len(literal) != len(value.keys) or not literal
                or any(not isinstance(key, str) or not key or type(count) is not int or count < 1
                       for key, count in literal.items())):
            raise ValueError("malformed direct population pin")
    return result


def population_replacements(source, tree, fence, owners):
    """Permit only count-preserving native-to-owned substitutions in this row.

    Helpers remain distinct kinds in the resulting pin. Normalizing each
    owned kind to the native API it replaces must reproduce the prior pinned
    population and argv distribution. An unrelated native count change or a
    helper outside this row refuses rather than being absorbed by regeneration.
    """
    nodes = population_literals(tree)
    pinned = {kind: ast.literal_eval(node) for kind, node in nodes.items()}
    counters = {kind: collections.Counter() for kind in nodes}
    snapshots = {}
    for path in sorted((ROOT / "floati").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        payload = ordinary(relative).read_bytes()
        snapshots[relative] = sha256(payload)
        kinds, argv = fence._subprocess_population(ast.parse(payload.decode("utf-8"), filename=relative))
        if {"owned_child", "run_child"}.intersection(kinds) and relative not in owners:
            raise ValueError("owned egress population outside reviewed row: " + relative)
        counters["kinds"].update(kinds)
        counters["argv"].update(argv)

    def native_counts(values):
        result = collections.Counter()
        for kind, count in values.items():
            result[{"owned_child": "Popen", "run_child": "run"}.get(kind, kind)] += count
        return dict(result)

    if native_counts(counters["kinds"]) != native_counts(pinned["kinds"]):
        raise ValueError("direct native population changed beyond reviewed owned-call substitutions")
    if dict(counters["argv"]) != pinned["argv"]:
        raise ValueError("direct argv distribution changed beyond reviewed owned-call substitutions")
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    replacements = []
    derived = {kind: dict(sorted(counter.items())) for kind, counter in counters.items()}
    for kind, node in nodes.items():
        if pinned[kind] == derived[kind]:
            continue
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        replacements.append((start, end, json.dumps(derived[kind], sort_keys=True).encode("utf-8")))
    return replacements, {"pinned": pinned, "derived": derived}, snapshots


def regenerate(write, row="loaded-wallclock"):
    if row not in ROW_OWNERS:
        raise ValueError("unreviewed row")
    owners = ROW_OWNERS[row]
    fence, classifier, original, tree = readers()
    reviewed_sha = {path: sha256(ordinary(path).read_bytes()) for path in sorted(owners)}
    raw_sites = fence._built_argv_egress_sites([ROOT / "floati", ROOT / "scripts"])
    raw_files = fence._built_argv_egress_files([ROOT / "floati", ROOT / "scripts"])
    sites = {(Path(path).relative_to(ROOT).as_posix(), value) for path, value in raw_sites}
    files = {Path(path).relative_to(ROOT).as_posix() for path in raw_files}
    if files != {path for path, _ in sites}:
        raise ValueError("canonical file/site census changed during observation")
    assignments = {}
    pinned = {}
    for kind, method in METHODS.items():
        assignments[kind], pinned[kind] = pin_assignment(tree, method)
        validate_pin(kind, pinned[kind])
    population = files | pinned["files"] | {path for path, _ in pinned["sites"]}
    included = set(classifier.classify_inventory(sorted(population), root=ROOT))
    if row == "loaded-wallclock" and set(classifier.classify_inventory(sorted(owners), root=ROOT)) != owners:
        raise ValueError("a reviewed row-owned file no longer classifies as included")
    if row == "fixture-finalizer":
        private_path = ".github/built-argv-private-pin.v0.txt"
        private_pin = {line.strip() for line in ordinary(private_path).read_text(encoding="utf-8").splitlines()
                       if line.strip()}
        validate_pin("files", private_pin)
        excluded = set(classifier.classify_inventory_excluded(sorted(files | private_pin), root=ROOT))
        private_delta = {"added": sorted((files & excluded) - (private_pin & excluded)),
                         "removed": sorted((private_pin & excluded) - (files & excluded))}
        if any(private_delta.values()):
            raise ValueError("excluded private pin drift requires its own regenerator: "
                             + json.dumps({"path": private_path, "delta": private_delta}, sort_keys=True))
    measured = {"files": files & included, "sites": {site for site in sites if site[0] in included}}
    old = {"files": pinned["files"] & included,
           "sites": {site for site in pinned["sites"] if site[0] in included}}
    delta = {kind: {"added": sorted(measured[kind] - old[kind]),
                    "removed": sorted(old[kind] - measured[kind])} for kind in METHODS}
    outside = sorted({entry_path(value) for kind in METHODS
                      for value in measured[kind] ^ old[kind] if entry_path(value) not in owners})
    if outside:
        raise ValueError("included census delta outside reviewed row-owned paths: " + repr(outside))
    desired = {kind: (pinned[kind] - old[kind]) | measured[kind] for kind in METHODS}
    replacements = [replacement for kind in METHODS
                    if (replacement := owned_literal(original, assignments[kind], desired[kind], owners)) is not None]
    population = None
    population_sources = {}
    if row == "fixture-finalizer":
        more, population, population_sources = population_replacements(original, tree, fence, owners)
        replacements.extend(more)
    wanted = original
    for start, end, replacement in sorted(replacements, reverse=True):
        wanted = wanted[:start] + replacement + wanted[end:]
    reparsed = ast.parse(wanted.decode("utf-8"), filename=TARGET)
    if any(pin_assignment(reparsed, METHODS[kind])[1] != desired[kind] for kind in METHODS):
        raise ValueError("generated public literal does not match canonical measurements")
    if population is not None and any(ast.literal_eval(node) != population["derived"][kind]
                                      for kind, node in population_literals(reparsed).items()):
        raise ValueError("generated population literal does not match canonical measurements")
    if ordinary(TARGET).read_bytes() != original or any(
            sha256(ordinary(path).read_bytes()) != value for path, value in reviewed_sha.items()):
        raise ValueError("owned source changed during observation; retry at frozen source")
    if population_sources and (
            set(population_sources) != {path.relative_to(ROOT).as_posix()
                                        for path in (ROOT / "floati").rglob("*.py")}
            or any(sha256(ordinary(path).read_bytes()) != value
                   for path, value in population_sources.items())):
        raise ValueError("population source changed during observation; retry at frozen source")
    changed = wanted != original
    if write and changed:
        ordinary(TARGET).write_bytes(wanted)
    return {
        "status": "written" if write and changed else "drift" if changed else "ok",
        "row": row, "reviewed_paths": sorted(owners), "reviewed_source_sha256": reviewed_sha,
        "target": TARGET, "target_before_sha256": sha256(original),
        "target_derived_sha256": sha256(wanted), "delta": delta,
        "included_counts": {kind: {"pinned": len(old[kind]), "derived": len(measured[kind])}
                            for kind in METHODS},
        "excluded_pinned_entries_preserved": {kind: len(pinned[kind] - old[kind]) for kind in METHODS},
        "direct_population": population,
        "script_sha256": sha256(Path(__file__).read_bytes()),
    }, changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--row", choices=tuple(ROW_OWNERS), default="loaded-wallclock")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true", help="compare only (default)")
    args = parser.parse_args()
    try:
        result, changed = regenerate(args.write, args.row)
    except (OSError, ValueError, RuntimeError, SyntaxError) as exc:
        print(json.dumps({"status": "refused", "code": "loaded_wallclock_public_pin_scope",
                          "detail": str(exc)}, sort_keys=True))
        return 20
    print(json.dumps(result, sort_keys=True))
    return 0 if args.write or not changed else 1


if __name__ == "__main__":
    raise SystemExit(main())

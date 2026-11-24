#!/usr/bin/env python3
"""Prepare, cut and check the complete ledger contract without a hand member list.

This is repository tooling, not a Floati verb. Discovery selects discriminator
readers/writers, validator definitions and their code dependencies, never whole
runtime modules merely because they contain a record writer. All governed
schemas remain byte pinned. The kind index is conservative; computed expressions
are protected by source slices even when their values cannot be enumerated.

Exit codes: 0 = success; 2 = argparse usage error; 20 = this script's typed
contract, document or I/O refusal (not the product adapter/root exit contract).
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from floati.identity_fence import OWNER_USERNAME

PREPARATION = "tests/fixtures/ledger-contract-preparation.json"
CURRENT = "bundle/ledger-contract-current.json"
VERSION = re.compile(r"c7\.(0|[1-9][0-9]*)\Z")
SOURCE_PIN = "python-contract-slice-owner-neutral-v2"
ANNOTATIONS = {"title", "description", "$comment", "examples", "default"}
LOWER_BOUNDS = {"minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties"}
UPPER_BOUNDS = {"maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties"}


class ContractRefusal(Exception):
    def __init__(self, code, detail, **evidence):
        super().__init__(detail)
        self.code, self.detail = code, detail
        self.evidence = evidence


def refuse(code, detail, **evidence):
    raise ContractRefusal("ledger_contract_" + code, detail, **evidence)


def member_diff(before, after):
    """Bound member names, retaining exact totals relative to the pinned census."""
    result = {"limit": 50}
    for section in ("kinds", "schemas", "sources"):
        old = before.get(section, {}) if isinstance(before, dict) else {}
        new = after.get(section, {}) if isinstance(after, dict) else {}
        old = old if isinstance(old, dict) else {}
        new = new if isinstance(new, dict) else {}
        changes = {
            "added": sorted(new.keys() - old.keys()),
            "removed": sorted(old.keys() - new.keys()),
            "changed": sorted(name for name in old.keys() & new.keys()
                              if old[name] != new[name]),
        }
        result[section] = {key: names[:50] for key, names in changes.items()}
        result[section]["counts"] = {key: len(names) for key, names in changes.items()}
    return result


def schema_drift(members, name, data):
    # Preserve the bytes actually observed even if a racing writer changes back.
    observed = copy.deepcopy(members)
    observed["schemas"][name] = sha(data)
    return member_diff(members, observed)


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def source_tree(data, relative):
    try:
        tree = ast.parse(data, filename=relative)
    except (SyntaxError, UnicodeError) as exc:
        refuse("source_invalid", str(exc))

    class NeutralizeOwner(ast.NodeTransformer):
        def visit_Constant(self, node):
            if isinstance(node.value, str):
                node.value = node.value.replace(OWNER_USERNAME, "<operator>").replace(
                    "penguinspecz", "<operator>",
                )
            return node

    return NeutralizeOwner().visit(tree)


def source_fingerprint(tree):
    # Do not hash ast.dump's representation: newer Python releases add empty
    # optional fields (for example type_params) to unchanged syntax.
    def structural(value):
        if isinstance(value, ast.AST):
            return {"node": type(value).__name__, "fields": {
                key: structural(item) for key, item in ast.iter_fields(value)
                if item is not None and item != []
            }}
        if isinstance(value, list):
            return [structural(item) for item in value]
        if isinstance(value, bytes):
            return {"bytes": value.hex()}
        if isinstance(value, complex):
            return {"complex": [value.real, value.imag]}
        if value is Ellipsis:
            return {"ellipsis": True}
        return value
    return sha(canonical(structural(tree)))


def ordinary(root, relative):
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or path.as_posix() != relative
            or any(part in {".", ".."} for part in path.parts)):
        refuse("path_invalid", str(relative))
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            refuse("path_invalid", relative)
    if not current.is_file():
        refuse("path_invalid", relative)
    return current


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        refuse("document_invalid", f"{path.name}: {exc}")


def files_below(root, relative, suffix):
    directory = root / relative
    if not directory.is_dir() or directory.is_symlink():
        refuse("path_invalid", relative)
    result = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            refuse("path_invalid", path.relative_to(root).as_posix())
        if path.is_file() and path.suffix == suffix:
            result.append(path.relative_to(root).as_posix())
    return result


def string_values(node, constants):
    """Finite constants; unresolved expressions remain protected by source slices."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        return constants.get(node.id, set())
    if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
        return set().union(*(string_values(item, constants) for item in node.elts))
    if isinstance(node, ast.Dict):
        return set().union(*(string_values(key, constants) for key in node.keys if key))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in {"set", "frozenset", "tuple", "list"} and node.args:
            return string_values(node.args[0], constants)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = string_values(node.left, constants), string_values(node.right, constants)
        if isinstance(node.left, (ast.Set, ast.List, ast.Tuple)):
            return left | right
        return {a + b for a in left for b in right}
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return string_values(node.left, constants) | string_values(node.right, constants)
    if isinstance(node, ast.IfExp):
        return string_values(node.body, constants) | string_values(node.orelse, constants)
    if isinstance(node, ast.JoinedStr):
        result = {""}
        for item in node.values:
            if isinstance(item, ast.FormattedValue):
                if item.conversion != -1 or item.format_spec is not None:
                    return set()
                values = string_values(item.value, constants)
            else:
                values = string_values(item, constants)
            result = {left + right for left in result for right in values}
        return result
    return set()


def contract_seed(node):
    """Recognize syntax, not a maintained registry of record kinds or modules."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if "validat" in node.name.lower():
            return True
    for item in ast.walk(node):
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
            if item.id.upper().endswith(("_KIND", "_KINDS", "_SPECS")):
                return True
        if isinstance(item, ast.Dict) and any(
            isinstance(key, ast.Constant) and key.value in {"kind", "event"}
            for key in item.keys
        ):
            return True
        if (isinstance(item, ast.Subscript) and isinstance(item.slice, ast.Constant)
                and item.slice.value in {"kind", "event"}):
            return True
        if (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                and item.func.attr in {"get", "setdefault", "pop", "update"} and item.args
                and isinstance(item.args[0], ast.Constant) and item.args[0].value in {"kind", "event"}):
            return True
        if isinstance(item, ast.Call) and any(
            keyword.arg in {"kind", "event"} for keyword in item.keywords
        ):
            return True
    return False


def contract_sources(trees):
    """Forward dependency closure from contract nodes, without pinning callers.

    A computed kind is guarded as executable syntax plus its referenced helper
    and constant definitions, including imports. No finite-name resolution is
    required for completeness at a discovered discriminator site. Functions
    are the conservative unit: changing their validation logic requires a cut;
    adding an unrelated sibling helper does not.
    """
    units, bindings, imports, selected, class_bindings = {}, {}, {}, set(), {}
    modules = {name[:-3].replace("/", "."): name for name in trees}
    modules.update({name[:-12].replace("/", "."): name for name in trees
                    if name.endswith("/__init__.py")})

    def import_bindings(relative, node):
        module = relative[:-3].replace("/", ".")
        package = module.split(".")[:-1]
        found = {}
        for item in ast.walk(node):
            if isinstance(item, ast.ImportFrom):
                parts = package[:len(package) - item.level + 1] if item.level else []
                target = ".".join(parts + ([item.module] if item.module else []))
                for alias in item.names:
                    found[alias.asname or alias.name] = (target, alias.name)
            elif isinstance(item, ast.Import):
                for alias in item.names:
                    # An unaliased dotted import binds its first component.
                    found[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0], None,
                    )
        return found

    for relative, tree in trees.items():
        bindings[relative] = {}
        imports[relative] = import_bindings(relative, tree)
        sequence = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                header = copy.copy(node)
                header.body = []
                sequence.append((header, None))
                sequence.extend((member, node.name) for member in node.body)
            else:
                sequence.append((node, None))
        for ordinal, (node, owner) in enumerate(sequence):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # module documentation is not a validator
            names = set()
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            else:
                names.update(item.id for item in ast.walk(node)
                             if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store))
            # Ordinals identify graph nodes only, and never enter the pin.
            key = (relative, ordinal)
            units[key] = node
            node._contract_owner = owner
            for name in names:
                if owner:
                    class_bindings.setdefault((relative, owner, name), set()).add(key)
                else:
                    bindings[relative].setdefault(name, set()).add(key)
            if contract_seed(node):
                selected.add(key)

    def resolve(relative, name, seen=None):
        seen = set() if seen is None else seen
        if (relative, name) in seen:
            return set()
        seen.add((relative, name))
        found = set(bindings[relative].get(name, ()))
        imported = imports[relative].get(name)
        if imported:
            target, symbol = imported
            if target in modules and symbol:
                found.update(resolve(modules[target], symbol, seen))
        return found

    # A generic writer can receive its discriminator from a caller with no
    # literal kind/event syntax. Follow that argument flow, pinning call/input
    # slices rather than the caller's unrelated instrumentation or output.
    for relative, slices in discriminator_call_slices(trees, modules, imports, resolve, units).items():
        start = 1 + max((key[1] for key in units if key[0] == relative), default=-1)
        for ordinal, node in enumerate(slices, start=start):
            key = (relative, ordinal)
            units[key] = node
            selected.add(key)

    pending = list(selected)
    while pending:
        relative, ordinal = pending.pop()
        node = units[(relative, ordinal)]
        local_imports = import_bindings(relative, node)
        namespace = {**imports[relative], **local_imports}
        dependencies = set()
        owner = getattr(node, "_contract_owner", None)
        if owner:
            dependencies.update(bindings[relative].get(owner, ()))
        for item in ast.walk(node):
            if not isinstance(item, ast.Name) or not isinstance(item.ctx, ast.Load):
                continue
            dependencies.update(resolve(relative, item.id))
            if item.id in namespace:
                target, symbol = namespace[item.id]
                path = modules.get(target)
                if path and symbol:
                    dependencies.update(resolve(path, symbol))
        # Module-qualified imports (including aliases) are dependencies too.
        for item in ast.walk(node):
            if isinstance(item, ast.Attribute):
                parts, base = [], item
                while isinstance(base, ast.Attribute):
                    parts.insert(0, base.attr)
                    base = base.value
                if isinstance(base, ast.Name) and base.id in namespace:
                    target, symbol = namespace[base.id]
                    prefix = target + ("." + symbol if symbol else "")
                    path = modules.get(".".join([prefix] + parts[:-1]))
                    if path:
                        dependencies.update(resolve(path, parts[-1]))
            if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name):
                if owner and item.value.id in {"self", "cls"}:
                    dependencies.update(class_bindings.get((relative, owner, item.attr), ()))
                dependencies.update(class_bindings.get((relative, item.value.id, item.attr), ()))
                imported = namespace.get(item.value.id)
                if imported:
                    target, symbol = imported
                    path = modules.get(target + "." + symbol) if symbol else modules.get(target)
                    if path:
                        dependencies.update(resolve(path, item.attr))
                    if symbol and target in modules:
                        dependencies.update(class_bindings.get((modules[target], symbol, item.attr), ()))
        for dependency in dependencies - selected:
            selected.add(dependency)
            pending.append(dependency)

    sources = {}
    for relative in trees:
        fingerprints = sorted(sha(canonical({
            "owner": getattr(units[key], "_contract_owner", None),
            "node": source_fingerprint(units[key]),
        })) for key in selected if key[0] == relative)
        if fingerprints:
            sources[relative] = sha(canonical(fingerprints))
    return sources


def discriminator_call_slices(trees, modules, imports, resolve, units):
    """Trace parameters and their reaching assignments into discriminator sites.

    The fixed point handles wrappers/reexports without making the wrappers
    themselves frozen functions. Slices retain controlling tests as well as
    assignment values; changing a branch that chooses a kind is contract drift.
    """
    functions, by_node, by_name = {}, {}, {}
    for relative, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = (relative, id(node))
                functions[key] = node
                by_node[id(node)] = key
                by_name.setdefault(node.name, set()).add(key)
        # Module-level calls can introduce a discriminator just as a function
        # body can. Give them an empty parameter scope for the same flow pass.
        module_scope = ast.FunctionDef(
            name="<module>", args=ast.arguments(posonlyargs=[], args=[], vararg=None,
            kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]),
            body=[node for node in tree.body if not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))],
            decorator_list=[], returns=None,
        )
        functions[(relative, id(module_scope))] = module_scope

    load_cache = {}

    def loads(node):
        if id(node) not in load_cache:
            load_cache[id(node)] = {item.id for item in ast.walk(node)
                                   if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)}
        return load_cache[id(node)]

    def key_access(node):
        return ((isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant)
                 and node.slice.value in {"kind", "event"}) or
                (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == "get" and node.args
                 and isinstance(node.args[0], ast.Constant)
                 and node.args[0].value in {"kind", "event"}))

    def calls(relative, expression):
        if isinstance(expression, ast.Name):
            candidates = resolve(relative, expression.id)
            targets = {by_node[id(units[key])] for key in candidates if id(units[key]) in by_node}
            if not targets and not candidates:
                targets.update(key for key in by_name.get(expression.id, ()) if key[0] == relative)
            return targets
        elif isinstance(expression, ast.Attribute):
            # Resolve module-qualified calls. A method with the same spelling
            # elsewhere is not evidence about this receiver (notably dict.get).
            parts, base = [], expression
            while isinstance(base, ast.Attribute):
                parts.insert(0, base.attr)
                base = base.value
            candidates = set()
            if isinstance(base, ast.Name):
                path = modules.get(".".join([base.id] + parts[:-1]))
                imported = imports[relative].get(base.id)
                if imported:
                    module, symbol = imported
                    prefix = module + ("." + symbol if symbol else "")
                    path = modules.get(".".join([prefix] + parts[:-1]))
                if path:
                    candidates.update(resolve(path, parts[-1]))
        else:
            return set()
        return {by_node[id(units[key])] for key in candidates if id(units[key]) in by_node}

    assignments, roots, sensitive, call_graph, data_receivers = {}, {}, {}, {}, {}
    for key, function in functions.items():
        mapping, expressions = {}, []
        discriminator_names = {
            target.id for node in ast.walk(function) if isinstance(node, ast.Assign)
            and key_access(node.value) for target in node.targets if isinstance(target, ast.Name)
        }

        def discriminator(node):
            return key_access(node) or isinstance(node, ast.Name) and node.id in discriminator_names

        def visit(node, controls=()):
            if node is not function and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                return
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for item in ast.walk(target):
                        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                            mapping.setdefault(item.id, []).append((node, controls))
                    if key_access(target) and node.value is not None:
                        expressions.append(node.value)
            if isinstance(node, ast.Dict):
                expressions.extend(value for name, value in zip(node.keys, node.values)
                                   if isinstance(name, ast.Constant) and name.value in {"kind", "event"})
            if isinstance(node, ast.Call):
                expressions.extend(keyword.value for keyword in node.keywords
                                   if keyword.arg in {"kind", "event"})
            if isinstance(node, ast.Compare):
                operands = [node.left, *node.comparators]
                if any(discriminator(part) for part in operands):
                    # Record values are data, not new accepted-kind definitions.
                    # Trace the accepted-value side, not the caller's whole row.
                    expressions.extend(part for part in operands if not discriminator(part))
            next_controls = controls + (node.test,) if isinstance(node, (ast.If, ast.While, ast.IfExp)) else controls
            for child in ast.iter_child_nodes(node):
                visit(child, next_controls)

        visit(function)
        assignments[key], roots[key], sensitive[key] = mapping, expressions, set()
        data_receivers[key] = [node.value if isinstance(node, ast.Subscript) else node.func.value
                               for node in ast.walk(function) if key_access(node)]
        call_graph[key] = [(node, calls(key[0], node.func)) for node in ast.walk(function)
                           if isinstance(node, ast.Call)]

    expansion_cache = {}

    def expand(key, expressions):
        initial = frozenset().union(*(loads(node) for node in expressions))
        cache_key = (key, initial)
        if cache_key in expansion_cache:
            return expansion_cache[cache_key]
        names, statements = set(initial), {}
        pending = list(names)
        while pending:
            name = pending.pop()
            for statement, controls in assignments[key].get(name, ()):
                statements[id(statement)] = statement
                for control in controls:
                    statements[id(control)] = control
                more = loads(statement) | set().union(*(loads(node) for node in controls))
                pending.extend(more - names)
                names.update(more)
        result = names, sorted(statements.values(), key=lambda node: (
            getattr(node, "lineno", 0), getattr(node, "col_offset", 0), type(node).__name__,
        ))
        expansion_cache[cache_key] = result
        return result

    def arguments(call, target):
        function = functions[target]
        positional = function.args.posonlyargs + function.args.args
        if (isinstance(call.func, ast.Attribute) and positional
                and positional[0].arg in {"self", "cls"}):
            positional = positional[1:]
        values = {param.arg: value for param, value in zip(positional, call.args)}
        values.update({keyword.arg: keyword.value for keyword in call.keywords if keyword.arg})
        # Starred/keyword-unpacked calls cannot be statically bound: retain the
        # entire argument expression as testimony instead of silently omitting it.
        unknown = [arg for arg in call.args if isinstance(arg, ast.Starred)]
        unknown.extend(keyword.value for keyword in call.keywords if keyword.arg is None)
        return [value for name, value in values.items() if name in sensitive[target]] + unknown

    changed = True
    while changed:
        changed = False
        for key, function in functions.items():
            expressions = list(roots[key])
            for call, targets in call_graph[key]:
                for target in targets:
                    expressions.extend(arguments(call, target))
            names, _ = expand(key, expressions)
            parameters = {arg.arg for arg in function.args.posonlyargs + function.args.args + function.args.kwonlyargs}
            data_names, _ = expand(key, data_receivers[key])
            # Correlations within an input row do not make all callers' row
            # values new discriminator definitions. The validator body itself
            # remains pinned; only accepted-kind arguments flow backwards.
            more = names & parameters - data_names - sensitive[key]
            if more:
                sensitive[key].update(more)
                changed = True

    result = {}
    for key, function in functions.items():
        slices = result.setdefault(key[0], [])
        for call, targets in call_graph[key]:
            for target in targets:
                values = arguments(call, target) if sensitive[target] else []
                if values:
                    _, statements = expand(key, values)
                    # The callee's discriminator flow is already represented
                    # by its own slices. Do not select its unrelated body again
                    # through a reverse caller dependency.
                    edges = []
                    for name in loads(call.func):
                        edges.extend(units[binding] for binding in resolve(key[0], name)
                                     if isinstance(units[binding], (ast.Import, ast.ImportFrom)))
                    slices.append(ast.Tuple(elts=[ast.Constant(source_fingerprint(call.func)),
                                                  *sorted(edges, key=source_fingerprint),
                                                  *values, *statements], ctx=ast.Load()))
        if sensitive[key]:
            defaults = list(zip(function.args.args[-len(function.args.defaults):], function.args.defaults)) if function.args.defaults else []
            defaults += list(zip(function.args.kwonlyargs, function.args.kw_defaults))
            for parameter, value in defaults:
                if parameter.arg in sensitive[key] and value is not None:
                    slices.append(ast.Tuple(elts=[ast.Constant(parameter.arg), value], ctx=ast.Load()))
    return result


def kind_census(tree):
    constants = {}
    assignments = [node for node in ast.walk(tree) if isinstance(node, (ast.Assign, ast.AnnAssign))]
    # References may precede their declarations. The finite sets converge.
    for _ in range(len(assignments) + 1):
        changed = False
        for node in assignments:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    values = string_values(node.value, constants)
                    if values - constants.get(target.id, set()):
                        constants.setdefault(target.id, set()).update(values)
                        changed = True
        if not changed:
            break
    kinds = set()
    for name, values in constants.items():
        if name.upper().endswith(("_KIND", "_KINDS", "_SPECS")):
            kinds.update(values)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "kind":
                    kinds.update(string_values(keyword.value, constants))
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "kind":
                    kinds.update(string_values(value, constants))
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any((isinstance(value, ast.Subscript) and isinstance(value.slice, ast.Constant)
                    and value.slice.value == "kind") or
                   (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "get" and value.args
                    and isinstance(value.args[0], ast.Constant) and value.args[0].value == "kind")
                   for value in operands):
                for value in operands:
                    kinds.update(string_values(value, constants))
    return sorted(value for value in kinds if value and len(value) <= 128)


def schema_references(document):
    if isinstance(document, dict):
        for key, value in document.items():
            if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                yield value
            else:
                yield from schema_references(value)
    elif isinstance(document, list):
        for value in document:
            yield from schema_references(value)


def inventory(root):
    root = Path(root).resolve()
    schemas, kinds, trees = {}, {}, {}
    for relative in files_below(root, "floati", ".py"):
        data = ordinary(root, relative).read_bytes()
        tree = source_tree(data, relative)
        trees[relative] = tree
        for kind in kind_census(tree):
            kinds.setdefault(kind, []).append(relative)
    sources = contract_sources(trees)
    for relative in files_below(root, "schemas", ".json"):
        schemas[relative] = sha(ordinary(root, relative).read_bytes())
    for relative in schemas:
        document = read_json(root / relative)
        for reference in schema_references(document):
            if not isinstance(reference, str):
                refuse("schema_reference_invalid", relative)
            parsed = urlsplit(reference)
            # No network resolution and no unpinned external schema dependency.
            if parsed.scheme or parsed.netloc or parsed.query:
                refuse("schema_reference_invalid", f"{relative}: {reference}")
            target = (root / relative).parent / unquote(parsed.path)
            if not parsed.path:
                target = root / relative
            try:
                name = target.resolve().relative_to(root).as_posix()
            except ValueError:
                refuse("schema_reference_invalid", relative)
            if name not in schemas:
                refuse("schema_reference_invalid", f"{relative}: {reference}")
            if parsed.fragment:
                value = read_json(root / name)
                try:
                    fragment = unquote(parsed.fragment)
                    if not fragment.startswith("/"):
                        raise ValueError("only JSON pointers are supported")
                    for token in fragment[1:].split("/"):
                        value = value[token.replace("~1", "/").replace("~0", "~")]
                except (ValueError, KeyError, TypeError):
                    refuse("schema_reference_invalid", f"{relative}: {reference}")
    return {"sources": sources, "schemas": schemas, "kinds": dict(sorted(kinds.items()))}


def versions(root):
    directory = root / "bundle"
    if directory.is_symlink():
        refuse("path_invalid", "bundle")
    result = []
    if directory.exists():
        for child in directory.iterdir():
            if VERSION.fullmatch(child.name):
                if child.is_symlink() or not child.is_dir():
                    refuse("path_invalid", "bundle/" + child.name)
                result.append((int(child.name.split(".")[1]), child.name))
    return [name for _, name in sorted(result)]


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False,
                               allow_nan=False).encode("utf-8") + b"\n")


@contextmanager
def mutation_lock(root):
    directory = root / "bundle"
    if directory.is_symlink() or not directory.is_dir():
        refuse("path_invalid", "bundle")
    lock = directory / ".ledger-contract.lock"
    try:
        descriptor = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        refuse("busy", "another contract operation owns bundle/.ledger-contract.lock")
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink()


def publish(root, version, stage, *, replace=False):
    """Publish a completely validated directory and pointer, with owned rollback."""
    destination = root / "bundle" / version
    pointer = root / CURRENT
    preparation = root / PREPARATION
    if pointer.is_symlink():
        refuse("path_invalid", CURRENT)
    previous_pointer = pointer.read_bytes() if pointer.exists() else None
    previous_preparation = None
    if not replace:
        # The first cut retires preparation. Removing a cut and its pointer
        # together must not make the tree claim it was never cut.
        current = root
        for part in PurePosixPath(PREPARATION).parts:
            current = current / part
            if current.is_symlink():
                refuse("path_invalid", PREPARATION)
        if preparation.exists():
            previous_preparation = ordinary(root, PREPARATION).read_bytes()
    backup_parent = Path(tempfile.mkdtemp(prefix=".ledger-rollback-", dir=root / "bundle"))
    backup = backup_parent / version
    published = False
    committed = False
    try:
        if replace:
            destination.rename(backup)
        elif destination.exists() or destination.is_symlink():
            refuse("version_exists", version)
        try:
            stage.rename(destination)
            published = True
            staged_pointer = backup_parent / "current.json"
            write_json(staged_pointer, {"version": version, "sha256": sha(
                (destination / "ledger-contract.json").read_bytes(),
            )})
            staged_pointer.replace(pointer)
            if not replace:
                staged_preparation = backup_parent / "preparation.json"
                write_json(staged_preparation, {"state": "cut", "version": version})
                preparation.parent.mkdir(parents=True, exist_ok=True)
                staged_preparation.replace(preparation)
            committed = True
        except BaseException:
            if published:
                shutil.rmtree(destination)
            if backup.exists():
                backup.rename(destination)
            if previous_pointer is not None:
                pointer.write_bytes(previous_pointer)
            elif pointer.exists():
                pointer.unlink()
            if not replace:
                if previous_preparation is not None:
                    preparation.write_bytes(previous_preparation)
                elif preparation.exists():
                    preparation.unlink()
            raise
    except OSError as exc:
        refuse("publication_failed", f"{exc}; rollback location: {backup_parent.name}")
    finally:
        if committed or not backup.exists():
            shutil.rmtree(backup_parent)


def save_contract(directory, document):
    write_json(directory / "ledger-contract.json", document)
    (directory / "ledger-contract.sha256").write_text(
        sha((directory / "ledger-contract.json").read_bytes()) + "\n", encoding="ascii",
    )


def reviewed_notes(notes):
    """Validate explicit rulings separately from schema-loosening audits."""
    if (not isinstance(notes, list) or any(
        not isinstance(note, dict) or set(note) != {"row", "decision", "basis"}
        or any(not isinstance(value, str) or not value.strip() for value in note.values())
        for note in notes
    )):
        refuse("document_invalid", "ratification notes require row, decision and basis strings")
    return notes


def preparation_notes(root):
    path = root / PREPARATION
    if not path.exists() and not path.is_symlink():
        return []
    document = read_json(ordinary(root, PREPARATION))
    if isinstance(document, dict) and document.get("state") == "prepared_not_cut":
        return reviewed_notes(document.get("ratification_notes", []))
    return []


def cut(root, version, pins):
    root = Path(root).resolve()
    with mutation_lock(root):
        return _cut(root, version, pins)


def _cut(root, version, pins):
    existing = versions(root)
    previous_cut = current_version(root)
    if previous_cut:
        load_contract(root, previous_cut)
    if (not VERSION.fullmatch(version) or not existing
            or int(version.split(".")[1]) != int(existing[-1].split(".")[1]) + 1):
        if version in existing:
            if (root / "bundle" / version / "ledger-contract.json").exists():
                refuse("version_exists", version)
        refuse("version_invalid", "cut must name the next c7.N directory")
    # Compare all sections, all members and all digests, before the first write.
    current = inventory(root)
    if pins != current or not current["sources"] or not current["schemas"]:
        refuse("unpinned_member", "candidate pins do not exactly cover the current runtime and schemas",
               drift=member_diff(pins, current))
    notes = preparation_notes(root)
    destination = root / "bundle" / version
    if destination.exists() or destination.is_symlink():
        refuse("version_exists", version)
    stage = Path(tempfile.mkdtemp(prefix=".ledger-cut-", dir=root / "bundle"))
    try:
        for relative, digest in pins["schemas"].items():
            data = ordinary(root, relative).read_bytes()
            if sha(data) != digest:
                refuse("unpinned_member", relative, drift=schema_drift(pins, relative, data))
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        document = {"format_version": 1, "kind": "ledger_contract", "version": version,
                    "predecessor": existing[-1], "source_pin": SOURCE_PIN,
                    "predecessor_contract_sha256": sha((root / "bundle" / previous_cut / "ledger-contract.json").read_bytes()) if previous_cut else None,
                    "members": pins, "ratifications": []}
        if notes:
            document["ratification_notes"] = notes
        save_contract(stage, document)
        observed = inventory(root)
        if observed != pins:
            refuse("unpinned_member", "members changed during the cut",
                   drift=member_diff(pins, observed))
        if preparation_notes(root) != notes:
            refuse("unpinned_member", "ratification notes changed during the cut")
        publish(root, version, stage)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {"classification": "cut", "version": version, "counts": counts(pins)}


def load_contract(root, version):
    if not VERSION.fullmatch(version):
        refuse("version_invalid", version)
    relative = "bundle/" + version
    path = ordinary(root, relative + "/ledger-contract.json")
    digest = ordinary(root, relative + "/ledger-contract.sha256").read_text().strip()
    if sha(path.read_bytes()) != digest:
        refuse("bundle_invalid", "ledger-contract digest does not match")
    document = read_json(path)
    if (not isinstance(document, dict) or document.get("format_version") != 1
            or document.get("kind") != "ledger_contract" or document.get("version") != version
            or not isinstance(document.get("members"), dict)
            or set(document["members"]) != {"sources", "schemas", "kinds"}
            or document.get("source_pin") != SOURCE_PIN
            or not isinstance(document.get("predecessor"), str)
            or not VERSION.fullmatch(document["predecessor"])
            or int(document["predecessor"].split(".")[1]) + 1 != int(version.split(".")[1])
            or not isinstance(document.get("ratifications"), list)):
        refuse("bundle_invalid", "invalid ledger contract index")
    reviewed_notes(document.get("ratification_notes", []))
    predecessor_pin = document.get("predecessor_contract_sha256")
    if predecessor_pin is not None:
        previous_index = ordinary(root, "bundle/" + document["predecessor"] + "/ledger-contract.json")
        if sha(previous_index.read_bytes()) != predecessor_pin:
            refuse("bundle_invalid", "predecessor contract changed")
        load_contract(root, document["predecessor"])
    members = document["members"]
    for section in ("sources", "schemas"):
        if not isinstance(members[section], dict) or not members[section]:
            refuse("bundle_invalid", section)
        for name, pin in members[section].items():
            if not isinstance(pin, str) or re.fullmatch(r"[0-9a-f]{64}", pin) is None:
                refuse("bundle_invalid", str(name))
            # Validate source names too, even though sources are pinned, not copied.
            prefix = "floati/" if section == "sources" else "schemas/"
            if not name.startswith(prefix) or ".." in PurePosixPath(name).parts:
                refuse("bundle_invalid", name)
            if section == "schemas":
                snapshot = ordinary(root, relative + "/" + name)
                if sha(snapshot.read_bytes()) != pin:
                    refuse("bundle_invalid", name)
    if not isinstance(members["kinds"], dict) or any(
        not isinstance(paths, list) or not paths or any(path not in members["sources"] for path in paths)
        for paths in members["kinds"].values()
    ):
        refuse("bundle_invalid", "kind has no pinned validator source")
    # Each ratification retains the exact preceding index and old schema bytes.
    # Verify the chain even when the current runtime matches the latest pins.
    following = document
    for ordinal in range(len(document["ratifications"]), 0, -1):
        event = document["ratifications"][ordinal - 1]
        if not isinstance(event, dict) or event.get("audit") != f"ratifications/{ordinal}":
            refuse("bundle_invalid", "ratification audit path invalid")
        audit = relative + "/" + event["audit"]
        previous = read_json(ordinary(root, audit + "/before.json"))
        fixture_bytes = ordinary(root, audit + "/fixtures.json").read_bytes()
        if (not isinstance(previous, dict)
                or sha(canonical(previous)) != event.get("before_contract_sha256")
                or sha(fixture_bytes) != event.get("fixtures_sha256")
                or previous.get("ratifications") != document["ratifications"][:ordinal - 1]
                or previous.get("version") != version
                or not isinstance(previous.get("members"), dict)):
            refuse("bundle_invalid", "ratification history digest invalid")
        previous_members = previous["members"]
        if (previous_members.get("sources") != following["members"]["sources"]
                or previous_members.get("kinds") != following["members"]["kinds"]
                or not isinstance(previous_members.get("schemas"), dict)
                or previous_members["schemas"].keys() != following["members"]["schemas"].keys()):
            refuse("bundle_invalid", "ratification changed member identity")
        changed = sorted(name for name in previous_members["schemas"]
                         if previous_members["schemas"][name] != following["members"]["schemas"][name])
        if not changed or event.get("changed_schemas") != changed:
            refuse("bundle_invalid", "ratification changed-schema census invalid")
        for name in changed:
            before_schema = ordinary(root, audit + "/" + name).read_bytes()
            if sha(before_schema) != previous_members["schemas"][name]:
                refuse("bundle_invalid", name)
        following = previous
    return document


def is_loosening(old, new):
    """A sufficient inclusion proof, never a sample-based guess about schemas.

    Unsupported changes (regex rewrites, refs, conditionals, custom semantics)
    require recutting. Keeping such constraints byte-for-byte is permitted.
    """
    if canonical(old) == canonical(new) or new is True or old is False:
        return True
    if old is True:
        old = {}
    if new is False or not isinstance(old, dict) or not isinstance(new, dict):
        return False
    handled = ANNOTATIONS | LOWER_BOUNDS | UPPER_BOUNDS | {
        "required", "enum", "const", "type", "properties", "additionalProperties", "items",
        "pattern", "format", "uniqueItems", "$id", "$schema",
    }
    # Even an unchanged patternProperties/unevaluatedProperties/conditional can
    # interact with edits to properties. Do not treat unknown vocabulary as an
    # independent predicate. Equal subtrees already returned above.
    if (set(old) | set(new)) - handled:
        return False
    if any(canonical(old.get(key)) != canonical(new.get(key)) for key in ("$id", "$schema")):
        return False
    for key in LOWER_BOUNDS:
        if key in new and (key not in old or new[key] > old[key]):
            return False
    for key in UPPER_BOUNDS:
        if key in new and (key not in old or new[key] < old[key]):
            return False
    for key in ("pattern", "format", "const"):
        if key in new and (key not in old or canonical(old[key]) != canonical(new[key])):
            return False
    if new.get("uniqueItems") and not old.get("uniqueItems"):
        return False
    if not set(new.get("required", [])) <= set(old.get("required", [])):
        return False
    if "enum" in new:
        if "enum" not in old or not {canonical(value) for value in old["enum"]} <= {
            canonical(value) for value in new["enum"]
        }:
            return False
    if "type" in new:
        def types(value):
            result = {value} if isinstance(value, str) else set(value)
            return result | ({"integer"} if "number" in result else set())
        if "type" not in old or not types(old["type"]) <= types(new["type"]):
            return False
    old_extra, new_extra = old.get("additionalProperties", True), new.get("additionalProperties", True)
    if not is_loosening(old_extra, new_extra):
        return False
    old_props, new_props = old.get("properties", {}), new.get("properties", {})
    for key in set(old_props) | set(new_props):
        if not is_loosening(old_props.get(key, old_extra), new_props.get(key, new_extra)):
            return False
    if not is_loosening_items(old, new):
        return False
    return True


def is_loosening_items(old, new):
    # Equal schemas exit at the top; an absent items constraint is the true schema.
    before, after = old.get("items", True), new.get("items", True)
    if canonical(before) == canonical(after) or after is True or before is False:
        return True
    return is_loosening(before, after)


def assess(root, version, fixtures, snapshots=None):
    root = Path(root).resolve()
    contract = load_contract(root, version)
    before, after = contract["members"], inventory(root)
    # Validate immutable captured bytes, not a sequence of live schema reads.
    with tempfile.TemporaryDirectory(prefix="ledger-assessment-") as temporary:
        base = Path(temporary).resolve()
        for label, source, members in (("before", root / "bundle" / version, before),
                                       ("after", root, after)):
            for name, digest in members["schemas"].items():
                data = ordinary(source, name).read_bytes()
                if sha(data) != digest:
                    refuse("unpinned_member", name, drift=schema_drift(members, name, data))
                target = base / label / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        result = assess_snapshots(version, fixtures, before, after, base)
    observed, baseline = inventory(root), load_contract(root, version)
    if observed != after or baseline != contract:
        refuse("unpinned_member", "contract changed during assessment",
               drift=member_diff(after, observed),
               baseline_drift=member_diff(before, baseline["members"]))
    result["members_sha256"] = sha(canonical(after))
    result["baseline_sha256"] = sha(canonical(contract))
    if snapshots is not None:
        snapshots.update(members=after, baseline=before)
    return result


def assess_snapshots(version, fixtures, before, after, base):
    if before == after:
        return {"classification": "unchanged", "version": version, "counts": counts(after)}
    reasons, changed = [], []
    if before["sources"] != after["sources"] or before["kinds"] != after["kinds"]:
        reasons.append("runtime contract changed; semantic compatibility is unproved")
    if before["schemas"].keys() != after["schemas"].keys():
        reasons.append("schema membership changed")
    for name in before["schemas"].keys() & after["schemas"].keys():
        if before["schemas"][name] == after["schemas"][name]:
            continue
        changed.append(name)
        previous_path = base / "before" / name
        # Inclusion through refs can reverse under not/if/oneOf. Until polarity
        # analysis exists, any changed reference dependency requires a recut.
        referenced = False
        for schema_name in before["schemas"]:
            pinned = read_json(base / "before" / schema_name)
            for reference in schema_references(pinned):
                if not isinstance(reference, str):
                    refuse("bundle_invalid", schema_name)
                reference_path = urlsplit(reference).path
                target = ((base / "before" / schema_name).parent / unquote(reference_path)).resolve() if reference_path else base / "before" / schema_name
                if target == base / "before" / name or schema_name == name:
                    referenced = True
        if referenced:
            reasons.append(name + ": reference-dependent inclusion requires a recut")
            continue
        if not is_loosening(read_json(previous_path), read_json(base / "after" / name)):
            reasons.append(name + ": accepted-set inclusion unproved")
            continue
        examples = fixtures.get(name) if isinstance(fixtures, dict) else None
        if not isinstance(examples, list) or not examples:
            reasons.append(name + ": a preserved old-valid fixture is required")
            continue
        # Reuse the dependency-free schema validator, including Floati extensions.
        from tests.schema_validation import SchemaValidationError, validate_json_schema
        try:
            for instance in examples:
                validate_json_schema(instance, previous_path)
                validate_json_schema(instance, base / "after" / name)
        except (SchemaValidationError, ValueError, KeyError, TypeError) as exc:
            reasons.append(name + ": fixture does not preserve an old-valid record: " + str(exc))
    return {"classification": "recut" if reasons else "ratify", "version": version,
            "changed_schemas": sorted(changed), "reasons": reasons, "counts": counts(after),
            "drift": member_diff(before, after)}


def ratify(root, version, fixtures):
    root = Path(root).resolve()
    with mutation_lock(root):
        try:
            return _ratify(root, version, json.loads(canonical(fixtures)))
        except OSError as exc:
            refuse("publication_failed", str(exc))


def _ratify(root, version, fixtures):
    if current_version(root) != version:
        refuse("version_invalid", "ratify may update only the current ledger cut")
    snapshots = {}
    result = assess(root, version, fixtures, snapshots)
    if result["classification"] == "unchanged":
        return result
    if result["classification"] != "ratify":
        refuse("recut_required", "; ".join(result["reasons"]), drift=result["drift"])
    document = load_contract(root, version)
    current = inventory(root)
    if (sha(canonical(current)) != result["members_sha256"]
            or sha(canonical(document)) != result["baseline_sha256"]):
        refuse("unpinned_member", "ratification inputs changed after assessment",
               drift=member_diff(snapshots["members"], current),
               baseline_drift=member_diff(snapshots["baseline"], document["members"]))
    directory = root / "bundle" / version
    # Reject symlinks anywhere before copying or creating audit directories.
    files_below(root, "bundle/" + version, ".json")
    with tempfile.TemporaryDirectory(prefix=".ledger-ratify-", dir=root / "bundle") as temporary:
        stage = Path(temporary) / version
        shutil.copytree(directory, stage)
        audit = stage / "ratifications" / str(len(document["ratifications"]) + 1)
        if audit.exists():
            refuse("bundle_invalid", "ratification audit already exists")
        audit.mkdir(parents=True)
        write_json(audit / "before.json", document)
        write_json(audit / "fixtures.json", fixtures)
        for name in result["changed_schemas"]:
            target = stage / name
            previous = audit / name
            previous.parent.mkdir(parents=True, exist_ok=True)
            previous.write_bytes(target.read_bytes())
            data = ordinary(root, name).read_bytes()
            if sha(data) != current["schemas"][name]:
                refuse("unpinned_member", name, drift=schema_drift(current, name, data))
            target.write_bytes(data)
        document["ratifications"].append({
            "audit": audit.relative_to(stage).as_posix(),
            "before_contract_sha256": result["baseline_sha256"],
            "fixtures_sha256": sha((audit / "fixtures.json").read_bytes()),
            "changed_schemas": result["changed_schemas"],
        })
        document["members"] = current
        save_contract(stage, document)
        # Validate the staged schema/audit pins using the same reader. Retain
        # the immutable predecessor chain in this isolated validation tree.
        staged_root = Path(temporary) / "validation"
        (staged_root / "bundle").mkdir(parents=True)
        shutil.copytree(stage, staged_root / "bundle" / version)
        predecessor = document
        while predecessor.get("predecessor_contract_sha256") is not None:
            previous_version = predecessor["predecessor"]
            shutil.copytree(root / "bundle" / previous_version, staged_root / "bundle" / previous_version)
            predecessor = load_contract(root, previous_version)
        load_contract(staged_root, version)
        observed, baseline = inventory(root), load_contract(root, version)
        if (observed != current or sha(canonical(baseline)) != result["baseline_sha256"]):
            refuse("unpinned_member", "contract changed before ratification publication",
                   drift=member_diff(current, observed),
                   baseline_drift=member_diff(snapshots["baseline"], baseline["members"]))
        publish(root, version, stage, replace=True)
    return result


def counts(members):
    return {section: len(values) for section, values in members.items()}


def check(root):
    root = Path(root).resolve()
    with mutation_lock(root):
        return _check(root)


def current_version(root):
    path = root / CURRENT
    existing = versions(root)
    preparation = root / PREPARATION
    anchor = read_json(ordinary(root, PREPARATION)) if preparation.exists() or preparation.is_symlink() else None
    if path.exists() or path.is_symlink():
        pointer = read_json(ordinary(root, CURRENT))
        if (not isinstance(pointer, dict) or set(pointer) != {"version", "sha256"}
                or not existing or pointer["version"] != existing[-1]):
            refuse("bundle_invalid", "current ledger-cut pointer invalid")
        if anchor != {"state": "cut", "version": pointer["version"]}:
            refuse("bundle_invalid", "cut-history anchor missing or inconsistent")
        index = root / "bundle" / pointer["version"] / "ledger-contract.json"
        if not index.is_file() or index.is_symlink() or sha(index.read_bytes()) != pointer["sha256"]:
            refuse("bundle_invalid", "current ledger-cut index missing or changed")
        return pointer["version"]
    if isinstance(anchor, dict) and anchor.get("state") == "cut":
        refuse("bundle_invalid", "a recorded cut and its current pointer are missing")
    if any((root / "bundle" / version / name).exists() for version in existing
           for name in ("ledger-contract.json", "ledger-contract.sha256", "ratifications")):
        refuse("bundle_invalid", "ledger-cut pointer missing")
    return None


def _check(root):
    version = current_version(root)
    if version:
        result = assess(root, version, {})
        if result["classification"] != "unchanged":
            refuse("recut_required", "pending contract drift; assess with fixtures, then ratify or cut a new version",
                   drift=result["drift"])
        return result
    document = read_json(ordinary(root, PREPARATION))
    current = inventory(root)
    expected = {"state": "prepared_not_cut", "bundles": versions(root), "members": current}
    if isinstance(document, dict) and "ratification_notes" in document:
        expected["ratification_notes"] = reviewed_notes(document["ratification_notes"])
    if document != expected:
        refuse("preparation_drift", "pending rows changed the preparation census; review them before cutting",
               drift=member_diff(document.get("members") if isinstance(document, dict) else None, current))
    return {"classification": "prepared", "counts": counts(current), "bundle_cut": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inventory")
    sub.add_parser("check")
    prepare_parser = sub.add_parser("prepare", help="regenerate test preparation pins; never creates a bundle")
    prepare_parser.add_argument("--ratification-notes", type=Path,
                                help="reviewed JSON rulings to retain in the first cut")
    cut_parser = sub.add_parser("cut")
    cut_parser.add_argument("--version", required=True)
    cut_parser.add_argument("--pins", type=Path, help="optional independently reviewed inventory; exact coverage required")
    for command in ("assess", "ratify"):
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--version", required=True)
        command_parser.add_argument("--fixtures", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "inventory":
            result = inventory(root)
        elif args.command == "check":
            result = check(root)
        elif args.command == "prepare":
            notes = reviewed_notes(read_json(args.ratification_notes)) if args.ratification_notes else []
            with mutation_lock(root):
                members = inventory(root)
                if current_version(root):
                    refuse("recut_required", "preparation cannot replace an existing cut")
                target = root / PREPARATION
                current = root
                for part in PurePosixPath(PREPARATION).parts:
                    current = current / part
                    if current.is_symlink():
                        refuse("path_invalid", PREPARATION)
                if target.exists() or target.is_symlink():
                    ordinary(root, PREPARATION)
                document = {"state": "prepared_not_cut", "bundles": versions(root), "members": members}
                if notes:
                    document["ratification_notes"] = notes
                write_json(target, document)
            result = {"classification": "prepared", "bundle_cut": False, "counts": counts(members)}
        elif args.command == "cut":
            pins = read_json(args.pins) if args.pins else inventory(root)
            if isinstance(pins, dict) and pins.get("status") == "ok":
                pins = pins.get("evidence")
            result = cut(root, args.version, pins)
        else:
            result = globals()[args.command](root, args.version, read_json(args.fixtures))
        print(json.dumps({"status": "ok", "evidence": result}, sort_keys=True))
        return 0
    except ContractRefusal as exc:
        print(json.dumps({"status": "refused", "evidence": {
            "code": exc.code, "detail": exc.detail, **exc.evidence,
        }}, sort_keys=True))
        return 20
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"status": "refused", "evidence": {
            "code": "ledger_contract_document_or_io_invalid", "detail": str(exc),
        }}, sort_keys=True))
        return 20


if __name__ == "__main__":
    raise SystemExit(main())

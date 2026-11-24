"""SEND-REFUSAL-REMEDY-2: every send-reachable refusal names a typeable remedy.

Architect: of the refusal codes `floati send` can return, ~48 of 54 carry
remedy kind none. Property: every refusal code reachable from send names
its case and ONE typeable remedy (reuse F2-SEND-REFUSAL-REMEDY-1 /
F2-REFUSAL-REMEDY-1 vocabulary). The check never loosens. A code with no
honest remedy gets an explicit reason, not a fake flag.

FIRST: derive the reachable population mechanically from `_send` plus the
send parser's `arguments_invalid` error, not from a hand list.

Ways this check could be wrong, each with a case below:

1. Hand census. A new send-path constructor would be invisible. Built: AST
   call-graph from `_send`.
2. Whole `validate_record`. That function refuses hundreds of kinds send
   never writes. Built: only `message_envelope`, `delivery_claim`, and
   `denial_receipt` kind branches, plus the kind-agnostic preamble.
3. Missing claim/root guards. Built: follow `load_claim_document` and
   `resolve_command_root`; require `claim_path_invalid` and `cannot_speak`.
4. Dynamic `_deny(reason_code)`. Built: collect literal `_deny` arguments.
5. Treating `wake_controller_only` as send-reachable because jsonl is
   shared. Built: live assertNotIn for the three sealed-ledger `*_only`
   codes; they are printed exclusions.
6. Vacuous empty graph. Built: required send-path codes must be present
   and the printed count must be at least the prior census floor.
7. Loosening by allowlisting kind none. Built: any reachable kind-none
   site fails; no exemption set. An explicit `kind: none` dict is not a
   typeable remedy.
8. Global SEND map leak. Built: status/init keep unnamed remedies; send
   rebinds in `_send` / `EventLog.send`.
9. Hand SEND_MODULES. Built: follow imports and `self` methods from
   `_send`; print the derived module set. `ids.uuid7_hex` is in the graph.
10. Wrong roster/ledger remedies. Built: no `floati node list`; record_*
    and journal_* do not say retire the root.
"""

from __future__ import annotations

import ast
import re
import tempfile
import unittest
from pathlib import Path

from floati.errors import (
    DRILL_REMEDIES,
    SEND_EXPLICIT_NONE,
    SEND_REFUSAL_REMEDIES,
    send_path_remedy,
)
from tests.test_rem_2 import (
    _call_name,
    _code_arg,
    _const_str,
    _nonempty_remedy,
    _remedy_node,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SEND_RECORD_KINDS = frozenset(
    {"message_envelope", "delivery_claim", "denial_receipt"}
)
# Prior hand census floor (54 other codes plus the two repaired reply codes).
SEND_REACHABLE_FLOOR = 54
REQUIRED_SEND_CODES = frozenset(
    {
        "arguments_invalid",
        "cannot_speak",
        "claim_path_invalid",
        "idempotency_conflict",
        "ledger_lock_timeout",
        "note_empty",
        "recipient_unregistered",
        "reply_to_unknown",
        "sha_absent",
        "sha_unbanked",
        "sha_unchecked",
    }
)
FORBIDDEN_SEND_CODES = frozenset(
    {
        "lifecycle_prompts_harness_unknown",
        "wake_controller_only",
        "effect_controller_only",
        "thread_observer_only",
    }
)
LIBRARY_ONLY_SEND_CODES = frozenset(
    {
        "attempt_binding_invalid",
        "attempt_binding_session_mismatch",
        "attempt_id_invalid",
        "time_invalid",
    }
)
LEDGER_WRITE_CODES = frozenset(
    code
    for code in SEND_REFUSAL_REMEDIES
    if code.startswith("record_") or code.startswith("journal_")
)
SEND_CODE_EXCLUSIONS = frozenset(
    {
        "wake_controller_only",
        "effect_controller_only",
        "thread_observer_only",
    }
)
STDLIB_OR_EXTERNAL = frozenset(
    {
        "append",
        "attr",
        "datetime",
        "dict",
        "frozenset",
        "getattr",
        "isinstance",
        "len",
        "list",
        "max",
        "min",
        "next",
        "object",
        "open",
        "print",
        "range",
        "set",
        "sorted",
        "str",
        "super",
        "tuple",
        "type",
        "zip",
    }
)


SKIP_KIND_BRANCH = object()


def _send_nonempty_remedy(node: ast.AST | None) -> bool:
    if isinstance(node, ast.Dict):
        kind = None
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "kind":
                kind = _const_str(value)
        if kind == "none":
            return False
        if kind == "transient":
            return True
        return False
    return _nonempty_remedy(node)


def _simple_kind_eq(test: ast.AST) -> str | None:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    if not isinstance(test.ops[0], ast.Eq):
        return None
    left, right = test.left, test.comparators[0]
    for first, second in ((left, right), (right, left)):
        if isinstance(first, ast.Name) and first.id == "kind":
            return _const_str(second)
    return None


def _kind_test(test: ast.AST):
    """Return a kind string, SKIP_KIND_BRANCH, or None if this is not a kind test."""

    direct = _simple_kind_eq(test)
    if direct is not None:
        return direct
    if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
        kinds = [kind for kind in (_simple_kind_eq(value) for value in test.values) if kind]
        if len(kinds) == 1:
            return kinds[0]
        if any(_kind_membership(value) for value in test.values):
            return SKIP_KIND_BRANCH
        return None
    if _kind_membership(test):
        return SKIP_KIND_BRANCH
    return None


def _kind_membership(test: ast.AST) -> bool:
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return False
    if not isinstance(test.ops[0], (ast.In, ast.NotIn)):
        return False
    left = test.left
    return isinstance(left, ast.Name) and left.id == "kind"


def _kind_equals(test: ast.AST) -> str | None:
    result = _kind_test(test)
    return result if isinstance(result, str) else None


def _import_map(tree: ast.AST) -> dict[str, str]:
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module:
            continue
        module = node.module.replace(".", "/")
        if node.level == 1:
            module = "floati/" + module.split("/")[-1]
        elif node.level:
            continue
        elif node.module.startswith("floati"):
            module = node.module.replace(".", "/")
        else:
            continue
        path = module + ".py"
        for alias in node.names:
            names[alias.asname or alias.name] = path
    return names


class _Index:
    def __init__(self, root: Path) -> None:
        self.functions: dict[tuple[str, str], ast.FunctionDef] = {}
        self.methods: dict[tuple[str, str, str], ast.FunctionDef] = {}
        self.imports: dict[str, dict[str, str]] = {}
        self.trees: dict[str, ast.Module] = {}
        self.instance_attrs: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.bases: dict[tuple[str, str], list[tuple[str, str]]] = {}
        package = root / "floati"
        for path in sorted(package.rglob("*.py")):
            rel = path.relative_to(root).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            self.trees[rel] = tree
            self.imports[rel] = _import_map(tree)
            for node in tree.body:
                if isinstance(node, ast.FunctionDef):
                    self.functions[(rel, node.name)] = node
                elif isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, ast.FunctionDef):
                            self.methods[(rel, node.name, item.name)] = item
        self._bind_bases()
        self._bind_instance_attrs()

    def resolve_class(self, rel: str, name: str) -> tuple[str, str] | None:
        imported = self.imports.get(rel, {})
        target = imported.get(name)
        if target and any(key[0] == target and key[1] == name for key in self.methods):
            return (target, name)
        if any(key[0] == rel and key[1] == name for key in self.methods):
            return (rel, name)
        return None

    def infer_function_class(
        self, rel: str, name: str, seen: set[tuple[str, str]] | None = None
    ) -> tuple[str, str] | None:
        walking = set() if seen is None else seen
        key = (rel, name)
        if not name or key in walking:
            return None
        walking.add(key)
        fn = self.functions.get(key)
        fn_rel = rel
        if fn is None:
            imported = self.imports.get(rel, {})
            target = imported.get(name)
            if target:
                fn = self.functions.get((target, name))
                fn_rel = target
        if fn is None:
            return None
        skip = {"ProtocolRefusal", "IntegrityFailure", "DurabilityFailure", "Exception"}

        def from_expr(expr: ast.AST | None) -> tuple[str, str] | None:
            if expr is None:
                return None
            if isinstance(expr, ast.Tuple) and expr.elts:
                return from_expr(expr.elts[0])
            if isinstance(expr, ast.Name):
                return None
            if not isinstance(expr, ast.Call):
                return None
            func = expr.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id not in skip:
                    found = self.resolve_class(fn_rel, func.value.id)
                    if found is not None:
                        return found
            call_name = _call_name(func)
            if call_name in skip:
                return None
            found = self.resolve_class(fn_rel, call_name)
            if found is not None:
                return found
            found = self.infer_function_class(fn_rel, call_name, walking)
            if found is not None:
                return found
            imported = self.imports.get(fn_rel, {})
            target = imported.get(call_name)
            if target:
                return self.infer_function_class(target, call_name, walking)
            return None

        assigned: dict[str, tuple[str, str]] = {}
        for stmt in ast.walk(fn):
            if not isinstance(stmt, ast.Assign):
                continue
            found = from_expr(stmt.value)
            if found is None:
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    assigned[target.id] = found
                elif isinstance(target, ast.Tuple):
                    names = [elt.id for elt in target.elts if isinstance(elt, ast.Name)]
                    if names:
                        assigned[names[0]] = found
        for node in ast.walk(fn):
            if not isinstance(node, ast.Return):
                continue
            if isinstance(node.value, ast.Name) and node.value.id in assigned:
                return assigned[node.value.id]
            found = from_expr(node.value)
            if found is not None:
                return found
        return None

    def _bind_bases(self) -> None:
        for rel, tree in self.trees.items():
            for node in tree.body:
                if not isinstance(node, ast.ClassDef):
                    continue
                bases: list[tuple[str, str]] = []
                for base in node.bases:
                    name = base.id if isinstance(base, ast.Name) else _call_name(base)
                    if not name:
                        continue
                    found = self.resolve_class(rel, name)
                    if found is not None:
                        bases.append(found)
                self.bases[(rel, node.name)] = bases

    def resolve_method(
        self, rel: str, class_name: str, method: str
    ) -> tuple[str, str, str] | None:
        key = (rel, class_name, method)
        if key in self.methods:
            return key
        for base_rel, base_name in self.bases.get((rel, class_name), []):
            found = self.resolve_method(base_rel, base_name, method)
            if found is not None:
                return found
        return None

    def _bind_instance_attrs(self) -> None:
        for (rel, class_name, method_name), fn in self.methods.items():
            if method_name != "__init__":
                continue
            for stmt in ast.walk(fn):
                if not isinstance(stmt, ast.Assign):
                    continue
                for target in stmt.targets:
                    if not (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        continue
                    found = None
                    for call in ast.walk(stmt.value):
                        if not isinstance(call, ast.Call):
                            continue
                        cls = self.resolve_class(rel, _call_name(call.func))
                        if cls is not None:
                            found = cls
                    if found is not None:
                        self.instance_attrs[(rel, class_name, target.attr)] = found


def _iter_calls(fn: ast.FunctionDef) -> list[ast.Call]:
    return [node for node in ast.walk(fn) if isinstance(node, ast.Call)]


def _call_bindings(fn: ast.FunctionDef, call: ast.Call) -> dict[str, str]:
    names = [
        arg.arg
        for arg in [*fn.args.posonlyargs, *fn.args.args]
        if arg.arg not in {"self", "cls"}
    ]
    bindings: dict[str, str] = {}
    for index, value in enumerate(call.args):
        if index >= len(names):
            break
        literal = _const_str(value)
        if literal:
            bindings[names[index]] = literal
    for keyword in call.keywords:
        if not keyword.arg:
            continue
        literal = _const_str(keyword.value)
        if literal:
            bindings[keyword.arg] = literal
    return bindings


def _collect_from_block(
    stmts: list[ast.stmt],
    kind_context: str | None,
    on_refuse: object,
    on_call: object,
) -> None:
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            kind = _kind_test(stmt.test)
            if kind is SKIP_KIND_BRANCH:
                _collect_from_block(stmt.orelse, kind_context, on_refuse, on_call)
                continue
            if isinstance(kind, str):
                _collect_from_block(stmt.body, kind, on_refuse, on_call)
                _collect_from_block(stmt.orelse, kind_context, on_refuse, on_call)
                continue
            _collect_from_block(stmt.body, kind_context, on_refuse, on_call)
            _collect_from_block(stmt.orelse, kind_context, on_refuse, on_call)
            continue
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            for node in ast.walk(getattr(stmt, "iter", stmt)):
                if isinstance(node, ast.Call):
                    on_call(node)
                    name = _call_name(node.func)
                    if name in {"ProtocolRefusal", "refuse", "FrameError"}:
                        on_refuse(node)
            _collect_from_block(stmt.body, kind_context, on_refuse, on_call)
            orelse = getattr(stmt, "orelse", None)
            if orelse:
                _collect_from_block(orelse, kind_context, on_refuse, on_call)
            continue
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            included = kind_context is None or kind_context in SEND_RECORD_KINDS
            if included:
                for item in stmt.items:
                    for node in ast.walk(item.context_expr):
                        if isinstance(node, ast.Call):
                            on_call(node)
                            name = _call_name(node.func)
                            if name in {"ProtocolRefusal", "refuse", "FrameError"}:
                                on_refuse(node)
            _collect_from_block(stmt.body, kind_context, on_refuse, on_call)
            continue
        if isinstance(stmt, ast.Try):
            _collect_from_block(stmt.body, kind_context, on_refuse, on_call)
            for handler in stmt.handlers:
                _collect_from_block(handler.body, kind_context, on_refuse, on_call)
            _collect_from_block(stmt.orelse, kind_context, on_refuse, on_call)
            _collect_from_block(stmt.finalbody, kind_context, on_refuse, on_call)
            continue
        if isinstance(stmt, ast.FunctionDef):
            continue
        included = kind_context is None or kind_context in SEND_RECORD_KINDS
        if not included:
            continue
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                on_call(node)
                name = _call_name(node.func)
                if name in {"ProtocolRefusal", "refuse", "FrameError"}:
                    on_refuse(node)


def send_reachable_sites(root: Path) -> tuple[list[tuple[str, bool]], frozenset[str]]:
    """`(code, nonempty_remedy)` for each send-reachable constructor or `_deny`."""

    index = _Index(root)
    seeds = [
        ("floati/cli.py", "_send"),
        ("floati/cli.py", "_ArtifactParser", "error"),
    ]
    queued: list[tuple[tuple, dict[str, str]]] = [(key, {}) for key in seeds]
    seen: set[tuple] = set()
    sites: list[tuple[str, bool]] = []

    def enqueue(key: tuple, bindings: dict[str, str] | None = None) -> None:
        rel = key[0]
        if not isinstance(rel, str) or not rel.startswith("floati/") or not rel.endswith(".py"):
            return
        queued.append((key, dict(bindings or {})))

    current_locals: dict[str, tuple[str, str]] = {}

    def _enqueue_fn(rel: str, name: str, call: ast.Call) -> bool:
        fn = index.functions.get((rel, name))
        if fn is None:
            return False
        enqueue((rel, name), _call_bindings(fn, call))
        return True

    def _enqueue_method(rel: str, class_name: str, name: str, call: ast.Call) -> bool:
        found = index.resolve_method(rel, class_name, name)
        if found is None:
            return False
        enqueue(found, _call_bindings(index.methods[found], call))
        return True

    def resolve_call(rel: str, call: ast.Call, current_class: str | None = None) -> None:
        func = call.func
        name = _call_name(func)
        if not name or name in STDLIB_OR_EXTERNAL or name in {
            "ProtocolRefusal",
            "IntegrityFailure",
            "DurabilityFailure",
            "refuse",
        }:
            return
        if name == "_deny" or (
            isinstance(func, ast.Attribute) and func.attr == "_deny"
        ):
            code = _const_str(call.args[2] if len(call.args) >= 3 else None)
            if code is None:
                for keyword in call.keywords:
                    if keyword.arg == "reason_code":
                        code = _const_str(keyword.value)
            if code and code not in SEND_CODE_EXCLUSIONS:
                detail = None
                for keyword in call.keywords:
                    if keyword.arg == "detail":
                        detail = keyword.value
                sites.append((code, detail is not None))
            return
        if name in {"resolve_node_id", "validate_identifier"}:
            field = None
            if name == "validate_identifier" and len(call.args) >= 2:
                field = _const_str(call.args[1])
            for keyword in call.keywords:
                if keyword.arg == "unknown_code":
                    code = _const_str(keyword.value)
                    if code and code not in SEND_CODE_EXCLUSIONS:
                        sites.append((code, True))
                if keyword.arg == "field":
                    field = _const_str(keyword.value)
            if field:
                derived = f"{field}_invalid"
                if derived not in SEND_CODE_EXCLUSIONS:
                    sites.append((derived, True))
        if name == "_read_bound_regular":
            for keyword in call.keywords:
                if keyword.arg == "code":
                    code = _const_str(keyword.value)
                    if code and code not in SEND_CODE_EXCLUSIONS:
                        sites.append((code, False))
        imported = index.imports.get(rel, {})
        if isinstance(func, ast.Name):
            if _enqueue_fn(rel, name, call):
                return
            target = imported.get(name)
            if target:
                _enqueue_fn(target, name, call)
            return
        if isinstance(func, ast.Attribute):
            owner = func.value
            if (
                current_class
                and isinstance(owner, ast.Name)
                and owner.id in {"self", "cls"}
            ):
                _enqueue_method(rel, current_class, name, call)
                return
            if (
                current_class
                and isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id in {"self", "cls"}
            ):
                cls = index.instance_attrs.get((rel, current_class, owner.attr))
                if cls is not None:
                    _enqueue_method(cls[0], cls[1], name, call)
                    return
            if isinstance(owner, ast.Name) and owner.id in current_locals:
                cls = current_locals[owner.id]
                _enqueue_method(cls[0], cls[1], name, call)
                return
            owner_name = _call_name(owner) if isinstance(owner, ast.Name) else (
                _call_name(owner.func) if isinstance(owner, ast.Call) else ""
            )
            cls = index.resolve_class(rel, owner_name)
            if cls is not None:
                _enqueue_method(cls[0], cls[1], name, call)
                return
            owner_module = imported.get(owner_name)
            if owner_module:
                _enqueue_method(owner_module, owner_name, name, call)
            return

    while queued:
        key, param_literals = queued.pop()
        seen_key = key + (tuple(sorted(param_literals.items())),)
        if seen_key in seen:
            continue
        seen.add(seen_key)
        if len(key) == 2:
            fn = index.functions.get(key)
            rel = key[0]
            current_class = None
        else:
            fn = index.methods.get(key)
            rel = key[0]
            current_class = key[1]
        if fn is None:
            continue
        current_locals = {}
        for stmt in ast.walk(fn):
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not isinstance(target, ast.Name):
                continue
            value = stmt.value
            if (
                current_class is not None
                and isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in {"self", "cls"}
            ):
                cls = index.instance_attrs.get((rel, current_class, value.attr))
                if cls is not None:
                    current_locals[target.id] = cls
            elif isinstance(value, ast.Call):
                call_name = _call_name(value.func)
                cls = index.resolve_class(rel, call_name)
                if cls is None:
                    cls = index.infer_function_class(rel, call_name)
                if cls is not None:
                    current_locals[target.id] = cls
        fn_params = {
            arg.arg
            for arg in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]
        }
        for nested in fn.body:
            if isinstance(nested, ast.FunctionDef):
                index.functions[(rel, nested.name)] = nested
        for decorator in fn.decorator_list:
            if isinstance(decorator, ast.Name):
                resolve_call(
                    rel,
                    ast.Call(func=decorator, args=[], keywords=[]),
                    current_class,
                )
            elif isinstance(decorator, ast.Attribute):
                resolve_call(
                    rel,
                    ast.Call(func=decorator, args=[], keywords=[]),
                    current_class,
                )

        def on_refuse(call: ast.Call) -> None:
            name = _call_name(call.func)
            arg = _code_arg(call)
            if name == "FrameError":
                code = _const_str(arg)
                if code is None:
                    sites.append(("{uneval}", False))
                    return
                if code not in SEND_CODE_EXCLUSIONS:
                    sites.append((code, False))
                return
            bound = _send_nonempty_remedy(_remedy_node(call))
            if isinstance(arg, ast.Attribute) and arg.attr == "code":
                return
            if isinstance(arg, ast.Name) and arg.id in fn_params:
                code = param_literals.get(arg.id)
                if code and code not in SEND_CODE_EXCLUSIONS:
                    sites.append((code, bound))
                elif code is None:
                    sites.append(("{uneval}", bound))
                return
            code = _const_str(arg)
            if code is None:
                sites.append(("{uneval}", bound))
                return
            if code not in SEND_CODE_EXCLUSIONS:
                sites.append((code, bound))

        def on_call(call: ast.Call) -> None:
            resolve_call(rel, call, current_class)

        _collect_from_block(fn.body, None, on_refuse, on_call)

    return sites, frozenset(key[0] for key in seen if isinstance(key[0], str))


def send_reachable_census(root: Path) -> dict[str, object]:
    sites, modules = send_reachable_sites(root)
    site_bounds: dict[str, list[bool]] = {}
    for code, bound in sites:
        site_bounds.setdefault(code, []).append(bound)
    codes: set[str] = set()
    kind_none: set[str] = set()
    for code, flags in site_bounds.items():
        dynamic = code == "{uneval}" or "{dyn}" in code
        if not dynamic:
            codes.add(code)
        if flags and all(flags):
            continue
        if dynamic:
            kind_none.add(code)
            continue
        if code in DRILL_REMEDIES or code in SEND_EXPLICIT_NONE:
            continue
        mapped = send_path_remedy(code, None)
        if isinstance(mapped, str) and mapped.strip():
            continue
        if isinstance(mapped, dict) and mapped.get("kind") == "none":
            kind_none.add(code)
            continue
        kind_none.add(code)
    return {
        "codes": frozenset(codes),
        "kind_none": frozenset(kind_none),
        "count": len(codes),
        "modules": modules,
    }


class SendRefusalRemedy2Tests(unittest.TestCase):
    def _planted_tree(self, *, cli: str, extra: dict[str, str] | None = None) -> Path:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "errors.py").write_text(
            "DRILL_REMEDIES = {}\n"
            "class ProtocolRefusal(Exception):\n"
            "    def __init__(self, code, detail, remedy=None):\n"
            "        self.code = code\n",
            encoding="utf-8",
        )
        (package / "cli.py").write_text(cli, encoding="utf-8")
        for name, body in (extra or {}).items():
            (package / name).write_text(body, encoding="utf-8")
        return root

    def test_constructed_send_graph_sees_kind_none_and_skips_other_kinds(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "errors.py").write_text(
            "UNNAMED_REMEDY = {'kind': 'none', 'why': 'none'}\n"
            "DRILL_REMEDIES = {}\n"
            "def bind_refusal_remedy(code, remedy=None):\n"
            "    return {'kind': 'none', 'why': 'none'}\n"
            "class ProtocolRefusal(Exception):\n"
            "    def __init__(self, code, detail, remedy=None):\n"
            "        self.code = code\n",
            encoding="utf-8",
        )
        (package / "cli.py").write_text(
            "from .events import EventLog\n"
            "from .errors import ProtocolRefusal\n"
            "class _ArtifactParser:\n"
            "    def error(self, message):\n"
            "        raise ProtocolRefusal('arguments_invalid', message)\n"
            "def _send(args):\n"
            "    EventLog().send()\n"
            "    raise ProtocolRefusal('fixture_unbound_send', 'x')\n",
            encoding="utf-8",
        )
        (package / "ids.py").write_text(
            "from .errors import ProtocolRefusal\n"
            "def uuid7_hex():\n"
            "    raise ProtocolRefusal('fixture_uuid7', 'x')\n",
            encoding="utf-8",
        )
        (package / "events.py").write_text(
            "from .errors import ProtocolRefusal\n"
            "from .ids import uuid7_hex\n"
            "from .records import validate_record\n"
            "class EventLog:\n"
            "    def send(self):\n"
            "        uuid7_hex()\n"
            "        validate_record({'kind': 'message_envelope'})\n"
            "        raise ProtocolRefusal('fixture_conflict', 'x')\n"
            "        raise ProtocolRefusal(\n"
            "            'fixture_explicit_none', 'x',\n"
            "            remedy={'kind': 'none', 'why': 'library-only'},\n"
            "        )\n"
            "        self._deny('a', 'b', 'reply_to_unknown', detail={'remedy': 'omit'})\n",
            encoding="utf-8",
        )
        (package / "records.py").write_text(
            "from .errors import ProtocolRefusal\n"
            "def validate_record(record):\n"
            "    def refuse(code, detail, remedy=None):\n"
            "        raise ProtocolRefusal(code, detail, remedy=remedy)\n"
            "    refuse('record_kind_invalid', 'x')\n"
            "    kind = record.get('kind')\n"
            "    if kind == 'message_envelope':\n"
            "        refuse('doc_invalid', 'x', 'pass --doc')\n"
            "    if kind == 'wake_daemon_lifecycle_receipt':\n"
            "        refuse('wake_controller_only', 'x')\n",
            encoding="utf-8",
        )
        derived = send_reachable_census(root)
        self.assertIn("arguments_invalid", derived["codes"])
        self.assertIn("fixture_unbound_send", derived["codes"])
        self.assertIn("fixture_conflict", derived["codes"])
        self.assertIn("reply_to_unknown", derived["codes"])
        self.assertIn("doc_invalid", derived["codes"])
        self.assertIn("record_kind_invalid", derived["codes"])
        self.assertNotIn("wake_controller_only", derived["codes"])
        self.assertIn("fixture_uuid7", derived["codes"])
        self.assertIn("fixture_conflict", derived["kind_none"])
        self.assertIn("fixture_unbound_send", derived["kind_none"])
        self.assertIn("fixture_explicit_none", derived["kind_none"])
        self.assertNotIn("doc_invalid", derived["kind_none"])

    def test_live_send_reachable_codes_have_typeable_remedies(self) -> None:
        derived = send_reachable_census(REPOSITORY_ROOT)
        codes = derived["codes"]
        kind_none = derived["kind_none"]
        print("SEND_REACHABLE_COUNT", derived["count"])
        print("SEND_MODULES", sorted(derived["modules"]))
        print("SEND_CODE_EXCLUSIONS", sorted(SEND_CODE_EXCLUSIONS))
        self.assertGreaterEqual(derived["count"], SEND_REACHABLE_FLOOR, sorted(codes))
        missing = sorted(REQUIRED_SEND_CODES - codes)
        self.assertEqual([], missing, missing)
        leaked = sorted(FORBIDDEN_SEND_CODES & codes)
        self.assertEqual([], leaked, leaked)
        self.assertEqual(
            [],
            sorted(kind_none),
            f"SEND_REACHABLE_COUNT={derived['count']} kind_none={sorted(kind_none)}",
        )

    def test_send_map_does_not_name_missing_commands_or_retire_the_root(self) -> None:
        for code in ("recipient_unregistered", "unknown_sender", "unknown_recipient"):
            remedy = SEND_REFUSAL_REMEDIES[code]
            self.assertNotIn("floati node list", remedy, code)
            self.assertIn("detail", remedy, code)
        for code in LEDGER_WRITE_CODES:
            remedy = SEND_REFUSAL_REMEDIES[code]
            self.assertNotIn("retire the root", remedy, code)
        from floati import errors as error_mod

        none_map = getattr(error_mod, "SEND_EXPLICIT_NONE", {})
        for code in LIBRARY_ONLY_SEND_CODES:
            mapped = none_map.get(code)
            self.assertIsInstance(mapped, dict, code)
            self.assertEqual("none", mapped.get("kind"), code)
            self.assertTrue(str(mapped.get("why") or "").strip(), code)
        self.assertIn("seat board", SEND_REFUSAL_REMEDIES["node_lease_expired"])

    def test_self_registry_resolve_node_id_is_in_the_send_graph(self) -> None:
        """R3 RED: self.registry.resolve_node_id stayed green at R2."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "errors.py").write_text(
            "DRILL_REMEDIES = {}\n"
            "class ProtocolRefusal(Exception):\n"
            "    def __init__(self, code, detail, remedy=None):\n"
            "        self.code = code\n",
            encoding="utf-8",
        )
        (package / "cli.py").write_text(
            "from .events import EventLog\n"
            "class _ArtifactParser:\n"
            "    def error(self, message):\n"
            "        raise Exception(message)\n"
            "def _send(args):\n"
            "    EventLog().send()\n",
            encoding="utf-8",
        )
        (package / "events.py").write_text(
            "from .registry import Registry\n"
            "class EventLog:\n"
            "    def __init__(self):\n"
            "        self.registry = Registry()\n"
            "    def send(self):\n"
            "        self.registry.resolve_node_id('x', field='recipient')\n",
            encoding="utf-8",
        )
        (package / "registry.py").write_text(
            "from .errors import ProtocolRefusal\n"
            "class Registry:\n"
            "    def resolve_node_id(self, node_id, *, field='node', unknown_code='unknown_node'):\n"
            "        raise ProtocolRefusal('planted_resolve', 'x')\n",
            encoding="utf-8",
        )
        derived = send_reachable_census(root)
        self.assertIn("planted_resolve", derived["codes"])
        self.assertIn("planted_resolve", derived["kind_none"])

    def test_unevaluable_refusal_code_is_not_skipped(self) -> None:
        """R3 RED: f-string codes were dropped; the census must fail them."""

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        package = root / "floati"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "errors.py").write_text(
            "DRILL_REMEDIES = {}\n"
            "class ProtocolRefusal(Exception):\n"
            "    def __init__(self, code, detail, remedy=None):\n"
            "        self.code = code\n",
            encoding="utf-8",
        )
        (package / "cli.py").write_text(
            "from .errors import ProtocolRefusal\n"
            "class _ArtifactParser:\n"
            "    def error(self, message):\n"
            "        raise Exception(message)\n"
            "def _send(args):\n"
            "    field = 'node'\n"
            "    raise ProtocolRefusal(f'{field}_invalid', 'x')\n",
            encoding="utf-8",
        )
        derived = send_reachable_census(root)
        self.assertTrue(
            derived["kind_none"],
            "unevaluable send-path constructors must fail the census, not vanish",
        )

    def test_ordinary_from_and_to_typos_are_named_in_the_census(self) -> None:
        """Deciding line: Bad Name --from/--to were kind-none and invisible."""

        derived = send_reachable_census(REPOSITORY_ROOT)
        self.assertIn("node_invalid", derived["codes"])
        self.assertIn("recipient_invalid", derived["codes"])
        self.assertNotIn("node_invalid", derived["kind_none"])
        self.assertNotIn("recipient_invalid", derived["kind_none"])

    def test_sha_invalid_names_how_to_expand_a_short_id(self) -> None:
        self.assertIn("git rev-parse", SEND_REFUSAL_REMEDIES["sha_invalid"])

    def test_code_parameter_is_followed_from_the_call_site_literal(self) -> None:
        """R4 RED: ProtocolRefusal(code) with code a parameter vanished."""

        derived = send_reachable_census(
            self._planted_tree(
                cli=(
                    "from .helpers import raise_code\n"
                    "class _ArtifactParser:\n"
                    "    def error(self, message):\n"
                    "        raise Exception(message)\n"
                    "def _send(args):\n"
                    "    raise_code('planted_param')\n"
                ),
                extra={
                    "helpers.py": (
                        "from .errors import ProtocolRefusal\n"
                        "def raise_code(code):\n"
                        "    raise ProtocolRefusal(code, 'x')\n"
                    )
                },
            )
        )
        self.assertIn("planted_param", derived["codes"])
        self.assertIn("planted_param", derived["kind_none"])

    def test_method_on_a_local_in_a_plain_function_is_followed(self) -> None:
        """R4 RED: log = EventLog(); log.send() in a plain function stayed green."""

        derived = send_reachable_census(
            self._planted_tree(
                cli=(
                    "from .events import EventLog\n"
                    "class _ArtifactParser:\n"
                    "    def error(self, message):\n"
                    "        raise Exception(message)\n"
                    "def _send(args):\n"
                    "    log = EventLog()\n"
                    "    log.send()\n"
                ),
                extra={
                    "events.py": (
                        "from .errors import ProtocolRefusal\n"
                        "class EventLog:\n"
                        "    def send(self):\n"
                        "        raise ProtocolRefusal('planted_local', 'x')\n"
                    )
                },
            )
        )
        self.assertIn("planted_local", derived["codes"])
        self.assertIn("planted_local", derived["kind_none"])

    def test_inherited_method_is_followed(self) -> None:
        """R4 RED: a Child inheriting Base.send stayed green at R3."""

        derived = send_reachable_census(
            self._planted_tree(
                cli=(
                    "from .events import ChildLog\n"
                    "class _ArtifactParser:\n"
                    "    def error(self, message):\n"
                    "        raise Exception(message)\n"
                    "def _send(args):\n"
                    "    ChildLog().send()\n"
                ),
                extra={
                    "events.py": (
                        "from .errors import ProtocolRefusal\n"
                        "class BaseLog:\n"
                        "    def send(self):\n"
                        "        raise ProtocolRefusal('planted_inherited', 'x')\n"
                        "class ChildLog(BaseLog):\n"
                        "    pass\n"
                    )
                },
            )
        )
        self.assertIn("planted_inherited", derived["codes"])
        self.assertIn("planted_inherited", derived["kind_none"])

    def test_five_shapes_are_followed_only_in_a_planted_tree(self) -> None:
        """R6: the five named plants live in a temp tree, never in floati/."""

        derived = send_reachable_census(
            self._planted_tree(
                cli=(
                    "from .root import resolve_command_root\n"
                    "from .registry import Registry\n"
                    "from .helpers import raise_code\n"
                    "class _ArtifactParser:\n"
                    "    def error(self, message):\n"
                    "        raise Exception(message)\n"
                    "def _root(path):\n"
                    "    return resolve_command_root(path)\n"
                    "def _send(args):\n"
                    "    root = _root(args.root)\n"
                    "    root.planted_gate_probe()\n"
                    "    Registry.planted_static_gate()\n"
                    "    Registry.planted_class_gate()\n"
                    "    raise_code(code='planted_keyword')\n"
                ),
                extra={
                    "root.py": (
                        "from .errors import ProtocolRefusal\n"
                        "from .nested import Nested\n"
                        "class FloatiRoot:\n"
                        "    def __init__(self):\n"
                        "        self.nested = Nested()\n"
                        "    @classmethod\n"
                        "    def open_direct_home(cls, path, create=False):\n"
                        "        return cls()\n"
                        "    def planted_gate_probe(self):\n"
                        "        raise ProtocolRefusal('planted_unique_local', 'x')\n"
                        "    def chained(self):\n"
                        "        self.nested.probe()\n"
                        "def resolve_command_root(path, create=False):\n"
                        "    return FloatiRoot.open_direct_home(path, create=create)\n"
                    ),
                    "nested.py": (
                        "from .errors import ProtocolRefusal\n"
                        "class Nested:\n"
                        "    def probe(self):\n"
                        "        raise ProtocolRefusal('planted_attr_chain', 'x')\n"
                    ),
                    "registry.py": (
                        "from .errors import ProtocolRefusal\n"
                        "class Registry:\n"
                        "    @staticmethod\n"
                        "    def planted_static_gate():\n"
                        "        raise ProtocolRefusal('planted_static_gate', 'x')\n"
                        "    @classmethod\n"
                        "    def planted_class_gate(cls):\n"
                        "        raise ProtocolRefusal('planted_classmethod', 'x')\n"
                    ),
                    "helpers.py": (
                        "from .errors import ProtocolRefusal\n"
                        "def raise_code(code):\n"
                        "    raise ProtocolRefusal(code, 'x')\n"
                    ),
                },
            )
        )
        for code in (
            "planted_unique_local",
            "planted_static_gate",
            "planted_classmethod",
            "planted_keyword",
        ):
            self.assertIn(code, derived["codes"], code)
            self.assertIn(code, derived["kind_none"], code)
        chained = send_reachable_census(
            self._planted_tree(
                cli=(
                    "from .root import FloatiRoot\n"
                    "class _ArtifactParser:\n"
                    "    def error(self, message):\n"
                    "        raise Exception(message)\n"
                    "def _send(args):\n"
                    "    FloatiRoot().chained()\n"
                ),
                extra={
                    "root.py": (
                        "from .nested import Nested\n"
                        "class FloatiRoot:\n"
                        "    def __init__(self):\n"
                        "        self.nested = Nested()\n"
                        "    def chained(self):\n"
                        "        self.nested.probe()\n"
                    ),
                    "nested.py": (
                        "from .errors import ProtocolRefusal\n"
                        "class Nested:\n"
                        "    def probe(self):\n"
                        "        raise ProtocolRefusal('planted_attr_chain', 'x')\n"
                    ),
                },
            )
        )
        self.assertIn("planted_attr_chain", chained["codes"])
        self.assertIn("planted_attr_chain", chained["kind_none"])

    def test_product_send_path_has_no_gate_probes(self) -> None:
        cli = (REPOSITORY_ROOT / "floati" / "cli.py").read_text(encoding="utf-8")
        root = (REPOSITORY_ROOT / "floati" / "root.py").read_text(encoding="utf-8")
        registry = (REPOSITORY_ROOT / "floati" / "registry.py").read_text(encoding="utf-8")
        errors = (REPOSITORY_ROOT / "floati" / "errors.py").read_text(encoding="utf-8")
        self.assertNotIn("__gate_probe__", cli)
        self.assertNotIn("planted_gate_probe", cli + root)
        self.assertNotIn("planted_static_gate", registry)
        self.assertNotIn("planted_local_method_gate", errors)
        self.assertNotIn("planted_static_gate", errors)
        derived = send_reachable_census(REPOSITORY_ROOT)
        self.assertNotIn("planted_unique_local", derived["codes"])
        self.assertNotIn("planted_local_method_gate", derived["codes"])
        self.assertNotIn("planted_static_gate", derived["codes"])

    def test_shared_send_remedy_text_names_only_send_flags(self) -> None:
        """Shared send-map copy may name only flags `floati send` has."""

        from floati.cli import _parser

        parser = _parser()
        send = None
        for action in parser._subparsers._group_actions:
            choices = getattr(action, "choices", None)
            if choices and "send" in choices:
                send = choices["send"]
                break
        self.assertIsNotNone(send)
        send_flags = set()
        for action in send._actions:
            send_flags.update(flag for flag in action.option_strings if flag.startswith("--"))
        leftover: list[tuple[str, str]] = []
        for code, text in SEND_REFUSAL_REMEDIES.items():
            for flag in re.findall(r"--[a-z0-9][a-z0-9-]*", text):
                if flag in send_flags:
                    continue
                if "floati seat board" in text and flag in {"--as", "--session"}:
                    continue
                leftover.append((code, flag))
        self.assertEqual([], leftover, leftover)


if __name__ == "__main__":
    unittest.main()

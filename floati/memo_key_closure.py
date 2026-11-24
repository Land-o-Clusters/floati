"""Version-independent closure of bindings reachable from loaded modules.

Discovery does not walk bytecode and does not call the functions it sees.
It starts at the root functions' modules and follows floati modules those
modules import: globals that are modules or were defined in floati modules,
and import statements resolved against ``sys.modules``. Discovery does not
import a module that is not already loaded. That name stays unresolved, and
the key changes when ordinary execution loads it. If an import cannot be
resolved, the key over-includes every floati module already loaded in this
process.

Every module in that closure contributes every global, every class defined
there (its own ``__dict__`` entries; a read walks the MRO), and every
function's defaults, keyword defaults, and closure cells. A function defined
outside floati is the installed object: replacing it changes the key, and
its own cells are not walked. Mutable containers are frozen by value.

Names that are ``_CACHE`` or end in ``_CACHE`` are not key inputs. They are
derived storage. Replacing a cached value is corruption of a derived result,
not a rebind of an input the memo is allowed to miss.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import types


_CACHE = {}
# Annotation helpers are not bindings. On 3.14 ``__annotate_func__`` closes
# over the class namespace, so freezing it changes from one read to the next.
_SKIP_NAMES = frozenset({
    "__annotations__", "__annotate__", "__annotate_func__", "__builtins__",
    "__cached__", "__dict__", "__doc__", "__file__", "__loader__",
    "__module__", "__name__", "__package__", "__slots__", "__spec__",
    "__weakref__",
})
_TOKEN_CACHE = {}
_INDEX_CACHE = {}
_CYCLE_CACHE = {}
_MISSING_CACHE = {}
_SNAPSHOT_CACHE = {}
_CHECKING_CACHE = set()
_CALLABLE_CACHE = {}
_PROPERTY_CACHE = {}
_IMPORT_CACHE = {}
_REF_CACHE = {}
_DESCRIPTOR_CACHE = {}
_STRUCTURE_CACHE = {}
_NAMESPACE_CACHE = {}
_FLAT_CACHE = object()
_EMPTY_CELL = ("empty-cell",)
_MUTABLE_CONTAINERS = (dict, list, set)


def _skipped_name(name):
    # Derived caches are not inputs. Their names are excluded on purpose:
    # tampering with a cache's values is corruption, and the key tracks the
    # inputs the cache was built from instead.
    return name in _SKIP_NAMES or name == "_CACHE" or name.endswith("_CACHE")


def _is_floati(name):
    return isinstance(name, str) and (name == "floati" or name.startswith("floati."))


def _order(item):
    return repr(item)


def _cycle_token(marker):
    token = _CYCLE_CACHE.get(marker)
    if token is None:
        token = ("cycle", marker)
        _CYCLE_CACHE[marker] = token
    return token


def _freeze(value, seen=None, namespaces=None):
    """Stable binding token.

    Replacing an object yields a new token. A mapping or sequence keeps one
    token until one of its entries is replaced, so the warm path does not
    copy the grammar those entries point at. A function token also covers
    its defaults, keyword defaults, and closure cells.
    """
    namespace = _module_namespace(value)
    if (namespace is not None and namespaces is not None
            and namespaces.get(namespace.__name__) is value):
        # The closure reads this module's bindings individually. Recursing
        # through a globals() alias would reintroduce excluded derived caches
        # and make observation storage part of the semantic contract. An
        # indirect alias not discovered in this closure stays recursively
        # frozen, so it cannot hide semantic changes in an omitted module.
        marker = id(value)
        token = _NAMESPACE_CACHE.get(marker)
        if token is None or token[1] is not namespace:
            token = ("namespace", namespace, marker)
            _NAMESPACE_CACHE[marker] = token
        return token
    if isinstance(value, property):
        marker = id(value)
        if seen is not None and marker in seen:
            return _cycle_token(marker)
        seen = set(seen or ())
        seen.add(marker)
        parts = tuple(_freeze(accessor, seen, namespaces) for accessor in (value.fget, value.fset, value.fdel))
        cached = _PROPERTY_CACHE.get(marker)
        if cached is not None and all(old is new for old, new in zip(cached[2:], parts)):
            return cached
        token = ("property", value) + parts
        _PROPERTY_CACHE[marker] = token
        return token
    unwrapped = _unwrap(value)
    if isinstance(unwrapped, types.FunctionType):
        return _freeze_function(value, unwrapped, seen, namespaces)
    if isinstance(value, tuple):
        marker = id(value)
        if _TOKEN_CACHE.get(marker) is _FLAT_CACHE:
            return value
        if not any(isinstance(item, _MUTABLE_CONTAINERS) or isinstance(item, tuple) for item in value):
            _TOKEN_CACHE[marker] = _FLAT_CACHE
            return value
    elif not isinstance(value, _MUTABLE_CONTAINERS):
        return value
    marker = id(value)
    if marker in _CHECKING_CACHE or (seen is not None and marker in seen):
        return _cycle_token(marker)
    cached = _TOKEN_CACHE.get(marker)
    if cached is _FLAT_CACHE:
        if isinstance(value, tuple):
            return value
        _TOKEN_CACHE.pop(marker, None)
        cached = None
    if cached is not None and marker not in _CHECKING_CACHE and _container_unchanged(value, cached, namespaces):
        return cached
    if seen is None:
        seen = set()
    seen.add(marker)
    _CHECKING_CACHE.add(marker)
    try:
        if isinstance(value, dict):
            items = tuple(sorted(
                ((_freeze(key, seen, namespaces), _freeze(item, seen, namespaces)) for key, item in value.items()),
                key=_order,
            ))
            token = ("dict", marker, items)
            try:
                _INDEX_CACHE[marker] = {key: item for key, item in items}
            except TypeError:
                _INDEX_CACHE[marker] = None
        elif isinstance(value, list) or isinstance(value, tuple):
            items = tuple(_freeze(item, seen, namespaces) for item in value)
            token = ("seq", marker, items)
            _INDEX_CACHE[marker] = items
        else:
            items = tuple(_freeze(item, seen, namespaces) for item in value)
            ordered = tuple(sorted(items, key=_order))
            token = ("set", marker, ordered)
            _INDEX_CACHE[marker] = {id(item): item for item in ordered}
        _TOKEN_CACHE[marker] = token
        return token
    finally:
        _CHECKING_CACHE.discard(marker)


def _container_unchanged(value, cached, namespaces):
    marker = id(value)
    if marker in _CHECKING_CACHE:
        return True
    _CHECKING_CACHE.add(marker)
    try:
        index = _INDEX_CACHE.get(marker)
        if isinstance(value, dict):
            items = cached[2]
            if index is None or len(value) != len(items):
                return False
            for key, item in value.items():
                previous = index.get(_freeze(key, namespaces=namespaces), _MISSING)
                if previous is _MISSING or _freeze(item, namespaces=namespaces) is not previous:
                    return False
            return True
        if isinstance(value, (list, tuple)):
            items = cached[2]
            if len(value) != len(items):
                return False
            for item, previous in zip(value, items):
                if _freeze(item, namespaces=namespaces) is not previous:
                    return False
            return True
        if len(value) != len(cached[2]):
            return False
        if isinstance(index, dict):
            for item in value:
                if id(_freeze(item, namespaces=namespaces)) not in index:
                    return False
            return True
        return False
    finally:
        _CHECKING_CACHE.discard(marker)


def _freeze_function(original, fn, seen, namespaces):
    # A function defined outside floati is the object that was installed.
    # Its defaults and cells belong to that other module, not to the floati
    # closure. Replacing the object still changes the token.
    if isinstance(getattr(fn, "__module__", None), str) and not _is_floati(fn.__module__):
        return original
    marker = id(original)
    defaults = fn.__defaults__
    kw = fn.__kwdefaults__
    closure = fn.__closure__
    cached = _CALLABLE_CACHE.get(marker)
    if cached is not None and cached[0] is defaults and cached[1] is kw and cached[2] is closure:
        if (
            kw is None
            and closure is None
            and (defaults is None or _TOKEN_CACHE.get(id(defaults)) is _FLAT_CACHE)
        ):
            return cached[3]
        if _callable_parts_unchanged(defaults, kw, closure, cached, namespaces):
            return cached[3]
    if seen is None:
        seen = set()
    elif marker in seen:
        return _cycle_token(marker)
    seen = set(seen)
    seen.add(marker)
    if defaults is None:
        frozen_defaults = None
    else:
        frozen_defaults = _freeze(defaults, seen, namespaces)
    if kw is None:
        frozen_kw = None
    else:
        frozen_kw = _freeze(kw, seen, namespaces)
    if closure:
        cells = []
        for cell in closure:
            try:
                cells.append(_freeze(cell.cell_contents, seen, namespaces))
            except ValueError:
                cells.append(_EMPTY_CELL)
        cell_token = tuple(cells)
    else:
        cell_token = None
    token = ("callable", original, frozen_defaults, frozen_kw, cell_token)
    _CALLABLE_CACHE[marker] = (defaults, kw, closure, token, frozen_defaults, frozen_kw, cell_token)
    return token


def _callable_parts_unchanged(defaults, kw, closure, cached, namespaces):
    frozen_defaults, frozen_kw, cell_token = cached[4], cached[5], cached[6]
    if defaults is None:
        defaults_ok = True
    elif isinstance(defaults, tuple) and _TOKEN_CACHE.get(id(defaults)) is _FLAT_CACHE:
        defaults_ok = True
    else:
        defaults_ok = frozen_defaults is not None and _freeze(defaults, namespaces=namespaces) is frozen_defaults
    if kw is None:
        kw_ok = True
    else:
        kw_ok = frozen_kw is not None and _container_unchanged(kw, frozen_kw, namespaces)
    if not closure:
        cells_ok = True
    elif cell_token is None:
        cells_ok = False
    else:
        cells_ok = True
        for cell, previous in zip(closure, cell_token):
            try:
                current = cell.cell_contents
            except ValueError:
                current = _EMPTY_CELL
            if _freeze(current, namespaces=namespaces) is not previous:
                cells_ok = False
                break
    return defaults_ok and kw_ok and cells_ok


def _unwrap(value):
    if isinstance(value, types.MethodType):
        return value.__func__
    if isinstance(value, (staticmethod, classmethod)):
        return value.__func__
    return value


def _bound_function(value, seen=None):
    """Function a root or wrapper actually runs, without calling it."""
    if seen is None:
        seen = set()
    marker = id(value)
    if marker in seen:
        return None
    seen.add(marker)
    value = _unwrap(value)
    if isinstance(value, types.FunctionType):
        return value
    wrapped = getattr(value, "_mock_wraps", None)
    if wrapped is None:
        return None
    return _bound_function(wrapped, seen)


def _live_stamp(globals_dict):
    module_name = globals_dict.get("__name__")
    stamped = []
    for key, value in globals_dict.items():
        if _skipped_name(key):
            continue
        unwrapped = _unwrap(value)
        if not (isinstance(value, (types.ModuleType, type))
                or isinstance(unwrapped, types.FunctionType)
                or _module_namespace(value) is not None):
            continue
        extra = ()
        if isinstance(value, type) and getattr(value, "__module__", None) == module_name:
            extra = tuple(
                (attr, id(raw)) for attr, raw in value.__dict__.items() if not _skipped_name(attr)
            )
        stamped.append((key, id(value), extra))
    return tuple(stamped)


def _cached_refs(globals_dict):
    stamp = _live_stamp(globals_dict)
    marker = id(globals_dict)
    cached = _REF_CACHE.get(marker)
    if cached is not None and cached[0] == stamp:
        return cached[1], cached[2]
    refs, failed = _compute_refs(globals_dict)
    _REF_CACHE[marker] = (stamp, refs, failed)
    return refs, failed


def _compute_refs(globals_dict):
    refs = set()
    parsed, failed = _parsed_imports(globals_dict)
    refs.update(parsed)
    seen = set()
    module_name = globals_dict.get("__name__")
    for key, value in globals_dict.items():
        if _skipped_name(key):
            continue
        _live_refs(value, refs, seen, module_name)
    return frozenset(ref for ref in refs if _is_floati(ref)), failed


def _module_namespace(value):
    if not isinstance(value, dict):
        return None
    name = value.get("__name__")
    if not _is_floati(name):
        return None
    module = sys.modules.get(name)
    if isinstance(module, types.ModuleType) and module.__dict__ is value:
        return module
    return None


def _live_refs(value, refs, seen, module_name):
    marker = id(value)
    if marker in seen:
        return
    seen.add(marker)
    namespace = _module_namespace(value)
    if namespace is not None:
        refs.add(namespace.__name__)
        return
    if isinstance(value, types.ModuleType):
        if _is_floati(getattr(value, "__name__", None)):
            refs.add(value.__name__)
        return
    owning = getattr(value, "__module__", None)
    if _is_floati(owning):
        refs.add(owning)
    if isinstance(value, type):
        for cls in value.__mro__:
            owning = getattr(cls, "__module__", None)
            if _is_floati(owning):
                refs.add(owning)
        if getattr(value, "__module__", None) == module_name:
            for attr, raw in value.__dict__.items():
                if _skipped_name(attr):
                    continue
                _live_refs(raw, refs, seen, module_name)
        return
    fn = _unwrap(value)
    if not isinstance(fn, types.FunctionType):
        return
    for item in fn.__defaults__ or ():
        _live_refs(item, refs, seen, module_name)
    for item in (fn.__kwdefaults__ or {}).values():
        _live_refs(item, refs, seen, module_name)
    if fn.__closure__:
        for cell in fn.__closure__:
            try:
                _live_refs(cell.cell_contents, refs, seen, module_name)
            except ValueError:
                continue


_DYNAMIC_LITERAL = re.compile(
    r"""(?:import_module|__import__)\s*\(\s*['\"]([^'\"]+)['\"]"""
)
_DYNAMIC_CALL = re.compile(r"""(?:import_module|__import__)\s*\(""")


def _import_statements(source):
    """Import statements, including indented and parenthesized ones.

    A line that only looks like an import is still returned. Parsing it may
    fail, and the caller then over-includes.
    """
    block = []
    depth = 0
    for line in source.splitlines():
        stripped = line.strip()
        if not block:
            if not (stripped.startswith("import ") or stripped.startswith("from ")):
                continue
            block = [stripped]
        else:
            block.append(stripped)
        depth += stripped.count("(") - stripped.count(")")
        if depth <= 0 and not stripped.endswith("\\"):
            yield " ".join(block)
            block = []
            depth = 0
    if block:
        yield " ".join(block)


def _parsed_imports(globals_dict):
    marker = id(globals_dict)
    cached = _IMPORT_CACHE.get(marker)
    if cached is not None:
        return cached
    path = globals_dict.get("__file__")
    if not isinstance(path, str):
        cached = (frozenset(), True)
        _IMPORT_CACHE[marker] = cached
        return cached
    if path.endswith(".pyc"):
        source_path = path[:-1]
        if os.path.isfile(source_path):
            path = source_path
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except (OSError, UnicodeError, ValueError):
        cached = (frozenset(), True)
        _IMPORT_CACHE[marker] = cached
        return cached
    package = globals_dict.get("__package__") or ""
    names = set()
    failed = False
    for statement in _import_statements(source):
        try:
            tree = ast.parse(statement)
        except SyntaxError:
            # A prose line can start with "from" or "import". It is not an
            # import. A statement that does parse is the one that counts.
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _is_floati(alias.name):
                        names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                resolved, uncertain = _resolve_from_import(package, node)
                names.update(resolved)
                failed = failed or uncertain
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    for match in _DYNAMIC_LITERAL.finditer(code):
        if _is_floati(match.group(1)):
            names.add(match.group(1))
        else:
            failed = True
    if _DYNAMIC_CALL.search(code) and not _DYNAMIC_LITERAL.search(code):
        failed = True
    cached = (frozenset(names), failed)
    _IMPORT_CACHE[marker] = cached
    return cached


def _resolve_from_import(package, node):
    names = set()
    level = node.level or 0
    if level:
        parts = package.split(".") if package else []
        if level > len(parts):
            return names, True
        prefix = parts[:len(parts) - level + 1]
        if node.module:
            full = ".".join(prefix + node.module.split("."))
            if _is_floati(full):
                names.add(full)
            return names, False
        uncertain = False
        for alias in node.names:
            if alias.name == "*":
                continue
            full = ".".join(prefix + [alias.name])
            kind = _child_module(prefix, alias.name)
            if kind is None:
                uncertain = True
            elif kind and _is_floati(full):
                names.add(full)
        base = ".".join(prefix)
        if _is_floati(base):
            names.add(base)
        return names, uncertain
    base = node.module or ""
    if _is_floati(base) and node.module and "." in node.module:
        names.add(base)
        return names, False
    if base == "floati" or (node.module is None and not level):
        uncertain = False
        parent = base or "floati"
        for alias in node.names:
            if alias.name == "*":
                if _is_floati(parent):
                    names.add(parent)
                continue
            full = parent + "." + alias.name
            kind = _child_module(parent.split("."), alias.name)
            if kind is None:
                uncertain = True
            elif kind:
                names.add(full)
        if _is_floati(parent):
            names.add(parent)
        return names, uncertain
    if _is_floati(base):
        names.add(base)
    return names, False


def _child_module(prefix, child):
    """True when ``child`` is a submodule, False when it is not, None when unknown."""
    if not isinstance(child, str) or not child.isidentifier():
        return False
    package_name = ".".join(part for part in prefix if part)
    full = package_name + "." + child if package_name else child
    if full in sys.modules and isinstance(sys.modules.get(full), types.ModuleType):
        return True
    package = sys.modules.get(package_name) if package_name else None
    search = getattr(package, "__path__", None) if isinstance(package, types.ModuleType) else None
    if not search:
        return None
    for entry in search:
        base = os.path.join(entry, child)
        if os.path.isfile(base + ".py") or os.path.isfile(os.path.join(base, "__init__.py")):
            return True
    return False


def _closure(root_globals, incomplete):
    namespaces = {}
    unresolved = set()
    seen = set()
    pending = []
    over = incomplete
    for globals_dict in root_globals:
        name = globals_dict.get("__name__")
        if isinstance(name, str):
            pending.append((name, globals_dict, True))
        else:
            over = True
    expanded = False
    while True:
        while pending or (over and not expanded):
            if not pending:
                expanded = True
                for key, loaded in list(sys.modules.items()):
                    if _is_floati(key) and isinstance(loaded, types.ModuleType) and key not in namespaces:
                        pending.append((loaded.__name__, loaded.__dict__, False))
                continue
            name, globals_dict, is_root = pending.pop()
            if id(globals_dict) in seen:
                continue
            if not is_root and not _is_floati(name):
                continue
            seen.add(id(globals_dict))
            namespaces[name] = globals_dict
            refs, failed = _cached_refs(globals_dict)
            if failed:
                over = True
            for ref in refs:
                if ref in namespaces or ref in unresolved:
                    continue
                loaded = sys.modules.get(ref)
                if isinstance(loaded, types.ModuleType) and _is_floati(loaded.__name__):
                    pending.append((loaded.__name__, loaded.__dict__, False))
                else:
                    unresolved.add(ref)
        # Do not import here. Loading the whole closure compiles every module
        # and the first waiter call then misses its deadline. A name that is
        # not loaded yet stays unresolved; the next snapshot includes it once
        # ordinary execution has loaded it.
        break
    return namespaces, tuple(sorted(unresolved))


def _name_stamp(globals_dict):
    module_name = globals_dict.get("__name__")
    stamped = []
    for key, value in globals_dict.items():
        if _skipped_name(key):
            continue
        if isinstance(value, type) and getattr(value, "__module__", None) == module_name:
            attrs = tuple(attr for attr in value.__dict__ if not _skipped_name(attr))
            stamped.append((key, attrs))
        else:
            stamped.append(key)
    return tuple(stamped)


def _descriptor_tuple(namespaces, unresolved):
    stamp = (
        tuple((name, _name_stamp(namespaces[name])) for name in sorted(namespaces)),
        unresolved,
    )
    cached = _DESCRIPTOR_CACHE.get(stamp)
    if cached is not None:
        return cached
    recorded = set()
    for module_name, globals_dict in namespaces.items():
        defined = globals_dict.get("__name__")
        for name, value in globals_dict.items():
            if _skipped_name(name):
                continue
            recorded.add(("global", module_name, name))
            if isinstance(value, type) and getattr(value, "__module__", None) == defined:
                qualname = value.__qualname__
                for attr in value.__dict__:
                    if _skipped_name(attr):
                        continue
                    recorded.add(("attr", module_name, qualname, attr))
    for name in unresolved:
        recorded.add(("unresolved", name))
    descriptors = tuple(sorted(recorded, key=_order))
    _DESCRIPTOR_CACHE[stamp] = descriptors
    return descriptors


def _loaded_floati_sig():
    count = 0
    sig = 0
    for key, loaded in sys.modules.items():
        if isinstance(key, str) and (key == "floati" or key.startswith("floati.")):
            count += 1
            sig ^= hash(key) ^ id(loaded)
    return count, sig


def _name_skipped(name):
    return (
        name in _SKIP_NAMES
        or name == "_CACHE"
        or (isinstance(name, str) and name.endswith("_CACHE"))
    )


def _globals_sig(globals_dict):
    """Order-independent signature of the names and callable identities in one module.

    Keyword defaults and container contents are not part of the signature. The
    value read still sees those. A new name, or a replaced function or class,
    changes the signature so discovery runs again.
    """
    sig = len(globals_dict)
    module_name = globals_dict.get("__name__")
    for key, value in globals_dict.items():
        if _name_skipped(key):
            continue
        sig ^= hash(key)
        kind = type(value)
        if _module_namespace(value) is not None:
            sig ^= id(value)
        if kind is types.ModuleType or kind is type or kind is types.FunctionType or kind is types.MethodType or kind is staticmethod or kind is classmethod:
            sig ^= id(value)
        if kind is type and getattr(value, "__module__", None) == module_name:
            for attr, raw in value.__dict__.items():
                if _name_skipped(attr):
                    continue
                sig ^= hash(attr)
                raw_kind = type(raw)
                if raw_kind is type or raw_kind is types.FunctionType or raw_kind is types.MethodType or raw_kind is staticmethod or raw_kind is classmethod or raw_kind is types.ModuleType:
                    sig ^= id(raw)
    return sig


def _reuse_closure(code_key, root_globals, incomplete):
    if incomplete:
        return None
    loaded_sig = _loaded_floati_sig()
    root_ids = tuple(sorted(id(item) for item in root_globals))
    held = _STRUCTURE_CACHE.get(code_key)
    if held is None or held[0] != loaded_sig or held[1] != root_ids:
        return None
    namespaces, sigs = held[2], held[5]
    for name, globals_dict in namespaces.items():
        if _globals_sig(globals_dict) != sigs.get(name):
            return None
    return held


def _store_closure(code_key, root_globals, namespaces, unresolved, descriptors):
    sigs = {name: _globals_sig(globals_dict) for name, globals_dict in namespaces.items()}
    _STRUCTURE_CACHE[code_key] = (
        _loaded_floati_sig(),
        tuple(sorted(id(item) for item in root_globals)),
        namespaces,
        unresolved,
        descriptors,
        sigs,
    )


def _root_globals(functions):
    found = []
    seen = set()
    incomplete = False
    for function in functions:
        globals_dict = getattr(function, "__globals__", None)
        if not isinstance(globals_dict, dict) or not isinstance(globals_dict.get("__name__"), str):
            incomplete = True
            continue
        if id(globals_dict) in seen:
            continue
        seen.add(id(globals_dict))
        found.append(globals_dict)
    return found, incomplete


def binding_snapshot(roots):
    """Frozen aliases and constants reachable from ``roots``.

    Each call re-reads the current bindings, so a rebind changes the snapshot.
    A root that is not itself a function still contributes its object, and a
    wrapper that names the function it calls is walked through that function.
    Discovery over-includes when it cannot see an import; it never drops a
    floati module it has already found.
    """
    functions = []
    standins = []
    for index, root in enumerate(roots):
        function = _bound_function(root)
        if function is None:
            standins.append((index, root))
        else:
            functions.append(function)
            if _unwrap(root) is not function:
                standins.append((index, root))
    root_globals, incomplete = _root_globals(functions)
    code_key = tuple(sorted(id(function.__code__) for function in functions))
    held = _reuse_closure(code_key, root_globals, incomplete)
    if held is None:
        namespaces, unresolved = _closure(root_globals, incomplete)
        descriptors = _descriptor_tuple(namespaces, unresolved)
        _store_closure(code_key, root_globals, namespaces, unresolved, descriptors)
    else:
        namespaces, unresolved, descriptors = held[2], held[3], held[4]
    width = len(descriptors) + len(standins)
    previous = _SNAPSHOT_CACHE.get(code_key)
    if previous is not None and previous[0] == descriptors and len(previous[1]) == width:
        cached = previous[1]
        for index, descriptor in enumerate(descriptors):
            if _read(descriptor, namespaces) is not cached[index][1]:
                break
        else:
            for offset, (_index, root) in enumerate(standins):
                if cached[len(descriptors) + offset][1] is not root:
                    break
            else:
                return cached
    snapshot = tuple((descriptor, _read(descriptor, namespaces)) for descriptor in descriptors)
    if standins:
        snapshot = snapshot + tuple((("root", index), root) for index, root in standins)
    _SNAPSHOT_CACHE[code_key] = (descriptors, snapshot)
    return snapshot


def projection_binding_snapshot():
    """Closure consumed by the waiter projection and its validators."""
    from .cursor_presentations import CursorPresentationLedger, project_wake_items
    from .wake_hold import WakeHoldController
    return binding_snapshot((
        project_wake_items,
        WakeHoldController._read,
        CursorPresentationLedger._select_waiter,
        CursorPresentationLedger._waiter_controller_read,
    ))


class _Missing:
    pass


_MISSING = _Missing()


def _missing_token(descriptor):
    token = _MISSING_CACHE.get(descriptor)
    if token is None:
        token = ("missing", descriptor)
        _MISSING_CACHE[descriptor] = token
    return token


def _loaded_token(name):
    marker = ("loaded", name)
    token = _MISSING_CACHE.get(marker)
    if token is None:
        token = marker
        _MISSING_CACHE[marker] = token
    return token


def _read(descriptor, namespaces):
    if descriptor[0] == "unresolved":
        loaded = sys.modules.get(descriptor[1])
        if isinstance(loaded, types.ModuleType):
            return _loaded_token(descriptor[1])
        return _missing_token(descriptor)
    try:
        globals_dict = namespaces[descriptor[1]]
        if descriptor[0] == "global":
            value = globals_dict[descriptor[2]]
            kind = type(value)
            if (
                kind is str
                or kind is int
                or kind is float
                or kind is bytes
                or kind is bool
                or value is None
                or kind is types.ModuleType
                or kind is type
                or kind is frozenset
            ):
                return value
            return _freeze(value, namespaces=namespaces)
        obj = globals_dict[descriptor[2].split(".", 1)[0]]
        if "." in descriptor[2]:
            for part in descriptor[2].split(".")[1:]:
                obj = obj.__dict__[part]
        for cls in obj.__mro__:
            if descriptor[3] in cls.__dict__:
                return _freeze(cls.__dict__[descriptor[3]], namespaces=namespaces)
    except (KeyError, AttributeError, TypeError):
        return _missing_token(descriptor)
    return _missing_token(descriptor)

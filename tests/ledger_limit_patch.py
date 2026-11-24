"""Patch the native record limit and the module-level names that copy it."""
import ast
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from importlib import import_module
from importlib.util import resolve_name
from pathlib import Path
from unittest.mock import patch


@lru_cache(maxsize=1)
def ledger_limit_bindings():
    product = Path(__file__).resolve().parents[1] / "floati"
    sources = {path: path.read_text(encoding="utf-8") for path in product.rglob("*.py")}
    bindings = {"floati.jsonl.MAX_LEDGER_RECORDS"}
    edges = {}
    while True:
        names = {name.rsplit(".", 1)[1] for name in bindings}
        for path in list(sources):
            source = sources[path]
            if not any(name in source for name in names):
                continue
            del sources[path]
            module = ".".join(path.relative_to(product.parent).with_suffix("").parts)
            package = module.rpartition(".")[0]
            if path.name == "__init__.py":
                module = package
            imports = {}
            for statement in ast.parse(source).body:
                if isinstance(statement, ast.ImportFrom):
                    origin = resolve_name("." * statement.level + (statement.module or ""), package)
                    for alias in statement.names:
                        local = alias.asname or alias.name
                        imports[local] = origin + "." + alias.name
                        edges[module + "." + local] = imports[local]
                elif isinstance(statement, ast.Import):
                    for alias in statement.names:
                        local = alias.asname or alias.name.split(".")[0]
                        imports[local] = alias.name if alias.asname else local
                elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    value = statement.value
                    if isinstance(value, ast.Name):
                        source_name = module + "." + value.id
                    elif isinstance(value, ast.Attribute):
                        parts = ast.unparse(value).split(".")
                        source_name = ".".join([imports.get(parts[0], parts[0]), *parts[1:]])
                    else:
                        continue
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            edges[module + "." + target.id] = source_name
        new = {name for name, source_name in edges.items() if source_name in bindings} - bindings
        if not new:
            return tuple(sorted(bindings))
        bindings.update(new)


@contextmanager
def patched_ledger_limit(limit):
    # Load all owners before changing the first name. A newly imported owner
    # must restore the actual original value when the context exits.
    targets = []
    for coordinate in ledger_limit_bindings():
        module, _, name = coordinate.rpartition(".")
        targets.append((import_module(module), name))
    with ExitStack() as stack:
        for module, name in targets:
            stack.enter_context(patch.object(module, name, limit))
        yield

#!/usr/bin/env python3
"""Save every native discovery occurrence without running test methods."""

import argparse
import ast
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.discovery_census import native_census


def context_evidence(census):
    evidence = {}
    names = {name for origins in census["classes_with_multiple_origins"].values()
             for name in origins}
    for name in sorted(names):
        module = sys.modules[name]
        path = Path(module.__file__)
        source = path.read_text()
        tree = ast.parse(source)
        evidence[name] = {
            "source": str(path.relative_to(ROOT)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "hooks": {hook: callable(getattr(module, hook, None))
                      for hook in ("setUpModule", "tearDownModule", "load_tests")},
            "module_statements": [
                {"line": node.lineno, "source": ast.get_source_segment(source, node)}
                for node in tree.body
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            ],
            "class_expressions": [
                {"line": node.lineno, "source": ast.get_source_segment(source, node)}
                for cls in tree.body if isinstance(cls, ast.ClassDef)
                for node in [*cls.decorator_list, *cls.bases, *cls.body]
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            ],
        }
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if Path.cwd().resolve() != ROOT:
        parser.error("run from the repository root")
    # Ordinary HOME/XDG pin and original-user-site exception, before discovery
    # imports any test module or evaluates its path defaults.
    import tests

    try:
        census = native_census()
        census["context_evidence"] = context_evidence(census)
        encoded = json.dumps(census, sort_keys=True, separators=(",", ":")).encode()
        args.output.write_bytes(gzip.compress(encoded, mtime=0))
        print(json.dumps({key: value for key, value in census.items()
                          if key not in ("occurrences", "context_evidence")}, sort_keys=True))
    finally:
        # Own only the scratch created by the import above, never pre-pin HOME.
        shutil.rmtree(tests.HOME_PIN_ROOT)


if __name__ == "__main__":
    main()

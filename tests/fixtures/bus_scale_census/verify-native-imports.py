"""Check the real native fixture import boundary in a fresh Python process."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types


BANK = Path(__file__).resolve().parent
ROOT = BANK.parents[2]
sys.path.insert(0, str(ROOT))


def load(filename, name):
    specification = importlib.util.spec_from_file_location(name, BANK / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def require_native_boundary(home):
    if os.environ.get("HOME") != home:
        raise AssertionError("native fixture imports changed the child's exact HOME")
    if any(name == "tests" or name.startswith("tests.") for name in sys.modules):
        raise AssertionError("native fixture imports loaded the tests package")


def main():
    home = os.environ.get("HOME")
    require_native_boundary(home)
    entry = load("bus-scale-native-entry-2026-10-09.py", "native_fixture_import_entry")
    adapters = [entry.routine_module(case)
                for case in ("sent", "consume", "wake-pause", "seat-teardown")]
    entry.verify_policy()
    require_native_boundary(home)
    bootstrap = load("_proof_namespace.py", "native_fixture_import_bootstrap")
    namespace = bootstrap.bind()
    modules = [importlib.import_module(namespace.__name__ + "." + name)
               for name in bootstrap.PROOF_MODULES]
    require_native_boundary(home)
    foreign = types.ModuleType(namespace.__name__)
    foreign.__path__ = [str(BANK / "foreign")]
    sys.modules[namespace.__name__] = foreign
    try:
        try:
            bootstrap.bind()
        except RuntimeError:
            if sys.modules[namespace.__name__] is not foreign:
                raise AssertionError("bootstrap replaced the refused foreign namespace")
        else:
            raise AssertionError("bootstrap admitted a foreign namespace")
    finally:
        sys.modules[namespace.__name__] = namespace
    require_native_boundary(home)
    print(json.dumps({"home_preserved": True, "tests_imported": False,
                      "adapters": len(adapters), "proof_modules": len(modules),
                      "foreign_namespace_refused": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Derive REM-2 declarations using the unchanged test module's own census().

--write may bootstrap conflicted PINNED declarations with empty placeholders
solely to import the native census. All imports, census functions and tests
remain unchanged; the placeholders are replaced before success is reported.
An exception restores both input files. Default mode checks a clean module.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tests/test_rem_2.py"
SIDECAR = MODULE.with_name("rem_2_unbound_pin.txt")


def load_module():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("tests.test_rem_2", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    original = MODULE.read_text(encoding="utf-8")
    old_sidecar = SIDECAR.read_text(encoding="utf-8")
    first = re.search(r"^PINNED_[A-Z_]+\s*=", original, re.MULTILINE).start()
    prefix = original[:first]
    marker = prefix.rfind("\n<<<<<<<")
    if marker >= 0:
        prefix = prefix[:marker + 1]
    suffix = original[original.index("def _set_digest("):]
    if any(marker in prefix + suffix for marker in ("<<<<<<<", "=======", ">>>>>>>")):
        raise RuntimeError("Conflict outside derived PINNED declarations")
    section = original[first:original.index("def _set_digest(")]
    names = list(dict.fromkeys(re.findall(r"^(PINNED_[A-Z_]+)\s*=", section, re.MULTILINE)))
    try:
        if args.write:
            stub = "\n\n".join(name + " = None" for name in names)
            MODULE.write_text(prefix + stub + "\n\n\n" + suffix, encoding="utf-8")
        module = load_module()
        derived = module.census(ROOT)
        declarations = []
        for name in names:
            key = name.removeprefix("PINNED_").lower()
            if name == "PINNED_UNRESOLVED_PROTOCOL":
                value = repr(derived[key])
            elif key.endswith("_count"):
                value = repr(len(derived[key.removesuffix("_count")]))
            elif key.endswith("_digest"):
                value = repr(module._set_digest(derived[key.removesuffix("_digest")]))
            elif key == "unbound":
                value = ('frozenset(\n    Path(__file__).with_name("rem_2_unbound_pin.txt")'
                         '.read_text(encoding="utf-8").splitlines()\n)')
            else:
                codes = sorted(derived[key])
                value = "frozenset({\n" + "".join("    " + repr(code) + ",\n" for code in codes) + "})"
            declarations.append(name + " = " + value)
        wanted = prefix + "\n\n".join(declarations) + "\n\n\n" + suffix
        sidecar = "\n".join(sorted(derived["unbound"])) + "\n"
        drift = wanted != original or sidecar != old_sidecar
        if args.write:
            MODULE.write_text(wanted, encoding="utf-8")
            SIDECAR.write_text(sidecar, encoding="utf-8")
            imported = load_module()
            if imported.census(ROOT) != derived or imported.PINNED_UNBOUND != derived["unbound"]:
                raise RuntimeError("Native module/census/sidecar readback differs")
        print(json.dumps({"status": "WROTE" if args.write else "DRIFT" if drift else "UNCHANGED",
            "module_import": True, "census_counts": {key: len(value) if isinstance(value, frozenset) else value
                for key, value in derived.items()},
            "digests": {key: module._set_digest(derived[key]) for key in
                ("protocol", "kind_none", "other", "integrity", "durability")}}, sort_keys=True))
        return 0 if args.write or not drift else 1
    except BaseException:
        if args.write:
            MODULE.write_text(original, encoding="utf-8")
            SIDECAR.write_text(old_sidecar, encoding="utf-8")
        raise


if __name__ == "__main__":
    raise SystemExit(main())

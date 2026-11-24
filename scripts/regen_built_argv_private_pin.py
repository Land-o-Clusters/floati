#!/usr/bin/env python3
"""Regenerate the excluded built-argv file census using the fence's own readers.

Default mode checks for drift; --write replaces the private pin. A public
projection without the export policy cannot regenerate a private census.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIN_RELATIVE = ".github/built-argv-private-pin.v0.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true")
    arguments = parser.parse_args()

    sys.path.insert(0, str(ROOT))
    from tests.export_inventory import (
        classify_inventory_excluded,
        export_policy_is_present,
    )
    from tests.test_no_listener_fence import _built_argv_egress_files

    if not export_policy_is_present(ROOT):
        parser.error("export_policy_absent: cannot derive a private census")
    measured = _built_argv_egress_files([ROOT / "floati", ROOT / "scripts"])
    relative = sorted(Path(path).relative_to(ROOT).as_posix() for path in measured)
    excluded = sorted(classify_inventory_excluded(relative, root=ROOT))
    wanted = "".join(path + "\n" for path in excluded)
    destination = ROOT / PIN_RELATIVE
    current = destination.read_text(encoding="utf-8") if destination.is_file() else ""
    if wanted == current:
        print("UNCHANGED %s  %d files" % (PIN_RELATIVE, len(excluded)))
        return 0
    if arguments.write:
        destination.write_text(wanted, encoding="utf-8")
        print("WROTE     %s  %d files" % (PIN_RELATIVE, len(excluded)))
        return 0
    print("DRIFT     %s  %d files derived. Re-run with --write."
          % (PIN_RELATIVE, len(excluded)))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

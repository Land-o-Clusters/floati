#!/usr/bin/env python3
"""Run one unqualified parallel diagnostic under the immutable Floati profile."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", required=True, type=Path,
                        help="absent private temporary directory for diagnostic evidence")
    parser.add_argument("--costs", required=True, type=Path,
                        help="ordinary historical CSV; advisory partition costs only")
    args = parser.parse_args(arguments)
    try:
        from parallel_qualification import run_diagnostic
        receipt = run_diagnostic(Path(__file__).resolve().parents[1], args.output, args.costs)
    except KeyboardInterrupt:
        receipt = {"status": "interrupted", "qualified": False, "successful": False,
                   "error": "diagnostic interrupted before a terminal receipt returned",
                   "cleanup": {"successful": False, "state": "unknown"}, "exit_code": 130}
    except Exception as exc:
        receipt = {"status": "invalid", "qualified": False, "successful": False,
                   "error": type(exc).__name__ + ": " + str(exc),
                   "cleanup": {"successful": False, "state": "unknown"}}
    print(json.dumps(receipt, sort_keys=True), flush=True)
    if receipt.get("status") == "interrupted":
        return receipt.get("exit_code", 130)
    return 0 if (receipt.get("status") == "complete" and receipt.get("successful") is True
                 and receipt.get("qualified") is False
                 and receipt.get("cleanup", {}).get("successful") is True) else 1


if __name__ == "__main__":
    raise SystemExit(main())

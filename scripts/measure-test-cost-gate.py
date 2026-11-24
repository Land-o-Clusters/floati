#!/usr/bin/env python3
"""Explicit focused cost witness; never discover or execute the full suite.

Run with the same scratch HOME and interpreter as the focused unittest command.
Native recording and test IDs come from Harbor's existing unittest recorder.
Raw logs and the summary are written only to a new private temporary directory.
"""

import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest

from parallel_unittest import _census, run_suite


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    output = Path(tempfile.mkdtemp(prefix="tcg-focused-", dir="\x2fprivate/tmp"))
    load_before = os.getloadavg()
    suite = unittest.defaultTestLoader.loadTestsFromName("tests.test_test_cost_gate")
    census = _census(suite)
    with (output / "unittest.log").open("w") as stream:
        result = run_suite(suite, {"key": "focused", "ids": census["ids"]},
                           census["population_digest"], stream)
    summary = {"status": result["status"], "successful": result["successful"],
               "tests_run": result["tests_run"], "test_durations": result["test_durations"],
               "case_seconds": math.fsum(result["test_durations"].values()),
               "duration_seconds": result["duration_seconds"],
               "selected_ids": result["selected_ids"], "started_ids": result["started_ids"],
               "load_before": load_before, "load_after": os.getloadavg(),
               "raw_log": str(output / "unittest.log")}
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({"summary": str(output / "summary.json"), **summary}, sort_keys=True))
    return 0 if result["successful"] else 1


if __name__ == "__main__":
    sys.exit(main())

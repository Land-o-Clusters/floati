"""Explicit serial diagnostic; no timing or profiling enters test discovery."""

import argparse
import cProfile
import json
import os
from pathlib import Path
import pstats
import sys
import time
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("tests", nargs="+")
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    counts = {"git": 0, "other": 0}

    def audit(event, values):
        if event == "subprocess.Popen":
            command = values[1]
            git = isinstance(command, (list, tuple)) and "/usr/bin/git" in command
            counts["git" if git else "other"] += 1

    sys.addaudithook(audit)
    rows = []

    class TimedResult(unittest.TextTestResult):
        def startTest(self, test):
            self.started = time.perf_counter()
            self.before = dict(counts)
            super().startTest(test)

        def stopTest(self, test):
            rows.append({"test": test.id(), "seconds": time.perf_counter() - self.started,
                         "subprocesses": {key: counts[key] - self.before[key] for key in counts}})
            super().stopTest(test)

    suite = unittest.defaultTestLoader.loadTestsFromNames(args.tests)
    load_before = os.getloadavg()
    started = time.perf_counter()
    profile = cProfile.Profile() if args.profile else None
    if profile:
        profile.enable()
    result = unittest.TextTestRunner(verbosity=2, resultclass=TimedResult).run(suite)
    if profile:
        profile.disable()
        profile.dump_stats(str(args.profile))
    evidence = {"tests": rows, "seconds": time.perf_counter() - started,
                "load_before": load_before, "load_after": os.getloadavg(),
                "run": result.testsRun, "failures": len(result.failures),
                "errors": len(result.errors), "skipped": len(result.skipped),
                "subprocesses": counts}
    if profile:
        stats = pstats.Stats(profile).stats
        evidence["profile"] = [
            {"file": Path(key[0]).name, "line": key[1], "function": key[2],
             "primitive_calls": value[0], "calls": value[1],
             "self_seconds": value[2], "cumulative_seconds": value[3]}
            for key, value in sorted(stats.items(), key=lambda item: item[1][3], reverse=True)
            if "floati/" in key[0] or key[2] in {"run", "_execute_child"}]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

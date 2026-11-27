"""Opt-in native teardown timings; never part of default test discovery.

The caller owns the test hold and supplies an existing mktemp scratch directory
as HOME, TMPDIR and FLOATI_TEST_TEMP_ROOT. The original native scale test runs
unchanged at 100k; the 10k companion borrows exactly its fixture and assertions.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import stat
import sys
import time
import unittest


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
TEST = ("tests.test_seat_teardown_scale.SeatTeardownNativeScaleTests."
    "test_native_observer_final_hundred_thousand_includes_report_pair")


def cpu():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return own.ru_utime + own.ru_stime + children.ru_utime + children.ru_stime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--records", type=int, choices=(10000, 100000), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pre-bank-baseline", action="store_true",
                        help="measure the original witness in the pre-index source archive")
    args = parser.parse_args()
    if args.pre_bank_baseline and ((SOURCE / "floati/send_prefix.py").exists()
                                   or args.records != 100000):
        parser.error("the pre-bank baseline requires the full original witness and no send index")
    scratch = args.output.parent
    try:
        metadata = scratch.stat()
    except OSError:
        metadata = None
    if (scratch.parent != Path("\x2fprivate/tmp") or metadata is None
            or not stat.S_ISDIR(metadata.st_mode) or scratch.is_symlink()
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700
            or any(os.environ.get(name) != str(scratch) for name in
                ("HOME", "TMPDIR", "FLOATI_TEST_TEMP_ROOT"))):
        parser.error("output parent must be the caller's existing private scratch HOME and fixture root")
    try:
        directory = os.open(scratch, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not os.path.samestat(metadata, os.fstat(directory)):
                raise OSError("scratch directory identity changed")
            descriptor = os.open(args.output.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory)
        finally:
            os.close(directory)
    except OSError:
        parser.error("output must name a new summary file in the private scratch directory")
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        return _measure(args.records, output, require_bank=not args.pre_bank_baseline)


def _measure(records, output, *, require_bank=True):
    from tests.test_seat_teardown_scale import SeatTeardownNativeScaleTests
    phases, fixtures, files, banks = [], [], [], []
    SeatTeardownNativeScaleTests.cold_send_index = require_bank

    def timed(label, original):
        def wrapped(case, *positional, **keywords):
            before, started = cpu(), time.perf_counter()
            try:
                value = original(case, *positional, **keywords)
            finally:
                row = {"phase": label, "wall_seconds": time.perf_counter() - started,
                    "cpu_seconds": cpu() - before}
                phases.append(row)
                print(json.dumps(row), flush=True)
            if label == "setUp":
                fixtures.append(str(case.f.base))
            if label == "perform" and not files:
                home = case.root.tenant_home / ".send-prefix"
                bank = home / "bank.bin"
                banks.append(bank.stat().st_size if bank.is_file() else 0)
                files.append(sum(path.is_file() for path in home.rglob("*")))
            return value
        return wrapped

    for name in ("setUp", "seed_history", "perform", "doCleanups"):
        setattr(SeatTeardownNativeScaleTests, name, timed(name, getattr(SeatTeardownNativeScaleTests, name)))
    load_start, started, before = os.getloadavg(), time.perf_counter(), cpu()
    result, code = None, 1
    try:
        if records == 100000:
            result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromName(TEST))
            code = 0 if result.wasSuccessful() else 1
        else:
            case = SeatTeardownNativeScaleTests(TEST.rsplit(".", 1)[1])
            try:
                case.setUp()
                case.seed_history(records - 2)
                case.assert_compact_completion(case.perform(native=True), records - 2)
            finally:
                case.tearDown()
                case.doCleanups()
            code = 0
        if code == 0 and require_bank and not any(banks):
            print("The first native perform did not publish a cold send index bank.", file=sys.stderr)
            code = 1
    finally:
        summary = {"records": records, "test": TEST, "exit": code,
            "wall_seconds": time.perf_counter() - started, "cpu_seconds": cpu() - before,
            "load_start": load_start, "load_end": os.getloadavg(), "phases": phases,
            "fixture_directories": fixtures, "cache_files_after_first_perform": files,
            "bank_bytes_after_first_perform": banks,
            "require_bank": require_bank,
            "test_sha256": hashlib.sha256((SOURCE / "tests/test_seat_teardown_scale.py").read_bytes()).hexdigest()}
        if result is not None:
            summary.update(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                skips=len(result.skipped))
        output.write(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())

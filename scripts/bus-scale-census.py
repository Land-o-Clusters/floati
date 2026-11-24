#!/usr/bin/env python3
"""Run the native L7 census only in an architect-owned quiet window.

Inspect with --dry-run. To measure, create a private empty temporary directory
and pass --run --bank ABSOLUTE_DIRECTORY. Retain the whole bank: result.json
and case evidence accompany the closing BUS-SCALE-CENSUS-1.json receipt.
This command is deliberately outside unittest discovery and floati.selftest.
Use --verdict all-cases for the original gate, including the deferred 1.1 cases.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests import bus_scale_census as instrument
from tests.temp_roots import REAL_TEMP_ROOT
from floati.cli import _parser


# Select controls from the explicit and default suites. The full-scale profile
# restores their original sizes and native timing controls.
FULL_SCALE_MODULES = (
    "tests.harbor_bus_scale_seeds", "tests.harbor_bus_scale_case_seeds",
    "tests.harbor_bus_scale_expanded_observation",
    "tests.test_bus_scale_seat_teardown_boundary",
    "tests.harbor_bus_scale_seat_teardown_observation",
    "tests.harbor_bus_scale_seat_teardown_refusal_observation",
    "tests.harbor_bus_scale_teardown_product_proof",
    "tests.harbor_bus_scale_teardown_host_observation",
    "tests.harbor_bus_scale_teardown_instrument_boundaries",
    "tests.harbor_bus_scale_attention_proof.AttentionProofNativeBindingTests",
    "tests.harbor_bus_scale_arm_clock", "tests.harbor_bus_scale_arm_clock_materialization",
    "tests.harbor_bus_scale_history_output", "tests.harbor_bus_scale_producer",
    "tests.test_registry_prefix", "tests.test_bus_scale_wait_effects",
    "tests.test_bus_scale_wait_control_lock", "tests.harbor_bus_scale_verify_entry_policy",
    "tests.harbor_bus_scale_instrument.CensusInstrumentTests.test_planted_linear_command_is_red_on_all_three_measured_metrics",
    "tests.harbor_bus_scale_instrument.CensusInstrumentTests.test_constant_command_is_green_on_all_three_measured_metrics",
    "tests.harbor_bus_scale_instrument.NativeSendCensusTests",
    "tests.harbor_bus_scale_instrument.CensusNativeWaitTests",
    "tests.harbor_bus_scale_instrument.CensusNativeAttentionTests",
    "tests.harbor_bus_scale_instrument.CensusExpandedHistoryBatchTests",
    "tests.harbor_bus_scale_instrument.CensusExpandedFamilyTests",
    "tests.harbor_bus_scale_routine_reads", "tests.harbor_bus_scale_routine_writes",
    "tests.harbor_bus_scale_routine_lifecycle",
    "tests.harbor_bus_scale_exhaustion_family",
    "tests.harbor_bus_scale_matrix.BusScaleMatrixTests.test_every_family_requires_exactly_five_balanced_pairs",
    "tests.harbor_bus_scale_workloads.BusScaleWorkloadTests.test_every_case_including_history_requires_all_probe_coverage",
    "tests.test_bus_scale_cursor_prefix_effects",
    "tests.test_chart_registry_prefix",
    "tests.test_send_prefix", "tests.test_cursor_prefix", "tests.test_consumed_prefix",
    "tests.test_cursor_selected_consumption", "tests.test_receipt_prefix",
    "tests.test_inbox_prefix", "tests.test_fixture_registry_cache",
)


def full_scale_suite():
    from tests.bus_scale_test_profile import full_scale
    with full_scale():
        return unittest.defaultTestLoader.loadTestsFromNames(FULL_SCALE_MODULES)


def _leaves(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _leaves(item)
        else:
            yield item


def run_full_scale_controls(bank):
    from tests.bus_scale_test_profile import full_scale
    bank.mkdir(mode=0o700)
    with full_scale():
        suite = full_scale_suite()
        expected = [test.id() for test in _leaves(suite)]
        started = time.monotonic()
        with (bank / "unittest.log").open("x") as stream, contextlib.redirect_stdout(stream):
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
        receipt = {"successful": result.wasSuccessful() and not result.skipped,
                   "tests": result.testsRun, "expected_ids": expected,
                   "seconds": time.monotonic() - started,
                   "failures": [test.id() for test, _ in result.failures],
                   "errors": [test.id() for test, _ in result.errors],
                   "skips": [test.id() for test, _ in result.skipped]}
        receipt["successful"] &= result.testsRun == len(expected)
    (bank / "result.json").write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    return receipt


def emit(value):
    print(json.dumps(value, sort_keys=True))


def refuse(code, detail):
    emit({"row_id": "BUS-SCALE-CENSUS-1", "status": "refused",
          "evidence": {"code": code, "detail": detail}})
    return 20


def instrument_refusal(result):
    mismatch = instrument.ChildInterpreterMismatch.code
    clock_codes = {"preparation_deadline", "measured_deadline", "child_command_deadline", "child_collection_deadline"}
    cases = {case: error["code"] for case, error in result["errors"].items()
             if error.get("code") == mismatch
             or (case in instrument.EXPANDED_SPLIT_CLOCK and error.get("code") in clock_codes)}
    if not cases:
        return {}
    codes = set(cases.values())
    if mismatch in codes:
        return {"status": "refused", "evidence": {"code": mismatch, "cases": cases}}
    return {"status": "refused", "evidence": {
        "code": next(iter(codes)) if len(codes) == 1 else "census_clock_deadlines", "cases": cases}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="list all cases; launch no native measurements")
    mode.add_argument("--run", action="store_true", help="explicitly start the full native measurement")
    mode.add_argument("--measure", nargs="+", metavar="CASE",
                      help="measure selected native cases and retain their private evidence bank")
    parser.add_argument("--bank", type=Path, help="existing canonical private empty owned temporary directory")
    parser.add_argument("--verdict", choices=("l7", "all-cases"), default="l7",
                        help="L7 owner-ruling deferrals (default), or the original all-case gate")
    args = parser.parse_args(argv)
    if Path.cwd().resolve() != ROOT:
        return refuse("census_source_root_required", "run from this source worktree root")
    if not args.run and not args.dry_run and not args.measure:
        return refuse("census_mode_required", "choose --dry-run or explicit --run --bank PATH")
    try:
        verdict = instrument.verdict_population(set(instrument.population(_parser())["cases"]) - {"send"},
                                                args.verdict)
    except (OSError, ValueError) as error:
        return refuse("census_deferrals_invalid", str(error))
    if args.dry_run:
        if args.bank is not None:
            return refuse("census_dry_bank_forbidden", "--dry-run does not create or use a bank")
        cases = verdict["judged"]["cases"]
        emit({"row_id": "BUS-SCALE-CENSUS-1", "status": "planned",
              "verdict": verdict,
              "pending": cases, "case_count": len(cases), "native_measurement": False,
              "additional_cases": ["send"], "calibration_controls": ["linear", "constant"],
              "full_scale_controls": [test.id() for test in _leaves(full_scale_suite())]})
        return 0
    if args.measure and (not set(args.measure).issubset(verdict["judged"]["cases"])
                         or len(set(args.measure)) != len(args.measure)):
        return refuse("census_selection_invalid", "select distinct cases included in the requested verdict")
    bank = args.bank
    if args.measure and bank is None:
        bank = Path(tempfile.mkdtemp(dir=REAL_TEMP_ROOT))
    if bank is None:
        return refuse("census_bank_required", "--run requires --bank PATH")
    # Admit before the instrument can launch, and never replace an old receipt.
    try:
        if (not bank.is_absolute() or bank.resolve(strict=True) != bank
                or Path(REAL_TEMP_ROOT) not in bank.parents or not bank.is_dir()
                or bank.stat().st_uid != os.getuid() or bank.stat().st_mode & 0o077
                or any(bank.iterdir())):
            raise ValueError("bank must be canonical, private, owned, empty temporary storage")
    except (OSError, ValueError, RuntimeError) as error:
        return refuse("census_bank_refused", str(error))
    # Native progress goes to stderr; stdout is the one closing artifact.
    from tests.bus_scale_test_profile import full_scale
    if args.measure:
        print("BUS_SCALE_BANK " + str(bank), file=sys.stderr, flush=True)
        with full_scale(), contextlib.redirect_stdout(sys.stderr):
            result = instrument.remaining_census(bank, cases=args.measure, verdict_mode=args.verdict)
        refusal = instrument_refusal(result)
        emit({"bank": str(bank), "source": result["source_before"]["commit"],
              "selected": result["selected"], "measured": result["measured"],
              "cases": result["cases"], "errors": result["errors"], "findings": result["findings"],
              "selection_complete": result["selection_complete"], "source_unchanged": result["source_unchanged"],
              **refusal})
        if refusal:
            return 20
        return int(not (result["selection_complete"] and result["source_unchanged"]
                        and not result["errors"] and not result["findings"]))
    with full_scale(), contextlib.redirect_stdout(sys.stderr):
        result = instrument.remaining_census(bank, verdict_mode=args.verdict)
        controls = run_full_scale_controls(bank / "full-scale-controls")
    receipt = dict(result, row_id="BUS-SCALE-CENSUS-1", full_scale_controls=controls)
    cases = set(verdict["judged"]["cases"])
    complete = (result["status"] == "green" and not result["pending"]
                and not result["findings"] and not result["errors"]
                and result["source_unchanged"] is True
                and result["native_measurement"] is True
                and result.get("verdict") == verdict
                and len(result["measured"]) == len(cases)
                and set(result["measured"]) == cases
                and controls["successful"] is True)
    if not complete and receipt["status"] == "green":
        receipt["status"] = "incomplete"
    refusal = instrument_refusal(result)
    receipt.update(refusal)
    with (bank / "BUS-SCALE-CENSUS-1.json").open("x") as stream:
        stream.write(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    emit(receipt)
    if refusal:
        return 20
    return 0 if complete else 1


if __name__ == "__main__":
    raise SystemExit(main())

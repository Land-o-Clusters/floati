"""Explicit native replay of three instrument-blocked cases; no discovery."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from floati.cli import _parser
from tests import bus_scale_census as census
from tests.temp_roots import REAL_TEMP_ROOT


PRIOR = Path("\x2fprivate/tmp/bus-scale-pending-result-xvwts5ps/result.json")


def run_followup():
    selected = ["intake-preview", "node-prompts", "verify"]
    prior = json.loads(PRIOR.read_text())
    dispositions = {}
    for case in set(census.population(_parser())["cases"]) - {"send"} - set(selected):
        dispositions[case] = prior["selection"]["prior_dispositions"].get(case, {
            "reason": "prior_native_measurement" if case in prior["measured"]
                      else "prior_host_permission_block_unmeasured",
            "evidence": str(PRIOR)})
    bank = Path(tempfile.mkdtemp(prefix="bus-scale3-replay-", dir=REAL_TEMP_ROOT)).resolve()
    print("BUS_SCALE3_REPLAY_BANK " + str(bank), flush=True)
    try:
        result = census.remaining_census(bank, cases=selected, prior_dispositions=dispositions)
        result["followup_observer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        census._write_json(bank / "result.json", result)
        if result["selected_pending"] or not result["source_unchanged"]:
            raise AssertionError(json.dumps(result["errors"], sort_keys=True))
    finally:
        print("BUS_SCALE3_REPLAY_RESULT " + str(bank / "result.json"), flush=True)


def load_tests(loader, tests, pattern):
    # This observes complete native costs, including failures of the 1.5
    # threshold; it does not waive findings or close the unselected census.
    return unittest.TestSuite([unittest.FunctionTestCase(run_followup,
        description="native replay after strict instrument repairs")])

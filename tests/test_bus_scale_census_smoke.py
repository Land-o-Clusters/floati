"""Default coverage for the explicit bus-scale census controls."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import unittest


# Pinned from the composed discovery census. Update with the moved-ID map.
# The train renamed one workloads method (all_146 -> all_full); the count holds.
EXPLICIT_COUNT = 440
EXPLICIT_SHA256 = "f634699e979b582d6be20eda3a546d7cbdd119c12a48a17563f741acd7368b41"
ENTRY = Path(__file__).resolve().parents[1] / "scripts/bus-scale-census.py"


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


class BusScaleCensusSmokeTests(unittest.TestCase):
    def test_explicit_population_and_census_entry(self):
        module = "tests.harbor_bus_scale_census"
        self.assertIsNotNone(importlib.util.find_spec(module), module)
        loader = unittest.TestLoader()
        ids = sorted(test_ids(loader.loadTestsFromName(module)))
        self.assertEqual([], loader.errors)
        self.assertEqual(EXPLICIT_COUNT, len(ids))
        encoded = "".join(test_id + "\n" for test_id in ids).encode("utf-8")
        self.assertEqual(EXPLICIT_SHA256, hashlib.sha256(encoded).hexdigest())

        spec = importlib.util.spec_from_file_location("bus_scale_census_smoke_entry", ENTRY)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        output = io.StringIO()
        # The dry run exercises the real parser and plan without a native bank.
        with contextlib.redirect_stdout(output):
            status = entry.main(["--dry-run"])
        self.assertEqual(0, status, output.getvalue())
        plan = json.loads(output.getvalue())
        self.assertEqual("planned", plan["status"])
        self.assertFalse(plan["native_measurement"])
        self.assertGreater(plan["case_count"], 0)
        self.assertEqual(plan["case_count"], len(plan["pending"]))
        self.assertTrue(plan["full_scale_controls"])
        self.assertFalse(any("_FailedTest" in test_id for test_id in plan["full_scale_controls"]))

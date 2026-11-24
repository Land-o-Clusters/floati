"""Representative onboarding fixtures work without either installed TOML parser.

SEAT-ONBOARD-1-F1: product refusal is intentional on Python 3.9 without
tomli; the portable fixtures must exercise that refusal instead of assuming
that every supported interpreter can prepare a TOML ownership plan. All ordinary
tests still run once; this deliberately narrower smoke does not execute every
test body again under the outer blocker or claim cold imports of cached modules.
"""
from __future__ import annotations

import builtins
from collections import Counter
import io
import sys
from types import ModuleType
import unittest
from unittest import mock


ONBOARDING_MODULES = (
    "tests.test_seat_onboard",
    "tests.test_seat_onboard_cli_holding",
    "tests.test_seat_onboard_history",
    "tests.test_seat_onboard_hook",
    "tests.test_seat_onboard_lifecycle",
    "tests.test_seat_onboard_process_failure",
    "tests.test_seat_onboard_record",
    "tests.test_seat_onboard_recovery",
    "tests.test_seat_onboard_transport",
    "tests.test_seat_harness_config",
    "tests.test_seat_toml_fixture",
)
PARSERS = frozenset({"tomllib", "tomli"})
SMOKE_TESTS = (
    "tests.test_seat_onboard.SeatOnboardingTests.test_one_remove_restores_configuration_and_leaves_no_owned_home",
    "tests.test_seat_onboard_transport.SeatOnboardTransportTests.test_broken_transport_does_not_block_owned_seat_removal",
    "tests.test_seat_onboard_record.SeatOnboardRecordTests.test_effect_free_preparing_record_without_stage_is_readable",
    "tests.test_seat_harness_config.SeatHarnessConfigTests.test_exact_baseline_bytes_and_modes_return_on_restore",
    "tests.test_seat_toml_fixture.SeatTomlFixtureTests.test_tomli_fixture_exercises_the_actual_production_fallback_branch",
    "tests.test_seat_toml_fixture.SeatTomlFixtureIsolationTests.test_contained_fixture_setup_failure_restores_importer_and_scratch",
    "tests.test_seat_toml_fixture.SeatTomlFixtureIsolationTests.test_import_patch_is_local_and_always_removed_by_cleanup",
)


def _case_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _case_ids(item)
        else:
            yield item.id()


class SeatOnboardParserAbsenceTests(unittest.TestCase):
    def test_fixture_smoke_handles_both_toml_parsers_absent_without_skips(self):
        """Catch fixtures that mistake a remediable parser absence for a bug."""
        original_import = builtins.__import__
        blocked = Counter()

        def without_parsers(name, *args, **kwargs):
            root = name.partition(".")[0]
            if root in PARSERS:
                blocked[root] += 1
                raise ModuleNotFoundError(
                    "SEAT-ONBOARD-1-F1 deliberately unavailable: " + root,
                    name=root,
                )
            return original_import(name, *args, **kwargs)

        # Cached parser modules must not defeat the import boundary. Existing
        # entries are preserved; absent entries receive harmless sentinels.
        absent = object()
        original_cache = {name: sys.modules.get(name, absent) for name in PARSERS}
        stream = io.StringIO()
        try:
            for name, cached in original_cache.items():
                if cached is absent:
                    sys.modules[name] = ModuleType(name)
            with mock.patch("builtins.__import__", side_effect=without_parsers):
                for name in sorted(PARSERS):
                    with self.assertRaises(ModuleNotFoundError):
                        builtins.__import__(name)
                loader = unittest.TestLoader()
                modules = loader.loadTestsFromNames(ONBOARDING_MODULES)
                self.assertGreater(modules.countTestCases(), 0, "module checks loaded no tests")
                self.assertFalse(loader.errors, "aggregate module loading failed")
                self.assertEqual(len(SMOKE_TESTS), len(set(SMOKE_TESTS)),
                                 "fixture smoke IDs must be unique")
                suite = loader.loadTestsFromNames(SMOKE_TESTS)
                expected_count = suite.countTestCases()
                self.assertFalse(loader.errors, "fixture smoke loading failed")
                self.assertEqual(SMOKE_TESTS, tuple(_case_ids(suite)))
                result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        finally:
            # Only the two controlled entries belong to this fixture. Retain
            # unrelated modules loaded by the aggregate and their identities.
            for name, cached in original_cache.items():
                if cached is absent:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = cached

        self.assertIs(original_import, builtins.__import__)
        for name, cached in original_cache.items():
            if cached is absent:
                self.assertNotIn(name, sys.modules)
            else:
                self.assertIs(cached, sys.modules[name])

        problems = [("failure", test.id(), trace) for test, trace in result.failures]
        problems.extend(("error", test.id(), trace) for test, trace in result.errors)
        reasons = Counter(trace.strip().splitlines()[-1] for _, _, trace in problems)
        summary = (
            "parser-absence fixture smoke: modules={0}, tests={1}/{2}, failures={3}, "
            "errors={4}, skips={5}; blocked={6}".format(
                len(ONBOARDING_MODULES), result.testsRun, expected_count,
                len(result.failures), len(result.errors), len(result.skipped),
                dict(sorted(blocked.items())),
            )
        )
        evidence = [summary]
        evidence.extend(
            "reason x{0}: {1}".format(count, reason[:500])
            for reason, count in reasons.most_common(6)
        )
        evidence.extend(
            "{0}: {1}: {2}".format(kind, identity, trace.strip().splitlines()[-1][:500])
            for kind, identity, trace in problems[:10]
        )
        diagnostic = "\n".join(evidence)
        self.assertEqual(expected_count, result.testsRun, diagnostic)
        self.assertFalse(result.skipped, diagnostic)
        self.assertTrue(result.wasSuccessful(), diagnostic)


if __name__ == "__main__":
    unittest.main()

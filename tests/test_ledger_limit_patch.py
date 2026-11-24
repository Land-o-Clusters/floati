"""The default teardown boundary changes every ledger-limit binding together."""
import unittest
from unittest.mock import patch

from floati import archive_source_pool, jsonl, seat_teardown_capacity
from tests import test_seat_teardown_scale as scale


class LedgerLimitPatchTests(unittest.TestCase):
    def test_default_boundary_uses_one_small_limit_and_restores_all_bindings(self):
        original = jsonl.MAX_LEDGER_RECORDS
        case = scale.SeatTeardownScaleTests(
            "test_initial_hundred_thousand_refuses_capacity_before_any_teardown_effect")
        observed = []

        def inspect_history(count):
            observed.append((count, jsonl.MAX_LEDGER_RECORDS, archive_source_pool.MAX_AXIS_RECORDS,
                             seat_teardown_capacity.MAX_LEDGER_RECORDS))

        with patch.object(case, "seed_history", inspect_history), patch.object(case, "assert_no_effect_refusal"):
            result = unittest.TestResult()
            case.run(result)
        self.assertTrue(result.wasSuccessful(), result.errors + result.failures)
        count = observed[0][0]
        self.assertLess(count, original)
        self.assertEqual([(count, count, count, count), (count - 1, count, count, count)], observed)
        self.assertEqual((original,) * 3, (jsonl.MAX_LEDGER_RECORDS, archive_source_pool.MAX_AXIS_RECORDS,
                                          seat_teardown_capacity.MAX_LEDGER_RECORDS))

    def test_source_binding_population_is_pinned_and_every_value_is_restored(self):
        from importlib import import_module
        from tests.ledger_limit_patch import ledger_limit_bindings, patched_ledger_limit
        bindings = ledger_limit_bindings()
        # SCALE measured 16 on main. The 1122b train adds three module-level
        # bindings: dispatch records, the quiet window and replay history;
        # the inbound hold adds a fourth. Train 1122f adds a fifth:
        # confluence_fleet_state passes the bound to the roll-lineage read in
        # _roll_receipts, so it must move with the others.
        self.assertEqual(21, len(bindings), bindings)
        self.assertIn("floati.confluence_fleet_state.MAX_LEDGER_RECORDS", bindings)
        owners = [(import_module(module), name) for module, _, name in
                  (coordinate.rpartition(".") for coordinate in bindings)]
        original = [getattr(module, name) for module, name in owners]
        self.assertEqual({100000}, set(original))
        with self.assertRaises(InterruptedError):
            with patched_ledger_limit(17):
                self.assertEqual({17}, {getattr(module, name) for module, name in owners})
                raise InterruptedError()
        self.assertEqual(original, [getattr(module, name) for module, name in owners])

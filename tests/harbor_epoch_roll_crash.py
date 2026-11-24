"""Explicit epoch-roll crash recovery tests over boundaries and mutation ordinals."""

import unittest

from tests import test_bus_epoch_roll as epoch_fixture


class _AbruptConvergenceUnit:
    """One independently schedulable slice of the abrupt-death matrix.

    A mixin, not a TestCase, so the loader never collects it on its own.  Each
    unit drives a fresh GovernedBusEpochRollTests harness (its setUp gives the
    unit its own temporary tree) and runs every case as a subtest of the unit.
    """

    ABRUPT_MODE = ""
    ABRUPT_SHARD = 0

    def _abrupt_harness(self) -> epoch_fixture.GovernedBusEpochRollTests:
        harness = epoch_fixture.GovernedBusEpochRollTests(
            "test_doctor_never_initiates_roll_or_mutates_complete_state"
        )
        harness.setUp()
        self.addCleanup(harness.doCleanups)  # type: ignore[attr-defined]
        return harness

    def test_abrupt_cases_converge(self) -> None:
        case: unittest.TestCase = self  # type: ignore[assignment]
        harness = self._abrupt_harness()
        if self.ABRUPT_MODE == "boundary":
            boundaries, _roll = harness._fault_contract()
            case.assertTrue(boundaries)
            abrupt_child = harness._abrupt_fork_server()
            for index, boundary in enumerate(boundaries):
                with case.subTest(boundary=boundary):
                    harness._abrupt_boundary_case(abrupt_child, index, boundary)
            return
        case.assertEqual("ordinal", self.ABRUPT_MODE)
        ordinals = epoch_fixture.GovernedBusEpochRollTests._abrupt_shard_ordinals(
            harness._abrupt_mutation_count(), self.ABRUPT_SHARD, epoch_fixture.ABRUPT_ORDINAL_SHARDS
        )
        case.assertTrue(ordinals, "an ordinal unit with no ordinals is a vacuous green")
        abrupt_child = harness._abrupt_fork_server()
        for ordinal in ordinals:
            with case.subTest(mutation_ordinal=ordinal):
                harness._abrupt_ordinal_case(abrupt_child, ordinal)


class AbruptBoundaryConvergenceTests(_AbruptConvergenceUnit, unittest.TestCase):
    ABRUPT_MODE = "boundary"


class AbruptOrdinalConvergenceShard0Tests(_AbruptConvergenceUnit, unittest.TestCase):
    ABRUPT_MODE, ABRUPT_SHARD = "ordinal", 0


class AbruptOrdinalConvergenceShard1Tests(_AbruptConvergenceUnit, unittest.TestCase):
    ABRUPT_MODE, ABRUPT_SHARD = "ordinal", 1


class AbruptOrdinalConvergenceShard2Tests(_AbruptConvergenceUnit, unittest.TestCase):
    ABRUPT_MODE, ABRUPT_SHARD = "ordinal", 2


class AbruptOrdinalConvergenceShard3Tests(_AbruptConvergenceUnit, unittest.TestCase):
    ABRUPT_MODE, ABRUPT_SHARD = "ordinal", 3

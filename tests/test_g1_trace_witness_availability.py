"""The missed-poll regression consumes completed testimony without live Git.

Fresh Git availability is already measured by the separate native G1 gate.
This regression must remain executable when that availability is withheld.
"""
import subprocess
import unittest
from unittest.mock import patch

from tests import test_g1_trace_witness as regression


class RecordedWitnessAvailabilityTests(unittest.TestCase):
    def test_missed_poll_regression_does_not_start_another_git_fixture(self):
        case = regression.PositiveWitnessRaceRegression(
            "test_g1_still_proves_the_mechanism_when_every_process_poll_misses")
        with patch.object(subprocess, "run", side_effect=AssertionError(
                "recorded witness regression must not launch live Git")) as launch:
            case.debug()
        launch.assert_not_called()

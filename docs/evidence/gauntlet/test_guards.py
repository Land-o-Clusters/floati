"""RED-first guards for the WS-H skeleton runner. Not a product suite."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_gauntlet import LIVE_FLEET_ROOT, GauntletGuardError, require_scratch_root


class GauntletGuardTests(unittest.TestCase):
    def test_live_fleet_root_is_refused(self) -> None:
        with self.assertRaises(GauntletGuardError) as caught:
            require_scratch_root(LIVE_FLEET_ROOT)
        self.assertEqual("live_fleet_forbidden", caught.exception.code)

    def test_path_outside_clone_scratch_is_refused(self) -> None:
        with self.assertRaises(GauntletGuardError) as caught:
            require_scratch_root(Path("/private/tmp/floati-work"))
        self.assertEqual("scratch_containment", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

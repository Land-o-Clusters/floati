from __future__ import annotations

import unittest

from tests.test_draft_night_watch import DRAFT_TEST_IDS as NIGHT_WATCH_IDS
from tests.test_draft_window_scheduling import DRAFT_TEST_IDS as WINDOW_IDS


class DraftDiscoveryContractTests(unittest.TestCase):
    def test_window_scheduling_contract_is_present_in_ordinary_discovery(self) -> None:
        """Catches deleting the adapter while leaving the dark package apparently green."""
        self.assertEqual(12, len(WINDOW_IDS))
        self.assertTrue(any(value.endswith(".test_red_1_an_unknown_window_schedules_nothing") for value in WINDOW_IDS))
        self.assertTrue(any(value.endswith(".test_closed_refusal_set_is_documented_bijectively") for value in WINDOW_IDS))

    def test_night_watch_contract_is_present_in_ordinary_discovery(self) -> None:
        """Catches omitting either the fence bank or the nineteen scenario receipts."""
        self.assertEqual(19, len(NIGHT_WATCH_IDS))
        self.assertTrue(any(value.endswith(".test_product_manifest_never_names_this_draft") for value in NIGHT_WATCH_IDS))
        self.assertTrue(any(value.endswith(".test_16_renderer_emits_placeholder_keys_only") for value in NIGHT_WATCH_IDS))


if __name__ == "__main__":
    unittest.main()

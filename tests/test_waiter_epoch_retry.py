"""Native Stop lock/member races retain bounded re-arm and exact refusals."""
from pathlib import Path
import tempfile
import unittest

from tests.temp_roots import REAL_TEMP_ROOT
from tests.probe_waiter_epoch_retry import run_case


class WaiterEpochRetryTests(unittest.TestCase):
    def check_native(self, case):
        with tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT) as base:
            result = run_case(case, Path(base) / case)
            for name, passed in result["checks"].items():
                with self.subTest(check=name):
                    self.assertTrue(passed, result)

    def test_epoch_lock_deadline_blocks_to_rearm_without_integrity_exit(self):
        self.check_native("lock_deadline")

    def test_epoch_lock_release_delivers_exact_mail_in_original_window(self):
        self.check_native("lock_release")

    def test_epoch_acquisition_timeout_then_release_retries_in_original_window(self):
        self.check_native("lock_retry_release")

    def test_short_epoch_contention_preserves_strict_native_map_refusal(self):
        self.check_native("lock_short_map")

    def test_actual_selected_identity_change_replans_and_delivers(self):
        self.check_native("member_once")

    def test_corruption_after_selected_race_retains_native_refusal(self):
        self.check_native("member_corrupt")

    def test_member_retry_rechecks_actual_native_pause_before_delivery(self):
        self.check_native("member_pause")

    def test_same_code_planner_protocol_refusal_is_not_retried_as_race(self):
        self.check_native("planner_refusal")

    def test_same_code_integrity_failure_is_not_retried_as_race(self):
        self.check_native("member_integrity")

"""Native hereditary projection must preserve cuts, failures and outer lifetime."""
from pathlib import Path
import tempfile
import unittest

from tests.probe_epoch_prefix import run_controls
from tests.temp_roots import REAL_TEMP_ROOT


class EpochPrefixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.result = run_controls(Path(cls.temporary.name) / "native")

    def controls(self, *names):
        for name in names:
            with self.subTest(control=name):
                self.assertTrue(self.result["checks"].get(name, False), self.result)

    def test_outer_native_replay_work_is_one_admission_per_archive(self):
        self.controls("first_outer_replays_at_most_one_complete_archive",
            "second_outer_replays_at_most_one_complete_archive",
            "first_outer_has_native_imported_origins", "second_outer_admits_new_origins")

    def test_r3_roll_preserves_states_through_native_v3_holds(self):
        self.controls("r3_roll_uses_native_v3_holds")

    def test_all_small_cuts_preserve_complete_native_output_and_refusal(self):
        self.controls("private_prefix_index_available",
            "every_small_cut_matches_native_bytes_or_exact_refusal",
            "small_controls_include_valid_and_inconsistent_cuts",
            "supported_valid_cuts_use_one_native_admission")

    def test_physical_gaps_metadata_and_state_precedence_are_retained(self):
        self.controls("small_source_has_physical_foreign_and_metadata_gaps",
            "retraction_ack_hold_and_counts_preserve_precedence",
            "unpresented_delivery_counts_without_hold", "presented_delivery_counts_and_holds",
            "native_roll_states_and_counts")

    def test_repeated_digest_accepts_any_sufficient_occurrence(self):
        self.controls("repeated_digest_uses_earliest_sufficient_occurrence")

    def test_late_semantic_failure_does_not_reject_an_earlier_valid_cut(self):
        self.controls("late_semantically_bad_delivery_earlier_valid_cut_survives",
            "late_semantically_bad_delivery_exact_native_fallback",
            "late_semantically_bad_ack_earlier_valid_cut_survives",
            "late_semantically_bad_ack_exact_native_fallback")

    def test_unsupported_nonhereditary_teardown_uses_native_refusal_point(self):
        self.controls("unsupported_teardown_complete_uses_native_fallback",
            "unsupported_teardown_incomplete_uses_native_fallback")

    def test_ordinary_roll_head_and_imported_holds_keep_their_distinct_boundaries(self):
        self.controls("ordinary_roll_head_without_teardown_is_supported",
            "imported_hold_physical_hashes_require_native_fallback",
            "imported_hold_fallback_preserves_sealed_native_states")

    def test_returned_testimony_and_outer_scope_are_isolated(self):
        self.controls("public_origin_mutation_cannot_poison_later_testimony",
            "outer_verification_scope_is_closed", "second_outer_admits_new_origins")

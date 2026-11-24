"""Indexed native wake replay preserves proofs, refusal order and bounded work."""
from pathlib import Path
import tempfile
import unittest

from tests.temp_roots import REAL_TEMP_ROOT
from tests.probe_wake_projection_index import run_controls


class WakeProjectionIndexTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.result = run_controls(Path(cls.temporary.name) / "native")

    def check_controls(self, *names):
        for name in names:
            with self.subTest(control=name):
                self.assertTrue(self.result["checks"][name], self.result)

    def test_native_replay_does_not_rescan_events_or_prefixes_for_every_hold(self):
        self.check_controls("event_work_independent_of_hold_count", "prefix_work_independent_of_hold_count")

    def test_native_physical_states_and_ack_precedence_are_preserved(self):
        self.check_controls("native_exact_held_states", "native_plain_and_counted_equivalent",
            "native_ack_still_wins_over_hold")

    def test_reordered_duplicate_and_foreign_ids_keep_exact_native_refusals(self):
        self.check_controls("reordered_ids", "duplicate_ids", "foreign_ids")

    def test_original_cut_coverage_and_immediate_delivery_precedence_are_preserved(self):
        self.check_controls("uncovered_event_prefix", "missing_event_prefix",
            "immediate_delivery_precedes_event_check", "missing_ack_prefix")

    def test_duplicate_digest_coverage_uses_any_covering_occurrence(self):
        self.check_controls("duplicate_digest_later_occurrence_covers",
            "duplicate_digest_only_before_named_frames")

    def test_forged_hold_and_malformed_event_cannot_gain_index_admission(self):
        self.check_controls("forged_decision_digest", "malformed_event_precedes_delivery")

    def test_all_eight_envelope_shapes_preserve_complete_native_payloads(self):
        self.check_controls("genuine_native_attempt_binding")
        for name in ("bare", "reply", "binding", "session", "reply_binding",
                "reply_session", "binding_session", "reply_binding_session"):
            self.check_controls("envelope_shape_" + name,
                "validator_shape_%s_False" % name, "validator_shape_%s_True" % name)

    def test_envelope_shape_and_value_faults_preserve_exact_chained_refusals(self):
        for name in ("missing_required", "extra_field", "shape_before_value", "sha_before_reply",
                "reply_before_binding", "partial_binding", "binding_without_session",
                "binding_session_mismatch"):
            self.check_controls(name, "validator_%s_False" % name, "validator_%s_True" % name)

    def test_exact_dict_subclass_and_mapping_conversion_preserve_the_native_boundary(self):
        self.check_controls("projector_conversion_exact_dict", "projector_conversion_dict_subclass",
            "projector_conversion_mapping", "mapping_conversion_failure", "direct_validator_rejects_mapping")

    def test_top_level_copy_and_existing_nested_alias_behavior_are_preserved(self):
        self.check_controls("projected_top_level_mutation_isolated", "input_top_level_mutation_isolated",
            "existing_nested_binding_alias_preserved", "existing_nested_alias_mutation_visible")

    def test_chain_fault_precedence_and_malformed_unselected_rows_are_preserved(self):
        self.check_controls("chained_envelope_complete_output", "chain_pair_before_shape_value",
            "chain_seq_before_shape_value", "chain_prev_before_shape_value", "malformed_unselected_envelope")

    def test_reply_and_retraction_semantics_keep_exact_native_chained_refusals(self):
        self.check_controls("semantic_retraction_complete_output", "semantic_reply_order",
            "semantic_reply_parties", "semantic_retraction_order", "semantic_retraction_session",
            "semantic_retraction_party", "semantic_retraction_duplicate")

    def test_rebound_native_spec_is_consulted_by_the_validator(self):
        self.check_controls("dynamic_native_spec_extension", "dynamic_native_spec_old_shape_refused")

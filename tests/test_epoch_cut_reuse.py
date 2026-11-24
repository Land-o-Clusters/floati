"""Native archive and source-cut admission is private to one invocation."""
from pathlib import Path
import tempfile
import unittest

from tests.probe_epoch_cut_reuse import run_controls
from tests.temp_roots import REAL_TEMP_ROOT


class EpochCutReuseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.result = run_controls(Path(cls.temporary.name) / "native")

    def controls(self, *names):
        for name in names:
            with self.subTest(control=name):
                self.assertTrue(self.result["checks"][name], self.result)

    def test_each_outer_invocation_admits_archive_once_and_rechecks_membership(self):
        self.controls("first_invocation_admits_archive_source_once", "first_invocation_rechecks_membership",
            "later_invocation_admits_new_origins_afresh", "later_invocation_rechecks_membership")

    def test_r3_roll_preserves_states_through_native_v3_holds(self):
        self.controls("r3_roll_uses_native_v3_holds")

    def test_returned_archive_and_origin_testimony_is_detached(self):
        self.controls("returned_source_is_detached", "returned_origin_is_detached", "returned_states_are_detached",
            "public_origins_do_not_alias_private_testimony", "returned_direct_origin_is_detached",
            "returned_source_nested_items_are_detached", "returned_direct_origin_nested_items_are_detached",
            "returned_proof_original_nested_items_are_detached",
            "public_origins_nested_items_do_not_alias_private_testimony")

    def test_native_projection_and_seal_are_retained_for_progressive_cuts(self):
        self.controls("native_states_exact", "progressive_cut_has_native_seal")

    def test_changed_and_failed_cuts_cannot_borrow_prior_success(self):
        self.controls("changed_current_cut_refused", "previous_seal_rejects_changed_cut", "failed_origin_is_not_admitted")

    def test_archive_rewrites_refuse_before_outward_source_or_origin_return(self):
        self.controls("archive_rewrite_refused_before_source_return", "archive_rewrite_refused_before_origin_hit")

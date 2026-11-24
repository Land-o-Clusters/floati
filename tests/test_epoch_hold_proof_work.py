"""Native carried holds retain exact testimony with bounded envelope work."""
from pathlib import Path
import tempfile
import unittest

from tests.probe_epoch_hold_proof_work import run_controls
from tests.temp_roots import REAL_TEMP_ROOT


class EpochHoldProofWorkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.result = run_controls(Path(cls.temporary.name) / "native")

    def controls(self, *names):
        for name in names:
            with self.subTest(control=name):
                self.assertTrue(self.result["checks"][name], self.result)

    def test_original_envelope_encoding_work_does_not_multiply_by_hold_count(self):
        self.controls("verification_first_bounded", "verification_repeat_bounded")

    def test_r3_roll_preserves_states_through_native_v3_holds(self):
        self.controls("r3_roll_uses_native_v3_holds")

    def test_native_held_and_fresh_states_remain_exact(self):
        self.controls("exact_native_states", "repeat_exact_native_states", "proof_is_native_and_present")

    def test_changed_or_missing_envelopes_and_forged_holds_are_refused(self):
        self.controls("changed_current_envelope", "missing_current_envelope", "forged_imported_hold",
            "sealed_proof_rejects_changed_cut")

    def test_archive_tamper_is_refused_and_restoration_requires_fresh_resolver(self):
        self.controls("archive_tamper", "restored_archive_fresh_resolver")

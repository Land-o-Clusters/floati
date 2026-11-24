"""A warm waiter memo must readmit when a consumed binding changes.

The projection compared here is the owner's full select result: the same
selection bytes, or the same exact native refusal, as a second owner that
never saw the previous binding.
"""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import probe_waiter_projection_memo as probe
from tests.temp_roots import REAL_TEMP_ROOT


class WaiterSemanticKeyTests(unittest.TestCase):
    def _warm_owner(self, name, harness="cursor"):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        fixture = probe.NativeFixture(Path(temporary.name) / name, harness)
        fixture.seed_rolled()
        admitted = probe.observation(fixture.select)
        self.assertIn("bytes", admitted, admitted)
        self.assertIsNotNone(fixture.plane._load_memo)
        warmed = probe.observation(fixture.select)
        self.assertEqual(warmed, admitted)
        return fixture, admitted

    def test_root_archive_source_contract_change_readmits_projection(self):
        """Stable code bindings cannot stand in for the root's runtime contract."""
        from floati.cursor_presentations import CursorPresentationLedger
        from floati.epoch_carry import ArchiveProofResolver
        from floati.memo_key_closure import projection_binding_snapshot

        native_contract = ArchiveProofResolver._source_contract
        native_load = CursorPresentationLedger._load_native

        def root_contract(resolver):
            return native_contract(resolver) + (
                (resolver.root.tenant_home / "state/archive-contract.test").read_bytes(),)

        loads = []

        def observed_load(owner, *args, **kwargs):
            loads.append(owner.root.tenant_home)
            return native_load(owner, *args, **kwargs)

        for harness in ("codex", "cursor"):
            with self.subTest(harness=harness):
                fixture, admitted = self._warm_owner("archive-contract-" + harness, harness)
                path = fixture.root.tenant_home / "state/archive-contract.test"
                path.write_bytes(b"archive-contract-one")
                # Install observers BEFORE warming and keep their identities
                # fixed throughout the data change. No binding is rebound.
                with patch.object(ArchiveProofResolver, "_source_contract", root_contract),\
                        patch.object(CursorPresentationLedger, "_load_native", observed_load):
                    self.assertEqual(probe.observation(fixture.select), admitted)
                    fixture.select()
                    loads.clear()
                    self.assertEqual(probe.observation(fixture.select), admitted)
                    self.assertEqual(loads, [], "unchanged root contract must reuse admission")
                    bindings = projection_binding_snapshot()
                    before = ArchiveProofResolver(fixture.root)._source_contract()
                    path.write_bytes(b"archive-contract-two")
                    self.assertNotEqual(ArchiveProofResolver(fixture.root)._source_contract(), before)
                    self.assertEqual(projection_binding_snapshot(), bindings)
                    warm = probe.observation(fixture.select)
                    self.assertTrue(loads, "changed root archive contract reused the old admission")
                    cold = probe.observation(lambda: fixture.select(native=True))
                    self.assertEqual(warm, cold)
                    self.assertEqual(warm, admitted)

    def _assert_rebind_matches_cold_owner(self, fixture, admitted, owner, name, replacement):
        original = getattr(owner, name)
        try:
            setattr(owner, name, replacement)
            warm = probe.observation(fixture.select)
            cold = probe.observation(lambda: fixture.select(native=True))
            with self.subTest(phase="rebound"):
                self.assertNotEqual(cold, admitted)
                self.assertEqual(warm, cold)
        finally:
            setattr(owner, name, original)
        restored_warm = probe.observation(fixture.select)
        restored_cold = probe.observation(lambda: fixture.select(native=True))
        with self.subTest(phase="restored"):
            self.assertEqual(restored_warm, restored_cold)
            self.assertEqual(restored_cold, admitted)

    def test_rebind_event_domain_matches_cold_owner(self):
        """Every consumed binding must readmit exactly as a cold owner does."""
        from floati import epoch_carry, events, jsonl, wake_hold
        from floati.epoch_carry import _VerifiedHoldProofs
        from floati.errors import IntegrityFailure, ProtocolRefusal
        from floati.wake_hold import WakeHoldController

        def refusing_record_validator(*_args, **_kwargs):
            raise ProtocolRefusal("record_rejected", "rebound validator refuses the record")

        def refusing_event_validator(*_args, **_kwargs):
            raise IntegrityFailure("record_rejected", "rebound event validator refuses the record")

        def refusing_validates(self, _receipt):
            raise IntegrityFailure("record_rejected", "rebound hold proof refuses the receipt")

        def refusing_encode(_record):
            raise IntegrityFailure("record_rejected", "rebound frame encoder refuses the record")

        original_digest = wake_hold.wake_hold_decision_digest

        def rebound_digest(row, _original=original_digest):
            digest = _original(row)
            flipped = "0" if digest[:1] != "0" else "1"
            return flipped + digest[1:]

        current = dict(jsonl._decode_path_records.__kwdefaults__ or {})
        cases = (
            ("event-domain", WakeHoldController, "_EVENT_DOMAIN",
                WakeHoldController._EVENT_DOMAIN + "-rebound"),
            ("delivery-domain", WakeHoldController, "_DELIVERY_DOMAIN",
                WakeHoldController._DELIVERY_DOMAIN + "-rebound"),
            ("ack-domain", WakeHoldController, "_ACK_DOMAIN",
                WakeHoldController._ACK_DOMAIN + "-rebound"),
            ("validate-record", wake_hold, "validate_record", refusing_record_validator),
            ("event-kinds", wake_hold, "WAKE_EVENT_KINDS", frozenset()),
            ("decision-digest", wake_hold, "wake_hold_decision_digest", rebound_digest),
            ("validate-events", events, "validate_event_records", refusing_event_validator),
            ("hold-validates", _VerifiedHoldProofs, "validates", refusing_validates),
            ("encode-frame", epoch_carry, "encode_frame", refusing_encode),
            ("decode-kwdefault", jsonl._decode_path_records, "__kwdefaults__",
                dict(current, max_bytes=1)),
        )
        fixture, admitted = self._warm_owner("rebound-bindings")
        for case, owner, name, replacement in cases:
            with self.subTest(case=case):
                self._assert_rebind_matches_cold_owner(
                    fixture, admitted, owner, name, replacement)


if __name__ == "__main__":
    unittest.main()

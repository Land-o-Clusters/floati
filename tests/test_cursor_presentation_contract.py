"""Closed Cursor/Codex presentation testimony: real runtime/schema parity."""

from __future__ import annotations

import unittest
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.records import is_known_record_kind, validate_record
from tests.schema_validation import SchemaValidationError, validate_json_schema


KIND = "cursor_presentation_receipt"
SCHEMA = Path(__file__).resolve().parents[1] / "schemas/v1/cursor-presentation-receipt.schema.json"
UUID = "018f7e9b3c117abc8def0123456789ab"
OTHER = "018f7e9b3c127abc8def0123456789ab"


class CursorPresentationContractTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(is_known_record_kind(KIND), "new presentation kind is absent")
        self.assertTrue(SCHEMA.is_file(), "governed presentation schema is absent")

    def row(self, **changes):
        row = {
            "schema_version": 1, "id": "cursor-presentation-" + UUID,
            "tenant_id": "scratch-fleet", "timestamp": "2026-10-03T12:00:00.000Z",
            "kind": KIND, "batch_id": "cursor-presentation-" + UUID,
            "node_id": "scratch-seat", "acting_session_id": "native:conversation-A",
            "workspace": "/scratch/workspace", "consent_receipt_id": "codex-wait-consent-" + UUID,
            "claim_receipt_id": "codex-wait-session-" + UUID, "harness": "cursor",
            "epoch_anchor": "genesis-" + UUID, "phase": "prepared",
            "item_ids": ["msg-" + UUID, "msg-" + OTHER],
            "event_prefix_digest": "a" * 64, "delivery_prefix_digest": "b" * 64,
            "acknowledgment_prefix_digest": "c" * 64, "output_digest": "d" * 64,
            "delivery_receipt_id": None,
        }
        row.update(changes)
        return row

    def assert_valid(self, row):
        for integrity in (False, True):
            self.assertEqual(row, validate_record(row, "scratch-fleet", {KIND}, integrity=integrity))
        validate_json_schema(row, SCHEMA)

    def assert_invalid(self, row):
        with self.assertRaises(ProtocolRefusal):
            validate_record(row, "scratch-fleet", {KIND}, integrity=False)
        with self.assertRaises(IntegrityFailure):
            validate_record(row, "scratch-fleet", {KIND}, integrity=True)
        with self.assertRaises(SchemaValidationError):
            validate_json_schema(row, SCHEMA)

    def test_both_native_harnesses_accept_prepare_and_exact_delivery_terminal(self):
        """Rejecting native terminal delivery kinds would strand prepared mail."""
        for harness in ("cursor", "codex"):
            with self.subTest(harness=harness):
                self.assert_valid(self.row(harness=harness))
                for prefix in ("delivery-", "wake-hold-"):
                    self.assert_valid(self.row(harness=harness, phase="flushed",
                                              delivery_receipt_id=prefix + OTHER))

    def test_closed_shape_and_strict_version_refuse_unknown_or_missing_fields(self):
        self.assert_valid(self.row())
        for version in (0, 2, True, "1", None):
            with self.subTest(version=version):
                self.assert_invalid(self.row(schema_version=version))
        self.assert_invalid(self.row(extra="unreviewed"))
        incomplete = self.row()
        del incomplete["output_digest"]
        self.assert_invalid(incomplete)
        with self.assertRaises(ProtocolRefusal):
            validate_record(self.row(schema_version=1.0), "scratch-fleet", {KIND}, integrity=False)

    def test_phase_requires_absent_prepared_and_proven_flushed_delivery(self):
        self.assert_valid(self.row())
        for row in (self.row(phase="flushed"), self.row(phase="unknown"),
                    self.row(delivery_receipt_id="delivery-" + UUID),
                    self.row(phase="flushed", delivery_receipt_id="acks-" + UUID)):
            with self.subTest(row=row):
                self.assert_invalid(row)

    def test_nullable_claim_is_cursor_only_and_harness_is_closed(self):
        self.assert_valid(self.row(claim_receipt_id=None))
        self.assert_invalid(self.row(harness="codex", claim_receipt_id=None))
        self.assert_invalid(self.row(harness="claude"))
        self.assert_invalid(self.row(claim_receipt_id="codex-wait-consent-" + UUID))

    def test_native_session_remains_opaque_but_rejects_reserved_and_unsafe_selectors(self):
        for session in ("A", "opaque.session_1:turn-2", "X" * 256):
            self.assert_valid(self.row(acting_session_id=session))
        for session in ("", "all", "GLOBAL", "*", "bad/session", "bad\nidentity", "X" * 257, 17):
            with self.subTest(session=session):
                self.assert_invalid(self.row(acting_session_id=session))

    def test_workspace_is_canonical_absolute_shape_without_filesystem_resolution(self):
        for workspace in ("/", "/scratch/workspace", "/scratch/space name"):
            self.assert_valid(self.row(workspace=workspace))
        for workspace in ("relative", "/scratch/../workspace", "/scratch/./workspace",
                          "/scratch//workspace", "//scratch/workspace", "/scratch/workspace/", "/bad\nworkspace", ""):
            with self.subTest(workspace=workspace):
                self.assert_invalid(self.row(workspace=workspace))

    def test_epoch_anchor_is_bounded_safe_and_preserves_native_roll_identity(self):
        for epoch in ("genesis-" + UUID, "bus-epoch-roll-receipt-" + UUID, "x" * 128):
            self.assert_valid(self.row(epoch_anchor=epoch))
        for epoch in ("", "x" * 129, "bad/epoch", "bad\nepoch", " epoch", 1):
            with self.subTest(epoch=epoch):
                self.assert_invalid(self.row(epoch_anchor=epoch))

    def test_items_preserve_order_and_enforce_unique_exact_nonempty_message_ids(self):
        reversed_ids = ["msg-" + OTHER, "msg-" + UUID]
        self.assert_valid(self.row(item_ids=reversed_ids))
        maximum = ["msg-" + f"{index:012x}7abc8def0123456789ab" for index in range(1000)]
        self.assert_valid(self.row(item_ids=maximum))
        for items in ([], ["msg-" + UUID] * 2, ["foreign-" + UUID], [17], tuple(reversed_ids),
                      maximum + ["msg-" + UUID]):
            with self.subTest(length=len(items)):
                self.assert_invalid(self.row(item_ids=items))

    def test_prefix_output_and_native_receipt_references_cannot_be_forged_by_shape(self):
        self.assert_valid(self.row())
        for field in ("event_prefix_digest", "delivery_prefix_digest", "acknowledgment_prefix_digest", "output_digest"):
            for value in ("A" * 64, "a" * 63, "z" * 64, None):
                with self.subTest(field=field, value=value):
                    self.assert_invalid(self.row(**{field: value}))
        for field in ("id", "batch_id", "consent_receipt_id", "claim_receipt_id"):
            self.assert_invalid(self.row(**{field: "foreign-" + UUID}))
        self.assert_invalid(self.row(node_id="bad/node"))


if __name__ == "__main__":
    unittest.main()

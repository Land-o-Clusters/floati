"""Automatic rotation testimony keeps manual v1 receipts readable and closed."""
from copy import deepcopy
import unittest

from floati.errors import ProtocolRefusal
from floati.records import validate_record


def receipt():
    return {
        "schema_version": 1, "kind": "bus_epoch_roll_receipt",
        "id": "bus-epoch-roll-receipt-00000000000070008000000000000000",
        "tenant_id": "fixture", "timestamp": "2026-10-15T00:00:01.000Z",
        "archive_path": "\x2fprivate/tmp/fixture/archive-owned",
        "actor": "fixture-node", "idempotency_key": "fixture-automatic",
        "invalidated_followers": ["tail_followers", "waiters", "monitors"],
        "epoch_id": "epoch-fixture", "span": {"byte_start": 0, "byte_end": 2048},
        "archive_sha256": "0" * 64, "archive_file_count": 1,
        "plane_counts": {"events": 1, "deliveries": 0, "acks": 0},
    }


def automatic():
    return {
        "trigger": "send", "policy": {
            "max_bytes": 1024, "max_age_days": 7, "source": "operator_declared"},
        "observed_bytes": 2048, "observed_at": "2026-10-15T00:00:00.000Z",
        "epoch_started_at": "2026-10-14T00:00:00.000Z", "due_reasons": ["bytes"],
    }


class AutomaticEpochMetadataTests(unittest.TestCase):
    def validate(self, row):
        return validate_record(row, "fixture", frozenset({"bus_epoch_roll_receipt"}), integrity=False)

    def test_manual_receipt_and_closed_automatic_extension_are_readable(self):
        self.validate(receipt())
        self.validate(dict(receipt(), automatic=automatic()))

    def test_teardown_commitment_is_closed_bounded_and_optional(self):
        meta = dict(automatic(), teardown_carry={"count": 2, "digest": "a" * 64})
        self.validate(dict(receipt(), automatic=meta))
        for commitment in (
            None, [], {"count": True, "digest": "a" * 64},
            {"count": -1, "digest": "a" * 64},
            {"count": 100001, "digest": "a" * 64},
            {"count": 2, "digest": "short"},
            {"count": 2, "digest": "a" * 64, "accepted_without_check": True},
        ):
            with self.subTest(commitment=commitment), self.assertRaises(ProtocolRefusal):
                self.validate(dict(receipt(), automatic=dict(automatic(), teardown_carry=commitment)))

    def test_policy_reason_must_be_supported_by_the_observed_cut(self):
        for changes in (
            {"due_reasons": ["age"]}, {"due_reasons": []},
            {"observed_bytes": 1023}, {"observed_bytes": True},
            {"observed_at": "2026-10-15T00:00:02.000Z"},
            {"epoch_started_at": "2026-10-16T00:00:00.000Z"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ProtocolRefusal):
                self.validate(dict(receipt(), automatic=dict(automatic(), **changes)))

    def test_age_is_supported_at_exact_seven_day_boundary(self):
        meta = dict(automatic(), observed_bytes=0,
                    epoch_started_at="2026-10-08T00:00:00.000Z", due_reasons=["age"])
        self.validate(dict(receipt(), automatic=meta))
        meta["epoch_started_at"] = "2026-10-08T00:00:00.001Z"
        with self.assertRaises(ProtocolRefusal):
            self.validate(dict(receipt(), automatic=meta))

    def test_open_or_malformed_decision_never_raises_an_untyped_exception(self):
        cases = [None, [], dict(automatic(), credential="invented"),
                 dict(automatic(), trigger=[]), dict(automatic(), trigger="manual"),
                 dict(automatic(), policy=[]),
                 dict(automatic(), policy=dict(automatic()["policy"], source=[])),
                 dict(automatic(), policy=dict(automatic()["policy"], max_age_days=True))]
        for meta in cases:
            with self.subTest(meta=meta), self.assertRaises(ProtocolRefusal):
                self.validate(dict(receipt(), automatic=deepcopy(meta)))

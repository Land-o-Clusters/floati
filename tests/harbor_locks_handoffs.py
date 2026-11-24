from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.locks.handoffs import ReviewHandoffController
from floati.locks.ledger import LockLedger
from floati.locks.projection import LockProjection
from floati.root import FloatiRoot


NOW = "2026-08-26T21:00:00.000Z"
LATER = "2026-08-26T21:01:00.000Z"
LATEST = "2026-08-26T21:02:00.000Z"
DIGEST = "d" * 64
WITNESS = {"kind": "path_present", "path": "floati/locks/ledger.py"}


class LocksReviewHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open(Path(self.temporary.name).resolve(), "alpha")
        self.ledger = LockLedger(self.root)
        self.controller = ReviewHandoffController(
            self.ledger,
            identity_resolver=lambda recipient: recipient == "reviewer-one",
        )

    def queue(self):
        return self.controller.queue(
            handoff_id="review-one",
            recipient="reviewer-one",
            car_id="car-one",
            ref="refs/heads/codex/car-one",
            base_ref="refs/remotes/origin/main",
            witness=WITNESS,
            rearm_event="reviewer-one-ready",
            now=NOW,
        )

    def test_stopped_handoff_rearms_only_on_named_event_and_delivers_by_receipt(self) -> None:
        """Catches silence or a mismatched wake being treated as successful delivery."""

        self.queue()
        self.controller.stop(
            "review-one", attempts=3, stopped_at=LATER,
            rearm_event="reviewer-one-ready",
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.rearm("review-one", observed_event="someone-else-ready", now=LATEST)
        self.assertEqual("handoff_rearm_event_mismatch", caught.exception.code)
        self.controller.rearm("review-one", observed_event="reviewer-one-ready", now=LATEST)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.controller.record_delivered(
                "review-one", receipt_id="", receipt_digest=DIGEST, now=LATEST,
            )
        self.assertEqual("delivery_receipt_required", caught.exception.code)
        self.controller.record_delivered(
            "review-one", receipt_id="delivery-review-one", receipt_digest=DIGEST, now=LATEST,
        )

        handoff = self.ledger.snapshot().handoffs["review-one"]
        self.assertEqual("delivered", handoff.status)
        self.assertEqual(3, handoff.attempts)
        self.assertEqual(LATER, handoff.stopped_at)
        self.assertEqual("delivery-review-one", handoff.receipt_id)
        self.assertEqual(DIGEST, handoff.receipt_digest)

    def test_repeated_snapshots_do_not_infer_delivery_from_silence(self) -> None:
        """Catches elapsed reads silently completing an undelivered handoff."""

        self.queue()
        self.controller.stop(
            "review-one", attempts=2, stopped_at=LATER,
            rearm_event="reviewer-one-ready",
        )
        first = self.ledger.snapshot().handoffs["review-one"]
        second = self.ledger.snapshot().handoffs["review-one"]
        self.assertEqual("stopped", first.status)
        self.assertEqual(first, second)
        self.assertIsNone(second.receipt_id)

    def test_recipient_identity_refuses_fail_closed_without_appending(self) -> None:
        """Catches missing, generic, false, or failed identity resolution."""

        for recipient in (None, "", "unknown", "missing", "default", "other-reviewer"):
            with self.subTest(recipient=recipient):
                with self.assertRaises(ProtocolRefusal) as caught:
                    self.controller.queue(
                        handoff_id="review-one",
                        recipient=recipient,
                        car_id="car-one",
                        ref="refs/heads/codex/car-one",
                        base_ref="refs/remotes/origin/main",
                        witness=WITNESS,
                        rearm_event="reviewer-one-ready",
                        now=NOW,
                    )
                self.assertEqual("handoff_recipient_unresolved", caught.exception.code)
                self.assertEqual([], self.ledger.records())

        raising = ReviewHandoffController(
            self.ledger,
            identity_resolver=lambda _recipient: (_ for _ in ()).throw(RuntimeError("resolver down")),
        )
        with self.assertRaises(ProtocolRefusal) as caught:
            raising.queue(
                handoff_id="review-one",
                recipient="reviewer-one",
                car_id="car-one",
                ref="refs/heads/codex/car-one",
                base_ref="refs/remotes/origin/main",
                witness=WITNESS,
                rearm_event="reviewer-one-ready",
                now=NOW,
            )
        self.assertEqual("handoff_recipient_unresolved", caught.exception.code)
        self.assertEqual([], self.ledger.records())

    def test_transition_records_preserve_the_queued_binding(self) -> None:
        """Catches a transition rebinding the recipient, refs, car, or witness."""

        queued = self.queue()
        stopped = self.controller.stop(
            "review-one", attempts=1, stopped_at=LATER,
            rearm_event="reviewer-one-ready",
        )
        rearmed = self.controller.rearm(
            "review-one", observed_event="reviewer-one-ready", now=LATEST,
        )
        delivered = self.controller.record_delivered(
            "review-one", receipt_id="delivery-review-one", receipt_digest=DIGEST, now=LATEST,
        )
        binding_fields = (
            "handoff_id", "recipient", "car_id", "ref", "base_ref", "witness", "rearm_event",
        )
        for row in (stopped, rearmed, delivered):
            self.assertEqual(
                {field: queued[field] for field in binding_fields},
                {field: row[field] for field in binding_fields},
            )

    def test_replay_rejects_a_changed_transition_binding(self) -> None:
        """Catches stored transition testimony being rebound after append."""

        self.queue()
        self.controller.stop(
            "review-one", attempts=1, stopped_at=LATER,
            rearm_event="reviewer-one-ready",
        )
        original = self.ledger.records()
        mutations = {
            "recipient": "reviewer-two",
            "car_id": "car-two",
            "ref": "refs/heads/codex/car-two",
            "base_ref": "refs/remotes/origin/stable",
            "witness": {"kind": "path_present", "path": "floati/locks/contracts.py"},
            "rearm_event": "reviewer-two-ready",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                records = deepcopy(original)
                records[1][field] = value
                with self.assertRaises(IntegrityFailure) as caught:
                    LockProjection.from_records(records, "alpha", integrity=True)
                self.assertEqual("handoff_binding_invalid", caught.exception.code)


if __name__ == "__main__":
    unittest.main()

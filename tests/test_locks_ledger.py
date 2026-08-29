from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from floati.errors import ProtocolRefusal
from floati.locks.ledger import LockLedger
from floati.root import FloatiRoot


NOW = "2026-08-26T20:00:00.000Z"
FUTURE = "2026-08-26T21:00:00.000Z"
EXPIRED = "2026-08-26T21:00:00.000Z"
NEXT_EXPIRY = "2026-08-26T22:00:00.000Z"
WITNESS = {
    "kind": "blob_sha256",
    "path": "floati/locks/contracts.py",
    "sha256": "b" * 64,
}


class InjectedCrash(RuntimeError):
    pass


class LocksLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open(Path(self.temporary.name), "alpha")
        self.ledger = LockLedger(self.root)

    def acquire(self) -> None:
        self.ledger.acquire(
            lock_id="merge-main",
            holder="lane-floati",
            expires_at=FUTURE,
            escalation_holder="fable",
            now=NOW,
        )

    def acquire_and_escalate(self) -> None:
        self.acquire()
        self.ledger.escalate(
            lock_id="merge-main",
            requested_by="fable",
            now=EXPIRED,
            expires_at=NEXT_EXPIRY,
            escalation_holder="backup",
            rearm_event="delivery-path-rearmed",
        )

    def test_lock_is_invalid_without_holder_expiry_and_named_escalation(self) -> None:
        """Catches any lock acquisition path persisting an incomplete promise."""

        cases = (
            {"holder": None, "expires_at": FUTURE, "escalation_holder": "fable"},
            {"holder": "lane-floati", "expires_at": None, "escalation_holder": "fable"},
            {"holder": "lane-floati", "expires_at": FUTURE, "escalation_holder": None},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(ProtocolRefusal):
                self.ledger.acquire(lock_id="merge-main", now=NOW, **values)
        self.assertEqual([], self.ledger.records())

    def test_car_and_measurement_require_full_refs(self) -> None:
        """Catches a detached object or ref-less result entering queue truth."""

        for ref in (
            "HEAD",
            "a" * 40,
            "main",
            "refs/tags/v1",
            "refs/heads/main@{0}",
            "refs/heads/.hidden",
            "refs/heads/name.lock",
            "refs/heads/trailing.",
        ):
            with self.subTest(ref=ref), self.assertRaises(ProtocolRefusal) as caught:
                self.ledger.submit_car(
                    car_id="car-one",
                    ref=ref,
                    ref_oid="a" * 40,
                    witness=WITNESS,
                    now=NOW,
                )
            self.assertEqual("car_ref_required", caught.exception.code)

        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.record_measurement(
                car_id="car-one",
                measured_ref=None,
                measured_tree="b" * 40,
                test_count=4_882,
                failure_count=0,
                evidence_digest="c" * 64,
                now=NOW,
            )
        self.assertEqual("measurement_ref_required", caught.exception.code)

    def test_expired_escalation_changes_holder_and_exposes_pending_announcement(self) -> None:
        """Catches a holder takeover without the displaced-holder obligation."""

        self.acquire()
        row = self.ledger.escalate(
            lock_id="merge-main",
            requested_by="fable",
            now=EXPIRED,
            expires_at=NEXT_EXPIRY,
            escalation_holder="backup",
            rearm_event="delivery-path-rearmed",
        )

        lock = self.ledger.snapshot().locks["merge-main"]
        self.assertEqual("fable", lock.holder)
        self.assertEqual("pending", lock.announcement.status)
        self.assertEqual("lane-floati", lock.announcement.recipient)
        self.assertEqual("delivery-path-rearmed", lock.announcement.rearm_event)
        self.assertEqual(
            "[[locks.escalation.action_taken_not_role]]",
            lock.announcement.copy_key,
        )
        self.assertEqual("lock_escalated", row["kind"])

    def test_crash_after_holder_selection_persists_neither_holder_nor_obligation(self) -> None:
        """Catches holder and announcement being written as separate transactions."""

        self.acquire()

        def crash(stage: str) -> None:
            if stage == "after_holder_decision_before_atomic_append":
                raise InjectedCrash()

        with self.assertRaises(InjectedCrash):
            self.ledger.escalate(
                lock_id="merge-main",
                requested_by="fable",
                now=EXPIRED,
                expires_at=NEXT_EXPIRY,
                escalation_holder="backup",
                rearm_event="delivery-path-rearmed",
                crash_hook=crash,
            )

        fresh = LockLedger(self.root).snapshot().locks["merge-main"]
        self.assertEqual("lane-floati", fresh.holder)
        self.assertIsNone(fresh.announcement)

    def test_silence_never_delivers_or_releases_an_announcement(self) -> None:
        """Catches repeated observation or a missing receipt inventing delivery."""

        self.acquire_and_escalate()
        for _ in range(3):
            view = LockLedger(self.root).snapshot().locks["merge-main"].announcement
            self.assertEqual("pending", view.status)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.record_announcement_delivery(
                "merge-main",
                receipt_id=None,
                receipt_digest=None,
                now=NEXT_EXPIRY,
            )
        self.assertEqual("delivery_receipt_required", caught.exception.code)

    def test_stopped_announcement_remains_visible_and_requires_its_rearm_event(self) -> None:
        """Catches retry exhaustion deleting debt or any event re-arming it."""

        self.acquire_and_escalate()
        self.ledger.stop_announcement(
            "merge-main",
            attempts=3,
            stopped_at=NEXT_EXPIRY,
            rearm_event="delivery-path-rearmed",
        )
        stopped = self.ledger.snapshot().locks["merge-main"].announcement
        self.assertEqual("stopped", stopped.status)
        self.assertEqual(3, stopped.attempts)
        self.assertEqual(NEXT_EXPIRY, stopped.stopped_at)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.rearm_announcement(
                "merge-main",
                observed_event="new-arrival",
                now=NEXT_EXPIRY,
            )
        self.assertEqual("announcement_rearm_event_mismatch", caught.exception.code)
        self.ledger.rearm_announcement(
            "merge-main",
            observed_event="delivery-path-rearmed",
            now=NEXT_EXPIRY,
        )
        rearmed = self.ledger.snapshot().locks["merge-main"].announcement
        self.assertEqual("pending", rearmed.status)
        self.assertEqual(3, rearmed.attempts)

    def test_announcement_delivery_requires_and_preserves_explicit_receipt(self) -> None:
        """Catches send acceptance being recorded as delivery evidence."""

        self.acquire_and_escalate()
        self.ledger.record_announcement_delivery(
            "merge-main",
            receipt_id="delivery-018f7e9b3c117abc8def0123456789ab",
            receipt_digest="d" * 64,
            now=NEXT_EXPIRY,
        )
        delivered = self.ledger.snapshot().locks["merge-main"].announcement
        self.assertEqual("delivered", delivered.status)
        self.assertEqual(
            "delivery-018f7e9b3c117abc8def0123456789ab",
            delivered.receipt_id,
        )
        self.assertEqual("d" * 64, delivered.receipt_digest)


if __name__ == "__main__":
    unittest.main()

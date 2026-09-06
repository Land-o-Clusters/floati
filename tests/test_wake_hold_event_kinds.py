"""WAKE-STACK-2 B: the wake projection must read the ledger the writer writes.

Measured incident (2026-08-29, on the fleet's live bus root): two
``delivery_claim`` records — a LEGAL current-vocabulary write that
``floati send --claim`` appends to ``events.jsonl`` — sat at physical lines
2433 and 2435, and every wake cycle for every node on that root refused
``consumption_state_unavailable`` with the detail "event or retraction
testimony is malformed".  291 such refusals are in
``state/wake-daemon/logs/890944d4....stderr.log``.  The refusal named no
record, no ledger and no remedy, so nobody could find the two lines; the
root was eventually rolled to a fresh epoch to clear it.

Two separable defects, one witness each:

1.  ``WakeHoldController._read`` reads the event ledger with
    ``events.EVENT_KINDS`` (which contains ``delivery_claim``) and then hands
    those exact records to ``project_wake_items``, which re-validates them
    against a SECOND, narrower, hand-written kind set that omits it.  The
    writer and the reader of one ledger disagree, so a legal append deafens
    every wake daemon on the root, permanently.
2.  The refusal is anonymous.  A malformed record must be nameable: the
    ledger, the physical record position, its id, and the one verb that
    clears it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from floati.errors import IntegrityFailure
from floati.root import FloatiRoot


NOW = "2026-08-29T16:17:00.000Z"
ENVELOPE_ID = "msg-018f7e9b3c117abc8def0123456789ab"
CLAIM_ID = "delivery-claim-018f7e9b3c127abc8def0123456789ab"


def envelope(message_id: str = ENVELOPE_ID, *, recipient: str = "bob") -> dict:
    return {
        "schema_version": 0,
        "id": message_id,
        "tenant_id": "alpha",
        "timestamp": NOW,
        "kind": "message_envelope",
        "sender": "sender",
        "recipient": recipient,
        "repo": "floati",
        "sha": "a" * 40,
        "doc": "docs/evidence/wake.md",
        "note": "wake evidence",
        "idempotency_key": "message-key",
        "attempt_binding": "absent_legacy",
    }


def delivery_claim(note_ref: str = ENVELOPE_ID) -> dict:
    """The exact record shape ``floati send --claim`` appends beside an envelope."""

    return {
        "schema_version": 0,
        "id": CLAIM_ID,
        "tenant_id": "alpha",
        "timestamp": NOW,
        "kind": "delivery_claim",
        "sha": "a" * 40,
        "repo_path": "/srv/floati-harbor-reverify",
        "bank": ["tests.test_verify_receipts"],
        "declared": {"ran": 1, "result": "OK"},
        "artifacts": [],
        "note_ref": note_ref,
        "deadline_seconds": 60,
    }


class WakeProjectionReadsTheWrittenLedgerTests(unittest.TestCase):
    """RED before the fix: a legal delivery_claim deafens the wake projection."""

    def _project(self, events: list) -> tuple:
        from floati.wake_hold import project_wake_items

        return project_wake_items(
            events=events,
            deliveries=[],
            acknowledgments=[],
            recipient="bob",
            worker_session_id=None,
            tenant_id="alpha",
            event_prefix_digests=tuple(str(index) * 64 for index in range(len(events) + 1)),
            delivery_prefix_digests=("a" * 64,),
            acknowledgment_prefix_digests=("b" * 64,),
        )

    def test_the_writer_and_the_reader_of_the_event_ledger_permit_one_kind_set(self) -> None:
        """Catches a second hand-written kind set drifting from events.EVENT_KINDS."""
        from floati.events import EVENT_KINDS
        from floati.wake_hold import WAKE_EVENT_KINDS

        self.assertEqual(set(EVENT_KINDS), set(WAKE_EVENT_KINDS))

    def test_a_legal_delivery_claim_does_not_deafen_the_wake_projection(self) -> None:
        """Catches the 2026-08-29 deafness: send --claim silencing every wake."""
        states = self._project([envelope(), delivery_claim()])

        self.assertEqual([ENVELOPE_ID], [item.message["id"] for item in states])
        self.assertEqual(["fresh"], [item.state for item in states])

    def test_the_projection_still_refuses_a_kind_the_event_ledger_never_takes(self) -> None:
        """Catches the fix being a loosened fence rather than one honest kind set."""
        foreign = dict(envelope(), kind="ack_receipt")

        with self.assertRaises(IntegrityFailure) as caught:
            self._project([foreign])

        self.assertEqual("consumption_state_unavailable", caught.exception.code)


class MalformedEventTestimonyNamesItsRecordTests(unittest.TestCase):
    """RED before the fix: the refusal says 'malformed' and nothing else."""

    def _refusal(self, events: list) -> IntegrityFailure:
        from floati.wake_hold import project_wake_items

        with self.assertRaises(IntegrityFailure) as caught:
            project_wake_items(
                events=events,
                deliveries=[],
                acknowledgments=[],
                recipient="bob",
                worker_session_id=None,
                tenant_id="alpha",
                event_prefix_digests=tuple(str(index) * 64 for index in range(len(events) + 1)),
                delivery_prefix_digests=("a" * 64,),
                acknowledgment_prefix_digests=("b" * 64,),
            )
        return caught.exception

    def test_a_malformed_event_names_its_ledger_position_id_and_remedy(self) -> None:
        """Catches an anonymous refusal that cannot be chased to a line."""
        broken = dict(envelope("msg-018f7e9b3c137abc8def0123456789ab"), sha="not-a-sha")

        failure = self._refusal([envelope(), broken])

        self.assertEqual("consumption_state_unavailable", failure.code)
        self.assertIn("events.jsonl", failure.detail)
        self.assertIn("record 2", failure.detail)
        self.assertIn("msg-018f7e9b3c137abc8def0123456789ab", failure.detail)
        self.assertIn("floati repair quarantine", failure.detail)

    def test_a_record_with_a_hostile_id_is_cited_by_position_only(self) -> None:
        """Catches the detail becoming a terminal-injection surface."""
        hostile = dict(envelope(), id="msg-‮evil", sha="not-a-sha")

        failure = self._refusal([hostile])

        self.assertIn("record 1", failure.detail)
        self.assertNotIn("‮", failure.detail)
        self.assertNotIn("", failure.detail)

    def test_the_projection_recovers_once_the_malformed_record_is_repaired(self) -> None:
        """Catches a fix that names the record but cannot come back green."""
        from floati.wake_hold import project_wake_items

        broken = dict(envelope("msg-018f7e9b3c137abc8def0123456789ab"), sha="not-a-sha")
        self._refusal([envelope(), broken])

        repaired = project_wake_items(
            events=[envelope()],
            deliveries=[],
            acknowledgments=[],
            recipient="bob",
            worker_session_id=None,
            tenant_id="alpha",
            event_prefix_digests=("0" * 64, "1" * 64),
            delivery_prefix_digests=("a" * 64,),
            acknowledgment_prefix_digests=("b" * 64,),
        )
        self.assertEqual([ENVELOPE_ID], [item.message["id"] for item in repaired])


class WakeHoldControllerReadsAClaimedLedgerTests(unittest.TestCase):
    """The end-to-end path grok's daemon died on, on a real root."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name).resolve() / "fleet", create=True
        )
        from floati.registry import Registry

        registry = Registry(self.root)
        registry.register("sender", "Codex")
        registry.register("bob", "Codex")

    def test_a_send_with_a_claim_leaves_the_recipient_wakeable(self) -> None:
        """Catches `floati send --claim` permanently deafening one bus root."""
        from floati.events import EventLog
        from floati.wake_hold import WakeHoldController

        EventLog(self.root).send(
            "sender",
            "bob",
            "floati",
            "b" * 40,
            "docs/evidence/wake.md",
            "claimed evidence",
            idempotency_key="claimed-send",
            claim={
                "kind": "delivery_claim",
                "schema_version": 0,
                "sha": "b" * 40,
                "repo_path": str(Path(self.temporary.name).resolve()),
                "bank": ["tests.test_verify_receipts"],
                "declared": {"ran": 1, "result": "OK"},
                "artifacts": [],
                "deadline_seconds": 60,
            },
        )

        decision = WakeHoldController(self.root).evaluate("bob", idempotency_key="claimed-wake")

        self.assertTrue(decision["wake_required"])
        self.assertEqual(1, decision["fresh_total"])
        self.assertEqual(1, len(decision["receipt"]["item_ids"]))


if __name__ == "__main__":  # pragma: no cover - module is run by the discoverer
    unittest.main()

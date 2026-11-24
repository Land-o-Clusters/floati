"""Warm native presentation readers must keep proving current scratch evidence."""
from __future__ import annotations

import hashlib
import json
import os
import unittest

from floati import fixture_ids as public_ids
from floati.bus_epoch import epoch_guard
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EVENT_KINDS
from floati.framing import encode_frame
from floati.jsonl import read_records_with_prefix_digests
from floati.wake_hold import WakeHoldController, wake_coordination_guard
from tests import test_cursor_presentation_history as history
from tests import test_f2_cursor_claimant as claimant


class WaiterHistoricalCutCacheTests(unittest.TestCase):
    def setUp(self):
        self.h = history.CursorPresentationHistoryTests("runTest")
        self.addCleanup(self.h.doCleanups)
        self.h.setUp()
        self.root, self.node, self.plane = self.h.root, self.h.node, self.h.plane

    def authority(self):
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            binding = self.h.c.f.participant.binding
            consent = CodexWaitConsentLedger(self.root).require_armed(binding)
            classification = CodexWaitSessionLedger(self.root)._participate_already_guarded(
                binding, consent, claimant.A, classified=True)
            return binding, consent, classification

    def assert_stale_authority_refused(self, authority):
        before_history = self.h.history.read_bytes()
        before_deliveries = self.h.c.deliveries()
        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            with self.assertRaises(ProtocolRefusal) as caught:
                self.plane.select(*authority, harness="cursor")
        self.assertEqual("presentation_authority_changed", caught.exception.code)
        self.assertEqual(before_history, self.h.history.read_bytes())
        self.assertEqual(before_deliveries, self.h.c.deliveries())

    def test_same_size_rewritten_cut_cannot_reuse_old_ordinary_message_eligibility(self):
        """Dropping cut digests from reuse would admit a now-foreign historical message."""
        old, rows = self.h.publish("cut-cache-rewritten-recipient")
        self.assertEqual(rows, self.plane.read(self.node))
        events = self.root.resolve_relative("events.jsonl")
        before = events.stat()
        original_bytes = events.read_bytes()
        records, prefixes = read_records_with_prefix_digests(
            self.root, "events.jsonl", allowed_kinds=set(EVENT_KINDS),
            domain=WakeHoldController._EVENT_DOMAIN, skip_unknown_kinds=True)
        self.assertEqual(prefixes[-1], rows[0]["event_prefix_digest"])
        foreign = public_ids.builder("shadow")
        rewritten = [dict(row, recipient=foreign) if row.get("recipient") == self.node
            else row for row in records]
        rewritten_bytes = b"".join(encode_frame(row) for row in rewritten)
        self.assertNotEqual(original_bytes, rewritten_bytes)
        self.assertEqual(len(original_bytes), len(rewritten_bytes))
        events.write_bytes(rewritten_bytes)
        os.utime(events, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = events.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
            (after.st_ino, after.st_size, after.st_mtime_ns))
        changed_records, changed_prefixes = read_records_with_prefix_digests(
            self.root, "events.jsonl", allowed_kinds=set(EVENT_KINDS),
            domain=WakeHoldController._EVENT_DOMAIN, skip_unknown_kinds=True)
        self.assertEqual(foreign, next(row["recipient"] for row in changed_records
            if row["id"] == old["id"]))
        self.assertNotEqual(prefixes[-1], changed_prefixes[-1])

        # Keep the forged batch and its write-ahead witness internally exact.
        # The refusal must come from current native message eligibility, not
        # a stale digest lookup or a damaged presentation witness.
        changed = [dict(row, event_prefix_digest=changed_prefixes[-1]) for row in rows]
        prepared = encode_frame(changed[0])
        self.h.history.write_bytes(prepared + encode_frame(changed[1]))
        witness = json.loads(self.h.witness.read_bytes())
        witness.update(before_bytes=len(prepared),
            before_digest=hashlib.sha256(prepared).hexdigest(), reserved_record=changed[1])
        self.h.witness.write_text(json.dumps(witness) + "\n")
        before_history = self.h.history.read_bytes()
        before_deliveries = self.h.c.deliveries()
        with self.assertRaises(IntegrityFailure) as caught:
            self.plane.read(self.node)
        self.assertEqual("presentation_history_unavailable", caught.exception.code)
        self.assertIn("foreign", caught.exception.detail)
        self.assertEqual(before_history, self.h.history.read_bytes())
        self.assertEqual(before_deliveries, self.h.c.deliveries())

    def test_valid_appended_mail_and_ack_leave_old_cut_readable_and_current_selection_fresh(self):
        """Reusing history must neither apply later ACKs backwards nor freeze current mail."""
        old, rows = self.h.publish("cut-cache-old-history")
        self.assertEqual(rows, self.plane.read(self.node))
        fresh = self.h.c.f.send("cut-cache-unrelated-fresh-tail")
        SparseCursor(self.root).ack(self.node, [old["id"]], acting_session_id=claimant.A)
        self.assertEqual(rows, self.plane.read(self.node))
        with self.h.selected() as (_, selection):
            self.assertEqual([fresh["id"]], [row["id"] for row in selection["fresh_messages"]])
            self.assertEqual([], selection["held_items"])
        self.assertEqual(rows, self.plane.read(self.node))

    def test_warm_history_does_not_authorize_the_predecessor_after_native_takeover(self):
        """A cached cut cannot bypass the current native claimant check."""
        _, rows = self.h.publish("cut-cache-before-takeover")
        self.assertEqual(rows, self.plane.read(self.node))
        authority = self.authority()
        self.h.c.f.arm_session(claimant.B)
        self.assert_stale_authority_refused(authority)

    def test_warm_history_does_not_retain_consent_after_native_rearm(self):
        """A cached cut cannot retain the old consent receipt or its longer window."""
        _, rows = self.h.publish("cut-cache-before-consent-change")
        self.assertEqual(rows, self.plane.read(self.node))
        authority = self.authority()
        CodexWaitConsentLedger(self.root).arm(authority[0], hook_timeout_seconds=10,
            wait_deadline_seconds=1, idempotency_key="cut-cache-shorter-consent")
        self.assert_stale_authority_refused(authority)


if __name__ == "__main__":
    unittest.main()

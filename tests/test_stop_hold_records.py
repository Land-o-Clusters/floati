"""Native hold correlation, ownership and observational-read controls."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tests  # Keep product path defaults away from the operator's HOME.

from floati.bus_epoch import epoch_guard
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.ids import uuid7_hex
from floati.jsonl import append_record
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.seat_waiter_lifecycle import retire_waiter
from floati.stop_hold import StopHoldLedger
from floati.waiter_consent import arm_waiter_consent
from floati.wake_hold import wake_coordination_guard
from tests.temp_roots import REAL_TEMP_ROOT


START = "2026-10-02T12:00:00.000Z"
END = "2026-10-02T12:00:01.000Z"


class StopHoldRecordTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        self.node = "hold-recipient"
        Registry(self.root).register(self.node, "Codex")
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        arm_waiter_consent(self.root.path, self.workspace, self.node, "Codex", 30, 7)
        self.binding = resolve_participant(self.root.path, self.workspace).binding
        self.consent = CodexWaitConsentLedger(self.root).require_armed(self.binding)
        self.session = "native-hold-session"
        CodexWaitSessionLedger(self.root).arm(self.binding, self.consent, self.session,
            idempotency_key="real-native-claim")
        self.ledger = StopHoldLedger(self.root)
        self.relative = self.ledger.relative(self.node)
        self.path = self.root.path / self.relative

    def start(self, **changes):
        arguments = {"started_at": START, "deadline_seconds": 7}
        arguments.update(changes)
        return self.ledger.start(self.binding, self.consent, self.session, **arguments)

    def append(self, row):
        append_record(self.root, self.relative, row, allowed_kinds={"stop_hold_receipt"})

    def test_close_replays_exact_native_exit_without_another_append(self):
        start = self.start()
        exit_row = self.ledger.close(start, timestamp=END)
        before = self.path.read_bytes()
        replay = self.ledger.close(start, timestamp="2026-10-02T12:00:02.000Z")
        self.assertEqual(exit_row, replay)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual([start, exit_row], self.ledger.read(self.node))

    def test_changed_start_cannot_close_the_original_invocation(self):
        start = self.start()
        before = self.path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as caught:
            self.ledger.close(dict(start, acting_session_id="foreign-session"), timestamp=END)
        self.assertEqual("stop_hold_close_conflict", caught.exception.code)
        self.assertEqual(before, self.path.read_bytes())

    def test_closure_preserves_original_consent_after_native_withdrawal(self):
        start = self.start()
        retire_waiter(self.root, self.node, self.workspace, "withdraw-after-start")
        closed = self.ledger.close(start, timestamp=END)
        self.assertEqual(start["consent_receipt_id"], closed["consent_receipt_id"])
        self.assertEqual(["start", "exit"], [row["phase"] for row in self.ledger.read(self.node)])

    def test_duplicate_exit_is_malformed_history_instead_of_a_live_or_closed_guess(self):
        start = self.start()
        closed = self.ledger.close(start, timestamp=END)
        self.append(dict(closed, id="stop-hold-" + uuid7_hex()))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.read(self.node)
        self.assertEqual("stop_hold_progress_invalid", caught.exception.code)

    def test_exit_with_foreign_immutable_consent_is_rejected(self):
        start = self.start()
        self.append(dict(start, id="stop-hold-" + uuid7_hex(), phase="exit", timestamp=END,
            consent_receipt_id="codex-wait-consent-" + uuid7_hex()))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.read(self.node)
        self.assertEqual("stop_hold_progress_invalid", caught.exception.code)

    def test_orphan_exit_is_rejected_without_reconstructing_a_start(self):
        start = self.start()
        self.path.unlink()
        self.append(dict(start, id="stop-hold-" + uuid7_hex(), phase="exit", timestamp=END))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.read(self.node)
        self.assertEqual("stop_hold_progress_invalid", caught.exception.code)

    def test_another_nodes_valid_row_cannot_witness_this_ledger(self):
        start = self.start()
        self.path.unlink()
        self.append(dict(start, node_id="foreign-recipient"))
        with self.assertRaises(IntegrityFailure) as caught:
            self.ledger.read(self.node)
        self.assertEqual("stop_hold_progress_invalid", caught.exception.code)

    def test_symlinked_leaf_is_rejected_even_when_target_is_valid_owned_history(self):
        self.start()
        retained = self.path.with_name("retained.jsonl")
        self.path.rename(retained)
        self.path.symlink_to(retained)
        before = retained.read_bytes()
        with self.assertRaises(IntegrityFailure):
            self.ledger.read(self.node)
        self.assertEqual(before, retained.read_bytes())

    def test_symlinked_parent_is_rejected_without_following_internal_alias(self):
        self.start()
        parent = self.path.parent
        retained = parent.with_name("retained-holds")
        parent.rename(retained)
        parent.symlink_to(retained, target_is_directory=True)
        with self.assertRaises(IntegrityFailure):
            self.ledger.read(self.node)

    def test_invalid_budget_refuses_before_creating_hold_history(self):
        for seconds in (True, 0, -1, 8, float("nan"), float("inf"), 10 ** 1000):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ProtocolRefusal):
                    self.start(deadline_seconds=seconds)
                self.assertFalse(self.path.exists())

    def test_superseded_claim_cannot_publish_new_hold_start(self):
        CodexWaitSessionLedger(self.root).arm(self.binding, self.consent, "new-owner-session",
            idempotency_key="superseding-claim", take_over=True)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.start()
        self.assertEqual("stop_hold_identity_invalid", caught.exception.code)
        self.assertFalse(self.path.exists())

    def test_stale_consent_cannot_publish_new_hold_start(self):
        CodexWaitConsentLedger(self.root).arm(self.binding,
            hook_timeout_seconds=30, wait_deadline_seconds=6, idempotency_key="new-consent")
        with self.assertRaises(ProtocolRefusal):
            self.start()
        self.assertFalse(self.path.exists())

    def test_already_guarded_start_and_close_share_the_existing_physical_guard(self):
        from unittest import mock

        with epoch_guard(self.root, exclusive=False), wake_coordination_guard(self.root, self.node):
            with mock.patch("floati.stop_hold.wake_coordination_guard",
                            side_effect=AssertionError("private seam must not reacquire node guard")):
                start = self.ledger._start_already_guarded(self.binding, self.consent, self.session,
                    started_at=START, deadline_seconds=7)
                closed = self.ledger._close_already_guarded(start, timestamp=END)
        self.assertEqual([start, closed], self.ledger.read(self.node))

    def test_malformed_shape_is_rejected_before_changing_retained_history(self):
        start = self.start()
        before = self.path.read_bytes()
        with self.assertRaises(ProtocolRefusal):
            self.ledger.close(dict(start, body="foreign content"), timestamp=END)
        self.assertEqual(before, self.path.read_bytes())

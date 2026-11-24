"""A native sender must progress while a reader validates detached evidence.

Only the reader thread's real validation/deep-copy boundary is gated. The
sender, clocks, ledger locks, framing, and native validation stay unchanged.
The watchdog is owned-thread cleanup, not a performance qualification bound.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import tests  # Pin HOME before native producers resolve defaults.

from floati import jsonl
from floati.events import EVENT_KINDS, EventLog
from floati.jsonl import VerifiedLedgerCursor
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.wake_hold import WakeHoldController
from tests.readiness import wait_event, wait_join


class VerifiedCursorLockHoldTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="floati-cursor-lock-hold-")
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "codex")
        self.events = EventLog(self.root)
        self.cursor = VerifiedLedgerCursor()
        self.sequence = 0

    def send(self, label):
        self.sequence += 1
        return self.events.send(
            "sender", "recipient", "floati", "a" * 40,
            "docs/dispatch.md", label, idempotency_key="native-send-" + str(self.sequence),
        )

    def read(self):
        return self.cursor.read(
            self.root, "events.jsonl", allowed_kinds=set(EVENT_KINDS),
            domain=WakeHoldController._EVENT_DOMAIN, skip_unknown_kinds=True,
        )

    def sender_progress_while_reader_paused(self, phase):
        seed = self.send("seed before capture")
        if phase != "cold_validation":
            self.read()
        if phase == "suffix_validation":
            suffix = self.send("suffix before capture")
            selected_message = str(suffix["id"])
        else:
            selected_message = str(seed["id"])
        before_send = self.events.event_records()
        paused, release, reader_done = threading.Event(), threading.Event(), threading.Event()
        reader_identity = []
        reader_results, reader_failures = [], []
        gate_used = []

        def gate_once():
            if gate_used:
                return
            gate_used.append(True)
            paused.set()
            wait_event(release, "owned reader boundary release", timeout=120)

        native_validate = jsonl.validate_record
        native_deepcopy = jsonl.copy.deepcopy

        def validate_reader_record(record, *args, **kwargs):
            if (reader_identity and threading.get_ident() == reader_identity[0]
                    and isinstance(record, dict) and record.get("id") == selected_message):
                gate_once()
            return native_validate(record, *args, **kwargs)

        def clone_reader_snapshot(value, *args, **kwargs):
            if (reader_identity and threading.get_ident() == reader_identity[0]
                    and value is self.cursor._records):
                gate_once()
            return native_deepcopy(value, *args, **kwargs)

        def reader():
            reader_identity.append(threading.get_ident())
            try:
                reader_results.append(self.read())
            except BaseException as error:
                reader_failures.append(error)
            finally:
                reader_done.set()

        target = ("floati.jsonl.copy.deepcopy" if phase == "unchanged_clone"
                  else "floati.jsonl.validate_record")
        observer = clone_reader_snapshot if phase == "unchanged_clone" else validate_reader_record
        thread = threading.Thread(target=reader, name="native-cursor-" + phase)
        sent, sender_failure = None, None
        paused_observed, sender_returned_before_release = False, False
        with mock.patch(target, side_effect=observer):
            thread.start()
            try:
                paused_observed = wait_event(paused, "native reader selected validation boundary")
                if paused_observed:
                    try:
                        # No waiter retry window, timeout override, fake clock,
                        # or lock patch: native EventLog keeps its default
                        # one-second ledger acquisition budget.
                        sent = self.send("sender while reader CPU work is paused")
                    except BaseException as error:
                        sender_failure = error
                    sender_returned_before_release = not reader_done.is_set()
            finally:
                release.set()
                wait_join(thread, "owned native reader must not outlive the test")

        self.assertTrue(paused_observed, "real native reader never reached the selected boundary")
        self.assertEqual([], reader_failures, "native reader failed after its boundary resumed")
        self.assertTrue(sender_returned_before_release, "sender returned only after reader validation resumed")
        self.assertIsNone(
            sender_failure,
            "native sender could not append while reader CPU work was paused: "
            + repr(sender_failure),
        )
        self.assertIsNotNone(sent)
        self.assertEqual(1, len(reader_results))
        captured_rows, captured_prefixes = reader_results[0]
        self.assertEqual(before_send, captured_rows, "reader must return its exact detached pre-send prefix")
        self.assertEqual(len(captured_rows) + 1, len(captured_prefixes))
        next_rows, next_prefixes = self.read()
        self.assertEqual(self.events.event_records(), next_rows)
        self.assertEqual(captured_prefixes, next_prefixes[:len(captured_prefixes)])
        self.assertEqual(1, sum(row["id"] == sent["id"] for row in next_rows))

    def test_native_sender_progresses_during_cold_reader_validation(self):
        self.sender_progress_while_reader_paused("cold_validation")

    def test_native_sender_progresses_during_suffix_reader_validation(self):
        self.sender_progress_while_reader_paused("suffix_validation")

    def test_native_sender_progresses_during_unchanged_reader_snapshot_clone(self):
        self.sender_progress_while_reader_paused("unchanged_clone")


if __name__ == "__main__":
    unittest.main()

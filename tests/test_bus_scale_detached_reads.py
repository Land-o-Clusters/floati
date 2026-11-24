"""Routine history reads release ledger locks while retaining native meaning."""

from __future__ import annotations

import fcntl
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from floati import jsonl
from floati.cursor import SparseCursor
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.framing import encode_frame
from floati.graph import HarborGraph
from floati.message_consumption import MessageConsumption
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.test_incremental_jsonl import _ObservedHistoryReader


class DetachedRoutineReadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(Path(temporary.name) / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.events = EventLog(self.root)
        self.cursor = SparseCursor(self.root)
        self.consumption = MessageConsumption(self.root)
        self.first = self.send("first")
        self.events.drain("recipient", acting_session_id="read-session")
        self.first_receipt = self.consumption.consume("recipient", self.first["id"], "read-session")
        self.paths = {
            self.root.resolve_relative(relative)
            for relative in ("events.jsonl", "receipts/deliveries/recipient.jsonl",
                             "receipts/acks/recipient.jsonl", "receipts/consumed/recipient.jsonl")
        }

    def send(self, key):
        return self.events.send("sender", "recipient", "floati", "a" * 40,
            "docs/evidence/detached-reads.md", "detached reader fixture",
            idempotency_key=key)["message"]

    @staticmethod
    def writer_available(path):
        with path.with_name(path.name + ".lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True

    @contextmanager
    def observing(self):
        native_open, native_decode = Path.open, jsonl._decode_path_records
        reads, decodes = [], []
        events_path = self.root.resolve_relative("events.jsonl")
        first_event_frame = events_path.read_bytes().splitlines(keepends=True)[0]

        def opening(path, mode="r", *args, **kwargs):
            stream = native_open(path, mode, *args, **kwargs)
            if path in self.paths and mode == "rb":
                return _ObservedHistoryReader(stream,
                    lambda: reads.append((path, self.writer_available(path))))
            return stream

        def decoding(path, *args, **kwargs):
            if path in self.paths:
                payload = args[2]
                # Native epoch-age admission decodes one physical event
                # frame under its planner lock. Independently match the
                # exact input; no complete-history decoder gets an exemption.
                bounded_first_frame = (path == events_path
                    and payload == first_event_frame
                    and len(payload) <= jsonl.MAX_RECORD_BYTES)
                decodes.append((path, self.writer_available(path), bounded_first_frame))
            return native_decode(path, *args, **kwargs)

        with mock.patch.object(Path, "open", opening), mock.patch.object(jsonl, "_decode_path_records", decoding):
            yield reads, decodes

    def assert_detached(self, observation):
        reads, decodes = observation
        self.assertTrue(reads, "the native command must actually read its history")
        self.assertTrue(all(available for _path, available in reads),
            "routine command retained a ledger lock during history byte reads: " + repr(reads))
        self.assertTrue(all(available or bounded for _path, available, bounded in decodes),
            "routine command retained a ledger lock during history decoding: " + repr(decodes))

    def test_consume_cold_join_reads_and_decodes_history_outside_ledger_locks(self):
        second = self.send("second")
        self.events.drain("recipient", acting_session_id="read-session")
        from floati.consumed_prefix import cache_name
        checkpoint = self.root.path / cache_name(self.consumption._relative("recipient")) / "checkpoint.json"
        self.assertTrue(checkpoint.is_file(), "cold control needs a previously certified consumed source")
        checkpoint.unlink()
        with self.observing() as observation:
            receipt = self.consumption.consume("recipient", second["id"], "read-session")
        self.assertEqual(second["id"], receipt["message_id"])
        self.assert_detached(observation)

    def test_inbox_reads_and_decodes_history_outside_ledger_locks(self):
        second = self.send("second")
        with self.observing() as observation:
            messages, _delivery, acknowledgment = self.events.drain("recipient", acting_session_id="read-session")
        self.assertEqual([second], messages)
        self.assertEqual([second["id"]], acknowledgment["item_ids"])
        self.assert_detached(observation)

    def test_graph_no_longer_reads_the_unused_growing_message_history(self):
        expected = HarborGraph(self.root).artifact()
        with self.observing() as observation:
            actual = HarborGraph(self.root).artifact()
        self.assertEqual(expected, actual)
        self.assertEqual(([], []), observation,
            "topology must not read or decode message/delivery/ACK/consumption history")

    def test_log_history_remains_detached_with_complete_output(self):
        second = self.send("second")
        with self.observing() as observation:
            messages = self.events.records()
        self.assertEqual([self.first, second], messages)
        self.assert_detached(observation)

    def test_consumption_rechecks_a_concurrent_append_before_publication(self):
        second = self.send("second")
        self.events.drain("recipient", acting_session_id="read-session")
        path = self.root.resolve_relative("receipts/consumed/recipient.jsonl")
        before = path.read_bytes()
        concurrent = self.consumption.consume("recipient", second["id"], "other-session")
        path.write_bytes(before)
        native_locked = jsonl._locked_path
        injected = []

        @contextmanager
        def locking(lock_path, *, exclusive, relative=None):
            if exclusive and lock_path == path.with_name(path.name + ".lock") and not injected:
                with native_locked(lock_path, exclusive=True, relative=relative):
                    with path.open("ab") as stream:
                        stream.write(encode_frame(concurrent))
                injected.append(True)
            with native_locked(lock_path, exclusive=exclusive, relative=relative):
                yield

        with mock.patch.object(jsonl, "_locked_path", locking):
            receipt = self.consumption.consume("recipient", second["id"], "read-session")
        self.assertEqual([True], injected)
        self.assertEqual(concurrent, receipt, "a concurrent native receipt must win idempotent replay")
        self.assertEqual([self.first_receipt, concurrent], self.consumption.records("recipient"))

    def test_detached_consumption_transaction_refuses_a_foreign_physical_coordinate(self):
        foreign = self.root.resolve_relative("work/foreign.jsonl")
        foreign.parent.mkdir(parents=True, exist_ok=True)
        foreign.write_bytes(b"")
        alias = self.root.resolve_relative("receipts/consumed/alias.jsonl")
        alias.symlink_to(foreign)
        with self.assertRaises(ProtocolRefusal) as caught:
            jsonl._transact_consumption_records(self.root, "receipts/consumed/alias.jsonl",
                lambda _rows: (self.first_receipt, [self.first_receipt]))
        self.assertEqual("consumption_controller_only", caught.exception.code)
        self.assertEqual(b"", foreign.read_bytes(), "refusal must precede all foreign-ledger mutation")

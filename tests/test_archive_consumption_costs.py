"""Small native fixtures expose collector read/copy and derived-output bounds."""
import tests
from copy import deepcopy
from pathlib import Path
import unittest
from unittest import mock

from floati import archive_consumption_checkpoint as checkpoint, message_consumption, cursor
from floati.cursor import SparseCursor
from floati.epoch_carry import ArchiveProofResolver
from floati.errors import ProtocolRefusal
from floati.message_consumption import MessageConsumption
from tests import test_archive_consumption_checkpoint as native_fixture


class ConsumptionCollectorCostTests(unittest.TestCase):
    setUp = native_fixture.ArchiveConsumptionCheckpointTests.setUp
    _send = native_fixture.ArchiveConsumptionCheckpointTests._send
    plan = native_fixture.ArchiveConsumptionCheckpointTests.plan

    def batch(self):
        messages = [self._send() for _ in range(4)]
        self.events.present("recipient")
        self.cursor.ack("recipient", [row["id"] for row in messages], acting_session_id="seen")
        return MessageConsumption(self.root).consume_many("recipient", [row["id"] for row in messages], "acting")

    def test_collector_reuses_one_native_ack_plane_and_one_detached_ack(self):
        receipts = self.batch()
        reads, copies, source_calls = [], [], []
        class CountingAck(dict):
            def __deepcopy__(self, memo):
                copies.append(self["id"])
                result = {}
                memo[id(self)] = result
                for key, value in self.items():
                    result[deepcopy(key, memo)] = deepcopy(value, memo)
                return result
        native_read = message_consumption.read_records_snapshot
        native_cursor_read = cursor.read_records_snapshot
        native_bound = checkpoint._read_bound_regular
        native_source = ArchiveProofResolver.source
        def read(root, path, **kwargs):
            rows = native_read(root, path, **kwargs)
            if "acks" in Path(path).parts:
                reads.append(Path(path))
                return [CountingAck(row) for row in rows]
            return rows
        def source(*args, **kwargs):
            source_calls.append(1)
            rows = list(native_source(*args, **kwargs))
            rows[4] = [CountingAck(row) for row in rows[4]]
            return tuple(rows)
        def cursor_read(root, path, **kwargs):
            if "acks" in Path(path).parts:
                reads.append(Path(path))
            return native_cursor_read(root, path, **kwargs)
        def bound(path, *args, **kwargs):
            if "acks" in Path(path).parts:
                reads.append(Path(path))
            return native_bound(path, *args, **kwargs)
        with mock.patch.object(message_consumption, "read_records_snapshot", read),\
                mock.patch.object(cursor, "read_records_snapshot", cursor_read),\
                mock.patch.object(checkpoint, "_read_bound_regular", bound),\
                mock.patch.object(ArchiveProofResolver, "source", source):
            result = self.plan()
        self.assertEqual(len(result["joins"]), len(receipts))
        self.assertEqual(len(source_calls), 1, "one captured source must serve the whole native batch")
        self.assertLessEqual(len(reads), 2, "native ACK plane reread for each receipt")
        self.assertEqual(len(copies), 1, "native ACK cloned for each receipt")

    def test_collector_refuses_capture_byte_and_record_budget(self):
        self.batch()
        for name in ("MAX_CAPTURED_BYTES", "MAX_CAPTURED_RECORDS", "MAX_DERIVED_REFERENCES", "MAX_DERIVED_BYTES"):
            with self.subTest(name=name), mock.patch.object(checkpoint, name, 1):
                with mock.patch.object(checkpoint, "deepcopy", side_effect=AssertionError("cloned before budget refusal")):
                    with self.assertRaises(ProtocolRefusal) as caught:
                        self.plan()
                self.assertEqual("archive_consumption_checkpoint_blocked", caught.exception.code)

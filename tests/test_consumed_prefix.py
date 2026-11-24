"""Consumption certification preserves native joins without history rereads."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati import jsonl, key_prefix, send_prefix
from floati.cursor import SparseCursor
from floati.errors import IntegrityFailure, ProtocolRefusal
from floati.events import EventLog
from floati.message_consumption import MessageConsumption
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.bus_scale_io import IOProbe
from tests.temp_roots import REAL_TEMP_ROOT
from tests.bus_scale_test_profile import StableSourceFixture
from tests.bus_scale_test_profile import stable_fixture_sources
from tests.bus_scale_prefix_fixture import shared_prefix_baseline


class ConsumedPrefixTests(StableSourceFixture, unittest.TestCase):
    @stable_fixture_sources()
    @shared_prefix_baseline()
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(temporary.name) / "fleet", create=True)
        for node in ("sender", "recipient"):
            Registry(self.root).register(node, "Codex")
        self.events = EventLog(self.root)
        self.cursor = SparseCursor(self.root)
        self.relative = Path("receipts/consumed/recipient.jsonl")
        self.path = self.root.resolve_relative(self.relative)

    def bank(self, relative=None):
        coordinate = self.relative if relative is None else Path(relative)
        digest = hashlib.sha256(coordinate.as_posix().encode()).hexdigest()
        return self.root.tenant_home / (".consumed-prefix-" + digest)

    def acked(self, key, *, worker=None):
        message = self.events.send(
            "sender", "recipient", "floati", "a" * 40,
            "docs/evidence/consumed-prefix.md", "native consumption fixture",
            idempotency_key=key, worker_session_id=worker)["message"]
        self.events.present("recipient", worker_session_id=worker)
        self.cursor.ack(
            "recipient", [message["id"]], acting_session_id="seen-session",
            worker_session_id=worker)
        return message

    def consume(self, message, *, key=None, session="acting-session"):
        return MessageConsumption(self.root).consume(
            "recipient", message["id"], session, idempotency_key=key)

    def certify(self):
        first = self.acked("first")
        receipt = self.consume(first, key="first-key")
        self.assertTrue(
            (self.bank() / "checkpoint.json").is_file(),
            "native consume must certify its exact ruled consumed ledger")
        return first, receipt

    def observe(self, operation):
        with IOProbe(self.root.tenant_home) as probe:
            result = operation()
        probe.assert_complete()
        return result, probe.read_bytes_by_path.get(
            self.relative.as_posix(), 0)

    def test_fresh_append_and_retry_read_zero_certified_history_bytes(self):
        first, receipt = self.certify()
        second = self.acked("second")
        appended, read_bytes = self.observe(lambda: self.consume(second))
        self.assertEqual(second["id"], appended["message_id"])
        self.assertEqual(0, read_bytes,
                         "fresh append replayed certified consumed history")
        replay, read_bytes = self.observe(
            lambda: self.consume(first, key="first-key"))
        self.assertEqual(receipt, replay)
        self.assertEqual(0, read_bytes,
                         "historical retry replayed certified consumed history")

    def test_event_delivery_and_ack_growth_does_not_invalidate_certification(self):
        first, receipt = self.certify()
        for index in range(8):
            self.acked("growth-" + str(index))
        replay, read_bytes = self.observe(
            lambda: self.consume(first, key="first-key"))
        self.assertEqual(receipt, replay)
        self.assertEqual(
            0, read_bytes,
            "append-monotone join-source growth invalidated consumed testimony")

    def test_one_byte_old_receipt_change_rebuilds_full_join_and_refuses(self):
        _first, receipt = self.certify()
        second = self.acked("second")
        before = self.path.stat()
        raw = self.path.read_bytes()
        selected = receipt["ack_receipt_id"].encode()
        changed = selected[:-1] + (
            b"0" if selected[-1:] != b"0" else b"1")
        damaged = raw.replace(selected, changed, 1)
        self.assertEqual(1, sum(left != right
                               for left, right in zip(raw, damaged)))
        self.path.write_bytes(damaged)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(IntegrityFailure) as refused:
            self.consume(second)
        self.assertEqual("consumption_evidence_invalid", refused.exception.code)
        self.assertEqual(damaged, self.path.read_bytes())

    def test_progress_invalidates_consumption_when_its_exact_ack_is_replaced(self):
        from floati.framing import encode_frame
        from floati.ids import uuid7_hex

        first, _receipt = self.certify()
        ledger = MessageConsumption(self.root)
        self.assertEqual(1, ledger.progress("recipient")["consumed"])
        relative = self.cursor._relative_path_for("recipient")
        rows = jsonl.read_records(self.root, relative, allowed_kinds={"ack_receipt"})
        replacement = dict(rows[0], id="ack-" + uuid7_hex())
        self.root.resolve_relative(relative).write_bytes(encode_frame(replacement))
        # A lawful native transaction recertifies the replacement ACK plane;
        # it does not restore the exact receipt named by the old consumption.
        self.cursor.ack("recipient", [first["id"]], acting_session_id="replacement-seen")
        with self.assertRaises(IntegrityFailure) as native:
            ledger._progress("recipient")
        self.assertEqual("consumption_evidence_invalid", native.exception.code)
        self.assertEqual(ledger.unknown_progress(native.exception), ledger.progress("recipient"))

    def test_missing_checkpoint_rebuilds_native_history_and_heals_retry(self):
        first, receipt = self.certify()
        (self.bank() / "checkpoint.json").unlink()
        replay, read_bytes = self.observe(
            lambda: self.consume(first, key="first-key"))
        self.assertEqual(receipt, replay)
        self.assertGreater(read_bytes, 0,
                           "missing checkpoint must run the native join")
        replay, read_bytes = self.observe(
            lambda: self.consume(first, key="first-key"))
        self.assertEqual(receipt, replay)
        self.assertEqual(0, read_bytes)

    def test_missing_selected_entry_cannot_be_interpreted_as_absence(self):
        first, receipt = self.certify()
        lookup = send_prefix._key("message", first["id"])
        filename = key_prefix.KeyPrefix.filename(lookup)
        selected = self.bank() / "nodes" / filename
        self.assertTrue(selected.is_file(),
                        "certified map lacks its message receipt")
        selected.unlink()
        before = self.path.read_bytes()
        replay, read_bytes = self.observe(
            lambda: self.consume(first, key="first-key"))
        self.assertEqual(receipt, replay)
        self.assertGreater(read_bytes, 0)
        self.assertEqual(before, self.path.read_bytes())
        replay, read_bytes = self.observe(
            lambda: self.consume(first, key="first-key"))
        self.assertEqual(receipt, replay)
        self.assertEqual(0, read_bytes)

    def test_explicit_key_conflict_is_still_checked_across_worker_planes(self):
        self.certify()
        worker = "worker-session"
        message = self.acked("worker-message", worker=worker)
        worker_relative = MessageConsumption(self.root)._relative(
            "recipient", worker)
        worker_path = self.root.resolve_relative(worker_relative)
        before = self.path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as refused:
            self.consume(message, key="first-key", session=worker)
        self.assertEqual("consume_idempotency_conflict", refused.exception.code)
        self.assertEqual(before, self.path.read_bytes())
        self.assertFalse(worker_path.exists())

    def test_worker_plane_uses_the_exact_relative_path_hash(self):
        worker = "worker-session"
        message = self.acked("worker-message", worker=worker)
        receipt = self.consume(message, session=worker)
        relative = MessageConsumption(self.root)._relative(
            "recipient", worker)
        self.assertEqual(worker, receipt["worker_session_id"])
        self.assertTrue((self.bank(relative) / "checkpoint.json").is_file())
        self.assertFalse(self.bank().exists())

    def test_writer_advances_prefix_while_holding_consumed_ledger_lock(self):
        self.certify()
        second = self.acked("second")
        lock = self.path.with_name(self.path.name + ".lock")
        native_locked = jsonl._locked_path
        native_appended = key_prefix.KeyPrefix.appended
        active = []
        observations = []

        @contextmanager
        def locked(selected, *, exclusive, relative=None, **kwargs):
            with native_locked(
                    selected, exclusive=exclusive, relative=relative, **kwargs):
                owned = selected == lock and exclusive
                if owned:
                    active.append(True)
                try:
                    yield
                finally:
                    if owned:
                        active.pop()

        def appended(prefix, batch, encoded, identity):
            if prefix.path == self.path:
                observations.append((bool(active), len(batch)))
            return native_appended(prefix, batch, encoded, identity)

        with patch.object(jsonl, "_locked_path", locked), patch.object(
                key_prefix.KeyPrefix, "appended", appended):
            self.consume(second)
        self.assertEqual([(True, 1)], observations,
                         "native consumed append did not advance under its lock")

    def test_duplicate_receipt_id_keeps_native_refusal_and_original_bytes(self):
        _first, receipt = self.certify()
        second = self.acked("second")
        before = self.path.read_bytes()
        with patch("floati.message_consumption.uuid7_hex",
                   return_value=receipt["id"][len("consume-"):]):
            with self.assertRaises(ProtocolRefusal) as refused:
                self.consume(second)
        self.assertEqual("duplicate_record_id", refused.exception.code)
        self.assertEqual(before, self.path.read_bytes())

    def test_warm_bank_does_not_bypass_candidate_ack_validation(self):
        self.certify()
        message = self.events.send(
            "sender", "recipient", "floati", "a" * 40,
            "docs/evidence/consumed-prefix.md", "unacknowledged candidate",
            idempotency_key="unacknowledged")["message"]
        before = self.path.read_bytes()
        with self.assertRaises(ProtocolRefusal) as refused:
            self.consume(message)
        self.assertEqual("consume_item_not_acknowledged", refused.exception.code)
        self.assertEqual(before, self.path.read_bytes())

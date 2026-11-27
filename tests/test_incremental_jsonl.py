"""PF-R5 verified incremental JSONL cursor contracts."""

from __future__ import annotations

from floati import fixture_ids as public_ids

import os
import fcntl
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import IntegrityFailure
from floati.events import EVENT_KINDS, EventLog
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.registry import Registry
from floati.root import FloatiRoot
from tests.readiness import wait_join
from tests.temp_roots import REAL_TEMP_ROOT


# A hash domain the product carries as a salt, rebuilt here from the fence's
# own governed token rather than spelled -- and deliberately NOT imported
# from the module under test, so this stays an independent witness to the
# bytes rather than a mirror of them.
DOMAIN = RETIRED_PRODUCT_NAME + "-wake-hold-events-v1"


class VerifiedLedgerCursorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        registry = Registry(self.root)
        registry.register(public_ids.builder("source"), "worker")
        registry.register(public_ids.worker("sink"), "worker")
        self.events = EventLog(self.root, registry)
        self.path = self.root.resolve_relative("events.jsonl")

    def send(self, key: str) -> dict:
        return self.events.send(
            public_ids.builder("source"),
            public_ids.worker("sink"),
            "floati",
            "a" * 40,
            "docs/evidence/incremental-jsonl.md",
            "incremental cursor fixture",
            idempotency_key=key,
        )

    def read(self, cursor: object):
        return cursor.read(
            self.root,
            "events.jsonl",
            allowed_kinds=set(EVENT_KINDS),
            domain=DOMAIN,
        )

    def assert_send_frames(self, rows: list[dict], *receipts: dict) -> None:
        """Each native send retains its envelope and exact frozen companion."""

        self.assertEqual(2 * len(receipts), len(rows))
        for index, receipt in enumerate(receipts):
            envelope, observation = rows[2 * index : 2 * index + 2]
            self.assertEqual(receipt["message"], envelope)
            self.assertEqual(
                {
                    "schema_version": 1,
                    "id": observation["id"],
                    "tenant_id": envelope["tenant_id"],
                    "timestamp": envelope["timestamp"],
                    "kind": "send_readiness_observation",
                    "envelope_id": envelope["id"],
                    "sender": envelope["sender"],
                    "recipient": envelope["recipient"],
                    "observed_at": envelope["timestamp"],
                    "readiness": receipt["recipient_readiness"],
                    "idempotency_key": envelope["idempotency_key"],
                },
                observation,
            )

    def test_append_validates_only_new_frames_and_extends_prefix_testimony(self) -> None:
        from floati.jsonl import VerifiedLedgerCursor

        first = self.send("cursor-first")
        cursor = VerifiedLedgerCursor()
        first_rows, first_prefixes = self.read(cursor)
        self.assert_send_frames(first_rows, first)
        self.assertEqual(3, len(first_prefixes))
        second = self.send("cursor-second")

        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("incremental append replayed the complete ledger"),
        ):
            rows, prefixes = self.read(cursor)

        self.assert_send_frames(rows, first, second)
        self.assertEqual(5, len(prefixes))
        self.assertEqual(first_rows, rows[: len(first_rows)])
        self.assertEqual(first_prefixes, prefixes[: len(first_prefixes)])

    def test_truncation_and_identity_change_each_force_one_full_replay(self) -> None:
        from floati.jsonl import VerifiedLedgerCursor

        first = self.send("cursor-reset-first")
        first_frames = self.path.read_bytes()
        second = self.send("cursor-reset-second")
        cursor = VerifiedLedgerCursor()
        self.assert_send_frames(self.read(cursor)[0], first, second)
        self.path.write_bytes(first_frames)
        real_read_bytes = Path.read_bytes
        reads = [0]

        def recording_read_bytes(path: Path) -> bytes:
            if path.resolve(strict=False) == self.path.resolve(strict=False):
                reads[0] += 1
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", recording_read_bytes):
            truncated, truncated_prefixes = self.read(cursor)
        self.assert_send_frames(truncated, first)
        self.assertEqual(3, len(truncated_prefixes))
        self.assertEqual(1, reads[0])

        replacement = self.path.with_name("events.replacement")
        replacement.write_bytes(self.path.read_bytes())
        os.replace(replacement, self.path)
        reads[0] = 0
        with mock.patch.object(Path, "read_bytes", recording_read_bytes):
            replaced, replaced_prefixes = self.read(cursor)
        self.assertEqual(truncated, replaced)
        self.assertEqual(truncated_prefixes, replaced_prefixes)
        self.assertEqual(1, reads[0])

    def test_same_inode_truncate_and_regrow_forces_one_full_replay(self) -> None:
        """A rolled epoch cannot masquerade as an append after regrowing."""

        from floati.jsonl import VerifiedLedgerCursor

        first = self.send("cursor-regrow-first")
        second = self.send("cursor-regrow-second")
        cursor = VerifiedLedgerCursor()
        original_rows, original_prefixes = self.read(cursor)
        self.assert_send_frames(original_rows, first, second)
        self.assertEqual(5, len(original_prefixes))
        original_bytes = self.path.read_bytes()
        original_frame_count = len(original_bytes.splitlines(keepends=True))
        self.assertEqual(4, original_frame_count)
        third = self.send("cursor-regrow-third")
        fourth = self.send("cursor-regrow-fourth")
        frames = self.path.read_bytes().splitlines(keepends=True)
        original_identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        self.assertEqual(8, len(frames))
        regrown_bytes = b"".join(frames[original_frame_count:])
        self.assertGreaterEqual(len(regrown_bytes), len(original_bytes))
        self.path.write_bytes(regrown_bytes)
        self.assertEqual(
            original_identity,
            (self.path.stat().st_dev, self.path.stat().st_ino),
        )

        real_read_bytes = Path.read_bytes
        reads = [0]

        def recording_read_bytes(path: Path) -> bytes:
            if path.resolve(strict=False) == self.path.resolve(strict=False):
                reads[0] += 1
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", recording_read_bytes):
            regrown, regrown_prefixes = self.read(cursor)
        self.assert_send_frames(regrown, third, fourth)
        self.assertEqual(5, len(regrown_prefixes))
        self.assertEqual(1, reads[0])

    def test_incremental_malformed_frame_refuses_without_advancing_cursor(self) -> None:
        from floati.jsonl import VerifiedLedgerCursor

        self.send("cursor-malformed-first")
        cursor = VerifiedLedgerCursor()
        before = self.read(cursor)
        with self.path.open("ab") as handle:
            handle.write(b'{"incomplete":')

        with self.assertRaises(IntegrityFailure) as caught:
            self.read(cursor)
        self.assertEqual("incomplete_jsonl_line", caught.exception.code)

        with self.path.open("ab") as handle:
            handle.write(b"true}\n")
        with self.assertRaises(IntegrityFailure):
            self.read(cursor)
        self.assertEqual(before, cursor.snapshot())

    def test_returned_records_cannot_mutate_the_verified_cached_prefix(self) -> None:
        from floati.jsonl import VerifiedLedgerCursor

        original = self.send("cursor-snapshot-copy")
        cursor = VerifiedLedgerCursor()
        rows, _prefixes = self.read(cursor)
        rows[0]["id"] = "msg-mutated-by-caller"
        rows[1]["readiness"]["mechanism_evidence"][0]["reason"] = "mutated-by-caller"

        retained, _prefixes = self.read(cursor)
        self.assert_send_frames(retained, original)


class _ObservedHistoryReader:
    """Pause the real descriptor read at a deterministic concurrent boundary."""

    def __init__(self, stream, before_read):
        self.stream = stream
        self.before_read = before_read

    def __enter__(self):
        self.stream.__enter__()
        return self

    def __exit__(self, *args):
        return self.stream.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self.stream, name)

    def read(self, *args):
        self.before_read()
        return self.stream.read(*args)


class LogHistorySnapshotTests(unittest.TestCase):
    """A complete-history query must not monopolize the append ledger lock."""

    def setUp(self) -> None:
        from floati.framing import encode_frame

        self.temp = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open(Path(self.temp.name), "alpha")
        self.events = EventLog(self.root)
        self.path = self.root.resolve_relative("events.jsonl")
        self.first = self.envelope(1)
        self.second = self.envelope(2)
        self.path.write_bytes(encode_frame(self.first))

    def envelope(self, ordinal: int) -> dict:
        return {
            "schema_version": 0,
            "id": "msg-0000000000007000800000000000000" + str(ordinal),
            "tenant_id": "alpha",
            "timestamp": "2026-10-25T12:00:00.000Z",
            "kind": "message_envelope",
            "sender": "sender",
            "recipient": "recipient",
            "repo": "floati",
            "sha": "a" * 40,
            "doc": "docs/evidence/history.md",
            "note": "retained history " + str(ordinal),
            "idempotency_key": "history-" + str(ordinal),
        }

    def observe_read(self, callback):
        native_open = Path.open

        def opening(path, mode="r", *args, **kwargs):
            stream = native_open(path, mode, *args, **kwargs)
            if path == self.path and mode == "rb":
                return _ObservedHistoryReader(stream, callback)
            return stream

        return mock.patch.object(Path, "open", opening)

    def with_writer_lock(self, callback) -> bool:
        with self.path.with_name("events.jsonl.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            try:
                callback()
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return True

    def test_history_bytes_are_read_after_releasing_the_ledger_lock(self) -> None:
        available = []
        with self.observe_read(lambda: available.append(self.with_writer_lock(lambda: None))):
            records = self.events.records()
        self.assertEqual([self.first], records)
        self.assertEqual([True], available, "history I/O still blocks an independent ledger writer")

    def test_history_decode_does_not_block_an_independent_ledger_writer(self) -> None:
        from floati import jsonl

        native_decode = jsonl._decode_path_records
        available = []

        def decoding(*args, **kwargs):
            available.append(self.with_writer_lock(lambda: None))
            return native_decode(*args, **kwargs)

        with mock.patch.object(jsonl, "_decode_path_records", decoding):
            records = self.events.records()
        self.assertEqual([self.first], records)
        self.assertEqual([True], available, "history decoding still retains the ledger lock")

    def replace_history_generation(self) -> None:
        from floati import bus_epoch, jsonl
        from floati.framing import encode_frame

        with bus_epoch.epoch_guard(self.root, exclusive=True):
            selected = bus_epoch._preflight_selected_members(self.root)
            with jsonl._epoch_selected_writer(self.root, selected) as replace:
                replace(selected, encode_frame(self.second))

    def test_native_epoch_replacement_is_excluded_during_unlocked_history_read(self) -> None:
        from floati import bus_epoch
        from floati.errors import ProtocolRefusal

        outcome = {}
        append_lock_available = []

        def replacing():
            try:
                self.replace_history_generation()
                outcome["replaced"] = True
            except ProtocolRefusal as failure:
                outcome["refusal"] = failure.code
            except BaseException as failure:
                outcome["unexpected"] = repr(failure)

        def during_read():
            append_lock_available.append(self.with_writer_lock(lambda: None))
            writer = threading.Thread(target=replacing)
            writer.start()
            wait_join(writer, "native epoch attempt finishes during history read")
            self.assertFalse(writer.is_alive(), "native epoch attempt did not finish its bounded lock attempt")

        records, failure = None, None
        with mock.patch.object(bus_epoch, "EPOCH_LOCK_TIMEOUT_SECONDS", 0), self.observe_read(during_read):
            try:
                records = self.events.records()
            except IntegrityFailure as caught:
                failure = caught
        self.assertEqual([True], append_lock_available, "the history query retained its ledger lock")
        self.assertEqual(
            {"refusal": "epoch_lock_timeout"}, outcome,
            "native epoch replacement mutated the generation while its descriptor was being read",
        )
        self.assertIsNone(failure, "the captured history was truncated by an admitted epoch writer")
        self.assertEqual([self.first], records)
        identity = self.path.stat().st_ino
        self.replace_history_generation()
        self.assertEqual(identity, self.path.stat().st_ino, "control must exercise native in-place replacement")
        self.assertEqual([self.second], self.events.records())

    def test_epoch_replacement_can_proceed_after_history_bytes_detach_before_decode(self) -> None:
        from floati import jsonl

        native_decode = jsonl._decode_path_records
        replacements = []

        def decoding(*args, **kwargs):
            self.replace_history_generation()
            replacements.append(True)
            return native_decode(*args, **kwargs)

        with mock.patch.object(jsonl, "_decode_path_records", decoding):
            records = self.events.records()
        self.assertEqual([True], replacements)
        self.assertEqual([self.first], records)
        self.assertEqual([self.second], self.events.records())

    def test_append_after_snapshot_boundary_is_visible_only_to_the_next_query(self) -> None:
        from floati.framing import encode_frame

        def append_after_seal():
            with self.path.open("ab") as handle:
                handle.write(encode_frame(self.second))

        with self.observe_read(append_after_seal):
            records = self.events.records()
        self.assertEqual([self.first], records, "history escaped its captured byte boundary")
        self.assertEqual([self.first, self.second], self.events.records())

    def test_replacement_after_snapshot_boundary_preserves_the_opened_generation(self) -> None:
        from floati.framing import encode_frame

        replacement = self.path.with_name("events.replacement")
        replacement.write_bytes(encode_frame(self.second))
        with self.observe_read(lambda: os.replace(replacement, self.path)):
            records = self.events.records()
        self.assertEqual([self.first], records)
        self.assertEqual([self.second], self.events.records())

    def test_truncated_sealed_input_refuses_instead_of_returning_an_empty_history(self) -> None:
        with self.observe_read(lambda: self.path.write_bytes(b"")):
            with self.assertRaises(IntegrityFailure):
                self.events.records()

    def test_history_keeps_future_kind_skipping_and_known_event_order_validation(self) -> None:
        from floati.framing import encode_frame

        future = dict(self.second, kind="future_history_kind", id="future-00000000000070008000000000000002")
        self.path.write_bytes(encode_frame(self.first) + encode_frame(future))
        self.assertEqual([self.first], self.events.records())
        invalid = dict(self.second, reply_to="msg-00000000000070008000000000000003")
        self.path.write_bytes(encode_frame(self.first) + encode_frame(invalid))
        with self.assertRaises(IntegrityFailure) as caught:
            self.events.records()
        self.assertEqual("message_order_invalid", caught.exception.code)

    def test_history_cannot_follow_an_event_symlink_outside_the_selected_root(self) -> None:
        from floati.errors import ProtocolRefusal

        outside = Path(self.temp.name) / "foreign.jsonl"
        outside.write_bytes(self.path.read_bytes())
        self.path.unlink()
        self.path.symlink_to(outside)
        with self.assertRaises(ProtocolRefusal) as caught:
            self.events.records()
        self.assertEqual("path_not_contained", caught.exception.code)

    def test_observation_history_read_never_creates_a_lock_in_the_observed_tenant(self) -> None:
        from floati.jsonl import read_records_compatible_sealed_snapshot

        observer = FloatiRoot.open(Path(self.temp.name), "observer")
        authority = observer.observe_tenant(observer.grant_observation("alpha"), "alpha")
        before = {path.relative_to(self.root.tenant_home) for path in self.root.tenant_home.rglob("*")}
        records, unknown = read_records_compatible_sealed_snapshot(
            authority, "events.jsonl", allowed_kinds=set(EVENT_KINDS)
        )
        self.assertEqual([self.first], records)
        self.assertEqual([], unknown)
        after = {path.relative_to(self.root.tenant_home) for path in self.root.tenant_home.rglob("*")}
        self.assertEqual(before, after, "read authority created files in the observed tenant")


if __name__ == "__main__":
    unittest.main()

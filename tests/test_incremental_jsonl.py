"""PF-R5 verified incremental JSONL cursor contracts."""

from __future__ import annotations

from floati import fixture_ids as public_ids

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati.errors import IntegrityFailure
from floati.events import EVENT_KINDS, EventLog
from floati.identity_fence import RETIRED_PRODUCT_NAME
from floati.registry import Registry
from floati.root import FloatiRoot
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

    def test_append_validates_only_new_frames_and_extends_prefix_testimony(self) -> None:
        from floati.jsonl import VerifiedLedgerCursor

        first = self.send("cursor-first")
        cursor = VerifiedLedgerCursor()
        first_rows, first_prefixes = self.read(cursor)
        second = self.send("cursor-second")

        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("incremental append replayed the complete ledger"),
        ):
            rows, prefixes = self.read(cursor)

        self.assertEqual([first, second], rows)
        self.assertEqual(first_prefixes, prefixes[: len(first_prefixes)])

    def test_truncation_and_identity_change_each_force_one_full_replay(self) -> None:
        from floati.jsonl import VerifiedLedgerCursor

        self.send("cursor-reset-first")
        self.send("cursor-reset-second")
        cursor = VerifiedLedgerCursor()
        self.read(cursor)
        first_frame = self.path.read_bytes().splitlines(keepends=True)[0]
        self.path.write_bytes(first_frame)
        real_read_bytes = Path.read_bytes
        reads = [0]

        def recording_read_bytes(path: Path) -> bytes:
            if path.resolve(strict=False) == self.path.resolve(strict=False):
                reads[0] += 1
            return real_read_bytes(path)

        with mock.patch.object(Path, "read_bytes", recording_read_bytes):
            truncated, _prefixes = self.read(cursor)
        self.assertEqual(1, len(truncated))
        self.assertEqual(1, reads[0])

        replacement = self.path.with_name("events.replacement")
        replacement.write_bytes(self.path.read_bytes())
        os.replace(replacement, self.path)
        reads[0] = 0
        with mock.patch.object(Path, "read_bytes", recording_read_bytes):
            replaced, _prefixes = self.read(cursor)
        self.assertEqual(truncated, replaced)
        self.assertEqual(1, reads[0])

    def test_same_inode_truncate_and_regrow_forces_one_full_replay(self) -> None:
        """A rolled epoch cannot masquerade as an append after regrowing."""

        from floati.jsonl import VerifiedLedgerCursor

        first = self.send("cursor-regrow-first")
        second = self.send("cursor-regrow-second")
        cursor = VerifiedLedgerCursor()
        self.assertEqual([first, second], self.read(cursor)[0])
        third = self.send("cursor-regrow-third")
        fourth = self.send("cursor-regrow-fourth")
        frames = self.path.read_bytes().splitlines(keepends=True)
        original_identity = (self.path.stat().st_dev, self.path.stat().st_ino)
        self.path.write_bytes(b"".join(frames[2:]))
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
            regrown, _prefixes = self.read(cursor)
        self.assertEqual([third, fourth], regrown)
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

        retained, _prefixes = self.read(cursor)
        self.assertEqual([original], retained)


if __name__ == "__main__":
    unittest.main()

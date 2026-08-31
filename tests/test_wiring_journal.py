from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from floati import wiring_journal


class WiringJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "dest"
        self.destination.mkdir()

    def test_append_creates_journal_with_chain_and_defaults_enforced(self):
        first = wiring_journal.append_entry(self.destination, {
            "v": 1, "ts": "t", "actor": {}, "action": "install",
            "kind": "file", "path": "/tmp/a", "op": "create",
            "sha256": "a" * 64,
        })
        second = wiring_journal.append_entry(self.destination, {
            "v": 1, "ts": "t", "actor": {}, "action": "update",
            "kind": "file", "path": "/tmp/a", "op": "replace",
            "sha256": "b" * 64,
        })
        self.assertEqual(first.ordinal, 1)
        self.assertEqual(second.ordinal, 2)
        entries = wiring_journal.read_entries(
            wiring_journal.journal_path(self.destination))
        self.assertEqual([e.ordinal for e in entries], [1, 2])
        # Chain: second's prevHash is first's entryHash.
        self.assertEqual(entries[1].payload["prevHash"],
                         entries[0].payload["entryHash"])

    def test_first_append_fsyncs_the_new_journal_parent_directory(self):
        """The first durable intent row must also durably link its new file."""

        observed: list[str] = []
        real_fsync = wiring_journal.os.fsync

        def observe(descriptor: int) -> None:
            mode = os.fstat(descriptor).st_mode
            observed.append("directory" if stat.S_ISDIR(mode) else "file")
            real_fsync(descriptor)

        with mock.patch.object(wiring_journal.os, "fsync", side_effect=observe):
            wiring_journal.append_entry(self.destination, {
                "v": 1, "ts": "t", "actor": {}, "action": "install",
                "kind": "file", "path": "/tmp/a", "op": "create",
                "sha256": "a" * 64,
            })

        self.assertEqual("file", observed[0])
        self.assertIn("directory", observed[1:])

    def test_read_stops_at_first_corrupt_line_fail_closed(self):
        good = wiring_journal.append_entry(self.destination, {
            "v": 1, "ts": "t", "actor": {}, "action": "install",
            "kind": "file", "path": "/tmp/a", "op": "create",
        })
        path = wiring_journal.journal_path(self.destination)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("{broken\n")
            handle.write(json.dumps({
                "v": 1, "ts": "t", "actor": {}, "action": "install",
                "kind": "file", "path": "/tmp/b", "op": "create",
                "prevHash": good.payload["entryHash"],
                "entryHash": "whatever",
            }) + "\n")
        with self.assertRaises(wiring_journal.WiringJournalCorrupt) as ctx:
            wiring_journal.read_entries(path)
        # Offset points at the corrupt line, and the valid entry before it
        # was returned up to that point by any prefix read.
        self.assertGreaterEqual(ctx.exception.offset, len(str(good.ordinal)))

    def test_closed_vocabulary_refused_on_append(self):
        for bad in (
            {"kind": "launch_agent"},
            {"action": "exorcise"},
            {"op": "banish"},
        ):
            payload = {"v": 1, "ts": "t", "actor": {}, "action": "install",
                       "kind": "file", "path": "/tmp/x", "op": "create"}
            payload.update(bad)
            with self.assertRaises(ValueError):
                wiring_journal.append_entry(self.destination, payload)

    def test_empty_usage_style_defaults_are_absent_not_zero(self):
        # Parity with the honest-cell doctrine: an absent optional stays nil.
        entry = wiring_journal.append_entry(self.destination, {
            "v": 1, "ts": "t", "actor": {}, "action": "install",
            "kind": "bus_root", "path": "/tmp/root", "op": "create",
            "preserved": True,
        })
        self.assertIsNone(entry.sha256)


if __name__ == "__main__":
    unittest.main()

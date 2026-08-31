from __future__ import annotations

from floati import fixture_ids as public_ids

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.cursor import SparseCursor
from floati.events import EventLog
from floati.registry import Registry
from floati.root import FloatiRoot


class InboxSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "inbox-snapshot", create=True
        )
        registry = Registry(self.root)
        registry.register(public_ids.worker('alpha'), "worker")
        registry.register("bob", "worker")
        self.events = EventLog(self.root)

    def send(self, index: int) -> dict[str, object]:
        return self.events.send(
            public_ids.worker('alpha'),
            "bob",
            "slipway",
            f"{index + 1:x}" * 40,
            "docs/evidence/HM3H-GAUNTLET.md",
            f"snapshot message {index}",
            idempotency_key=f"snapshot-message-{index}",
        )

    def test_stable_read_and_simple_event_tail_do_not_full_scan(self) -> None:
        first_message = self.send(0)
        first, _receipt = self.events.present("bob")
        self.assertEqual([first_message["id"]], [row["id"] for row in first])

        with patch.object(
            EventLog,
            "_present_full_scan",
            side_effect=AssertionError("authoritative full scan was used"),
        ):
            stable, _receipt = self.events.present("bob")
        self.assertEqual([first_message["id"]], [row["id"] for row in stable])

        second_message = self.send(1)
        with patch.object(
            EventLog,
            "_present_full_scan",
            side_effect=AssertionError("event tail was not replayed"),
        ):
            tailed, _receipt = self.events.present("bob")
        self.assertEqual(
            [first_message["id"], second_message["id"]],
            [row["id"] for row in tailed],
        )

    def test_ack_that_exposes_truncated_history_falls_back_correctly(self) -> None:
        first_message = self.send(0)
        second_message = self.send(1)
        shown, _receipt = self.events.present("bob", limit=1)
        self.assertEqual([first_message["id"]], [row["id"] for row in shown])
        SparseCursor(self.root).ack(
            "bob", [first_message["id"]], acting_session_id="snapshot-session"
        )

        with patch.object(
            EventLog,
            "_present_full_scan",
            wraps=self.events._present_full_scan,
        ) as full_scan:
            next_messages, _receipt = self.events.present("bob", limit=1)

        self.assertEqual(1, full_scan.call_count)
        self.assertEqual(
            [second_message["id"]], [row["id"] for row in next_messages]
        )


if __name__ == "__main__":
    unittest.main()

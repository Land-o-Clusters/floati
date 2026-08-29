from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.jsonl import append_record
from floati.replay import ReplayTimeline
from floati.replay_render import render_replay_plain
from floati.root import FloatiRoot


UUIDS = (
    "018f7e9b3c117abc8def0123456789ab",
    "018f7e9b3c127abc8def0123456789ab",
)


class ReplaySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "replay-snapshot", create=True
        )

    def append_denial(self, index: int, timestamp: str) -> dict[str, object]:
        record = {
            "schema_version": 0,
            "id": f"denial-{UUIDS[index]}",
            "tenant_id": self.root.tenant_id,
            "timestamp": timestamp,
            "kind": "denial_receipt",
            "attempt_id": f"attempt-{UUIDS[index]}",
            "claimed_sender": "alice",
            "claimed_recipient": "bob",
            "reason_code": "unknown_sender",
        }
        append_record(
            self.root,
            "receipts/denials.jsonl",
            record,
            allowed_kinds={"denial_receipt"},
        )
        return record

    def test_second_plain_render_uses_verified_cache(self) -> None:
        self.append_denial(0, "2026-08-01T12:00:00.000Z")
        expected = render_replay_plain(ReplayTimeline.from_root(self.root).artifact())

        with patch(
            "floati.replay._read_full_timeline",
            side_effect=AssertionError("stable replay performed a full scan"),
        ):
            actual = render_replay_plain(
                ReplayTimeline.from_root(self.root).artifact()
            )

        self.assertEqual(expected, actual)

    def test_last_source_tail_appends_under_ordinal_not_hostile_time(self) -> None:
        first = self.append_denial(0, "2036-01-01T00:00:00.000Z")
        before = render_replay_plain(ReplayTimeline.from_root(self.root).artifact())
        second = self.append_denial(1, "2020-01-01T00:00:00.000Z")

        with patch(
            "floati.replay._read_full_timeline",
            side_effect=AssertionError("denial tail performed a full scan"),
        ):
            artifact = ReplayTimeline.from_root(self.root).artifact()
            after = render_replay_plain(artifact)

        self.assertGreater(len(after), len(before))
        self.assertEqual(2, after.count("DENIED"))
        events = artifact["events"]
        self.assertEqual(
            [first["id"], second["id"]],
            [event["record_id"] for event in events],
        )
        self.assertEqual([1, 2], [event["source_ordinal"] for event in events])
        self.assertEqual(sorted(event["elapsed_ms"] for event in events), [event["elapsed_ms"] for event in events])


if __name__ == "__main__":
    unittest.main()

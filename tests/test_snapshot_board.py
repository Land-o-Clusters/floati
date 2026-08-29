from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from floati.registry import Registry
from floati.errors import SnapshotRefusal
from floati.root import FloatiRoot
from floati.tui import model_from_root
from floati.tui_render import HarborBoardModel, render_plain_dump
from floati.work import WorkLog


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class BoardSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temp.name) / "board-snapshot", create=True
        )
        Registry(self.root).register("alice", "worker")
        WorkLog(self.root).add("first item", "alice", [], now=NOW)

    def test_stable_board_and_work_tail_do_not_full_scan(self) -> None:
        expected = render_plain_dump(model_from_root(self.root, NOW))
        with patch(
            "floati.tui._model_from_root_full",
            side_effect=AssertionError("stable board performed a full scan"),
        ):
            stable = render_plain_dump(model_from_root(self.root, NOW))
        self.assertEqual(expected, stable)

        WorkLog(self.root).add(
            "second item", "alice", [], now=NOW + timedelta(seconds=1)
        )
        with patch(
            "floati.tui._model_from_root_full",
            side_effect=AssertionError("work tail performed a full scan"),
        ):
            tailed = render_plain_dump(
                model_from_root(self.root, NOW + timedelta(seconds=1))
            )
        self.assertIn("second item", tailed)
        self.assertGreater(len(tailed), len(expected))

    def test_snapshot_model_round_trip_preserves_receipt_append_order(self) -> None:
        expected = render_plain_dump(model_from_root(self.root, NOW))
        actual = render_plain_dump(model_from_root(self.root, NOW))

        self.assertEqual(expected, actual)

    def test_pre_attention_effect_snapshot_is_rebuilt(self) -> None:
        payload = model_from_root(self.root, NOW).to_snapshot()
        payload["effects"].pop("attention")

        with self.assertRaisesRegex(SnapshotRefusal, "snapshot_payload_invalid"):
            HarborBoardModel.from_snapshot(payload)


if __name__ == "__main__":
    unittest.main()

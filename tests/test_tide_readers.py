from __future__ import annotations

from floati import fixture_ids as public_ids

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from floati.errors import ProtocolRefusal
from floati.tide_catalog import metric_for
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.tide_policy import TideTestimonyLedger


class TideReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="\x2fprivate\x2ftmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_codex_reader_scans_exact_session_to_latest_nested_usage(self) -> None:
        from floati.tide import BoundedTideReader

        session = "019f6386-ba54-7c82-8091-d3d490cf24d4"
        target = self.base / "codex/2026/08/28" / f"rollout-now-{session}.jsonl"
        target.parent.mkdir(parents=True)
        rows = [
            {"payload": {"type": "session_meta", "id": session}},
            {"payload": {"info": {"last_token_usage": {"total_tokens": 700}, "model_context_window": 1000}}},
        ]
        target.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        reading = BoundedTideReader(codex_root=self.base / "codex", cursor_root=self.base / "cursor").read(
            SimpleNamespace(harness="codex", session_id=session),
            metric_for("codex", "context_fraction"),
        )
        self.assertEqual("0.700000", reading.value)
        self.assertIn(str(target), reading.sources)

    def test_cursor_reader_uses_exact_session_transcript_only(self) -> None:
        from floati.tide import BoundedTideReader

        session = "cursor-session"
        target = self.base / "cursor/project/agent-transcripts" / session / f"{session}.jsonl"
        target.parent.mkdir(parents=True)
        target.write_text('{"role":"user"}\n{"role":"assistant"}\n', encoding="utf-8")
        reader = BoundedTideReader(codex_root=self.base / "codex", cursor_root=self.base / "cursor")
        count = reader.read(SimpleNamespace(harness="cursor", session_id=session), metric_for("cursor", "turn_count"))
        size = reader.read(SimpleNamespace(harness="cursor", session_id=session), metric_for("cursor", "transcript_bytes"))
        self.assertEqual("2", count.value)
        self.assertEqual(str(target.stat().st_size), size.value)

    def test_reader_refuses_a_match_reached_through_a_symlinked_ancestor(self) -> None:
        from floati.tide import BoundedTideReader

        session = "019f6386-ba54-7c82-8091-d3d490cf24d4"
        outside = self.base / "outside/08/28"
        outside.mkdir(parents=True)
        target = outside / f"rollout-now-{session}.jsonl"
        target.write_text(
            json.dumps({"payload": {"info": {"last_token_usage": {"total_tokens": 7}, "model_context_window": 10}}}) + "\n",
            encoding="utf-8",
        )
        codex_root = self.base / "codex"
        codex_root.mkdir()
        (codex_root / "2026").symlink_to(self.base / "outside", target_is_directory=True)

        with self.assertRaises(ProtocolRefusal) as caught:
            BoundedTideReader(codex_root=codex_root).read(
                SimpleNamespace(harness="codex", session_id=session),
                metric_for("codex", "context_fraction"),
            )

        self.assertEqual("tide_reading_unavailable", caught.exception.code)

    def test_class_b_reader_preserves_testimony_stamp_and_command_source(self) -> None:
        from floati.tide import BoundedTideReader

        root = FloatiRoot.open_direct_home(self.base / "fleet", create=True)
        Registry(root).register(public_ids.builder('cursor'), "cursor")
        testimony = TideTestimonyLedger(root).record(
            public_ids.builder('cursor'), "self_reported_context_fraction", "75%", "/context",
            idempotency_key="testimony",
        )
        reading = BoundedTideReader(root=root).read(
            SimpleNamespace(harness="cursor", node_id=public_ids.builder('cursor'), session_id="unused"),
            metric_for("cursor", "self_reported_context_fraction"),
        )
        self.assertEqual("SELF_REPORTED", reading.stamp)
        self.assertEqual("0.750000", reading.value)
        self.assertIn(f"testimony:{testimony['id']}", reading.sources)


if __name__ == "__main__":
    unittest.main()

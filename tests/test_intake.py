from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
import argparse
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from floati.errors import ProtocolRefusal
from floati.jsonl import read_records_snapshot
from floati.registry import Registry
from floati.root import FloatiRoot
from floati.work import WorkLog
from tests.temp_roots import REAL_TEMP_ROOT


class LocalIntakeGrammarTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.queue = Path(self.temporary.name) / "queue"
        self.queue.mkdir()
        (self.queue / "good.md").write_text(
            "# Ship the buoy\nverbatim body\n## not a title\n",
            encoding="utf-8",
        )
        (self.queue / "absent.md").write_text(
            "plain task without a heading\n",
            encoding="utf-8",
        )
        (self.queue / "ambiguous.md").write_text(
            "# First title\nbody\n# Second title\n",
            encoding="utf-8",
        )
        (self.queue / "large.md").write_bytes(
            b"# Large task\n" + b"x" * (256 * 1024)
        )
        (self.queue / "invalid.md").write_bytes(b"# Invalid\n\xff\xfe")
        self.symlink = self.queue / "symlink.md"
        self.symlink.symlink_to(self.queue / "good.md")

    def _tree_snapshot(self) -> tuple[tuple[str, str, bytes | str], ...]:
        rows: list[tuple[str, str, bytes | str]] = []
        for path in sorted(self.queue.iterdir()):
            relative = path.name
            mode = stat.S_IFMT(path.lstat().st_mode)
            if stat.S_ISLNK(mode):
                rows.append((relative, "symlink", os.readlink(path)))
            else:
                rows.append((relative, "file", path.read_bytes()))
        return tuple(rows)

    def test_scan_reports_six_verdicts_and_writes_nothing(self) -> None:
        from floati.intake import scan_directory

        before = self._tree_snapshot()
        verdicts = scan_directory(self.queue)
        after = self._tree_snapshot()

        self.assertEqual(before, after)
        self.assertEqual(
            ["absent.md", "ambiguous.md", "good.md", "invalid.md", "large.md", "symlink.md"],
            [row["path"] for row in verdicts],
        )
        self.assertTrue(all(set(row) == {"path", "verdict", "code", "detail"} for row in verdicts))
        by_path = {row["path"]: row for row in verdicts}
        self.assertEqual("eligible", by_path["good.md"]["verdict"])
        self.assertEqual("intake_markdown_title_absent", by_path["absent.md"]["code"])
        self.assertEqual("intake_markdown_title_ambiguous", by_path["ambiguous.md"]["code"])
        self.assertEqual("intake_markdown_too_large", by_path["large.md"]["code"])
        self.assertEqual("intake_markdown_not_utf8", by_path["invalid.md"]["code"])
        self.assertEqual("intake_path_unsafe", by_path["symlink.md"]["code"])
        for path, row in by_path.items():
            if row["verdict"] == "refused":
                self.assertIn(path, row["detail"])

    def test_local_parser_returns_h1_title_and_verbatim_body(self) -> None:
        from floati.intake import parse_local_markdown

        title, body = parse_local_markdown(self.queue / "good.md")

        self.assertEqual("Ship the buoy", title)
        self.assertEqual("verbatim body\n## not a title\n", body)

    def test_parser_rejects_unsafe_path_with_typed_refusal(self) -> None:
        from floati.intake import parse_local_markdown

        with self.assertRaises(ProtocolRefusal) as raised:
            parse_local_markdown(self.symlink)

        self.assertEqual("intake_path_unsafe", raised.exception.code)
        self.assertIn("symlink.md", raised.exception.detail)

    def test_scan_cli_is_read_exposed_and_binds_explicit_root_and_directory(self) -> None:
        from floati.cli import _parser

        parser = _parser()
        current = parser
        for command in ("intake", "scan"):
            action = next(
                item
                for item in current._actions
                if isinstance(item, argparse._SubParsersAction)
            )
            current = action.choices[command]

        parsed = parser.parse_args([
            "intake",
            "scan",
            "--root",
            str(Path(self.temporary.name) / "fleet"),
            "--from",
            str(self.queue),
        ])

        self.assertEqual("scan", parsed.intake_command)
        self.assertEqual(str(self.queue), parsed.directory)
        self.assertEqual("read", current.floati_mcp_exposure)


class LocalIntakeAdoptionTests(unittest.TestCase):
    NOW = datetime(2026, 8, 30, 12, 34, 56, 789000, tzinfo=timezone.utc)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = FloatiRoot.open_direct_home(
            Path(self.temporary.name) / "fleet", create=True
        )
        Registry(self.root).register("builder-a", "Codex")
        self.queue = Path(self.temporary.name) / "queue"
        self.queue.mkdir()
        self.source = self.queue / "good.md"
        self.source.write_text(
            "# Ship the buoy\nverbatim body\n", encoding="utf-8"
        )

    def test_adopt_writes_payload_work_item_and_validated_snapshot(self) -> None:
        from floati.intake import (
            INTAKE_SNAPSHOT_RELATIVE,
            adopt_local,
            resolve_snapshot,
        )

        result = adopt_local(
            self.root,
            self.queue,
            "good.md",
            owner="builder-a",
            now=self.NOW,
        )

        snapshot_id = result["snapshot_id"]
        payload_path = self.root.resolve_relative(result["payload_path"])
        self.assertTrue(payload_path.is_file())
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {
                "schema_version",
                "snapshot_id",
                "source_kind",
                "source_id",
                "retrieved_at_testimony",
                "content",
                "metadata",
            },
            set(payload),
        )
        self.assertEqual(snapshot_id, payload["snapshot_id"])
        self.assertEqual("local_markdown", payload["source_kind"])
        self.assertEqual("file:good.md", payload["source_id"])
        self.assertEqual("2026-08-30T12:34:56.789Z", payload["retrieved_at_testimony"])
        self.assertEqual("verbatim body\n", payload["content"])
        self.assertEqual({}, payload["metadata"])

        rows = read_records_snapshot(
            self.root,
            INTAKE_SNAPSHOT_RELATIVE,
            allowed_kinds={"intake_snapshot"},
        )
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual(snapshot_id, row["id"])
        self.assertEqual("local_markdown", row["source_kind"])
        self.assertEqual("file:good.md", row["source_id"])
        self.assertEqual(payload["retrieved_at_testimony"], row["retrieved_at_testimony"])
        self.assertEqual(
            hashlib.sha256(payload["content"].encode("utf-8")).hexdigest(),
            row["content_digest"],
        )
        self.assertEqual(
            hashlib.sha256(b"{}").hexdigest(), row["metadata_digest"]
        )
        self.assertEqual(result["work_item_id"], row["work_item_id"])
        self.assertEqual([], WorkLog(self.root).show(result["work_item_id"])[0]["artifact_bindings"])
        self.assertEqual(payload, resolve_snapshot(self.root, snapshot_id))

    def test_orphan_work_item_is_reported_after_interruption_before_snapshot_row(self) -> None:
        from floati.intake import adopt_local, show_snapshots

        def interrupt(_item: dict[str, object]) -> None:
            raise RuntimeError("controlled interruption")

        with self.assertRaises(RuntimeError):
            adopt_local(
                self.root,
                self.queue,
                "good.md",
                owner="builder-a",
                now=self.NOW,
                after_work_item=interrupt,
            )

        work_items = WorkLog(self.root).show()
        self.assertEqual(1, len(work_items))
        orphan = work_items[0]
        with self.assertRaises(ProtocolRefusal) as raised:
            show_snapshots(self.root)
        self.assertEqual("intake_snapshot_row_absent", raised.exception.code)
        self.assertIn(str(orphan["id"]), raised.exception.detail)
        self.assertIn("intake/v0/intake-snapshot-", raised.exception.detail)

    def test_show_cli_is_read_exposed(self) -> None:
        from floati.cli import _parser

        parser = _parser()
        current = parser
        for command in ("intake", "show"):
            action = next(
                item
                for item in current._actions
                if isinstance(item, argparse._SubParsersAction)
            )
            current = action.choices[command]

        parsed = parser.parse_args([
            "intake", "show", "--root", str(self.root.path)
        ])
        self.assertEqual("show", parsed.intake_command)
        self.assertEqual("read", current.floati_mcp_exposure)

    def test_adopt_cli_requires_from_and_path_and_is_not_mcp_exposed(self) -> None:
        from floati.cli import _parser

        parser = _parser()
        current = parser
        for command in ("intake", "adopt"):
            action = next(
                item
                for item in current._actions
                if isinstance(item, argparse._SubParsersAction)
            )
            current = action.choices[command]

        parsed = parser.parse_args([
            "intake",
            "adopt",
            "--root",
            str(self.root.path),
            "--source",
            "local",
            "--from",
            str(self.queue),
            "--path",
            "good.md",
            "--owner",
            "builder-a",
            "--now",
            "2026-08-30T12:34:56.789Z",
        ])
        self.assertEqual("adopt", parsed.intake_command)
        self.assertEqual("local", parsed.source)
        self.assertEqual("good.md", parsed.relative_path)
        self.assertEqual("never", current.floati_mcp_exposure)

    def test_local_scan_and_adopt_make_zero_socket_calls(self) -> None:
        from floati.intake import adopt_local, scan_directory

        with mock.patch(
            "socket.socket", side_effect=AssertionError("local intake opened a socket")
        ), mock.patch(
            "socket.create_connection",
            side_effect=AssertionError("local intake opened a connection"),
        ):
            scan_directory(self.queue)
            adopt_local(
                self.root,
                self.queue,
                "good.md",
                owner="builder-a",
                now=self.NOW,
            )

    def test_resolve_refuses_a_mutated_payload_then_accepts_exact_restoration(self) -> None:
        from floati.intake import adopt_local, resolve_snapshot

        result = adopt_local(
            self.root,
            self.queue,
            "good.md",
            owner="builder-a",
            now=self.NOW,
        )
        payload_path = self.root.resolve_relative(result["payload_path"])
        original = payload_path.read_bytes()
        mutated = original.replace(b"verbatim body", b"mutated body", 1)
        self.assertNotEqual(original, mutated)
        payload_path.write_bytes(mutated)
        try:
            with self.assertRaises(ProtocolRefusal) as raised:
                resolve_snapshot(self.root, result["snapshot_id"])
            self.assertEqual(
                "intake_snapshot_digest_mismatch", raised.exception.code
            )
        finally:
            payload_path.write_bytes(original)
        self.assertEqual(
            "verbatim body\n",
            resolve_snapshot(self.root, result["snapshot_id"])["content"],
        )

    def test_idempotency_refuses_unchanged_source_and_preserves_changed_history(self) -> None:
        from floati.intake import INTAKE_SNAPSHOT_RELATIVE, adopt_local, resolve_snapshot

        first = adopt_local(
            self.root,
            self.queue,
            "good.md",
            owner="builder-a",
            now=self.NOW,
        )
        first_payload_path = self.root.resolve_relative(first["payload_path"])
        first_payload_bytes = first_payload_path.read_bytes()
        first_rows_bytes = self.root.resolve_relative(INTAKE_SNAPSHOT_RELATIVE).read_bytes()

        with self.assertRaises(ProtocolRefusal) as raised:
            adopt_local(
                self.root,
                self.queue,
                "good.md",
                owner="builder-a",
                now=self.NOW,
            )
        self.assertEqual("intake_snapshot_already_adopted", raised.exception.code)
        self.assertIn(str(first["snapshot_id"]), raised.exception.detail)
        self.assertEqual(first_rows_bytes, self.root.resolve_relative(INTAKE_SNAPSHOT_RELATIVE).read_bytes())

        self.source.write_text(
            "# Ship the buoy\nchanged body\n", encoding="utf-8"
        )
        second = adopt_local(
            self.root,
            self.queue,
            "good.md",
            owner="builder-a",
            now=self.NOW,
        )
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertNotEqual(first["work_item_id"], second["work_item_id"])
        rows = read_records_snapshot(
            self.root,
            INTAKE_SNAPSHOT_RELATIVE,
            allowed_kinds={"intake_snapshot"},
        )
        self.assertEqual(2, len(rows))
        self.assertEqual(first_payload_bytes, first_payload_path.read_bytes())
        self.assertEqual("verbatim body\n", resolve_snapshot(self.root, first["snapshot_id"])["content"])
        self.assertEqual("changed body\n", resolve_snapshot(self.root, second["snapshot_id"])["content"])


if __name__ == "__main__":
    unittest.main()

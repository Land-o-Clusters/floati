from __future__ import annotations

import argparse
import hashlib
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from floati.errors import ProtocolRefusal
from floati.purge import PurgeWriter, register_cli


class PurgeWriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.trash = self.base / "Trash"
        self.trash.mkdir()
        trash_patch = patch("floati.purge._trash_dir", return_value=self.trash)
        trash_patch.start()
        self.addCleanup(trash_patch.stop)

    @staticmethod
    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def root(self, name: str = "records") -> Path:
        path = self.base / name
        path.mkdir()
        return path

    def test_explicit_roots_are_required_and_files_are_foreign(self) -> None:
        with self.assertRaises(ProtocolRefusal) as missing:
            PurgeWriter([]).run()
        self.assertEqual("purge_roots_required", missing.exception.code)

        foreign_file = self.base / "foreign.txt"
        foreign_file.write_text("keep\n", encoding="utf-8")
        valid_root = self.root("valid")
        (valid_root / "owned.txt").write_text("owned\n", encoding="utf-8")

        with self.assertRaises(ProtocolRefusal) as foreign:
            PurgeWriter([foreign_file, valid_root]).run()
        self.assertEqual("purge_foreign_file", foreign.exception.code)
        self.assertTrue(foreign_file.is_file())
        self.assertTrue(valid_root.is_dir())

    def test_missing_symlink_and_overlapping_roots_refuse_before_mutation(self) -> None:
        missing = self.base / "missing"
        with self.assertRaises(ProtocolRefusal) as missing_error:
            PurgeWriter([missing]).run()
        self.assertEqual("purge_root_missing", missing_error.exception.code)

        real = self.root("real")
        link = self.base / "root-link"
        link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ProtocolRefusal) as symlink_error:
            PurgeWriter([link]).run()
        self.assertEqual("purge_root_symlink", symlink_error.exception.code)
        self.assertTrue(real.is_dir())

        parent = self.root("parent")
        child = parent / "child"
        child.mkdir()
        (parent / "keep.txt").write_text("keep\n", encoding="utf-8")
        with self.assertRaises(ProtocolRefusal) as overlap:
            PurgeWriter([parent, child]).run()
        self.assertEqual("purge_root_overlap", overlap.exception.code)
        self.assertTrue(parent.is_dir())
        self.assertTrue(child.is_dir())

    def test_symlink_or_special_descendant_is_foreign_and_blocks_all_roots(self) -> None:
        unsafe = self.root("unsafe")
        outside = self.base / "outside.txt"
        outside.write_text("precious\n", encoding="utf-8")
        (unsafe / "foreign-link").symlink_to(outside)
        safe = self.root("safe")
        (safe / "safe.txt").write_text("safe\n", encoding="utf-8")

        with self.assertRaises(ProtocolRefusal) as raised:
            PurgeWriter([unsafe, safe]).run()
        self.assertEqual("purge_foreign_entry", raised.exception.code)
        self.assertTrue(unsafe.is_dir())
        self.assertTrue(safe.is_dir())
        self.assertTrue(outside.is_file())
        self.assertEqual([], list(self.trash.iterdir()))

        fifo_root = self.root("fifo")
        fifo = fifo_root / "foreign-fifo"
        try:
            os.mkfifo(fifo)
        except (AttributeError, NotImplementedError, OSError):
            self.skipTest("FIFO fixtures are unavailable on this host")
        with self.assertRaises(ProtocolRefusal) as fifo_error:
            PurgeWriter([fifo_root]).run()
        self.assertEqual("purge_foreign_entry", fifo_error.exception.code)
        self.assertTrue(stat.S_ISFIFO(fifo.stat().st_mode))

    def test_dry_run_preview_is_digest_bound_and_does_not_move_anything(self) -> None:
        root = self.root()
        first = root / "a.txt"
        second = root / "nested" / "b.txt"
        second.parent.mkdir()
        first.write_bytes(b"first\n")
        second.write_bytes(b"second\n")
        before = {
            path.relative_to(self.base): (path.stat().st_ino, path.read_bytes())
            for path in (first, second)
        }

        result = PurgeWriter(
            [root],
            dry_run=True,
            timestamp="20260828T000000Z",
        ).run()

        self.assertTrue(result["dry_run"])
        self.assertTrue(result["trash_only"])
        self.assertEqual(1, result["root_count"])
        self.assertEqual(2, result["file_count"])
        self.assertEqual(2, len(result["file_receipts"]))
        self.assertTrue(result["preview"].strip())
        self.assertEqual(
            [self.digest(b"first\n"), self.digest(b"second\n")],
            [receipt["sha256"] for receipt in result["file_receipts"]],
        )
        self.assertEqual(
            before,
            {
                path.relative_to(self.base): (path.stat().st_ino, path.read_bytes())
                for path in (first, second)
            },
        )
        self.assertTrue(root.is_dir())
        self.assertEqual([], list(self.trash.iterdir()))

    def test_normal_run_moves_the_root_and_returns_one_receipt_per_file(self) -> None:
        root = self.root("records")
        first = root / "a.txt"
        second = root / "nested" / "b.txt"
        second.parent.mkdir()
        first.write_bytes(b"first\n")
        second.write_bytes(b"second\n")

        result = PurgeWriter(
            [root],
            timestamp="20260828T000000Z",
        ).run()

        self.assertFalse(root.exists())
        self.assertEqual(1, result["root_count"])
        self.assertEqual(2, result["file_count"])
        self.assertEqual(2, result["trashed_count"])
        root_receipt = result["root_receipts"][0]
        moved_root = Path(root_receipt["trash"])
        self.assertEqual(
            self.trash.resolve() / "floati-records-20260828T000000Z", moved_root
        )
        self.assertEqual(b"first\n", (moved_root / "a.txt").read_bytes())
        self.assertEqual(b"second\n", (moved_root / "nested" / "b.txt").read_bytes())
        for receipt in result["file_receipts"]:
            original = Path(receipt["original"])
            destination = Path(receipt["trash"])
            self.assertEqual(str(root.resolve()), receipt["root"])
            self.assertEqual(receipt["sha256"], self.digest(destination.read_bytes()))
            self.assertEqual(receipt["size"], destination.stat().st_size)
            self.assertFalse(original.exists())

    def test_collision_gets_a_suffix_without_overwriting_existing_trash(self) -> None:
        root = self.root("records")
        (root / "note.txt").write_text("new\n", encoding="utf-8")
        occupied = self.trash / "floati-records-20260828T000000Z"
        occupied.mkdir()
        (occupied / "old.txt").write_text("old\n", encoding="utf-8")

        result = PurgeWriter(
            [root],
            timestamp="20260828T000000Z",
        ).run()

        moved = Path(result["root_receipts"][0]["trash"])
        self.assertEqual(
            self.trash.resolve() / "floati-records-20260828T000000Z-1", moved
        )
        self.assertEqual("old\n", (occupied / "old.txt").read_text(encoding="utf-8"))
        self.assertEqual("new\n", (moved / "note.txt").read_text(encoding="utf-8"))

    def test_preview_plan_refuses_same_size_rewrite_before_any_root_moves(self) -> None:
        first = self.root("first")
        second = self.root("second")
        first_file = first / "a.txt"
        second_file = second / "b.txt"
        first_file.write_bytes(b"aaaa\n")
        second_file.write_bytes(b"bbbb\n")
        writer = PurgeWriter(
            [first, second],
            timestamp="20260828T000000Z",
        )
        plan = writer.plan()
        first_file.write_bytes(b"zzzz\n")

        with self.assertRaises(ProtocolRefusal) as raised:
            writer.execute(plan)
        self.assertEqual("purge_identity_changed", raised.exception.code)
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertEqual([], list(self.trash.iterdir()))

    def test_trash_path_is_fixed_and_source_contains_no_delete_primitive(self) -> None:
        source = Path(__file__).parents[1] / "floati" / "purge.py"
        self.assertNotIn("unlink(", source.read_text(encoding="utf-8"))
        self.assertNotIn("rmtree(", source.read_text(encoding="utf-8"))


class PurgeCliRegistrationTests(unittest.TestCase):
    def test_registration_exposes_repeatable_roots_and_dry_run_only(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        register_cli(commands)

        parsed = parser.parse_args(
            ["purge", "--root", "/tmp/one", "--preserved-root", "/tmp/two", "--dry-run"]
        )

        self.assertEqual("purge", parsed.command)
        self.assertEqual(["/tmp/one", "/tmp/two"], parsed.roots)
        self.assertTrue(parsed.dry_run)
        with self.assertRaises(SystemExit):
            parser.parse_args(["purge", "--root", "/tmp/one", "--purge"])


if __name__ == "__main__":
    unittest.main()

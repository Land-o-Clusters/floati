from __future__ import annotations

import argparse
import errno
import hashlib
import os
import stat
import tempfile
import unittest
import urllib.parse
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from floati import purge
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


class _RecordingRename:
    """A stand-in for one platform's libc exclusive-rename symbol.

    It records the exact ``(dirfd, source, dirfd, destination, flag)`` tuple it
    was handed and then performs the rename, so a test can prove WHICH
    primitive was selected and WHICH flag travelled with it.  Neither the
    syscall nor its kernel semantics are under test here — that is the harbor's
    ubuntu runner's job; what is under test is the pairing.
    """

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, source_dir, source, destination_dir, destination, flag):
        self.calls.append((source_dir, source, destination_dir, destination, flag))
        os.rename(
            os.fsdecode(source),
            os.fsdecode(destination),
            src_dir_fd=None if source_dir < 0 else source_dir,
            dst_dir_fd=None if destination_dir < 0 else destination_dir,
        )
        return 0


class ExclusiveRenamePlatformPairingTests(unittest.TestCase):
    """The (function, dirfd, flag) tuple is bound per DECLARED platform."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()

    def test_the_rename_tuple_is_selected_by_declared_platform_never_by_probing(
        self,
    ) -> None:
        expectations = (
            ("darwin", -2, 0x00000004),
            ("linux", -100, 0x00000001),
        )
        for platform_name, expected_dir_fd, expected_flag in expectations:
            with self.subTest(platform=platform_name):
                darwin = _RecordingRename()
                linux = _RecordingRename()
                source = self.base / f"{platform_name}-source"
                source.write_text("payload\n", encoding="utf-8")
                destination = self.base / f"{platform_name}-destination"

                with patch("sys.platform", platform_name), patch.object(
                    purge, "_RENAMEATX_NP", darwin
                ), patch.object(purge, "_RENAMEAT2", linux, create=True):
                    purge._rename_exclusive(source, destination)

                chosen = darwin if platform_name == "darwin" else linux
                other = linux if platform_name == "darwin" else darwin
                self.assertEqual([], other.calls)
                self.assertEqual(1, len(chosen.calls))
                observed = chosen.calls[0]
                self.assertEqual(expected_dir_fd, observed[0])
                self.assertEqual(expected_dir_fd, observed[2])
                self.assertEqual(expected_flag, observed[4])
                self.assertTrue(destination.is_file())
                self.assertFalse(source.exists())

    def test_a_host_without_the_declared_platforms_primitive_refuses_unmoved(
        self,
    ) -> None:
        source = self.base / "source"
        source.write_text("payload\n", encoding="utf-8")
        destination = self.base / "destination"

        with patch("sys.platform", "linux"), patch.object(
            purge, "_RENAMEATX_NP", _RecordingRename()
        ), patch.object(purge, "_RENAMEAT2", None, create=True):
            with self.assertRaises(OSError) as raised:
                purge._rename_exclusive(source, destination)

        self.assertEqual(errno.ENOTSUP, raised.exception.errno)
        self.assertTrue(source.is_file())
        self.assertFalse(destination.exists())


class _TrashBindingCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        home_patch = patch("floati.purge._account_home", return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("XDG_DATA_HOME", None)

    def freedesktop(self, root: Path | None = None) -> Path:
        root = root or self.home / ".local" / "share" / "Trash"
        (root / "files").mkdir(parents=True)
        (root / "info").mkdir(parents=True)
        return root

    def root(self, name: str = "records") -> Path:
        path = self.home / name
        path.mkdir()
        (path / "record.txt").write_text(f"{name}\n", encoding="utf-8")
        return path

    def purge_under(self, platform_name: str, roots, stamp: str):
        primitive = _RecordingRename()
        symbol = "_RENAMEATX_NP" if platform_name == "darwin" else "_RENAMEAT2"
        with patch("sys.platform", platform_name), patch.object(
            purge, symbol, primitive, create=True
        ):
            return PurgeWriter(roots, timestamp=stamp).run()

    def trashinfo_fields(self, path: Path) -> dict:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        fields = {"__header__": lines[0]}
        for line in lines[1:]:
            if not line:
                continue
            key, _, value = line.partition("=")
            fields[key] = value
        return fields


class LinuxFreedesktopTrashTests(_TrashBindingCase):
    def test_linux_trash_is_the_freedesktop_files_directory(self) -> None:
        root = self.freedesktop()
        with patch("sys.platform", "linux"):
            self.assertEqual(root / "files", purge._trash_dir())
            self.assertEqual(root / "info", purge._trash_info_dir(root / "files"))
        with patch("sys.platform", "darwin"):
            self.assertIsNone(purge._trash_info_dir(root / "files"))
        self.assertFalse((self.home / ".Trash").exists())

    def test_absent_freedesktop_trash_refuses_with_a_remedy_naming_it(self) -> None:
        files = self.home / ".local" / "share" / "Trash" / "files"
        info = self.home / ".local" / "share" / "Trash" / "info"

        with patch("sys.platform", "linux"):
            with self.assertRaises(ProtocolRefusal) as raised:
                purge._trash_dir()

        self.assertEqual("purge_trash_unavailable", raised.exception.code)
        self.assertIsNotNone(raised.exception.remedy)
        self.assertIn(str(files), raised.exception.remedy)
        self.assertIn(str(info), raised.exception.remedy)
        self.assertFalse(files.exists())

    def test_a_half_built_trash_is_still_unavailable_and_names_the_missing_half(
        self,
    ) -> None:
        root = self.home / ".local" / "share" / "Trash"
        (root / "files").mkdir(parents=True)

        with patch("sys.platform", "linux"):
            with self.assertRaises(ProtocolRefusal) as raised:
                purge._trash_dir()

        self.assertEqual("purge_trash_unavailable", raised.exception.code)
        self.assertIn(str(root / "info"), raised.exception.detail)
        self.assertNotIn(str(root / "files"), raised.exception.detail)
        self.assertIsNotNone(raised.exception.remedy)

    def test_xdg_data_home_inside_the_account_home_is_honoured(self) -> None:
        declared = self.home / "declared-data-home"
        self.freedesktop(declared / "Trash")

        with patch("sys.platform", "linux"), patch.dict(
            os.environ, {"XDG_DATA_HOME": str(declared)}
        ):
            self.assertEqual(declared / "Trash" / "files", purge._trash_dir())

    def test_xdg_data_home_outside_the_account_home_cannot_steer_the_trash(
        self,
    ) -> None:
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        attacker = Path(elsewhere.name).resolve()
        self.freedesktop(attacker / "Trash")
        default = self.freedesktop()

        with patch("sys.platform", "linux"), patch.dict(
            os.environ, {"XDG_DATA_HOME": str(attacker)}
        ):
            resolved = purge._trash_dir()

        self.assertEqual(default / "files", resolved)
        self.assertEqual([], list((attacker / "Trash" / "files").iterdir()))

    def test_a_trashinfo_record_is_written_beside_each_moved_root(self) -> None:
        trash = self.freedesktop()
        root = self.root("records")

        self.purge_under("linux", [root], "20260828T000000Z")

        name = "floati-records-20260828T000000Z"
        moved = trash / "files" / name
        self.assertTrue(moved.is_dir())
        self.assertFalse(root.exists())

        info = trash / "info" / f"{name}.trashinfo"
        self.assertTrue(info.is_file())
        fields = self.trashinfo_fields(info)
        self.assertEqual("[Trash Info]", fields["__header__"])
        self.assertEqual({"__header__", "Path", "DeletionDate"}, set(fields))
        self.assertEqual(str(root), urllib.parse.unquote(fields["Path"]))
        datetime.strptime(fields["DeletionDate"], "%Y-%m-%dT%H:%M:%S")

    def test_a_stale_trashinfo_name_is_not_reused_by_a_later_purge(self) -> None:
        trash = self.freedesktop()
        name = "floati-records-20260828T000000Z"
        (trash / "info" / f"{name}.trashinfo").write_text(
            "[Trash Info]\nPath=/gone\nDeletionDate=2026-08-28T00:00:00\n",
            encoding="utf-8",
        )
        root = self.root("records")

        self.purge_under("linux", [root], "20260828T000000Z")

        self.assertFalse((trash / "files" / name).exists())
        self.assertTrue((trash / "files" / f"{name}-1").is_dir())
        self.assertTrue((trash / "info" / f"{name}-1.trashinfo").is_file())


class MacOSTrashBindingControlTests(_TrashBindingCase):
    """Control: the macOS binding and its receipts are unchanged."""

    def test_macos_binding_is_the_account_dot_trash_and_writes_no_trashinfo(
        self,
    ) -> None:
        trash = self.home / ".Trash"
        trash.mkdir()
        root = self.root("records")

        with patch("sys.platform", "darwin"):
            self.assertEqual(trash, purge._trash_dir())

        evidence = self.purge_under("darwin", [root], "20260828T000000Z")

        moved = trash / "floati-records-20260828T000000Z"
        self.assertTrue(moved.is_dir())
        self.assertFalse(root.exists())
        self.assertTrue(evidence["trash_only"])
        self.assertEqual(str(trash), evidence["trash_dir"])
        self.assertEqual(1, evidence["moved_root_count"])
        self.assertEqual(1, evidence["trashed_count"])
        self.assertEqual(
            [{"original": str(root), "trash": str(moved), "status": "trashed"}],
            evidence["root_receipts"],
        )
        self.assertEqual(
            [str(moved / "record.txt")],
            [receipt["trash"] for receipt in evidence["file_receipts"]],
        )
        self.assertEqual([], sorted(self.home.rglob("*.trashinfo")))
        self.assertFalse((self.home / ".local").exists())

    def test_macos_refuses_an_absent_dot_trash_with_a_remedy_naming_it(self) -> None:
        with patch("sys.platform", "darwin"):
            with self.assertRaises(ProtocolRefusal) as raised:
                purge._trash_dir()

        self.assertEqual("purge_trash_unavailable", raised.exception.code)
        self.assertIn(str(self.home / ".Trash"), raised.exception.detail)
        self.assertIsNotNone(raised.exception.remedy)


class PurgeCliRegistrationTests(unittest.TestCase):
    def test_registration_exposes_repeatable_roots_and_dry_run_only(self) -> None:
        parser = argparse.ArgumentParser()
        commands = parser.add_subparsers(dest="command", required=True)
        register_cli(commands)

        parsed = parser.parse_args(
            ["purge", "--root", "\x2ftmp/one", "--preserved-root", "\x2ftmp/two", "--dry-run"]
        )

        self.assertEqual("purge", parsed.command)
        self.assertEqual(["\x2ftmp/one", "\x2ftmp/two"], parsed.roots)
        self.assertTrue(parsed.dry_run)
        with self.assertRaises(SystemExit):
            parser.parse_args(["purge", "--root", "\x2ftmp/one", "--purge"])


if __name__ == "__main__":
    unittest.main()

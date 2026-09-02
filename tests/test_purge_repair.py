from __future__ import annotations

import argparse
import errno
import inspect
import os
import pwd
import sys
import tempfile
import unittest
import unicodedata
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from floati import purge
from floati.errors import DurabilityFailure, ProtocolRefusal


_REAL_TRASH_DIR = purge._trash_dir


class PurgeSevenFindingRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.trash = self.base / "Trash"
        self.trash.mkdir()
        trash_patch = patch("floati.purge._trash_dir", return_value=self.trash)
        trash_patch.start()
        self.addCleanup(trash_patch.stop)

    def root(self, name: str) -> Path:
        root = self.base / name
        root.mkdir()
        (root / "record.txt").write_text(f"{name}\n", encoding="utf-8")
        return root

    def test_symlinked_ancestor_refuses_and_names_lexical_and_resolved_roots(self) -> None:
        real_parent = self.base / "real"
        real_parent.mkdir()
        real_root = real_parent / "records"
        real_root.mkdir()
        (real_root / "record.txt").write_text("precious\n", encoding="utf-8")
        alias_parent = self.base / "alias"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
        lexical_root = alias_parent / "records"

        with self.assertRaises(ProtocolRefusal) as raised:
            purge.PurgeWriter([lexical_root]).plan()

        self.assertEqual("purge_root_symlink_ancestor", raised.exception.code)
        self.assertIn(str(lexical_root), raised.exception.detail)
        self.assertIn(str(real_root.resolve()), raised.exception.detail)
        self.assertTrue(real_root.is_dir())
        self.assertEqual([], list(self.trash.iterdir()))

    def test_public_surfaces_cannot_select_an_alternate_trash_directory(self) -> None:
        self.assertNotIn("trash_dir", inspect.signature(purge.PurgeWriter).parameters)
        self.assertNotIn("trash_dir", inspect.signature(purge.plan).parameters)
        root = self.root("records")

        with self.assertRaises(TypeError):
            purge.PurgeWriter([root], trash_dir=self.base / "attacker")
        with self.assertRaises(TypeError):
            purge.plan([root], trash_dir=self.base / "attacker")

        self.assertTrue(root.is_dir())
        self.assertFalse((self.base / "attacker").exists())

    def test_fixed_trash_authority_ignores_caller_home_environment(self) -> None:
        # The claim is the fixed authority: the caller's HOME (and XDG_DATA_HOME)
        # may not steer the Trash.  WHICH directory the account's Trash is, is
        # the platform's answer -- macOS ~/.Trash, elsewhere the freedesktop
        # files/ half -- so the expectation is parametrised by platform and the
        # claim is asserted on BOTH answers, including the refusal.
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        alternate_home = self.base / "alternate-home"
        (alternate_home / ".Trash").mkdir(parents=True)
        (alternate_home / ".local" / "share" / "Trash" / "files").mkdir(parents=True)
        (alternate_home / ".local" / "share" / "Trash" / "info").mkdir(parents=True)

        if sys.platform == "darwin":
            expected = account_home / ".Trash"
            refused = alternate_home / ".Trash"
        else:
            expected = account_home / ".local" / "share" / "Trash" / "files"
            refused = alternate_home / ".local" / "share" / "Trash" / "files"

        raised = None
        resolved = None
        with patch.dict(os.environ, {"HOME": str(alternate_home)}):
            os.environ.pop("XDG_DATA_HOME", None)
            if expected.is_dir():
                resolved = _REAL_TRASH_DIR()
            else:
                with self.assertRaises(ProtocolRefusal) as capture:
                    _REAL_TRASH_DIR()
                raised = capture.exception

        if resolved is not None:
            self.assertEqual(expected.resolve(), resolved)
            self.assertNotEqual(refused.resolve(), resolved)
            return

        # This host has no Trash of its own.  The answer is still the account's
        # directory -- named in a typed refusal with an actionable remedy, and
        # never the caller's.
        self.assertEqual("purge_trash_unavailable", raised.code)
        self.assertIsNotNone(raised.remedy)
        self.assertIn(str(expected), raised.remedy)
        self.assertNotIn(str(alternate_home), raised.detail)
        self.assertNotIn(str(alternate_home), raised.remedy)

    def test_tilde_root_is_not_expanded_through_caller_environment(self) -> None:
        alternate_home = self.base / "alternate-home"
        records = alternate_home / "records"
        records.mkdir(parents=True)

        with patch.dict(os.environ, {"HOME": str(alternate_home)}):
            with self.assertRaises(ProtocolRefusal) as raised:
                purge.PurgeWriter(["~/records"]).plan()

        self.assertEqual("purge_root_absolute_required", raised.exception.code)
        self.assertTrue(records.is_dir())

    def test_forged_plan_destination_outside_fixed_trash_refuses_before_move(self) -> None:
        root = self.root("records")
        writer = purge.PurgeWriter([root], timestamp="20260828T000000Z")
        plan = writer.plan()
        redirected = self.base / "redirected-root"
        forged_root = replace(plan.roots[0], trash=redirected)
        forged_files = tuple(
            replace(file, trash=redirected / file.relative) for file in plan.files
        )
        forged = replace(plan, roots=(forged_root,), files=forged_files)

        with self.assertRaises(ProtocolRefusal) as raised:
            writer.execute(forged)

        self.assertEqual("purge_plan_invalid", raised.exception.code)
        self.assertTrue(root.is_dir())
        self.assertFalse(redirected.exists())
        self.assertEqual([], list(self.trash.iterdir()))

    def test_cross_device_preflight_refuses_all_roots_before_first_move(self) -> None:
        first = self.root("first")
        second = self.root("second")
        writer = purge.PurgeWriter([first, second], timestamp="20260828T000000Z")
        plan = writer.plan()
        forged = replace(
            plan,
            roots=(plan.roots[0], replace(plan.roots[1], device=plan.roots[1].device + 1)),
        )

        with patch("floati.purge._rename_exclusive_at", create=True) as exclusive, patch(
            "floati.purge.os.rename"
        ) as rename:
            with self.assertRaises(ProtocolRefusal) as raised:
                writer.execute(forged)

        self.assertEqual("purge_cross_device", raised.exception.code)
        exclusive.assert_not_called()
        rename.assert_not_called()
        self.assertTrue(first.is_dir())
        self.assertTrue(second.is_dir())
        self.assertEqual([], list(self.trash.iterdir()))

    def test_failed_rollback_emits_typed_stranded_root_receipt(self) -> None:
        first = self.root("first")
        second = self.root("second")
        writer = purge.PurgeWriter([first, second], timestamp="20260828T000000Z")
        plan = writer.plan()
        real_rename = os.rename

        def exclusive_move(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            if source_name == second.name:
                raise OSError(errno.EIO, "forced second move failure")
            if source_name == plan.roots[0].trash.name:
                raise OSError(errno.EIO, "forced rollback failure")
            real_rename(
                source_name,
                destination_name,
                src_dir_fd=source_dir_fd,
                dst_dir_fd=destination_dir_fd,
            )

        with patch(
            "floati.purge._rename_exclusive_at",
            side_effect=exclusive_move,
            create=True,
        ):
            with self.assertRaises((DurabilityFailure, ProtocolRefusal)) as raised:
                writer.execute(plan)

        self.assertIsInstance(raised.exception, DurabilityFailure)
        self.assertEqual("purge_rollback_failed", raised.exception.code)
        evidence = raised.exception.evidence
        self.assertEqual("purge_rollback_failed", evidence["reason_code"])
        self.assertEqual(
            [
                {
                    "kind": "stranded-root-receipt",
                    "original": str(first),
                    "trash": str(plan.roots[0].trash),
                    "status": "stranded",
                }
            ],
            evidence["stranded_root_receipts"],
        )
        self.assertFalse(first.exists())
        self.assertTrue(plan.roots[0].trash.is_dir())
        self.assertTrue(second.is_dir())

    def test_rollback_refuses_when_neither_original_nor_trash_path_proves_restoration(self) -> None:
        first = self.root("first")
        second = self.root("second")
        quarantine = self.base / "unproven-location"
        writer = purge.PurgeWriter([first, second], timestamp="20260828T000000Z")
        plan = writer.plan()
        real_rename = os.rename

        def disappearing_move(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            if source_name == second.name:
                real_rename(plan.roots[0].trash, quarantine)
                raise OSError(errno.EIO, "forced second move failure after displacement")
            real_rename(
                source_name,
                destination_name,
                src_dir_fd=source_dir_fd,
                dst_dir_fd=destination_dir_fd,
            )

        with patch("floati.purge._rename_exclusive_at", side_effect=disappearing_move):
            with self.assertRaises((DurabilityFailure, ProtocolRefusal)) as raised:
                writer.execute(plan)

        self.assertIsInstance(raised.exception, DurabilityFailure)
        self.assertEqual("purge_rollback_failed", raised.exception.code)
        self.assertEqual(
            [
                {
                    "kind": "stranded-root-receipt",
                    "original": str(first),
                    "trash": str(plan.roots[0].trash),
                    "status": "stranded",
                }
            ],
            raised.exception.evidence["stranded_root_receipts"],
        )
        self.assertFalse(first.exists())
        self.assertFalse(plan.roots[0].trash.exists())
        self.assertTrue(quarantine.is_dir())
        self.assertTrue(second.is_dir())

    def test_rollback_inspection_failure_still_emits_stranded_receipt(self) -> None:
        first = self.root("first")
        second = self.root("second")
        writer = purge.PurgeWriter([first, second], timestamp="20260828T000000Z")
        plan = writer.plan()
        real_rename_at = purge._rename_exclusive_at
        real_stat_at = purge._stat_at
        transaction_failed = False

        def fail_second_move(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal transaction_failed
            if source_name == second.name:
                transaction_failed = True
                raise OSError(errno.EIO, "forced second move failure")
            real_rename_at(
                source_dir_fd,
                source_name,
                destination_dir_fd,
                destination_name,
            )

        def fail_rollback_inspection(directory_fd: int, name: str):
            if transaction_failed and name == plan.roots[0].trash.name:
                raise OSError(errno.EIO, "forced rollback inspection failure")
            return real_stat_at(directory_fd, name)

        with patch(
            "floati.purge._rename_exclusive_at",
            side_effect=fail_second_move,
        ), patch("floati.purge._stat_at", side_effect=fail_rollback_inspection):
            with self.assertRaises(DurabilityFailure) as raised:
                writer.execute(plan)

        self.assertEqual("purge_rollback_failed", raised.exception.code)
        self.assertEqual(
            [
                {
                    "kind": "stranded-root-receipt",
                    "original": str(first),
                    "trash": str(plan.roots[0].trash),
                    "status": "stranded",
                }
            ],
            raised.exception.evidence["stranded_root_receipts"],
        )

    def test_rollback_never_moves_replacement_target_to_original_path(self) -> None:
        first = self.root("first")
        second = self.root("second")
        attacker = self.root("attacker")
        quarantine = self.base / "quarantined-original"
        writer = purge.PurgeWriter([first, second], timestamp="20260828T000000Z")
        plan = writer.plan()
        real_rename_at = purge._rename_exclusive_at

        def replace_target_then_fail(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            if source_name == second.name:
                os.rename(plan.roots[0].trash, quarantine)
                os.rename(attacker, plan.roots[0].trash)
                raise OSError(errno.EIO, "forced second move failure after replacement")
            real_rename_at(
                source_dir_fd,
                source_name,
                destination_dir_fd,
                destination_name,
            )

        with patch(
            "floati.purge._rename_exclusive_at",
            side_effect=replace_target_then_fail,
        ):
            with self.assertRaises(DurabilityFailure) as raised:
                writer.execute(plan)

        self.assertEqual("purge_rollback_failed", raised.exception.code)
        self.assertFalse(first.exists())
        self.assertTrue(quarantine.is_dir())
        self.assertEqual(
            "attacker\n",
            (plan.roots[0].trash / "record.txt").read_text(encoding="utf-8"),
        )

    def test_handler_preserves_stranded_receipts_as_degraded_evidence(self) -> None:
        first = self.root("first")
        second = self.root("second")
        real_rename = os.rename
        calls = 0

        def failed_transaction(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError(errno.EIO, "forced second move failure")
            if calls == 3:
                raise OSError(errno.EIO, "forced rollback failure")
            real_rename(
                source_name,
                destination_name,
                src_dir_fd=source_dir_fd,
                dst_dir_fd=destination_dir_fd,
            )

        args = argparse.Namespace(roots=[str(first), str(second)], dry_run=False)
        with patch("floati.purge._rename_exclusive_at", side_effect=failed_transaction):
            try:
                status, evidence, exit_code = purge._handle(args)
            except ProtocolRefusal as exc:
                self.fail(f"post-mutation evidence leaked as refusal: {exc}")

        self.assertEqual("degraded", status)
        self.assertEqual(35, exit_code)
        self.assertEqual("purge_rollback_failed", evidence["code"])
        self.assertEqual(1, len(evidence["stranded_root_receipts"]))

    def test_source_swap_at_rename_boundary_cannot_yield_success_receipts(self) -> None:
        root = self.root("records")
        original_away = self.base / "original-away"
        replacement = self.root("replacement")
        writer = purge.PurgeWriter([root], timestamp="20260828T000000Z")
        plan = writer.plan()
        original_open = writer._open_verified_root_at_move

        def swap_after_open(planned_root, expected):
            descriptor = original_open(planned_root, expected)
            os.rename(root, original_away)
            os.rename(replacement, root)
            return descriptor

        with patch.object(
            writer,
            "_open_verified_root_at_move",
            side_effect=swap_after_open,
        ):
            with self.assertRaises(ProtocolRefusal) as raised:
                writer.execute(plan)

        self.assertEqual("purge_identity_changed", raised.exception.code)
        self.assertTrue(original_away.is_dir())
        self.assertTrue(root.is_dir())
        self.assertFalse(plan.roots[0].trash.exists())

    def test_trash_parent_replacement_after_verify_refuses_and_restores_root(self) -> None:
        root = self.root("records")
        relocated_trash = self.base / "relocated-trash"
        writer = purge.PurgeWriter([root], timestamp="20260828T000000Z")
        plan = writer.plan()
        original_open = writer._open_verified_root_at_move

        def replace_trash_after_open(planned_root, expected):
            descriptor = original_open(planned_root, expected)
            os.rename(self.trash, relocated_trash)
            self.trash.mkdir()
            return descriptor

        with patch.object(
            writer,
            "_open_verified_root_at_move",
            side_effect=replace_trash_after_open,
        ):
            with self.assertRaises(ProtocolRefusal) as raised:
                writer.execute(plan)

        self.assertEqual("purge_trash_changed", raised.exception.code)
        self.assertTrue(root.is_dir())
        self.assertEqual([], list(self.trash.iterdir()))
        self.assertEqual([], list(relocated_trash.iterdir()))

    def test_source_parent_replacement_during_rename_cannot_yield_success(self) -> None:
        source_parent = self.base / "source-parent"
        source_parent.mkdir()
        root = source_parent / "records"
        root.mkdir()
        (root / "record.txt").write_text("records\n", encoding="utf-8")
        relocated_parent = self.base / "relocated-source-parent"
        writer = purge.PurgeWriter([root], timestamp="20260828T000000Z")
        plan = writer.plan()
        real_rename_at = purge._rename_exclusive_at
        replaced = False

        def replace_parent_then_move(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal replaced
            if not replaced:
                replaced = True
                os.rename(source_parent, relocated_parent)
                source_parent.mkdir()
            real_rename_at(
                source_dir_fd,
                source_name,
                destination_dir_fd,
                destination_name,
            )

        with patch(
            "floati.purge._rename_exclusive_at",
            side_effect=replace_parent_then_move,
        ):
            with self.assertRaises(DurabilityFailure) as raised:
                writer.execute(plan)

        self.assertEqual("purge_rollback_failed", raised.exception.code)
        self.assertEqual(1, len(raised.exception.evidence["stranded_root_receipts"]))
        self.assertTrue((relocated_parent / "records").is_dir())
        self.assertFalse(plan.roots[0].trash.exists())

    def test_file_change_during_rename_yields_degraded_restoration_evidence(self) -> None:
        root = self.root("records")
        writer = purge.PurgeWriter([root], timestamp="20260828T000000Z")
        plan = writer.plan()
        real_rename_at = purge._rename_exclusive_at
        changed = False

        def change_after_move(
            source_dir_fd: int,
            source_name: str,
            destination_dir_fd: int,
            destination_name: str,
        ) -> None:
            nonlocal changed
            real_rename_at(
                source_dir_fd,
                source_name,
                destination_dir_fd,
                destination_name,
            )
            if not changed:
                changed = True
                (plan.roots[0].trash / "record.txt").write_text(
                    "intruder\n",
                    encoding="utf-8",
                )

        with patch("floati.purge._rename_exclusive_at", side_effect=change_after_move):
            with self.assertRaises(DurabilityFailure) as raised:
                writer.execute(plan)

        self.assertEqual("purge_move_failed", raised.exception.code)
        self.assertEqual([], raised.exception.evidence["stranded_root_receipts"])
        self.assertEqual(1, len(raised.exception.evidence["restored_root_receipts"]))
        self.assertEqual("intruder\n", (root / "record.txt").read_text(encoding="utf-8"))
        self.assertFalse(plan.roots[0].trash.exists())

    def test_file_swap_after_bulk_verify_refuses_at_move_boundary(self) -> None:
        root = self.root("records")
        record = root / "record.txt"
        writer = purge.PurgeWriter([root], timestamp="20260828T000000Z")
        original_scan = purge._scan_root
        scans = 0

        def racing_scan(scanned_root: Path):
            nonlocal scans
            observation = original_scan(scanned_root)
            scans += 1
            if scans == 2:
                record.write_text("intruder\n", encoding="utf-8")
            return observation

        with patch("floati.purge._scan_root", side_effect=racing_scan):
            plan = writer.plan()
            with self.assertRaises(ProtocolRefusal) as raised:
                writer.execute(plan)

        self.assertEqual("purge_identity_changed", raised.exception.code)
        self.assertTrue(root.is_dir())
        self.assertEqual("intruder\n", record.read_text(encoding="utf-8"))
        self.assertEqual([], list(self.trash.iterdir()))

    def _exclusive_rename_primitive_is_present(self, exclusive_move) -> bool:
        """Measure whether THIS host gives the product an exclusive rename.

        Measured, never assumed: floati/purge.py answers ENOTSUP — "the host
        has no exclusive rename primitive" — where it has none, and that typed
        absence is the product's own answer, not a fallback that replaces the
        target. Asking by moving a directory nothing else owns keeps this
        reading correct for whatever primitive a host acquires later, where
        naming the platform or a libc symbol would go stale silently.
        """

        probe = self.base / "exclusive-rename-probe"
        moved = self.base / "exclusive-rename-probe-moved"
        probe.mkdir()
        try:
            exclusive_move(probe, moved)
        except OSError as exc:
            if exc.errno == errno.ENOTSUP:
                probe.rmdir()
                return False
            raise
        moved.rmdir()
        return True

    def test_exclusive_move_refuses_filesystem_equivalent_occupied_target(self) -> None:
        source = self.root("source")
        occupied = self.trash / unicodedata.normalize("NFC", "floati-Résumé")
        occupied.mkdir()
        protected = occupied / "protected.txt"
        protected.write_text("keep\n", encoding="utf-8")
        equivalent = self.trash / unicodedata.normalize("NFD", "FLOATI-RÉSUMÉ")

        exclusive_move = getattr(purge, "_rename_exclusive", None)
        self.assertTrue(callable(exclusive_move), "exclusive rename seam is absent")

        # APFS and HFS+ fold case AND Unicode normalisation, so `equivalent` is
        # a second spelling of the SAME directory entry and the exclusive move
        # must refuse it. ext4 has neither property, so on the ubuntu runner
        # that spelling names nothing and the old fixture failed at its own
        # precondition — `host filesystem did not expose equivalence` — before
        # the product was ever asked anything.
        #
        # ⇒ A PRECONDITION THAT ONLY ONE HOST SATISFIES IS NOT A SKIP TO ADD,
        # IT IS A SECOND TYPED ANSWER TO WRITE.
        #
        # So the host fact is OBSERVED and the question survives either way:
        # where the host folds names, the guarded target is the folded
        # spelling; where it does not, the folded spelling is asserted ABSENT
        # (that is the typed absence) and the guarded target is the occupant's
        # own spelling. Both hosts assert the same law — the exclusive move
        # never replaces an occupied target — and the occupant is read back
        # intact on both.
        host_folds_names = equivalent.exists()
        if host_folds_names:
            self.assertTrue(equivalent.is_dir())
            self.assertEqual(
                (occupied.stat().st_dev, occupied.stat().st_ino),
                (equivalent.stat().st_dev, equivalent.stat().st_ino),
                "the equivalent spelling must be the same directory entry",
            )
            target = equivalent
        else:
            self.assertFalse(os.path.lexists(str(equivalent)))
            self.assertEqual([occupied.name], [entry.name for entry in self.trash.iterdir()])
            target = occupied

        expected = (
            errno.EEXIST
            if self._exclusive_rename_primitive_is_present(exclusive_move)
            else errno.ENOTSUP
        )
        with self.assertRaises(OSError) as raised:
            exclusive_move(source, target)

        self.assertEqual(expected, raised.exception.errno)
        self.assertTrue(source.is_dir())
        self.assertEqual("keep\n", protected.read_text(encoding="utf-8"))
        self.assertTrue(occupied.is_dir())

    def test_unreadable_subtree_cannot_be_omitted_from_complete_inventory(self) -> None:
        root = self.root("records")
        blocked = root / "blocked"
        blocked.mkdir()
        (blocked / "hidden.txt").write_text("hidden\n", encoding="utf-8")
        real_scandir = os.scandir

        def guarded_scandir(path: os.PathLike[str]):
            if Path(path) == blocked:
                raise PermissionError(errno.EACCES, "forced unreadable subtree")
            return real_scandir(path)

        with patch.object(Path, "rglob", return_value=[blocked]), patch(
            "floati.purge.os.scandir", side_effect=guarded_scandir
        ):
            with self.assertRaises(ProtocolRefusal) as raised:
                purge.PurgeWriter([root]).plan()

        self.assertEqual("purge_root_unreadable", raised.exception.code)
        self.assertTrue(root.is_dir())
        self.assertEqual([], list(self.trash.iterdir()))


if __name__ == "__main__":
    unittest.main()

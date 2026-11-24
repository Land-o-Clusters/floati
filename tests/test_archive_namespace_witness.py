"""Native namespace observations must not enumerate replaced directories."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from floati import epoch_carry
from floati.root import FloatiRoot
from tests.temp_roots import REAL_TEMP_ROOT


class ArchiveNamespaceWitnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = FloatiRoot.open_direct_home(self.base / "home", create=True)

    def test_replaced_directory_is_never_enumerated_as_the_captured_child(self):
        parent = self.root.tenant_home / "state"
        target = parent / "nested"
        target.mkdir(parents=True, exist_ok=True)
        (target / "original").write_bytes(b"owned")
        foreign = self.base / "foreign"
        foreign.mkdir()
        (foreign / "private").write_bytes(b"foreign")
        original_inode = target.stat().st_ino
        foreign_inode = foreign.stat().st_ino
        moved = self.base / "displaced"
        native = os.scandir
        changed, listed = [], []
        def observed(coordinate):
            def status():
                return os.fstat(coordinate) if isinstance(coordinate, int) else os.stat(coordinate)
            if status().st_ino == original_inode and not changed:
                target.rename(moved)
                foreign.rename(target)
                changed.append(True)
            if status().st_ino == foreign_inode:
                listed.append(True)
            return native(coordinate)
        with patch.object(os, "scandir", observed):
            witness = epoch_carry.carry_dependency_witness(self.root)
        self.assertTrue(changed, "the native directory substitution must execute")
        with self.subTest(property="changed namespace refuses observation"):
            self.assertIsNone(witness)
        with self.subTest(property="replacement directory is never listed"):
            self.assertEqual([], listed)

    def test_directory_stream_is_bounded_before_bulk_enumeration(self):
        for index in range(12):
            (self.root.tenant_home / ("member-" + str(index))).write_bytes(b"x")
        native = os.scandir
        advanced = []
        class Observed:
            def __init__(self, coordinate):
                self.original = native(coordinate)
            def __enter__(self):
                self.original.__enter__()
                return self
            def __exit__(self, *arguments):
                return self.original.__exit__(*arguments)
            def __iter__(self):
                return self
            def __next__(self):
                value = next(self.original)
                advanced.append(value.name)
                return value
        with patch.object(epoch_carry, "MAX_DEPENDENCY_WITNESS_ENTRIES", 3, create=True),\
                patch.object(os, "scandir", Observed):
            witness = epoch_carry.carry_dependency_witness(self.root)
        self.assertIsNone(witness, "a reached namespace support bound cannot authorize cached work")
        self.assertLessEqual(len(advanced), 3, "namespace iterator advanced past its reserved support")

    def test_nested_provider_state_receipts_remain_native_dependencies(self):
        """FLEET-ADHOC-THREADS-1: only root provider storage is private."""
        for plane in ("acks", "deliveries"):
            with self.subTest(plane=plane):
                receipt = self.root.tenant_home / "receipts" / plane / "provider-state" / "worker.jsonl"
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_bytes(b"first receipt\n")
                before = epoch_carry.carry_dependency_witness(self.root)
                self.assertIsNotNone(before)
                self.assertIn(receipt.relative_to(self.root.tenant_home).as_posix(),
                              {relative for relative, _identity in before})
                with receipt.open("ab") as handle:
                    handle.write(b"next receipt\n")
                self.assertNotEqual(before, epoch_carry.carry_dependency_witness(self.root),
                                    "native receipt changes must invalidate cached history")

    def test_exact_complete_namespace_limit_preserves_the_original_witness(self):
        for name in ("first", "second"):
            (self.root.tenant_home / name).write_bytes(b"x")
        original = epoch_carry.carry_dependency_witness(self.root)
        self.assertEqual(3, len(original))
        with patch.object(epoch_carry, "MAX_DEPENDENCY_WITNESS_ENTRIES", 3):
            self.assertEqual(original, epoch_carry.carry_dependency_witness(self.root))

    def test_unchanged_native_tree_keeps_every_identity_including_symlinks(self):
        directory = self.root.tenant_home / "state"
        directory.mkdir(exist_ok=True)
        (directory / "member").write_bytes(b"data")
        (directory / "link").symlink_to(self.base / "absent")
        from floati.bus_epoch import _selected_member_identity
        expected = [(".", _selected_member_identity(self.root.tenant_home.lstat()))]
        expected.extend((path.relative_to(self.root.tenant_home).as_posix(),
                         _selected_member_identity(path.lstat()))
                        for path in self.root.tenant_home.rglob("*"))
        self.assertEqual(tuple(sorted(expected)), epoch_carry.carry_dependency_witness(self.root))


if __name__ == "__main__":
    unittest.main()

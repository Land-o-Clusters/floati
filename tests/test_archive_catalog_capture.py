"""Real native commit bytes must be bounded before a changing read allocates."""
import os
from copy import deepcopy
import copy
import unittest
from unittest import mock

from floati import archive_catalog as commits, bus_epoch
from floati.errors import IntegrityFailure
from floati.framing import encode_frame
from floati.ids import uuid7_hex
from tests import test_archive_catalog as fixture_module


class ArchiveCatalogCaptureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.ArchiveCatalogTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.install_fixture_commit()
        self.root = self.fixture.root

    def growing_read(self, operation, target=None):
        if target is None:
            target, original, _ = self.fixture.chunks[0]
        else:
            original = target.read_bytes()
        inode = target.stat().st_ino
        native = os.read
        observed = []
        def growing(fd, count):
            if os.fstat(fd).st_ino == inode:
                if not observed:
                    with target.open("ab") as stream:
                        stream.write(b"x" * (128 * 1024))
                raw = native(fd, count)
                observed.append(len(raw))
                return raw
            return native(fd, count)
        with mock.patch.object(os, "read", growing), self.assertRaises(IntegrityFailure):
            operation(target, len(original))
        self.assertLessEqual(sum(observed), len(original) + 1,
                             "changed payload was read beyond the admitted size+1")

    def test_hidden_ancestry_read_has_size_plus_one_growth_bound(self):
        self.growing_read(lambda target, size: commits.read_committed_generation(self.root))

    def test_byte_handle_chunk_and_ledger_rereads_keep_growth_bound(self):
        for kind in ("chunk", "ledger"):
            with self.subTest(kind=kind):
                handle = commits.read_committed_generation(self.root)
                target = self.fixture.chunks[0][0] if kind == "chunk" else self.fixture.ledger
                raw = target.read_bytes()
                try:
                    self.growing_read(lambda target, size: handle.read_chunk("catalog", 0)
                        if kind == "chunk" else handle.commit, target)
                finally:
                    target.write_bytes(raw)

    def test_capture_extension_never_adopts_same_byte_replacement(self):
        target, raw, _ = self.fixture.chunks[0]
        relative = target.relative_to(self.root.path).as_posix()
        with bus_epoch.epoch_guard(self.root):
            expected = commits._capture_identities(self.root, (relative,))
            replacement = self.fixture.base / "new-same-bytes"
            replacement.write_bytes(raw)
            replacement.replace(target)
            with self.assertRaises(IntegrityFailure):
                commits._capture_identities(self.root, (), previous=expected)

    def test_expected_parent_swap_refuses_before_replacement_bytes(self):
        target, raw, _ = self.fixture.chunks[0]
        relative = target.relative_to(self.root.path).as_posix()
        with bus_epoch.epoch_guard(self.root):
            expected = commits._capture_identities(self.root, (relative,))
            target.parent.rename(self.fixture.base / "old-chunk-directory")
            target.parent.mkdir()
            target.write_bytes(raw)
            native, reads = os.read, []
            inode = target.stat().st_ino
            def watched(fd, count):
                if os.fstat(fd).st_ino == inode:
                    reads.append(count)
                return native(fd, count)
            with mock.patch.object(os, "read", watched), self.assertRaises(IntegrityFailure):
                commits._read(self.root, relative, limit=len(raw), _expected_identities=expected)
            self.assertEqual([], reads)

    def test_stat_open_parent_replacement_is_not_descended(self):
        target, raw, _ = self.fixture.chunks[0]
        relative = target.relative_to(self.root.path).as_posix()
        native_open, native_read, changed, reads = os.open, os.read, [], []
        def changed_open(path, flags, *args, **kwargs):
            if path == "chunks" and kwargs.get("dir_fd") is not None and not changed:
                target.parent.rename(self.fixture.base / "old-parent")
                target.parent.mkdir()
                target.write_bytes(raw)
                changed.append(target.stat().st_ino)
            return native_open(path, flags, *args, **kwargs)
        def watched(fd, count):
            if changed and os.fstat(fd).st_ino == changed[0]:
                reads.append(count)
            return native_read(fd, count)
        with bus_epoch.epoch_guard(self.root), mock.patch.object(os, "open", changed_open),\
                mock.patch.object(os, "read", watched), self.assertRaises(IntegrityFailure):
            commits._capture_identities(self.root, (relative,))
        self.assertEqual([], reads)

    def test_regular_to_fifo_swap_is_opened_nonblocking_then_refused(self):
        target, raw, _ = self.fixture.chunks[0]
        relative = target.relative_to(self.root.path).as_posix()
        native, swapped, opened = os.open, [], []
        def fifo_open(path, flags, *args, **kwargs):
            if path == target.name and kwargs.get("dir_fd") is not None and not swapped:
                target.unlink()
                os.mkfifo(target)
                swapped.append(True)
                # Fail the old implementation before the actual FIFO open:
                # the regression must never hang waiting for a FIFO writer.
                self.assertTrue(flags & os.O_NONBLOCK, "stat/open replacement can block on FIFO")
                descriptor = native(path, flags, *args, **kwargs)
                opened.append(True)
                return descriptor
            return native(path, flags, *args, **kwargs)
        with bus_epoch.epoch_guard(self.root), mock.patch.object(os, "open", fifo_open),\
                self.assertRaises(IntegrityFailure):
            commits._read(self.root, relative, limit=len(raw))
        self.assertEqual([True], swapped)
        self.assertEqual([True], opened)

    def test_missing_malformed_or_overcount_capture_cannot_select_a_reader(self):
        target, raw, _ = self.fixture.chunks[0]
        relative = target.relative_to(self.root.path).as_posix()
        with bus_epoch.epoch_guard(self.root):
            expected = dict(commits._capture_identities(self.root, (relative,)))
            for changed in ({key: value for key, value in expected.items() if key != relative},
                            {".": (1,)}, {"../outside": (1,) * 7}, {".": (True,) * 7}):
                with self.subTest(changed=changed), self.assertRaises(IntegrityFailure):
                    commits._read(self.root, relative, limit=len(raw), _expected_identities=changed)
            with mock.patch.object(commits, "_MAX_CAPTURE_COORDINATES", 1), self.assertRaises(IntegrityFailure):
                commits._read(self.root, relative, limit=len(raw), _expected_identities=expected)

    def test_explicit_capture_covers_hidden_ancestor_and_handle_reads(self):
        paths = (commits.LEDGER,) + tuple(path.relative_to(self.root.path).as_posix()
                                        for path, raw, row in self.fixture.chunks)
        with bus_epoch.epoch_guard(self.root):
            expected = commits._capture_identities(self.root, paths)
        handle = commits.read_committed_generation(self.root, _expected_identities=expected)
        self.assertEqual(self.fixture.record, handle.commit)
        self.assertEqual(self.fixture.chunks[0][1], handle.read_chunk("catalog", 0))
        without_chunk = dict(expected)
        del without_chunk[paths[-1]]
        with self.assertRaises(IntegrityFailure):
            commits.read_committed_generation(self.root, _expected_identities=without_chunk)

    def test_repeated_native_ancestry_does_not_revalidate_complete_map_per_reference(self):
        rows, previous = [], None
        for _ in range(8):
            row = deepcopy(self.fixture.record)
            row["id"] = "epoch-history-commit-" + uuid7_hex()
            row["previous_generation"] = previous
            row["generation"] = commits.generation_digest(row)
            rows.append(row)
            previous = row["generation"]
        self.fixture.ledger.write_bytes(b"".join(encode_frame(row) for row in rows))
        paths = (commits.LEDGER,) + tuple(path.relative_to(self.root.path).as_posix()
                                        for path, raw, row in self.fixture.chunks)
        with bus_epoch.epoch_guard(self.root):
            expected = commits._capture_identities(self.root, paths)
        native_map, native_coordinate = commits._capture_map, commits._coordinate
        inside_map, visits = [], []
        def mapping(value):
            inside_map.append(True)
            try:
                return native_map(value)
            finally:
                inside_map.pop()
        def coordinate(relative):
            if inside_map:
                visits.append(relative)
            return native_coordinate(relative)
        with mock.patch.object(commits, "_capture_map", mapping),\
                mock.patch.object(commits, "_coordinate", coordinate):
            handle = commits.read_committed_generation(self.root, _expected_identities=expected)
        self.assertEqual(previous, handle._commit["generation"])
        self.assertLessEqual(len(visits), 12 * len(expected),
            "complete capture validation multiplied by repeated ancestry references")

    def test_validated_capture_is_detached_immutable_and_forgery_cannot_skip_shape(self):
        target, raw, _ = self.fixture.chunks[0]
        relative = target.relative_to(self.root.path).as_posix()
        with bus_epoch.epoch_guard(self.root):
            ordinary = dict(commits._capture_identities(self.root, (relative,)))
            expected = commits._capture_map(ordinary)
            ordinary[relative] = (0,) * 7
            self.assertEqual(raw, commits._read(self.root, relative, limit=len(raw),
                _expected_identities=expected)[0])
            with self.assertRaises(IntegrityFailure):
                type(expected)(ordinary)
            with self.assertRaises(TypeError):
                expected[relative] = (0,) * 7
            with self.assertRaises(AttributeError):
                expected._coordinates = ordinary
            class Child(type(expected)):
                pass
            for value in (object.__new__(type(expected)), object.__new__(Child),
                          copy.copy(expected), deepcopy(expected)):
                with self.subTest(kind=type(value)), self.assertRaises(IntegrityFailure):
                    commits._read(self.root, relative, limit=len(raw), _expected_identities=value)

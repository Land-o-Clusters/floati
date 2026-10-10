"""Selected native notifier controls; no waiter decisions are fabricated."""

from __future__ import annotations

import errno
import os
import select
import struct
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from floati import tui
from floati.errors import ProtocolRefusal


class _Call:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self.callback(*args)


class F013WaiterObserverTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / "selected"
        self.directory.mkdir()
        self.ledger = self.directory / "events.jsonl"
        self.ledger.write_bytes(b"before\n")
        self.paths = (self.root, self.directory, self.ledger)

    def _native(self, paths=None):
        observer = tui.BoardFilesystemWakeup(self.root, _paths=self.paths if paths is None else paths)
        self.addCleanup(observer.close)
        return observer

    def _ready(self, observer):
        self.assertEqual([observer], select.select([observer], [], [], 1.0)[0])
        self.assertIs(observer.drain(), True)

    def _fake_inotify(self, *, fail_path=None):
        read, write = os.pipe()
        os.set_blocking(read, False)
        self.addCleanup(os.close, write)
        watches = {}

        def add(_descriptor, encoded, _mask):
            path = Path(os.fsdecode(encoded))
            if path.parent == Path("/proc/self/fd"):
                metadata = os.fstat(int(path.name))
                path = next(coordinate for coordinate in (self.root, self.directory)
                            if (coordinate.stat().st_dev, coordinate.stat().st_ino)
                            == (metadata.st_dev, metadata.st_ino))
            if path == fail_path:
                return -1
            return watches.setdefault(path, len(watches) + 1)

        libc = SimpleNamespace(
            inotify_init1=_Call(lambda _flags: read),
            inotify_add_watch=_Call(add),
            inotify_rm_watch=_Call(lambda _descriptor, _watch: 0),
        )
        return read, write, libc, watches

    def _inotify(self):
        read, write, libc, watches = self._fake_inotify()
        with mock.patch.object(tui.ctypes, "CDLL", return_value=libc):
            observer = tui._InotifyBoardFilesystemWakeup(self.root, _paths=self.paths)
        self.addCleanup(observer.close)
        return observer, read, write, watches

    def test_native_selected_append_is_dirty_then_quiet(self):
        observer = self._native()
        self.assertIs(observer.drain(), False)
        with self.ledger.open("ab") as stream:
            stream.write(b"after\n")
        self._ready(observer)
        self.assertIs(observer.drain(), False)
        expected = set(self.paths if hasattr(select, "kqueue") else self.paths[:2])
        self.assertEqual(expected, set(observer.registered_paths()))

    def test_native_selected_atomic_replacement_observes_the_new_inode(self):
        observer = self._native()
        replacement = self.directory / "replacement"
        replacement.write_bytes(b"replacement\n")
        os.replace(replacement, self.ledger)
        self._ready(observer)
        self.assertIs(observer.drain(), False)
        with self.ledger.open("ab") as stream:
            stream.write(b"new inode append\n")
        self._ready(observer)

    def test_native_missing_leaf_creation_is_observed_without_recursive_scan(self):
        missing = self.directory / "future.jsonl"
        with mock.patch.object(tui, "_board_watch_paths", side_effect=AssertionError("recursive scan")),\
                mock.patch.object(tui, "_board_watch_directories", side_effect=AssertionError("recursive scan")):
            observer = self._native((self.root, self.directory, missing))
            self.assertNotIn(missing, observer.registered_paths())
            missing.write_bytes(b"created\n")
            self._ready(observer)
            expected = {self.root, self.directory}
            if hasattr(select, "kqueue"):
                expected.add(missing)
            self.assertEqual(expected, set(observer.registered_paths()))

    def test_native_default_board_still_watches_unselected_subtrees(self):
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        observer = tui.BoardFilesystemWakeup(self.root)
        self.addCleanup(observer.close)
        self.assertIn(unrelated, observer.registered_paths())
        (unrelated / "notice").write_bytes(b"changed\n")
        self.assertEqual([observer], select.select([observer], [], [], 1.0)[0])
        self.assertIsNone(observer.drain())

    def test_selected_scope_rejects_symlink_and_outside_root(self):
        alias = self.root / "alias"
        alias.symlink_to(self.directory, target_is_directory=True)
        for paths in ((self.root, alias / "events.jsonl"), (self.root, self.root.parent)):
            with self.subTest(paths=paths), self.assertRaises(ProtocolRefusal) as refused:
                tui.BoardFilesystemWakeup(self.root, _paths=paths)
            self.assertEqual("board_event_watch_unavailable", refused.exception.code)

    def test_inotify_partial_enoent_registration_is_unavailable_and_closes_fd(self):
        read, _write, libc, _watches = self._fake_inotify(fail_path=self.directory)
        with mock.patch.object(tui.ctypes, "CDLL", return_value=libc),\
                mock.patch.object(tui.ctypes, "get_errno", return_value=errno.ENOENT):
            with self.assertRaises(ProtocolRefusal) as refused:
                tui._InotifyBoardFilesystemWakeup(self.root, _paths=self.paths)
        self.assertEqual("board_event_watch_unavailable", refused.exception.code)
        with self.assertRaises(OSError):
            os.fstat(read)

    def test_inotify_aba_path_replacement_cannot_redirect_the_registered_directory(self):
        _read, _write, libc, _watches = self._fake_inotify()
        original = self.directory.stat().st_ino
        retired = self.root / "retired"
        native_add = libc.inotify_add_watch.callback
        bound = []

        def add(descriptor, encoded, mask):
            path = Path(os.fsdecode(encoded))
            self.assertEqual(Path("/proc/self/fd"), path.parent)
            directory = int(path.name)
            if os.fstat(directory).st_ino == original:
                self.directory.rename(retired)
                self.directory.mkdir()
                try:
                    self.assertNotEqual(self.directory.stat().st_ino, os.fstat(directory).st_ino)
                    bound.append(os.fstat(directory).st_ino)
                finally:
                    self.directory.rmdir()
                    retired.rename(self.directory)
            return native_add(descriptor, encoded, mask)

        libc.inotify_add_watch.callback = add
        with mock.patch.object(tui.ctypes, "CDLL", return_value=libc):
            observer = tui._InotifyBoardFilesystemWakeup(self.root, _paths=self.paths)
        self.addCleanup(observer.close)
        self.assertEqual([original], bound)
        self.assertEqual({self.root, self.directory}, set(observer.registered_paths()))

    def test_inotify_overflow_invalidation_and_malformed_data_are_unavailable(self):
        observer, _read, write, watches = self._inotify()
        watch = watches[self.root]
        for data in (struct.pack("iIII", -1, 0x4000, 0, 0),
                     struct.pack("iIII", watch, 0x8000, 0, 0),
                     struct.pack("iIII", watch, 0x2000, 0, 0),
                     struct.pack("iIII", watch, 0x400, 0, 0),
                     struct.pack("iIII", watch, 0x800, 0, 0),
                     struct.pack("iIII", watch, 0, 0, 0),
                     b"truncated", struct.pack("iIII", watch, 2, 0, 8) + b"x\0\0\0",
                     struct.pack("iIII", watch, 2, 0, 4) + b"xxxx"):
            with self.subTest(data=data):
                os.write(write, data)
                with self.assertRaises(ProtocolRefusal) as refused:
                    observer.drain()
                self.assertEqual("board_event_watch_unavailable", refused.exception.code)

    def test_inotify_continuous_event_stream_has_a_bounded_drain(self):
        observer, _read, _write, watches = self._inotify()
        event = struct.pack("iIII", watches[self.root], 2, 0, 0)
        with mock.patch.object(tui.os, "read", return_value=event) as reading:
            with self.assertRaises(ProtocolRefusal) as refused:
                observer.drain()
        self.assertEqual("board_event_watch_unavailable", refused.exception.code)
        self.assertLessEqual(reading.call_count, 16)

    def test_inotify_read_failure_and_repeated_close_release_the_descriptor(self):
        observer, read, _write, _watches = self._inotify()
        with mock.patch.object(tui.os, "read", side_effect=OSError(errno.EIO, "read failure")):
            with self.assertRaises(ProtocolRefusal):
                observer.drain()
        observer.close()
        observer.close()
        self.assertEqual((), observer.registered_paths())
        with self.assertRaises(OSError):
            os.fstat(read)

    def test_kqueue_partial_registration_failure_closes_all_acquired_descriptors(self):
        descriptors = []
        native_open = os.open

        def opened(*args, **kwargs):
            descriptor = native_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor

        class Queue:
            closed = False
            calls = 0

            def control(self, changes, _maximum, _timeout):
                if changes:
                    self.calls += 1
                    if self.calls == 2:
                        raise OSError(errno.EACCES, "registration failure")
                return []

            def close(self):
                self.closed = True

        queue = Queue()
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(tui.select, "kqueue", return_value=queue, create=True))
            stack.enter_context(mock.patch.object(tui.select, "kevent", side_effect=lambda *args, **kwargs: object(), create=True))
            for index, name in enumerate(("KQ_NOTE_WRITE", "KQ_NOTE_EXTEND", "KQ_NOTE_ATTRIB", "KQ_NOTE_LINK",
                                          "KQ_NOTE_RENAME", "KQ_NOTE_DELETE", "KQ_NOTE_REVOKE",
                                          "KQ_FILTER_VNODE", "KQ_EV_ADD", "KQ_EV_CLEAR")):
                stack.enter_context(mock.patch.object(tui.select, name, 1 << index, create=True))
            stack.enter_context(mock.patch.object(tui.os, "open", side_effect=opened))
            with self.assertRaises(ProtocolRefusal):
                tui._KqueueBoardFilesystemWakeup(self.root, _paths=self.paths)
        self.assertTrue(queue.closed)
        self.assertGreaterEqual(len(descriptors), 2)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_kqueue_ancestor_aba_cannot_certify_a_foreign_vnode(self):
        foreign = self.root / "foreign"
        foreign.mkdir()
        (foreign / self.ledger.name).write_bytes(b"foreign\n")
        retired = self.root / "retired"
        descriptors = []
        native_open, native_dup, native_lstat = os.open, os.dup, Path.lstat
        swapped = False
        observations = 0
        bound = []
        original = self.ledger.stat().st_ino

        def restore():
            nonlocal swapped
            if swapped:
                self.directory.unlink()
                retired.rename(self.directory)
                swapped = False

        def opened(*args, **kwargs):
            nonlocal swapped
            if Path(args[0]).name == self.ledger.name:
                self.directory.rename(retired)
                self.directory.symlink_to(foreign, target_is_directory=True)
                swapped = True
            descriptor = native_open(*args, **kwargs)
            descriptors.append(descriptor)
            if Path(args[0]).name == self.ledger.name:
                bound.append(os.fstat(descriptor).st_ino)
            return descriptor

        def duplicated(descriptor):
            result = native_dup(descriptor)
            descriptors.append(result)
            return result

        def observed(path, *args, **kwargs):
            nonlocal observations
            result = native_lstat(path, *args, **kwargs)
            if swapped and path == self.ledger:
                observations += 1
                if observations == 2:
                    # An absolute-path implementation can see the foreign
                    # inode at both checks, then the original caller snapshot.
                    restore()
            return result

        class Queue:
            closed = False

            def control(self, _changes, _maximum, _timeout):
                return []

            def close(self):
                self.closed = True

        queue = Queue()
        try:
            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(tui.select, "kqueue", return_value=queue, create=True))
                stack.enter_context(mock.patch.object(tui.select, "kevent", side_effect=lambda *args, **kwargs: object(), create=True))
                for index, name in enumerate(("KQ_NOTE_WRITE", "KQ_NOTE_EXTEND", "KQ_NOTE_ATTRIB", "KQ_NOTE_LINK",
                                              "KQ_NOTE_RENAME", "KQ_NOTE_DELETE", "KQ_NOTE_REVOKE",
                                              "KQ_FILTER_VNODE", "KQ_EV_ADD", "KQ_EV_CLEAR")):
                    stack.enter_context(mock.patch.object(tui.select, name, 1 << index, create=True))
                stack.enter_context(mock.patch.object(tui.os, "open", side_effect=opened))
                stack.enter_context(mock.patch.object(tui.os, "dup", side_effect=duplicated))
                stack.enter_context(mock.patch.object(Path, "lstat", autospec=True, side_effect=observed))
                with self.assertRaises(ProtocolRefusal) as refused:
                    tui._KqueueBoardFilesystemWakeup(self.root, _paths=self.paths)
                self.assertEqual("board_event_watch_unavailable", refused.exception.code)
        finally:
            restore()
        self.assertEqual([original], bound)
        self.assertTrue(queue.closed)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

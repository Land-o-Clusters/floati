"""An ENOENT after selecting an existing input parent loses watch coverage.

Private deterministic backend fixtures bind real owned directory descriptors.
Only native registration or its first open is faulted; no hook/process runs.
The parent owns RED/GREEN execution.
"""
import errno
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import tests
from floati import tui
from floati.errors import ProtocolRefusal
from tests.temp_roots import REAL_TEMP_ROOT


class HostWatchBindingGapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=REAL_TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.parent = self.root / "state" / "host-observations" / "selected-seat"
        self.parent.mkdir(parents=True)
        self.target = self.parent / "latest.json"

    def selected_open(self, failed, *, inject):
        native_open = tui._open_selected_watch_path
        def open_path(root, path):
            if inject and path == self.parent and not failed:
                self.assertTrue(path.is_dir(), "control must fault an already selected existing parent")
                failed.append("open")
                raise FileNotFoundError(errno.ENOENT, "fixture selected-parent binding gap")
            return native_open(root, path)
        return open_path

    def assert_rebound_is_not_complete_coverage(self, backend, failed, mapping):
        proxy = tui.BoardFilesystemWakeup.__new__(tui.BoardFilesystemWakeup)
        proxy._backend = backend
        try:
            backend._refresh()
        except (ProtocolRefusal, OSError) as refusal:
            self.assertTrue(failed, "refusal preceded the controlled acquisition gap")
            if isinstance(refusal, ProtocolRefusal):
                self.assertEqual("board_event_watch_unavailable", refusal.code)
            return  # Refusing acquisition also prevents a due publisher starting.
        self.assertTrue(failed, "first selected binding was never faulted")
        backend._refresh()  # The path/registration has recovered, without a new source.
        self.assertIn(self.parent, mapping, "control did not restore the missing parent watch")
        self.assertTrue(proxy.coverage_lost,
            "later ancestor/parent registration silently erased a real initial coverage gap")

    def kqueue_fixture(self):
        backend = tui._KqueueBoardFilesystemWakeup.__new__(tui._KqueueBoardFilesystemWakeup)
        backend._root, backend._paths = self.root, (self.target,)
        backend._descriptors, backend._observer_snapshots = {}, {}
        backend._closed, backend.coverage_lost = False, False
        backend._queue = SimpleNamespace(control=lambda *_args: [], close=lambda: None)
        self.addCleanup(backend.close)
        return backend

    def kqueue_api(self):
        # Permit the same deterministic registration control on Linux and macOS.
        return mock.patch.multiple(tui.select, create=True,
            KQ_NOTE_WRITE=1, KQ_NOTE_EXTEND=2, KQ_NOTE_ATTRIB=4, KQ_NOTE_LINK=8,
            KQ_NOTE_RENAME=16, KQ_NOTE_DELETE=32, KQ_NOTE_REVOKE=64,
            KQ_FILTER_VNODE=-4, KQ_EV_ADD=1, KQ_EV_CLEAR=32,
            kevent=lambda *args, **kwargs: SimpleNamespace(args=args, **kwargs))

    def test_kqueue_existing_selected_parent_open_gap_remains_sticky_after_rebind(self):
        backend, failed = self.kqueue_fixture(), []
        with self.kqueue_api(), mock.patch.object(tui, "_open_selected_watch_path",
                side_effect=self.selected_open(failed, inject=True)):
            self.assert_rebound_is_not_complete_coverage(backend, failed, backend._descriptors)

    def inotify_fixture(self, failed, *, registration_gap=False):
        reader, writer = os.pipe()
        self.addCleanup(os.close, writer)
        backend = tui._InotifyBoardFilesystemWakeup.__new__(tui._InotifyBoardFilesystemWakeup)
        backend._root, backend._paths = self.root, (self.target,)
        backend._watches, backend._identities = {}, {}
        backend._descriptor, backend._closed, backend.coverage_lost = reader, False, False
        parent = self.parent.stat()
        parent_identity = (parent.st_dev, parent.st_ino)
        registrations = {}
        def add_watch(_descriptor, coordinate, _mask):
            # The actual backend passes its owned selected-directory FD.
            opened = os.fstat(int(os.fsdecode(coordinate).rsplit("/", 1)[-1]))
            identity = (opened.st_dev, opened.st_ino)
            if registration_gap and identity == parent_identity and not failed:
                failed.append("registration")
                return -1
            if identity not in registrations:
                registrations[identity] = len(registrations) + 1
            return registrations[identity]
        backend._libc = SimpleNamespace(inotify_add_watch=add_watch,
            inotify_rm_watch=lambda *_args: 0)
        self.addCleanup(backend.close)
        return backend

    def test_inotify_existing_selected_parent_open_gap_remains_sticky_after_rebind(self):
        failed = []
        backend = self.inotify_fixture(failed)
        with mock.patch.object(tui, "_open_selected_watch_path",
                side_effect=self.selected_open(failed, inject=True)):
            self.assert_rebound_is_not_complete_coverage(backend, failed, backend._watches)

    def test_inotify_selected_parent_registration_enoent_remains_sticky_after_rebind(self):
        failed = []
        backend = self.inotify_fixture(failed, registration_gap=True)
        with mock.patch.object(tui.ctypes, "get_errno", return_value=errno.ENOENT):
            self.assert_rebound_is_not_complete_coverage(backend, failed, backend._watches)

    def test_absent_future_input_with_fully_bound_existing_parents_is_not_coverage_loss(self):
        self.assertFalse(self.target.exists())
        kqueue = self.kqueue_fixture()
        with self.kqueue_api():
            kqueue._refresh()
            self.assertIn(self.parent, kqueue._descriptors)
            self.assertFalse(kqueue.coverage_lost)
        inotify = self.inotify_fixture([])
        inotify._refresh()
        self.assertIn(self.parent, inotify._watches)
        self.assertFalse(inotify.coverage_lost)

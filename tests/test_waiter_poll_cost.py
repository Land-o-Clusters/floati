"""Native idle-cost and change-notification controls; scratch fleets only."""
from __future__ import annotations

import io
import json
import os
import unittest
from unittest import mock

from floati import codex_wait, cursor_presentations, tui, wake_hold
from tests import test_codex_wait as baseline
from tests.waiter_timing import scripted_readiness


class WaiterPollCostTests(unittest.TestCase):
    def setUp(self):
        self.f = baseline.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.payload = {"cwd": str(self.f.workspace), "session_id": "cost-conversation"}

    def hold(self):
        for index in range(3):
            self.f.send("cost-held-" + str(index))
        output, error = io.StringIO(), io.StringIO()
        codex_wait.run_stop_waiter(bus_home=self.f.bus_home, hook_payload=self.payload,
                                   stdout=output, stderr=error)
        self.assertIn("3 new message(s)", output.getvalue(), error.getvalue())

    def run_idle(self, change=None):
        clock, polls = [0.0], []
        output, error = io.StringIO(), io.StringIO()

        def sleep(seconds):
            clock[0] += seconds
            polls.append(clock[0])
            if change is not None and len(polls) == 1:
                change()

        codex_wait.run_stop_waiter(bus_home=self.f.bus_home, hook_payload=self.payload,
                                   stdout=output, stderr=error,
                                   monotonic=lambda: clock[0], wait_readable=scripted_readiness((sleep), interval=(0.1)), sleep=sleep,
                                   poll_interval_seconds=0.1)
        return json.loads(output.getvalue()), error.getvalue(), polls

    def test_unchanged_hold_does_not_reproject_history_on_each_poll(self):
        """Removing the change gate restores repeated semantic replay cost."""
        self.hold()
        with mock.patch.object(cursor_presentations, "project_wake_items",
                               wraps=cursor_presentations.project_wake_items) as projection:
            decision, error, polls = self.run_idle()
        self.assertIn("deadline exhausted", decision["reason"], error)
        self.assertGreaterEqual(len(polls), 19)
        self.assertGreater(projection.call_count, 0, "cold conversation history never reached the observed projector")
        # Initial decision, watcher-registration changes, final guarded exit.
        self.assertLessEqual(projection.call_count, 3,
                             "unchanged polls repeatedly projected all durable mail")

    def test_same_mtime_new_mail_is_published_by_the_next_poll(self):
        """An mtime-only clean hint would suppress a real new envelope."""
        self.hold()
        path = self.f.root.resolve_relative("events.jsonl")
        before = path.stat()
        fresh = []

        def append():
            fresh.append(self.f.send("cost-new"))
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        decision, error, polls = self.run_idle(append)
        self.assertEqual([0.1], polls)
        self.assertIn(fresh[0]["id"], decision["reason"], error)
        self.assertEqual(before.st_mtime_ns, path.stat().st_mtime_ns)

    def test_same_size_middle_rewrite_cannot_reuse_a_validated_prefix(self):
        """A dirty cursor retaining its old middle bytes hides broken testimony."""
        self.hold()
        path = self.f.root.resolve_relative("events.jsonl")
        before = path.stat()
        raw = path.read_bytes()
        frames = raw.splitlines(keepends=True)
        messages = [index for index, frame in enumerate(frames) if b"live ping" in frame]
        self.assertEqual(3, len(messages))
        frames[messages[1]] = frames[messages[1]].replace(b"live ping", b"live pang")
        rewritten = b"".join(frames)
        self.assertNotEqual(raw, rewritten)
        self.assertEqual(len(raw), len(rewritten))

        def corrupt():
            path.write_bytes(rewritten)
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

        decision, error, _polls = self.run_idle(corrupt)
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_size, before.st_mtime_ns),
                         (after.st_ino, after.st_size, after.st_mtime_ns))
        self.assertIn("integrity", decision["reason"], error)
        self.assertNotIn("new message(s)", decision["reason"])

    def test_unavailable_notifications_keep_bounded_polling_and_cursor_cache(self):
        self.hold()
        with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("unavailable")),\
             mock.patch.object(codex_wait, "WakeHoldController", wraps=wake_hold.WakeHoldController) as controllers:
            decision, error, polls = self.run_idle()
        self.assertIn("deadline exhausted", decision["reason"])
        self.assertGreaterEqual(len(polls), 19)
        self.assertEqual(1, controllers.call_count, "fallback discarded the native cursor on clean polls")
        self.assertEqual(1, error.count("using bounded polling"))

    def test_unavailable_notifications_still_deliver_next_poll_mail(self):
        self.hold()
        fresh = []
        with mock.patch.object(tui, "BoardFilesystemWakeup", side_effect=OSError("unavailable")):
            decision, error, polls = self.run_idle(lambda: fresh.append(self.f.send("fallback-new")))
        self.assertEqual([0.1], polls)
        self.assertIn(fresh[0]["id"], decision["reason"], error)

    def test_selected_watch_rejects_an_ancestor_substituted_after_enumeration(self):
        root, outside = self.f.base / "watch-root", self.f.base / "outside-watch"
        root.mkdir()
        outside.mkdir()
        parent = root / "named"
        parent.mkdir()
        target = parent / "ledger"
        target.write_text("inside")
        (outside / "ledger").write_text("outside")
        self.assertIn(target, tui._selected_watch_paths(root, (target,)))
        real_open = os.open

        def replace_before_open(path, flags, **kwargs):
            if path == "named":
                parent.rename(root / "original-named")
                parent.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, **kwargs)

        with mock.patch.object(os, "open", side_effect=replace_before_open):
            with self.assertRaises(OSError):
                tui._open_selected_watch_path(root, target)

    def test_selected_watch_closes_all_owned_descriptors_on_identity_failure(self):
        root = self.f.base / "watch-close"
        root.mkdir()
        target = root / "ledger"
        target.write_text("inside")
        opened = []
        real_open, real_stat = os.open, os.stat

        def observe_open(*args, **kwargs):
            descriptor = real_open(*args, **kwargs)
            opened.append(descriptor)
            return descriptor

        def fail_identity(path, **kwargs):
            if path == "ledger" and "dir_fd" in kwargs:
                raise OSError("identity read failed")
            return real_stat(path, **kwargs)

        with mock.patch.object(os, "open", side_effect=observe_open),\
             mock.patch.object(os, "stat", side_effect=fail_identity):
            with self.assertRaises(OSError):
                tui._open_selected_watch_path(root, target)
        self.assertTrue(opened)
        for descriptor in opened:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_selected_inotify_drain_is_bounded_under_an_event_flood(self):
        import struct

        source = tui._InotifyBoardFilesystemWakeup.__new__(tui._InotifyBoardFilesystemWakeup)
        parent = self.f.root.tenant_home
        source._paths = (parent / "events.jsonl",)
        source._watches = {parent: 7}
        source._descriptor = -1
        event = struct.pack("iIII", 7, 0x00000002, 0, 12) + b"events.jsonl"
        with mock.patch.object(os, "read", return_value=event) as read,\
             mock.patch.object(source, "_refresh"):
            self.assertTrue(source.drain())
        self.assertEqual(1, read.call_count, "an unbounded drain can outrun the wait deadline")

    def test_selected_kqueue_refresh_rebinds_a_hard_linked_atomic_replacement(self):
        import select

        root = self.f.base / "watch-replace"
        root.mkdir()
        target = root / "ledger"
        target.write_text("old")
        os.link(target, root / "old-still-alive")
        source = tui._KqueueBoardFilesystemWakeup.__new__(tui._KqueueBoardFilesystemWakeup)
        source._root, source._paths = root, (target,)
        source._closed = False
        source._queue = mock.Mock()
        source._descriptors = {target: tui._open_selected_watch_path(root, target)}
        self.addCleanup(source.close)
        old = os.fstat(source._descriptors[target]).st_ino
        replacement = root / "replacement"
        replacement.write_text("new")
        os.replace(replacement, target)
        constants = {name: 1 << index for index, name in enumerate((
            "KQ_NOTE_WRITE", "KQ_NOTE_EXTEND", "KQ_NOTE_ATTRIB", "KQ_NOTE_LINK",
            "KQ_NOTE_RENAME", "KQ_NOTE_DELETE", "KQ_NOTE_REVOKE", "KQ_FILTER_VNODE",
            "KQ_EV_ADD", "KQ_EV_CLEAR"))}
        with mock.patch.multiple(select, create=True, **constants),\
             mock.patch.object(select, "kevent", create=True):
            source._refresh()
        self.assertNotEqual(old, os.fstat(source._descriptors[target]).st_ino)
        self.assertEqual(target.stat().st_ino, os.fstat(source._descriptors[target]).st_ino)

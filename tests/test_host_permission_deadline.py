"""The owned deadline process publishes from its exact native occurrence."""
import importlib
import os
import select
import time
import unittest
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from floati.events import EventLog
from tests import test_seat_execution_host_tool_native as native_fixture
from tests.readiness import Readiness, wait_readable, wait_until


class HostPermissionDeadlineTests(unittest.TestCase):
    def run(self, result=None):
        # unittest normally skips doCleanups on KeyboardInterrupt. The gateway
        # can be interrupted by an architect HOLD while a native child exists.
        try:
            return super().run(result)
        finally:
            self.doCleanups()

    def setUp(self):
        self.fixture = native_fixture.HostToolCallbackNativeTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def modules(self):
        try:
            return (importlib.import_module("floati.host_observations"),
                    importlib.import_module("floati.host_deadline"))
        except ModuleNotFoundError:
            self.fail("owned native permission deadline producer is absent")

    def occurrence(self, observer):
        payload = self.fixture.payload()
        payload["hook_event_name"] = "PermissionRequest"
        del payload["tool_use_id"]
        del payload["tool_response"]
        return observer.observe_codex_host(self.fixture.root.path, payload,
            now=self.fixture.now, launch=False)

    def wait_exit(self, owner):
        result = []
        def exited():
            value = owner.poll()
            if value is not None:
                result.append(value)
                return True
            return False
        wait_until(exited, "owned native deadline child %s exit/reap" % owner.pid)
        return result[0]

    def clear(self, observer):
        return observer.observe_codex_host(self.fixture.root.path,
            self.fixture.payload(0, "later success"),
            now=self.fixture.now + timedelta(seconds=1), launch=False)

    def test_owned_child_starts_promptly_and_exits_after_one_due_publication(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        # This representable start exposes the frozen boundary's rounding:
        # (128.1 + 900) - 128.1 is slightly less than 900.
        occurrence.started_monotonic = 128.1
        # Only the clock boundary is controlled; fork, native admission and
        # EventLog publication are the real product operations on a private root.
        with patch.object(deadline, "_utc_now",
                return_value=self.fixture.now + timedelta(seconds=900)),\
                patch.object(deadline, "_monotonic",
                    return_value=occurrence.started_monotonic + 901):
            started = time.monotonic()
            owner = deadline.start_deadline(occurrence)
        self.assertIsNotNone(owner, "native due-child startup was unavailable")
        self.addCleanup(owner.cancel)
        self.assertLess(time.monotonic() - started, 5,
            "a permission observer must not wait for its 15-minute deadline")
        result = self.wait_exit(owner)
        self.assertEqual(0, result, "owned child failed to publish/reap under its controlled clock")
        self.assertEqual(1, sum(row["kind"] == "message_envelope"
            for row in EventLog(self.fixture.root).event_records()))
        self.assertTrue(owner.stdio_detached)

    def test_wall_clock_jump_alone_cannot_make_the_native_deadline_due(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        with patch.object(deadline, "_utc_now",
                return_value=self.fixture.now + timedelta(hours=1)),\
                patch.object(deadline, "_monotonic",
                    return_value=occurrence.started_monotonic):
            owner = deadline.start_deadline(occurrence)
        self.addCleanup(owner.cancel)
        self.assertIsNone(owner.poll())
        owner.cancel()
        self.assertIsNotNone(owner.poll(), "cancel must collect the owned child's exit")
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_unavailable_process_start_never_claims_fact_delivery(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        with patch.object(deadline.os, "fork", side_effect=OSError("fixture fork refusal")):
            self.assertIsNone(deadline.start_deadline(occurrence))
        projection = observer.host_approval_projection(self.fixture.root, "builder")
        self.assertEqual("UNKNOWN", projection["state"])
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_partial_pipe_start_failure_closes_its_owned_descriptors(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        native_pipe, opened = os.pipe, []
        def fail_second_pipe():
            if opened:
                raise OSError("fixture second pipe unavailable")
            descriptors = native_pipe()
            opened.extend(descriptors)
            return descriptors
        try:
            with patch.object(os, "pipe", side_effect=fail_second_pipe):
                self.assertIsNone(deadline.start_deadline(occurrence))
            for descriptor in opened:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
            self.assertEqual([], EventLog(self.fixture.root).event_records())
        finally:
            for descriptor in opened:
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def test_monotonic_elapsed_alone_cannot_publish_before_utc_age_is_due(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        with patch.object(deadline, "_utc_now", return_value=self.fixture.now - timedelta(seconds=1)),\
                patch.object(deadline, "_monotonic", return_value=occurrence.started_monotonic + 901):
            owner = deadline.start_deadline(occurrence)
        self.assertIsNotNone(owner)
        self.addCleanup(owner.cancel)
        self.assertIsNone(owner.poll())
        owner.cancel()
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_watch_start_failure_never_releases_a_publisher(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        with patch("floati.tui.BoardFilesystemWakeup", side_effect=OSError("fixture watch refusal")):
            self.assertIsNone(deadline.start_deadline(occurrence))
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_startup_timeout_cancels_before_release_even_when_both_clocks_are_due(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        from floati.tui import BoardFilesystemWakeup
        def slow_watch(*args, **kwargs):
            time.sleep(0.1)
            return BoardFilesystemWakeup(*args, **kwargs)
        with patch("floati.tui.BoardFilesystemWakeup", side_effect=slow_watch),\
                patch.object(deadline, "_utc_now", return_value=self.fixture.now + timedelta(seconds=900)),\
                patch.object(deadline, "_monotonic", return_value=occurrence.started_monotonic + 900):
            self.assertIsNone(deadline.start_deadline(occurrence, startup_timeout_seconds=0.01))
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_parent_write_failure_before_release_never_publishes(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        parent, native_write = os.getpid(), os.write
        def fail_parent_write(descriptor, data):
            if os.getpid() == parent:
                raise OSError("fixture parent pipe failure")
            return native_write(descriptor, data)
        with patch.object(os, "write", side_effect=fail_parent_write),\
                patch.object(deadline, "_utc_now", return_value=self.fixture.now + timedelta(seconds=900)),\
                patch.object(deadline, "_monotonic", return_value=occurrence.started_monotonic + 900):
            self.assertIsNone(deadline.start_deadline(occurrence))
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_real_waiting_child_exits_on_later_native_activity_without_mail(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        startup = Readiness("native invalidation fixture READY/release")
        native_select = select.select
        startup_pending = True
        def fixture_select(readers, writers, errors, timeout):
            nonlocal startup_pending
            if startup_pending:
                # Each fork branch gets its own first select: the parent waits
                # for READY and the prepared child waits for release. This
                # scenario tests later invalidation, not startup scheduling.
                startup_pending = False
                timeout = startup.remaining()
            return native_select(readers, writers, errors, timeout)
        # Only this scenario substitutes the startup I/O watchdog. The child
        # retains the real native watcher and every subsequent select timeout;
        # the separate product startup/deadline tests keep their own bounds.
        with patch.object(deadline, "select", SimpleNamespace(select=fixture_select)):
            owner = deadline.start_deadline(occurrence)
        self.assertIsNotNone(owner)
        self.addCleanup(owner.cancel)
        self.assertIsNone(owner.poll())
        with ExitStack() as resources:
            exited = Readiness("owned native invalidation child kernel exit")
            if hasattr(select, "kqueue"):
                source = select.kqueue()
                resources.callback(source.close)
                source.control([select.kevent(owner.pid,
                    filter=select.KQ_FILTER_PROC, flags=select.KQ_EV_ADD,
                    fflags=select.KQ_NOTE_EXIT)], 0)
                self.clear(observer)
                events = source.control(None, 1, exited.remaining())
                if not events:
                    raise exited.failure()
                self.assertEqual(owner.pid, events[0].ident)
                self.assertTrue(events[0].fflags & select.KQ_NOTE_EXIT)
            else:
                descriptor = os.pidfd_open(owner.pid)
                resources.callback(os.close, descriptor)
                self.clear(observer)
                exited.readable(descriptor)
            self.assertEqual(0, owner.poll())
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_duplicate_start_reuses_one_owned_process_for_the_exact_occurrence(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        first = deadline.start_deadline(occurrence)
        self.assertIsNotNone(first)
        self.addCleanup(first.cancel)
        self.assertIs(first, deadline.start_deadline(occurrence))
        self.clear(observer)
        self.assertEqual(0, self.wait_exit(first))
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_detached_parent_control_eof_keeps_the_released_child_waiting(self):
        observer, deadline = self.modules()
        owner = deadline.start_deadline(self.occurrence(observer))
        self.assertIsNotNone(owner)
        self.addCleanup(owner.cancel)
        self.addCleanup(self.clear, observer)
        owner.detach()
        self.assertIsNone(owner.poll(), "ordinary parent exit must not cancel a released child")
        self.clear(observer)
        self.assertEqual(0, self.wait_exit(owner))
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_waiting_child_does_not_retain_any_captured_hook_output_pipe(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        reader, writer = os.pipe()
        saved = [os.dup(1), os.dup(2)]
        owner = None
        try:
            os.dup2(writer, 1); os.dup2(writer, 2)
            owner = deadline.start_deadline(occurrence)
        finally:
            os.dup2(saved[0], 1); os.dup2(saved[1], 2)
            for descriptor in saved + [writer]:
                os.close(descriptor)
        self.addCleanup(os.close, reader)
        self.assertIsNotNone(owner)
        self.addCleanup(owner.cancel)
        self.assertIsNone(owner.poll(), "child must still be waiting while EOF is proved")
        wait_readable(reader, "native child retained the parent's captured output pipe")
        self.assertEqual(b"", os.read(reader, 1), "observer wrote unexpected hook output")
        self.clear(observer)
        self.assertEqual(0, self.wait_exit(owner))


    def test_unproved_startup_cleanup_retains_the_exact_owned_child(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        from floati.tui import BoardFilesystemWakeup
        retained_by_fixture = []
        def slow_watch(*args, **kwargs):
            time.sleep(0.2)
            return BoardFilesystemWakeup(*args, **kwargs)
        def unproved_cancel(owner):
            retained_by_fixture.append(owner)
            owner.detach()  # Actual failed cancel closes control before reporting an unproved reap.
            return None
        with patch("floati.tui.BoardFilesystemWakeup", side_effect=slow_watch),\
                patch.object(deadline.DeadlineOwner, "cancel", new=unproved_cancel):
            self.assertIsNone(deadline.start_deadline(occurrence, startup_timeout_seconds=0.01))
        for captured in retained_by_fixture:
            self.addCleanup(captured.cancel)
        owner = occurrence.owner
        self.assertIsNotNone(owner, "unproved cleanup discarded the retained native child handle")
        self.addCleanup(owner.cancel)
        self.assertTrue(occurrence.startup_cleanup_unproved)
        self.assertIsNone(owner._control)
        self.assertIsNone(deadline.start_deadline(occurrence), "failed startup must not release or relaunch")
        self.assertIsNotNone(owner.cancel(), "retained owner could not collect its own eventual exit")
        self.assertEqual([], EventLog(self.fixture.root).event_records())

    def test_lost_notification_coverage_exits_without_publication(self):
        observer, deadline = self.modules()
        for loss, already_due in (("overflow", False), ("overflow", True),
                                  ("lost_watch", False), ("lost_watch", True)):
            with self.subTest(loss=loss, already_due=already_due):
                occurrence = self.occurrence(observer)
                class LostWatch:
                    def __init__(self, *args, **kwargs):
                        self.reader, self.writer = os.pipe()
                        self.coverage_lost = False
                        os.write(self.writer, b"L")
                    def fileno(self):
                        return self.reader
                    def drain(self):
                        os.read(self.reader, 1)
                        self.coverage_lost = True
                        return True
                    def close(self):
                        os.close(self.reader)
                        os.close(self.writer)
                ticks = [occurrence.started_monotonic + (900 if already_due else 0),
                         occurrence.started_monotonic + 900]
                with patch("floati.tui.BoardFilesystemWakeup", LostWatch),\
                        patch.object(deadline, "_utc_now", return_value=self.fixture.now + timedelta(seconds=900)),\
                        patch.object(deadline, "_monotonic", side_effect=ticks):
                    owner = deadline.start_deadline(occurrence)
                self.assertIsNotNone(owner)
                self.addCleanup(owner.cancel)
                self.wait_exit(owner)
                self.assertEqual([], EventLog(self.fixture.root).event_records(), loss)

    def test_native_backends_distinguish_lost_coverage_from_ordinary_change(self):
        import struct
        from types import SimpleNamespace
        from floati import tui
        from pathlib import Path
        path = Path("\x2fprivate/tmp/fixture-watch")
        for mask in (0x00004000, 0x00008000):
            backend = tui._InotifyBoardFilesystemWakeup.__new__(tui._InotifyBoardFilesystemWakeup)
            backend._paths, backend._watches = {path / "latest.json"}, {path: 4}
            backend._descriptor, backend.coverage_lost = -1, False
            backend._refresh = lambda: None
            data = struct.pack("iIII", -1 if mask == 0x00004000 else 4, mask, 0, 0)
            with patch.object(os, "read", return_value=data):
                backend.drain()
            self.assertTrue(backend.coverage_lost, "native overflow/lost watch was only treated as a normal change")
        if hasattr(select, "kqueue"):
            for flag in (select.KQ_NOTE_RENAME, select.KQ_NOTE_DELETE, select.KQ_NOTE_REVOKE):
                reader, writer = os.pipe()
                try:
                    backend = tui._KqueueBoardFilesystemWakeup.__new__(tui._KqueueBoardFilesystemWakeup)
                    backend._paths, backend._descriptors = (path,), {path: reader}
                    backend.coverage_lost = False
                    backend._refresh = lambda: None
                    backend._queue = SimpleNamespace(control=lambda *args: [SimpleNamespace(ident=reader, fflags=flag)])
                    backend.drain()
                    reader = None  # drain retires this exact watched descriptor.
                    self.assertTrue(backend.coverage_lost, "native watch invalidation was silently refreshed")
                finally:
                    if reader is not None:
                        os.close(reader)
                    os.close(writer)


    def test_child_cleanup_exception_cannot_resume_the_native_hook_caller(self):
        observer, deadline = self.modules()
        occurrence = self.occurrence(observer)
        from floati.tui import BoardFilesystemWakeup
        parent_pid = os.getpid()
        escaped = self.fixture.base / "escaped-native-child"
        def broken_close_watch(*args, **kwargs):
            source = BoardFilesystemWakeup(*args, **kwargs)
            native_close = source.close
            def broken_close():
                native_close()
                raise OSError("fixture watch close failed")
            source.close = broken_close
            return source
        with patch("floati.tui.BoardFilesystemWakeup", side_effect=broken_close_watch),\
                patch.object(deadline, "_utc_now", return_value=self.fixture.now + timedelta(seconds=900)),\
                patch.object(deadline, "_monotonic", return_value=occurrence.started_monotonic + 901):
            # Exercise cleanup after publication, not a frozen floating-point
            # threshold: (start + 900) - start can remain just below 900.
            try:
                owner = deadline.start_deadline(occurrence)
            except OSError:
                if os.getpid() != parent_pid:
                    # Contain the deliberately broken OLD fork branch. This
                    # fixture never lets it continue the inherited test runner.
                    escaped.write_text("native child escaped its exit boundary")
                    os._exit(97)
                raise
        self.assertIsNotNone(owner)
        def cleanup_owner():
            try:
                if owner.poll() is None:
                    # Invalidate a failing frozen-clock control before the
                    # retained owner's bounded cancellation/reap attempt.
                    self.clear(observer)
            finally:
                self.assertIsNotNone(owner.cancel(),
                    "owned cleanup remains unproved for native child %s" % owner.pid)
        self.addCleanup(cleanup_owner)
        self.assertEqual(1, self.wait_exit(owner), "child cleanup failure escaped unconditional native exit")
        self.assertFalse(escaped.exists(), "owned child resumed the hook caller")
        self.assertEqual(1, sum(row["kind"] == "message_envelope"
            for row in EventLog(self.fixture.root).event_records()),
            "a cleanup failure must not duplicate the already committed atomic outcome")


if __name__ == "__main__":
    unittest.main()

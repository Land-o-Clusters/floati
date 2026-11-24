"""F2 teardown prerequisite: a native session release must stop Cursor re-arm.

These reconstruct the missing lower-level boundary, not the entire future
seat teardown verb. Fixtures and tests.__init__ keep HOME and fleet scratch.
"""
from tests.readiness import wait_event
import io
import json
import fcntl
import os
from pathlib import Path
import threading
import unittest
from unittest import mock

from floati.codex_wait_contract import CodexWaitSessionLedger
from floati.cursor_wait import run_cursor_stop_wait
from floati.events import EventLog
from floati.wake_control import WakeController
from tests import test_codex_wait as wait_fixture
from tests import test_seat_teardown as teardown_fixture
from tests.waiter_timing import scripted_readiness


class CursorSessionPauseTests(unittest.TestCase):
    def setUp(self):
        self.f = wait_fixture.CodexWaitRuntimeTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.root = self.f.root
        self.workspace = self.f.workspace
        self.node = self.f.participant.binding.node_id
        self.f.arm_session('released-chat')

    def stop(self, session='released-chat', status='completed', on_sleep=None, output=None):
        output = io.StringIO() if output is None else output
        ticks = [0.0]

        def sleep(duration):
            ticks[0] += duration
            if on_sleep is not None:
                on_sleep()

        with mock.patch('floati.cursor_wait.Path.cwd', return_value=self.workspace):
            result = run_cursor_stop_wait(root=self.root.path, node=self.node,
                runtime=self.workspace, deadline_seconds=1, poll_seconds=.1,
                hook_timeout_seconds=10, payload={'conversation_id': session,
                    'status': status, 'loop_count': 0}, stdout=output,
                stderr=io.StringIO(), clock=lambda: ticks[0], sleep=sleep,
                wait_readable=scripted_readiness(sleep, interval=0.1),
                ledger_mtime=lambda: 0)
        self.assertEqual(0, result)
        self.elapsed = ticks[0]
        return json.loads(output.getvalue())

    def test_released_session_cannot_rearm_after_any_native_stop_status(self):
        WakeController(self.root).pause(self.node, 'released-chat',
            idempotency_key='release-this-chat')
        for status in ('completed', 'error', 'aborted'):
            with self.subTest(status=status):
                self.assertEqual({}, self.stop(status=status))

    def test_released_chat_does_not_offer_or_ack_later_mail(self):
        from floati.registry import Registry
        Registry(self.root).register('architect-a', 'architect')
        WakeController(self.root).pause(self.node, 'released-chat',
            idempotency_key='release-before-mail')
        message = EventLog(self.root).send('architect-a', self.node, 'floati',
            'a' * 40, 'docs/evidence/ping.md', 'later mail',
            idempotency_key='later-mail')['message']
        self.assertEqual({}, self.stop())
        self.assertEqual([message['id']], EventLog(self.root).unacked_ids(self.node))
        self.assertFalse((self.root.path / 'receipts/deliveries' /
            (self.node + '.jsonl')).exists())

    def test_unreleased_chat_still_rearms(self):
        self.assertIn('followup_message', self.stop())

    def test_old_session_release_does_not_deafen_the_next_chat(self):
        WakeController(self.root).pause(self.node, 'released-chat',
            idempotency_key='old-chat-only')
        self.f.arm_session('next-chat')
        self.assertIn('followup_message', self.stop(session='next-chat'))
        self.assertEqual({}, self.stop())
        self.assertEqual('superseded_session', self.journal()[-1]['claim_state'])
        self.assertEqual('next-chat', self.journal()[-1]['current_session_id'])

    def test_release_during_hold_exits_at_the_next_poll(self):
        def release():
            WakeController(self.root).pause(self.node, 'released-chat',
                idempotency_key='release-during-hold')
        self.assertEqual({}, self.stop(on_sleep=release))
        self.assertEqual(.1, self.elapsed)
        rows = self.journal()
        self.assertEqual('exit_empty_participation', rows[-1]['event'])
        self.assertEqual('paused', rows[-1]['reason_code'])

    def journal(self):
        path = self.root.path / 'state/cursor-wait' / self.node / 'journal.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_malformed_release_does_not_resurrect_the_chat(self):
        pause = WakeController(self.root).pause(self.node, 'released-chat',
            idempotency_key='malformed-release')
        marker = Path(pause['marker'])
        marker.write_text('{"private-token":"must-not-print"}')
        self.assertEqual({}, self.stop())
        row = self.journal()[-1]
        self.assertEqual('exit_empty_pause_unverified', row['event'])
        self.assertEqual('wake_marker_invalid', row['reason_code'])
        self.assertNotIn('must-not-print', json.dumps(row))

    def test_missing_conversation_cannot_use_a_node_wide_fallback_release(self):
        WakeController(self.root).pause(self.node, 'cursor-stop-' + self.node,
            idempotency_key='fallback-pause')
        self.assertIn('followup_message', self.stop(session=None))

    def test_symlink_release_cannot_hide_an_exact_session_pause(self):
        pause = WakeController(self.root).pause(self.node, 'released-chat',
            idempotency_key='symlink-release')
        marker = Path(pause['marker'])
        marker.unlink()
        for case_index, target in enumerate((self.root.path / 'missing-marker',
                       self.root.path.parent / 'outside-missing-marker')):
            marker.symlink_to(target)
            try:
                for status in ('completed', 'error', 'aborted'):
                    with self.subTest(target_role=(
                        "inside-root-missing-marker",
                        "outside-root-missing-marker",
                    )[case_index], status=status):
                        self.assertEqual({}, self.stop(status=status))
                        self.assertEqual('exit_empty_pause_unverified',
                            self.journal()[-1]['event'])
                        self.assertEqual('wake_marker_invalid',
                            self.journal()[-1]['reason_code'])
            finally:
                marker.unlink()

    def test_dangling_marker_ancestor_cannot_hide_a_paused_session(self):
        pause = WakeController(self.root).pause(self.node, 'released-chat',
            idempotency_key='ancestor-symlink-release')
        marker = Path(pause['marker'])
        original = marker.parent.with_name(marker.parent.name + '-preserved')
        marker.parent.rename(original)
        marker.parent.symlink_to(self.root.path / 'missing-node-directory',
            target_is_directory=True)
        for status in ('completed', 'error', 'aborted'):
            with self.subTest(status=status):
                self.assertEqual({}, self.stop(status=status))
                self.assertEqual('exit_empty_pause_unverified',
                    self.journal()[-1]['event'])
                self.assertEqual('wake_marker_invalid',
                    self.journal()[-1]['reason_code'])
        self.assertTrue((original / marker.name).is_file())

    def test_pause_cannot_commit_in_the_middle_of_continuation_publication(self):
        attempted, finished = threading.Event(), threading.Event()
        errors = []
        order = []
        first_outcome = []
        real_flock = fcntl.flock
        with WakeController(self.root)._lock(self.node):
            pass
        native_lock = self.root.resolve_relative(Path('state/wake-control') / (self.node + '.lock')).stat()

        def observe_flock(descriptor, operation):
            metadata = os.fstat(descriptor)
            watched = (threading.current_thread() is worker and operation == fcntl.LOCK_EX
                and (metadata.st_dev, metadata.st_ino) == (native_lock.st_dev, native_lock.st_ino))
            if watched and not first_outcome:
                # Probe the original FD, then preserve the controller's actual
                # blocking operation. This observes the native lock boundary;
                # it neither substitutes a lock nor relies on a sleep race.
                try:
                    real_flock(descriptor, operation | fcntl.LOCK_NB)
                except BlockingIOError:
                    first_outcome.append('blocked')
                else:
                    first_outcome.append('acquired')
                attempted.set()
            return real_flock(descriptor, operation)

        def pause():
            try:
                WakeController(self.root).pause(self.node, 'released-chat',
                    idempotency_key='concurrent-publication-pause')
                order.append('pause_committed')
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=pause)
        owner = self

        class PausingOutput(io.StringIO):
            def flush(self):
                worker.start()
                owner.assertTrue(wait_event(attempted, 'cursor_session_pause: attempted synchronization'), 'pause did not reach the exact native wake-control flock')
                owner.assertEqual(['blocked'], first_outcome)
                order.append('continuation_flushed')
                return super().flush()

        try:
            with mock.patch('floati.jsonl.fcntl.flock', side_effect=observe_flock):
                self.assertIn('followup_message', self.stop(output=PausingOutput()))
        finally:
            if worker.ident is not None:
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(errors)
        self.assertTrue(finished.is_set())
        self.assertLess(order.index('continuation_flushed'), order.index('pause_committed'))
        self.assertEqual({}, self.stop())


class CursorCompletedTeardownTests(unittest.TestCase):
    """Real scratch teardown receipts, not a mocked completion predicate."""
    stop = CursorSessionPauseTests.stop
    journal = CursorSessionPauseTests.journal

    def setUp(self):
        self.f = teardown_fixture.SeatTeardownTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.root = self.f.root
        self.workspace = self.f.workspace
        self.node = teardown_fixture.NODE
        self.session = teardown_fixture.SESSION

    def assert_torn_down(self, status='completed', **kwargs):
        self.assertEqual({}, self.stop(session=self.session, status=status, **kwargs))
        row = self.journal()[-1]
        self.assertEqual('exit_empty_teardown', row['event'])
        self.assertNotIn('publication', row)

    def test_exact_completed_teardown_suppresses_every_native_stop_status(self):
        result = self.f.teardown()
        for status in ('completed', 'error', 'aborted'):
            with self.subTest(status=status):
                self.assert_torn_down(status)
                self.assertEqual(result['id'], self.journal()[-1]['teardown_receipt_id'])

    def test_completion_during_wait_is_rechecked_without_event_change(self):
        self.assert_torn_down(on_sleep=lambda: self.f.teardown())
        self.assertEqual(.1, self.elapsed)

    def test_completed_chat_preserves_later_mail_for_successor(self):
        self.f.teardown()
        message = self.f.send('later-cursor-mail')
        self.assert_torn_down()
        self.assertEqual([message['id']], EventLog(self.root).unacked_ids(self.node))
        self.assertFalse((self.root.path / 'receipts/deliveries' / (self.node + '.jsonl')).exists())
        CodexWaitSessionLedger(self.root).arm(self.f.binding, self.f.consent, 'next-chat',
            idempotency_key='cursor-successor', take_over=True)
        self.assertIn('followup_message', self.stop(session='next-chat'))
        self.assertEqual({}, self.stop(session=self.session))
        self.assertEqual('superseded_session', self.journal()[-1]['claim_state'])
        self.assertEqual('next-chat', self.journal()[-1]['current_session_id'])

    def test_exact_paused_chat_with_corrupt_teardown_ledger_cannot_continue(self):
        self.f.teardown()
        ledger = self.root.resolve_relative(Path('receipts/seat-teardown') / (self.node + '.jsonl'))
        ledger.write_text('{"private-token":"must-not-print"}\n')
        for status in ('completed', 'error'):
            with self.subTest(status=status):
                body = self.stop(session=self.session, status=status)
                self.assertEqual({}, body)
                row = self.journal()[-1]
                self.assertEqual('exit_empty_pause_unverified', row['event'])
                self.assertEqual('seat_teardown_unverified', row['reason_code'])
                self.assertNotIn('publication', row)
                self.assertNotIn('must-not-print', json.dumps(row))

    def test_unpaused_chat_with_corrupt_teardown_ledger_keeps_typed_diagnostic(self):
        ledger = self.root.resolve_relative(Path('receipts/seat-teardown') / (self.node + '.jsonl'))
        ledger.parent.mkdir(parents=True)
        ledger.write_text('{"private-token":"must-not-print"}\n')
        body = self.stop(session=self.session, status='error')
        self.assertIn('followup_message', body)
        self.assertEqual('followup_participation_refused', self.journal()[-1]['event'])
        self.assertNotIn('must-not-print', json.dumps(body))

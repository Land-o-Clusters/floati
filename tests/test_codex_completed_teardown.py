"""Codex Stop uses real scratch teardown receipts and the native pause lock."""
from tests.readiness import wait_event, wait_join
from contextlib import contextmanager
import fcntl
import io
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest import mock

from floati.codex_wait import run_stop_waiter
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger
from floati.events import EventLog
from floati.errors import ProtocolRefusal
from floati.cursor import SparseCursor
from floati.jsonl import read_records_snapshot
from floati.wake_control import WakeController
from tests import test_seat_teardown as fixture
from tests.waiter_timing import scripted_readiness


class CodexCompletedTeardownTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SeatTeardownTests()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.root, self.node = self.f.root, fixture.NODE
        self.workspace, self.session = self.f.workspace, fixture.SESSION

    def stop(self, session=None, on_sleep=None, output=None, native_time=False):
        self.output = io.StringIO() if output is None else output
        self.errors, self.result = io.StringIO(), {}
        ticks = [0.0]

        def sleep(seconds):
            ticks[0] += seconds
            if on_sleep is not None:
                on_sleep()

        status = run_stop_waiter(bus_home=self.root.tenant_home,
            hook_payload={'cwd': str(self.workspace), 'session_id': session or self.session},
            stdout=self.output, stderr=self.errors,
            monotonic=time.monotonic if native_time else lambda: ticks[0],
            wait_readable=scripted_readiness((time.sleep if native_time else sleep), interval=(.1)), sleep=time.sleep if native_time else sleep,
            poll_interval_seconds=.1, _waiter_result=self.result)
        self.assertEqual(0, status)
        self.elapsed = ticks[0]
        return self.output.getvalue()

    def exits(self):
        return read_records_snapshot(self.root, Path('receipts/wake-waiter-exit') /
            (self.node + '.jsonl'), allowed_kinds={'wake_waiter_exit_receipt'})

    def assert_completed(self, receipt, **kwargs):
        self.assertIn(self.stop(**kwargs), ('', '{}\n'))
        self.assertEqual('completed_teardown', self.result['kind'],
            {'result': self.result, 'stderr': self.errors.getvalue()})
        self.assertEqual(receipt['id'], self.result['teardown_receipt_id'])
        self.assertEqual({}, self.result['decision'])
        self.assertEqual('paused', self.exits()[-1]['reason_code'])

    def test_completed_receipt_returns_typed_empty_without_new_reason_enum(self):
        self.assert_completed(self.f.teardown())

    def test_startup_native_contention_retains_completed_teardown_type(self):
        self.completed_startup_contention(native_time=True)

    def test_startup_exhausted_window_retains_completed_teardown_type(self):
        self.completed_startup_contention(native_time=False)

    def completed_startup_contention(self, *, native_time):
        receipt = self.f.teardown()
        relative = CodexWaitSessionLedger._relative(self.node)
        lock = self.root.resolve_relative(relative.with_suffix(relative.suffix + '.lock'))
        release, worker = self.hold_startup_claim_lock()
        from floati import jsonl
        original_lock = jsonl._locked_path

        @contextmanager
        def release_after_native_contention(path, **kwargs):
            try:
                with original_lock(path, **kwargs):
                    yield
            except ProtocolRefusal as failure:
                # Recovery starts after an observed real acquisition timeout,
                # never after an independent holder's startup timer expires.
                if native_time and path == lock and failure.code == 'ledger_lock_timeout':
                    release.set()
                    wait_join(worker, 'completed teardown contended holder release')
                raise

        try:
            with mock.patch.object(jsonl, '_locked_path', release_after_native_contention):
                self.assert_completed(receipt, native_time=native_time)
        finally:
            release.set()
            wait_join(worker, 'completed teardown owned holder')

    def test_completed_chat_preserves_later_mail_for_successor(self):
        receipt = self.f.teardown()
        message = self.f.send('later-codex-mail')
        self.assert_completed(receipt)
        self.assertEqual([message['id']], EventLog(self.root).unacked_ids(self.node))
        self.assertFalse((self.root.path / 'receipts/deliveries' / (self.node + '.jsonl')).exists())
        CodexWaitSessionLedger(self.root).arm(self.f.binding, self.f.consent, 'successor',
            idempotency_key='codex-successor', take_over=True)
        self.assertIn(message['id'], self.stop(session='successor'))
        self.assertEqual('fresh_work', self.result['kind'])
        self.assertEqual('{}\n', self.stop())
        self.assertEqual('superseded_session', self.result['reason'])

    def test_completion_during_wait_is_rechecked_before_deadline(self):
        completed = []
        def teardown():
            if not completed:
                completed.append(self.f.teardown())
        self.stop(on_sleep=teardown)
        self.assertEqual('completed_teardown', self.result['kind'])
        self.assertEqual(completed[0]['id'], self.result['teardown_receipt_id'])
        self.assertEqual(.1, self.elapsed)

    def test_ordinary_pause_keeps_current_semantics(self):
        WakeController(self.root).pause(self.node, self.session, idempotency_key='ordinary-pause')
        self.assertEqual('', self.stop())
        self.assertEqual('authority_changed', self.result['kind'])
        self.assertEqual('paused', self.result['reason'])
        self.assertNotIn('teardown_receipt_id', self.result)

    def test_malformed_exact_marker_is_empty_and_redacted(self):
        pause = WakeController(self.root).pause(self.node, self.session, idempotency_key='bad-marker')
        Path(pause['marker']).write_text('{"private-token":"must-not-print"}')
        self.assertIn(self.stop(), ('', '{}\n'))
        self.assertEqual('pause_unverified', self.result['reason'])
        self.assertNotIn('must-not-print', self.output.getvalue() + self.errors.getvalue())

    def test_valid_exact_pause_with_corrupted_teardown_history_stays_empty(self):
        self.f.teardown()
        path = self.root.path / 'receipts/seat-teardown' / (self.node + '.jsonl')
        path.write_text('{"private-token":"must-not-print"}\n')
        self.assertIn(self.stop(), ('', '{}\n'))
        self.assertEqual('pause_unverified', self.result['reason'])
        self.assertEqual('integrity_failure', self.exits()[-1]['reason_code'])
        self.assertNotIn('must-not-print', self.output.getvalue() + self.errors.getvalue())

    def fault_input(self):
        message = self.f.send('diagnostic-fault-mail')
        path = self.root.path / 'receipts/cursor-presentations' / (self.node + '.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('not-json\n')
        self.assertIn('integrity refusal', self.stop())
        return message

    def fault_hold_terminal(self, *, teardown):
        message = self.fault_input()
        effects = []
        def act():
            if effects:
                return
            if teardown:
                EventLog(self.root).present(self.node)
                SparseCursor(self.root).ack(self.node, [message['id']], acting_session_id=self.session)
                effects.append(self.f.teardown())
            else:
                effects.append(WakeController(self.root).pause(self.node, self.session,
                    idempotency_key='pause-during-diagnostic-hold'))
        self.assertIn(self.stop(on_sleep=act), ('', '{}\n'))
        self.assertTrue(effects)
        self.assertLessEqual(self.elapsed, 2)
        if teardown:
            self.assertEqual('completed_teardown', self.result['kind'])
            self.assertEqual(effects[0]['id'], self.result['teardown_receipt_id'])
        else:
            self.assertEqual('paused', self.result['reason'])

    def test_native_pause_during_diagnostic_hold_cannot_publish_continuation(self):
        self.fault_hold_terminal(teardown=False)

    def test_real_teardown_during_diagnostic_hold_returns_typed_empty(self):
        self.fault_hold_terminal(teardown=True)

    def test_diagnostic_terminal_flush_owns_exact_native_pause_lock(self):
        self.fault_input()
        self.assert_native_pause_serialized(lambda output: self.stop(output=output))

    def test_initial_malformed_consent_flush_owns_exact_native_pause_lock(self):
        self.root.resolve_relative(CodexWaitConsentLedger._relative(self.node)).write_text('not-json\n')
        self.assert_native_pause_serialized(lambda output: self.stop(output=output))

    def test_pause_during_known_corrupt_consent_hold_stays_empty_unverified(self):
        path = self.root.resolve_relative(CodexWaitConsentLedger._relative(self.node))
        original = path.read_bytes()
        self.assertIn('integrity refusal', self.stop(on_sleep=lambda: path.write_bytes(b'{')))
        path.write_bytes(original)
        steps = []
        def corrupt_then_pause():
            if not steps:
                path.write_bytes(b'{')
                steps.append('corrupt')
            elif steps == ['corrupt']:
                WakeController(self.root).pause(self.node, self.session,
                    idempotency_key='pause-after-inflight-consent-fault')
                steps.append('pause')
        self.assertIn(self.stop(on_sleep=corrupt_then_pause), ('', '{}\n'))
        self.assertEqual(['corrupt', 'pause'], steps)
        self.assertEqual('pause_unverified', self.result['reason'])
        self.assertEqual('', self.errors.getvalue())

    def test_lock_exhaustion_terminal_flush_owns_exact_native_pause_lock(self):
        self.hold_startup_claim_lock()
        self.assert_native_pause_serialized(lambda output: self.stop(output=output))

    def test_lock_exhaustion_output_failure_is_not_guard_unavailability(self):
        self.hold_startup_claim_lock()
        class BrokenFlush(io.StringIO):
            def flush(self):
                raise BrokenPipeError('closed scratch output')
        output = BrokenFlush()
        with self.assertRaises(BrokenPipeError):
            self.stop(output=output)
        self.assertEqual(1, len(output.getvalue().splitlines()))

    def hold_startup_claim_lock(self):
        relative = CodexWaitSessionLedger._relative(self.node)
        lock = self.root.resolve_relative(relative.with_suffix(relative.suffix + '.lock'))
        ready, release = threading.Event(), threading.Event()
        def hold():
            with lock.open('a+b') as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                ready.set()
                wait_event(release, 'completed teardown claim holder release')
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        worker = threading.Thread(target=hold)
        worker.start()
        self.addCleanup(wait_join, worker, 'completed teardown claim holder cleanup')
        self.addCleanup(release.set)
        self.assertTrue(wait_event(ready, "test_codex_completed_teardown ready"))
        return release, worker

    def test_dangling_marker_or_ancestor_cannot_resurrect_session(self):
        pause = WakeController(self.root).pause(self.node, self.session, idempotency_key='symlink-marker')
        marker = Path(pause['marker'])
        marker.unlink()
        marker.symlink_to(self.root.path / 'absent-marker')
        self.assertIn(self.stop(), ('', '{}\n'))
        self.assertEqual('pause_unverified', self.result['reason'])
        marker.unlink()
        saved = marker.parent.with_name(marker.parent.name + '-saved')
        marker.parent.rename(saved)
        marker.parent.symlink_to(self.root.path / 'absent-directory', target_is_directory=True)
        self.assertIn(self.stop(), ('', '{}\n'))
        self.assertEqual('pause_unverified', self.result['reason'])

    def test_pause_cannot_commit_during_exact_native_mail_flush(self):
        message = self.f.send('codex-publication-race')
        self.assert_native_pause_serialized(lambda output: self.stop(output=output), message['id'])

    def assert_native_pause_serialized(self, invoke, expected=None):
        attempted, finished = threading.Event(), threading.Event()
        failures, first_outcome, order = [], [], []
        real_flock = fcntl.flock
        with WakeController(self.root)._lock(self.node):
            pass
        lock = self.root.resolve_relative(Path('state/wake-control') / (self.node + '.lock')).stat()

        def observe(descriptor, operation):
            metadata = os.fstat(descriptor)
            watched = (threading.current_thread() is worker and operation == fcntl.LOCK_EX
                and (metadata.st_dev, metadata.st_ino) == (lock.st_dev, lock.st_ino))
            if watched and not first_outcome:
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
                WakeController(self.root).pause(self.node, self.session, idempotency_key='during-flush')
                order.append('pause_committed')
            except BaseException as exc:
                failures.append(exc)
            finally:
                finished.set()

        worker = threading.Thread(target=pause)
        owner = self
        class PausingOutput(io.StringIO):
            def flush(self):
                worker.start()
                owner.assertTrue(wait_event(attempted, "test_codex_completed_teardown attempted"), 'pause never reached exact native flock')
                owner.assertEqual(['blocked'], first_outcome)
                order.append('mail_flushed')
                return super().flush()
        try:
            with mock.patch('floati.jsonl.fcntl.flock', side_effect=observe):
                observed = invoke(PausingOutput())
                if expected is not None:
                    self.assertIn(expected, observed)
        finally:
            if worker.ident is not None:
                wait_join(worker, "test_codex_completed_teardown worker")
        self.assertFalse(worker.is_alive())
        self.assertFalse(failures)
        self.assertTrue(finished.is_set())
        self.assertEqual(['blocked'], first_outcome)
        self.assertLess(order.index('mail_flushed'), order.index('pause_committed'))


if __name__ == '__main__':
    unittest.main()

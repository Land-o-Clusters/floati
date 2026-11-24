"""Teardown retires its exact native hook before observing workspace jobs."""
import unittest
import subprocess
from unittest.mock import patch

from floati.codex_wait_liveness import process_start_epoch
from floati.errors import ProtocolRefusal
from floati.registry import Registry
from floati.records import validate_record
from floati.seat_teardown import SeatTeardown
from floati.waiter_invocation import _paths, _read
from tests import test_seat_teardown as fixture
from tests.cursor_invocation_fixture import CursorInvocationFixture


class SeatTeardownLiveWaiterTests(unittest.TestCase):
    def setUp(self):
        register = Registry.register
        def cursor_seat(registry, node, role):
            return register(registry, node, 'Cursor' if node == fixture.NODE else role)
        with patch.object(Registry, 'register', cursor_seat):
            fixture.SeatTeardownTests.setUp(self)
        self.waiter = CursorInvocationFixture(self, root=self.root,
            workspace=self.workspace, node=fixture.NODE, session=fixture.SESSION)

    def test_live_waiter_exits_with_correlated_receipt_before_teardown_completes(self):
        job = self.waiter.start()
        self.waiter.wait_started()
        pid = job['process'].pid
        birth = process_start_epoch(pid)
        start = self.waiter.rows()[0]
        _, state, _ = _paths(self.root, fixture.NODE, fixture.SESSION)

        def start_is_bound():
            lease = _read(state)
            return (lease is not None and lease['phase'] == 'running'
                and lease['pid'] == pid and lease['invocation_id'] == start['invocation_id'])

        # The receipt is visible before its invocation ID is published in the lease.
        self.waiter.wait_until(start_is_bound, 'native hold start was not bound to the live waiter')
        completed = SeatTeardown(self.root).teardown(fixture.NODE, self.home,
            fixture.SESSION, repo='floati', doc='report.md', idempotency_key='live-teardown')
        self.assertEqual('completed', completed['step'])
        self.assertEqual(0, job['process'].poll())
        self.assertNotEqual(birth, process_start_epoch(pid))
        exits = [row for row in self.waiter.rows() if row['phase'] == 'exit']
        self.assertEqual(1, len(exits))
        self.assertEqual(start['invocation_id'], exits[0]['invocation_id'])
        retired = completed['activity']['retired_waiters']
        self.assertEqual(1, len(retired))
        self.assertEqual(pid, retired[0]['pid'])
        self.assertEqual(birth, retired[0]['process_start_epoch'])
        self.assertEqual(start['id'], retired[0]['start_receipt_id'])
        self.assertEqual(exits[0]['id'], retired[0]['exit_receipt_id'])
        self.assertEqual('teardown_requested', retired[0]['reason'])

    def _refusal_preserves_live_waiter(self, job, *, doc='report.md'):
        with self.assertRaises(ProtocolRefusal):
            SeatTeardown(self.root).teardown(fixture.NODE, self.home,
                fixture.SESSION, repo='floati', doc=doc, idempotency_key='refused-live-teardown')
        self.assertIsNone(job['process'].poll(), 'a refused preflight must preserve its native hold')
        self.assertEqual(['start'], [row['phase'] for row in self.waiter.rows()])

    def test_dirty_data_refusal_does_not_retire_live_waiter(self):
        waiter = self.waiter.start()
        self.waiter.wait_started()
        report = self.workspace / 'report.md'
        original = report.read_bytes()
        for precondition in ('dirty-data', 'missing-report', 'foreign-job'):
            with self.subTest(precondition=precondition):
                job = None
                if precondition == 'dirty-data':
                    report.write_text('unfinished work\n')
                elif precondition == 'foreign-job':
                    job = subprocess.Popen(['/bin/sleep', '30'], cwd=self.workspace)
                try:
                    self._refusal_preserves_live_waiter(waiter,
                        doc='missing.md' if precondition == 'missing-report' else 'report.md')
                finally:
                    report.write_bytes(original)
                    if job is not None:
                        if job.poll() is None:
                            job.terminate()
                        job.wait(timeout=5)

    def test_oversized_process_birth_has_typed_record_refusal(self):
        record = SeatTeardown(self.root).teardown(fixture.NODE, self.home,
            fixture.SESSION, repo='floati', doc='report.md', idempotency_key='birth-record')
        record['activity']['native_host_workers'] = [{'pid': 123,
            'process_start_epoch': 10**1000, 'executable': '/managed/cursor-agent',
            'reason': 'cursor_managed_agent_exact_workspace'}]
        with self.assertRaises(ProtocolRefusal) as caught:
            validate_record(record, self.root.tenant_id, {'seat_teardown_receipt'}, integrity=False)
        self.assertEqual('seat_teardown_record_invalid', caught.exception.code)

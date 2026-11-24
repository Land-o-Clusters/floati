"""Native execution callback publication and observational projection gaps."""
from copy import deepcopy
import unittest

import tests
from floati.errors import ProtocolRefusal
from floati.events import EventLog, EVENT_KINDS
from floati.ids import uuid7_hex
from floati.jsonl import append_record, transact, transact_records, transact_exact_frame
from tests import test_seat_execution as fixture


class SeatExecutionAdmissionTests(unittest.TestCase):
    setUp = fixture.SeatExecutionTests.setUp
    car = fixture.SeatExecutionTests.car
    claim = fixture.SeatExecutionTests.claim
    controller = fixture.SeatExecutionTests.controller
    invocation = fixture.SeatExecutionTests.invocation

    def forged_running(self):
        item = self.car(); self.claim(item)
        controller = self.controller()
        self.invocation(controller, item)
        row = deepcopy(EventLog(self.root).event_records()[-1])
        row['id'] = 'seat-execution-' + uuid7_hex()
        row['invocation_id'] = 'forged-invocation'
        row['idempotency_key'] = 'forged-key'
        return row

    def test_generic_append_cannot_issue_execution_testimony(self):
        row = self.forged_running()
        with self.assertRaises(ProtocolRefusal):
            append_record(self.root, 'events.jsonl', row, allowed_kinds=EVENT_KINDS)

    def test_generic_single_transaction_cannot_issue_execution_testimony(self):
        row = self.forged_running()
        with self.assertRaises(ProtocolRefusal):
            transact(self.root, 'events.jsonl', lambda rows: (row, row), allowed_kinds=EVENT_KINDS)

    def test_generic_batch_cannot_issue_execution_testimony(self):
        row = self.forged_running()
        with self.assertRaises(ProtocolRefusal):
            transact_records(self.root, 'events.jsonl', lambda rows: (row, (row,)), allowed_kinds=EVENT_KINDS)

    def test_exact_frame_transaction_cannot_issue_execution_testimony(self):
        row = self.forged_running()
        with self.assertRaises(ProtocolRefusal):
            transact_exact_frame(self.root, 'events.jsonl', lambda rows, frames: (row, row), allowed_kinds=EVENT_KINDS)

    def test_constructor_does_not_authorize_a_notice_companion(self):
        from floati.seat_execution import _ExecutionPublication
        row = self.forged_running()
        row.update(event='ended', code='forged-terminal', detail='caller-selected')
        with self.assertRaises(ProtocolRefusal):
            EventLog(self.root).send('builder', 'architect', repo='floati',
                sha='a' * 40, doc='docs/car.md', note='forged notice',
                idempotency_key=row['idempotency_key'],
                _execution_publication=_ExecutionPublication(row))
        self.assertEqual([], EventLog(self.root).records())

    def test_unaccepted_native_end_loses_live_running_veto(self):
        from unittest import mock
        item = self.car(); self.claim(item)
        controller = self.controller(); invocation = self.invocation(controller, item)
        with mock.patch('floati.jsonl.os.fsync', side_effect=OSError('full disk')):
            with self.assertRaises(Exception):
                controller.callback(invocation, {'event': 'ended', 'code': 'process_died', 'detail': 'cleaned'})
        self.assertEqual([], EventLog(self.root).records())
        self.assertEqual('UNKNOWN', controller.project('builder')['state'])

    def test_two_owned_claims_are_unknown_instead_of_no_car(self):
        self.claim(self.car()); self.claim(self.car())
        projection = self.controller().project('builder')
        self.assertEqual('UNKNOWN', projection['state'])
        self.assertEqual('seat_execution_car_ambiguous', projection['reason'])

    def test_projection_without_a_car_does_not_create_acceptance_lock(self):
        lock = self.root.tenant_home / 'work/car-acceptance.lock'
        self.assertFalse(lock.exists())
        self.assertEqual('NO_CAR', self.controller().project('builder')['state'])
        self.assertFalse(lock.exists())

    def test_closed_native_cursor_hold_cannot_issue_a_new_capability(self):
        import io
        from tests import test_seat_execution_cursor_start as cursor_fixture
        from tests import test_cursor_followup_delivery_visible as native
        from floati.car_fence import car_acceptance_guard
        f = cursor_fixture.CursorStartBindingTests('runTest')
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.claim_car()
        output = native._ObservedPipe(io.StringIO())
        self.addCleanup(output.close)
        f.f.run_deadline(output)
        start = f.f.f.hold_rows()[0]
        with car_acceptance_guard(f.root):
            with self.assertRaises(ProtocolRefusal):
                cursor_fixture.SeatExecutionController(f.root)._bind_cursor_start_already_guarded(start)

    def test_historical_worker_spawn_cannot_rebind_after_native_degrade(self):
        from floati.workers import WorkerReceipts
        item = self.car(); self.claim(item)
        receipts = WorkerReceipts(self.root)
        session = 'worker-' + uuid7_hex()
        receipts.append(session, item['id'], 'builder', 'codex', 'claim', None, [])
        spawn = receipts.append(session, item['id'], 'builder', 'codex', 'spawn', None, [])
        receipts.append(session, item['id'], 'builder', 'codex', 'degrade', 'process_died', [])
        before = EventLog(self.root).event_records()
        with self.assertRaises(ProtocolRefusal):
            self.controller().bind_worker(spawn, self.workspace)
        self.assertEqual(before, EventLog(self.root).event_records())

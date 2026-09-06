"""Boarding orders authority, resume and consumption without replaying mail."""
import unittest
from unittest import mock
from floati import fixture_ids as ids
from floati.codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from floati.errors import ProtocolRefusal
from floati.events import EventLog
from floati.registry import Registry
from floati.wake_control import WakeController
from tests import test_codex_wait as fixture

class SeatBoardTests(unittest.TestCase):
    write_map = fixture.CodexWaitContractTests.write_map
    send = fixture.CodexWaitRuntimeTests.send
    arm_session = fixture.CodexWaitRuntimeTests.arm_session
    def setUp(self):
        fixture.CodexWaitContractTests.setUp(self)
        from tests import test_workspace_seat_declaration as declarations
        declarations.WorkspaceSeatDeclarationTests.write_seat_fixture(self, self.workspace, self.root.path, ids.builder('floati'))
        Registry(self.root).register('architect', 'architect')
        self.write_map([{'workspace': str(self.workspace), 'node_id': ids.builder('floati')}])
        self.participant = resolve_participant(self.bus_home, self.workspace)
        CodexWaitConsentLedger(self.root).arm(self.participant.binding, hook_timeout_seconds=10,
            wait_deadline_seconds=2, idempotency_key='board-consent')
    def board(self, session='new-session', key='board-test', **kwargs):
        from floati.seat_board import SeatBoard
        return SeatBoard(self.root).board(ids.builder('floati'), self.workspace, session,
            idempotency_key=key, **kwargs)
    def test_board_drains_after_arm_and_completed_replay_does_not_drain_new_mail(self):
        first = self.send('first')
        result = self.board()
        self.assertEqual([first['id']], result['item_ids'])
        self.assertEqual('already_active', result['resume_outcome'])
        self.send('second')
        self.assertEqual(result, self.board())
        self.assertEqual(1, len(EventLog(self.root).present(ids.builder('floati'))[0]))
    def test_contested_claim_requires_explicit_takeover(self):
        prior = self.arm_session('prior-session')
        with self.assertRaises(ProtocolRefusal) as caught:
            self.board()
        self.assertEqual('seat_board_claim_contested', caught.exception.code)
        self.assertIn('--take-over', caught.exception.detail)
        result = self.board(take_over=True)
        rows = CodexWaitSessionLedger(self.root)
        self.assertEqual(prior['id'], result['predecessor_receipt_id'])
    def test_undeclared_workspace_refuses(self):
        from floati.seat_board import SeatBoard
        with self.assertRaises(ProtocolRefusal) as caught:
            SeatBoard(self.root).board(ids.builder('floati'), self.base, 'new-session', idempotency_key='bad')
        self.assertEqual('seat_board_workspace_undeclared', caught.exception.code)
    def test_paused_session_resumes_before_drain(self):
        WakeController(self.root).pause(ids.builder('floati'), 'new-session', idempotency_key='pause')
        self.assertEqual('resumed', self.board()['resume_outcome'])
    def test_drain_crash_refuses_replay_instead_of_consuming_again(self):
        self.send('first')
        with mock.patch.object(EventLog, '_present_already_guarded', side_effect=OSError('crash')):
            with self.assertRaises(OSError): self.board()
        with self.assertRaises(ProtocolRefusal) as caught: self.board()
        self.assertEqual('seat_board_drain_unknown', caught.exception.code)
    def test_arm_replay_after_displacement_cannot_drain(self):
        from floati.seat_board import SeatBoard
        with mock.patch.object(SeatBoard, '_resume', side_effect=OSError('crash')):
            with self.assertRaises(OSError): self.board()
        self.arm_session('displacing-session')
        with self.assertRaises(ProtocolRefusal) as caught: self.board()
        self.assertEqual('seat_board_claim_contested', caught.exception.code)

    def test_waiter_map_without_seat_declaration_refuses(self):
        (self.workspace / 'SEAT.json').unlink()
        with self.assertRaises(ProtocolRefusal) as caught: self.board()
        self.assertEqual('seat_board_workspace_undeclared', caught.exception.code)
    def test_mismatched_seat_declaration_refuses(self):
        import json
        path = self.workspace / 'SEAT.json'
        row = json.loads(path.read_text())
        row['node_id'] = 'different-builder'
        path.write_text(json.dumps(row))
        with self.assertRaises(ProtocolRefusal) as caught: self.board()
        self.assertEqual('workspace_identity_mismatch', caught.exception.code)
    def test_ordinary_arm_waits_until_board_finishes_consumption(self):
        import threading
        self.send('concurrent')
        attempted, finished = threading.Event(), threading.Event()
        failures = []
        def contender():
            attempted.set()
            try: self.arm_session('other-session')
            except Exception as exc: failures.append(exc)
            finally: finished.set()
        real = EventLog._present_already_guarded
        threads = []
        def present(log, *args, **kwargs):
            thread = threading.Thread(target=contender)
            threads.append(thread)
            thread.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(finished.wait(.1))
            return real(log, *args, **kwargs)
        with mock.patch.object(EventLog, '_present_already_guarded', present): self.board()
        for thread in threads: thread.join(2)
        self.assertTrue(finished.is_set())
        self.assertFalse(failures)
    def test_resume_completion_crash_reuses_native_receipt(self):
        from floati.seat_board import SeatBoard
        WakeController(self.root).pause(ids.builder('floati'), 'new-session', idempotency_key='pause')
        real = SeatBoard._append
        def append(board, path, row):
            if row['step'] == 'resumed': raise OSError('crash after resume')
            return real(board, path, row)
        with mock.patch.object(SeatBoard, '_append', append):
            with self.assertRaises(OSError): self.board()
        result = self.board()
        self.assertEqual('resumed', result['resume_outcome'])
        self.assertTrue(result['resume_receipt_id'].startswith('wake-control-'))
    def test_progress_schema_and_native_validation_reject_false_evidence(self):
        from pathlib import Path
        from floati.jsonl import read_records_snapshot
        from floati.records import validate_record
        from tests.schema_validation import validate_json_schema
        self.board()
        rows = read_records_snapshot(self.root, 'receipts/seat-board/' + ids.builder('floati') + '.jsonl', allowed_kinds={'seat_board_receipt'})
        self.assertEqual(['started','armed','resumed','drain_started','completed'], [r['step'] for r in rows])
        schema = Path('schemas/v1/seat-board-record.schema.json')
        for row in rows: validate_json_schema(row, schema)
        for changes in ({'resume_outcome':'resumed','resume_receipt_id':None}, {'arm_receipt_id':None}):
            row = dict(rows[-1], **changes)
            with self.assertRaises(ProtocolRefusal): validate_record(row,self.root.tenant_id,{'seat_board_receipt'},integrity=False)
            with self.assertRaises(AssertionError): validate_json_schema(row,schema)
    def test_replay_rejects_batch_that_does_not_match_native_receipts(self):
        import json
        from floati.errors import IntegrityFailure
        self.send('first')
        self.board()
        second = self.send('second')
        path = self.root.resolve_relative('receipts/seat-board/' + ids.builder('floati') + '.jsonl')
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[-1]['item_ids'] = [second['id']]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        with self.assertRaises(IntegrityFailure) as caught: self.board()
        self.assertEqual('seat_board_progress_invalid', caught.exception.code)

    def test_new_pause_invalidates_resumed_progress_before_consumption(self):
        from floati.seat_board import SeatBoard
        self.send('paused-retry')
        real = SeatBoard._append
        def append(board, path, row):
            if row['step'] == 'drain_started': raise OSError('crash before drain intent')
            return real(board, path, row)
        with mock.patch.object(SeatBoard, '_append', append):
            with self.assertRaises(OSError): self.board()
        WakeController(self.root).pause(ids.builder('floati'), 'new-session', idempotency_key='new-pause')
        with self.assertRaises(ProtocolRefusal) as caught: self.board()
        self.assertEqual('seat_board_resume_stale', caught.exception.code)
        self.assertFalse(self.root.resolve_relative('receipts/acks/' + ids.builder('floati') + '.jsonl').exists())
    def test_cli_requires_explicit_session_even_with_runtime_identity(self):
        import contextlib, io, json, os
        from floati.cli import main
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'runtime-session'}), contextlib.redirect_stdout(output):
            result = main(['seat','board','--root',str(self.root.path),'--as',ids.builder('floati'),
                '--workspace',str(self.workspace),'--idempotency-key','cli-refused'])
        artifact = json.loads(output.getvalue())
        self.assertEqual(20, result, artifact)
        self.assertEqual('arguments_invalid', artifact['evidence']['code'])
        self.assertIn('--session', artifact['evidence']['detail'])
        self.assertFalse(self.root.resolve_relative('receipts/seat-board').exists())

    def test_cli_boards_using_declared_session_and_returns_messages(self):
        import contextlib, io, json, os
        from floati.cli import main
        message = self.send('cli-message')
        output = io.StringIO()
        with mock.patch.dict(os.environ, {'CODEX_THREAD_ID': 'runtime-session'}), contextlib.redirect_stdout(output):
            result = main(['seat','board','--root',str(self.root.path),'--as',ids.builder('floati'),
                '--workspace',str(self.workspace),'--idempotency-key','cli-board','--session','declared-session'])
        artifact = json.loads(output.getvalue())
        self.assertEqual(0, result, artifact)
        self.assertEqual('declared-session', artifact['evidence']['acting_session_id'])
        self.assertEqual([message['id']], artifact['evidence']['item_ids'])
        self.assertEqual([message], artifact['evidence']['messages'])

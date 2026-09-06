"""Explicit, receipted boarding; native claim receipts remain the authority."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from .bus_epoch import shared_epoch_operation
from .codex_wait_contract import CodexWaitConsentLedger, CodexWaitSessionLedger, resolve_participant
from .cursor import SparseCursor
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EventLog
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .registry import utc_now
from .seat_declaration import require_declared_coordinate
from .wake_control import WakeController, validate_session_id
from .wake_hold import wake_coordination_guard, _validate_wake_key

KIND = 'seat_board_receipt'
STEPS = ('started', 'armed', 'resumed', 'drain_started', 'completed')

@contextmanager
def _step(name):
    try:
        yield
    except ProtocolRefusal as exc:
        raise ProtocolRefusal(exc.code, "boarding " + name + " step: " + exc.detail, exc.remedy) from exc

def _validate_progress(rows):
    if not rows:
        return
    if [r['step'] for r in rows] != list(STEPS[:len(rows)]):
        raise IntegrityFailure('seat_board_progress_invalid', 'boarding step order is inconsistent')
    for prior, row in zip(rows, rows[1:]):
        for field in ('node_id', 'acting_session_id', 'coordinate_digest', 'idempotency_key'):
            if prior[field] != row[field]:
                raise IntegrityFailure('seat_board_progress_invalid', 'boarding coordinate changed between steps')
        for field in ('arm_receipt_id', 'predecessor_receipt_id', 'resume_receipt_id', 'resume_outcome'):
            if prior[field] is not None and prior[field] != row[field]:
                raise IntegrityFailure('seat_board_progress_invalid', 'boarding native evidence changed between steps')

class SeatBoard:
    def __init__(self, root):
        self.root = root

    def _append(self, relative, row):
        def decide(prior):
            matching = [r for r in prior if r['idempotency_key'] == row['idempotency_key']]
            if matching and matching[-1]['step'] == row['step']:
                return matching[-1], None
            _validate_progress([*matching, row])
            return row, row
        return transact(self.root, relative, decide, allowed_kinds={KIND})

    def _resume(self, node, session, key):
        controller = WakeController(self.root)
        if controller.status(node, session)['state'] == 'paused':
            return 'resumed', controller.resume(node, session, idempotency_key=key)['receipt']['id']
        prior = read_records_snapshot(self.root, controller._ledger(node), allowed_kinds={'wake_control_receipt'})
        match = next((r for r in reversed(prior) if r['idempotency_key'] == key), None)
        if match is not None:
            expected_digest = controller._identity(node, session)[2]
            if match['operation'] != 'resume' or match['state'] != 'resume_requested' or match['session_digest'] != expected_digest:
                raise ProtocolRefusal('seat_board_resume_conflict',
                    'resume key names different native evidence; inspect the wake control receipt')
            return 'resumed', match['id']
        return 'already_active', None

    @shared_epoch_operation
    def board(self, node, workspace, session, *, idempotency_key, take_over=False):
        session = validate_session_id(session)
        key = _validate_wake_key(idempotency_key)
        participant = resolve_participant(self.root.tenant_home, Path(workspace))
        if participant is None or participant.binding.node_id != node:
            raise ProtocolRefusal('seat_board_workspace_undeclared',
                'workspace is not declared for this node; install its governed waiter binding before boarding')
        binding = participant.binding
        with _step('declaration'):
            declaration = require_declared_coordinate(binding.workspace, node, self.root)
        if declaration['workspace_identity'] != 'declared':
            raise ProtocolRefusal('seat_board_workspace_undeclared',
                'boarding declaration step requires a matching SEAT.json; publish the governed workspace declaration first')
        with _step("consent"):
            consent = CodexWaitConsentLedger(self.root).require_armed(binding)
        relative = Path('receipts/seat-board') / (node + '.jsonl')
        coordinate = hashlib.sha256((node + '\0' + str(binding.workspace) + '\0' + session
            + '\0' + str(bool(take_over))).encode()).hexdigest()
        native_key = 'board-' + hashlib.sha256((key + '\0' + coordinate).encode()).hexdigest()
        with wake_coordination_guard(self.root, node):
            rows = [r for r in read_records_snapshot(self.root, relative, allowed_kinds={KIND})
                    if r['idempotency_key'] == key]
            _validate_progress(rows)
            if rows and rows[0]['coordinate_digest'] != coordinate:
                raise ProtocolRefusal('seat_board_idempotency_conflict', 'boarding key has different coordinates', 'use a new key for different boarding coordinates')
            if rows and rows[-1]['step'] == 'completed':
                return self._result(rows[-1])
            if rows and rows[-1]['step'] == 'drain_started':
                raise ProtocolRefusal('seat_board_drain_unknown',
                    'boarding drain began without a completion receipt', 'inspect delivery and acknowledgment evidence before authorizing another boarding key')
            state = dict(rows[-1]) if rows else {
                'schema_version': 1, 'kind': KIND, 'tenant_id': self.root.tenant_id,
                'node_id': node, 'acting_session_id': session, 'coordinate_digest': coordinate,
                'idempotency_key': key, 'arm_receipt_id': None, 'predecessor_receipt_id': None,
                'resume_receipt_id': None, 'resume_outcome': None,
                'delivery_receipt_id': None, 'ack_receipt_id': None, 'item_ids': [],
            }
            def record(step):
                state.update(id='seat-board-' + uuid7_hex(), timestamp=utc_now(), step=step)
                self._append(relative, dict(state))
            with _step("arm"):
                authority = CodexWaitSessionLedger(self.root)._arm_already_guarded(
                    binding, consent, session, idempotency_key=native_key + '-arm', take_over=take_over, require_current=True)
            if not rows:
                record('started')
            if state.get('step') == 'started':
                state['arm_receipt_id'] = authority['id']
                state['predecessor_receipt_id'] = authority['predecessor_receipt_id']
                record('armed')
            if state['step'] == 'armed':
                with _step('resume'):
                    outcome, receipt = self._resume(node, session, native_key + '-resume')
                state.update(resume_outcome=outcome, resume_receipt_id=receipt)
                record('resumed')
            controller = WakeController(self.root)
            with controller._lock(node):
                if controller.status(node, session)['state'] == 'paused':
                    raise ProtocolRefusal('seat_board_resume_stale',
                        'boarding resume step was superseded by a pause', 'explicitly resume this session before retrying the boarding key')
                record('drain_started')
                with _step('drain'):
                    messages, delivery = EventLog(self.root)._present_already_guarded(node)
                    acknowledgment = None
                    if messages:
                        acknowledgment = SparseCursor(self.root)._ack_already_guarded(node,
                            [str(m['id']) for m in messages], acting_session_id=session)
                state.update(item_ids=[m['id'] for m in messages],
                    delivery_receipt_id=None if delivery is None else delivery['id'],
                    ack_receipt_id=None if acknowledgment is None else acknowledgment['id'])
                record('completed')
            return self._result(state)

    def _result(self, row):
        # Returning IDs and native receipts does not manufacture another presentation.
        result = {key: row[key] for key in ('id', 'node_id', 'acting_session_id', 'step',
            'arm_receipt_id', 'predecessor_receipt_id', 'resume_receipt_id', 'resume_outcome',
            'delivery_receipt_id', 'ack_receipt_id', 'item_ids')}

        node = row['node_id']
        native_key = 'board-' + hashlib.sha256((row['idempotency_key'] + '\0' + row['coordinate_digest']).encode()).hexdigest()
        def native(relative, kind, identity):
            receipts = read_records_snapshot(self.root, relative, allowed_kinds=kind)
            matches = [r for r in receipts if r['id'] == identity]
            if len(matches) != 1:
                raise ProtocolRefusal('seat_board_replay_unavailable',
                    'boarding native receipt is absent from the current plane; inspect the completed record, do not redrain')
            return matches[0]
        arm = native(CodexWaitSessionLedger._relative(node), {'codex_wait_session_receipt'}, row['arm_receipt_id'])
        if arm['idempotency_key'] != native_key + '-arm' or arm['node_id'] != node or arm['acting_session_id'] != row['acting_session_id'] or arm['predecessor_receipt_id'] != row['predecessor_receipt_id']:
            raise IntegrityFailure('seat_board_progress_invalid', 'boarding arm reference has different ownership')
        if row['resume_outcome'] == 'resumed':
            controller = WakeController(self.root)
            resume = native(controller._ledger(node), {'wake_control_receipt'}, row['resume_receipt_id'])
            if resume['idempotency_key'] != native_key + '-resume' or resume['operation'] != 'resume' or resume['state'] != 'resume_requested' or resume['session_digest'] != controller._identity(node, row['acting_session_id'])[2]:
                raise IntegrityFailure('seat_board_progress_invalid', 'boarding resume reference has different ownership')
        if row['item_ids']:
            delivery = native(Path('receipts/deliveries') / (node + '.jsonl'),
                {'delivery_receipt', 'wake_hold_receipt'}, row['delivery_receipt_id'])
            ack = native(Path('receipts/acks') / (node + '.jsonl'), {'ack_receipt'}, row['ack_receipt_id'])
            if any(r['recipient'] != node or r['item_ids'] != row['item_ids'] for r in (delivery, ack)) or ack['acting_session_id'] != row['acting_session_id']:
                raise IntegrityFailure('seat_board_progress_invalid', 'boarding batch differs from native consumption evidence')
        by_id = {r['id']: r for r in EventLog(self.root).event_records() if r['kind'] == 'message_envelope'}
        if any(item not in by_id for item in row['item_ids']):
            raise ProtocolRefusal('seat_board_replay_unavailable',
                'recorded boarding messages are no longer in the current event plane; inspect the completed receipt, do not redrain')
        result['messages'] = [by_id[item] for item in row['item_ids']]
        if any(message['recipient'] != node or message.get('worker_session_id') is not None for message in result['messages']):
            raise IntegrityFailure('seat_board_progress_invalid', 'boarding messages have different ownership')
        return result

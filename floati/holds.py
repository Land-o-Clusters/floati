"""Explicit project holds: strict authoring, tolerant read-only history."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import shlex

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames
from .ids import uuid7_hex
from .jsonl import (MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES,
                    _append_frame, _encode_record, _locked_path)
from .records import HoldReceipt
from .registry import Registry, utc_now
from .root import FloatiRoot, validate_identifier


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _canonical_time(value):
    moment = _time(value)
    return None if moment is None else moment.isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _phase(row):
    phase, released = row.get('phase'), row.get('released_at')
    if phase == 'held':
        return 'held' if released in (None, '') else 'unknown'
    if phase == 'released' or (phase is None and released not in (None, '')):
        time = released if 'released_at' in row else row.get('timestamp')
        return 'released' if _time(time) is not None and isinstance(row.get('predecessor_receipt_id'), str) else 'unknown'
    return 'held' if phase is None else 'unknown'


def _read(root, node):
    """No schema validator, lock creation, history rewrite or home discovery."""
    path = root.resolve_relative(Path('receipts/holds') / (node + '.jsonl'))
    try:
        with path.open('rb') as stream:
            data = stream.read(MAX_LEDGER_BYTES + 1)
    except FileNotFoundError:
        return [], 0
    if len(data) > MAX_LEDGER_BYTES:
        return [], 1
    rows, partial, ids = [], 0, set()
    for index, raw in enumerate(data.splitlines(keepends=True)):
        if index >= MAX_LEDGER_RECORDS:
            partial += 1
            break
        try:
            if len(raw) > MAX_RECORD_BYTES:
                raise ValueError('oversize')
            row, = decode_frames(raw)
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                    or row.get('kind', 'hold_receipt') != 'hold_receipt'
                    or row.get('tenant_id', root.tenant_id) != root.tenant_id
                    or row.get('node_id', node) != node or row['id'] in ids):
                raise ValueError('unattributable hold row')
            ids.add(row['id'])
            rows.append(row)
            if _phase(row) == 'unknown':
                partial += 1
        except (FrameError, ValueError, TypeError):
            partial += 1
    return rows, partial


def _open(rows):
    held, released = {}, set()
    for row in rows:
        if _phase(row) == 'released':
            predecessor = row.get('predecessor_receipt_id')
            if isinstance(predecessor, str):
                released.add(predecessor)
        elif not row.get('predecessor_receipt_id'):
            held[row['id']] = row
    return [row for ident, row in held.items() if ident not in released]


def hold_projection(root, node, now=None):
    current = datetime.now(timezone.utc) if now is None else now
    try:
        rows, partial = _read(root, node)
    except (OSError, ProtocolRefusal):
        return {'holds': [], 'partial_records': 1, 'history_unavailable': True}
    holds = []
    for row in _open(rows):
        started = _time(row.get('started_at', row.get('timestamp')))
        expected = _time(row.get('expected_release'))
        age = None if started is None or started > current else int((current - started).total_seconds())
        holds.append({'id': row['id'], 'node_id': node, 'reason': row.get('reason'),
                      'age_seconds': age, 'overdue': expected is not None and current > expected,
                      'expected_release': row.get('expected_release'),
                      'expected_release_unknown': row.get('expected_release') is not None and expected is None})
    return {'holds': holds, 'partial_records': partial}


def hold_line(hold):
    age = 'unknown age' if hold['age_seconds'] is None else str(hold['age_seconds']) + 's old'
    reason = json.dumps(hold.get('reason'), ensure_ascii=True)
    unknown = ' expected release unknown' if hold.get('expected_release_unknown') else ''
    return ('HOLD ' + ('OVERDUE ' if hold['overdue'] else '') + json.dumps(hold['id'], ensure_ascii=True)
            + ' ' + age + ' ' + reason + unknown)


def _finding(code, severity, node, detail, remedy=None):
    # Subjects are node identifiers, never filesystem paths.
    return {'code': code, 'severity': severity, 'subject': node,
            'detail': detail, 'remediation': remedy}


def project_hold_findings(root):
    try:
        directory = root.resolve_relative('receipts/holds')
        if not directory.exists():
            return []
        paths = sorted(directory.glob('*.jsonl'))
    except (OSError, ProtocolRefusal):
        return [_finding('hold_ledger_partial', 'warning', 'holds', 'hold history unavailable')]
    findings = []
    for path in paths:
        node = path.stem
        try:
            validate_identifier(node)
            projection = hold_projection(root, node)
        except (OSError, ProtocolRefusal):
            findings.append(_finding('hold_ledger_partial', 'warning', 'holds', 'hold history unavailable'))
            continue
        for overdue in (False, True):
            selected = [h for h in projection['holds'] if h['overdue'] == overdue]
            if selected:
                remedy = shlex.join(['floati', 'hold', 'release', '--root', str(root.path),
                                     '--as', node, '--hold', selected[0]['id'],
                                     '--idempotency-key', 'hold-release-' + uuid7_hex()])
                findings.append(_finding('hold_overdue' if overdue else 'hold_open',
                                         'warning' if overdue else 'info', node,
                                         '; '.join(hold_line(h) for h in selected), remedy))
        if projection['partial_records']:
            findings.append(_finding('hold_ledger_partial', 'warning', node,
                                     str(projection['partial_records']) + ' unreadable hold record(s); healthy history retained'))
    return findings


class HoldLedger:
    def __init__(self, root):
        self.root = root

    def _write(self, node, key, operation, reason=None, expected=None, hold=None):
        validate_identifier(node)
        try:
            Registry(self.root).require_active(node)
        except ProtocolRefusal:
            raise ProtocolRefusal('hold_node_unknown', 'hold authoring needs a registered active node',
                                  remedy='Select a registered active node with --as before authoring a hold.') from None
        now = utc_now()
        if operation == 'start':
            if expected is not None and _time(expected) is None:
                raise ProtocolRefusal('hold_expected_release_invalid', 'expected release must be a UTC timestamp',
                                      remedy='Pass --expected-release as a UTC RFC3339 timestamp ending in Z, or omit it.')
            candidate = HoldReceipt('hold-' + uuid7_hex(), self.root.tenant_id, now,
                                    node, reason, now, expected, 'held', None, None, key).to_dict()
        else:
            candidate = None
            if hold is not None and (not isinstance(hold, str) or not hold.startswith('hold-')):
                raise ProtocolRefusal('hold_unknown', 'selected hold does not belong to this node',
                                      remedy='Select an open hold ID belonging to the node named by --as.')
        relative = Path('receipts/holds') / (node + '.jsonl')
        path = self.root.resolve_relative(relative)
        with _locked_path(path.with_name(path.name + '.lock'), exclusive=True,
                          relative=relative.as_posix() + '.lock'):
            rows, partial = _read(self.root, node)
            if partial:
                raise ProtocolRefusal('hold_history_uncertain', 'authoring requires complete hold history; read remains available',
                                      remedy='Inspect the node hold ledger and its doctor partial-history finding before authoring; preserve the existing history.')
            prior = next((r for r in rows if r.get('idempotency_key') == key), None)
            if prior is not None:
                matches = (prior.get('phase', 'held') == ('held' if operation == 'start' else 'released'))
                if operation == 'start':
                    matches = matches and prior.get('reason') == reason and prior.get('expected_release') == expected
                else:
                    matches = matches and (hold is None or prior.get('predecessor_receipt_id') == hold)
                if not matches:
                    raise ProtocolRefusal('hold_key_conflict', 'idempotency key already names a different hold operation',
                                          remedy='Replay the original request with this key, or choose a fresh key for the different request.')
                return prior
            if operation == 'release':
                opened = _open(rows)
                if hold is None:
                    if len(opened) > 1:
                        remedy = shlex.join(['floati', 'hold', 'release', '--root', str(self.root.path), '--as', node,
                                             '--hold', opened[0]['id'], '--idempotency-key', key])
                        raise ProtocolRefusal('hold_ambiguous', 'choose one of the open holds', remedy=remedy)
                    if not opened:
                        raise ProtocolRefusal('hold_none_open', 'this node has no open hold',
                                              remedy='Select a node with an open hold, or replay a previous release with its original key.')
                    original = opened[0]
                else:
                    original = next((r for r in opened if r['id'] == hold), None)
                    if original is None:
                        if any(r['id'] == hold for r in rows):
                            raise ProtocolRefusal('hold_already_released', 'selected hold is already terminal',
                                                  remedy='Replay the release with its original key; choose another open hold for a new release.')
                        raise ProtocolRefusal('hold_unknown', 'selected hold does not belong to this node',
                                              remedy='Select an open hold ID belonging to the node named by --as.')
                started = _canonical_time(original.get('started_at', original.get('timestamp')))
                expected = _canonical_time(original.get('expected_release'))
                candidate = HoldReceipt('hold-' + uuid7_hex(), self.root.tenant_id, now,
                                        node, None, started, expected, 'released', now,
                                        original['id'], key).to_dict()
            if len(rows) >= MAX_LEDGER_RECORDS:
                raise ProtocolRefusal('ledger_record_limit', 'hold ledger record bound reached')
            encoded = _encode_record(candidate, self.root.tenant_id, frozenset({'hold_receipt'}), max_bytes=MAX_RECORD_BYTES)
            if path.exists() and path.stat().st_size + len(encoded) > MAX_LEDGER_BYTES:
                raise ProtocolRefusal('ledger_byte_limit', 'hold ledger byte bound reached',
                                      remedy='Preserve this full ledger and obtain an explicit retention plan before adding more hold history.')
            _append_frame(path, encoded)
            return candidate

    def start(self, node, reason, key, expected_release=None):
        return self._write(node, key, 'start', reason=reason, expected=expected_release)

    def release(self, node, key, hold=None):
        return self._write(node, key, 'release', hold=hold)


def register_cli(commands):
    hold = commands.add_parser('hold')
    actions = hold.add_subparsers(dest='hold_action', required=True)
    for action in ('start', 'release'):
        command = actions.add_parser(action)
        command.add_argument('--root', required=True, metavar='ROOT')
        command.add_argument('--as', dest='actor', required=True, metavar='NODE')
        command.add_argument('--idempotency-key', required=True, metavar='KEY')
        if action == 'start':
            command.add_argument('--reason', required=True, metavar='TEXT')
            command.add_argument('--expected-release', metavar='TIMESTAMP')
        else:
            command.add_argument('--hold', metavar='HOLD')
        command.set_defaults(handler=_handle)

    inbound = actions.add_parser('inbound')
    inbound_actions = inbound.add_subparsers(dest='inbound_action', required=True)
    for action in ('start', 'show'):
        command = inbound_actions.add_parser(action)
        command.add_argument('--root', required=True, metavar='ROOT')
        command.add_argument('--node', required=True, metavar='NODE')
        if action == 'start':
            command.add_argument('--idempotency-key', required=True, metavar='KEY')
            command.add_argument('--reason', required=True, metavar='TEXT')
            command.add_argument('--expected-release', metavar='TIMESTAMP')
        command.set_defaults(handler=_handle_inbound)


def _handle(args):
    from .cli import _root
    ledger = HoldLedger(_root(args.root))
    if args.hold_action == 'start':
        receipt = ledger.start(args.actor, args.reason, args.idempotency_key, args.expected_release)
    else:
        receipt = ledger.release(args.actor, args.idempotency_key, args.hold)
    return 'ok', receipt, 0


def _handle_inbound(args):
    from .cli import _root
    from .inbound_hold import inbound_hold_projection, start_hold

    try:
        root = _root(args.root)
        if args.inbound_action == 'start':
            evidence = start_hold(root, args.node, args.reason, args.idempotency_key,
                                  args.expected_release)
        else:
            evidence = inbound_hold_projection(root, args.node)
    except ProtocolRefusal as exc:
        return 'refused', {'code': exc.code, 'detail': exc.detail, 'remedy': exc.remedy}, 20
    except (IntegrityFailure, DurabilityFailure) as exc:
        return 'malformed_evidence', {'code': exc.code, 'detail': str(exc)}, 33
    return 'ok', evidence, 0

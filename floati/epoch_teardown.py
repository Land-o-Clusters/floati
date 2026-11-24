"""Roll-time teardown testimony bound to one exact active roll receipt.

Existing archives remain retained evidence, but boot never opens them here.
Only a native EX-owned outgoing check may issue a new verification seed.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .errors import IntegrityFailure, ProtocolRefusal
from .framing import decode_frames, encode_frame
from .ids import uuid7_hex
from .jsonl import MAX_LEDGER_RECORDS, MAX_RECORD_BYTES, _decode_path_records, read_records_snapshot
from .records import (WAKE_HOLD_KINDS, _record_ref, _terminal_unsafe,
    epoch_carry_verification_digest, validate_record)
from .registry import utc_now


KIND = 'seat_teardown_epoch_verification'
CARRY_KIND = 'epoch_carry_verification'
_SEED_FIELDS = frozenset({'teardown_receipt_id', 'teardown_context_sha256',
    'outgoing_epoch_id', 'report_envelope_id', 'report_envelope_sha256', 'verification'})
_SEED_DOMAIN = b'floati-seat-teardown-epoch-seeds-v1\0'
_CONTEXT_DOMAIN = b'floati-seat-teardown-context-v1\0'
_REMEDY = ('preserve the native teardown receipts, pause markers and retained epoch evidence; '
    'ask the declared architect to reconcile the named teardown cutoff before treating the session as completed')


def _failure(detail):
    return IntegrityFailure('seat_teardown_native_invalid', detail)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')


def context_sha256(row):
    """Bind stable transaction/cutoff context, never a mutable phase frame."""
    from .seat_teardown import _IMMUTABLE, _CUTOFF_FIELDS
    try:
        fields = (*_IMMUTABLE, *_CUTOFF_FIELDS[row['schema_version']])
        context = {field: row[field] for field in fields}
    except (KeyError, TypeError) as exc:
        raise _failure('teardown immutable cutoff context is incomplete') from exc
    return hashlib.sha256(_CONTEXT_DOMAIN + _canonical(context)).hexdigest()


def _seed(record):
    return {field: deepcopy(record[field]) for field in _SEED_FIELDS}


def _validate_seed(seed, *, bound):
    expected = _SEED_FIELDS if bound else _SEED_FIELDS - {'outgoing_epoch_id'}
    if not isinstance(seed, dict) or set(seed) != expected:
        raise _failure('teardown verification seed has an open or incomplete shape')
    def refuse(_code, detail, *_remedy):
        raise _failure(detail)
    _record_ref(seed['teardown_receipt_id'], 'seat-teardown-', 'teardown_receipt_id', refuse)
    if not isinstance(seed['teardown_context_sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', seed['teardown_context_sha256']) is None:
        raise _failure('teardown verification context digest is invalid')
    if bound and (not isinstance(seed['outgoing_epoch_id'], str)
            or re.fullmatch(r'epoch-[0-9a-f]{32}', seed['outgoing_epoch_id']) is None):
        raise _failure('teardown verification outgoing epoch is invalid')
    report, digest = seed['report_envelope_id'], seed['report_envelope_sha256']
    if (report is None) != (digest is None) or (report is not None and (
            not isinstance(report, str) or not report.startswith('msg-')
            or not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None)):
        raise _failure('teardown report attestation is not one exact nullable pair')
    if report is not None:
        _record_ref(report, 'msg-', 'report_envelope_id', refuse)
    verification = seed['verification']
    if not isinstance(verification, dict) or set(verification) != {'state', 'failure_code', 'remedy'}:
        raise _failure('teardown verification result has an open shape')
    if verification['state'] == 'verified':
        if verification['failure_code'] is not None or verification['remedy'] is not None:
            raise _failure('verified teardown testimony cannot carry a failure')
    elif verification['state'] == 'failed':
        code, remedy = verification['failure_code'], verification['remedy']
        if (not isinstance(code, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,127}', code) is None
                or not isinstance(remedy, str) or not 1 <= len(remedy) <= 1024
                or _terminal_unsafe(remedy)):
            raise _failure('failed teardown testimony lacks a bounded named refusal')
    else:
        raise _failure('teardown verification state is unknown')


def finalize_verifications(seeds, outgoing_epoch_id):
    """Deterministic canonical seed commitment, before encoding the new roll."""
    result = []
    for source in seeds:
        seed = deepcopy(source)
        if not isinstance(seed, dict):
            raise _failure('teardown verification seed is not native data')
        if 'outgoing_epoch_id' in seed:
            if seed['outgoing_epoch_id'] != outgoing_epoch_id:
                raise _failure('teardown seed changed its outgoing epoch')
            _validate_seed(seed, bound=True)
        else:
            _validate_seed(seed, bound=False)
            seed['outgoing_epoch_id'] = outgoing_epoch_id
            _validate_seed(seed, bound=True)
        result.append(seed)
    result.sort(key=lambda row: row['teardown_receipt_id'])
    if len(result) > MAX_LEDGER_RECORDS or len({row['teardown_receipt_id'] for row in result}) != len(result):
        raise _failure('teardown seed collection is duplicated or exceeds the native bound')
    return tuple(result), {'count': len(result),
        'digest': hashlib.sha256(_SEED_DOMAIN + _canonical(result)).hexdigest()}


def validate_event_verifications(events):
    """Pure active-anchor/complete-set validation; failed results remain data."""
    _validate_carry_verification(events)
    records = [row for row in events if row.get('kind') == KIND]
    anchor = events[0] if events and events[0].get('kind') == 'bus_epoch_roll_receipt' else None
    automatic = anchor.get('automatic') if anchor is not None else None
    commitment = automatic.get('teardown_carry') if isinstance(automatic, dict) else None
    if not records and commitment is None:
        return ()
    if anchor is None or not isinstance(commitment, dict) or set(commitment) != {'count', 'digest'}:
        raise _failure('teardown carry lacks its exact active roll commitment')
    if (type(commitment['count']) is not int or not 0 <= commitment['count'] <= MAX_LEDGER_RECORDS
            or not isinstance(commitment['digest'], str)
            or re.fullmatch(r'[0-9a-f]{64}', commitment['digest']) is None):
        raise _failure('teardown carry commitment is malformed')
    anchor = validate_record(dict(anchor), anchor['tenant_id'], {'bus_epoch_roll_receipt'}, integrity=True)
    roll_digest = hashlib.sha256(encode_frame(anchor)).hexdigest()
    identities, transactions, seeds = set(), [], []
    for record in records:
        record = validate_record(dict(record), anchor['tenant_id'], {KIND}, integrity=True)
        if (record['id'] in identities or record['roll_receipt_id'] != anchor['id']
                or record['roll_receipt_sha256'] != roll_digest
                or record['outgoing_epoch_id'] != anchor['epoch_id']):
            raise _failure('teardown carry is duplicated or bound to a different roll frame')
        identities.add(record['id'])
        transactions.append(record['teardown_receipt_id'])
        seeds.append(_seed(record))
    if transactions != sorted(set(transactions)):
        raise _failure('teardown carry transaction order is duplicated or changed')
    _final, expected = finalize_verifications(seeds, anchor['epoch_id'])
    if expected != commitment:
        raise _failure('teardown carry complete seed set differs from the roll commitment')
    return tuple(deepcopy(records))


def _validate_carry_verification(events):
    records = [row for row in events if row.get('kind') == CARRY_KIND]
    if not records:
        return None
    if len(records) != 1 or not events or events[0].get('kind') != 'bus_epoch_roll_receipt':
        raise IntegrityFailure('epoch_carry_verification_invalid',
            'carry verification requires one row and an exact active roll anchor')
    anchor = validate_record(dict(events[0]), events[0]['tenant_id'],
        {'bus_epoch_roll_receipt'}, integrity=True)
    record = validate_record(dict(records[0]), anchor['tenant_id'], {CARRY_KIND}, integrity=True)
    if (record['roll_receipt_id'] != anchor['id']
            or record['roll_receipt_sha256'] != hashlib.sha256(encode_frame(anchor)).hexdigest()):
        raise IntegrityFailure('epoch_carry_verification_invalid',
            'carry verification is bound to a different active roll frame')
    if record['schema_version'] == 1:
        _committed_envelopes(events, record)
    return record


def _committed_envelopes(events, record):
    # The transaction emits all retained envelopes before the first new send.
    # Reproduce precisely that prefix. A new envelope cannot replace a missing
    # one, even when the count still matches.
    from itertools import islice
    envelopes = (row for row in events if row.get('kind') == 'message_envelope')
    entries = [{'id': row['id'], 'sha256': hashlib.sha256(encode_frame(row)).hexdigest()}
        for row in islice(envelopes, record['envelope_count'])]
    if len(entries) != record['envelope_count'] or _carry_head(entries) != record['digest']:
        raise IntegrityFailure('epoch_carry_verification_invalid',
            'carried envelope frames differ from the roll inventory')
    return entries


EMPTY_CARRY_HEAD = hashlib.sha256(b'floati-epoch-carry-verification-v1\0').hexdigest()


def _carry_head(entries, head=EMPTY_CARRY_HEAD):
    for entry in entries:
        head = hashlib.sha256(bytes.fromhex(head) + bytes.fromhex(entry['sha256'])).hexdigest()
    return head


def carried_envelope_digests(events):
    """Return the active roll's inventory; an older epoch has no such row."""
    validate_event_verifications(events)
    record = next((row for row in events if row.get('kind') == CARRY_KIND), None)
    if record is None:
        return None
    entries = (_committed_envelopes(events, record) if record['schema_version'] == 1
               else record['carried_envelopes'])
    return {entry['id']: entry['sha256'] for entry in entries}


def carry_verification_frame(root, retained, receipt):
    """Mint one envelope inventory from the transaction's retained physical bytes."""
    from .bus_epoch import _require_owned_epoch_guard
    _require_owned_epoch_guard(root, exclusive=True)
    entries = []
    for relative, plane, raw in retained:
        if relative != 'events.jsonl':
            continue
        if plane != 'events':
            raise IntegrityFailure('epoch_carry_verification_invalid',
                'carried event payload has a different plane')
        for frame, row in zip(raw.splitlines(keepends=True), decode_frames(raw)):
            if row.get('kind') == 'message_envelope':
                if frame != encode_frame(row):
                    raise IntegrityFailure('epoch_carry_verification_invalid',
                        'carried envelope frame is not canonical')
                entries.append({'id': row['id'], 'sha256': hashlib.sha256(frame).hexdigest()})
    record = dict(schema_version=0, kind=CARRY_KIND, tenant_id=root.tenant_id,
        id='epoch-carry-verification-' + uuid7_hex(), timestamp=utc_now(),
        roll_receipt_id=receipt['id'],
        roll_receipt_sha256=hashlib.sha256(encode_frame(receipt)).hexdigest(),
        carried_envelopes=[], digest='0' * 64)
    # Validated message UUIDs and SHA-256 digests have fixed encoded widths.
    size = len(encode_frame(record))
    if entries:
        size += (len(_canonical(entries[0])) + 1) * len(entries) - 1
    if size > MAX_RECORD_BYTES:
        del record['carried_envelopes']
        record.update(schema_version=1, envelope_count=len(entries), digest=_carry_head(entries))
    else:
        entries.sort(key=lambda entry: entry['id'])
        record.update(carried_envelopes=entries, digest=epoch_carry_verification_digest(entries))
    return encode_frame(validate_record(record, root.tenant_id, {CARRY_KIND}, integrity=False))


def verification_frames(root, final_seeds, receipt):
    """Bind already frozen native seeds to the complete final roll frame."""
    seeds, commitment = finalize_verifications(final_seeds, receipt['epoch_id'])
    saved = receipt.get('automatic', {}).get('teardown_carry')
    if (seeds or saved is not None) and saved != commitment:
        raise _failure('new roll does not commit its exact teardown seeds')
    digest = hashlib.sha256(encode_frame(receipt)).hexdigest()
    frames = []
    for seed in seeds:
        record = dict(seed, schema_version=0, kind=KIND, tenant_id=root.tenant_id,
            id='seat-teardown-epoch-verification-' + uuid7_hex(), timestamp=utc_now(),
            roll_receipt_id=receipt['id'], roll_receipt_sha256=digest)
        record = validate_record(record, root.tenant_id, {KIND}, integrity=False)
        frames.append(encode_frame(record))
    return b''.join(frames)


def carried_verification(root, row, events):
    """Read one committed active verification without any archive lookup."""
    records = validate_event_verifications(events)
    matches = [record for record in records if record['teardown_receipt_id'] == row['started_receipt_id']]
    if not matches:
        return None
    record = matches[0]
    if record['tenant_id'] != root.tenant_id or record['teardown_context_sha256'] != context_sha256(row):
        raise _failure('teardown carry does not bind this exact immutable cutoff context')
    result = record['verification']
    if result['state'] == 'failed':
        raise ProtocolRefusal(result['failure_code'], 'native teardown cutoff failed verification before the active roll',
            remedy=result['remedy'])
    return deepcopy(record)


def collect_verifications(root, old_payloads):
    """Verify all live teardown transactions against the frozen outgoing cut."""
    from .bus_epoch import _require_owned_epoch_guard
    from .events import EVENT_KINDS, validate_event_records
    from .seat_teardown import KIND as RECEIPT_KIND, _validate_progress, _validate_historical
    _require_owned_epoch_guard(root, exclusive=True)
    selected = {}
    for relative, plane, raw in old_payloads:
        coordinate = Path(relative)
        expected = 'events' if relative == 'events.jsonl' else (
            coordinate.parts[1] if len(coordinate.parts) in (3, 4)
            and coordinate.parts[0] == 'receipts' and coordinate.parts[1] in {'deliveries', 'acks'}
            and coordinate.suffix == '.jsonl' else None)
        if relative in selected or plane != expected or coordinate.is_absolute() or '..' in coordinate.parts:
            raise _failure('outgoing teardown source has an invalid or repeated selected coordinate')
        kinds = EVENT_KINDS if plane == 'events' else WAKE_HOLD_KINDS if plane == 'deliveries' else {'ack_receipt'}
        selected[relative] = _decode_path_records(root.tenant_home / relative, root.tenant_id,
            frozenset(kinds), raw, unrecognized={} if plane == 'events' else None)
    if 'events.jsonl' not in selected:
        raise _failure('outgoing teardown source omits its mandatory events')
    events = selected['events.jsonl']
    validate_event_records(events)
    namespace = root.tenant_home / 'receipts/seat-teardown'
    if not namespace.exists():
        if namespace.is_symlink():
            raise _failure('teardown receipt namespace is redirected')
        return ()
    if namespace.is_symlink() or not namespace.is_dir():
        raise _failure('teardown receipt namespace is not ordinary')
    seeds = []
    for path in sorted(namespace.iterdir()):
        if path.suffix != '.jsonl':
            continue
        if path.is_symlink() or not path.is_file():
            raise _failure('teardown receipt source is not ordinary')
        rows = read_records_snapshot(root, path.relative_to(root.tenant_home), allowed_kinds={RECEIPT_KIND})
        if any(row['node_id'] != path.stem for row in rows):
            raise _failure('teardown receipt source crosses its native node coordinate')
        groups = {}
        for row in rows:
            groups.setdefault(row['idempotency_key'], []).append(row)
        for group in groups.values():
            _validate_progress(group)
            row = group[-1]
            # Schema/progress failures are outside this catch. Only a native
            # historical verification result becomes typed failed testimony.
            try:
                report_id, report_digest = _validate_historical(root, row, events,
                    _selected_rows=selected)
            except (IntegrityFailure, ProtocolRefusal) as failure:
                remedy = getattr(failure, 'remedy', None)
                if not isinstance(remedy, str) or not 1 <= len(remedy) <= 1024 or _terminal_unsafe(remedy):
                    remedy = _REMEDY
                result = {'state': 'failed', 'failure_code': failure.code, 'remedy': remedy}
                report_id, report_digest = None, None
            else:
                result = {'state': 'verified', 'failure_code': None, 'remedy': None}
            seeds.append({'teardown_receipt_id': row['started_receipt_id'],
                'teardown_context_sha256': context_sha256(row),
                'report_envelope_id': report_id, 'report_envelope_sha256': report_digest,
                'verification': result})
    # The finalizer supplies outgoing_epoch_id later, after native old facts.
    if len({row['teardown_receipt_id'] for row in seeds}) != len(seeds):
        raise _failure('live teardown transactions reuse one stable started receipt')
    return tuple(sorted(seeds, key=lambda row: row['teardown_receipt_id']))

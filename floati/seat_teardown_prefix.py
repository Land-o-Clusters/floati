"""Bounded commitments to native teardown ID prefixes, in append order."""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath

from .errors import IntegrityFailure


_DOMAIN = b"floati-seat-teardown-prefix-v2\0"
ID_BLOCK = 32


def _domain(ledger):
    relative = PurePosixPath(str(ledger))
    if relative.is_absolute() or not relative.parts or any(
            part in {'.', '..'} for part in relative.parts):
        raise ValueError('prefix ledger must be an ordinary relative coordinate')
    return _DOMAIN + relative.as_posix().encode('utf-8') + b'\0'


def _id_bytes(row):
    identity = row['id'].encode('utf-8')
    return len(identity).to_bytes(4, 'big') + identity


def prefix_proof(rows, ledger):
    """Bind one ID sequence to its repository-derived relative ledger."""
    digest = hashlib.sha256(_domain(ledger))
    for row in rows:
        digest.update(_id_bytes(row))
    return {'count': len(rows), 'last_id': rows[-1]['id'] if rows else None,
            'digest': digest.hexdigest()}


def require_prefix(rows, proof, ledger, *, exact=False):
    """Verify the saved prefix; exact additionally forbids any later append."""
    count = proof['count']
    if len(rows) < count or (exact and len(rows) != count):
        raise IntegrityFailure('seat_teardown_native_invalid',
            str(ledger) + ' does not retain the saved native cutoff length')
    prefix = rows[:count]
    if prefix_proof(prefix, ledger) != proof:
        raise IntegrityFailure('seat_teardown_native_invalid',
            str(ledger) + ' cutoff is not the preserved native prefix')
    return prefix


def advance_event_ids(rows, prior, start, put):
    """Seal historical cutoffs in immutable blocks; the live block stays bounded."""
    from .sha256_midstate import SHA256
    if prior:
        digest = SHA256(state=prior['state'], length=prior['length'], tail=bytes.fromhex(prior['tail']))
        partial = list(prior['partial'])
        executions = prior['executions']
        fast = digest
    else:
        domain = _domain('events.jsonl')
        digest = SHA256().update(domain)
        fast = hashlib.sha256(domain)
        partial, executions = [], 0
    for ordinal, row in enumerate(rows, start + 1):
        data = _id_bytes(row)
        digest.update(data)
        if fast is not digest:
            fast.update(data)
        partial.append([row['id'], fast.hexdigest()])
        if len(partial) == ID_BLOCK:
            put('teardown-ids', str((ordinal - 1) // ID_BLOCK), partial)
            partial = []
        if row['kind'] == 'seat_execution':
            put('execution', str(executions), row)
            executions += 1
        elif row['kind'] == 'message_retracted':
            put('retracted-at', row['retracted_message_id'], ordinal)
    return {'state': list(digest.state), 'length': digest.length, 'tail': digest.tail.hex(),
            'partial': partial, 'executions': executions}


def event_proof(prefix, count):
    from .send_prefix import CacheMiss, _HEX
    total = prefix.payload['count']
    if type(count) is not int or not 0 <= count <= total:
        raise CacheMiss()
    if not count:
        return prefix_proof([], 'events.jsonl')
    block, index = divmod(count - 1, ID_BLOCK)
    live = block == total // ID_BLOCK
    values = prefix.payload['folds']['teardown']['partial'] if live else prefix.get('teardown-ids', str(block))
    if type(values) is not list or len(values) != (total % ID_BLOCK if live else ID_BLOCK):
        raise CacheMiss()
    last_id, digest = values[index]
    if (type(values[index]) is not list or type(last_id) is not str or not 1 <= len(last_id) <= 128
            or type(digest) is not str or _HEX.fullmatch(digest) is None):
        raise CacheMiss()
    return {'count': count, 'last_id': last_id, 'digest': digest}


def validate_event_fold(prefix):
    from .send_prefix import CacheMiss
    from .sha256_midstate import SHA256
    count = prefix.payload['count']
    fold = prefix.payload['folds']['teardown']
    if (type(fold) is not dict or set(fold) != {'state', 'length', 'tail', 'partial', 'executions'}
            or type(fold['executions']) is not int or not 0 <= fold['executions'] <= count
            or type(fold['partial']) is not list or len(fold['partial']) != count % ID_BLOCK
            or type(fold['state']) is not list or type(fold['tail']) is not str
            or len(fold['tail']) > 126 or type(fold['length']) is not int
            or not len(_domain('events.jsonl')) + 5 * count <= fold['length'] <=
                len(_domain('events.jsonl')) + 4 * count + prefix.payload['offset']):
        raise CacheMiss()
    tail = bytes.fromhex(fold['tail'])
    digest = SHA256(state=fold['state'], length=fold['length'], tail=tail)
    if tail.hex() != fold['tail'] or digest.hexdigest() != event_proof(prefix, count)['digest']:
        raise CacheMiss()


def _certified(root, select, *, snapshot=False):
    """Take a detached fact at one unrolled, known-kind event identity."""
    from contextlib import nullcontext
    from . import jsonl, send_prefix
    from .inbox_prefix import InboxPrefix, FIRST
    try:
        prefix = InboxPrefix(root, read_only=True)
        guard = nullcontext()
        if not snapshot and prefix.path.exists():
            path, relative = jsonl._lock_beside(prefix.path, 'events.jsonl')
            guard = jsonl._locked_path(path, exclusive=False, relative=relative)
        with guard:
            prefix.load()
        first = prefix.get('first', FIRST)
        if ((first is None) != (prefix.payload['count'] == 0)
                or first is not None and first['kind'] == 'bus_epoch_roll_receipt'):
            return None
        result = select(prefix)
        prefix._assert_current()
        return result
    except send_prefix._CACHE_ERRORS:
        return None


def certified_count(root):
    return _certified(root, lambda prefix: prefix.payload['count'], snapshot=True)


def certified_executions(root):
    from .archive_execution_replay import validate_execution_history
    from .send_prefix import CacheMiss
    def select(prefix):
        count = prefix.payload['folds']['teardown']['executions']
        if prefix.get('execution', str(count)) is not None:
            raise CacheMiss()
        result = []
        for index in range(count):
            row = prefix.get('execution', str(index))
            if type(row) is not dict or row.get('kind') != 'seat_execution':
                raise CacheMiss()
            result.append(row)
        try:
            validate_execution_history(root.tenant_id, result)
        except IntegrityFailure as failure:
            raise CacheMiss() from failure
        return result
    return _certified(root, select)


def _check_native_endpoint(prefix, last_id):
    """Bind the endpoint of a new cutoff to its last physical source frame."""
    import os
    import stat
    from .framing import decode_frames
    from .jsonl import MAX_RECORD_BYTES
    from .send_prefix import CacheMiss, _FIELDS
    end = prefix.payload['offset']
    limit = min(end, MAX_RECORD_BYTES + 1)  # Include the preceding terminator.
    width = min(limit, 512)
    descriptor = os.open(prefix.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or [getattr(opened, field) for field in _FIELDS] != prefix.payload['identity']):
            raise CacheMiss()
        while width:
            stream.seek(end - width)
            data = stream.read(width)
            if len(data) != width or not data.endswith(b'\n'):
                break
            start = data.rfind(b'\n', 0, -1) + 1
            if start or width == end:
                row, = decode_frames(data[start:])
                if row['id'] == last_id:
                    return
                break
            if width == limit:
                break
            width = min(2 * width, limit)
    raise CacheMiss()


def certified_mail(root, node, *, saved=None, report_id=None):
    """The current recipient projection and its slice at one v2 cutoff.

    Missing report, wrong commitment or a changed certificate goes through
    the native reader so the original ordered failure remains authoritative.
    """
    from .inbox_prefix import recipient_key
    from .send_prefix import CacheMiss
    def select(prefix):
        count = prefix.payload['count'] if saved is None else saved['count']
        proof = event_proof(prefix, count)
        if saved is None and count:
            _check_native_endpoint(prefix, proof['last_id'])
        if saved is not None and saved != proof:
            raise CacheMiss()
        current, historical = [], []
        previous_position = 0
        for index in range(prefix.recipient_count(node)):
            row = prefix.get('recipient', recipient_key(node, index))
            pos, envelope = prefix.positioned_message(row['id'])
            if row != envelope or envelope['recipient'] != node or pos <= previous_position:
                raise CacheMiss()
            previous_position = pos
            current.append(envelope)
            if pos <= count:
                historical.append(envelope)
            retraction = prefix.get('retracted', row['id'])
            if retraction is not None:
                at = prefix.get('retracted-at', row['id'])
                if (type(at) is not int or not pos < at <= prefix.payload['count']
                        or retraction['kind'] != 'message_retracted' or retraction['retracted_message_id'] != row['id']):
                    raise CacheMiss()
                current.append(retraction)
                if at <= count:
                    historical.append(retraction)
        if report_id is not None:
            report = prefix.get('message', report_id)
            if report is None:
                raise CacheMiss()
            if report['recipient'] != node:
                current.append(report)
        return current, historical, proof
    return _certified(root, select)

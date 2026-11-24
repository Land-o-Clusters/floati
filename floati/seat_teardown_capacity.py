"""Pre-effect teardown capacity checks; native appends remain authoritative."""

from __future__ import annotations

from copy import deepcopy

from .errors import ProtocolRefusal, bind_refusal_remedy
from .events import EventLog
from .ids import uuid7_hex
from .jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES, _encode_record, read_records_snapshot
from .registry import utc_now
from .wake_control import WakeController


_REMEDY = (
    'preserve all native receipts, history and pause markers; ask the declared '
    'architect to perform the governed epoch roll to reduce retained history, '
    'then retry the saved teardown coordinate; never truncate evidence'
)
_KINDS = frozenset({'seat_teardown_receipt'})


def bind_capacity_refusal(failure):
    """Give native size/capacity failures a preservation and rollover remedy."""
    if failure.code == 'record_too_large':
        return ProtocolRefusal('seat_teardown_record_too_large', failure.detail, remedy=_REMEDY)
    if failure.code in {'ledger_too_large', 'ledger_record_limit'}:
        return ProtocolRefusal('seat_teardown_capacity', failure.detail, remedy=_REMEDY)
    return failure


def _capacity(root, relative, count, writes, encoded_bytes):
    path = root.resolve_relative(relative)
    size = path.stat().st_size if path.exists() else 0
    if count + writes > MAX_LEDGER_RECORDS or size + encoded_bytes > MAX_LEDGER_BYTES:
        raise ProtocolRefusal('seat_teardown_capacity',
            str(relative) + ' lacks capacity for the remaining native teardown writes',
            remedy=_REMEDY)


def preflight_teardown(root, state, relative):
    """Check all prospective phase records before recording a pause intent.

    This does not reserve the shared event ledger against unrelated sends.
    Its native atomic batch writer must still enforce physical capacity.
    """
    if len(state['ack_cutoff_refs']) > 1000 or len(state['abandoned_pause_intents']) > 100:
        raise ProtocolRefusal('seat_teardown_record_too_large',
            'teardown scope or abandoned-reservation inventory exceeds its bounded record contract',
            remedy=_REMEDY)
    from .seat_teardown_prefix import certified_count
    physical_count = certified_count(root)
    if physical_count is None:
        events, unknown = EventLog(root).compatible_event_records(snapshot=True)
        physical_count = len(events) + sum(int(item['count']) for item in unknown)
    # The report is an atomic envelope/readiness pair. Reserve the existing
    # maximum frame budget for each: this is a bound, not a measured size.
    _capacity(root, EventLog(root).relative_path, physical_count, 2, 2 * MAX_RECORD_BYTES)

    encoded = []
    stamp = utc_now()
    for step in ('started', 'paused', 'reported', 'completed'):
        row = deepcopy(state)
        row.update(id='seat-teardown-' + uuid7_hex(), timestamp=stamp, step=step,
            pause_receipt_id=None if step == 'started' else state['pause_intent']['id'],
            envelope_id='msg-' + uuid7_hex() if step in {'reported', 'completed'} else None)
        try:
            encoded.append(_encode_record(row, root.tenant_id, _KINDS))
        except ProtocolRefusal as exc:
            bound = bind_capacity_refusal(exc)
            if bound is exc and exc.code == 'seat_teardown_record_invalid':
                exc.remedy = bind_refusal_remedy(exc.code,
                    'preserve native evidence; restore bounded schema-valid activity and work testimony; '
                    'ask the declared architect for a governed epoch roll when retained scope/work history is the cause')
                raise
            raise bound from exc
    existing = read_records_snapshot(root, relative, allowed_kinds=set(_KINDS))
    _capacity(root, relative, len(existing), len(encoded), sum(map(len, encoded)))

    controller = WakeController(root)
    wake_relative = controller._ledger(state['node_id'])
    wake = read_records_snapshot(root, wake_relative, allowed_kinds={'wake_control_receipt'})
    intent = _encode_record(state['pause_intent'], root.tenant_id, frozenset({'wake_control_receipt'}))
    _capacity(root, wake_relative, len(wake), 1, len(intent))

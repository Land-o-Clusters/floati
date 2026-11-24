"""F2-STOP-SKIP-1 (amended 2026-10-03, OPUS-RULED): a skipped Stop is UNKNOWN, not silent.

Waiting mail older than the stated threshold with no stop hold started
since it is exactly the seat a skipped Stop leaves behind - and from the
ledger alone, a long turn and a skipped Stop look alike. Doctor therefore
says UNKNOWN, with the numbers it does know (the node, the oldest waiting
mail age, the last hold start or its honest absence), the honest sentence,
and the only honest remedy: look at the seat. A fresh hold - one started
since the waiting mail arrived - or no waiting mail never raises it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from .artifact_subject import artifact_subject
from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .mail_health import _parse_timestamp

STOP_SKIP_STATED_THRESHOLD_MINUTES = 15
# Stated, not measured - the delivery-stall precedent
# (DELIVERY_STALL_RED_MINUTES, stamped ruling 2026-08-22): a judgment with
# margin, amended by ruling, never re-derived from sightings.


def project_stop_skip_findings(
    events: Sequence[Dict[str, object]],
    root,
    nodes: Sequence[str],
    now: datetime,
    *,
    threshold_minutes: int = STOP_SKIP_STATED_THRESHOLD_MINUTES,
    delivery_times=None,
) -> List[Dict[str, object]]:
    """One typed UNKNOWN finding per seat whose Stop may have been skipped."""

    from .delivery_health import _receipt_item_times
    from .stop_hold import StopHoldLedger

    findings: List[Dict[str, object]] = []
    ledger = StopHoldLedger(root)
    for node in nodes:
        delivered = set(_receipt_item_times(root, "deliveries", node)
            if delivery_times is None else delivery_times[node])
        pending = [
            record
            for record in events
            if record.get("kind") == "message_envelope"
            and record.get("recipient") == node
            and record.get("id") not in delivered
        ]
        if not pending:
            continue
        oldest = min(_parse_timestamp(str(record["timestamp"])) for record in pending)
        current = now.astimezone(timezone.utc)
        age_minutes = int((current - oldest).total_seconds() // 60)
        if age_minutes <= threshold_minutes:
            continue
        try:
            hold_rows = ledger.read(node)
        except (ProtocolRefusal, IntegrityFailure, DurabilityFailure, OSError) as exc:
            findings.append({
                "code": "stop_skip_hold_unreadable",
                "severity": "warning",
                "subject": artifact_subject(node),
                "detail": (
                    f"cannot read the stop-hold ledger for {node}, so the "
                    f"skipped-Stop check is unknown: {type(exc).__name__}"
                ),
                "remediation": "read receipts/stop-holds for this node",
            })
            continue
        starts = [
            str(row["started_at"])
            for row in hold_rows
            if row.get("phase") == "start"
        ]
        last_hold_started: Optional[str] = max(starts) if starts else None
        if last_hold_started is not None and _parse_timestamp(
            last_hold_started
        ) >= oldest:
            continue
        findings.append({
            "code": "stop_skip_unknown",
            "severity": "warning",
            "subject": artifact_subject(node),
            "detail": (
                f"oldest waiting mail for {node} is {age_minutes}m old "
                f"(sent {oldest.isoformat(timespec='milliseconds').replace('+00:00', 'Z')}) "
                "and no stop hold has started since it "
                f"(last hold start: {last_hold_started or 'none on record'}); "
                "a long turn and a skipped Stop look alike from here "
                f"(threshold {threshold_minutes}m, stated not measured)"
            ),
            "remediation": "look at the seat",
            "stop_skip": {
                "node": node,
                "oldest_waiting_mail_age_minutes": age_minutes,
                "oldest_waiting_mail_sent_at": oldest.isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z"),
                "last_hold_started_at": last_hold_started,
                "threshold_minutes": threshold_minutes,
            },
        })
    return findings

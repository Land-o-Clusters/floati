"""WD-4: the idle-retire doctor finding, one pure function.

The ruling on doctor.py contention puts every new doctor finding in its
own module: a pure function returning findings, registered in doctor.py
with one line. This module owns the WD-4 announcement — the retirement
date, printed from seven days out. The copy is the architect's, applied
verbatim from the 2026-09-10 dispatch §3c.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping


def idle_retire_lines(node_label: str, runtime: Mapping[str, Any]) -> list[str]:
    """The WD-4 lines a wake daemon runtime earns, or none.

    A consented `idle_retire_days` greater than zero with at least one
    idle stamp (last wake attempt or last mail seen) that is seven or
    more days old announces its retirement date. A daemon with no
    consented window, no stamps, or a younger clock renders nothing new —
    the unqualified case stays byte-identical to the pre-WD-4 line set.
    """

    last_wake = runtime.get("last_wake_attempt_at")
    last_mail = runtime.get("last_mail_seen_at")
    retire_days = runtime.get("idle_retire_days")
    idle_stamps = [
        float(item)
        for item in (last_wake, last_mail)
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    ]
    if (
        isinstance(retire_days, int)
        and not isinstance(retire_days, bool)
        and retire_days > 0
        and idle_stamps
        and (datetime.now(timezone.utc).timestamp() - max(idle_stamps))
        >= 7 * 86400.0
    ):
        retire_date = datetime.fromtimestamp(
            max(idle_stamps) + retire_days * 86400.0, tz=timezone.utc
        ).date().isoformat()
        return [
            f"wake daemon {node_label} retires on {retire_date} unless it "
            "wakes something first; change idle_retire_days at consent to "
            "keep it."
        ]
    return []

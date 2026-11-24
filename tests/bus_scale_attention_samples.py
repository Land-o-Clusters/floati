"""Independent literal admission samples, never native measurement evidence.

These fixtures deliberately do not call the proof implementation/extractor.
Actual native source binding is checked by harbor_bus_scale_attention_proof.
"""
from __future__ import annotations

import copy


_SEAT = {"state": "not_overdue", "reason": "no_waiting_mail", "remedy": "",
         "hold_evidence_state": "not_read", "threshold_minutes": 15,
         "threshold_basis": "inherited ruled judgment; not a measured turn duration", "detail": "",
         "nullness": {"oldest_waiting_at": True, "oldest_waiting_age_seconds": True,
                      "next_check_at": True, "last_hold_started_at": True}}
_WAKE = {"state": "healthy", "breaker_state": "closed", "pause_state": "unknown",
         "waiter_exit_reason": None, "documented_entrypoint_resolves": True, "remedy": "",
         "read_finding": None, "nullness": {"claim_session": True, "last_seen_session": True,
                                           "oldest_unread": True, "waiter_receipt_age_minutes": True}}


def attention_sample(case, scale):
    """One closed valid sample with equal categories and distinct source hashes."""
    if case not in {"supervise", "watch"}:
        raise ValueError("literal attention sample requires an attention case")
    return {"schema_version": 1, "case": case,
            "nodes": {"architect": {"seat_attention": copy.deepcopy(_SEAT),
                                     "wake_health": copy.deepcopy(_WAKE) if case == "watch" else None}},
            "sources": {"cpu": ("1" if scale in {1, "1x"} else "3") * 64,
                        "diagnostic": ("2" if scale in {1, "1x"} else "4") * 64}}

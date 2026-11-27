"""The local dispatch ledger. A grant debit is the allow receipt itself."""
from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import sys
from typing import Literal, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .holds import _time, hold_projection
from .ids import uuid7_hex
from .jsonl import read_records, transact
from .registry import utc_now
from .quiet_window import dispatch_window
from .root import FloatiRoot, validate_identifier
from .t3_dispatch_records import (
    CHILD_LINK_KIND, DISPATCH_FIELDS, DISPATCH_KIND, GRANT_KIND, OPTIONAL_DISPATCH_FACTS,
    SHA256, T3_DISPATCH_KINDS, T3_DISPATCH_SPECS, _match, _utc,
)

LEDGER = Path("receipts/t3-dispatches.jsonl")
FACT_FIELDS = DISPATCH_FIELDS - {
    "schema_version", "kind", "tenant_id", "id", "timestamp", "decision", "reason", "grant_id",
}


def record(root, kind, timestamp, **fields):
    return {"schema_version": 1, "kind": kind, "tenant_id": root.tenant_id,
            "timestamp": timestamp, "id": T3_DISPATCH_SPECS[kind][0] + uuid7_hex(), **fields}


def checked_ledger(root):
    if root.resolve_relative(LEDGER) != root.tenant_home / LEDGER:
        raise ProtocolRefusal("t3_dispatch_ledger_redirected", "Use the native T3 dispatch ledger.")
    return LEDGER


def transaction(root, decide):
    return transact(root, checked_ledger(root), decide, allowed_kinds=T3_DISPATCH_KINDS)


@dataclass(frozen=True)
class ChildLink:
    status: Literal["linked", "post-hook-not-observed", "unparseable-response", "call-failed"]
    task_id: Optional[str] = None
    child_thread_id: Optional[str] = None


def _child_link(rows, dispatch_key):
    dispatches = []
    observations = []
    for row in rows:
        if row.get("dispatch_key") != dispatch_key:
            continue
        if row["kind"] == DISPATCH_KIND:
            dispatches.append(row)
        elif row["kind"] == CHILD_LINK_KIND:
            observations.append(row)
    if len(dispatches) > 1 or len(observations) > 1:
        raise IntegrityFailure("t3_child_link_ambiguous", "The ledger repeats a dispatch or child link.")
    dispatch = dispatches[0] if dispatches else None
    observation = observations[0] if observations else None
    if observation and (not dispatch or dispatch["decision"] == "denied"
                        or observation["parent_thread_id"] != dispatch["parent_thread_id"]):
        raise IntegrityFailure("t3_child_link_unbound", "The child link has no matching allowed dispatch.")
    return dispatch, observation


def read_child_link(root: FloatiRoot, dispatch_key: str) -> ChildLink:
    """Read the child observation for an exact pre-call dispatch key."""
    if not _match(dispatch_key, SHA256):
        raise ProtocolRefusal("t3_dispatch_key_invalid", "Supply the dispatch receipt's exact key.")
    rows = read_records(root, checked_ledger(root), allowed_kinds=T3_DISPATCH_KINDS)
    _, observation = _child_link(rows, dispatch_key)
    if observation is None:
        return ChildLink("post-hook-not-observed")
    return ChildLink(observation["status"], observation["task_id"], observation["child_thread_id"])


def record_child_link(root, *, dispatch_key, parent_thread_id, link):
    fields = dict(dispatch_key=dispatch_key, parent_thread_id=parent_thread_id,
                  status=link.status, task_id=link.task_id, child_thread_id=link.child_thread_id)

    def decide(rows):
        dispatch, prior = _child_link(rows, dispatch_key)
        if not dispatch or dispatch["decision"] == "denied" or dispatch["parent_thread_id"] != parent_thread_id:
            raise ProtocolRefusal("t3_child_link_unbound", "The child link has no matching allowed dispatch.")
        if prior:
            if any(prior[field] != value for field, value in fields.items()):
                raise ProtocolRefusal("t3_child_link_conflict", "The dispatch already has a different child observation.")
            return None, None
        return None, record(root, CHILD_LINK_KIND, utc_now(), **fields)

    transaction(root, decide)


def _active_grant(rows, facts, now):
    uses = Counter(row["grant_id"] for row in rows if row["kind"] == DISPATCH_KIND)
    for grant in rows:
        if (grant["kind"] == GRANT_KIND
                and grant["provider_instance"] == facts["provider_instance"]
                and grant["model"] in (None, facts["requested_model"])
                and _utc(grant["timestamp"]) <= now < _utc(grant["expires_at"])
                and (grant["count"] is None or uses[grant["id"]] < grant["count"])):
            return grant["id"]
    return None


def _load_hold(root, facts, now):
    node = facts.get("hold_node")
    if node is not None:
        projection = hold_projection(root, validate_identifier(node), now=now)
        if projection["partial_records"]:
            raise ProtocolRefusal("t3_hold_state_unavailable", "Project hold history is incomplete.")
        hold = next((row for row in projection["holds"] if row["age_seconds"] is not None), None)
        if hold is not None:
            detail = hold["reason"]
            expected = _time(hold["expected_release"])
            return "t3_project_hold", {
                "id": hold["id"],
                "detail": detail[:512] if isinstance(detail, str) else None,
                "expected_release": expected.isoformat().replace("+00:00", "Z") if expected else None,
            }
    window = dispatch_window(root)
    return ("t3_quiet_window", window) if window is not None else (None, None)


def _verdict(decision, reason, hold):
    return (decision, reason, hold) if hold is not None else (decision, reason)


def worker(argv, *, gap=False):
    root_path, tenant = argv
    root = FloatiRoot.open_direct_home(Path(root_path))
    if root.path != Path(root_path) or root.tenant_id != tenant:
        raise ProtocolRefusal("t3_dispatch_root_changed", "The T3 dispatch ledger identity changed.")
    request = json.load(sys.stdin)
    facts = {field: request[field] for field in FACT_FIELDS}
    facts.update({field: request[field] for field in OPTIONAL_DISPATCH_FACTS if field in request})
    gated = request["gated"]

    def decide(rows):
        for prior in rows:
            if prior["kind"] != DISPATCH_KIND or prior["dispatch_key"] != facts["dispatch_key"]:
                continue
            if any(prior.get(field) != value for field, value in facts.items()
                   if field in prior or field not in OPTIONAL_DISPATCH_FACTS):
                raise ProtocolRefusal("t3_dispatch_key_conflict", "The hook call identity was reused.")
            if gated and prior["reason"] == "t3_ungated":
                raise ProtocolRefusal("t3_dispatch_key_conflict", "The hook policy changed.")
            return _verdict(prior["decision"], prior["reason"], prior.get("hold")), None
        timestamp = utc_now()
        grant_id = None
        hold_reason, hold = None, None
        unavailable = gap
        if not gap and facts.get("dispatch_class") == "load":
            try:
                hold_reason, hold = _load_hold(root, facts, _utc(timestamp))
            except (IntegrityFailure, ProtocolRefusal, OSError, ValueError, RecursionError):
                unavailable = True
        if hold is not None:
            decision, reason = "denied", hold_reason
        elif facts["provider_instance"] is None:
            decision, reason = "allowed_with_gap", "t3_target_unresolved"
        elif unavailable:
            decision, reason = ("denied" if gated else "allowed_with_gap"), "t3_check_unavailable"
        elif gated:
            grant_id = _active_grant(rows, facts, _utc(timestamp))
            decision, reason = (("allowed", "t3_grant_consumed") if grant_id
                                else ("denied", "t3_grant_required"))
        else:
            decision, reason = "allowed", "t3_ungated"
        receipt = record(root, DISPATCH_KIND, timestamp, **facts,
                         decision=decision, reason=reason, grant_id=grant_id)
        if hold is not None:
            receipt["hold"] = hold
        return _verdict(decision, reason, hold), receipt

    print(json.dumps(transaction(root, decide)))

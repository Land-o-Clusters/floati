"""Version-one records for the local T3 dispatch ledger."""
from datetime import datetime
import re

from .t3_dispatch_hook import BRIEF, HOLD_REASONS, NODE, coordinate

DISPATCH_KIND = "t3_dispatch_receipt"
GRANT_KIND = "t3_dispatch_grant"
CHILD_LINK_KIND = "t3_dispatch_child_link"
COMMON = {"schema_version", "kind", "tenant_id", "id", "timestamp"}
DISPATCH_FIELDS = COMMON | {"provider_instance",
    "parent_thread_id", "requested_model", "role", "isolation", "task_digest",
    "brief_path", "base_ref", "dispatch_key", "decision", "reason", "grant_id",
}
OPTIONAL_DISPATCH_FACTS = {"dispatch_class", "hold_node"}
OPTIONAL_DISPATCH_FIELDS = OPTIONAL_DISPATCH_FACTS | {"hold"}
GRANT_FIELDS = COMMON | {"provider_instance", "model", "count", "expires_at", "human_act", "idempotency_key"}
CHILD_LINK_FIELDS = COMMON | {"dispatch_key", "parent_thread_id", "status", "task_id", "child_thread_id"}
T3_DISPATCH_SPECS = {
    DISPATCH_KIND: ("t3-dispatch-", frozenset(DISPATCH_FIELDS)),
    GRANT_KIND: ("t3-grant-", frozenset(GRANT_FIELDS)),
    CHILD_LINK_KIND: ("t3-child-link-", frozenset(CHILD_LINK_FIELDS)),
}
T3_DISPATCH_KINDS = frozenset(T3_DISPATCH_SPECS)
UUID7 = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
SHA256 = r"[0-9a-f]{64}"
T3_RESULT_ID = r"[A-Za-z0-9][A-Za-z0-9._:/%-]{0,511}"
DECISIONS = {
    "allowed": {"t3_ungated", "t3_grant_consumed"},
    "denied": {"t3_grant_required", "t3_check_unavailable"} | HOLD_REASONS,
    "allowed_with_gap": {"t3_check_unavailable", "t3_target_unresolved"},
}


def _match(value, pattern):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _utc(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("expected UTC timestamp")
    moment = datetime.fromisoformat(value[:-1] + "+00:00")
    if moment.tzinfo is None:
        raise ValueError("expected UTC timestamp")
    return moment


def validate_t3_dispatch(record, tenant, refuse):
    kind = record["kind"]
    prefix, fields = T3_DISPATCH_SPECS[kind]

    def require(condition):
        if not condition:
            refuse("t3_dispatch_record_invalid", "The T3 dispatch record has invalid fields.")

    optional = OPTIONAL_DISPATCH_FIELDS if kind == DISPATCH_KIND else set()
    require(fields <= record.keys() <= fields | optional and type(record["schema_version"]) is int
            and record["schema_version"] == 1 and record["tenant_id"] == tenant
            and _match(record["id"], prefix + UUID7))
    try:
        timestamp = _utc(record["timestamp"])
        if kind == GRANT_KIND:
            require(_utc(record["expires_at"]) > timestamp)
    except (ValueError, OverflowError):
        require(False)
    if kind == GRANT_KIND:
        require(coordinate(record["provider_instance"]) and coordinate(record["idempotency_key"]))
        require(record["model"] is None or coordinate(record["model"]))
        count = record["count"]
        require(count is None or (type(count) is int and 1 <= count <= 10000))
        act = record["human_act"]
        require(isinstance(act, dict) and act.keys() == {"channel", "rendered_digest"}
                and act["channel"] == "controlling_terminal"
                and _match(act["rendered_digest"], SHA256))
    elif kind == CHILD_LINK_KIND:
        require(_match(record["dispatch_key"], SHA256) and coordinate(record["parent_thread_id"]))
        status = record["status"]
        require(status in ("linked", "unparseable-response", "call-failed"))
        for field in ("task_id", "child_thread_id"):
            require(_match(record[field], T3_RESULT_ID) if status == "linked" else record[field] is None)
    else:
        for field in ("provider_instance", "requested_model", "parent_thread_id", "role", "isolation", "base_ref"):
            require(record[field] is None or coordinate(record[field]))
        require(record["task_digest"] is None or _match(record["task_digest"], SHA256))
        require(_match(record["dispatch_key"], SHA256))
        brief = record["brief_path"]
        require(brief is None or (_match(brief, BRIEF) and len(brief) <= 256))
        decision, reason = record["decision"], record["reason"]
        require(isinstance(decision, str) and isinstance(reason, str)
                and reason in DECISIONS.get(decision, ()))
        require(_match(record["grant_id"], "t3-grant-" + UUID7)
                if reason == "t3_grant_consumed" else record["grant_id"] is None)
        require(record.get("dispatch_class") in (None, "load", "measurement"))
        node = record.get("hold_node")
        require(node is None or _match(node, NODE))
        hold = record.get("hold")
        if reason in HOLD_REASONS:
            prefix = "quiet-window-" if reason == "t3_quiet_window" else "hold-"
            require(record.get("dispatch_class") == "load"
                    and (reason != "t3_project_hold" or node is not None)
                    and isinstance(hold, dict) and hold.keys() == {"id", "detail", "expected_release"}
                    and _match(hold["id"], prefix + UUID7))
            require(hold["detail"] is None or (isinstance(hold["detail"], str) and len(hold["detail"]) <= 512))
            try:
                if hold["expected_release"] is not None:
                    _utc(hold["expected_release"])
            except (ValueError, OverflowError):
                require(False)
        else:
            require(hold is None)
    return dict(record)

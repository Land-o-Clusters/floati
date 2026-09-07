"""Order registry retirement around the lane service's removal preflight."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any

from .errors import DurabilityFailure, ProtocolRefusal
from .jsonl import MAX_LEDGER_RECORDS, _encode_record


def preflight_retirement_records(root, existing, candidates, allowed_kinds):
    """Mirror the append admission before any lane directory can disappear."""
    if len(existing) + len(candidates) > MAX_LEDGER_RECORDS:
        raise ProtocolRefusal(
            "lane_retirement_preflight_failed", "registry record capacity is exhausted",
            "Preserve the node and its lanes; resolve registry capacity before retrying retirement.",
        )
    seen = {row["id"] for row in existing}
    for candidate in candidates:
        _encode_record(candidate, root.tenant_id, frozenset(allowed_kinds))
        if candidate["id"] in seen:
            raise ProtocolRefusal(
                "lane_retirement_preflight_failed", "retirement repeats a registry record id",
                "Create a fresh validated retirement preview before retrying; preserve all lanes.",
            )
        seen.add(candidate["id"])


@contextmanager
def retirement_lane_scope(root):
    """The common lock order is lane operation, registry, then lane ledger."""
    from .lane_workspaces import lane_workspace_guard

    completed: list[dict[str, Any]] = []
    with lane_workspace_guard(root):
        try:
            yield completed
        except (ProtocolRefusal, DurabilityFailure, OSError) as exc:
            if completed:
                removed = [row["removed"] for row in completed]
                raise DurabilityFailure(
                    "lane_retirement_incomplete",
                    f"lanes were closed but registry retirement is incomplete; removed={removed}; "
                    f"cause={getattr(exc, 'code', type(exc).__name__)}; preserve lane history, "
                    "inspect registry state, and retry retirement only after reconciling these coordinates",
                ) from exc
            raise


def close_retiring_node_lanes(root, node, completed):
    from .lane_workspaces import close_node_lanes

    completed.extend(close_node_lanes(root, node, lock_already_held=True))

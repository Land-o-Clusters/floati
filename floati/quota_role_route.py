"""Compare verified node/account bindings against one Puddle quota generation."""

import re

from .puddle_quota_snapshot import QuotaUnavailable
from .puddle_quota_v2 import (
    ACCOUNT_REF, FRESHNESS, current_account_reading, quota_stamp, read_account_quota, reading_index,
)

_BINDING_ID = re.compile(r"b-[a-z0-9][a-z0-9_-]{0,61}")
_BINDING_FIELDS = {"harness", "account_ref", "binding_id", "valid_from", "expires_at"}


def _binding_key(binding, at):
    if (not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS
            or not isinstance(binding["binding_id"], str)
            or not _BINDING_ID.fullmatch(binding["binding_id"])
            or not isinstance(binding["account_ref"], str)
            or not ACCOUNT_REF.fullmatch(binding["account_ref"])
            or not isinstance(binding["harness"], str)):
        raise ValueError("invalid account binding")
    start = quota_stamp(binding["valid_from"])
    expiry = quota_stamp(binding["expires_at"])
    if not start <= at < expiry or expiry - start > FRESHNESS:
        raise ValueError("account binding is not current")
    return binding["harness"], binding["account_ref"]


def _checked_binding(rows, node, harness, tenant, at):
    if len(rows) != 1:
        raise QuotaUnavailable("quota_account_unbound")
    row = rows[0]
    if (set(row) != _BINDING_FIELDS | {"tenant_id", "node_id"}
            or row["tenant_id"] != tenant or row["node_id"] != node
            or row["harness"] != harness):
        raise QuotaUnavailable("quota_account_unbound")
    binding = {field: row[field] for field in _BINDING_FIELDS}
    try:
        _binding_key(binding, at)
    except ValueError as exc:
        raise QuotaUnavailable("quota_account_unbound") from exc
    return binding


def _current_reading(binding, readings, at):
    return current_account_reading(readings.get(_binding_key(binding, at)), at)


def _unique_route(by_node, at):
    classes = {(row["harness"], row["value"]["window_kind"], row["value"]["window_seconds"])
               for row in by_node.values()}
    if len(classes) != 1 or next(iter(classes))[:2] != ("codex", "account_primary"):
        raise QuotaUnavailable("quota_window_incomparable")
    highest = max(row["value"]["remaining_bps"] for row in by_node.values())
    winners = [node for node, row in by_node.items() if row["value"]["remaining_bps"] == highest]
    if len(winners) != 1:
        raise QuotaUnavailable("quota_headroom_tied")
    winner = winners[0]
    age = (at - quota_stamp(by_node[winner]["observed_at"])).seconds
    return winner, age


def account_route(root, candidates, path, at, check_bindings):
    readings = read_account_quota(path, root.tenant_id)
    by_binding_node = {}
    for row in check_bindings(root, candidates, at):
        if isinstance(row, dict) and isinstance(row.get("node_id"), str):
            by_binding_node.setdefault(row["node_id"], []).append(row)
    bindings, by_node, compared = {}, {}, {}
    for node, harness in sorted(candidates.items()):
        binding = _checked_binding(by_binding_node.get(node, ()), node, harness, root.tenant_id, at)
        row = _current_reading(binding, readings, at)
        bindings[node], by_node[node] = binding, row
        compared[(row["harness"], row["account_ref"])] = row
    winner, age = _unique_route(by_node, at)
    return winner, age, bindings, [compared[key] for key in sorted(compared)]


def historical_account_route(bindings, rows, at):
    readings = reading_index(rows)
    keys = {_binding_key(binding, at) for binding in bindings.values()}
    if list(readings) != sorted(keys):
        raise ValueError("historical quota readings differ from the compared accounts")
    by_node = {node: _current_reading(binding, readings, at) for node, binding in bindings.items()}
    return _unique_route(by_node, at)


def binding_generations(bindings):
    return {node: (row["harness"], row["account_ref"], row["binding_id"])
            for node, row in bindings.items()}

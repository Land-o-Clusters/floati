"""Advisory quota evidence for an exact T3 delegation target."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from functools import cache

from .puddle_quota_snapshot import QuotaUnavailable
from .puddle_quota_v2 import (
    FRESHNESS, SOURCE, UnknownQuotaReading, current_account_reading, quota_stamp, read_account_quota,
)


@dataclass(frozen=True)
class T3Instance:
    instance_id: str
    harness: str
    # The trusted adapter uses the current registration and account generation.
    # An inventory listing by itself cannot establish continuity.
    current_since: datetime | None


@dataclass(frozen=True)
class T3Inventory:
    environment_id: str
    complete: bool
    instances: tuple[T3Instance, ...]


def _reference(tenant, environment, instance):
    fields = ("floati.t3-instance.v2", tenant, environment, instance)
    if any(not isinstance(field, str) or not field or "\0" in field for field in fields):
        raise QuotaUnavailable("t3_instance_binding_mismatch")
    try:
        return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()
    except UnicodeError as exc:
        raise QuotaUnavailable("t3_instance_binding_mismatch") from exc


def _current_instance(read_inventory, environment, instance_id, at):
    try:
        inventory = read_inventory(environment)
    except OSError as exc:
        raise QuotaUnavailable("t3_instance_inventory_unavailable") from exc
    if (not isinstance(inventory, T3Inventory) or inventory.complete is not True
            or inventory.environment_id != environment):
        raise QuotaUnavailable("t3_instance_inventory_unavailable")
    matches = [instance for instance in inventory.instances if instance.instance_id == instance_id]
    if not matches:
        raise QuotaUnavailable("t3_instance_removed")
    if len(matches) != 1:
        raise QuotaUnavailable("t3_instance_inventory_unavailable")
    instance = matches[0]
    since = instance.current_since
    if not isinstance(since, datetime) or since.utcoffset() is None or since > at:
        raise QuotaUnavailable("t3_instance_continuity_unknown")
    return instance


def _bound_reading(readings, reference, harness, account_ref):
    matches = [row for row in readings.values()
               if row.get("t3_provider_instance", {}).get("instance_ref") == reference]
    if len(matches) > 1:
        raise QuotaUnavailable("t3_instance_binding_conflict")
    if matches:
        row = matches[0]
        if row["harness"] == harness and (account_ref is None or account_ref == row["account_ref"]):
            return row
        raise QuotaUnavailable("t3_instance_binding_mismatch")
    if account_ref is not None and (harness, account_ref) in readings:
        mismatched = "t3_provider_instance" in readings[(harness, account_ref)]
    else:
        mismatched = any("t3_provider_instance" in row for (driver, _), row in readings.items()
                         if driver == harness)
    raise QuotaUnavailable("t3_instance_binding_mismatch" if mismatched
                           else "t3_instance_binding_missing")


def target_quota(root, path, environment, instance_id, read_inventory, *, at, account_ref=None):
    """Read a target again before use, through the trusted local inventory adapter.

    The adapter must lift fresh capabilities for this exact environment and
    attest registration continuity independently. The snapshot alone cannot
    prove that the target still exists or still refers to the same account.
    """
    return _target_quota(root, environment, instance_id, read_inventory,
                         lambda: _read_snapshot(root, path), at, account_ref)


def _read_snapshot(root, path):
    if path is None:
        raise QuotaUnavailable("quota_snapshot_not_selected")
    return read_account_quota(path, root.tenant_id)


def candidate_quota(root, path, environment, provider_ids, read_inventory, *, at):
    """Evaluate the proposed targets against one inventory and one export generation."""
    try:
        inventory = read_inventory(environment) if read_inventory is not None else None
    except (OSError, QuotaUnavailable):
        inventory = None

    @cache
    def snapshot():
        try:
            return _read_snapshot(root, path), None
        except QuotaUnavailable as exc:
            return None, str(exc)

    def read_snapshot():
        readings, code = snapshot()
        if code is not None:
            raise QuotaUnavailable(code)
        return readings

    return {
        "environment_id": environment,
        "evaluated_at": at.isoformat().replace("+00:00", "Z"),
        "source": SOURCE,
        "providers": [
            {"provider_instance": instance_id, **_target_quota(
                root, environment, instance_id, lambda _: inventory, read_snapshot, at, None,
            )}
            for instance_id in provider_ids
        ],
    }


def _target_quota(root, environment, instance_id, read_inventory, read_snapshot, at, account_ref):
    try:
        reference = _reference(root.tenant_id, environment, instance_id)
        instance = _current_instance(read_inventory, environment, instance_id, at)
        row = _bound_reading(read_snapshot(), reference, instance.harness, account_ref)
        current_account_reading(row, at)
        verified = quota_stamp(row["t3_provider_instance"]["verified_at"])
        if at < verified:
            raise QuotaUnavailable("t3_instance_binding_future")
        if at - verified >= FRESHNESS:
            raise QuotaUnavailable("t3_instance_binding_stale")
        if verified < instance.current_since:
            raise QuotaUnavailable("t3_instance_binding_changed")
        value = row["value"]
        if row["harness"] != "codex" or value["window_kind"] != "account_primary":
            raise QuotaUnavailable("quota_window_incomparable")
    except QuotaUnavailable as exc:
        result = {"outcome": "UNKNOWN", "code": str(exc)}
        if isinstance(exc, UnknownQuotaReading):
            result["reason"] = exc.reason
        return result
    return {
        "outcome": "MEASURED", "reading_id": row["reading_id"],
        "remaining_bps": value["remaining_bps"],
        "observed_at": row["observed_at"],
        "window_seconds": value["window_seconds"],
        "age_seconds": (at - quota_stamp(row["observed_at"])).seconds,
    }

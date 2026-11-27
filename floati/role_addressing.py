"""Preview and confirm one role-addressed question using measured quota."""

from datetime import datetime, timezone
from pathlib import Path

from .addressing import _digest, _refuse, load_recipient_preview
from .errors import ProtocolRefusal
from .puddle_quota_snapshot import (
    SOURCE, QuotaUnavailable, quota_utc, read_puddle_quota_snapshot, reading_interval,
)
from .puddle_quota_v2 import SNAPSHOT_NAME as ACCOUNT_SNAPSHOT_NAME, SOURCE as ACCOUNT_SOURCE
from .puddle_quota_v2 import UnknownQuotaReading
from .quota_role_route import account_route, binding_generations, historical_account_route
from .registry import Registry
from .root import validate_identifier
from .session_identity import IDENTIFIER_PATTERN
from .tide_catalog import canonical_harness


_MAX_CANDIDATES = 128


def is_role_address(address):
    return address.startswith("role:")


def _candidates(root, sender, role, now):
    registry = Registry(root)
    with registry.read_once():
        sender = registry.resolve_node_id(sender, field="sender", unknown_code="unknown_sender")
        entries, roles = {}, {}
        for record in registry.known_records():
            if record["kind"] == "registry_entry":
                entries[record["node_id"]] = record
            elif record["kind"] == "registry_role_record":
                roles[record["node_id"]] = record
        return {
            node: canonical_harness(entries[node]["role"])
            for node, assignment in sorted(roles.items())
            if node != sender and assignment["state"] == "active"
            and assignment["template_role"] == role
            and entries.get(node, {}).get("state") == "active"
            and registry.node_lease_state(node, now=now)["state"] in {"active", "not_leased"}
        }


def _winner(candidates, readings, at):
    harnesses = set(candidates.values())
    if harnesses - readings.keys():
        raise QuotaUnavailable("quota_reading_missing")
    for harness in sorted(harnesses):
        measured, expires = reading_interval(readings[harness])
        if not measured <= at < expires:
            raise QuotaUnavailable("quota_reading_stale")
    highest = max(readings[harness]["remaining_bps"] for harness in harnesses)
    winners = [node for node, harness in candidates.items()
               if readings[harness]["remaining_bps"] == highest]
    if len(winners) != 1:
        raise QuotaUnavailable("quota_headroom_tied")
    return winners[0]


def checked_account_bindings(root, candidates, at):
    """Boundary for the consented local adapter's synchronous identity check.

    There is no enrolled adapter on this runtime. The missing binding stays
    distinct from account handles merely observed in the quota file.
    """
    return ()


def role_recipient_preview(root, sender, address, snapshot_path, *, require_v2=False):
    role = validate_identifier(address[5:], "role")
    now = datetime.now(timezone.utc)
    candidates = _candidates(root, sender, role, now)
    try:
        if not candidates:
            raise QuotaUnavailable("quota_role_empty")
        if len(candidates) > _MAX_CANDIDATES:
            raise QuotaUnavailable("quota_role_too_large")
        if snapshot_path is not None and Path(snapshot_path).name == ACCOUNT_SNAPSHOT_NAME:
            winner, age, candidates, readings = account_route(
                root, candidates, snapshot_path, now, checked_account_bindings,
            )
            version, source = 3, ACCOUNT_SOURCE
        else:
            readings = read_puddle_quota_snapshot(snapshot_path)
            if require_v2:
                raise QuotaUnavailable("v2_evidence_required")
            now = datetime.now(timezone.utc)
            winner = _winner(candidates, readings, now)
            readings = {harness: readings[harness] for harness in sorted(set(candidates.values()))}
            measured, _ = reading_interval(readings[candidates[winner]])
            age = int((now - measured).total_seconds())
            version, source = 2, SOURCE
    except QuotaUnavailable as exc:
        unknown = {
            "outcome": "UNKNOWN", "code": str(exc), "address": address,
            "candidates": list(candidates),
            "remedy": "Choose a node and ask again with its exact ID.",
        }
        if isinstance(exc, UnknownQuotaReading):
            unknown["reason"] = exc.reason
        return unknown
    payload = {
        "preview_version": version, "tenant_id": root.tenant_id, "sender": sender,
        "address": address, "recipients": [winner],
        "route": {"source": source, "evaluated_at": now.isoformat(), "age_seconds": age,
                  "candidates": candidates, "readings": readings},
    }
    return dict(payload, digest=_digest(payload))


def validate_role_preview(preview, refuse):
    """Validate a historical decision from its embedded evidence alone."""
    try:
        version = preview["preview_version"]
        if (set(preview) != {"preview_version", "tenant_id", "sender", "address",
                             "recipients", "route", "digest"}
                or type(version) is not int or version not in (2, 3)
                or not is_role_address(preview["address"])):
            raise ValueError
        validate_identifier(preview["address"][5:], "role")
        validate_identifier(preview["sender"], "sender")
        validate_identifier(preview["tenant_id"], "tenant_id")
        route = preview["route"]
        if (not isinstance(route, dict)
                or set(route) != {"source", "evaluated_at", "age_seconds", "candidates", "readings"}
                or route["source"] != (ACCOUNT_SOURCE if version == 3 else SOURCE)
                or type(route["age_seconds"]) is not int):
            raise ValueError
        candidates, readings = route["candidates"], route["readings"]
        if (not isinstance(candidates, dict) or not 1 <= len(candidates) <= _MAX_CANDIDATES
                or preview["sender"] in candidates):
            raise ValueError
        for node in candidates:
            validate_identifier(node, "recipient")
        at = quota_utc(route["evaluated_at"])
        if version == 3:
            winner, age = historical_account_route(candidates, readings, at)
        else:
            if (not isinstance(readings, dict) or set(readings) != set(candidates.values())
                    or any(not IDENTIFIER_PATTERN.fullmatch(h) for h in candidates.values())):
                raise ValueError
            winner = _winner(candidates, readings, at)
            measured, _ = reading_interval(readings[candidates[winner]])
            age = int((at - measured).total_seconds())
        if (preview["recipients"] != [winner]
                or route["age_seconds"] != age
                or preview["digest"] != _digest({k: v for k, v in preview.items() if k != "digest"})):
            raise ValueError
    except (AttributeError, KeyError, ProtocolRefusal, TypeError, ValueError):
        refuse("address_preview_invalid", "The role preview has invalid quota evidence or a mismatched digest.")


def load_role_preview(root, sender, address, path):
    saved = load_recipient_preview(path, commands={"ask"})
    validate_role_preview(saved, _refuse)
    if (saved["tenant_id"], saved["sender"], saved["address"]) != (root.tenant_id, sender, address):
        _refuse("address_preview_invalid", "recipient preview names a different fleet, sender, or address")
    return saved


def confirm_role_preview(root, saved, snapshot_path):
    version = saved["preview_version"]
    current = role_recipient_preview(
        root, saved["sender"], saved["address"], snapshot_path, require_v2=version == 3,
    )
    if current.get("outcome") == "UNKNOWN":
        return current
    prior = saved["route"]
    route = current["route"]
    same_version = current["preview_version"] == version
    same_bindings = (
        binding_generations(route["candidates"]) == binding_generations(prior["candidates"])
        if same_version and version == 3 else route["candidates"] == prior["candidates"]
    )
    if (not same_version or not same_bindings or current["recipients"] != saved["recipients"]
            or route["readings"] != prior["readings"]
            or quota_utc(route["evaluated_at"]) < quota_utc(prior["evaluated_at"])):
        _refuse("address_preview_changed", "The role preview is out of date.",
                "Preview the role again or ask one exact node.")
    return None

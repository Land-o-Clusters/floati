"""Single-harness bootstrap and unambiguous CLI default resolution."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .errors import IntegrityFailure, ProtocolRefusal
from .jsonl import read_records_snapshot
from .planes import AuthorityGrantStore, MAX_TTL_SECONDS
from .records import validate_role
from .registry import Registry
from .root import FloatiRoot, validate_identifier


SOLO_AUTHORITY_SUBJECT = "solo-work"
SOLO_CONFIG = Path("solo.json")


def _now(value: Optional[datetime]) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if current.tzinfo is None or current.utcoffset() is None:
        raise ProtocolRefusal("time_invalid", "an aware UTC-compatible datetime is required")
    return current.astimezone(timezone.utc)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def validate_solo_bootstrap_inputs(node_id: str, harness: str) -> tuple[str, str]:
    """Validate all lexical solo inputs before a caller creates a direct home."""

    node = validate_identifier(node_id, "node")
    return node, validate_role(harness)


def _validate_config(raw: object) -> Dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version", "kind", "node_id", "harness", "authority_subject"
    }:
        raise IntegrityFailure(
            "solo_configuration_malformed", "solo configuration has an unexpected shape"
        )
    if raw.get("schema_version") != 0 or raw.get("kind") != "solo_configuration":
        raise IntegrityFailure(
            "solo_configuration_malformed", "solo configuration version is unsupported"
        )
    try:
        validate_identifier(raw.get("node_id"), "node")  # type: ignore[arg-type]
    except ProtocolRefusal as exc:
        raise IntegrityFailure(
            "solo_configuration_malformed", "solo node identifier is invalid"
        ) from exc
    try:
        harness = validate_role(raw.get("harness"), integrity=True)
    except IntegrityFailure as exc:
        raise IntegrityFailure(
            "solo_configuration_malformed", "solo harness must contain 1 to 64 characters"
        ) from exc
    if raw.get("authority_subject") != SOLO_AUTHORITY_SUBJECT:
        raise IntegrityFailure(
            "solo_configuration_malformed", "solo authority subject is unsupported"
        )
    return dict(raw)


def read_solo(root: FloatiRoot, *, required: bool = True) -> Optional[Dict[str, object]]:
    path = root.resolve_relative(SOLO_CONFIG)
    if not path.exists():
        if required:
            raise ProtocolRefusal(
                "solo_configuration_missing", "this root was not initialized for solo use"
            )
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityFailure(
            "solo_configuration_malformed", "solo configuration is not valid JSON"
        ) from exc
    return _validate_config(raw)


def _write_config(root: FloatiRoot, config: Dict[str, object]) -> None:
    path = root.resolve_relative(SOLO_CONFIG)
    encoded = (
        json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = read_solo(root)
        if existing != config:
            raise ProtocolRefusal(
                "solo_identity_mismatch", "existing solo identity does not match the request"
            )
        return
    try:
        written = os.write(descriptor, encoded)
        if written != len(encoded):
            raise OSError("short solo configuration write")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _registry_rows(root: FloatiRoot) -> list[Dict[str, object]]:
    return read_records_snapshot(
        root, "registry/entries.jsonl", allowed_kinds={"registry_entry"}
    )


def initialize_solo(
    root: FloatiRoot,
    node_id: str,
    harness: str,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    node, harness = validate_solo_bootstrap_inputs(node_id, harness)
    current = _now(now)
    config: Dict[str, object] = {
        "schema_version": 0,
        "kind": "solo_configuration",
        "node_id": node,
        "harness": harness,
        "authority_subject": SOLO_AUTHORITY_SUBJECT,
    }
    existing = read_solo(root, required=False)
    if existing is not None and existing != config:
        raise ProtocolRefusal(
            "solo_identity_mismatch", "existing solo identity does not match the request"
        )
    rows = _registry_rows(root)
    if not rows:
        Registry(root).register(node, harness)
    elif len(rows) != 1 or rows[0].get("node_id") != node or rows[0].get("role") != harness:
        raise ProtocolRefusal(
            "solo_identity_mismatch", "solo mode requires exactly the requested registered node"
        )
    grants = read_records_snapshot(
        root,
        f"authority-grants/{SOLO_AUTHORITY_SUBJECT}.jsonl",
        allowed_kinds={"authority_grant"},
    )
    grant: Dict[str, object]
    if grants:
        latest = grants[-1]
        if latest.get("holder") != node:
            raise ProtocolRefusal(
                "solo_authority_mismatch", "solo authority belongs to a different holder"
            )
        if latest.get("state") == "active" and current < _parse_time(str(latest["expires_at"])):
            grant = latest
        else:
            grant = AuthorityGrantStore(root).claim(
                SOLO_AUTHORITY_SUBJECT,
                node,
                MAX_TTL_SECONDS,
                MAX_TTL_SECONDS,
                current,
            )
    else:
        grant = AuthorityGrantStore(root).claim(
            SOLO_AUTHORITY_SUBJECT,
            node,
            MAX_TTL_SECONDS,
            MAX_TTL_SECONDS,
            current,
        )
    _write_config(root, config)
    return {**config, "authority_epoch": grant["epoch"], "expires_at": grant["expires_at"]}


def resolve_solo_node(root: FloatiRoot) -> str:
    config = read_solo(root)
    assert config is not None
    rows = _registry_rows(root)
    node = str(config["node_id"])
    if len(rows) != 1 or rows[0].get("node_id") != node or rows[0].get("state") != "active":
        raise ProtocolRefusal(
            "solo_identity_ambiguous", "solo defaults require exactly one matching active node"
        )
    return node


def resolve_solo_authority(
    root: FloatiRoot, node_id: str, now: Optional[datetime] = None
) -> Dict[str, object]:
    node = resolve_solo_node(root)
    if node != node_id:
        raise ProtocolRefusal(
            "solo_identity_mismatch", "explicit actor does not match the solo identity"
        )
    records = read_records_snapshot(
        root,
        f"authority-grants/{SOLO_AUTHORITY_SUBJECT}.jsonl",
        allowed_kinds={"authority_grant"},
    )
    if not records:
        raise ProtocolRefusal("solo_authority_missing", "solo authority is missing")
    latest = records[-1]
    if latest.get("holder") != node or latest.get("state") != "active":
        raise ProtocolRefusal("solo_authority_inactive", "solo authority is not active")
    if _now(now) >= _parse_time(str(latest["expires_at"])):
        raise ProtocolRefusal("solo_authority_expired", "solo authority has expired")
    return {
        "authority_subject": SOLO_AUTHORITY_SUBJECT,
        "authority_epoch": latest["epoch"],
    }

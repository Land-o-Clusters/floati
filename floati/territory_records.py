"""Closed version-one territory records (NET-TERRITORY-1).

A territory lease names absolute working paths one node holds until its
expiry. It is a liveness fact other readers consult; it gates nothing. The
record pins a path's shape (absolute and normalised), never a host prefix.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import posixpath
import re
import unicodedata

TERRITORY_RELATIVE = Path("territories/records.jsonl")
UUID = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
COMMON = {"schema_version", "id", "tenant_id", "timestamp", "kind"}
LEASE_PREFIX = "territory-lease-"
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 7 * 24 * 3600
MAX_PATHS = 64
MAX_PATH_BYTES = 4096
LEASE_FIELDS = COMMON | {"node_id", "paths", "ttl_seconds", "expires_at", "phase",
                         "released_at", "predecessor_receipt_id", "idempotency_key"}
TERRITORY_SPECS = {
    "territory_lease": (LEASE_PREFIX, frozenset(LEASE_FIELDS)),
}
TERRITORY_KINDS = frozenset(TERRITORY_SPECS)


def parse_timestamp(value):
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_timestamp(moment):
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def path_shape_valid(value):
    """Absolute, normalised, visible and bounded; any host prefix is accepted."""
    return (isinstance(value, str) and value.startswith("/")
            and posixpath.normpath(value) == value and not value.startswith("//")
            and len(value.encode("utf-8", "surrogatepass")) <= MAX_PATH_BYTES
            and not any(unicodedata.category(c).startswith("C") for c in value))


def visible_text(value, limit):
    return (isinstance(value, str) and value.strip() != ""
            and not any(unicodedata.category(c).startswith("C") for c in value)
            and len(value.encode("utf-8")) <= limit)


def key_valid(value):
    return isinstance(value, str) and 1 <= len(value) <= 128 and visible_text(value, 512)


def _relative_shape_valid(value):
    if value == ".":
        return True
    return (isinstance(value, str) and value != "" and not value.startswith("/")
            and posixpath.normpath(value) == value and value.split("/")[0] != ".."
            and len(value.encode("utf-8", "surrogatepass")) <= MAX_PATH_BYTES
            and not any(unicodedata.category(c).startswith("C") for c in value))


def _paths(value, label, error):
    """Each claimed path: its absolute spelling and, inside a Git worktree,
    its repository and repository-relative path as they were at claim time."""
    if type(value) is not list or not 1 <= len(value) <= MAX_PATHS:
        error("territory_record_invalid", label + " must hold 1 to 64 claimed paths")
    seen = set()
    for entry in value:
        if type(entry) is not dict or set(entry) != {"path", "repository", "relative"}:
            error("territory_record_invalid", label + " entries carry exactly path, repository and relative")
        if not path_shape_valid(entry["path"]) or entry["path"] in seen:
            error("territory_record_invalid", label + " paths must be unique absolute normalised paths")
        seen.add(entry["path"])
        if entry["repository"] is None:
            if entry["relative"] is not None:
                error("territory_record_invalid", "a path outside a repository has no relative path")
        elif not path_shape_valid(entry["repository"]) or not _relative_shape_valid(entry["relative"]):
            error("territory_record_invalid", "a repository path needs an absolute repository and a relative path")


def _reference(value, prefix, error, label):
    if not isinstance(value, str) or re.fullmatch(re.escape(prefix) + UUID, value) is None:
        error("territory_record_invalid", label + " must be one " + prefix + " reference")


def validate_territory_record(record, tenant, error):
    kind = record.get("kind")
    prefix, fields = TERRITORY_SPECS[kind]
    if set(record) != set(fields):
        error("territory_record_invalid", kind + " must carry exactly its closed fields")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        error("schema_version_invalid", "territory records require integer version one")
    if record["tenant_id"] != tenant:
        error("tenant_mismatch", "territory record belongs to another tenant")
    _reference(record["id"], prefix, error, "id")
    stamped = parse_timestamp(record["timestamp"])
    if stamped is None:
        error("territory_record_invalid", "timestamp must be a UTC millisecond timestamp")
    if not key_valid(record["idempotency_key"]):
        error("territory_record_invalid", "idempotency key must be visible text of 1 to 128 characters")
    phase = record["phase"]
    if not isinstance(record["node_id"], str) or IDENTIFIER.fullmatch(record["node_id"]) is None:
        error("territory_record_invalid", "node_id must be one node identifier")
    _paths(record["paths"], "paths", error)
    ttl = record["ttl_seconds"]
    if type(ttl) is not int or not MIN_TTL_SECONDS <= ttl <= MAX_TTL_SECONDS:
        error("territory_record_invalid", "ttl_seconds is outside its bounds")
    if parse_timestamp(record["expires_at"]) is None:
        error("territory_record_invalid", "expires_at must be a UTC millisecond timestamp")
    if phase == "leased":
        if record["released_at"] is not None or record["predecessor_receipt_id"] is not None:
            error("territory_record_invalid", "a new lease has no release or predecessor")
        if parse_timestamp(record["expires_at"]) != stamped + timedelta(seconds=ttl):
            error("territory_record_invalid", "a new lease expires exactly ttl_seconds after it starts")
    elif phase == "released":
        _reference(record["predecessor_receipt_id"], LEASE_PREFIX, error, "predecessor_receipt_id")
        if parse_timestamp(record["released_at"]) is None:
            error("territory_record_invalid", "a release carries its terminal timestamp")
    else:
        error("territory_record_invalid", "lease phase must be leased or released")
    return deepcopy(record)

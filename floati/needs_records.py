"""Closed version-one records of the needs-you inbox and its attention budget.

One root-wide ledger, ``receipts/needs/ledger.jsonl``, holds every kind below.
Only ``floati.needs_inbox`` authors rows. An item is what an agent raises for a
person, shaped as a bound record (the bound-record contract, version 1): an
actor, at most one coordinate, a claimed statement, and a ``supersedes`` link
for corrections; a read row and a budget row are each the receipt of a person's act at a
controlling terminal and bind the digest of the panel that person saw; a
dispatch-hold row records one dispatch held, released or sent while the person
had switched dispatch holding on and the unread review queue was over budget.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
import re
import unicodedata

LEDGER_DIRECTORY = PurePosixPath("receipts/needs")
COMMON = frozenset({"schema_version", "id", "tenant_id", "timestamp", "kind"})
ITEM_FIELDS = COMMON | {"actor", "item_kind", "statement", "coordinates", "supersedes",
                        "idempotency_key", "request_digest"}
READ_FIELDS = COMMON | {"item_ids", "seen", "human_act", "idempotency_key"}
CLOSE_FIELDS = COMMON | {"item_id", "actor", "reason", "idempotency_key"}
BUDGET_FIELDS = COMMON | {"limit", "hold_dispatches", "human_act", "idempotency_key"}
HOLD_FIELDS = COMMON | {"phase", "hold_id", "request_digest", "sender", "request", "reason",
                        "release_cause", "human_act", "idempotency_key"}
OBSERVED_FIELDS = COMMON | {"actor", "item_kind", "statement", "coordinates", "supersedes", "event_key"}
OBSERVATION_FIELDS = COMMON | {"actor", "source", "outcome", "cause"}
WAKE_FIELDS = COMMON | {"actor", "session_digest", "invocation_id", "items"}
MAX_ITEMS_PER_WAKE = 16
MAX_OBSERVED_COUNT = 2**31 - 1
NEEDS_SPECS = {
    "needs_item": ("needs-item-", frozenset(ITEM_FIELDS)),
    # An observed item shares the item id space, so a read or a close names it like any item.
    "needs_observed": ("needs-item-", frozenset(OBSERVED_FIELDS)),
    "needs_observation": ("needs-observation-", frozenset(OBSERVATION_FIELDS)),
    # The keys one seat's Stop waiter was woken for: "new" means new to that seat.
    "needs_wake": ("needs-wake-", frozenset(WAKE_FIELDS)),
    "needs_read": ("needs-read-", frozenset(READ_FIELDS)),
    "needs_close": ("needs-close-", frozenset(CLOSE_FIELDS)),
    "needs_budget": ("needs-budget-", frozenset(BUDGET_FIELDS)),
    "needs_dispatch_hold": ("needs-dispatch-hold-", frozenset(HOLD_FIELDS)),
}
NEEDS_KINDS = frozenset(NEEDS_SPECS)
ITEM_TYPES = frozenset({"review", "approval", "question"})
FRESHNESS_STATES = frozenset({"current", "stale", "unknown", "unbound"})
HOLD_PHASES = frozenset({"held", "released", "dispatched"})
RELEASE_CAUSES = frozenset({"owner", "under_budget", "hold_off"})
HUMAN_ACT_CHANNEL = "controlling_terminal"
REQUEST_FIELDS = frozenset({"recipient", "repo", "sha", "doc", "note_sha256",
                            "message_class", "idempotency_key"})
MAX_ITEMS_PER_READ = 64
MAX_SUBJECT_PATHS = 64
MAX_BUDGET = 10000

_UUID7 = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _timestamp(value, field, error):
    if not isinstance(value, str) or not value.endswith("Z"):
        error("needs_time_invalid", field + " must be a UTC timestamp ending in Z")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        error("needs_time_invalid", field + " must be a UTC timestamp ending in Z")
    if moment.tzinfo is None:
        error("needs_time_invalid", field + " must carry a UTC offset")


def _reference(value, prefix, field, error):
    if not isinstance(value, str) or re.fullmatch(re.escape(prefix) + _UUID7, value) is None:
        error("needs_reference_invalid", field + " must name one " + prefix + " record")


def _identifier(value, field, error):
    if not isinstance(value, str) or _IDENTIFIER.match(value) is None:
        error("needs_identifier_invalid", field + " must be one bounded identifier")


def _optional_identifier(value, field, error):
    if value is not None:
        _identifier(value, field, error)


def visible_text(value, limit):
    """One line of visible text within ``limit`` UTF-8 bytes."""
    return (isinstance(value, str) and bool(value.strip())
            and not any(unicodedata.category(c).startswith("C") for c in value)
            and len(value.encode("utf-8")) <= limit)


def _text(value, field, limit, error):
    if not visible_text(value, limit):
        error("needs_text_invalid", field + " must be one line of visible text within "
              + str(limit) + " UTF-8 bytes")


def _repository(value, error):
    if (not isinstance(value, str) or not value.startswith("/") or len(value.encode("utf-8")) > 4096
            or any(unicodedata.category(c).startswith("C") for c in value)):
        error("needs_coordinate_invalid", "a coordinate repository is one absolute path")


def _paths(paths, error, minimum=0):
    from .bound_record import file_path

    if not isinstance(paths, list) or not minimum <= len(paths) <= MAX_SUBJECT_PATHS or paths != sorted(set(paths)):
        error("needs_coordinate_invalid", "paths are at most 64 sorted unique repository-relative paths")
    for path in paths:
        file_path(path, error)


_SHAPES = {"ref": frozenset({"kind", "repository", "ref", "commit", "paths", "provenance"}),
           "commit": frozenset({"kind", "repository", "commit", "provenance"}),
           "files": frozenset({"kind", "repository", "files", "provenance"})}


def _coordinates(coordinates, error):
    """Bound-record coordinates (contract version 1): an inbox item names at most one."""
    if not isinstance(coordinates, list) or len(coordinates) > 1:
        error("needs_coordinate_invalid", "an inbox item names at most one coordinate")
    for coordinate in coordinates:
        kind = coordinate.get("kind") if isinstance(coordinate, dict) else None
        if kind == "brief":
            error("coordinate_kind_unsupported", "an inbox item binds a ref, a commit or files")
        if kind not in _SHAPES or frozenset(coordinate) != _SHAPES[kind]:
            error("needs_coordinate_invalid", "a coordinate is one closed ref, commit or files shape")
        _repository(coordinate["repository"], error)
        allowed = ("observed",) if kind == "files" else ("claimed",) if kind == "commit" else ("observed", "claimed")
        if coordinate["provenance"] not in allowed:
            error("needs_coordinate_invalid", "a coordinate carries the stamp its writer assigns")
        if kind in ("ref", "commit") and (not isinstance(coordinate["commit"], str)
                                          or _COMMIT.match(coordinate["commit"]) is None):
            error("needs_coordinate_invalid", "a coordinate commit is one full lowercase object id")
        if kind == "ref":
            ref = coordinate["ref"]
            if not isinstance(ref, str) or not 1 <= len(ref) <= 512 or ref.startswith("-"):
                error("needs_coordinate_invalid", "a coordinate ref is one ref name of at most 512 characters")
            _paths(coordinate["paths"], error)
        if kind == "files":
            files = coordinate["files"]
            if (not isinstance(files, list) or not all(
                    isinstance(entry, dict) and frozenset(entry) == {"path", "sha256"}
                    and isinstance(entry["sha256"], str) and _SHA256.match(entry["sha256"]) is not None
                    for entry in files)):
                error("needs_coordinate_invalid", "a files coordinate names each path with its SHA-256")
            _paths([entry["path"] for entry in files], error, minimum=1)


_THREAD_FIELDS = frozenset({"kind", "orchestrator", "project", "thread_id", "run_id", "status",
                            "pending_requests", "background_tasks", "provenance"})
_OBSERVED_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_OBSERVED_STATUS = re.compile(r"[a-z][a-z_-]{0,63}\Z")
OBSERVATION_OUTCOMES = frozenset({"read", "unknown"})


def _source(source, error):
    if (not isinstance(source, dict) or frozenset(source) != {"orchestrator", "project"}
            or not all(isinstance(source[key], str) and _IDENTIFIER.match(source[key]) is not None
                       for key in ("orchestrator", "project"))):
        error("needs_source_invalid", "a source names its orchestrator and project")


def _thread_coordinates(coordinates, error):
    """An observed item names exactly one thread event, stamped observed by its writer."""
    if not isinstance(coordinates, list) or len(coordinates) != 1:
        error("needs_coordinate_invalid", "an observed item names exactly one thread")
    (coordinate,) = coordinates
    if not isinstance(coordinate, dict) or frozenset(coordinate) != _THREAD_FIELDS or coordinate["kind"] != "thread":
        error("needs_coordinate_invalid", "an observed coordinate is one closed thread shape")
    if coordinate["provenance"] != "observed":
        error("needs_coordinate_invalid", "an observed coordinate is stamped observed")
    _source({"orchestrator": coordinate["orchestrator"], "project": coordinate["project"]}, error)
    for field in ("thread_id", "run_id"):
        if not isinstance(coordinate[field], str) or _OBSERVED_IDENTIFIER.match(coordinate[field]) is None:
            error("needs_coordinate_invalid", field + " must be one identifier")
    if not isinstance(coordinate["status"], str) or _OBSERVED_STATUS.match(coordinate["status"]) is None:
        error("needs_coordinate_invalid", "status must be one lowercase status word")
    _count(coordinate["pending_requests"], "pending_requests", error)
    _count(coordinate["background_tasks"], "background_tasks", error)


def _statement(statement, error, provenance="claimed"):
    if (not isinstance(statement, dict) or frozenset(statement) != {"title", "detail", "provenance"}
            or statement["provenance"] != provenance):
        error("needs_statement_invalid", "a statement is the title and detail with the stamp its writer assigns")
    _text(statement["title"], "title", 256, error)
    if statement["detail"] is not None:
        _text(statement["detail"], "detail", 2048, error)


def _human_act(act, error):
    if (not isinstance(act, dict) or frozenset(act) != {"channel", "rendered_digest"}
            or act["channel"] != HUMAN_ACT_CHANNEL
            or not isinstance(act["rendered_digest"], str)
            or _SHA256.match(act["rendered_digest"]) is None):
        error("needs_human_act_invalid",
              "this act records the controlling-terminal confirmation and the rendered panel digest")


def _count(value, field, error, limit=MAX_OBSERVED_COUNT):
    if type(value) is not int or not 0 <= value <= limit:
        error("needs_count_invalid", field + " must be a whole number from 0 to " + str(limit))


def _request(request, error):
    if not isinstance(request, dict) or frozenset(request) != REQUEST_FIELDS:
        error("needs_request_invalid", "a held dispatch records its closed request preview")
    for field in ("recipient", "repo", "doc"):
        if not isinstance(request[field], str) or not request[field] or len(request[field]) > 4096:
            error("needs_request_invalid", field + " must be one bounded string")
    if not isinstance(request["sha"], str) or _COMMIT.match(request["sha"]) is None:
        error("needs_request_invalid", "sha must be one full lowercase object id")
    if not isinstance(request["note_sha256"], str) or _SHA256.match(request["note_sha256"]) is None:
        error("needs_request_invalid", "note_sha256 must be one lowercase SHA-256")
    if request["message_class"] not in ("normal", "fyi"):
        error("needs_request_invalid", "a held dispatch is a normal or an fyi message")
    if request["idempotency_key"] is not None and (
            not isinstance(request["idempotency_key"], str) or not 1 <= len(request["idempotency_key"]) <= 128):
        error("needs_request_invalid", "idempotency_key must be 1 to 128 characters or null")


def _null(record, fields, error):
    for field in fields:
        if record[field] is not None:
            error("needs_phase_invalid", field + " does not belong to this phase")


def _reason(reason, release, error):
    if release:
        keys = {"budget", "unread_reviews"}
    else:
        keys = {"budget", "unread_reviews", "oldest"}
    if not isinstance(reason, dict) or frozenset(reason) != keys:
        error("needs_reason_invalid", "a hold reason names the budget, the unread review count and the oldest item")
    if not (release and reason["budget"] is None):
        _count(reason["budget"], "budget", error, MAX_BUDGET)
    _count(reason["unread_reviews"], "unread_reviews", error)
    if not release:
        oldest = reason["oldest"]
        if not isinstance(oldest, dict) or frozenset(oldest) != {"item_id", "filed_at", "title"}:
            error("needs_reason_invalid", "the oldest unread review names its id, filing time and title")
        _reference(oldest["item_id"], "needs-item-", "oldest.item_id", error)
        _timestamp(oldest["filed_at"], "oldest.filed_at", error)
        _text(oldest["title"], "oldest.title", 256, error)


def validate_needs_record(record, tenant, error):
    """Strict authoring and reading validation for every needs-inbox kind."""
    kind = record.get("kind") if isinstance(record, dict) else None
    if kind not in NEEDS_SPECS:
        error("record_kind_invalid", "record kind is not a needs-inbox kind")
    prefix, fields = NEEDS_SPECS[kind]
    if frozenset(record) != fields:
        error("record_fields_invalid", kind + " has missing or extra fields")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        error("schema_version_invalid", "needs-inbox records require integer version one")
    if record["tenant_id"] != tenant:
        error("tenant_mismatch", "record tenant does not match the selected root")
    _reference(record["id"], prefix, "id", error)
    _timestamp(record["timestamp"], "timestamp", error)
    if kind == "needs_item":
        _identifier(record["actor"], "actor", error)
        if record["item_kind"] not in ITEM_TYPES:
            error("needs_item_kind_invalid", "an item is a review, an approval or a question")
        _statement(record["statement"], error)
        _coordinates(record["coordinates"], error)
        if record["supersedes"] is not None:
            _reference(record["supersedes"], "needs-item-", "supersedes", error)
        _identifier(record["idempotency_key"], "idempotency_key", error)
        if not isinstance(record["request_digest"], str) or _SHA256.match(record["request_digest"]) is None:
            error("needs_request_invalid", "request_digest must be one lowercase SHA-256")
    elif kind == "needs_observed":
        _identifier(record["actor"], "actor", error)
        if record["item_kind"] not in ("review", "question"):
            error("needs_item_kind_invalid", "an observed item is a review or a question")
        _statement(record["statement"], error, "observed")
        _thread_coordinates(record["coordinates"], error)
        if record["supersedes"] is not None:
            _reference(record["supersedes"], "needs-item-", "supersedes", error)
        if not isinstance(record["event_key"], str) or _SHA256.match(record["event_key"]) is None:
            error("needs_event_key_invalid", "event_key must be one lowercase SHA-256")
    elif kind == "needs_observation":
        _identifier(record["actor"], "actor", error)
        _source(record["source"], error)
        if record["outcome"] not in OBSERVATION_OUTCOMES:
            error("needs_observation_invalid", "an observation outcome is read or unknown")
        cause = record["cause"]
        if record["outcome"] == "read":
            if cause is not None:
                error("needs_observation_invalid", "a read outcome has no cause")
        elif (not isinstance(cause, dict) or frozenset(cause) != {"code", "detail"}
              or not isinstance(cause["code"], str) or _IDENTIFIER.match(cause["code"]) is None):
            error("needs_observation_invalid", "an unknown outcome names its cause code and detail")
        else:
            _text(cause["detail"], "cause.detail", 1024, error)
    elif kind == "needs_wake":
        _identifier(record["actor"], "actor", error)
        for field in ("session_digest",):
            if not isinstance(record[field], str) or _SHA256.match(record[field]) is None:
                error("needs_wake_invalid", field + " must be one lowercase SHA-256")
        _identifier(record["invocation_id"], "invocation_id", error)
        items = record["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS_PER_WAKE:
            error("needs_wake_invalid", "a wake names 1 to 16 observed items")
        keys = []
        for entry in items:
            if not isinstance(entry, dict) or frozenset(entry) != {"item_id", "event_key", "coordinate"}:
                error("needs_wake_invalid", "each woken item names its id, event key and coordinate")
            _reference(entry["item_id"], "needs-item-", "items.item_id", error)
            if not isinstance(entry["event_key"], str) or _SHA256.match(entry["event_key"]) is None:
                error("needs_event_key_invalid", "event_key must be one lowercase SHA-256")
            _thread_coordinates([entry["coordinate"]], error)
            keys.append(entry["event_key"])
        if len(set(keys)) != len(keys):
            error("needs_wake_invalid", "a wake names each event key once")
    elif kind == "needs_read":
        ids, seen = record["item_ids"], record["seen"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ITEMS_PER_READ or len(set(ids)) != len(ids):
            error("needs_read_invalid", "a read names 1 to 64 unique items")
        for item in ids:
            _reference(item, "needs-item-", "item_ids", error)
        if not isinstance(seen, list) or len(seen) != len(ids):
            error("needs_read_invalid", "a read records what was seen for every item it names")
        for item, view in zip(ids, seen):
            if (not isinstance(view, dict) or frozenset(view) != {"item_id", "freshness", "coordinate"}
                    or view["item_id"] != item or view["freshness"] not in FRESHNESS_STATES
                    or not (view["coordinate"] is None or (isinstance(view["coordinate"], str)
                                                           and len(view["coordinate"]) <= 128))):
                error("needs_read_invalid", "each seen entry names its item, its freshness and its coordinate")
        _human_act(record["human_act"], error)
        _identifier(record["idempotency_key"], "idempotency_key", error)
    elif kind == "needs_close":
        _reference(record["item_id"], "needs-item-", "item_id", error)
        _identifier(record["actor"], "actor", error)
        _text(record["reason"], "reason", 1024, error)
        _identifier(record["idempotency_key"], "idempotency_key", error)
    elif kind == "needs_budget":
        if record["limit"] is not None:
            _count(record["limit"], "limit", error, MAX_BUDGET)
        if type(record["hold_dispatches"]) is not bool or (record["limit"] is None and record["hold_dispatches"]):
            error("needs_budget_invalid", "holding dispatches is true or false, and needs a declared budget")
        _human_act(record["human_act"], error)
        _identifier(record["idempotency_key"], "idempotency_key", error)
    else:
        phase = record["phase"]
        if phase not in HOLD_PHASES:
            error("needs_phase_invalid", "a dispatch hold is held, released or dispatched")
        if not isinstance(record["request_digest"], str) or _SHA256.match(record["request_digest"]) is None:
            error("needs_request_invalid", "request_digest must be one lowercase SHA-256")
        _identifier(record["sender"], "sender", error)
        if phase == "held":
            _null(record, ("hold_id", "release_cause", "human_act", "idempotency_key"), error)
            _request(record["request"], error)
            _reason(record["reason"], False, error)
        else:
            _reference(record["hold_id"], "needs-dispatch-hold-", "hold_id", error)
            _null(record, ("request",), error)
            if phase == "dispatched":
                _null(record, ("reason", "release_cause", "human_act", "idempotency_key"), error)
            elif record["release_cause"] == "owner":
                _null(record, ("reason",), error)
                _human_act(record["human_act"], error)
                _identifier(record["idempotency_key"], "idempotency_key", error)
            elif record["release_cause"] in ("under_budget", "hold_off"):
                _null(record, ("human_act", "idempotency_key"), error)
                _reason(record["reason"], True, error)
            else:
                error("needs_phase_invalid", "a release is the owner's act, the queue within budget or the hold switched off")
    return dict(record)

"""Bounded local Markdown intake and immutable snapshot primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .root import FloatiRoot
from .work import WorkLog, _now as _work_now


MAX_MARKDOWN_BYTES = 256 * 1024
INTAKE_SNAPSHOT_RELATIVE = Path("intake/v0/snapshots.jsonl")
_INTAKE_PAYLOAD_DIRECTORY = Path("intake/v0")
_TITLE = re.compile(r"^# +(\S.*?)\s*$")
_SNAPSHOT_ID = re.compile(r"^intake-snapshot-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$")
_RELATIVE_SOURCE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._/-]{0,255}$")
_GITHUB_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_GITHUB_SOURCE = re.compile(
    r"^github:([A-Za-z0-9._-]{1,100})/([A-Za-z0-9._-]{1,100})#([1-9][0-9]{0,9})$"
)
_OUTBOUND_RISK = {
    "comment": "low",
    "pr_link": "low",
    "label_add": "medium",
    "label_remove": "medium",
    "close": "high",
}
GITHUB_REQUEST_FIELDS = {
    "comment": frozenset({"body"}),
    "label_add": frozenset({"labels"}),
    "label_remove": frozenset({"label"}),
    "close": frozenset({"state", "state_reason"}),
    "pr_link": frozenset({"body"}),
}
_PR_LINK_BODY = re.compile(r"^Linked pull request: #([1-9][0-9]{0,9})$")
_CROSS_REPOSITORY_PR_LINK_BODY = re.compile(
    r"^Linked pull request: [A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9][0-9]{0,9}$"
)


def _unsafe(path: Path) -> ProtocolRefusal:
    return ProtocolRefusal(
        "intake_path_unsafe",
        f"intake path is unsafe: {path}",
    )


def _validate_file(path: Path) -> None:
    if not path.is_absolute() or path.is_symlink():
        raise _unsafe(path)
    try:
        if not path.is_file():
            raise _unsafe(path)
    except OSError as exc:
        raise _unsafe(path) from exc


def _validate_directory(directory: Path) -> Path:
    directory = Path(directory)
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise _unsafe(directory)
    try:
        return directory.resolve()
    except OSError as exc:
        raise _unsafe(directory) from exc


def _read_bytes(path: Path, limit: int) -> bytes:
    """Read one regular non-symlink file through an O_NOFOLLOW descriptor."""

    _validate_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise _unsafe(path) from exc
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode):
            raise _unsafe(path)
        chunks: list[bytes] = []
        total = 0
        while total <= limit:
            chunk = os.read(descriptor, limit + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                break
        return b"".join(chunks)
    except ProtocolRefusal:
        raise
    except OSError as exc:
        raise ProtocolRefusal(
            "intake_path_unsafe", f"Markdown file cannot be read: {path}"
        ) from exc
    finally:
        os.close(descriptor)


def parse_local_markdown(path: Path) -> Tuple[str, str]:
    """Parse one safe Markdown task into its H1 title and verbatim body."""

    path = Path(path)
    try:
        raw = _read_bytes(path, MAX_MARKDOWN_BYTES)
        if len(raw) > MAX_MARKDOWN_BYTES:
            raise ProtocolRefusal(
                "intake_markdown_too_large",
                f"Markdown file exceeds {MAX_MARKDOWN_BYTES} bytes: {path}",
            )
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolRefusal(
            "intake_markdown_not_utf8",
            f"Markdown file is not UTF-8: {path}",
        ) from exc
    except OSError as exc:
        raise ProtocolRefusal(
            "intake_path_unsafe",
            f"Markdown file cannot be read: {path}",
        ) from exc

    lines = text.splitlines(keepends=True)
    headings: List[Tuple[int, str]] = []
    for index, line in enumerate(lines):
        candidate = line.rstrip("\r\n")
        match = _TITLE.fullmatch(candidate)
        if match is not None:
            headings.append((index, match.group(1).strip()))
    if not headings:
        raise ProtocolRefusal(
            "intake_markdown_title_absent",
            f"Markdown file has no ATX H1 title: {path}",
        )
    if len(headings) > 1:
        raise ProtocolRefusal(
            "intake_markdown_title_ambiguous",
            f"Markdown file has more than one ATX H1 title: {path}",
        )
    title_line, title = headings[0]
    body = "".join(lines[title_line + 1 :])
    return title, body


def scan_directory(
    directory: Path,
) -> List[Dict[str, object]]:
    """Return one deterministic verdict for every direct Markdown entry."""

    directory = _validate_directory(Path(directory))
    try:
        entries = sorted(
            (entry for entry in directory.iterdir() if entry.name.endswith(".md")),
            key=lambda entry: entry.name,
        )
    except OSError as exc:
        raise ProtocolRefusal(
            "intake_path_unsafe",
            f"intake directory cannot be read: {directory}",
        ) from exc

    verdicts: List[Dict[str, object]] = []
    for entry in entries:
        row: Dict[str, object] = {
            "path": entry.name,
            "verdict": "eligible",
            "code": None,
            "detail": "eligible",
        }
        try:
            parse_local_markdown(entry)
        except ProtocolRefusal as exc:
            row.update(verdict="refused", code=exc.code, detail=exc.detail)
        verdicts.append(row)
    return verdicts


def _now(value: Optional[datetime]) -> datetime:
    """Use the work-log clock contract without a second wall-clock surface."""

    return _work_now(value)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal(
            "intake_payload_invalid", "intake payload cannot be canonicalized"
        ) from exc


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _local_path(directory: Path, relative_path: str) -> tuple[Path, str]:
    root = _validate_directory(directory)
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or not _RELATIVE_SOURCE.fullmatch(relative_path)
    ):
        raise _unsafe(Path(relative_path) if isinstance(relative_path, str) else Path("<invalid>"))
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative_path.split("/")):
        raise _unsafe(Path(relative_path))
    candidate = root / relative
    try:
        if candidate.is_symlink():
            raise _unsafe(candidate)
    except OSError as exc:
        raise _unsafe(candidate) from exc
    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise _unsafe(candidate) from exc
    if not resolved.name.endswith(".md"):
        raise _unsafe(candidate)
    return candidate, relative.as_posix()


def _write_payload(root: FloatiRoot, relative_path: str, payload: Dict[str, object]) -> None:
    path = root.resolve_relative(relative_path)
    parent = path.parent
    encoded = _canonical_json(payload)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise ProtocolRefusal(
            "intake_snapshot_payload_exists",
            f"intake snapshot payload already exists: {relative_path}",
        ) from exc
    except OSError as exc:
        raise ProtocolRefusal(
            "intake_payload_unavailable",
            f"intake snapshot payload cannot be created: {relative_path}",
        ) from exc
    try:
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("short payload write")
            offset += written
        os.fsync(descriptor)
    except OSError as exc:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise ProtocolRefusal(
            "intake_payload_unavailable",
            f"intake snapshot payload cannot be written: {relative_path}",
        ) from exc
    finally:
        os.close(descriptor)
    try:
        parent_descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as exc:
        raise ProtocolRefusal(
            "intake_payload_unavailable",
            f"intake snapshot payload directory cannot be synced: {parent}",
        ) from exc


def _snapshot_rows(root: FloatiRoot) -> list[Dict[str, object]]:
    return read_records_snapshot(
        root,
        INTAKE_SNAPSHOT_RELATIVE,
        allowed_kinds={"intake_snapshot"},
    )


def _already_adopted(rows: list[Dict[str, object]], key: str) -> None:
    prior = next((row for row in rows if row.get("idempotency_key") == key), None)
    if prior is not None:
        raise ProtocolRefusal(
            "intake_snapshot_already_adopted",
            f"intake snapshot already adopted as {prior['id']}",
        )


def _append_snapshot_record(root: FloatiRoot, record: Dict[str, object]) -> Dict[str, object]:
    def decide(existing: list[Dict[str, object]]) -> tuple[Dict[str, object], Dict[str, object]]:
        _already_adopted(existing, str(record["idempotency_key"]))
        return record, record

    return transact(
        root,
        INTAKE_SNAPSHOT_RELATIVE,
        decide,
        allowed_kinds={"intake_snapshot"},
    )


def adopt_local(
    root: FloatiRoot,
    directory: Path,
    relative_path: str,
    owner: Optional[str] = None,
    now: Optional[datetime] = None,
    *,
    after_work_item: Optional[Callable[[Dict[str, object]], None]] = None,
) -> Dict[str, object]:
    """Adopt one explicit local Markdown file into an immutable snapshot."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("root_required", "a validated writable root is required")
    source_path, source_relative = _local_path(directory, relative_path)
    current = _now(now)
    # The timestamp marks the start of the byte arrival/read boundary.
    title, body = parse_local_markdown(source_path)
    source_id = f"file:{source_relative}"
    content_digest = _digest(body.encode("utf-8"))
    metadata: Dict[str, object] = {}
    metadata_digest = _digest(_canonical_json(metadata))
    idempotency_key = _digest((source_id + "\x00" + content_digest).encode("utf-8"))
    _already_adopted(_snapshot_rows(root), idempotency_key)

    snapshot_id = "intake-snapshot-" + uuid7_hex()
    payload_path = f"{_INTAKE_PAYLOAD_DIRECTORY.as_posix()}/{snapshot_id}.json"
    payload: Dict[str, object] = {
        "schema_version": 0,
        "snapshot_id": snapshot_id,
        "source_kind": "local_markdown",
        "source_id": source_id,
        "retrieved_at_testimony": _timestamp(current),
        "content": body,
        "metadata": metadata,
    }
    _write_payload(root, payload_path, payload)

    if owner is None:
        from .solo import resolve_solo_node

        owner = resolve_solo_node(root)
    work_item = WorkLog(root).add(title, owner, [], now=current)
    if after_work_item is not None:
        after_work_item(work_item)

    record: Dict[str, object] = {
        "schema_version": 0,
        "id": snapshot_id,
        "tenant_id": root.tenant_id,
        "timestamp": _timestamp(current),
        "kind": "intake_snapshot",
        "source_kind": "local_markdown",
        "source_id": source_id,
        "retrieved_at_testimony": _timestamp(current),
        "content_digest": content_digest,
        "metadata_digest": metadata_digest,
        "payload_path": payload_path,
        "title": title,
        "work_item_id": work_item["id"],
        "idempotency_key": idempotency_key,
    }
    _append_snapshot_record(root, record)
    return {
        "snapshot_id": snapshot_id,
        "work_item_id": work_item["id"],
        "payload_path": payload_path,
        "source_id": source_id,
        "idempotency_key": idempotency_key,
    }


def adopt_github(
    root: FloatiRoot,
    source_owner: str,
    repository: str,
    issue_number: int,
    gh_executable: str,
    owner: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """Adopt one explicitly named GitHub issue into an immutable snapshot."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("root_required", "a validated writable root is required")
    if (
        not isinstance(source_owner, str)
        or _GITHUB_NAME.fullmatch(source_owner) is None
        or not isinstance(repository, str)
        or _GITHUB_NAME.fullmatch(repository) is None
    ):
        raise ProtocolRefusal(
            "github_repository_invalid", "GitHub repository must use owner/repository coordinates"
        )
    if (
        isinstance(issue_number, bool)
        or not isinstance(issue_number, int)
        or not 1 <= issue_number <= 9_999_999_999
    ):
        raise ProtocolRefusal(
            "github_issue_invalid", "GitHub issue number must be in [1, 9999999999]"
        )

    from .gh_process import read_github_issue

    metadata, current = read_github_issue(
        gh_executable, source_owner, repository, issue_number, now=now
    )
    title = str(metadata["title"])
    body = str(metadata["body"])
    source_id = f"github:{source_owner}/{repository}#{issue_number}"
    content_digest = _digest(body.encode("utf-8"))
    metadata_digest = _digest(_canonical_json(metadata))
    idempotency_key = _digest((source_id + "\x00" + content_digest).encode("utf-8"))
    _already_adopted(_snapshot_rows(root), idempotency_key)

    snapshot_id = "intake-snapshot-" + uuid7_hex()
    payload_path = f"{_INTAKE_PAYLOAD_DIRECTORY.as_posix()}/{snapshot_id}.json"
    payload: Dict[str, object] = {
        "schema_version": 0,
        "snapshot_id": snapshot_id,
        "source_kind": "github_issue",
        "source_id": source_id,
        "retrieved_at_testimony": _timestamp(current),
        "content": body,
        "metadata": metadata,
    }
    _write_payload(root, payload_path, payload)

    if owner is None:
        from .solo import resolve_solo_node

        owner = resolve_solo_node(root)
    work_item = WorkLog(root).add(title, owner, [], now=current)
    record: Dict[str, object] = {
        "schema_version": 0,
        "id": snapshot_id,
        "tenant_id": root.tenant_id,
        "timestamp": _timestamp(current),
        "kind": "intake_snapshot",
        "source_kind": "github_issue",
        "source_id": source_id,
        "retrieved_at_testimony": _timestamp(current),
        "content_digest": content_digest,
        "metadata_digest": metadata_digest,
        "payload_path": payload_path,
        "title": title,
        "work_item_id": work_item["id"],
        "idempotency_key": idempotency_key,
    }
    _append_snapshot_record(root, record)
    return {
        "snapshot_id": snapshot_id,
        "work_item_id": work_item["id"],
        "payload_path": payload_path,
        "source_id": source_id,
        "idempotency_key": idempotency_key,
    }


def _snapshot_id(value: object) -> str:
    if not isinstance(value, str) or _SNAPSHOT_ID.fullmatch(value) is None:
        raise ProtocolRefusal(
            "snapshot_id_invalid", "snapshot id must use the intake snapshot UUIDv7 prefix"
        )
    return value


def resolve_snapshot(root: FloatiRoot, snapshot_id: str) -> Dict[str, object]:
    """Resolve one payload only after checking its immutable testimony."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("root_required", "a validated root is required")
    _snapshot_id(snapshot_id)
    row = next((item for item in _snapshot_rows(root) if item["id"] == snapshot_id), None)
    if row is None:
        raise ProtocolRefusal(
            "intake_snapshot_not_found", f"intake snapshot does not exist: {snapshot_id}"
        )
    payload_path = str(row["payload_path"])
    try:
        path = root.resolve_relative(payload_path)
        raw = _read_bytes(path, MAX_MARKDOWN_BYTES * 4)
        payload = json.loads(raw.decode("utf-8"))
    except (ProtocolRefusal, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal(
            "intake_snapshot_digest_mismatch",
            f"intake snapshot payload cannot be resolved: {payload_path}",
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version", "snapshot_id", "source_kind", "source_id",
        "retrieved_at_testimony", "content", "metadata",
    }:
        raise ProtocolRefusal(
            "intake_snapshot_digest_mismatch",
            f"intake snapshot payload shape changed: {payload_path}",
        )
    try:
        canonical = _canonical_json(payload)
    except ProtocolRefusal as exc:
        raise ProtocolRefusal(
            "intake_snapshot_digest_mismatch", f"intake snapshot payload changed: {payload_path}"
        ) from exc
    if canonical != raw:
        raise ProtocolRefusal(
            "intake_snapshot_digest_mismatch",
            f"intake snapshot payload bytes changed: {payload_path}",
        )
    if (
        payload.get("schema_version") != 0
        or payload.get("snapshot_id") != row["id"]
        or payload.get("source_kind") != row["source_kind"]
        or payload.get("source_id") != row["source_id"]
        or payload.get("retrieved_at_testimony") != row["retrieved_at_testimony"]
        or not isinstance(payload.get("content"), str)
        or not isinstance(payload.get("metadata"), dict)
        or (
            payload.get("source_kind") == "local_markdown"
            and payload.get("metadata") != {}
        )
        or _digest(payload["content"].encode("utf-8")) != row["content_digest"]
        or _digest(_canonical_json(payload["metadata"])) != row["metadata_digest"]
    ):
        raise ProtocolRefusal(
            "intake_snapshot_digest_mismatch",
            f"intake snapshot payload digest does not match row: {payload_path}",
        )
    return payload


def _payload_candidates(root: FloatiRoot) -> list[str]:
    directory = root.resolve_relative(_INTAKE_PAYLOAD_DIRECTORY)
    if not directory.exists():
        return []
    if directory.is_symlink() or not directory.is_dir():
        raise ProtocolRefusal("intake_path_unsafe", "intake snapshot directory is unsafe")
    candidates = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.name.startswith("intake-snapshot-") and path.name.endswith(".json"):
            if path.is_symlink() or not path.is_file():
                raise ProtocolRefusal("intake_path_unsafe", f"intake payload path is unsafe: {path}")
            candidates.append(f"{_INTAKE_PAYLOAD_DIRECTORY.as_posix()}/{path.name}")
    return candidates


def show_snapshots(root: FloatiRoot, snapshot_id: Optional[str] = None) -> list[Dict[str, object]]:
    """Read snapshots and fail closed on a visible orphan work item."""

    rows = _snapshot_rows(root)
    candidates = _payload_candidates(root)
    referenced = {str(row["work_item_id"]) for row in rows}
    for item in WorkLog(root).show():
        item_id = str(item["id"])
        if item_id not in referenced and not item.get("artifact_bindings") and candidates:
            raise ProtocolRefusal(
                "intake_snapshot_row_absent",
                f"intake snapshot row absent for work item {item_id}; payload path {candidates[0]}",
            )
    selected = rows if snapshot_id is None else [
        row for row in rows if row["id"] == _snapshot_id(snapshot_id)
    ]
    if snapshot_id is not None and not selected:
        raise ProtocolRefusal(
            "intake_snapshot_not_found", f"intake snapshot does not exist: {snapshot_id}"
        )
    return [
        {"record": row, "payload": resolve_snapshot(root, str(row["id"]))}
        for row in selected
    ]


def _github_target(root: FloatiRoot, snapshot_id: str) -> tuple[Dict[str, object], str]:
    row = next(
        (item for item in _snapshot_rows(root) if item["id"] == _snapshot_id(snapshot_id)),
        None,
    )
    if row is None:
        raise ProtocolRefusal(
            "intake_snapshot_not_found", f"intake snapshot does not exist: {snapshot_id}"
        )
    resolve_snapshot(root, snapshot_id)
    source_id = str(row["source_id"])
    match = _GITHUB_SOURCE.fullmatch(source_id)
    if row["source_kind"] != "github_issue" or match is None:
        raise ProtocolRefusal(
            "intake_outbound_source_invalid",
            "outbound GitHub operations require a resolved GitHub issue snapshot",
        )
    target: Dict[str, object] = {
        "kind": "github_resource",
        "owner": match.group(1),
        "repo": match.group(2),
        "number": int(match.group(3)),
    }
    return target, source_id


def _outbound_request(operation: str, request: object) -> tuple[str, Dict[str, object]]:
    risk_class = _OUTBOUND_RISK.get(operation)
    if risk_class is None:
        raise ProtocolRefusal(
            "intake_operation_invalid",
            "intake operation must be comment, label_add, label_remove, close, or pr_link",
        )
    if not isinstance(request, dict) or not all(isinstance(key, str) for key in request):
        raise ProtocolRefusal(
            "intake_request_invalid", "intake outbound request must be one JSON object"
        )
    if set(request) != GITHUB_REQUEST_FIELDS[operation]:
        raise ProtocolRefusal(
            "intake_request_invalid",
            "intake request does not match the selected operation's closed field shape",
        )
    canonical_request = dict(request)
    if operation == "comment":
        body = request["body"]
        if not isinstance(body, str) or not body.strip():
            raise ProtocolRefusal(
                "intake_request_body_empty", "GitHub comment body must not be empty"
            )
        if len(body) > 65_536:
            raise ProtocolRefusal(
                "intake_request_body_too_large",
                "GitHub comment body exceeds 65536 characters",
            )
        if "\x00" in body:
            raise ProtocolRefusal(
                "intake_request_invalid", "GitHub comment body contains a NUL character"
            )
    elif operation == "label_add":
        labels = request["labels"]
        if not isinstance(labels, list) or not labels:
            raise ProtocolRefusal(
                "intake_label_invalid", "label_add requires at least one valid label"
            )
        for label in labels:
            _validate_github_label(label)
        canonical_request["labels"] = sorted(set(labels))
    elif operation == "label_remove":
        _validate_github_label(request["label"])
    elif operation == "close":
        if request["state"] != "closed" or request["state_reason"] not in {
            "completed",
            "not_planned",
        }:
            raise ProtocolRefusal(
                "intake_request_invalid",
                "close requires state=closed and an explicit ruled state_reason",
            )
    else:
        body = request["body"]
        if isinstance(body, str) and _CROSS_REPOSITORY_PR_LINK_BODY.fullmatch(body):
            raise ProtocolRefusal(
                "intake_pr_link_cross_repo_unruled",
                "cross-repository pull request links are not ruled in v1",
            )
        if not isinstance(body, str) or _PR_LINK_BODY.fullmatch(body) is None:
            raise ProtocolRefusal(
                "intake_request_invalid",
                "pr_link requires the fixed same-repository comment template",
            )
    # Canonicalization is also the bounded JSON-value/type validation boundary.
    _canonical_json(canonical_request)
    return risk_class, dict(sorted(canonical_request.items()))


def _validate_github_label(label: object) -> None:
    if (
        not isinstance(label, str)
        or not label
        or len(label) > 50
        or label != label.strip()
        or any(character in label for character in ("\x00", "\r", "\n"))
    ):
        raise ProtocolRefusal(
            "intake_label_invalid",
            "GitHub label must be non-empty, at most 50 characters, and unpadded",
        )


def preview_github_mutation(
    root: FloatiRoot,
    snapshot_id: str,
    operation: str,
    request: Dict[str, object],
) -> Dict[str, object]:
    """Return a pure, policy-aware preview for one GitHub mutation intent."""

    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal("root_required", "a validated root is required")
    target, source_id = _github_target(root, snapshot_id)
    risk_class, canonical_request = _outbound_request(operation, request)
    request_digest = _digest(
        _canonical_json(
            {
                "snapshot_id": snapshot_id,
                "operation": operation,
                "request": canonical_request,
                "target": target,
            }
        )
    )

    from .policy import RepositoryPolicy

    policy = RepositoryPolicy.load(root.path / "FLOATI.toml")
    return {
        "schema_version": 0,
        "snapshot_id": snapshot_id,
        "source_id": source_id,
        "operation": operation,
        "target": target,
        "request": canonical_request,
        "request_digest": request_digest,
        "risk_class": risk_class,
        "approval_required": policy.effect_approval_required(risk_class),
        "will_dispatch": False,
    }


def dispatch_github_mutation(
    root: FloatiRoot,
    snapshot_id: str,
    operation: str,
    request: Dict[str, object],
    *,
    confirm_digest: str,
    run_id: str,
    item_id: str,
    attempt_id: str,
    fence_token: str,
    requested_by: Optional[str] = None,
    approval_request_id: Optional[str] = None,
    approval_decision_id: Optional[str] = None,
    approval_consumption_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, object]:
    """Carry an exact preview digest into the sealed existing effect controller."""

    preview = preview_github_mutation(root, snapshot_id, operation, request)
    request_digest = str(preview["request_digest"])
    if confirm_digest != request_digest:
        raise ProtocolRefusal(
            "intake_preview_digest_mismatch",
            "dispatch confirmation must equal the recomputed intake preview digest",
        )
    if requested_by is None:
        row = next(item for item in _snapshot_rows(root) if item["id"] == snapshot_id)
        work_items = WorkLog(root).show(str(row["work_item_id"]))
        if not work_items:
            raise ProtocolRefusal(
                "intake_snapshot_work_item_missing",
                "intake dispatch requires its durable snapshot work item",
            )
        requested_by = str(work_items[0]["owner"])

    public_target = preview["target"]
    coordinate = (
        f"{public_target['owner']}/{public_target['repo']}#{public_target['number']}"
    )
    idempotency_key = request_digest

    from .approvals import ApprovalLedger
    from .effects import EffectController, EffectLedger
    from .policy import RepositoryPolicy
    from .runtruth import RunLedger

    controller = EffectController(
        EffectLedger(root),
        RunLedger(root),
        RepositoryPolicy.load(root.path / "FLOATI.toml"),
        ApprovalLedger(root),
    )
    return controller.intent(
        run_id=run_id,
        item_id=item_id,
        attempt_id=attempt_id,
        fence_token=fence_token,
        effect_type="github_mutation",
        target={
            "kind": "github_resource",
            "coordinate": coordinate,
            "identity_digest": _digest(coordinate.encode("utf-8")),
        },
        request_digest=request_digest,
        idempotency_key=idempotency_key,
        expected_confirmation={
            "kind": "github_idempotency_marker",
            "locator": idempotency_key,
            "expected_digest": request_digest,
        },
        reconciliation_adapter="github_explicit",
        risk_class=preview["risk_class"],
        budget_claim=[],
        requested_by=requested_by,
        approval_request_id=approval_request_id,
        approval_decision_id=approval_decision_id,
        approval_consumption_id=approval_consumption_id,
        now=now,
    )

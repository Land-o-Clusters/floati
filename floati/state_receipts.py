"""Metadata-only receipts proving a node state-file flush was observed."""

from __future__ import annotations

import json
import errno
import os
import re
import stat
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Tuple

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .root import FloatiRoot, validate_identifier


_UUID7_HEX = r"[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}"
_RECEIPT_ID = re.compile(r"^node-state-flush-" + _UUID7_HEX + r"$")
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "id",
        "tenant_id",
        "timestamp",
        "kind",
        "node_id",
        "state_file",
        "operation",
        "observed_mtime_ns",
        "observed_size_bytes",
        "prior_mtime_ns",
    }
)
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"
)


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _refuse("state_receipt_timestamp_invalid", "receipt timestamp must be timezone-aware")
    stamp = value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    if _TIMESTAMP.fullmatch(stamp) is None:
        _refuse("state_receipt_timestamp_invalid", "receipt timestamp is not canonical UTC")
    return stamp


def _validate_prior(value: object) -> Optional[int]:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        _refuse("state_receipt_prior_invalid", "prior mtime must be a non-negative integer")
    return value


def _safe_text(value: object, detail: str, *, multiline: bool = False) -> str:
    if not isinstance(value, str) or not value:
        _refuse("state_receipt_output_invalid", detail)
    if any(
        (ord(character) < 32 and (not multiline or character != "\n"))
        or ord(character) == 127
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    ):
        _refuse("state_receipt_output_invalid", detail)
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolRefusal("state_receipt_output_invalid", detail) from exc
    return value


def _canonical_state_file(root: FloatiRoot, node_id: str) -> Path:
    nodes = root.path / "nodes"
    workspace = nodes / node_id
    candidate = workspace / "STATE.md"
    for path in (nodes, workspace, candidate):
        if path.is_symlink():
            _refuse("state_receipt_symlink", "state vessel path contains a symlink")
    try:
        resolved_root = root.path.resolve()
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise ProtocolRefusal(
            "state_receipt_path_invalid", "state vessel path escapes the Floati root"
        ) from exc
    if resolved_candidate != candidate:
        _refuse("state_receipt_path_invalid", "state vessel path resolves away from its canonical location")
    return candidate


def _open_directory(path: Path, *, parent: Optional[int] = None, name: Optional[str] = None) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    try:
        descriptor = (
            os.open(name, flags, dir_fd=parent)
            if parent is not None and name is not None
            else os.open(path, flags)
        )
    except FileNotFoundError as exc:
        raise ProtocolRefusal("state_receipt_missing", "canonical state-file directory is missing") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP or path.is_symlink():
            raise ProtocolRefusal(
                "state_receipt_symlink", "canonical state-file path must not contain a symlink"
            ) from exc
        raise ProtocolRefusal(
            "state_receipt_path_invalid", "canonical state-file directory could not be opened safely"
        ) from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISDIR(identity.st_mode):
            _refuse("state_receipt_path_invalid", "canonical state-file parent must be a directory")
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise ProtocolRefusal(
            "state_receipt_path_invalid", "canonical state-file directory identity is unavailable"
        ) from exc
    except ProtocolRefusal:
        os.close(descriptor)
        raise


def _observe_state_file(root: FloatiRoot, node_id: str) -> Tuple[int, int]:
    path = _canonical_state_file(root, node_id)
    descriptors = []
    try:
        root_descriptor = _open_directory(root.path)
        descriptors.append(root_descriptor)
        nodes_descriptor = _open_directory(
            root.path / "nodes",
            parent=root_descriptor,
            name="nodes",
        )
        descriptors.append(nodes_descriptor)
        workspace_descriptor = _open_directory(
            path.parent,
            parent=nodes_descriptor,
            name=node_id,
        )
        descriptors.append(workspace_descriptor)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open("STATE.md", flags, dir_fd=workspace_descriptor)
        except FileNotFoundError as exc:
            raise ProtocolRefusal("state_receipt_missing", "canonical STATE.md is missing") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP or path.is_symlink():
                raise ProtocolRefusal(
                    "state_receipt_symlink", "canonical STATE.md must not be a symlink"
                ) from exc
            raise ProtocolRefusal(
                "state_receipt_path_invalid", "canonical STATE.md could not be opened safely"
            ) from exc
        try:
            identity = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if not stat.S_ISREG(identity.st_mode):
            _refuse("state_receipt_not_regular", "canonical STATE.md must be a regular file")
        return identity.st_mtime_ns, identity.st_size
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


class StateFileFlushReceipt:
    """Observe one canonical STATE.md vessel without reading its content."""

    def __init__(
        self,
        root: FloatiRoot,
        node_id: str,
        *,
        id_factory: Callable[[], str] = uuid7_hex,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(root, FloatiRoot):
            _refuse("state_receipt_root_invalid", "receipt requires a validated FloatiRoot")
        self.root = root
        self.node_id = validate_identifier(node_id, "node")
        if not callable(id_factory) or not callable(now):
            _refuse("state_receipt_factory_invalid", "receipt factories must be callable")
        self.id_factory = id_factory
        self.now = now

    @property
    def state_file(self) -> Path:
        return _canonical_state_file(self.root, self.node_id)

    def record(self, *, prior_mtime_ns: Optional[int] = None) -> Dict[str, object]:
        prior = _validate_prior(prior_mtime_ns)
        state_file = self.state_file
        observed_mtime, observed_size = _observe_state_file(self.root, self.node_id)
        if prior is not None and observed_mtime <= prior:
            _refuse(
                "state_receipt_mtime_not_newer",
                "STATE.md mtime did not advance beyond the prior flush observation",
            )
        try:
            generated_id = self.id_factory()
        except Exception as exc:
            raise ProtocolRefusal(
                "state_receipt_id_invalid", "receipt ID factory failed"
            ) from exc
        if not isinstance(generated_id, str) or _RECEIPT_ID.fullmatch(
            "node-state-flush-" + generated_id
        ) is None:
            _refuse("state_receipt_id_invalid", "receipt ID factory did not return UUIDv7 hex")
        return {
            "schema_version": 0,
            "id": "node-state-flush-" + generated_id,
            "tenant_id": self.root.tenant_id,
            "timestamp": _timestamp(self.now()),
            "kind": "node_state_flush_receipt",
            "node_id": self.node_id,
            "state_file": str(state_file),
            "operation": "flush",
            "observed_mtime_ns": observed_mtime,
            "observed_size_bytes": observed_size,
            "prior_mtime_ns": prior,
        }

    def to_json(
        self,
        receipt: Optional[Mapping[str, object]] = None,
        *,
        prior_mtime_ns: Optional[int] = None,
    ) -> str:
        selected = self.record(prior_mtime_ns=prior_mtime_ns) if receipt is None else receipt
        return serialize_state_receipt(selected)

    def render(
        self,
        receipt: Optional[Mapping[str, object]] = None,
        *,
        prior_mtime_ns: Optional[int] = None,
    ) -> str:
        selected = self.record(prior_mtime_ns=prior_mtime_ns) if receipt is None else receipt
        return render_state_receipt(selected)


def record_state_flush(
    root: FloatiRoot,
    node_id: str,
    *,
    prior_mtime_ns: Optional[int] = None,
    id_factory: Callable[[], str] = uuid7_hex,
    now: Callable[[], datetime] = _utc_now,
) -> Dict[str, object]:
    """Return one metadata receipt for a state flush observed after it ran."""

    return StateFileFlushReceipt(
        root,
        node_id,
        id_factory=id_factory,
        now=now,
    ).record(prior_mtime_ns=prior_mtime_ns)


def _validate_receipt(receipt: object) -> Mapping[str, object]:
    if not isinstance(receipt, Mapping):
        _refuse("state_receipt_output_invalid", "state receipt must be an object")
    if set(receipt) != _RECEIPT_FIELDS:
        _refuse("state_receipt_output_invalid", "state receipt fields do not match v0")
    if receipt.get("schema_version") != 0:
        _refuse("state_receipt_output_invalid", "state receipt schema version is invalid")
    identifier = receipt.get("id")
    if not isinstance(identifier, str) or _RECEIPT_ID.fullmatch(identifier) is None:
        _refuse("state_receipt_output_invalid", "state receipt ID is invalid")
    for field in ("tenant_id", "node_id"):
        try:
            validate_identifier(receipt.get(field), field)
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "state_receipt_output_invalid", f"state receipt {field} is invalid"
            ) from exc
    _safe_text(receipt.get("timestamp"), "state receipt timestamp is invalid")
    timestamp = str(receipt["timestamp"])
    if _TIMESTAMP.fullmatch(timestamp) is None:
        _refuse("state_receipt_output_invalid", "state receipt timestamp is not canonical")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolRefusal(
            "state_receipt_output_invalid", "state receipt timestamp is not a real UTC time"
        ) from exc
    if receipt.get("kind") != "node_state_flush_receipt" or receipt.get("operation") != "flush":
        _refuse("state_receipt_output_invalid", "state receipt kind or operation is invalid")
    state_file = _safe_text(receipt.get("state_file"), "state receipt path is invalid")
    if not Path(state_file).is_absolute() or Path(state_file).name != "STATE.md":
        _refuse("state_receipt_output_invalid", "state receipt path must be absolute")
    for field in ("observed_mtime_ns", "observed_size_bytes"):
        number = receipt.get(field)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            _refuse("state_receipt_output_invalid", f"state receipt {field} is invalid")
    prior = _validate_prior(receipt.get("prior_mtime_ns"))
    if prior is not None and receipt["observed_mtime_ns"] <= prior:
        _refuse(
            "state_receipt_output_invalid",
            "state receipt mtime does not advance beyond the prior observation",
        )
    return receipt


def serialize_state_receipt(receipt: Mapping[str, object]) -> str:
    """Serialize one receipt without opening its state vessel."""

    checked = _validate_receipt(receipt)
    return json.dumps(
        checked,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n"


def render_state_receipt(receipt: Mapping[str, object]) -> str:
    """Render receipt metadata without exposing state-file content."""

    checked = _validate_receipt(receipt)
    prior = checked["prior_mtime_ns"]
    prior_text = "none" if prior is None else str(prior)
    lines = [
        "NODE STATE FLUSH RECEIPT",
        f"NODE: {checked['node_id']}",
        f"STATE FILE: {checked['state_file']}",
        f"OPERATION: {checked['operation']}",
        f"OBSERVED MTIME NS: {checked['observed_mtime_ns']}",
        f"OBSERVED SIZE BYTES: {checked['observed_size_bytes']}",
        f"PRIOR MTIME NS: {prior_text}",
        f"RECEIPT ID: {checked['id']}",
        f"TIMESTAMP: {checked['timestamp']}",
    ]
    rendered = "\n".join(lines) + "\n"
    _safe_text(rendered, "state receipt board is invalid", multiline=True)
    return rendered

"""Strict loading for the shipped E1 context-observability absences."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple

from .context_absences_v0 import DATASET_JSON
from .errors import ProtocolRefusal


_MAX_DATASET_BYTES = 128 * 1024
_DATASET_FIELDS = frozenset(
    {"schema_version", "dataset_id", "source_commit", "harnesses", "rows"}
)
_ROW_FIELDS = frozenset(
    {"harness", "access_class", "state", "receipt_path", "receipt_sha256"}
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_HARNESS = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _safe_ascii(value: object, *, label: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        _refuse("context_absence_dataset_invalid", f"{label} must be bounded text")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    ):
        _refuse("context_absence_dataset_invalid", f"{label} is terminal-unsafe")
    try:
        value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolRefusal(
            "context_absence_dataset_invalid", f"{label} must be ASCII"
        ) from exc
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


@dataclass(frozen=True)
class ContextAbsence:
    harness: str
    access_class: str
    state: str
    receipt_path: str
    receipt_sha256: str

    @property
    def record(self) -> Dict[str, str]:
        return {
            "harness": self.harness,
            "access_class": self.access_class,
            "state": self.state,
            "receipt_path": self.receipt_path,
            "receipt_sha256": self.receipt_sha256,
        }


@dataclass(frozen=True)
class ContextAbsenceDataset:
    schema_version: int
    dataset_id: str
    source_commit: str
    harnesses: Tuple[str, ...]
    rows: Tuple[ContextAbsence, ...]

    def for_harness(self, recorded: object) -> ContextAbsence:
        text = _safe_ascii(recorded, label="recorded harness", maximum=64)
        selected = text.casefold()
        for row in self.rows:
            if row.harness.casefold() == selected:
                return row
        _refuse(
            "context_harness_unknown",
            f"recorded harness is absent from the E1 dataset: {text}",
        )


def _parse_row(value: object) -> ContextAbsence:
    if not isinstance(value, Mapping):
        _refuse("context_absence_dataset_invalid", "absence row must be an object")
    fields = set(value)
    if "access_class" not in fields:
        _refuse(
            "context_access_class_missing",
            "not_exposed row requires its measured access class",
        )
    if not {"receipt_path", "receipt_sha256"}.issubset(fields):
        _refuse(
            "context_absence_citation_missing",
            "not_exposed row requires receipt path and SHA-256",
        )
    if fields != _ROW_FIELDS:
        _refuse("context_absence_dataset_invalid", "absence row fields do not match v0")
    if any(isinstance(item, (int, float, bool)) for item in value.values()):
        _refuse(
            "context_absence_dataset_invalid",
            "not_exposed row fields must all be text",
        )
    harness = _safe_ascii(value.get("harness"), label="harness", maximum=64)
    if _HARNESS.fullmatch(harness) is None:
        _refuse("context_absence_dataset_invalid", "harness identifier is invalid")
    access_class = _safe_ascii(
        value.get("access_class"), label="access class", maximum=1
    )
    if access_class != "A":
        _refuse(
            "context_absence_dataset_invalid",
            "v0 absence rows must describe the external-programmatic class",
        )
    state_value = _safe_ascii(value.get("state"), label="absence state", maximum=32)
    if state_value != "not_exposed":
        _refuse("context_absence_dataset_invalid", "v0 absence state is invalid")
    receipt_path = _safe_ascii(value.get("receipt_path"), label="receipt path")
    selected_path = Path(receipt_path)
    if selected_path.is_absolute() or any(
        part in {"", ".", ".."} for part in selected_path.parts
    ):
        _refuse(
            "context_absence_dataset_invalid",
            "receipt path must be repository-relative without traversal",
        )
    receipt_sha256 = _safe_ascii(
        value.get("receipt_sha256"), label="receipt SHA-256", maximum=64
    )
    if _SHA256.fullmatch(receipt_sha256) is None:
        _refuse("context_absence_dataset_invalid", "receipt SHA-256 is invalid")
    return ContextAbsence(
        harness=harness,
        access_class=access_class,
        state=state_value,
        receipt_path=receipt_path,
        receipt_sha256=receipt_sha256,
    )


def parse_context_absence_dataset(record: object) -> ContextAbsenceDataset:
    """Validate one in-memory E1 dataset without consulting a fallback source."""

    if not isinstance(record, Mapping) or set(record) != _DATASET_FIELDS:
        _refuse("context_absence_dataset_invalid", "dataset fields do not match v0")
    schema_version = record.get("schema_version")
    if schema_version != 0 or isinstance(schema_version, bool):
        _refuse("context_absence_dataset_invalid", "dataset schema version is invalid")
    dataset_id = _safe_ascii(record.get("dataset_id"), label="dataset ID", maximum=64)
    if dataset_id != "e1-context-absence-v1":
        _refuse("context_absence_dataset_invalid", "dataset ID is invalid")
    source_commit = _safe_ascii(
        record.get("source_commit"), label="source commit", maximum=40
    )
    if _COMMIT.fullmatch(source_commit) is None:
        _refuse("context_absence_dataset_invalid", "source commit is invalid")

    raw_harnesses = record.get("harnesses")
    raw_rows = record.get("rows")
    if not isinstance(raw_harnesses, list) or len(raw_harnesses) != 8:
        _refuse("context_absence_dataset_invalid", "dataset must enumerate eight harnesses")
    harnesses = tuple(
        _safe_ascii(value, label="harness enumeration", maximum=64)
        for value in raw_harnesses
    )
    if (
        harnesses != tuple(sorted(harnesses))
        or len(set(harnesses)) != len(harnesses)
        or any(_HARNESS.fullmatch(value) is None for value in harnesses)
        or len({value.casefold() for value in harnesses}) != len(harnesses)
    ):
        _refuse(
            "context_absence_dataset_invalid",
            "harness enumeration must be unique, canonical, and sorted",
        )
    if not isinstance(raw_rows, list) or len(raw_rows) != len(harnesses):
        _refuse("context_absence_dataset_invalid", "dataset row count is invalid")
    rows = tuple(_parse_row(value) for value in raw_rows)
    if tuple(row.harness for row in rows) != harnesses:
        _refuse(
            "context_absence_dataset_invalid",
            "absence rows must exactly follow the harness enumeration",
        )
    return ContextAbsenceDataset(
        schema_version=0,
        dataset_id=dataset_id,
        source_commit=source_commit,
        harnesses=harnesses,
        rows=rows,
    )


def load_shipped_context_absences() -> ContextAbsenceDataset:
    """Load the one deployable package-owned serialized E1 dataset."""

    payload = DATASET_JSON.encode("utf-8")
    if len(payload) > _MAX_DATASET_BYTES:
        _refuse(
            "context_absence_dataset_unavailable",
            "shipped context absence dataset exceeds its size bound",
        )
    try:
        decoded = payload.decode("utf-8")
        record = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolRefusal(
            "context_absence_dataset_invalid",
            "shipped context absence dataset is not strict UTF-8 JSON",
        ) from exc
    return parse_context_absence_dataset(record)

"""Strict typed role templates loaded from explicit plain JSON files."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from .errors import ProtocolRefusal
from .root import validate_identifier


SHIPPED_ROLE_NAMES = (
    "architect",
    "builder",
    "github-manager",
    "researcher",
    "reviewer",
    "sre",
)
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "template_version",
        "role",
        "duties",
        "decision_rights",
        "stops",
        "fences",
        "cadence",
        "questions",
    }
)
_QUESTION_REQUIRED_FIELDS = frozenset({"key", "ask"})
_QUESTION_FIELDS = _QUESTION_REQUIRED_FIELDS | {"default"}
_CADENCE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_MAX_FILE_BYTES = 256 * 1024


def _refuse(detail: str) -> None:
    raise ProtocolRefusal("role_template_invalid", detail)


def _safe_text(value: object, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        _refuse(f"{field} must be text between 1 and {maximum} characters")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    ):
        _refuse(f"{field} contains terminal-unsafe text")
    return value


def _copy_rows(value: object, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        _refuse(f"{field} must contain between 1 and 64 lines")
    return tuple(_safe_text(item, f"{field} item") for item in value)


@dataclass(frozen=True)
class RoleQuestion:
    key: str
    ask: str
    default: Optional[str]

    @property
    def record(self) -> Dict[str, object]:
        value: Dict[str, object] = {"key": self.key, "ask": self.ask}
        if self.default is not None:
            value["default"] = self.default
        return value


@dataclass(frozen=True)
class RoleTemplate:
    schema_version: int
    template_version: int
    role: str
    duties: Tuple[str, ...]
    decision_rights: Tuple[str, ...]
    stops: Tuple[str, ...]
    fences: Tuple[str, ...]
    cadence: str
    questions: Tuple[RoleQuestion, ...]
    digest: str

    @property
    def record(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "template_version": self.template_version,
            "role": self.role,
            "duties": list(self.duties),
            "decision_rights": list(self.decision_rights),
            "stops": list(self.stops),
            "fences": list(self.fences),
            "cadence": self.cadence,
            "questions": [question.record for question in self.questions],
        }


def parse_role_template(record: object) -> RoleTemplate:
    if not isinstance(record, Mapping) or set(record) != _TOP_LEVEL_FIELDS:
        _refuse("role template fields do not match the v0 contract")
    schema_version = record.get("schema_version")
    template_version = record.get("template_version")
    if schema_version != 0 or isinstance(schema_version, bool):
        _refuse("schema_version must be 0")
    if (
        not isinstance(template_version, int)
        or isinstance(template_version, bool)
        or not 1 <= template_version <= 1_000_000
    ):
        _refuse("template_version must be an integer between 1 and 1000000")
    try:
        role = validate_identifier(record.get("role"), "role")
    except ProtocolRefusal as exc:
        raise ProtocolRefusal("role_template_invalid", "role identifier is invalid") from exc
    cadence_value = record.get("cadence")
    if not isinstance(cadence_value, str) or not _CADENCE.fullmatch(cadence_value):
        _refuse("cadence identifier is invalid")

    raw_questions = record.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 32:
        _refuse("questions must contain between 1 and 32 entries")
    questions = []
    seen_keys = set()
    for raw in raw_questions:
        if (
            not isinstance(raw, Mapping)
            or not _QUESTION_REQUIRED_FIELDS.issubset(raw)
            or not set(raw).issubset(_QUESTION_FIELDS)
        ):
            _refuse("question fields do not match the v0 contract")
        try:
            key = validate_identifier(raw.get("key"), "question_key")
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "role_template_invalid", "question key is invalid"
            ) from exc
        if key in seen_keys:
            _refuse("question keys must be unique")
        seen_keys.add(key)
        ask = _safe_text(raw.get("ask"), "question ask")
        default_value = raw.get("default")
        default = (
            None
            if "default" not in raw
            else _safe_text(default_value, "question default")
        )
        questions.append(RoleQuestion(key=key, ask=ask, default=default))

    provisional = RoleTemplate(
        schema_version=0,
        template_version=template_version,
        role=role,
        duties=_copy_rows(record.get("duties"), "duties"),
        decision_rights=_copy_rows(record.get("decision_rights"), "decision_rights"),
        stops=_copy_rows(record.get("stops"), "stops"),
        fences=_copy_rows(record.get("fences"), "fences"),
        cadence=cadence_value,
        questions=tuple(questions),
        digest="",
    )
    encoded = json.dumps(
        provisional.record,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return RoleTemplate(
        schema_version=provisional.schema_version,
        template_version=provisional.template_version,
        role=provisional.role,
        duties=provisional.duties,
        decision_rights=provisional.decision_rights,
        stops=provisional.stops,
        fences=provisional.fences,
        cadence=provisional.cadence,
        questions=provisional.questions,
        digest=hashlib.sha256(encoded).hexdigest(),
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


def load_role_template(path: Union[Path, str]) -> RoleTemplate:
    selected = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(selected, flags)
    except OSError as exc:
        raise ProtocolRefusal(
            "role_template_path_invalid", "role template path could not be opened safely"
        ) from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_size > _MAX_FILE_BYTES:
            raise ProtocolRefusal(
                "role_template_path_invalid", "role template must be a bounded regular file"
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(_MAX_FILE_BYTES + 1)
        if len(payload) > _MAX_FILE_BYTES:
            raise ProtocolRefusal(
                "role_template_path_invalid", "role template exceeds the file size limit"
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        decoded = payload.decode("utf-8")
        record = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolRefusal(
            "role_template_invalid", "role template is not strict UTF-8 JSON"
        ) from exc
    return parse_role_template(record)


def load_shipped_role_templates(directory: Union[Path, str]) -> Dict[str, RoleTemplate]:
    root = Path(directory)
    library: Dict[str, RoleTemplate] = {}
    for role in SHIPPED_ROLE_NAMES:
        template = load_role_template(root / f"{role}.json")
        if template.role != role:
            _refuse(f"shipped template {role} declares a different role")
        library[role] = template
    return library

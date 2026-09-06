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

from .errors import IntegrityFailure, ProtocolRefusal
from .root import validate_identifier


SHIPPED_ROLE_NAMES = (
    "architect",
    "builder",
    "github-manager",
    "researcher",
    "reviewer",
    "sre",
)
_REQUIRED_FIELDS = frozenset(
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
_TOP_LEVEL_FIELDS = _REQUIRED_FIELDS | {"ack_sla_minutes"}
_QUESTION_REQUIRED_FIELDS = frozenset({"key", "ask"})
_QUESTION_FIELDS = _QUESTION_REQUIRED_FIELDS | {"default"}
_CADENCE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
_MAX_FILE_BYTES = 256 * 1024


SOURCE_FILE_REMEDY = (
    "pass --from a readable regular JSON file no larger than 256 KiB"
)


RECORD_REMEDY = "make the source file a single JSON object with the v0 role template fields"


def _settable(field: str) -> str:
    """The top-level --set field behind a validator's own field name.

    Validators name nested positions ("duties item", "question ask"); the
    operator acts on the top-level field that contains them.
    """

    head = field.split()[0]
    return "questions" if head == "question" else head


def _field_remedy(field: str) -> str:
    """The act for the field the detail already names, computed from that name."""

    return f"correct {field} in the source file, or pass --set {_settable(field)}=value"


def _refuse(detail: str, remedy: str) -> None:
    raise ProtocolRefusal("role_template_invalid", detail, remedy=remedy)


def _safe_text(value: object, field: str, *, maximum: int = 500) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        _refuse(
            f"{field} must be text between 1 and {maximum} characters",
            _field_remedy(field),
        )
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        or unicodedata.bidirectional(character)
        in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
        for character in value
    ):
        _refuse(f"{field} contains terminal-unsafe text", _field_remedy(field))
    return value


def _copy_rows(value: object, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        _refuse(
            f"{field} must contain between 1 and 64 lines", _field_remedy(field)
        )
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
    ack_sla_minutes: Optional[int] = None

    @property
    def record(self) -> Dict[str, object]:
        value: Dict[str, object] = {
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
        if self.ack_sla_minutes is not None:
            value["ack_sla_minutes"] = self.ack_sla_minutes
        return value


def parse_role_template(record: object) -> RoleTemplate:
    if (
        not isinstance(record, Mapping)
        or not _REQUIRED_FIELDS.issubset(record)
        or not set(record).issubset(_TOP_LEVEL_FIELDS)
    ):
        if not isinstance(record, Mapping):
            _refuse("role template must be an object", RECORD_REMEDY)
        missing = sorted(_REQUIRED_FIELDS - set(record))
        unknown = sorted(str(key) for key in set(record) - _TOP_LEVEL_FIELDS)
        detail = "role template fields do not match the v0 contract"
        acts = []
        if missing:
            detail += "; missing fields: " + ", ".join(missing)
            acts.append("add " + ", ".join(missing) + " to the source file")
        if unknown:
            detail += "; unknown fields: " + ", ".join(ascii(key) for key in unknown)
            acts.append(
                "drop " + ", ".join(ascii(key) for key in unknown) + " from the source file"
            )
        _refuse(detail, " and ".join(acts) if acts else RECORD_REMEDY)
    schema_version = record.get("schema_version")
    template_version = record.get("template_version")
    if schema_version != 0 or isinstance(schema_version, bool):
        _refuse("schema_version must be 0", _field_remedy("schema_version"))
    if (
        not isinstance(template_version, int)
        or isinstance(template_version, bool)
        or not 1 <= template_version <= 1_000_000
    ):
        _refuse(
            "template_version must be an integer between 1 and 1000000",
            _field_remedy("template_version"),
        )
    try:
        role = validate_identifier(record.get("role"), "role")
    except ProtocolRefusal as exc:
        raise ProtocolRefusal(
            "role_template_invalid",
            "role identifier is invalid",
            remedy="set the template's role field to a lowercase identifier",
        ) from exc
    cadence_value = record.get("cadence")
    if not isinstance(cadence_value, str) or not _CADENCE.fullmatch(cadence_value):
        _refuse("cadence identifier is invalid", _field_remedy("cadence"))
    ack_sla_value = record.get("ack_sla_minutes")
    if ack_sla_value is not None and (
        not isinstance(ack_sla_value, int)
        or isinstance(ack_sla_value, bool)
        or not 1 <= ack_sla_value <= 1_000_000
    ):
        _refuse(
            "ack_sla_minutes must be an integer between 1 and 1000000",
            _field_remedy("ack_sla_minutes"),
        )

    raw_questions = record.get("questions")
    if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 32:
        _refuse(
            "questions must contain between 1 and 32 entries",
            _field_remedy("questions"),
        )
    questions = []
    seen_keys = set()
    for raw in raw_questions:
        if (
            not isinstance(raw, Mapping)
            or not _QUESTION_REQUIRED_FIELDS.issubset(raw)
            or not set(raw).issubset(_QUESTION_FIELDS)
        ):
            _refuse(
                "question fields do not match the v0 contract",
                _field_remedy("question fields"),
            )
        try:
            key = validate_identifier(raw.get("key"), "question_key")
        except ProtocolRefusal as exc:
            raise ProtocolRefusal(
                "role_template_invalid",
                "question key is invalid",
                remedy="set each question key to a lowercase identifier",
            ) from exc
        if key in seen_keys:
            _refuse("question keys must be unique", _field_remedy("question keys"))
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
        ack_sla_minutes=ack_sla_value,
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
        ack_sla_minutes=provisional.ack_sla_minutes,
    )


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate object key")
        value[key] = item
    return value


def load_role_template_payload(path: Union[Path, str]) -> Tuple[RoleTemplate, bytes]:
    """Read one bounded local file once; retain bytes for source-digest receipts."""
    selected = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(selected, flags)
    except OSError as exc:
        raise ProtocolRefusal(
            "role_template_path_invalid",
            "role template path could not be opened safely",
            remedy=SOURCE_FILE_REMEDY,
        ) from exc
    try:
        identity = os.fstat(descriptor)
        if not stat.S_ISREG(identity.st_mode) or identity.st_size > _MAX_FILE_BYTES:
            raise ProtocolRefusal(
                "role_template_path_invalid",
                "role template must be a bounded regular file",
                remedy=SOURCE_FILE_REMEDY,
            )
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            payload = stream.read(_MAX_FILE_BYTES + 1)
        if len(payload) > _MAX_FILE_BYTES:
            raise ProtocolRefusal(
                "role_template_path_invalid",
                "role template exceeds the file size limit",
                remedy=SOURCE_FILE_REMEDY,
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return _parse_role_template_bytes(payload), payload


def _parse_role_template_bytes(payload: bytes) -> RoleTemplate:
    try:
        decoded = payload.decode("utf-8")
        record = json.loads(
            decoded,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProtocolRefusal(
            "role_template_invalid",
            "role template is not strict UTF-8 JSON",
            remedy=(
                "save the file as strict UTF-8 JSON with no duplicate keys and "
                "no non-finite numbers"
            ),
        ) from exc
    return parse_role_template(record)


def load_role_template(path: Union[Path, str]) -> RoleTemplate:
    return load_role_template_payload(path)[0]


def load_shipped_role_templates(directory: Union[Path, str]) -> Dict[str, RoleTemplate]:
    root = Path(directory)
    library: Dict[str, RoleTemplate] = {}
    for role in SHIPPED_ROLE_NAMES:
        template = load_role_template(root / f"{role}.json")
        if template.role != role:
            # A bundled file, not operator input: there is no act to name and
            # repeating the command cannot change the answer.
            raise IntegrityFailure(
                "role_template_shipped_invalid",
                f"shipped role template {role}.json declares role "
                f"{template.role!r} and not {role!r}",
            )
        library[role] = template
    return library

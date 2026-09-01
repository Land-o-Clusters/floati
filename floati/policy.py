"""Finite, data-only loading for the repository-owned ``FLOATI.toml`` policy.

The policy grammar deliberately implements only the closed HM-3I v0 surface.
It is not a general TOML reader: accepting a feature that has no bounded policy
meaning would turn configuration presentation into an execution or authority
surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .errors import ProtocolRefusal


POLICY_FILENAME = "FLOATI.toml"
MAX_POLICY_BYTES = 64 * 1024
MAX_LINE_CHARACTERS = 2_048
MAX_STRING_CHARACTERS = 256
MAX_STRING_BYTES = 1_024
MAX_ARRAY_ITEMS = 64
MAX_NAMED_ENTRIES = 64
MAX_ARGV_ITEMS = 32
MAX_BUDGET_LIMIT = 1_000_000_000
MAX_ROUTE_RANK = 4_095
MAX_CONCURRENCY = 64

IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")
HEX_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
INTEGER_PATTERN = re.compile(r"^(?:0|[1-9][0-9]*)$")
BARE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

_NAMED_TABLES = {
    "budgets",
    "worker_profiles",
    "capability_selectors",
    "routing",
    "retry_classes",
    "approval_requirements",
    "verification",
    "merge_gates",
}
_REQUIRED_ROOT_TABLES = (
    "limits",
    "budgets",
    "worker_profiles",
    "capability_selectors",
    "routing",
    "retry_classes",
    "approval_requirements",
    "verification",
    "merge_gates",
)
_LIMIT_BOUNDS = {
    "max_items": (1, 64),
    "max_depth": (1, 16),
    "max_fan_out": (1, 8),
    "max_active_attempts": (1, 8),
}
_BUDGET_UNITS = frozenset(("attempts", "tokens", "milliseconds", "microcurrency"))
_CANCEL_MODES = frozenset(("native", "local_process_only", "unavailable"))
_SECRET_ISOLATION_MODES = frozenset(("process", "helper", "none"))
_RETRY_CLASSES = (
    "transient",
    "permanent",
    "operator_required",
    "policy_refusal",
    "cancelled",
    "unknown_effect",
)
_RISK_CLASSES = ("low", "medium", "high", "critical")
_DYNAMIC_TEXT_MARKERS = ("${", "{{", "}}", "$(`", "$(", "<%", "%>")


def _refuse(code: str, detail: str) -> None:
    raise ProtocolRefusal(code, detail)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        _refuse("policy_identifier_invalid", f"{field} must be a bounded lowercase identifier")
    return value


def _validate_plain_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        _refuse("policy_text_invalid", f"{field} must be a string")
    if not value or len(value) > MAX_STRING_CHARACTERS or len(value.encode("utf-8")) > MAX_STRING_BYTES:
        _refuse("policy_text_invalid", f"{field} exceeds the bounded v0 string contract")
    if any(ord(character) < 0x20 for character in value):
        _refuse("policy_text_invalid", f"{field} contains a control character")
    if any(marker in value for marker in _DYNAMIC_TEXT_MARKERS):
        _refuse("policy_dynamic_text", f"{field} contains an interpolation or template marker")
    return value


def _validate_exact_keys(mapping: object, expected: Sequence[str], field: str) -> Mapping[str, Any]:
    if not isinstance(mapping, dict) or set(mapping) != set(expected):
        _refuse("policy_fields_invalid", f"{field} must contain exactly {', '.join(expected)}")
    return mapping


def _validate_bounded_integer(value: object, field: str, lower: int, upper: int) -> int:
    if not _is_integer(value) or value < lower or value > upper:
        _refuse("policy_integer_invalid", f"{field} must be an integer from {lower} through {upper}")
    return value


def _validate_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        _refuse("policy_boolean_invalid", f"{field} must be a boolean")
    return value


def _validate_sorted_identifier_set(value: object, field: str, *, maximum: int = MAX_ARRAY_ITEMS) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        _refuse("policy_set_invalid", f"{field} must be a nonempty bounded array")
    identifiers = tuple(_validate_identifier(member, field) for member in value)
    if list(identifiers) != sorted(identifiers) or len(set(identifiers)) != len(identifiers):
        _refuse("policy_set_order_invalid", f"{field} must be sorted and unique")
    return identifiers


def _validate_argv(value: object, field: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_ARGV_ITEMS:
        _refuse("policy_argv_invalid", f"{field} must be a nonempty bounded argv array")
    return tuple(_validate_plain_text(member, field) for member in value)


def _freeze_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True)
class Budget:
    """One labelled hard limit; it does not predict a cost."""

    budget_id: str
    unit: str
    limit: int


@dataclass(frozen=True)
class WorkerProfile:
    """A bounded worker capability declaration."""

    profile_id: str
    capabilities: Tuple[str, ...]
    cancel_mode: str
    callback_support: bool
    max_concurrency: int
    secret_isolation: Optional[str] = None


@dataclass(frozen=True)
class CapabilitySelector:
    """A conjunction-only capability selector."""

    selector_id: str
    all_of: Tuple[str, ...]


@dataclass(frozen=True)
class RoutingRule:
    """One explicit ranked worker/selector route."""

    route_id: str
    worker_profile: str
    capability_selector: str
    rank: int


@dataclass(frozen=True)
class VerificationCommand:
    """Data-only verification argv.  Loading this object never executes it."""

    verification_id: str
    argv: Tuple[str, ...]


@dataclass(frozen=True)
class MergeGate:
    """A closed reference set to declared verification commands."""

    gate_id: str
    verification_ids: Tuple[str, ...]


@dataclass(frozen=True)
class RepositoryPolicy:
    """A fully validated immutable policy and its canonical semantic digest."""

    schema_version: int
    capability_registry: Tuple[str, ...]
    limits: Mapping[str, int]
    budgets: Mapping[str, Budget]
    worker_profiles: Mapping[str, WorkerProfile]
    capability_selectors: Mapping[str, CapabilitySelector]
    routing: Mapping[str, RoutingRule]
    retry_classes: Mapping[str, bool]
    approval_requirements: Mapping[str, bool]
    verification: Mapping[str, VerificationCommand]
    merge_gates: Mapping[str, MergeGate]
    routes: Tuple[RoutingRule, ...]
    canonical_bytes: bytes
    digest: str

    @classmethod
    def load(cls, path: Union[Path, str]) -> "RepositoryPolicy":
        """Read one lexical, regular, absolute ``FLOATI.toml`` without effects."""

        raw = _parse_policy_file(path)
        return _build_policy(raw)

    @property
    def policy_digest(self) -> str:
        """Spell out the digest's role without providing a mutable alias."""

        return self.digest

    def effect_approval_required(self, risk_class: object) -> bool:
        """Return the closed repository approval rule for one effect risk."""

        validate_repository_policy_integrity(self)
        if not isinstance(risk_class, str) or risk_class not in _RISK_CLASSES:
            _refuse(
                "effect_risk_invalid",
                "effect risk must be low, medium, high, or critical",
            )
        return bool(self.approval_requirements[risk_class])

    def effect_budget_limit(self, budget_id: object) -> int:
        """Return one repository-declared hard budget bound."""

        validate_repository_policy_integrity(self)
        if not isinstance(budget_id, str) or budget_id not in self.budgets:
            _refuse(
                "effect_budget_unknown",
                "effect claims require a repository-declared budget name",
            )
        return self.budgets[budget_id].limit

    def canonical(self) -> Dict[str, Any]:
        """Return a fresh plain semantic projection for downstream serialization."""

        return {
            "schema_version": self.schema_version,
            "capability_registry": list(self.capability_registry),
            "limits": dict(self.limits),
            "budgets": {
                identifier: {"unit": budget.unit, "limit": budget.limit}
                for identifier, budget in self.budgets.items()
            },
            "worker_profiles": {
                identifier: {
                    "capabilities": list(profile.capabilities),
                    "cancel_mode": profile.cancel_mode,
                    "callback_support": profile.callback_support,
                    "max_concurrency": profile.max_concurrency,
                    **(
                        {"secret_isolation": profile.secret_isolation}
                        if profile.secret_isolation is not None
                        else {}
                    ),
                }
                for identifier, profile in self.worker_profiles.items()
            },
            "capability_selectors": {
                identifier: {"all_of": list(selector.all_of)}
                for identifier, selector in self.capability_selectors.items()
            },
            "routing": {
                identifier: {
                    "worker_profile": route.worker_profile,
                    "capability_selector": route.capability_selector,
                    "rank": route.rank,
                }
                for identifier, route in self.routing.items()
            },
            "retry_classes": dict(self.retry_classes),
            "approval_requirements": dict(self.approval_requirements),
            "verification": {
                identifier: {"argv": list(command.argv)}
                for identifier, command in self.verification.items()
            },
            "merge_gates": {
                identifier: {"verification_ids": list(gate.verification_ids)}
                for identifier, gate in self.merge_gates.items()
            },
        }


# Authoritative consumers bind to these original base operations.  Exact policy
# instances remain valid if a caller later mutates or rebinds public class
# attributes, but those live attributes never participate in authority checks.
_REPOSITORY_POLICY_TYPE = RepositoryPolicy
_REPOSITORY_POLICY_CANONICAL = RepositoryPolicy.canonical
_REPOSITORY_POLICY_EFFECT_APPROVAL_REQUIRED = (
    RepositoryPolicy.effect_approval_required
)
_REPOSITORY_POLICY_EFFECT_BUDGET_LIMIT = RepositoryPolicy.effect_budget_limit


# The short public spelling in the plan is an alias, rather than a separate
# mutable wrapper with divergent parser behavior.
Policy = RepositoryPolicy


_MAPPING_PROXY_TYPE = type(MappingProxyType({}))


def validate_repository_policy_integrity(policy: object) -> RepositoryPolicy:
    """Reject forged or mutable policy objects whose live fields diverge from loaded bytes."""

    if type(policy) is not _REPOSITORY_POLICY_TYPE:
        raise ProtocolRefusal("policy_required", "a validated repository policy is required")
    mappings = (
        policy.limits, policy.budgets, policy.worker_profiles,
        policy.capability_selectors, policy.routing, policy.retry_classes,
        policy.approval_requirements, policy.verification, policy.merge_gates,
    )
    if (
        not isinstance(policy.capability_registry, tuple)
        or not isinstance(policy.routes, tuple)
        or any(not isinstance(mapping, _MAPPING_PROXY_TYPE) for mapping in mappings)
    ):
        raise ProtocolRefusal(
            "policy_integrity_invalid",
            "policy fields must retain their loaded immutable value types",
        )
    try:
        current = json.dumps(
            _REPOSITORY_POLICY_CANONICAL(policy),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        derived_routes = tuple(
            sorted(policy.routing.values(), key=lambda route: (route.rank, route.route_id))
        )
    except (AttributeError, KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal(
            "policy_integrity_invalid",
            "policy fields cannot be rederived as canonical semantics",
        ) from exc
    if (
        not isinstance(policy.canonical_bytes, bytes)
        or not isinstance(policy.digest, str)
        or current != policy.canonical_bytes
        or hashlib.sha256(current).hexdigest() != policy.digest
        or policy.routes != derived_routes
    ):
        raise ProtocolRefusal(
            "policy_integrity_invalid",
            "policy fields or derived routes diverge from cached semantics",
        )
    return policy


class PolicyDeploymentStatus(str, Enum):
    """The exact policy-deployment vocabulary imported by Delta Intake 5."""

    DEPLOYED = "DEPLOYED"
    DRIFTED = "DRIFTED"
    ABSENT = "ABSENT"
    CANNOT_SPEAK = "CANNOT_SPEAK"


@dataclass(frozen=True)
class PolicyDeploymentCheck:
    """A pure observation of one policy and one explicitly supplied baseline."""

    status: PolicyDeploymentStatus
    observed_digest: Optional[str]
    reviewed_digest: Optional[str]
    subject: Optional[str] = None
    error_code: Optional[str] = None


class PolicyDeploymentChecker:
    """Compare policy bytes with an external reviewed digest without inferring review."""

    @staticmethod
    def check(
        policy_path: Union[Path, str], reviewed_digest: Optional[str]
    ) -> PolicyDeploymentCheck:
        if reviewed_digest is not None and (
            not isinstance(reviewed_digest, str) or not HEX_DIGEST_PATTERN.fullmatch(reviewed_digest)
        ):
            return PolicyDeploymentCheck(
                status=PolicyDeploymentStatus.CANNOT_SPEAK,
                observed_digest=None,
                reviewed_digest=None,
                subject="reviewed_digest",
                error_code="reviewed_digest_invalid",
            )
        try:
            policy = Policy.load(policy_path)
        except ProtocolRefusal as exc:
            if exc.code == "policy_missing":
                return PolicyDeploymentCheck(
                    status=PolicyDeploymentStatus.ABSENT,
                    observed_digest=None,
                    reviewed_digest=reviewed_digest,
                    subject="policy",
                    error_code=exc.code,
                )
            return PolicyDeploymentCheck(
                status=PolicyDeploymentStatus.CANNOT_SPEAK,
                observed_digest=None,
                reviewed_digest=reviewed_digest,
                subject="policy",
                error_code=exc.code,
            )
        except Exception:
            # The checker is an observation boundary.  An unavailable path-like
            # object or filesystem comparison must never escape as an apparent
            # deployment result or an uncaught platform exception.
            return PolicyDeploymentCheck(
                status=PolicyDeploymentStatus.CANNOT_SPEAK,
                observed_digest=None,
                reviewed_digest=reviewed_digest,
                subject="policy",
                error_code="policy_unavailable",
            )
        if reviewed_digest is None:
            return PolicyDeploymentCheck(
                status=PolicyDeploymentStatus.ABSENT,
                observed_digest=policy.digest,
                reviewed_digest=None,
                subject="reviewed_digest",
                error_code="reviewed_digest_absent",
            )
        if policy.digest == reviewed_digest:
            return PolicyDeploymentCheck(
                status=PolicyDeploymentStatus.DEPLOYED,
                observed_digest=policy.digest,
                reviewed_digest=reviewed_digest,
            )
        return PolicyDeploymentCheck(
            status=PolicyDeploymentStatus.DRIFTED,
            observed_digest=policy.digest,
            reviewed_digest=reviewed_digest,
        )


def check_policy_deployment(
    policy_path: Union[Path, str], reviewed_digest: Optional[str]
) -> PolicyDeploymentCheck:
    """Convenience function for callers that do not need checker construction."""

    return PolicyDeploymentChecker.check(policy_path, reviewed_digest)


def _parse_policy_file(path: Union[Path, str]) -> Dict[str, Any]:
    candidate = _validate_policy_path(path)
    try:
        data = candidate.read_bytes()
    except FileNotFoundError as exc:
        raise ProtocolRefusal("policy_missing", "the explicit FLOATI.toml does not exist") from exc
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal("policy_unreadable", "the explicit FLOATI.toml cannot be read") from exc
    if len(data) > MAX_POLICY_BYTES:
        _refuse("policy_oversize", f"FLOATI.toml exceeds {MAX_POLICY_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolRefusal("policy_not_utf8", "FLOATI.toml must be valid UTF-8") from exc
    _validate_document_source(text)
    return _parse_document(text)


def _validate_policy_path(path: Union[Path, str]) -> Path:
    """Validate a path boundary without pretending normalized ``Path`` values retain source spelling.

    A raw string or custom path-like value is checked before ``Path`` can
    normalize literal `.` components.  A caller that already constructed a
    ``Path`` has intentionally supplied its normalized spelling; Python no
    longer exposes discarded dots, so this function checks the components that
    remain visible (including `..`) rather than claiming to recover them.
    """

    try:
        raw_path = os.fspath(path)
    except Exception as exc:
        raise ProtocolRefusal("policy_path_invalid", "an explicit filesystem path is required") from exc
    if not isinstance(raw_path, str):
        _refuse("policy_path_invalid", "FLOATI.toml path must be text, not bytes")
    if "\x00" in raw_path:
        _refuse("policy_path_invalid", "FLOATI.toml path must not contain NUL")
    try:
        raw_path.encode("utf-8")
    except UnicodeError as exc:
        raise ProtocolRefusal("policy_path_invalid", "FLOATI.toml path must be valid UTF-8 text") from exc
    _reject_raw_dot_components(raw_path)
    try:
        candidate = Path(raw_path)
    except Exception as exc:
        raise ProtocolRefusal("policy_path_invalid", "an explicit filesystem path is required") from exc
    if not candidate.is_absolute():
        _refuse("policy_path_not_absolute", "FLOATI.toml path must be absolute")
    if any(part in (".", "..") for part in candidate.parts):
        _refuse("policy_path_invalid", "FLOATI.toml path must not contain lexical dot components")
    if candidate.name != POLICY_FILENAME:
        _refuse("policy_filename_invalid", "policy path must name FLOATI.toml")
    _reject_symlink_components(candidate)
    try:
        metadata = os.lstat(candidate)
    except FileNotFoundError as exc:
        raise ProtocolRefusal("policy_missing", "the explicit FLOATI.toml does not exist") from exc
    except (OSError, TypeError, ValueError, UnicodeError) as exc:
        raise ProtocolRefusal("policy_unreadable", "the explicit FLOATI.toml cannot be inspected") from exc
    if not stat.S_ISREG(metadata.st_mode):
        _refuse("policy_not_regular", "FLOATI.toml must be a regular file")
    return candidate


def _reject_raw_dot_components(raw_path: str) -> None:
    spelling = raw_path
    if os.altsep:
        spelling = spelling.replace(os.altsep, os.sep)
    if any(component in (".", "..") for component in spelling.split(os.sep)):
        _refuse("policy_path_invalid", "FLOATI.toml path must not contain lexical dot components")


def _reject_symlink_components(candidate: Path) -> None:
    anchor = Path(candidate.anchor)
    current = anchor
    parts = candidate.parts
    start = 1 if candidate.anchor else 0
    for part in parts[start:]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            # The final absence is reported by the ordinary file check.  A
            # non-existent lexical parent cannot be a hidden symlink target.
            return
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            raise ProtocolRefusal("policy_unreadable", "policy path cannot be inspected") from exc
        if stat.S_ISLNK(metadata.st_mode):
            _refuse("policy_symlinked", "FLOATI.toml and every lexical component must not be symlinked")


def _validate_document_source(text: str) -> None:
    """Allow only literal space and LF/CRLF as document-level whitespace.

    UTF-8 semantic characters remain valid inside quoted strings, but parser
    syntax cannot inherit broad Python ``isspace``/``splitlines`` behavior.
    This check runs before comments are stripped, so controls cannot hide in
    ignored text.
    """

    for index, character in enumerate(text):
        if character == " ":
            continue
        if character == "\n":
            continue
        if character == "\r":
            if index + 1 >= len(text) or text[index + 1] != "\n":
                _refuse("policy_whitespace_invalid", "FLOATI.toml permits only LF or CRLF line endings")
            continue
        category = unicodedata.category(character)
        if character.isspace() or category in {"Cc", "Cf", "Zl", "Zp", "Zs"}:
            _refuse("policy_whitespace_invalid", "FLOATI.toml contains disallowed control or Unicode whitespace")


def _parse_document(text: str) -> Dict[str, Any]:
    root: Dict[str, Any] = {}
    seen_tables = set()
    current: Dict[str, Any] = root
    for line_number, original in enumerate(text.splitlines(), start=1):
        if len(original) > MAX_LINE_CHARACTERS:
            _refuse("policy_line_oversize", f"line {line_number} exceeds {MAX_LINE_CHARACTERS} characters")
        line = _strip_comment(original).strip()
        if not line:
            continue
        if line.startswith("[["):
            _refuse("policy_syntax_invalid", f"line {line_number} arrays of tables are forbidden")
        if line.startswith("["):
            if not line.endswith("]") or line.count("[") != 1 or line.count("]") != 1:
                _refuse("policy_syntax_invalid", f"line {line_number} has an invalid table header")
            table_path = _parse_table_path(line[1:-1], line_number)
            if table_path in seen_tables:
                _refuse("policy_duplicate_table", f"line {line_number} repeats a decoded table header")
            seen_tables.add(table_path)
            current = _table_for(root, table_path, line_number)
            continue
        key_text, value_text = _split_assignment(line, line_number)
        key = _parse_assignment_key(key_text, line_number)
        if key in current:
            _refuse("policy_duplicate_key", f"line {line_number} repeats a decoded key")
        current[key] = _parse_value(value_text, line_number)
    return root


def _strip_comment(line: str) -> str:
    quoted = False
    escaped = False
    for index, character in enumerate(line):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == "#":
            return line[:index]
    return line


def _split_assignment(line: str, line_number: int) -> Tuple[str, str]:
    quoted = False
    escaped = False
    bracket_depth = 0
    found: Optional[int] = None
    for index, character in enumerate(line):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "[":
            bracket_depth += 1
        elif character == "]":
            bracket_depth -= 1
            if bracket_depth < 0:
                _refuse("policy_syntax_invalid", f"line {line_number} has an unmatched bracket")
        elif character == "=" and bracket_depth == 0:
            if found is not None:
                _refuse("policy_syntax_invalid", f"line {line_number} has multiple assignments")
            found = index
    if quoted or bracket_depth != 0 or found is None:
        _refuse("policy_syntax_invalid", f"line {line_number} must contain one complete assignment")
    key = line[:found].strip()
    value = line[found + 1 :].strip()
    if not key or not value:
        _refuse("policy_syntax_invalid", f"line {line_number} has an empty assignment side")
    return key, value


def _parse_assignment_key(text: str, line_number: int) -> str:
    if "." in text:
        _refuse("policy_dotted_key", f"line {line_number} dotted assignments are forbidden")
    return _parse_key_component(text.strip(), line_number)


def _parse_table_path(text: str, line_number: int) -> Tuple[str, ...]:
    components: List[str] = []
    position = 0
    while position < len(text):
        while position < len(text) and text[position].isspace():
            position += 1
        component, position = _parse_key_component_at(text, position, line_number)
        components.append(component)
        while position < len(text) and text[position].isspace():
            position += 1
        if position == len(text):
            break
        if text[position] != ".":
            _refuse("policy_syntax_invalid", f"line {line_number} has an invalid table path")
        position += 1
        if position == len(text):
            _refuse("policy_syntax_invalid", f"line {line_number} ends a table path with a dot")
    if not components:
        _refuse("policy_syntax_invalid", f"line {line_number} has an empty table header")
    return tuple(components)


def _parse_key_component(text: str, line_number: int) -> str:
    component, position = _parse_key_component_at(text, 0, line_number)
    if text[position:].strip():
        _refuse("policy_syntax_invalid", f"line {line_number} has an invalid key")
    return component


def _parse_key_component_at(text: str, position: int, line_number: int) -> Tuple[str, int]:
    if position >= len(text):
        _refuse("policy_syntax_invalid", f"line {line_number} has an empty key component")
    if text[position] == '"':
        value, end = _parse_quoted_at(text, position, line_number)
        if not value:
            _refuse("policy_syntax_invalid", f"line {line_number} has an empty quoted key")
        return value, end
    end = position
    while end < len(text) and not text[end].isspace() and text[end] != ".":
        end += 1
    candidate = text[position:end]
    if not BARE_KEY_PATTERN.fullmatch(candidate):
        _refuse("policy_syntax_invalid", f"line {line_number} has an unsupported bare key")
    return candidate, end


def _table_for(root: Dict[str, Any], path: Tuple[str, ...], line_number: int) -> Dict[str, Any]:
    if path == ("limits",):
        existing = root.get("limits")
        if existing is None:
            root["limits"] = {}
            return root["limits"]
        if not isinstance(existing, dict):
            _refuse("policy_syntax_invalid", f"line {line_number} collides with a scalar table name")
        return existing
    if len(path) != 2 or path[0] not in _NAMED_TABLES:
        _refuse("policy_table_invalid", f"line {line_number} names a table outside the finite v0 surface")
    category, identifier = path
    container = root.get(category)
    if container is None:
        container = {}
        root[category] = container
    if not isinstance(container, dict):
        _refuse("policy_syntax_invalid", f"line {line_number} collides with a scalar table family")
    if identifier in container:
        _refuse("policy_duplicate_table", f"line {line_number} repeats a decoded table header")
    entry: Dict[str, Any] = {}
    container[identifier] = entry
    return entry


def _parse_value(text: str, line_number: int) -> Any:
    if text.startswith('"'):
        value, position = _parse_quoted_at(text, 0, line_number)
        if text[position:].strip():
            _refuse("policy_syntax_invalid", f"line {line_number} has trailing string content")
        return value
    if text.startswith("["):
        return _parse_array(text, line_number)
    if text == "true":
        return True
    if text == "false":
        return False
    if INTEGER_PATTERN.fullmatch(text):
        try:
            return int(text)
        except ValueError as exc:
            raise ProtocolRefusal("policy_syntax_invalid", f"line {line_number} has an invalid integer") from exc
    _refuse("policy_syntax_invalid", f"line {line_number} has an unsupported scalar")


def _parse_quoted_at(text: str, position: int, line_number: int) -> Tuple[str, int]:
    if position >= len(text) or text[position] != '"':
        _refuse("policy_syntax_invalid", f"line {line_number} expects a quoted string")
    result: List[str] = []
    index = position + 1
    escapes = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
    while index < len(text):
        character = text[index]
        if character == '"':
            value = "".join(result)
            if len(value) > MAX_STRING_CHARACTERS or len(value.encode("utf-8")) > MAX_STRING_BYTES:
                _refuse("policy_string_oversize", f"line {line_number} string exceeds the v0 bound")
            return value, index + 1
        if character == "\\":
            index += 1
            if index >= len(text) or text[index] not in escapes:
                _refuse("policy_escape_invalid", f"line {line_number} has an unsupported string escape")
            result.append(escapes[text[index]])
        elif ord(character) < 0x20:
            _refuse("policy_syntax_invalid", f"line {line_number} has a control character in a string")
        else:
            result.append(character)
        index += 1
    _refuse("policy_syntax_invalid", f"line {line_number} has an unterminated string")


def _parse_array(text: str, line_number: int) -> List[Any]:
    if not text.endswith("]"):
        _refuse("policy_syntax_invalid", f"line {line_number} has an unterminated array")
    values: List[Any] = []
    position = 1
    expecting_value = True
    while True:
        while position < len(text) - 1 and text[position].isspace():
            position += 1
        if position == len(text) - 1:
            if expecting_value and values:
                _refuse("policy_syntax_invalid", f"line {line_number} has a trailing array comma")
            break
        if not expecting_value:
            if text[position] != ",":
                _refuse("policy_syntax_invalid", f"line {line_number} array values need commas")
            position += 1
            expecting_value = True
            continue
        if text[position] == '"':
            value, position = _parse_quoted_at(text, position, line_number)
        else:
            start = position
            while position < len(text) - 1 and text[position] != ",":
                if text[position] in "[]{}":
                    _refuse("policy_syntax_invalid", f"line {line_number} has a nested or inline array value")
                position += 1
            token = text[start:position].strip()
            value = _parse_value(token, line_number)
        values.append(value)
        if len(values) > MAX_ARRAY_ITEMS:
            _refuse("policy_array_oversize", f"line {line_number} exceeds the v0 array bound")
        expecting_value = False
    if values and any(type(value) is not type(values[0]) for value in values[1:]):
        _refuse("policy_array_mixed", f"line {line_number} array values must be homogeneous")
    return values


def _build_policy(raw: Dict[str, Any]) -> RepositoryPolicy:
    expected_root = ("schema_version", "capability_registry") + _REQUIRED_ROOT_TABLES
    root = _validate_exact_keys(raw, expected_root, "policy root")
    schema_version = _validate_bounded_integer(root["schema_version"], "schema_version", 0, 0)
    capability_registry = _validate_sorted_identifier_set(
        root["capability_registry"], "capability_registry"
    )

    limits_raw = _validate_exact_keys(root["limits"], tuple(_LIMIT_BOUNDS), "limits")
    limits = {
        key: _validate_bounded_integer(limits_raw[key], "limits." + key, *bounds)
        for key, bounds in _LIMIT_BOUNDS.items()
    }

    budgets = _build_budgets(root["budgets"])
    worker_profiles = _build_worker_profiles(root["worker_profiles"])
    capability_selectors = _build_capability_selectors(root["capability_selectors"])
    registered = set(capability_registry)
    for identifier, profile in worker_profiles.items():
        if not set(profile.capabilities) <= registered:
            _refuse(
                "policy_capability_unregistered",
                f"worker_profiles.{identifier} names a capability outside capability_registry",
            )
    for identifier, selector in capability_selectors.items():
        if not set(selector.all_of) <= registered:
            _refuse(
                "policy_capability_unregistered",
                f"capability_selectors.{identifier} names a capability outside capability_registry",
            )
    routing = _build_routing(root["routing"], worker_profiles, capability_selectors)
    retry_classes = _build_retry_classes(root["retry_classes"])
    approval_requirements = _build_approval_requirements(root["approval_requirements"])
    verification = _build_verification(root["verification"])
    merge_gates = _build_merge_gates(root["merge_gates"], verification)

    semantic = {
        "schema_version": schema_version,
        "capability_registry": list(capability_registry),
        "limits": limits,
        "budgets": {
            identifier: {"unit": budget.unit, "limit": budget.limit}
            for identifier, budget in budgets.items()
        },
        "worker_profiles": {
            identifier: {
                "capabilities": list(profile.capabilities),
                "cancel_mode": profile.cancel_mode,
                "callback_support": profile.callback_support,
                "max_concurrency": profile.max_concurrency,
                **(
                    {"secret_isolation": profile.secret_isolation}
                    if profile.secret_isolation is not None
                    else {}
                ),
            }
            for identifier, profile in worker_profiles.items()
        },
        "capability_selectors": {
            identifier: {"all_of": list(selector.all_of)}
            for identifier, selector in capability_selectors.items()
        },
        "routing": {
            identifier: {
                "worker_profile": route.worker_profile,
                "capability_selector": route.capability_selector,
                "rank": route.rank,
            }
            for identifier, route in routing.items()
        },
        "retry_classes": retry_classes,
        "approval_requirements": approval_requirements,
        "verification": {
            identifier: {"argv": list(command.argv)}
            for identifier, command in verification.items()
        },
        "merge_gates": {
            identifier: {"verification_ids": list(gate.verification_ids)}
            for identifier, gate in merge_gates.items()
        },
    }
    try:
        canonical_bytes = json.dumps(
            semantic,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProtocolRefusal("policy_canonical_invalid", "policy cannot form canonical I-JSON") from exc
    routes = tuple(sorted(routing.values(), key=lambda route: (route.rank, route.route_id)))
    return RepositoryPolicy(
        schema_version=schema_version,
        capability_registry=capability_registry,
        limits=_freeze_mapping(limits),
        budgets=_freeze_mapping(budgets),
        worker_profiles=_freeze_mapping(worker_profiles),
        capability_selectors=_freeze_mapping(capability_selectors),
        routing=_freeze_mapping(routing),
        retry_classes=_freeze_mapping(retry_classes),
        approval_requirements=_freeze_mapping(approval_requirements),
        verification=_freeze_mapping(verification),
        merge_gates=_freeze_mapping(merge_gates),
        routes=routes,
        canonical_bytes=canonical_bytes,
        digest=hashlib.sha256(canonical_bytes).hexdigest(),
    )


def _validate_named_mapping(value: object, field: str) -> Mapping[str, Dict[str, Any]]:
    if not isinstance(value, dict) or not value or len(value) > MAX_NAMED_ENTRIES:
        _refuse("policy_table_count_invalid", f"{field} must have one through {MAX_NAMED_ENTRIES} entries")
    result: Dict[str, Dict[str, Any]] = {}
    for identifier, entry in value.items():
        result[_validate_identifier(identifier, field)] = entry
        if not isinstance(entry, dict):
            _refuse("policy_table_invalid", f"{field}.{identifier} must be a table")
    return result


def _build_budgets(value: object) -> Mapping[str, Budget]:
    result: Dict[str, Budget] = {}
    for identifier, entry in _validate_named_mapping(value, "budgets").items():
        fields = _validate_exact_keys(entry, ("unit", "limit"), "budgets." + identifier)
        unit = _validate_plain_text(fields["unit"], "budgets." + identifier + ".unit")
        if unit not in _BUDGET_UNITS:
            _refuse("policy_budget_unit_invalid", "budget unit is outside the v0 vocabulary")
        limit = _validate_bounded_integer(
            fields["limit"], "budgets." + identifier + ".limit", 1, MAX_BUDGET_LIMIT
        )
        result[identifier] = Budget(identifier, unit, limit)
    return dict(sorted(result.items()))


def _build_worker_profiles(value: object) -> Mapping[str, WorkerProfile]:
    result: Dict[str, WorkerProfile] = {}
    for identifier, entry in _validate_named_mapping(value, "worker_profiles").items():
        required = {
            "capabilities", "cancel_mode", "callback_support", "max_concurrency",
        }
        if set(entry) not in (required, required | {"secret_isolation"}):
            _refuse(
                "policy_fields_invalid",
                "worker_profiles." + identifier
                + " must contain capabilities, cancel_mode, callback_support, "
                "max_concurrency, and optional secret_isolation",
            )
        fields = entry
        capabilities = _validate_sorted_identifier_set(
            fields["capabilities"], "worker_profiles." + identifier + ".capabilities"
        )
        cancel_mode = _validate_plain_text(
            fields["cancel_mode"], "worker_profiles." + identifier + ".cancel_mode"
        )
        if cancel_mode not in _CANCEL_MODES:
            _refuse("policy_cancel_mode_invalid", "worker cancel_mode is outside the v0 vocabulary")
        callback_support = _validate_bool(
            fields["callback_support"], "worker_profiles." + identifier + ".callback_support"
        )
        max_concurrency = _validate_bounded_integer(
            fields["max_concurrency"], "worker_profiles." + identifier + ".max_concurrency", 1, MAX_CONCURRENCY
        )
        secret_isolation = fields.get("secret_isolation")
        if secret_isolation is not None:
            secret_isolation = _validate_plain_text(
                secret_isolation,
                "worker_profiles." + identifier + ".secret_isolation",
            )
            if secret_isolation not in _SECRET_ISOLATION_MODES:
                _refuse(
                    "policy_secret_isolation_invalid",
                    "worker secret_isolation is outside process, helper, or none",
                )
        result[identifier] = WorkerProfile(
            identifier, capabilities, cancel_mode, callback_support,
            max_concurrency, secret_isolation,
        )
    return dict(sorted(result.items()))


def _build_capability_selectors(value: object) -> Mapping[str, CapabilitySelector]:
    result: Dict[str, CapabilitySelector] = {}
    for identifier, entry in _validate_named_mapping(value, "capability_selectors").items():
        fields = _validate_exact_keys(entry, ("all_of",), "capability_selectors." + identifier)
        result[identifier] = CapabilitySelector(
            identifier,
            _validate_sorted_identifier_set(fields["all_of"], "capability_selectors." + identifier + ".all_of"),
        )
    return dict(sorted(result.items()))


def _build_routing(
    value: object,
    worker_profiles: Mapping[str, WorkerProfile],
    capability_selectors: Mapping[str, CapabilitySelector],
) -> Mapping[str, RoutingRule]:
    result: Dict[str, RoutingRule] = {}
    ranks = set()
    for identifier, entry in _validate_named_mapping(value, "routing").items():
        fields = _validate_exact_keys(
            entry, ("worker_profile", "capability_selector", "rank"), "routing." + identifier
        )
        worker_profile = _validate_identifier(fields["worker_profile"], "routing." + identifier + ".worker_profile")
        selector = _validate_identifier(
            fields["capability_selector"], "routing." + identifier + ".capability_selector"
        )
        if worker_profile not in worker_profiles or selector not in capability_selectors:
            _refuse("policy_routing_reference_invalid", "routing must reference declared profile and selector")
        rank = _validate_bounded_integer(fields["rank"], "routing." + identifier + ".rank", 0, MAX_ROUTE_RANK)
        if rank in ranks:
            _refuse("policy_routing_rank_duplicate", "routing ranks must be globally unique")
        ranks.add(rank)
        if not set(capability_selectors[selector].all_of).issubset(worker_profiles[worker_profile].capabilities):
            _refuse("policy_routing_capability_invalid", "routed worker does not satisfy its selector")
        result[identifier] = RoutingRule(identifier, worker_profile, selector, rank)
    return dict(sorted(result.items()))


def _build_retry_classes(value: object) -> Mapping[str, bool]:
    entries = _validate_named_mapping(value, "retry_classes")
    if set(entries) != set(_RETRY_CLASSES):
        _refuse("policy_retry_classes_invalid", "retry_classes must contain exactly the frozen six terms")
    result: Dict[str, bool] = {}
    for identifier in _RETRY_CLASSES:
        fields = _validate_exact_keys(entries[identifier], ("automatic",), "retry_classes." + identifier)
        automatic = _validate_bool(fields["automatic"], "retry_classes." + identifier + ".automatic")
        if automatic != (identifier == "transient"):
            _refuse("policy_retry_relaxation", "only transient may be automatically retried")
        result[identifier] = automatic
    return dict(sorted(result.items()))


def _build_approval_requirements(value: object) -> Mapping[str, bool]:
    entries = _validate_named_mapping(value, "approval_requirements")
    if set(entries) != set(_RISK_CLASSES):
        _refuse("policy_risk_classes_invalid", "approval_requirements must contain exactly the four risks")
    result: Dict[str, bool] = {}
    for identifier in _RISK_CLASSES:
        fields = _validate_exact_keys(
            entries[identifier], ("required",), "approval_requirements." + identifier
        )
        result[identifier] = _validate_bool(
            fields["required"], "approval_requirements." + identifier + ".required"
        )
    return dict(sorted(result.items()))


def _build_verification(value: object) -> Mapping[str, VerificationCommand]:
    result: Dict[str, VerificationCommand] = {}
    for identifier, entry in _validate_named_mapping(value, "verification").items():
        fields = _validate_exact_keys(entry, ("argv",), "verification." + identifier)
        result[identifier] = VerificationCommand(
            identifier, _validate_argv(fields["argv"], "verification." + identifier + ".argv")
        )
    return dict(sorted(result.items()))


def _build_merge_gates(
    value: object, verification: Mapping[str, VerificationCommand]
) -> Mapping[str, MergeGate]:
    result: Dict[str, MergeGate] = {}
    for identifier, entry in _validate_named_mapping(value, "merge_gates").items():
        fields = _validate_exact_keys(entry, ("verification_ids",), "merge_gates." + identifier)
        verification_ids = _validate_sorted_identifier_set(
            fields["verification_ids"], "merge_gates." + identifier + ".verification_ids"
        )
        if any(command_id not in verification for command_id in verification_ids):
            _refuse("policy_merge_gate_reference_invalid", "merge gates must name declared verification commands")
        result[identifier] = MergeGate(identifier, verification_ids)
    return dict(sorted(result.items()))


__all__ = [
    "Budget",
    "CapabilitySelector",
    "MergeGate",
    "Policy",
    "PolicyDeploymentCheck",
    "PolicyDeploymentChecker",
    "PolicyDeploymentStatus",
    "RepositoryPolicy",
    "RoutingRule",
    "VerificationCommand",
    "WorkerProfile",
    "check_policy_deployment",
]

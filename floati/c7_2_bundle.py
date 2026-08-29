"""Read-only C7.2 segment-aware bundle materialization and verification.

This sibling reader deliberately accepts only explicit v0 absence or explicit
v1 segment fields.  It never repairs a missing predecessor, selects an order
from a timestamp, or makes C7.1 understand a newer source record.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Mapping, Sequence

from . import c7_bundle as c7
from .errors import ProtocolRefusal
from .records import validate_repository_coordinate
from .root import FloatiRoot, validate_identifier


C7_2_SCHEMA_VERSION = "c7.2-candidate"
C7_2_INDEX_KIND = "c7_read_bundle_index"
C7_2_PROJECTION_KIND = "c7_canonical_projection"
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "bundle" / "c7.2"
_RUNTIME_ROOT = Path(__file__).resolve().parent.parent
_SEGMENT_KINDS = ("initial", "resume", "fork", "handoff")
_SEGMENT_ID = re.compile(r"^seg-[0-9a-f]{12}7[0-9a-f]{3}[89ab][0-9a-f]{15}$")
_HEX = frozenset("0123456789abcdef")
_C7_2_SOURCE_VERSIONS = frozenset({0, 1})
_INDEX_SCHEMA_IDENTITY = {
    "id": "https://landoclusters.com/floati/schemas/c7.2/c7-read-bundle.schema.json",
    "version": C7_2_SCHEMA_VERSION,
    "file": "schemas/c7-read-bundle.schema.json",
}
_INDEX_PREDECESSOR = {
    "path": "bundle/c7.1/bundle-index.json",
    "version": "c7.1-candidate",
    "mutation": "forbidden",
}


def canonical_json_bytes(value: object) -> bytes:
    """Expose the common compact I-JSON digest representation."""

    return c7.canonical_json_bytes(value)


def semantic_digest(projection: Mapping[str, object]) -> str:
    """Hash the timestamp-free semantic projection domain."""

    return c7.semantic_digest(projection)


def self_digest(projection: Mapping[str, object]) -> str:
    """Hash an emitted projection except its self-referential digest field."""

    return c7.self_digest(projection)


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX


def _require_mapping(value: object, code: str, detail: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProtocolRefusal(code, detail)
    return value


def _require_exact_keys(
    value: Mapping[str, object], keys: set[str], code: str, detail: str
) -> None:
    if set(value) != keys:
        raise ProtocolRefusal(code, detail)


def _require_nonempty_string(value: object, code: str, detail: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProtocolRefusal(code, detail)
    return value


def validate_c7_2_index(index: Mapping[str, object]) -> Dict[str, object]:
    """Fail closed before selecting an unsupported C7.2 contract package."""

    if index.get("schema_version") != C7_2_SCHEMA_VERSION:
        raise ProtocolRefusal("c7_version_unsupported", "C7 index version is not understood")
    expected = {
        "schema_version",
        "kind",
        "title",
        "approvals",
        "reader_upgrade",
        "index_schema",
        "predecessor",
        "schema_catalog",
        "families",
    }
    if set(index) != expected:
        raise ProtocolRefusal("c7_index_shape_invalid", "C7 index fields are not understood")
    if index.get("kind") != C7_2_INDEX_KIND:
        raise ProtocolRefusal("c7_index_kind_invalid", "C7 index kind is not understood")
    if index.get("approvals") != "excluded-c7.2":
        raise ProtocolRefusal("c7_approvals_not_excluded", "C7.2 cannot infer approval joins")
    _require_nonempty_string(index.get("title"), "c7_index_title_invalid", "C7 index needs a title")
    upgrade = index.get("reader_upgrade")
    if (
        not isinstance(upgrade, dict)
        or set(upgrade) != {"highest_understood", "unknown"}
        or upgrade.get("highest_understood") is not True
        or upgrade.get("unknown") != "fail_closed"
    ):
        raise ProtocolRefusal(
            "c7_upgrade_rule_invalid", "C7 index must fail closed for unknown versions"
        )
    if index.get("index_schema") != _INDEX_SCHEMA_IDENTITY:
        raise ProtocolRefusal(
            "c7_index_schema_invalid", "C7 index schema identity is not understood"
        )
    if index.get("predecessor") != _INDEX_PREDECESSOR:
        raise ProtocolRefusal(
            "c7_predecessor_invalid", "C7.2 must preserve its frozen C7.1 predecessor"
        )
    if index.get("schema_catalog") != "schema-catalog.json":
        raise ProtocolRefusal("c7_schema_catalog_invalid", "C7 schema catalog path is invalid")
    if index.get("families") != c7._INDEX_FAMILIES:
        raise ProtocolRefusal("c7_families_invalid", "C7 index needs its declared read families")
    return dict(index)


def _relative_path(value: object, code: str, detail: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise ProtocolRefusal(code, detail)
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ProtocolRefusal(code, detail)
    return path


def _catalog_sources(catalog: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    projection = catalog.get("projection_schema")
    if isinstance(projection, Mapping):
        yield projection
    entries = catalog.get("entries")
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        sources = entry.get("sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if isinstance(source, Mapping):
                yield source


def validate_c7_2_catalog(catalog: Mapping[str, object]) -> Dict[str, object]:
    """Validate every copied-source pointer before it controls a filesystem read."""

    if set(catalog) != {"schema_version", "kind", "projection_schema", "entries"}:
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog fields are invalid")
    if (
        catalog.get("schema_version") != C7_2_SCHEMA_VERSION
        or catalog.get("kind") != "c7_schema_catalog"
    ):
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog identity is invalid")
    projection = catalog.get("projection_schema")
    if not isinstance(projection, Mapping) or set(projection) != {
        "id",
        "version",
        "file",
        "sha256",
    }:
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 projection source is invalid")
    if (
        projection.get("id")
        != "https://landoclusters.com/floati/schemas/c7.2/canonical-projection.schema.json"
        or projection.get("version") != C7_2_SCHEMA_VERSION
        or projection.get("file") != "schemas/canonical-projection.schema.json"
        or not _is_digest(projection.get("sha256"))
    ):
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 projection source is invalid")
    entries = catalog.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog entries are invalid")
    source_fields = {"id", "version", "file", "sha256", "pointers", "ledger", "state_role"}
    entry_fields = {
        "family",
        "ledger",
        "ledger_template",
        "representation",
        "reason",
        "sources",
        "exposure",
    }
    families: set[str] = set()
    v1_binding_sources: list[Mapping[str, object]] = []
    approval_representation: object = None
    for entry in entries:
        if not isinstance(entry, Mapping) or not set(entry) <= entry_fields:
            raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog family is invalid")
        family = _require_nonempty_string(
            entry.get("family"), "c7_catalog_shape_invalid", "C7 catalog family is invalid"
        )
        if family in families or not isinstance(entry.get("sources"), list):
            raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog family is invalid")
        families.add(family)
        if family == "approval":
            approval_representation = entry.get("representation")
        for source in entry["sources"]:
            if not isinstance(source, Mapping) or not set(source) <= source_fields:
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog source is invalid")
            required = {"id", "version", "file", "sha256", "pointers"}
            if not required <= set(source):
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog source is invalid")
            version = source.get("version")
            if not _is_supported_source_version(version):
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog source version is invalid")
            _require_nonempty_string(
                source.get("id"), "c7_catalog_shape_invalid", "C7 catalog source is invalid"
            )
            _relative_path(
                source.get("file"), "c7_catalog_shape_invalid", "C7 catalog source path is invalid"
            )
            if not _is_digest(source.get("sha256")):
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog digest is invalid")
            pointers = source.get("pointers")
            if not isinstance(pointers, list) or not all(
                isinstance(pointer, str) and pointer.startswith("/") for pointer in pointers
            ):
                raise ProtocolRefusal("c7_catalog_shape_invalid", "C7 catalog pointers are invalid")
            if (
                source.get("id")
                == "https://landoclusters.com/floati/schemas/v1/attempt-harness-session-bound-record.schema.json"
            ):
                v1_binding_sources.append(source)
    expected_pointers = [
        "/run_id",
        "/item_id",
        "/attempt_id",
        "/fence_token",
        "/claim_id",
        "/lease_id",
        "/worker_session_id",
        "/harness_segments",
        "/harness_segments/n/ordinal",
        "/harness_segments/n/harness_session_id",
        "/harness_segments/n/segment_id",
        "/harness_segments/n/segment_kind",
        "/harness_segments/n/predecessor_segment_id",
    ]
    if len(v1_binding_sources) != 1:
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7.2 v1 binding source is absent")
    if approval_representation != "excluded-c7.2":
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7.2 approvals must remain excluded")
    binding = v1_binding_sources[0]
    if (
        binding.get("version") != 1
        or binding.get("file") != "schemas/v1/attempt-harness-session-bound-record.schema.json"
        or binding.get("pointers") != expected_pointers
    ):
        raise ProtocolRefusal("c7_catalog_shape_invalid", "C7.2 v1 binding source is invalid")
    predecessor = c7._load_json(
        _RUNTIME_ROOT / "bundle/c7.1/schema-catalog.json",
        "c7_catalog_predecessor_invalid",
    )
    normalized = deepcopy(dict(catalog))
    normalized["schema_version"] = predecessor.get("schema_version")
    normalized["projection_schema"] = deepcopy(predecessor.get("projection_schema"))
    predecessor_entries = {
        entry.get("family"): entry
        for entry in predecessor.get("entries", [])
        if isinstance(entry, Mapping)
    }
    for entry in normalized["entries"]:
        family = entry.get("family")
        if family == "approval":
            entry["representation"] = "excluded-c7.1"
        if family == "worker_harness_binding":
            entry["sources"] = [
                source for source in entry["sources"] if source.get("version") != 1
            ]
        if family in {"logical_outcome", "run_outcome"}:
            prior = predecessor_entries.get(family)
            if not isinstance(prior, Mapping):
                raise ProtocolRefusal(
                    "c7_catalog_predecessor_invalid",
                    "C7.1 predecessor catalog is incomplete",
                )
            entry["sources"] = deepcopy(prior.get("sources"))
    if normalized != predecessor:
        raise ProtocolRefusal(
            "c7_catalog_shape_invalid",
            "C7.2 must preserve the complete C7.1 catalog inventory",
        )
    return dict(catalog)


def _is_supported_source_version(value: object) -> bool:
    return (type(value) is int and value in {0, 1}) or value == C7_2_SCHEMA_VERSION


def _static_inventory(package: Path) -> list[PurePosixPath]:
    if package.is_symlink() or not package.is_dir():
        raise ProtocolRefusal("c7_contract_package_missing", "checked-in C7.2 package is absent")
    files: list[PurePosixPath] = []
    for path in sorted(package.rglob("*")):
        if path.is_symlink():
            raise ProtocolRefusal("c7_contract_package_invalid", "C7 package contains a symlink")
        if path.is_file():
            files.append(PurePosixPath(path.relative_to(package).as_posix()))
    if not files:
        raise ProtocolRefusal("c7_contract_package_missing", "checked-in C7.2 package is absent")
    return files


def _safe_regular_file(root: Path, relative: object, code: str, detail: str) -> Path:
    path = _relative_path(relative, code, detail)
    if c7._has_unsafe_symlink_component(root) or root.is_symlink() or not root.is_dir():
        raise ProtocolRefusal(code, detail)
    result = root
    for part in path.parts:
        result = result / part
        if result.is_symlink():
            raise ProtocolRefusal(code, detail)
    if not result.is_file():
        raise ProtocolRefusal(code, detail)
    return result


def _catalog_schema_source_path(source: Mapping[str, object]) -> Path:
    version = source.get("version")
    base = _PACKAGE_ROOT if version == C7_2_SCHEMA_VERSION else _RUNTIME_ROOT
    path = _safe_regular_file(
        base,
        source.get("file"),
        "c7_catalog_schema_missing",
        "C7 catalog schema source is unavailable",
    )
    if hashlib.sha256(path.read_bytes()).hexdigest() != source.get("sha256"):
        raise ProtocolRefusal("c7_catalog_schema_digest_invalid", "C7 catalog schema digest is invalid")
    return path


def _verify_static_inventory(root: Path) -> None:
    package = _PACKAGE_ROOT.resolve()
    for relative in _static_inventory(package):
        expected = _safe_regular_file(
            package,
            relative.as_posix(),
            "c7_contract_package_invalid",
            "C7 checked-in package file is invalid",
        )
        actual = _safe_regular_file(
            root,
            relative.as_posix(),
            "c7_static_inventory_invalid",
            "C7 snapshot package file is invalid",
        )
        if hashlib.sha256(expected.read_bytes()).hexdigest() != hashlib.sha256(
            actual.read_bytes()
        ).hexdigest():
            raise ProtocolRefusal(
                "c7_static_inventory_invalid", "C7 snapshot package digest is invalid"
            )


def _catalog_schema_entries(catalog: Mapping[str, object]) -> Iterable[Mapping[str, object]]:
    yield from _catalog_sources(catalog)


def _copy_catalog_schemas(destination: Path, catalog: Mapping[str, object]) -> None:
    copied: set[tuple[object, object]] = set()
    for source in _catalog_schema_entries(catalog):
        key = (source.get("version"), source.get("file"))
        if key in copied:
            continue
        copied.add(key)
        if source.get("version") == C7_2_SCHEMA_VERSION:
            continue
        target = c7._output_path(
            destination,
            str(source["file"]),
            "c7_destination_unwritable",
            "C7 catalog schema cannot be copied",
        )
        try:
            shutil.copyfile(_catalog_schema_source_path(source), target)
        except OSError as exc:
            raise ProtocolRefusal(
                "c7_destination_unwritable", "C7 catalog schema cannot be copied"
            ) from exc


def _verify_catalog_schemas(root: Path, catalog: Mapping[str, object]) -> None:
    seen: set[tuple[object, object]] = set()
    for source in _catalog_schema_entries(catalog):
        key = (source.get("version"), source.get("file"))
        if key in seen:
            continue
        seen.add(key)
        path = _safe_regular_file(
            root,
            source.get("file"),
            "c7_catalog_schema_missing",
            "C7 copied catalog schema is unavailable",
        )
        if hashlib.sha256(path.read_bytes()).hexdigest() != source.get("sha256"):
            raise ProtocolRefusal(
                "c7_catalog_schema_digest_invalid", "C7 copied catalog schema digest is invalid"
            )
        schema = c7._load_json(path, "c7_catalog_schema_invalid")
        if schema.get("$id") != source.get("id"):
            raise ProtocolRefusal("c7_catalog_schema_invalid", "C7 copied schema identity is invalid")
        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            raise ProtocolRefusal("c7_catalog_schema_invalid", "C7 copied schema is invalid")
        version = properties.get("schema_version")
        if not isinstance(version, Mapping) or version.get("const") != source.get("version"):
            raise ProtocolRefusal("c7_catalog_schema_invalid", "C7 copied schema version is invalid")


def _preflight_destination(root: FloatiRoot, destination: Path) -> Path:
    if not destination.is_absolute():
        raise ProtocolRefusal("c7_destination_not_absolute", "C7 destination must be absolute")
    if c7._has_unsafe_symlink_component(destination):
        raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
    try:
        source_home = root.tenant_home.resolve()
        package = _PACKAGE_ROOT.resolve()
        resolved_destination = destination.resolve(strict=False)
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_invalid", "C7 destination cannot be resolved") from exc
    for protected, code, detail in (
        (
            source_home,
            "c7_destination_inside_source",
            "C7 snapshot destination cannot be inside its source",
        ),
        (
            package,
            "c7_destination_contract_package",
            "C7 snapshot destination cannot be the checked-in package",
        ),
    ):
        try:
            resolved_destination.relative_to(protected)
        except ValueError:
            continue
        raise ProtocolRefusal(code, detail)
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink():
            raise ProtocolRefusal("c7_destination_symlink", "C7 destination traverses a symlink")
        if not destination.is_dir():
            raise ProtocolRefusal("c7_destination_invalid", "C7 destination is not a directory")
        try:
            for current, directories, filenames in os.walk(destination, followlinks=False):
                for name in [*directories, *filenames]:
                    if (Path(current) / name).is_symlink():
                        raise ProtocolRefusal(
                            "c7_destination_symlink", "C7 destination traverses a symlink"
                        )
                    raise ProtocolRefusal(
                        "c7_destination_not_fresh", "C7 snapshot destination must be fresh"
                    )
        except OSError as exc:
            raise ProtocolRefusal("c7_destination_invalid", "C7 destination cannot be inspected") from exc
    return destination


def _copy_contract_package(destination: Path) -> Dict[str, object]:
    package = _PACKAGE_ROOT
    index = validate_c7_2_index(
        c7._load_json(
            _safe_regular_file(
                package,
                "bundle-index.json",
                "c7_contract_package_missing",
                "C7 index is absent",
            ),
            "c7_index_unreadable",
        )
    )
    catalog = validate_c7_2_catalog(
        c7._load_json(
            _safe_regular_file(
                package,
                "schema-catalog.json",
                "c7_contract_package_missing",
                "C7 catalog is absent",
            ),
            "c7_catalog_unreadable",
        )
    )
    for source in _catalog_schema_entries(catalog):
        _catalog_schema_source_path(source)
    for relative in _static_inventory(package):
        source = _safe_regular_file(
            package,
            relative.as_posix(),
            "c7_contract_package_invalid",
            "C7 package file is invalid",
        )
        target = c7._output_path(
            destination,
            relative.as_posix(),
            "c7_destination_unwritable",
            "C7 package file cannot be copied",
        )
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise ProtocolRefusal("c7_destination_unwritable", "C7 package cannot be copied") from exc
    _copy_catalog_schemas(destination, catalog)
    return index


def _write_snapshot_source(destination: Path, relative: str, raw: bytes) -> None:
    if not isinstance(raw, bytes):
        raise ProtocolRefusal("c7_snapshot_bytes_invalid", "C7 source must be exact bytes")
    target = c7._output_path(
        destination,
        c7.RAW_PREFIX + relative,
        "c7_destination_unwritable",
        "C7 snapshot source cannot be written",
    )
    try:
        target.write_bytes(raw)
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_unwritable", "C7 snapshot source cannot be written") from exc


def _source_v1_segments(record: Mapping[str, object]) -> list[Mapping[str, object]]:
    segments = record.get("harness_segments")
    if not isinstance(segments, list) or not segments:
        raise ProtocolRefusal("harness_segments_invalid", "v1 harness segments are invalid")
    output: list[Mapping[str, object]] = []
    for expected_ordinal, raw in enumerate(segments, start=1):
        segment = _require_mapping(raw, "harness_segments_invalid", "v1 harness segment is invalid")
        allowed = {
            "ordinal",
            "harness_session_id",
            "segment_id",
            "segment_kind",
            "predecessor_segment_id",
        }
        if not {
            "ordinal",
            "harness_session_id",
            "segment_id",
            "segment_kind",
        } <= set(segment) <= allowed or type(segment.get("ordinal")) is not int or segment.get(
            "ordinal"
        ) != expected_ordinal:
            raise ProtocolRefusal("harness_segments_invalid", "v1 segment shape is invalid")
        if not _SEGMENT_ID.fullmatch(str(segment.get("segment_id", ""))):
            raise ProtocolRefusal("segment_id_invalid", "segment_id is invalid")
        kind = segment.get("segment_kind")
        if kind not in _SEGMENT_KINDS:
            raise ProtocolRefusal("segment_kind_invalid", "segment_kind is invalid")
        if kind == "initial":
            if "predecessor_segment_id" in segment:
                raise ProtocolRefusal("harness_segments_invalid", "initial segment has a predecessor")
        elif not _SEGMENT_ID.fullmatch(str(segment.get("predecessor_segment_id", ""))):
            raise ProtocolRefusal(
                "harness_segments_invalid", "transition segment needs a predecessor"
            )
        output.append(segment)
    return output


def _validate_explicit_lineage(records: Sequence[Mapping[str, object]]) -> None:
    """Validate explicit physical predecessor locations without sorting or repair."""

    positions: dict[str, dict[str, tuple[int, int]]] = {}
    attempts_by_segment: dict[str, set[str]] = {}
    transitions: list[tuple[str, int, Mapping[str, object]]] = []
    for frame, record in enumerate(records, start=1):
        if record.get("kind") != "attempt_harness_session_bound":
            continue
        version = record.get("schema_version")
        if version == 0:
            continue
        if version != 1:
            raise ProtocolRefusal("schema_version_invalid", "C7.2 binding version is invalid")
        attempt_id = _require_nonempty_string(
            record.get("attempt_id"), "harness_segments_invalid", "attempt id is invalid"
        )
        attempt_positions = positions.setdefault(attempt_id, {})
        for segment in _source_v1_segments(record):
            segment_id = str(segment["segment_id"])
            if segment_id in attempt_positions:
                raise ProtocolRefusal(
                    "harness_segment_id_duplicate",
                    "segment_id must be unique within one attempt lineage",
                )
            attempt_positions[segment_id] = (frame, int(segment["ordinal"]))
            attempts_by_segment.setdefault(segment_id, set()).add(attempt_id)
            if segment["segment_kind"] != "initial":
                transitions.append((attempt_id, frame, segment))
    for attempt_id, frame, segment in transitions:
        predecessor = str(segment["predecessor_segment_id"])
        location = positions[attempt_id].get(predecessor)
        if location is None:
            if attempts_by_segment.get(predecessor, set()) - {attempt_id}:
                raise ProtocolRefusal(
                    "harness_predecessor_attempt_mismatch",
                    "harness predecessor must belong to the same attempt",
                )
            raise ProtocolRefusal(
                "harness_predecessor_missing",
                "harness predecessor must exist in the same attempt lineage",
            )
        if location >= (frame, int(segment["ordinal"])):
            raise ProtocolRefusal(
                "harness_predecessor_not_prior",
                "harness predecessor must be at an earlier physical position",
            )


def _segments_for_binding(record: Mapping[str, object], frame: int) -> list[Dict[str, object]]:
    record_id = _require_nonempty_string(
        record.get("id"), "c7_projection_shape_invalid", "binding record id is invalid"
    )
    version = record.get("schema_version")
    raw_segments = record.get("harness_segments")
    if not isinstance(raw_segments, list):
        raise ProtocolRefusal("c7_projection_shape_invalid", "binding segments are invalid")
    projected: list[Dict[str, object]] = []
    for raw in raw_segments:
        segment = _require_mapping(raw, "c7_projection_shape_invalid", "binding segment is invalid")
        ordinal = segment.get("ordinal")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
            raise ProtocolRefusal("c7_projection_shape_invalid", "binding ordinal is invalid")
        item: Dict[str, object] = {
            "source_ref": {"binding_record_id": record_id, "ordinal": ordinal},
            "harness_session_id": segment.get("harness_session_id"),
        }
        if version == 0:
            for field in ("segment_id", "segment_kind", "predecessor_segment_id"):
                item[field] = c7._absent(
                    "not_durable_c7_2", c7.RAW_RUN_LEDGER, first=frame, last=frame
                )
        elif version == 1:
            item["segment_id"] = {"state": "present", "value": segment.get("segment_id")}
            item["segment_kind"] = {"state": "present", "value": segment.get("segment_kind")}
            if "predecessor_segment_id" in segment:
                item["predecessor_segment_id"] = {
                    "state": "present",
                    "value": segment.get("predecessor_segment_id"),
                }
            else:
                item["predecessor_segment_id"] = c7._absent(
                    "initial_segment", c7.RAW_RUN_LEDGER, first=frame, last=frame
                )
        else:
            raise ProtocolRefusal("schema_version_invalid", "binding schema version is invalid")
        projected.append(item)
    return projected


def _decorate_binding(
    candidate: Mapping[str, object], source_bindings: Mapping[str, tuple[int, Mapping[str, object]]]
) -> Dict[str, object]:
    output = dict(candidate)
    record_id = _require_nonempty_string(
        output.get("binding_record_id"), "c7_projection_shape_invalid", "binding id is invalid"
    )
    if record_id not in source_bindings:
        raise ProtocolRefusal("c7_projection_shape_invalid", "binding source is missing")
    frame, record = source_bindings[record_id]
    output["segments"] = _segments_for_binding(record, frame)
    return output


def _decorate_session_family(
    family: Mapping[str, object], records: Sequence[Mapping[str, object]]
) -> Dict[str, object]:
    bindings = {
        str(record["id"]): (frame, record)
        for frame, record in enumerate(records, start=1)
        if record.get("kind") == "attempt_harness_session_bound"
    }
    output = {
        key: value for key, value in family.items() if key != "segment_relation_vocabulary"
    }
    output["segment_kind_vocabulary"] = list(_SEGMENT_KINDS)
    if output.get("state") != "present":
        return output
    value = _require_mapping(
        output.get("value"), "c7_projection_shape_invalid", "session binding family is invalid"
    )
    replaced: Dict[str, object] = {}
    for attempt_id, raw_entry in value.items():
        entry = _require_mapping(
            raw_entry, "c7_projection_shape_invalid", "session binding entry is invalid"
        )
        if "state" not in entry:
            replaced[str(attempt_id)] = _decorate_binding(entry, bindings)
            continue
        candidate = dict(entry)
        candidates = candidate.get("candidates")
        if not isinstance(candidates, list):
            raise ProtocolRefusal("c7_projection_shape_invalid", "binding candidates are invalid")
        candidate["candidates"] = [
            _decorate_binding(
                _require_mapping(item, "c7_projection_shape_invalid", "binding candidate is invalid"),
                bindings,
            )
            for item in candidates
        ]
        replaced[str(attempt_id)] = candidate
    output["value"] = replaced
    return output


def _capture_error(capture: Mapping[str, object]) -> None:
    error = capture.get("error")
    if not isinstance(error, Mapping):
        return
    state = error.get("state")
    if not isinstance(state, Mapping) or not isinstance(state.get("code"), str):
        raise ProtocolRefusal("c7_source_invalid", "C7 run source is invalid")
    raise ProtocolRefusal(str(state["code"]), "C7 run source cannot be projected")


def _project_from_captures(
    captures: Mapping[str, Mapping[str, object]], *, tenant_id: str, repository: str
) -> Dict[str, object]:
    run_capture = _require_mapping(
        captures.get("runs"), "c7_capture_invalid", "C7 run capture is invalid"
    )
    _capture_error(run_capture)
    records = run_capture.get("records")
    if not isinstance(records, list) or not all(isinstance(record, Mapping) for record in records):
        raise ProtocolRefusal("c7_capture_invalid", "C7 run capture records are invalid")
    source_records = list(records)
    _validate_explicit_lineage(source_records)
    base = c7._project_from_captures(captures, tenant_id=tenant_id, repository=repository)
    families = _require_mapping(
        base.get("families"), "c7_projection_shape_invalid", "C7 families are invalid"
    )
    projection: Dict[str, object] = dict(base)
    projection["schema_version"] = C7_2_SCHEMA_VERSION
    decorated = dict(families)
    decorated["session_bindings"] = _decorate_session_family(
        _require_mapping(
            families.get("session_bindings"),
            "c7_projection_shape_invalid",
            "C7 session binding family is invalid",
        ),
        source_records,
    )
    projection["families"] = decorated
    projection["semantic_digest"] = semantic_digest(projection)
    projection["self_digest"] = self_digest(projection)
    return projection


def project_c7_2(
    root: FloatiRoot,
    *,
    repository: str,
    raw_run_bytes: bytes | None = None,
    _captured_sources: Mapping[str, Mapping[str, object]] | None = None,
) -> Dict[str, object]:
    """Project one explicit source root from its exact physical bytes."""

    root = c7._require_root(root)
    repository = validate_repository_coordinate(repository)
    captures = (
        dict(_captured_sources)
        if _captured_sources is not None
        else c7._capture_sources(
            root,
            repository,
            raw_run_bytes=raw_run_bytes,
            allowed_schema_versions=_C7_2_SOURCE_VERSIONS,
        )
    )
    return _project_from_captures(captures, tenant_id=root.tenant_id, repository=repository)


def _validate_present_segment_value(
    value: object, detail: str, *, allowed: set[str] | None = None
) -> object:
    row = _require_mapping(value, "c7_projection_shape_invalid", detail)
    _require_exact_keys(row, {"state", "value"}, "c7_projection_shape_invalid", detail)
    if row.get("state") != "present":
        raise ProtocolRefusal("c7_projection_shape_invalid", detail)
    item = row.get("value")
    if allowed is not None:
        if item not in allowed:
            raise ProtocolRefusal("c7_projection_shape_invalid", detail)
    elif not _SEGMENT_ID.fullmatch(str(item)):
        raise ProtocolRefusal("c7_projection_shape_invalid", detail)
    return item


def _validate_absent_segment_value(
    value: object, binding_frame: int, source_index: Mapping[str, tuple[int, str]], detail: str
) -> None:
    c7._validate_absent(value, detail)
    row = _require_mapping(value, "c7_projection_shape_invalid", detail)
    pointer = _require_mapping(
        row.get("raw_fallback"), "c7_projection_shape_invalid", detail
    )
    c7._validate_run_fallback(pointer, source_index, detail)
    if pointer.get("first_frame") != binding_frame or pointer.get("last_frame") != binding_frame:
        raise ProtocolRefusal("c7_projection_shape_invalid", detail)


def _validate_binding(
    value: object, source_index: Mapping[str, tuple[int, str]], detail: str
) -> None:
    row = _require_mapping(value, "c7_projection_shape_invalid", detail)
    required = {
        "binding_record_id",
        "frame",
        "run_id",
        "item_id",
        "claim_id",
        "lease_id",
        "worker_session_id",
        "segments",
    }
    allowed = required | {"supersession"}
    if not required <= set(row) <= allowed:
        raise ProtocolRefusal("c7_projection_shape_invalid", detail)
    binding_frame, kind = c7._source_frame(source_index, row.get("binding_record_id"), detail)
    if kind != "attempt_harness_session_bound" or row.get("frame") != binding_frame:
        raise ProtocolRefusal("c7_projection_shape_invalid", detail)
    for field in ("run_id", "item_id", "claim_id", "lease_id", "worker_session_id"):
        _require_nonempty_string(row.get(field), "c7_projection_shape_invalid", detail)
    segments = row.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ProtocolRefusal("c7_projection_shape_invalid", detail)
    ordinals: set[int] = set()
    for raw in segments:
        segment = _require_mapping(raw, "c7_projection_shape_invalid", detail)
        _require_exact_keys(
            segment,
            {
                "source_ref",
                "harness_session_id",
                "segment_id",
                "segment_kind",
                "predecessor_segment_id",
            },
            "c7_projection_shape_invalid",
            detail,
        )
        source_ref = _require_mapping(
            segment.get("source_ref"), "c7_projection_shape_invalid", detail
        )
        _require_exact_keys(
            source_ref,
            {"binding_record_id", "ordinal"},
            "c7_projection_shape_invalid",
            detail,
        )
        ordinal = source_ref.get("ordinal")
        if (
            source_ref.get("binding_record_id") != row.get("binding_record_id")
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or ordinal < 1
            or ordinal in ordinals
        ):
            raise ProtocolRefusal("c7_projection_shape_invalid", detail)
        ordinals.add(ordinal)
        _require_nonempty_string(
            segment.get("harness_session_id"), "c7_projection_shape_invalid", detail
        )
        segment_id = _require_mapping(
            segment.get("segment_id"), "c7_projection_shape_invalid", detail
        )
        kind_value = _require_mapping(
            segment.get("segment_kind"), "c7_projection_shape_invalid", detail
        )
        predecessor = _require_mapping(
            segment.get("predecessor_segment_id"), "c7_projection_shape_invalid", detail
        )
        if segment_id.get("state") == "absent":
            _validate_absent_segment_value(segment_id, binding_frame, source_index, detail)
            _validate_absent_segment_value(kind_value, binding_frame, source_index, detail)
            _validate_absent_segment_value(predecessor, binding_frame, source_index, detail)
            continue
        _validate_present_segment_value(segment_id, detail)
        kind = _validate_present_segment_value(
            kind_value, detail, allowed=set(_SEGMENT_KINDS)
        )
        if kind == "initial":
            _validate_absent_segment_value(predecessor, binding_frame, source_index, detail)
        else:
            _validate_present_segment_value(predecessor, detail)
    if "supersession" in row:
        supersession = _require_mapping(
            row["supersession"], "c7_projection_shape_invalid", detail
        )
        _require_exact_keys(
            supersession,
            {"rule", "superseded_binding_record_ids"},
            "c7_projection_shape_invalid",
            detail,
        )
        prior = supersession.get("superseded_binding_record_ids")
        if supersession.get("rule") != "physical_binding_frame_order" or not isinstance(
            prior, list
        ) or not prior or len(prior) != len(set(prior)):
            raise ProtocolRefusal("c7_projection_shape_invalid", detail)
        for record_id in prior:
            prior_frame, prior_kind = c7._source_frame(source_index, record_id, detail)
            if prior_kind != "attempt_harness_session_bound" or prior_frame >= binding_frame:
                raise ProtocolRefusal("c7_projection_shape_invalid", detail)


def _validate_session_family(
    value: object, source_index: Mapping[str, tuple[int, str]]
) -> None:
    row = _require_mapping(value, "c7_projection_shape_invalid", "C7 session family is invalid")
    state = c7._validate_family(
        row,
        "C7 session family is invalid",
        extras={"segment_kind_vocabulary"},
    )
    if row.get("segment_kind_vocabulary") != list(_SEGMENT_KINDS):
        raise ProtocolRefusal("c7_projection_shape_invalid", "C7 segment vocabulary is invalid")
    if state != "present":
        return
    values = _require_mapping(
        row.get("value"), "c7_projection_shape_invalid", "C7 session family is invalid"
    )
    if not values:
        raise ProtocolRefusal("c7_projection_shape_invalid", "C7 session map is empty")
    for attempt_id, raw in values.items():
        _require_nonempty_string(
            attempt_id, "c7_projection_shape_invalid", "C7 session key is invalid"
        )
        entry = _require_mapping(
            raw, "c7_projection_shape_invalid", "C7 session entry is invalid"
        )
        if "state" not in entry:
            _validate_binding(entry, source_index, "C7 session binding is invalid")
            continue
        _require_exact_keys(
            entry,
            {"state", "raw_fallback", "candidates"},
            "c7_projection_shape_invalid",
            "C7 conflicting binding is invalid",
        )
        c7._validate_error(
            {"state": entry.get("state"), "raw_fallback": entry.get("raw_fallback")},
            "C7 conflicting binding is invalid",
        )
        state_value = _require_mapping(
            entry.get("state"), "c7_projection_shape_invalid", "C7 conflict state is invalid"
        )
        if state_value.get("code") != "conflicting_binding":
            raise ProtocolRefusal("c7_projection_shape_invalid", "C7 conflict code is invalid")
        candidates = entry.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ProtocolRefusal("c7_projection_shape_invalid", "C7 conflict candidates are invalid")
        for candidate in candidates:
            _validate_binding(candidate, source_index, "C7 conflict candidate is invalid")


def validate_c7_2_projection(projection: Mapping[str, object]) -> Dict[str, object]:
    """Validate structure and both digest domains before snapshot re-projection."""

    expected = {
        "schema_version",
        "kind",
        "tenant_id",
        "repository",
        "raw_source",
        "raw_source_digest",
        "source_frames",
        "families",
        "auxiliary_sources",
        "cross_ledger_rule",
        "semantic_digest",
        "self_digest",
    }
    if set(projection) != expected or c7._contains_none(projection):
        raise ProtocolRefusal("c7_projection_shape_invalid", "C7 projection fields are invalid")
    if projection.get("schema_version") != C7_2_SCHEMA_VERSION:
        raise ProtocolRefusal("c7_projection_version_invalid", "C7 projection version is invalid")
    if projection.get("kind") != C7_2_PROJECTION_KIND:
        raise ProtocolRefusal("c7_projection_kind_invalid", "C7 projection kind is invalid")
    try:
        validate_identifier(projection.get("tenant_id"), "c7_tenant")
    except ProtocolRefusal as exc:
        raise ProtocolRefusal("c7_projection_tenant_invalid", "C7 projection tenant is invalid") from exc
    try:
        validate_repository_coordinate(projection.get("repository"))
    except ProtocolRefusal as exc:
        raise ProtocolRefusal(
            "c7_projection_repository_invalid", "C7 projection repository is invalid"
        ) from exc
    if projection.get("raw_source") != c7.RAW_RUN_LEDGER or not _is_digest(
        projection.get("raw_source_digest")
    ):
        raise ProtocolRefusal("c7_projection_shape_invalid", "C7 raw source is invalid")
    source_index = c7._validate_source_frames(
        projection.get("source_frames"), "C7 source frames are invalid"
    )
    families = _require_mapping(
        projection.get("families"), "c7_projection_shape_invalid", "C7 families are invalid"
    )
    _require_exact_keys(
        families, set(c7._FAMILY_NAMES), "c7_projection_shape_invalid", "C7 family names are invalid"
    )
    current = {
        "runs",
        "work_items",
        "attempts",
        "claims",
        "leases",
        "retries",
        "cancellations",
        "result_phases",
        "logical_outcomes",
        "run_outcomes",
        "task_contracts",
    }
    for name in current:
        state = c7._validate_family(families[name], f"C7 {name} family is invalid")
        if state == "present":
            c7._validate_current_map(name, families[name], source_index)
    auxiliary = c7._validate_auxiliary_sources(
        projection.get("auxiliary_sources"), str(projection["repository"])
    )
    _validate_session_family(families["session_bindings"], source_index)
    c7._validate_supervisor_orphans(
        families["supervisor_orphans"], source_index, auxiliary["registry"]
    )
    c7._validate_decisions(families["decisions"], auxiliary["decisions"])
    if projection.get("cross_ledger_rule") != "no_timestamp_merge":
        raise ProtocolRefusal("c7_projection_shape_invalid", "C7 cross-ledger rule is invalid")
    if not _is_digest(projection.get("semantic_digest")) or not _is_digest(
        projection.get("self_digest")
    ):
        raise ProtocolRefusal("c7_projection_shape_invalid", "C7 projection digests are invalid")
    if projection.get("semantic_digest") != semantic_digest(projection):
        raise ProtocolRefusal(
            "c7_semantic_digest_invalid", "C7 semantic projection digest does not match"
        )
    if projection.get("self_digest") != self_digest(projection):
        raise ProtocolRefusal("c7_self_digest_invalid", "C7 projection digest does not match")
    return dict(projection)


def build_c7_2_bundle(
    root: FloatiRoot, destination: Path | str, *, repository: str
) -> Dict[str, object]:
    """Materialize one self-contained C7.2 snapshot from exact captured bytes."""

    root = c7._require_root(root)
    repository = validate_repository_coordinate(repository)
    destination_path = _preflight_destination(root, c7._destination_path(destination))
    captures = c7._capture_sources(
        root, repository, allowed_schema_versions=_C7_2_SOURCE_VERSIONS
    )
    projection = _project_from_captures(captures, tenant_id=root.tenant_id, repository=repository)
    try:
        destination_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_unwritable", "C7 destination cannot be created") from exc
    index = _copy_contract_package(destination_path)
    for name in ("runs", "worker_receipts", "registry", "decisions", "work_items"):
        capture = captures[name]
        _write_snapshot_source(destination_path, str(capture["relative"]), capture["raw"])
    target = c7._output_path(
        destination_path,
        "families/run-projection.json",
        "c7_destination_unwritable",
        "C7 projection cannot be written",
    )
    try:
        target.write_bytes(canonical_json_bytes(projection) + b"\n")
    except OSError as exc:
        raise ProtocolRefusal("c7_destination_unwritable", "C7 projection cannot be written") from exc
    return index


def _load_bundle_json(root: Path, relative: str, code: str) -> Dict[str, object]:
    return c7._load_json(
        _safe_regular_file(root, relative, code, "C7 bundle file is unavailable"), code
    )


def _read_bundle_bytes(root: Path, relative: str, code: str) -> bytes:
    path = _safe_regular_file(root, relative, code, "C7 bundle file is unavailable")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProtocolRefusal(code, "C7 bundle file cannot be read") from exc


def _snapshot_captures(root: Path, projection: Mapping[str, object]) -> Dict[str, Dict[str, object]]:
    tenant_id = validate_identifier(projection.get("tenant_id"), "c7_tenant")
    repository = validate_repository_coordinate(projection.get("repository"))
    auxiliary = _require_mapping(
        projection.get("auxiliary_sources"), "c7_projection_shape_invalid", "C7 auxiliary sources are invalid"
    )
    layouts = {
        "runs": (c7.RUN_LEDGER, frozenset(c7.RUN_KINDS), None),
        "worker_receipts": (c7.WORKER_LEDGER, frozenset(c7.WORKER_KINDS), "worker_receipts"),
        "registry": (c7.REGISTRY_LEDGER, frozenset({"registry_entry"}), "registry"),
        "decisions": (
            f"repositories/{repository}/decisions.jsonl",
            frozenset(c7.DECISION_KINDS),
            "decisions",
        ),
        "work_items": (c7.WORK_ITEM_LEDGER, frozenset(c7.WORK_KINDS), "work_items"),
    }
    captures: Dict[str, Dict[str, object]] = {}
    for name, (relative, kinds, auxiliary_name) in layouts.items():
        raw_relative = c7.RAW_PREFIX + relative
        raw = _read_bundle_bytes(
            root,
            raw_relative,
            "c7_raw_source_unreadable" if name == "runs" else "c7_auxiliary_source_unreadable",
        )
        if name == "runs":
            expected_digest = projection.get("raw_source_digest")
            exists = True
        else:
            source = _require_mapping(
                auxiliary.get(auxiliary_name),
                "c7_auxiliary_source_invalid",
                "C7 auxiliary source is invalid",
            )
            if source.get("ledger") != raw_relative:
                raise ProtocolRefusal("c7_auxiliary_source_invalid", "C7 auxiliary ledger is invalid")
            expected_digest = source.get("raw_source_digest")
            exists = source.get("state") != "absent"
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            code = (
                "c7_raw_source_digest_invalid"
                if name == "runs"
                else "c7_auxiliary_source_digest_invalid"
            )
            raise ProtocolRefusal(code, "C7 captured source digest does not match")
        captures[name] = c7._capture_bytes(
            name=name,
            relative=relative,
            raw=raw,
            exists=exists,
            tenant_id=tenant_id,
            allowed_kinds=kinds,
            allowed_schema_versions=_C7_2_SOURCE_VERSIONS,
        )
    c7._bound_decision_repository(captures["decisions"], repository)
    return captures


def read_c7_2_bundle(destination: Path | str) -> Dict[str, Dict[str, object]]:
    """Read, validate, and reproject a C7.2 snapshot from captured bytes only."""

    root = c7._destination_path(destination)
    if not root.is_absolute():
        raise ProtocolRefusal("c7_destination_not_absolute", "C7 bundle path must be absolute")
    if c7._has_unsafe_symlink_component(root) or root.is_symlink() or not root.is_dir():
        raise ProtocolRefusal("c7_destination_symlink", "C7 bundle path traverses a symlink")
    index = validate_c7_2_index(_load_bundle_json(root, "bundle-index.json", "c7_index_unreadable"))
    _verify_static_inventory(root)
    catalog = validate_c7_2_catalog(
        _load_bundle_json(root, str(index["schema_catalog"]), "c7_catalog_unreadable")
    )
    _verify_catalog_schemas(root, catalog)
    projection = validate_c7_2_projection(
        _load_bundle_json(root, "families/run-projection.json", "c7_projection_unreadable")
    )
    for ledger in c7._pointer_ledgers(projection):
        _read_bundle_bytes(root, ledger, "c7_raw_fallback_unreadable")
    captures = _snapshot_captures(root, projection)
    expected = _project_from_captures(
        captures,
        tenant_id=str(projection["tenant_id"]),
        repository=str(projection["repository"]),
    )
    if canonical_json_bytes(expected) != canonical_json_bytes(projection):
        raise ProtocolRefusal(
            "c7_projection_reprojection_invalid",
            "C7 projection does not match captured physical sources",
        )
    return {"index": index, "projection": projection}

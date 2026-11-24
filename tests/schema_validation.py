"""Small dependency-free validator for the JSON Schema vocabulary used in tests."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Tuple, cast


class SchemaValidationError(AssertionError):
    pass


# HARBOR-SPEED-R-C: every validation used to re-read, re-parse and re-resolve
# its schema and each `$ref` target from disk (measured at harbor main
# 1fa68e48: ~12 Path.resolve() and 2 json.loads per call, ~45% of the 20,232
# validations test_effects/test_schemas run per Unicode-parity test). Two
# remedies, neither of which can serve a changed schema stale:
#
# * Parsed documents are memoised per process under a CONTENT key -- the
#   resolved path plus the sha256 of the bytes read on THIS call. Every call
#   still reads every schema it touches, so an edited, replaced or reverted
#   file is a new key. The memo is private to this module and `_validate`
#   only reads documents, so a shared parse is never mutated.
# * `$ref` resolution -- each referenced FILE's resolved path, and each
#   (file, fragment) JSON pointer -- is memoised for the duration of ONE call
#   only, exactly the lifetime the per-call document cache already had.
_DOCUMENT_MEMO: Dict[Tuple[Path, str], Mapping[str, Any]] = {}


class _CallCache:
    """Per-call state: documents loaded, `$ref` files resolved, pointers followed."""

    __slots__ = ("documents", "paths", "references")

    def __init__(self) -> None:
        self.documents: Dict[Path, Mapping[str, Any]] = {}
        self.paths: Dict[Tuple[Path, str], Path] = {}
        self.references: Dict[
            Tuple[Path, str],
            Tuple[Mapping[str, Any], Mapping[str, Any], Path],
        ] = {}


def validate_json_schema(instance: object, schema_path: Path) -> None:
    cache = _CallCache()
    resolved = Path(schema_path).resolve()
    schema = _load(resolved, cache)
    _validate(instance, schema, schema, resolved, "$", cache)


def _load(path: Path, cache: _CallCache) -> Mapping[str, Any]:
    if path not in cache.documents:
        data = path.read_bytes()
        key = (path, hashlib.sha256(data).hexdigest())
        value = _DOCUMENT_MEMO.get(key)
        if value is None:
            # Path.read_text decodes with universal newlines; do exactly that.
            text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            value = json.loads(text)
            if not isinstance(value, dict):
                raise SchemaValidationError(f"{path}: schema root is not an object")
            _DOCUMENT_MEMO[key] = value
        cache.documents[path] = value
    return cache.documents[path]


def _dereference(
    reference: str,
    document: Mapping[str, Any],
    schema_path: Path,
    location: str,
    cache: _CallCache,
) -> Tuple[Mapping[str, Any], Mapping[str, Any], Path]:
    """(target schema, its document, its path) for one `$ref`, once per call."""

    key = (schema_path, reference)
    resolved = cache.references.get(key)
    if resolved is None:
        target_name, _, fragment = reference.partition("#")
        if not target_name:
            target_path = schema_path
        else:
            # Many fragments of one file: resolve the file once per call.
            path_key = (schema_path, target_name)
            cached_path = cache.paths.get(path_key)
            if cached_path is None:
                cached_path = (schema_path.parent / target_name).resolve()
                cache.paths[path_key] = cached_path
            target_path = cached_path
        target_document = document if target_path == schema_path else _load(target_path, cache)
        target = _pointer(target_document, fragment, location)
        resolved = (target, target_document, target_path)
        cache.references[key] = resolved
    return resolved


def _pointer(document: object, fragment: str, location: str) -> Mapping[str, Any]:
    current = document
    if fragment:
        if not fragment.startswith("/"):
            raise SchemaValidationError(f"{location}: unsupported reference fragment")
        for raw in fragment[1:].split("/"):
            token = raw.replace("~1", "/").replace("~0", "~")
            if not isinstance(current, dict) or token not in current:
                raise SchemaValidationError(f"{location}: unresolved reference fragment")
            current = current[token]
    if not isinstance(current, dict):
        raise SchemaValidationError(f"{location}: referenced schema is not an object")
    return current


def _json_equal(left: object, right: object) -> bool:
    if _is_json_number(left) and _is_json_number(right):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        right_list = cast(list, right)
        return len(left) == len(right_list) and all(
            _json_equal(left_item, right_item)
            for left_item, right_item in zip(left, right_list)
        )
    if isinstance(left, dict):
        right_dict = cast(dict, right)
        return left.keys() == right_dict.keys() and all(
            _json_equal(left[key], right_dict[key]) for key in left
        )
    return left == right


def _is_json_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _matches_type(value: object, expected: str) -> bool:
    return {
        "array": isinstance(value, list),
        "boolean": isinstance(value, bool),
        "integer": _is_json_number(value)
        and (isinstance(value, int) or value.is_integer()),
        "null": value is None,
        "number": _is_json_number(value),
        "object": isinstance(value, dict),
        "string": isinstance(value, str),
    }.get(expected, False)


def _validate(
    instance: object,
    schema: Mapping[str, Any],
    document: Mapping[str, Any],
    schema_path: Path,
    location: str,
    cache: _CallCache,
) -> None:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        target, target_document, target_path = _dereference(
            reference, document, schema_path, location, cache
        )
        _validate(instance, target, target_document, target_path, location, cache)
        return

    if "const" in schema and not _json_equal(instance, schema["const"]):
        raise SchemaValidationError(f"{location}: does not equal const")
    if "enum" in schema and not any(_json_equal(instance, item) for item in schema["enum"]):
        raise SchemaValidationError(f"{location}: value is outside enum")

    expected_type = schema.get("type")
    if expected_type is not None:
        choices = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(isinstance(choice, str) and _matches_type(instance, choice) for choice in choices):
            raise SchemaValidationError(f"{location}: type mismatch, expected {choices}")

    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for choice in all_of:
            if not isinstance(choice, dict):
                raise SchemaValidationError(f"{location}: allOf branch is not an object")
            _validate(instance, choice, document, schema_path, location, cache)

    condition = schema.get("if")
    if isinstance(condition, dict):
        try:
            _validate(instance, condition, document, schema_path, location, cache)
        except SchemaValidationError:
            selected = schema.get("else")
        else:
            selected = schema.get("then")
        if isinstance(selected, dict):
            _validate(instance, selected, document, schema_path, location, cache)

    if "oneOf" in schema:
        matches = 0
        for choice in schema["oneOf"]:
            try:
                _validate(instance, choice, document, schema_path, location, cache)
            except SchemaValidationError:
                continue
            matches += 1
        if matches != 1:
            raise SchemaValidationError(f"{location}: oneOf matched {matches} branches")

    if "anyOf" in schema:
        matches = 0
        for choice in schema["anyOf"]:
            try:
                _validate(instance, choice, document, schema_path, location, cache)
            except SchemaValidationError:
                continue
            matches += 1
        if matches == 0:
            raise SchemaValidationError(f"{location}: anyOf matched no branches")

    contains = schema.get("contains")
    if isinstance(contains, dict):
        if not isinstance(instance, list):
            raise SchemaValidationError(f"{location}: contains requires an array")
        matches = 0
        for index, value in enumerate(instance):
            try:
                _validate(
                    value,
                    contains,
                    document,
                    schema_path,
                    f"{location}[{index}]",
                    cache,
                )
            except SchemaValidationError:
                continue
            matches += 1
        minimum = schema.get("minContains", 1)
        maximum = schema.get("maxContains")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 0:
            raise SchemaValidationError(f"{location}: invalid minContains")
        if matches < minimum or (
            isinstance(maximum, int)
            and not isinstance(maximum, bool)
            and matches > maximum
        ):
            raise SchemaValidationError(
                f"{location}: contains matched {matches} items"
            )

    if "not" in schema:
        try:
            _validate(instance, schema["not"], document, schema_path, location, cache)
        except SchemaValidationError:
            pass
        else:
            raise SchemaValidationError(f"{location}: forbidden by not")

    if isinstance(instance, dict):
        required = schema.get("required", [])
        missing = [key for key in required if key not in instance]
        if missing:
            raise SchemaValidationError(f"{location}: missing required keys {missing}")
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            extras = set(instance) - set(properties)
            additional = schema.get("additionalProperties", True)
            if additional is False and extras:
                raise SchemaValidationError(f"{location}: additional keys {sorted(extras)}")
            for key, value in instance.items():
                if key in properties:
                    _validate(
                        value,
                        properties[key],
                        document,
                        schema_path,
                        f"{location}.{key}",
                        cache,
                    )
                elif isinstance(additional, dict):
                    _validate(
                        value,
                        additional,
                        document,
                        schema_path,
                        f"{location}.{key}",
                        cache,
                    )

    if isinstance(instance, list):
        sorted_key = schema.get("x-floati-sorted-unique-key")
        if isinstance(sorted_key, str):
            keys = [
                item.get(sorted_key) for item in instance if isinstance(item, dict)
            ]
            if (
                len(keys) != len(instance)
                or any(not isinstance(key, str) for key in keys)
                or keys != sorted(set(keys))
            ):
                raise SchemaValidationError(
                    f"{location}: object rows are not sorted and unique by {sorted_key}"
                )
        if schema.get("x-floati-sorted-unique"):
            encoded = [
                json.dumps(item, sort_keys=True, separators=(",", ":"))
                for item in instance
            ]
            if encoded != sorted(set(encoded)):
                raise SchemaValidationError(
                    f"{location}: array items are not sorted and unique"
                )
        if schema.get("x-floati-sorted-unique-budget"):
            keys = [item.get("budget_id") for item in instance if isinstance(item, dict)]
            if len(keys) != len(instance) or keys != sorted(set(keys)):
                raise SchemaValidationError(f"{location}: budget rows are not sorted and unique")
        if "minItems" in schema and len(instance) < schema["minItems"]:
            raise SchemaValidationError(f"{location}: too few items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            raise SchemaValidationError(f"{location}: too many items")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in instance]
            if len(encoded) != len(set(encoded)):
                raise SchemaValidationError(f"{location}: duplicate items")
        prefix_items = schema.get("prefixItems")
        prefix_count = 0
        if isinstance(prefix_items, list):
            prefix_count = len(prefix_items)
            for index, choice in enumerate(prefix_items):
                if index >= len(instance):
                    break
                if not isinstance(choice, dict):
                    raise SchemaValidationError(
                        f"{location}: prefixItems branch is not an object"
                    )
                _validate(
                    instance[index], choice, document, schema_path,
                    f"{location}[{index}]", cache,
                )
        items = schema.get("items")
        if items is False and len(instance) > prefix_count:
            raise SchemaValidationError(f"{location}: additional array items")
        if isinstance(items, dict):
            for index, value in enumerate(instance[prefix_count:], start=prefix_count):
                _validate(
                    value,
                    items,
                    document,
                    schema_path,
                    f"{location}[{index}]",
                    cache,
                )

    if isinstance(instance, str):
        if schema.get("x-floati-terminal-unsafe") and any(
            unicodedata.category(character) in {"Cc", "Cs"}
            or unicodedata.bidirectional(character)
            in {"LRE", "RLE", "LRO", "RLO", "PDF", "LRI", "RLI", "FSI", "PDI", "BN"}
            for character in instance
        ):
            raise SchemaValidationError(f"{location}: terminal-unsafe Unicode")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            raise SchemaValidationError(f"{location}: string is too short")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            raise SchemaValidationError(f"{location}: string is too long")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            raise SchemaValidationError(f"{location}: string does not match pattern")
        if schema.get("format") == "date-time":
            try:
                parsed = datetime.fromisoformat(instance.replace("Z", "+00:00"))
            except ValueError as exc:
                raise SchemaValidationError(f"{location}: invalid date-time") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise SchemaValidationError(f"{location}: date-time has no offset")

    if _is_json_number(instance):
        if "minimum" in schema and instance < schema["minimum"]:
            raise SchemaValidationError(f"{location}: value is below minimum")
        if "maximum" in schema and instance > schema["maximum"]:
            raise SchemaValidationError(f"{location}: value is above maximum")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            raise SchemaValidationError(f"{location}: value is not above exclusive minimum")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            raise SchemaValidationError(f"{location}: value is not below exclusive maximum")

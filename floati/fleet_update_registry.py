"""The only FU-1 transport registry writer: two lexical string spans."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .root import validate_identifier

_SHA1 = re.compile(r"[0-9a-f]{40}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PINS = ("manifest_sha256", "source_sha")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique(pairs: list[tuple[object, object]]) -> dict[object, object]:
    answer: dict[object, object] = {}
    for key, value in pairs:
        if key in answer:
            raise ValueError("duplicate key")
        answer[key] = value
    return answer


def _decode(raw: bytes) -> dict[object, object]:
    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal("fleet_update_transport_registry_invalid", "registry is not strict UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ProtocolRefusal("fleet_update_transport_registry_invalid", "registry is not an object")
    return decoded


def _spans(text: str, start: int, end: int) -> Dict[str, Tuple[int, int]]:
    decoder = json.JSONDecoder(object_pairs_hook=_unique)
    pos = start
    while pos < end and text[pos].isspace(): pos += 1
    if pos >= end or text[pos] != "{": raise ValueError("not object")
    pos += 1; result: Dict[str, Tuple[int, int]] = {}
    while True:
        while pos < end and text[pos].isspace(): pos += 1
        if pos < end and text[pos] == "}": return result
        key, pos = decoder.raw_decode(text, pos)
        if not isinstance(key, str) or key in result: raise ValueError("key")
        while pos < end and text[pos].isspace(): pos += 1
        if pos >= end or text[pos] != ":": raise ValueError("colon")
        pos += 1
        while pos < end and text[pos].isspace(): pos += 1
        value_start = pos; _value, pos = decoder.raw_decode(text, pos)
        result[key] = (value_start, pos)
        while pos < end and text[pos].isspace(): pos += 1
        if pos < end and text[pos] == ",": pos += 1; continue
        if pos < end and text[pos] == "}": return result
        raise ValueError("terminator")


def _real_file(path: Path) -> Path:
    try:
        if path.is_symlink() or not path.is_file(): raise OSError("not real file")
        selected = path.resolve(strict=True)
    except OSError as exc:
        raise ProtocolRefusal("fleet_update_transport_registry_invalid", "registry is not one real file") from exc
    if selected != path.absolute():
        raise ProtocolRefusal("fleet_update_transport_registry_invalid", "registry path is not canonical")
    return selected


def _identity(path: Path) -> tuple[int, int]:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ProtocolRefusal("fleet_update_transport_registry_drift", "registry disappeared") from exc
    if not os.path.isfile(path):
        raise ProtocolRefusal("fleet_update_transport_registry_drift", "registry is no longer one file")
    return st.st_dev, st.st_ino


def _pin_spans(raw: bytes, name: str) -> tuple[dict[object, object], dict[str, tuple[int, int]]]:
    document = _decode(raw); transports = document.get("transports")
    selected = transports.get(name) if isinstance(transports, dict) else None
    if not isinstance(selected, dict):
        raise ProtocolRefusal("fleet_update_transport_missing", f"transport {name} is absent")
    try:
        text = raw.decode("utf-8")
        selected_span = _spans(text, *_spans(text, 0, len(text))["transports"])[name]
        pins = _spans(text, *selected_span)
        output = {field: pins[field] for field in _PINS}
        for start, end in output.values():
            literal = text[start:end]; value = json.loads(literal)
            if not isinstance(value, str) or literal != json.dumps(value): raise ValueError("noncanonical string")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProtocolRefusal("fleet_update_transport_registry_invalid", "selected transport pin spans are not uniquely writable") from exc
    return document, output


def planned_transport_registry_bytes(
    raw: bytes, transport_name: str, *, manifest_sha256: str, source_sha: str,
) -> bytes:
    """Derive the exact permitted post-state from one already-observed snapshot."""
    name = validate_identifier(transport_name, "transport")
    _document, spans = _pin_spans(raw, name)
    after = raw.decode("utf-8")
    for field, value in (("source_sha", source_sha), ("manifest_sha256", manifest_sha256)):
        start, end = spans[field]
        after = after[:start] + json.dumps(value) + after[end:]
    return after.encode("utf-8")


def planned_transport_registry_sha256(
    registry_path: Path, transport_name: str, *, manifest_sha256: str, source_sha: str
) -> str:
    """Return the exact byte digest of the only permitted surgical post-state."""
    selected = _real_file(registry_path)
    return _sha256(planned_transport_registry_bytes(
        selected.read_bytes(), transport_name,
        manifest_sha256=manifest_sha256, source_sha=source_sha,
    ))


def verify_transport_pins_post(
    registry_path: Path,
    transport_name: str,
    *,
    manifest_sha256: str,
    source_sha: str,
    expected_registry_sha256: str,
    _fault_hook: Optional[Callable[[str], None]] = None,
) -> Dict[str, object]:
    """Replay the post-rename durability boundary without modifying registry bytes."""

    name = validate_identifier(transport_name, "transport")
    if (
        _SHA256.fullmatch(manifest_sha256) is None
        or _SHA1.fullmatch(source_sha) is None
        or _SHA256.fullmatch(expected_registry_sha256) is None
    ):
        raise ProtocolRefusal(
            "fleet_update_transport_pin_invalid",
            "post-state transport pins or digest are invalid",
        )
    try:
        selected = _real_file(registry_path)
    except ProtocolRefusal as exc:
        raise DurabilityFailure(
            "fleet_update_transport_post_durability_failed",
            "registry post-state durability cannot open the selected registry",
        ) from exc
    descriptor = -1
    parent = -1
    try:
        descriptor = os.open(selected, os.O_RDONLY)
        opened = os.fstat(descriptor)
        current = _identity(selected)
        if current != (opened.st_dev, opened.st_ino):
            raise ProtocolRefusal(
                "fleet_update_transport_registry_drift",
                "registry identity changed while verifying post state",
            )
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        parent = os.open(selected.parent, os.O_RDONLY)
        if _fault_hook is not None:
            _fault_hook("after_parent_open")
        os.fsync(parent)
        if _fault_hook is not None:
            _fault_hook("after_parent_fsync")
        os.close(parent)
        parent = -1
        if _fault_hook is not None:
            _fault_hook("before_readback")
        readback = selected.read_bytes()
        if _identity(selected) != current:
            raise ProtocolRefusal(
                "fleet_update_transport_registry_drift",
                "registry identity changed after post-state durability",
            )
        if _sha256(readback) != expected_registry_sha256:
            raise IntegrityFailure(
                "fleet_update_transport_readback_invalid",
                "registry post-state digest differs from the exact expected bytes",
            )
        document, _spans = _pin_spans(readback, name)
        selected_pins = document["transports"][name]
        if (
            selected_pins.get("manifest_sha256") != manifest_sha256
            or selected_pins.get("source_sha") != source_sha
        ):
            raise IntegrityFailure(
                "fleet_update_transport_readback_invalid",
                "registry post-state pins differ from the exact expected values",
            )
    except (OSError, ProtocolRefusal, IntegrityFailure) as exc:
        raise DurabilityFailure(
            "fleet_update_transport_post_durability_failed",
            "registry post-state durability could not be replayed",
        ) from exc
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise DurabilityFailure(
                    "fleet_update_transport_post_durability_failed",
                    "registry post-state durability descriptor could not be closed",
                ) from exc
        if parent >= 0:
            try:
                os.close(parent)
            except OSError as exc:
                raise DurabilityFailure(
                    "fleet_update_transport_post_durability_failed",
                    "registry post-state durability parent could not be closed",
                ) from exc
    return {
        "registry": str(selected),
        "transport": name,
        "registry_after_sha256": expected_registry_sha256,
        "identity": current,
    }


def rewrite_transport_pins(registry_path: Path, transport_name: str, *, manifest_sha256: str, source_sha: str, expected_registry_sha256: Optional[str] = None, expected_identity: Optional[tuple[int, int]] = None, _fault_hook: Optional[Callable[[str], None]] = None) -> Dict[str, object]:
    """Durably replace exactly the two canonical selected pin literals."""
    selected = _real_file(registry_path); name = validate_identifier(transport_name, "transport")
    if _SHA256.fullmatch(manifest_sha256) is None or _SHA1.fullmatch(source_sha) is None:
        raise ProtocolRefusal("fleet_update_transport_pin_invalid", "replacement pins have invalid digest shapes")
    try:
        before_identity = _identity(selected)
        before = selected.read_bytes()
    except OSError as exc:
        raise ProtocolRefusal(
            "fleet_update_transport_registry_invalid",
            "registry could not be observed before replacement",
        ) from exc
    before_digest = _sha256(before)
    if expected_registry_sha256 is not None and expected_registry_sha256 != before_digest:
        raise ProtocolRefusal("fleet_update_transport_registry_drift", "registry content changed after preview")
    if expected_identity is not None and expected_identity != before_identity:
        raise ProtocolRefusal("fleet_update_transport_registry_drift", "registry inode changed after preview")
    _document, spans = _pin_spans(before, name); text = before.decode("utf-8")
    wanted = {"manifest_sha256": manifest_sha256, "source_sha": source_sha}; after_text = text
    for field in reversed(_PINS):
        start, end = spans[field]; after_text = after_text[:start] + json.dumps(wanted[field]) + after_text[end:]
    after = after_text.encode("utf-8"); descriptor = -1; temporary = ""; replaced = False
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{selected.name}.", dir=selected.parent)
        os.fchmod(descriptor, 0o600); offset = 0
        while offset < len(after):
            written = os.write(descriptor, after[offset:])
            if written <= 0: raise OSError("short write")
            offset += written
        os.fsync(descriptor)
        if _fault_hook is not None: _fault_hook("after_temp_fsync")
        if _fault_hook is not None: _fault_hook("before_replace")
        os.close(descriptor); descriptor = -1
        if _identity(selected) != before_identity or _sha256(selected.read_bytes()) != before_digest:
            raise ProtocolRefusal("fleet_update_transport_registry_drift", "registry changed before replacement")
        os.replace(temporary, selected); temporary = ""; replaced = True
        if _fault_hook is not None:
            _fault_hook("after_replace")
        verify_transport_pins_post(
            selected, name, manifest_sha256=manifest_sha256, source_sha=source_sha,
            expected_registry_sha256=_sha256(after), _fault_hook=_fault_hook,
        )
        readback = selected.read_bytes(); read_document, read_spans = _pin_spans(readback, name)
        if readback != after or any(read_document["transports"][name].get(k) != v for k, v in wanted.items()):
            raise IntegrityFailure("fleet_update_transport_readback_invalid", "registry readback did not match replacement")
        # Exact removal of both values proves prefix/middle/suffix preservation.
        # Spans are decoder character offsets, not UTF-8 byte offsets.
        without_before, without_after = text, after_text
        for field in reversed(_PINS):
            b0, b1 = spans[field]; a0, a1 = read_spans[field]
            without_before = without_before[:b0] + without_before[b1:]
            without_after = without_after[:a0] + without_after[a1:]
        if without_before != without_after:
            raise IntegrityFailure("fleet_update_transport_readback_invalid", "registry replacement changed unrelated bytes")
    except (OSError, ProtocolRefusal, IntegrityFailure) as exc:
        if replaced:
            raise DurabilityFailure(
                "fleet_update_transport_post_durability_failed",
                "registry was replaced but post-state durability did not complete",
            ) from exc
        if isinstance(exc, OSError):
            raise ProtocolRefusal("fleet_update_transport_write_failed", f"registry could not be replaced at {selected}") from exc
        raise
    finally:
        if descriptor >= 0: os.close(descriptor)
        if temporary:
            try: Path(temporary).unlink()
            except FileNotFoundError: pass
    return {"registry": str(selected), "transport": name, "registry_before_sha256": before_digest, "registry_after_sha256": _sha256(after), "changed_pins": list(_PINS)}

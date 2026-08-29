"""Bounded ACP v0 JSON-RPC fixture codec and non-launching local probe."""

from __future__ import annotations

import copy
import json
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional


MAXIMUM_ACP_LINE_BYTES = 1_048_576
MAXIMUM_ACP_DEPTH = 64
_ALLOWED_METHODS = frozenset(
    {"initialize", "session/new", "session/prompt", "session/update", "session/cancel"}
)
_REFERENCE_COMMANDS = (
    ("claude-code-acp", ("claude-code-acp",)),
    ("codex-acp", ("codex-acp",)),
    ("acp-agent", ("acp-agent",)),
)


class ACPRefusal(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ACPMessage:
    category: str
    fields: Mapping[str, Any]
    quarantined: Mapping[str, Any]


class _DuplicateKey(ValueError):
    pass


def _object(pairs: list[tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


class ACPAdapter:
    _KNOWN = frozenset({"jsonrpc", "id", "method", "params", "result", "error"})

    def decode_line(self, line: bytes | str) -> ACPMessage:
        if isinstance(line, str):
            try:
                encoded = line.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ACPRefusal("acp_json_invalid", "ACP line is not UTF-8 JSON") from exc
        elif isinstance(line, bytes):
            encoded = line
        else:
            raise ACPRefusal("acp_json_invalid", "ACP line must be bytes or text")
        if len(encoded) > MAXIMUM_ACP_LINE_BYTES:
            raise ACPRefusal("acp_line_too_large", "ACP line exceeds the v0 byte bound")
        if b"\n" in encoded.rstrip(b"\n") or b"\r" in encoded.rstrip(b"\r\n"):
            raise ACPRefusal("acp_multiline", "ACP transport accepts one JSON object per line")
        try:
            record = json.loads(encoded, object_pairs_hook=_object)
        except _DuplicateKey as exc:
            raise ACPRefusal("acp_duplicate_key", "ACP JSON contains a duplicate object key") from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ACPRefusal("acp_json_invalid", "ACP line is not finite JSON") from exc
        self._bounded(record)
        if not isinstance(record, dict) or record.get("jsonrpc") != "2.0":
            raise ACPRefusal("acp_envelope_invalid", "ACP v0 requires a JSON-RPC 2.0 object")
        category = self._category(record)
        method = record.get("method")
        if category in {"request", "notification"} and method not in _ALLOWED_METHODS:
            raise ACPRefusal("acp_method_unruled", "ACP method is outside the finite v0 allowlist")
        fields = {key: copy.deepcopy(value) for key, value in record.items() if key in self._KNOWN}
        quarantined = {
            key: copy.deepcopy(value) for key, value in record.items() if key not in self._KNOWN
        }
        return ACPMessage(category, fields, quarantined)

    def encode_line(self, message: ACPMessage) -> bytes:
        if not isinstance(message, ACPMessage):
            raise ACPRefusal("acp_message_invalid", "ACP encoder requires a decoded message")
        if set(message.fields) & set(message.quarantined):
            raise ACPRefusal("acp_extension_shadow", "ACP extension cannot shadow a contract field")
        record = copy.deepcopy(dict(message.fields))
        record.update(copy.deepcopy(dict(message.quarantined)))
        self._bounded(record)
        encoded = json.dumps(
            record, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(encoded) > MAXIMUM_ACP_LINE_BYTES:
            raise ACPRefusal("acp_line_too_large", "ACP line exceeds the v0 byte bound")
        self.decode_line(encoded)
        return encoded + b"\n"

    @staticmethod
    def _category(record: Mapping[str, Any]) -> str:
        has_id = "id" in record
        has_method = "method" in record
        has_result = "result" in record
        has_error = "error" in record
        if has_method and not has_result and not has_error:
            if has_id:
                ACPAdapter._request_id(record["id"])
                return "request"
            return "notification"
        if has_id and not has_method and has_result != has_error:
            ACPAdapter._request_id(record["id"])
            return "response"
        raise ACPRefusal("acp_envelope_invalid", "ACP JSON-RPC category is ambiguous")

    @staticmethod
    def _request_id(value: Any) -> None:
        valid_int = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**31 - 1
        valid_string = isinstance(value, str) and 1 <= len(value) <= 128
        if not (valid_int or valid_string):
            raise ACPRefusal("acp_id_invalid", "ACP request id is outside v0 bounds")

    @staticmethod
    def _bounded(value: Any, depth: int = 0) -> None:
        if depth > MAXIMUM_ACP_DEPTH:
            raise ACPRefusal("acp_depth_exceeded", "ACP JSON exceeds the v0 depth bound")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ACPRefusal("acp_json_invalid", "ACP object keys must be strings")
                ACPAdapter._bounded(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                ACPAdapter._bounded(child, depth + 1)
        elif isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ACPRefusal("acp_json_invalid", "ACP JSON must be finite")


def probe_reference_harness(
    *, which: Callable[[str], Optional[str]] = shutil.which
) -> Dict[str, object]:
    for executable, command in _REFERENCE_COMMANDS:
        resolved = which(executable)
        if resolved is not None:
            return {
                "status": "reference_harness_present_unlaunched",
                "executable": resolved,
                "command": list(command),
            }
    return {"status": "reference_harness_absent", "executable": None, "command": None}

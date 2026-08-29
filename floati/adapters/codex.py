"""Bounded, process-free codec for recorded Codex app-server envelopes.

This module deliberately knows only the request, response, and notification
categories recorded by the governing contract. It cannot launch or contact a
worker.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any, Dict, Mapping


MAXIMUM_FRAME_BYTES = 1_048_576
MAXIMUM_NESTING_DEPTH = 64
MAXIMUM_METHOD_CHARACTERS = 256
MAXIMUM_REQUEST_ID = 2**31 - 1


class ContractRefusal(ValueError):
    """A recorded envelope is outside the bounded dark contract."""


@dataclass(frozen=True)
class ContractMessage:
    category: str
    fields: Mapping[str, Any]
    quarantined: Mapping[str, Any]


class CodexContractAdapter:
    """Decode and re-encode envelopes without any worker I/O."""

    _KNOWN = {
        "request": frozenset(("id", "method", "params")),
        "response": frozenset(("id", "result", "error")),
        "notification": frozenset(("method", "params")),
    }

    def decode(self, record: Any) -> ContractMessage:
        self._bounded_json(record)
        if not isinstance(record, dict):
            raise ContractRefusal("envelope must be an object")
        category = self._category(record)
        known = self._KNOWN[category]
        fields = {key: copy.deepcopy(value) for key, value in record.items() if key in known}
        quarantined = {
            key: copy.deepcopy(value) for key, value in record.items() if key not in known
        }
        self._validate(category, fields)
        return ContractMessage(category, fields, quarantined)

    def encode(self, message: ContractMessage) -> Dict[str, Any]:
        if not isinstance(message, ContractMessage) or message.category not in self._KNOWN:
            raise ContractRefusal("message category is not a recorded contract category")
        if set(message.fields) & set(message.quarantined):
            raise ContractRefusal("quarantined fields cannot shadow contract fields")
        result = copy.deepcopy(dict(message.fields))
        result.update(copy.deepcopy(dict(message.quarantined)))
        self._bounded_json(result)
        decoded = self.decode(result)
        if decoded.category != message.category:
            raise ContractRefusal("encoded envelope changed category")
        return result

    @staticmethod
    def _category(record: Mapping[str, Any]) -> str:
        has_id = "id" in record
        has_method = "method" in record
        has_result = "result" in record
        has_error = "error" in record
        if has_id and has_method and not has_result and not has_error:
            return "request"
        if not has_id and has_method and not has_result and not has_error:
            return "notification"
        if has_id and not has_method and has_result != has_error:
            return "response"
        raise ContractRefusal("envelope category is ambiguous or malformed")

    @staticmethod
    def _validate(category: str, fields: Mapping[str, Any]) -> None:
        if category in ("request", "response"):
            identifier = fields.get("id")
            if (
                not isinstance(identifier, int)
                or isinstance(identifier, bool)
                or not 0 <= identifier <= MAXIMUM_REQUEST_ID
            ):
                raise ContractRefusal("request id is outside the recorded integer bound")
        if category in ("request", "notification"):
            method = fields.get("method")
            if not isinstance(method, str) or not 1 <= len(method) <= MAXIMUM_METHOD_CHARACTERS:
                raise ContractRefusal("method is outside the recorded string bound")
            if "params" in fields and fields["params"] is not None and not isinstance(
                fields["params"], dict
            ):
                raise ContractRefusal("params must be absent, null, or an object")
        if category == "response" and "error" in fields:
            error = fields["error"]
            if not isinstance(error, dict) or not {"code", "message"}.issubset(error):
                raise ContractRefusal("error response must contain code and message")
            if set(error) - {"code", "message", "data"}:
                raise ContractRefusal("error response contains an unrecorded field")
            code = error["code"]
            if not isinstance(code, int) or isinstance(code, bool):
                raise ContractRefusal("error code must be an integer")
            if not isinstance(error["message"], str):
                raise ContractRefusal("error message must be a string")

    @staticmethod
    def _bounded_json(value: Any) -> None:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise ContractRefusal("envelope is not finite JSON") from exc
        if len(encoded) > MAXIMUM_FRAME_BYTES:
            raise ContractRefusal("envelope exceeds the recorded one-line bound")
        CodexContractAdapter._check_depth(value, 0)

    @staticmethod
    def _check_depth(value: Any, depth: int) -> None:
        if depth > MAXIMUM_NESTING_DEPTH:
            raise ContractRefusal("envelope nesting exceeds the defensive bound")
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ContractRefusal("JSON object keys must be strings")
                CodexContractAdapter._check_depth(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                CodexContractAdapter._check_depth(child, depth + 1)

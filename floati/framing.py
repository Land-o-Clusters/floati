"""One canonical compact-I-JSON frame shared by every durable ledger."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Mapping, List, Any


FRAME_TERMINATOR = b"\n"


@dataclass(frozen=True)
class FrameError(ValueError):
    code: str
    detail: str
    line_number: int = 0

    def __str__(self) -> str:
        return self.detail


class _DuplicateJSONKey(ValueError):
    pass


def _exact_object(pairs: List[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, member in pairs:
        if key in value:
            raise _DuplicateJSONKey(key)
        value[key] = member
    return value


def encode_frame(record: Mapping[str, object]) -> bytes:
    """Encode one mapping using the protocol's sole canonical wire frame."""

    try:
        payload = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise FrameError("record_not_ijson", str(exc)) from exc
    return payload + FRAME_TERMINATOR


def decode_frames(data: bytes) -> List[Any]:
    """Decode complete canonical frames without applying record semantics."""

    if not isinstance(data, bytes):
        raise FrameError("frame_bytes_required", "framed input must be bytes")
    if data and not data.endswith(FRAME_TERMINATOR):
        raise FrameError("incomplete_frame", "framed input has an incomplete final record")
    decoded: List[Any] = []
    for line_number, raw in enumerate(data.splitlines(), start=1):
        if not raw:
            raise FrameError("blank_frame", "framed input contains a blank record", line_number)
        try:
            decoded.append(json.loads(raw, object_pairs_hook=_exact_object))
        except _DuplicateJSONKey as exc:
            raise FrameError(
                "duplicate_json_key",
                f"framed object repeats member {exc}",
                line_number,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FrameError("malformed_json", str(exc), line_number) from exc
    return decoded

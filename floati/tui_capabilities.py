"""Per-endpoint terminal capability receipts and bounded response parsing."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import time
from dataclasses import dataclass
from typing import Collection, Dict, Mapping, Optional, Sequence, TextIO, Tuple

from .errors import ProtocolRefusal
from .tui_protocol import KITTY_QUERY_IMAGE_ID, kitty_probe_query


CAPABILITY_NAMES = (
    "synchronized_output",
    "sgr_mouse",
    "sgr_pixels",
    "kitty_graphics",
    "kitty_keyboard",
    "rgb",
)
STATES = frozenset({"supported", "unsupported", "unknown"})
STAMPS = frozenset({"MEASURED", "DERIVED", "ESTIMATE"})
MAX_PROBE_BYTES = 4096
MAX_REMAINDER_BYTES = 64

DA1_QUERY = b"\x1b[c"
_QUERIES: Mapping[str, bytes] = {
    "synchronized_output": b"\x1b[?2026$p",
    "sgr_mouse": b"\x1b[?1006$p",
    "sgr_pixels": b"\x1b[?1016$p",
    "kitty_graphics": kitty_probe_query(),
    "kitty_keyboard": b"\x1b[?u",
    "rgb": b"\x1bP+q524742\x1b\\",
}
CAPABILITY_QUERY_BATCH = b"".join(_QUERIES[name] for name in CAPABILITY_NAMES)

_SOURCES: Mapping[str, str] = {
    "synchronized_output": "DECRQM:?2026",
    "sgr_mouse": "DECRQM:?1006",
    "sgr_pixels": "DECRQM:?1016",
    "kitty_graphics": "KITTY:a=q",
    "kitty_keyboard": "KITTY:?u",
    "rgb": "XTGETTCAP:RGB",
}

_DA1 = re.compile(rb"\x1b\[\?[0-9]+(?:;[0-9]+)*c")
_DA2 = re.compile(rb"\x1b\[>[0-9]+(?:;[0-9]+)*c")
_DECRPM = re.compile(rb"\x1b\[\?(2026|1006|1016);([0-4])\$y")
_KITTY_GRAPHICS = re.compile(
    rb"\x1b_G(?:[^;]*,)?i="
    + str(KITTY_QUERY_IMAGE_ID).encode("ascii")
    + rb"(?:,[^;]*)?;[^\x1b]*\x1b\\"
)
_KITTY_KEYBOARD = re.compile(rb"\x1b\[\?[0-9]+u")
_RGB = re.compile(rb"\x1bP[01]\+r524742(?:=[0-9A-Fa-f]*)?\x1b\\")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(payload: Mapping[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class CapabilityFact:
    name: str
    state: str
    stamp: str
    source: str
    evidence_digest: str

    def __post_init__(self) -> None:
        if (
            self.name not in CAPABILITY_NAMES
            or self.state not in STATES
            or self.stamp not in STAMPS
            or not self.source
            or re.fullmatch(r"[0-9a-f]{64}", self.evidence_digest) is None
        ):
            raise ProtocolRefusal(
                "terminal_capability_fact_invalid",
                "capability facts require a ruled name, state, stamp, source, and digest",
            )

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "state": self.state,
            "stamp": self.stamp,
            "source": self.source,
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True)
class TerminalCapabilityReceipt:
    schema_version: int
    endpoint_id: str
    endpoint_kind: str
    endpoint_stamp: str
    facts: Tuple[CapabilityFact, ...]
    receipt_digest: str

    @classmethod
    def create(
        cls,
        *,
        endpoint_id: str,
        endpoint_kind: str,
        endpoint_stamp: str,
        facts: Sequence[CapabilityFact],
    ) -> "TerminalCapabilityReceipt":
        selected = tuple(facts)
        if tuple(fact.name for fact in selected) != CAPABILITY_NAMES:
            raise ProtocolRefusal(
                "terminal_capability_receipt_invalid",
                "capability receipt facts must be complete, unique, and ordered",
            )
        base: Dict[str, object] = {
            "schema_version": 0,
            "kind": "terminal_capability_receipt",
            "endpoint_id": endpoint_id,
            "endpoint_kind": endpoint_kind,
            "endpoint_stamp": endpoint_stamp,
            "facts": [fact.to_dict() for fact in selected],
        }
        return cls(
            schema_version=0,
            endpoint_id=endpoint_id,
            endpoint_kind=endpoint_kind,
            endpoint_stamp=endpoint_stamp,
            facts=selected,
            receipt_digest=_sha256(_canonical(base)),
        )

    def __post_init__(self) -> None:
        expected_digest = _sha256(_canonical(self._base_dict()))
        if (
            self.schema_version != 0
            or isinstance(self.schema_version, bool)
            or not self.endpoint_id
            or self.endpoint_kind not in {"terminal", "tmux", "unknown"}
            or self.endpoint_stamp not in STAMPS
            or tuple(fact.name for fact in self.facts) != CAPABILITY_NAMES
            or re.fullmatch(r"[0-9a-f]{64}", self.receipt_digest) is None
            or self.receipt_digest != expected_digest
        ):
            raise ProtocolRefusal(
                "terminal_capability_receipt_invalid",
                "terminal capability receipt is malformed",
            )

    def _base_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "kind": "terminal_capability_receipt",
            "endpoint_id": self.endpoint_id,
            "endpoint_kind": self.endpoint_kind,
            "endpoint_stamp": self.endpoint_stamp,
            "facts": [fact.to_dict() for fact in self.facts],
        }

    def to_dict(self) -> Dict[str, object]:
        return dict(self._base_dict(), receipt_digest=self.receipt_digest)

    def to_json(self) -> str:
        return _canonical(self.to_dict()).decode("utf-8") + "\n"

    def enabled(self, name: str) -> bool:
        return any(
            fact.name == name
            and fact.state == "supported"
            and fact.stamp == "MEASURED"
            for fact in self.facts
        )


def parse_capability_responses(data: bytes) -> tuple[Dict[str, bytes], bytes]:
    """Extract complete recognized replies while preserving unrelated bytes."""

    if not isinstance(data, bytes):
        raise ProtocolRefusal(
            "terminal_probe_bytes_invalid", "terminal probe input must be bytes"
        )
    if len(data) > MAX_PROBE_BYTES:
        return {}, data[-MAX_REMAINDER_BYTES:]

    frames: list[tuple[int, int, str, bytes]] = []
    for match in _DA1.finditer(data):
        frames.append((match.start(), match.end(), "da1", match.group(0)))
    for match in _DA2.finditer(data):
        frames.append((match.start(), match.end(), "da2", match.group(0)))
    for match in _DECRPM.finditer(data):
        name = {
            b"2026": "synchronized_output",
            b"1006": "sgr_mouse",
            b"1016": "sgr_pixels",
        }[match.group(1)]
        frames.append((match.start(), match.end(), name, match.group(0)))
    for pattern, name in (
        (_KITTY_GRAPHICS, "kitty_graphics"),
        (_KITTY_KEYBOARD, "kitty_keyboard"),
        (_RGB, "rgb"),
    ):
        for match in pattern.finditer(data):
            frames.append((match.start(), match.end(), name, match.group(0)))
    frames.sort(key=lambda item: (item[0], item[1]))

    responses: Dict[str, bytes] = {}
    remainder = bytearray()
    cursor = 0
    for start, end, name, frame in frames:
        if start < cursor:
            continue
        remainder.extend(data[cursor:start])
        responses[name] = responses.get(name, b"") + frame
        cursor = end
    remainder.extend(data[cursor:])
    return responses, bytes(remainder)


def _endpoint(
    responses: Mapping[str, bytes], environment: Mapping[str, str]
) -> tuple[str, str, str]:
    tmux = environment.get("TMUX", "")
    if tmux:
        return "tmux", "ESTIMATE", _sha256(b"tmux\0" + tmux.encode("utf-8"))
    da2 = responses.get("da2")
    if da2 is not None and _DA2.fullmatch(da2) is not None:
        return "terminal", "MEASURED", _sha256(da2)
    term = environment.get("TERM", "")
    colorterm = environment.get("COLORTERM", "")
    if term or colorterm:
        testimony = ("TERM=" + term + "\0COLORTERM=" + colorterm).encode("utf-8")
        return "terminal", "DERIVED", _sha256(testimony)
    brand_values = tuple(
        environment.get(name, "")
        for name in ("TERM_PROGRAM", "KITTY_WINDOW_ID", "WEZTERM_EXECUTABLE")
        if environment.get(name, "")
    )
    if brand_values:
        return "terminal", "ESTIMATE", _sha256(
            b"brand\0" + b"\0".join(value.encode("utf-8") for value in brand_values)
        )
    return "unknown", "ESTIMATE", _sha256(b"endpoint:unknown")


def _measured_state(name: str, response: bytes) -> str:
    if name in {"synchronized_output", "sgr_mouse", "sgr_pixels"}:
        match = _DECRPM.fullmatch(response)
        return (
            "unknown"
            if match is None
            else "unsupported"
            if match.group(2) == b"0"
            else "supported"
        )
    if name == "kitty_graphics":
        match = _KITTY_GRAPHICS.fullmatch(response)
        if match is None:
            return "unknown"
        return "supported" if b";OK\x1b\\" in response else "unsupported"
    if name == "kitty_keyboard":
        return "supported" if _KITTY_KEYBOARD.fullmatch(response) else "unknown"
    if name == "rgb":
        match = _RGB.fullmatch(response)
        if match is None:
            return "unknown"
        return "supported" if response.startswith(b"\x1bP1+r") else "unsupported"
    return "unknown"


def _timeout_testimony(
    environment: Mapping[str, str],
) -> tuple[str, str, bytes]:
    brand = tuple(
        (name, environment.get(name, ""))
        for name in ("TMUX", "TERM_PROGRAM", "KITTY_WINDOW_ID", "WEZTERM_EXECUTABLE")
        if environment.get(name, "")
    )
    if brand:
        encoded = b"\0".join(
            (name + "=" + value).encode("utf-8") for name, value in brand
        )
        return "ESTIMATE", "brand_environment_after_timeout", encoded
    derived = tuple(
        (name, environment.get(name, ""))
        for name in ("TERM", "COLORTERM")
        if environment.get(name, "")
    )
    if derived:
        encoded = b"\0".join(
            (name + "=" + value).encode("utf-8") for name, value in derived
        )
        return "DERIVED", "terminal_environment_after_timeout", encoded
    return "MEASURED", "probe_timeout", b"probe_timeout"


def receipt_from_responses(
    responses: Mapping[str, bytes],
    *,
    timed_out: Collection[str],
    environment: Mapping[str, str],
) -> TerminalCapabilityReceipt:
    timeout_names = frozenset(timed_out)
    if not timeout_names.issubset(CAPABILITY_NAMES):
        raise ProtocolRefusal(
            "terminal_capability_timeout_invalid",
            "timed-out capability names must be ruled",
        )
    facts = []
    for name in CAPABILITY_NAMES:
        response = responses.get(name)
        if response is not None:
            state = _measured_state(name, response)
            stamp = "MEASURED"
            source = _SOURCES[name] if state != "unknown" else "probe_response_malformed"
            evidence = response
        elif name in timeout_names:
            state = "unknown"
            stamp, source, prior = _timeout_testimony(environment)
            evidence = _QUERIES[name] + b"\0timeout\0" + prior
        else:
            state = "unknown"
            stamp = "MEASURED"
            source = "probe_response_absent"
            evidence = _QUERIES[name] + b"\0absent"
        facts.append(
            CapabilityFact(
                name=name,
                state=state,
                stamp=stamp,
                source=source,
                evidence_digest=_sha256(evidence),
            )
        )
    endpoint_kind, endpoint_stamp, endpoint_id = _endpoint(responses, environment)
    return TerminalCapabilityReceipt.create(
        endpoint_id=endpoint_id,
        endpoint_kind=endpoint_kind,
        endpoint_stamp=endpoint_stamp,
        facts=facts,
    )


def _unknown_receipt(
    source: str, environment: Mapping[str, str]
) -> TerminalCapabilityReceipt:
    facts = tuple(
        CapabilityFact(
            name=name,
            state="unknown",
            stamp="MEASURED",
            source=source,
            evidence_digest=_sha256(_QUERIES[name] + b"\0" + source.encode("ascii")),
        )
        for name in CAPABILITY_NAMES
    )
    endpoint_kind, endpoint_stamp, endpoint_id = _endpoint({}, environment)
    return TerminalCapabilityReceipt.create(
        endpoint_id=endpoint_id,
        endpoint_kind=endpoint_kind,
        endpoint_stamp=endpoint_stamp,
        facts=facts,
    )


def probe_terminal_capabilities(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    timeout: float = 0.04,
    environment: Optional[Mapping[str, str]] = None,
    preloaded_response: Optional[bytes] = None,
) -> tuple[TerminalCapabilityReceipt, bytes]:
    """Measure one direct terminal endpoint behind a DA1 response barrier."""

    selected_environment: Mapping[str, str] = (
        os.environ if environment is None else environment
    )
    if not bool(getattr(output_stream, "isatty", lambda: False)()):
        return _unknown_receipt("stdout_not_tty", selected_environment), b""
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not 0 < float(timeout) <= 1.0
    ):
        raise ProtocolRefusal(
            "terminal_probe_timeout_invalid",
            "terminal capability timeout must be greater than zero and at most one second",
        )
    if preloaded_response is not None and not isinstance(preloaded_response, bytes):
        raise ProtocolRefusal(
            "terminal_probe_bytes_invalid", "preloaded terminal response must be bytes"
        )

    output_stream.write(DA1_QUERY.decode("ascii"))
    output_stream.flush()
    deadline = time.monotonic() + float(timeout)
    collected = b"" if preloaded_response is None else preloaded_response

    def read_available() -> bool:
        nonlocal collected
        if preloaded_response is not None:
            return False
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            return False
        descriptor = input_stream.fileno()
        readable, _, _ = select.select([descriptor], [], [], remaining)
        if not readable:
            return False
        collected += os.read(descriptor, 256)
        return True

    responses, remainder = parse_capability_responses(collected)
    while "da1" not in responses and len(collected) <= MAX_PROBE_BYTES:
        if not read_available():
            break
        responses, remainder = parse_capability_responses(collected)
    if "da1" not in responses:
        return (
            receipt_from_responses(
                {key: value for key, value in responses.items() if key == "da2"},
                timed_out=CAPABILITY_NAMES,
                environment=selected_environment,
            ),
            remainder,
        )

    output_stream.write(CAPABILITY_QUERY_BATCH.decode("ascii"))
    output_stream.flush()
    while not set(CAPABILITY_NAMES).issubset(responses):
        if len(collected) > MAX_PROBE_BYTES or not read_available():
            break
        responses, remainder = parse_capability_responses(collected)
    timed_out = tuple(name for name in CAPABILITY_NAMES if name not in responses)
    return (
        receipt_from_responses(
            responses,
            timed_out=timed_out,
            environment=selected_environment,
        ),
        remainder,
    )

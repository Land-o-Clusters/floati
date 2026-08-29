"""Dependency-free terminal protocol primitives for full-screen Floati views."""

from __future__ import annotations

import base64
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Tuple, Union


KITTY_IMAGE_ID = 7109
KITTY_QUERY_IMAGE_ID = 7108
MAX_TERMINAL_EVENT_BYTES = 64
MAX_MOUSE_COORDINATE = 32767


@dataclass(frozen=True)
class MouseEvent:
    button: int
    column: int
    row: int
    pressed: bool


TerminalInput = Union[str, MouseEvent]


_SGR_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
_KITTY_KEY = re.compile(rb"\x1b\[(\d+)(?:;([0-9:]+))?u")
_KITTY_ARROW = re.compile(rb"\x1b\[(?:1(?:;[0-9:]+)?)?([AB])")
_KITTY_OK = re.compile(
    rb"\x1b_G(?:[^;]*,)?i=" + str(KITTY_QUERY_IMAGE_ID).encode("ascii") + rb"(?:,[^;]*)?;OK\x1b\\"
)
_KITTY_RESPONSE = re.compile(
    rb"\x1b_G(?:[^;]*,)?i="
    + str(KITTY_QUERY_IMAGE_ID).encode("ascii")
    + rb"(?:,[^;]*)?;[^\x1b]*\x1b\\"
)


def _is_partial_sgr_mouse(raw: bytes) -> bool:
    payload = raw[3:]
    if any(byte != ord(";") and not ord("0") <= byte <= ord("9") for byte in payload):
        return False
    parts = payload.split(b";")
    if len(parts) > 3:
        return False
    return all(part for part in parts[:-1])


class TerminalInputDecoder:
    """Frame terminal events independently of arbitrary OS read boundaries."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Tuple[TerminalInput, ...]:
        self._buffer.extend(data)
        events = []
        while self._buffer:
            raw = bytes(self._buffer)
            if raw.startswith(b"\x1b[<"):
                terminators = [
                    position
                    for marker in (b"M", b"m")
                    for position in [raw.find(marker, 3)]
                    if position >= 0
                ]
                if not terminators:
                    if not _is_partial_sgr_mouse(raw):
                        self._buffer.clear()
                        break
                    if len(raw) > MAX_TERMINAL_EVENT_BYTES:
                        self._buffer.clear()
                    break
                frame_end = min(terminators) + 1
                frame = raw[:frame_end]
                del self._buffer[:frame_end]
                if len(frame) > MAX_TERMINAL_EVENT_BYTES:
                    continue
                match = _SGR_MOUSE.fullmatch(frame)
                if match is None:
                    continue
                values = tuple(int(match.group(index)) for index in (1, 2, 3))
                if (
                    values[0] > 255
                    or not 1 <= values[1] <= MAX_MOUSE_COORDINATE
                    or not 1 <= values[2] <= MAX_MOUSE_COORDINATE
                ):
                    continue
                events.append(
                    MouseEvent(
                        button=values[0],
                        column=values[1],
                        row=values[2],
                        pressed=match.group(4) == b"M",
                    )
                )
                continue
            if raw == b"\x1b":
                break
            if raw.startswith(b"\x1b["):
                if len(raw) < 3:
                    break
                final = next(
                    (
                        index
                        for index, byte in enumerate(raw[2:], start=2)
                        if 0x40 <= byte <= 0x7E
                    ),
                    None,
                )
                if final is None:
                    if len(raw) > MAX_TERMINAL_EVENT_BYTES:
                        self._buffer.clear()
                    break
                sequence = raw[: final + 1]
                del self._buffer[: final + 1]
                events.append(decode_terminal_input(sequence))
                continue
            lead = raw[0]
            size = 1
            if lead >= 0xF0:
                size = 4
            elif lead >= 0xE0:
                size = 3
            elif lead >= 0xC0:
                size = 2
            if len(raw) < size:
                break
            sequence = raw[:size]
            del self._buffer[:size]
            events.append(decode_terminal_input(sequence))
        return tuple(events)


def decode_terminal_input(data: bytes) -> TerminalInput:
    match = _SGR_MOUSE.fullmatch(data)
    if match is not None:
        return MouseEvent(
            button=int(match.group(1)),
            column=int(match.group(2)),
            row=int(match.group(3)),
            pressed=match.group(4) == b"M",
        )
    kitty_arrow = _KITTY_ARROW.fullmatch(data)
    if kitty_arrow is not None:
        return {b"A": "KEY_UP", b"B": "KEY_DOWN"}[kitty_arrow.group(1)]
    kitty_key = _KITTY_KEY.fullmatch(data)
    if kitty_key is not None:
        codepoint = int(kitty_key.group(1))
        if codepoint == 27:
            return "\x1b"
        if codepoint == 13:
            return "ENTER"
    text = data.decode("utf-8", errors="ignore")
    return {
        "\x1b[A": "KEY_UP",
        "\x1b[B": "KEY_DOWN",
        "\r": "ENTER",
        "\n": "ENTER",
    }.get(text, text)


def synchronized_output_frame(frame: str, *, image: bytes = b"") -> bytes:
    payload = b"\x1b[?2026h\x1b[H" + frame.encode("utf-8") + b"\x1b[J"
    if image:
        payload += b"\x1b[H" + image
    return payload + b"\x1b[?2026l"


def mouse_tracking(enabled: bool) -> bytes:
    suffix = b"h" if enabled else b"l"
    return b"\x1b[?1000" + suffix + b"\x1b[?1006" + suffix


def kitty_keyboard_mode(enabled: bool) -> bytes:
    """Push disambiguated keys for this screen, or pop the prior flags."""

    return b"\x1b[>1u" if enabled else b"\x1b[<u"


def kitty_graphics_supported(response: bytes) -> bool:
    return _KITTY_OK.search(response) is not None


def split_kitty_response(data: bytes) -> Tuple[bytes, bytes]:
    match = _KITTY_RESPONSE.search(data)
    if match is None:
        return b"", data
    return match.group(0), data[: match.start()] + data[match.end() :]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def _buoy_png() -> bytes:
    width = height = 16
    orange = (232, 98, 44, 255)
    dark = (18, 22, 28, 255)
    clear = (0, 0, 0, 0)
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            distance = (x - 7.5) ** 2 + (y - 8.5) ** 2
            ring = 18 <= distance <= 42
            mast = 7 <= x <= 8 and 1 <= y <= 7
            water = 3 <= x <= 12 and y in {13, 15}
            pixel = orange if ring or mast or water else dark if distance < 18 else clear
            row.extend(pixel)
        rows.append(bytes(row))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )


def kitty_buoy_image() -> bytes:
    encoded = base64.b64encode(_buoy_png())
    controls = f"a=T,f=100,t=d,q=2,i={KITTY_IMAGE_ID},s=16,v=16,c=2,r=1".encode("ascii")
    return b"\x1b_G" + controls + b";" + encoded + b"\x1b\\"


def kitty_probe_query() -> bytes:
    controls = f"a=q,f=32,t=d,q=2,i={KITTY_QUERY_IMAGE_ID},s=1,v=1".encode("ascii")
    return b"\x1b_G" + controls + b";AAAAAA==\x1b\\"


def kitty_delete_image() -> bytes:
    return f"\x1b_Ga=d,d=I,q=2,i={KITTY_IMAGE_ID}\x1b\\".encode("ascii")

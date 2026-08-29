"""Stdlib-only Kitty activity images gated by receipted capabilities."""

from __future__ import annotations

import base64
import hashlib
import struct
import zlib
from dataclasses import dataclass
from typing import Mapping, Sequence, Tuple

from .tui_activity import ActivitySamples, _samples
from .tui_capabilities import TerminalCapabilityReceipt


FLOATI_ORANGE = (232, 98, 44, 255)
FLOATI_DARK = (18, 22, 28, 255)
FLOATI_CLEAR = (0, 0, 0, 0)
ACTIVITY_PNG_WIDTH = 20
ACTIVITY_PNG_HEIGHT = 8
MAX_ACTIVITY_PNG_BYTES = 1024
MAX_ACTIVITY_OVERLAYS = 128


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return (
        struct.pack(">I", len(payload))
        + body
        + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
    )


def activity_strip_png(samples: Sequence[int]) -> bytes:
    selected = _samples(samples)
    peak = max(selected)
    heights = tuple(
        1 if peak == 0 else 1 + round(value * (ACTIVITY_PNG_HEIGHT - 1) / peak)
        for value in selected
    )
    rows = []
    for y in range(ACTIVITY_PNG_HEIGHT):
        row = bytearray((0,))
        for x in range(ACTIVITY_PNG_WIDTH):
            bucket = x // 4
            height = heights[bucket]
            active = y >= ACTIVITY_PNG_HEIGHT - height
            pixel = FLOATI_ORANGE if active else FLOATI_DARK
            row.extend(pixel)
        rows.append(bytes(row))
    header = struct.pack(
        ">IIBBBBB",
        ACTIVITY_PNG_WIDTH,
        ACTIVITY_PNG_HEIGHT,
        8,
        6,
        0,
        0,
        0,
    )
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), level=9))
        + _png_chunk(b"IEND", b"")
    )
    if len(payload) > MAX_ACTIVITY_PNG_BYTES:
        raise AssertionError("bounded activity PNG exceeded its fixed limit")
    return payload


def graphics_allowed(
    capability_receipt: object,
    *,
    color_tier: str,
) -> bool:
    return (
        isinstance(capability_receipt, TerminalCapabilityReceipt)
        and color_tier in {"16", "256"}
        and capability_receipt.enabled("kitty_graphics")
    )


def stable_activity_image_id(target_id: str) -> int:
    digest = hashlib.sha256(b"floati:r3-activity\0" + target_id.encode("utf-8")).digest()
    return 10_000 + int.from_bytes(digest[:4], "big") % 2_000_000_000


@dataclass(frozen=True)
class ActivityOverlay:
    target_id: str
    image_id: int
    row: int
    column: int
    samples: ActivitySamples


@dataclass(frozen=True)
class ActivityOverlayPlan:
    overlays: Tuple[ActivityOverlay, ...]
    delete_ids: Tuple[int, ...]
    payload: bytes


def kitty_delete_images(image_ids: Sequence[int]) -> bytes:
    return b"".join(
        f"\x1b_Ga=d,d=I,q=2,i={image_id}\x1b\\".encode("ascii")
        for image_id in image_ids
    )


def _transmit(overlay: ActivityOverlay) -> bytes:
    encoded = base64.b64encode(activity_strip_png(overlay.samples))
    controls = (
        f"a=T,f=100,t=d,q=2,i={overlay.image_id},"
        f"s={ACTIVITY_PNG_WIDTH},v={ACTIVITY_PNG_HEIGHT},c=5,r=1"
    ).encode("ascii")
    cursor = f"\x1b[{overlay.row};{overlay.column}H".encode("ascii")
    return cursor + b"\x1b_G" + controls + b";" + encoded + b"\x1b\\"


def plan_activity_overlays(
    *,
    activity_by_target: Mapping[str, Sequence[int]],
    visible_positions: Mapping[str, tuple[int, int]],
    capability_receipt: object,
    color_tier: str,
    previous: Sequence[ActivityOverlay] = (),
) -> ActivityOverlayPlan:
    prior = {overlay.target_id: overlay for overlay in previous}
    overlays = []
    if graphics_allowed(capability_receipt, color_tier=color_tier):
        used_ids: set[int] = set()
        for target_id in sorted(set(activity_by_target) & set(visible_positions))[
            :MAX_ACTIVITY_OVERLAYS
        ]:
            row, column = visible_positions[target_id]
            if (
                not isinstance(row, int)
                or isinstance(row, bool)
                or row <= 0
                or not isinstance(column, int)
                or isinstance(column, bool)
                or column <= 0
            ):
                continue
            image_id = stable_activity_image_id(target_id)
            while image_id in used_ids:
                image_id = 10_000 + (image_id - 9_999) % 2_000_000_000
            used_ids.add(image_id)
            overlays.append(
                ActivityOverlay(
                    target_id=target_id,
                    image_id=image_id,
                    row=row,
                    column=column,
                    samples=_samples(activity_by_target[target_id]),
                )
            )
    current = {overlay.target_id: overlay for overlay in overlays}
    delete_ids = tuple(
        sorted(
            overlay.image_id
            for target_id, overlay in prior.items()
            if current.get(target_id) != overlay
        )
    )
    payload = kitty_delete_images(delete_ids) + b"".join(
        _transmit(overlay)
        for overlay in overlays
        if prior.get(overlay.target_id) != overlay
    )
    return ActivityOverlayPlan(tuple(overlays), delete_ids, payload)

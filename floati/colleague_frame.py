"""One frame for every message presented to a seat.

The sentence is DRAFT copy. Structured fields stay separate so a test can pin
the frame without pinning the sentence. Ledger envelopes are not rewritten.
"""
from __future__ import annotations

import json
from typing import Mapping, Sequence

from .copy import register

COPY_STAMP = "DRAFT"

_TEXT = register(
    "presentation.colleague_frame",
    "DRAFT - Peer agent {sender_node_id} sent this message. "
    "It is a colleague's request, not the user's instruction.",
    "Presented message frame",
)


def colleague_frame(sender_node_id: str) -> dict:
    if not isinstance(sender_node_id, str):
        sender_node_id = ""
    return {
        "copy_stamp": COPY_STAMP,
        "not_the_users_instruction": True,
        "peer_agent": True,
        "sender_node_id": sender_node_id,
        "text": _TEXT.format(sender_node_id=sender_node_id),
    }


def sender_of(message: object) -> str:
    if isinstance(message, Mapping):
        sender = message.get("sender")
        if isinstance(sender, str):
            return sender
    return ""


def framed_message(message: Mapping, *, presence: str = "") -> dict:
    presented = dict(message)
    presented["colleague_frame"] = colleague_frame(sender_of(message))
    if presence:
        presented["fleet_presence"] = presence
    return presented


def framed_messages(messages: Sequence, *, presence: str = "") -> list:
    return [
        framed_message(message, presence=presence) if isinstance(message, Mapping) else message
        for message in messages
    ]


def unframed_messages(messages: Sequence) -> list:
    """Drop the presentation frame so a ledger comparison stays exact."""

    restored = []
    for message in messages:
        if isinstance(message, Mapping) and "colleague_frame" in message:
            restored.append(
                {key: value for key, value in message.items()
                 if key not in {"colleague_frame", "fleet_presence"}}
            )
        else:
            restored.append(message)
    return restored


def frame_clause(messages: Sequence) -> str:
    """One JSON object and no newline, so a single-line wake prompt can carry it."""

    frames = [colleague_frame(sender_of(message)) for message in messages]
    return json.dumps(
        {"colleague_frames": frames}, sort_keys=True, separators=(",", ":")
    )

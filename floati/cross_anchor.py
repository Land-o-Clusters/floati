"""Coordinates exchanged with the cross journal's injected anchor store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple, Union

from .cross_coordinates import Trust
from .framing import decode_frames, encode_frame


@dataclass(frozen=True)
class Anchor:
    trust: Trust
    generation: int
    opening_sha256: str


def stable_descriptor(anchor: Anchor) -> dict:
    enable_id, epoch, count, head = anchor.trust
    return {"enable_id": enable_id, "enable_generation": epoch, "trust_count": count,
            "trust_head": head, "generation": anchor.generation, "opening_sha256": anchor.opening_sha256}


def sealed_descriptor(position: tuple, checkpoint_sha256: str) -> dict:
    _, journal_id, generation, sequence, journal_head, length = position
    return {"journal_id": journal_id, "generation": generation, "through_seq": sequence,
            "head_sha256": journal_head, "byte_length": length, "checkpoint_sha256": checkpoint_sha256}


@dataclass(frozen=True)
class EnableCreate:
    initial: Anchor
    predecessor: Tuple[object, ...]
    predecessor_checkpoint: str
    token: str
    approval_sha256: str

    def encoded(self) -> bytes:
        return encode_frame({
            "format": "floati-cross-create-v1", "initial": stable_descriptor(self.initial),
            "predecessor": sealed_descriptor(self.predecessor, self.predecessor_checkpoint),
            "token": self.token, "approval_sha256": self.approval_sha256,
        })[:-1]


def successor_descriptor(position: tuple, journal_sha256: str, quota_sha256: str) -> dict:
    _, journal_id, generation, sequence, journal_head, length = position
    return {"journal_id": journal_id, "generation": generation, "through_seq": sequence,
            "head_sha256": journal_head, "byte_length": length, "journal_sha256": journal_sha256,
            "quota_sha256": quota_sha256}


@dataclass(frozen=True)
class QuotaMigration:
    predecessor: Tuple[object, ...]
    predecessor_checkpoint: str
    successor: Tuple[object, ...]
    successor_sha256: str
    quota_sha256: str
    token: str

    def encoded(self) -> bytes:
        return encode_frame({
            "format": "floati-cross-quota-migration-v1", "token": self.token,
            "predecessor": sealed_descriptor(self.predecessor, self.predecessor_checkpoint),
            "successor": successor_descriptor(self.successor, self.successor_sha256, self.quota_sha256),
        })[:-1]


@dataclass(frozen=True)
class QuotaMigrationPending:
    """The independent helper still owns this token's booked slot."""

    token: str


@dataclass(frozen=True)
class Handoff:
    prior: Anchor
    predecessor: Tuple[object, ...]
    predecessor_checkpoint: str
    intent_checkpoint: str
    successor: Tuple[object, ...]
    successor_sha256: str
    reason: str
    quota_sha256: Optional[str] = None


@dataclass(frozen=True)
class Narrowing:
    prior: Anchor
    predecessor: Tuple[object, ...]
    predecessor_checkpoint: str
    body_frame: bytes
    original: Optional["Narrowing"] = None

    def body(self) -> dict:
        return decode_frames(self.body_frame)[0]

    def encoded(self) -> bytes:
        enable_id, epoch, count, head = self.prior.trust
        _, journal_id, generation, sequence, journal_head, length = self.predecessor
        frame = {
            "format": "floati-cross-narrow-v1",
            "prior": {
                "enable_id": enable_id, "enable_generation": epoch, "trust_count": count,
                "trust_head": head, "generation": self.prior.generation, "opening_sha256": self.prior.opening_sha256,
            },
            "predecessor": {
                "journal_id": journal_id, "generation": generation, "through_seq": sequence,
                "head_sha256": journal_head, "byte_length": length, "checkpoint_sha256": self.predecessor_checkpoint,
            },
            "body": self.body(),
        }
        if self.original is not None:
            frame = {"format": "floati-cross-rebased-narrow-v1", "current": frame,
                     "original": decode_frames(self.original.encoded() + b"\n")[0]}
        return encode_frame(frame)[:-1]


@dataclass(frozen=True)
class NarrowingHandoff:
    narrowing: Narrowing
    handoff: Handoff


@dataclass(frozen=True)
class OutstandingWrite:
    """The backend cannot yet exclude a later effect of a prior request."""


StoreValue = Union[Anchor, EnableCreate, Handoff, Narrowing, NarrowingHandoff]


class AnchorStore(Protocol):
    def read(self) -> Optional[Union[StoreValue, OutstandingWrite]]:
        """Observe exact state or an outstanding backend writer. None proves absence."""

    def compare_and_set(self, expected: Optional[StoreValue], successor: Optional[StoreValue]) -> bool:
        """Replace only the expected value. Report success only on a confirmed write."""

"""Explicit review-handoff lifecycle for the private Locks package."""

from __future__ import annotations

from typing import Callable

from ..errors import ProtocolRefusal
from .ledger import LockLedger


_UNRESOLVED_IDENTITIES = frozenset({"unknown", "default"})


class ReviewHandoffController:
    def __init__(self, ledger: LockLedger, identity_resolver: Callable[[str], bool]) -> None:
        if type(ledger) is not LockLedger or not callable(identity_resolver):
            raise ProtocolRefusal(
                "handoff_controller_invalid",
                "review handoffs require one exact Locks ledger and identity resolver",
            )
        self._ledger = ledger
        self._identity_resolver = identity_resolver

    def queue(
        self,
        *,
        handoff_id: object,
        recipient: object,
        car_id: object,
        ref: object,
        base_ref: object,
        witness: object,
        rearm_event: object,
        now: object,
    ) -> dict[str, object]:
        if (
            type(recipient) is not str
            or not recipient
            or recipient.casefold() in _UNRESOLVED_IDENTITIES
        ):
            raise ProtocolRefusal(
                "handoff_recipient_unresolved",
                "review handoff requires one exact registered recipient identity",
            )
        try:
            resolved = self._identity_resolver(recipient)
        except Exception as exc:
            raise ProtocolRefusal(
                "handoff_recipient_unresolved",
                "review handoff recipient identity resolution failed",
            ) from exc
        if resolved is not True:
            raise ProtocolRefusal(
                "handoff_recipient_unresolved",
                "review handoff recipient identity is not registered",
            )
        return self._ledger.queue_review_handoff(
            handoff_id=handoff_id,
            recipient=recipient,
            car_id=car_id,
            ref=ref,
            base_ref=base_ref,
            witness=witness,
            rearm_event=rearm_event,
            now=now,
        )

    def stop(
        self,
        handoff_id: object,
        *,
        attempts: object,
        stopped_at: object,
        rearm_event: object,
    ) -> dict[str, object]:
        return self._ledger.stop_review_handoff(
            handoff_id,
            attempts=attempts,
            stopped_at=stopped_at,
            rearm_event=rearm_event,
        )

    def rearm(
        self,
        handoff_id: object,
        *,
        observed_event: object,
        now: object,
    ) -> dict[str, object]:
        return self._ledger.rearm_review_handoff(
            handoff_id,
            observed_event=observed_event,
            now=now,
        )

    def record_delivered(
        self,
        handoff_id: object,
        *,
        receipt_id: object,
        receipt_digest: object,
        now: object,
    ) -> dict[str, object]:
        return self._ledger.record_review_handoff_delivery(
            handoff_id,
            receipt_id=receipt_id,
            receipt_digest=receipt_digest,
            now=now,
        )

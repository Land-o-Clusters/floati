"""Sole typed append surface for the private Locks ledger."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Callable, Optional

from ..errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from ..framing import FrameError, decode_frames, encode_frame
from ..ids import uuid7_hex
from ..jsonl import MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES, _append_frame, _locked_path
from ..root import FloatiRoot
from .contracts import (
    LOCK_KINDS,
    validate_full_ref,
    validate_lock_record,
    validate_timestamp,
    validate_witness,
)
from .projection import LockProjection, LockSnapshot


_RECORDS_PATH = Path("locks/records.jsonl")


class LockLedger:
    def __init__(self, root: FloatiRoot) -> None:
        if type(root) is not FloatiRoot:
            raise ProtocolRefusal("root_required", "Locks ledger requires one exact writable FloatiRoot")
        self.root = root
        self._path = root.resolve_relative(_RECORDS_PATH)
        self._lock_path = root.resolve_relative(Path("locks/records.lock"))

    def _read_unlocked(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        try:
            stat = self._path.stat()
            data = self._path.read_bytes()
        except OSError as exc:
            raise DurabilityFailure("storage_unavailable", f"records.jsonl: {exc}") from exc
        if stat.st_size > MAX_LEDGER_BYTES:
            raise IntegrityFailure("ledger_too_large", "Locks ledger exceeds its byte bound")
        try:
            decoded = decode_frames(data)
        except FrameError as exc:
            raise IntegrityFailure(exc.code, exc.detail) from exc
        if len(decoded) > MAX_LEDGER_RECORDS:
            raise IntegrityFailure("ledger_record_limit", "Locks ledger exceeds its row bound")
        records: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw in decoded:
            row = validate_lock_record(raw, self.root.tenant_id, integrity=True)
            record_id = str(row["id"])
            if record_id in seen:
                raise IntegrityFailure("duplicate_record_id", "Locks ledger repeats a record id")
            seen.add(record_id)
            records.append(row)
        LockProjection.from_records(records, self.root.tenant_id, integrity=True)
        return records

    def records(self) -> list[dict[str, object]]:
        with _locked_path(self._lock_path, exclusive=False):
            return deepcopy(self._read_unlocked())

    def snapshot(self) -> LockSnapshot:
        with _locked_path(self._lock_path, exclusive=False):
            records = self._read_unlocked()
        return LockProjection.from_records(records, self.root.tenant_id, integrity=True).snapshot()

    def _append(
        self,
        build: Callable[[LockProjection], dict[str, object]],
        *,
        before_append: Optional[Callable[[], None]] = None,
    ) -> dict[str, object]:
        with _locked_path(self._lock_path, exclusive=True):
            existing = self._read_unlocked()
            if len(existing) >= MAX_LEDGER_RECORDS:
                raise ProtocolRefusal("ledger_record_limit", "Locks ledger reached its row bound")
            projection = LockProjection.from_records(existing, self.root.tenant_id, integrity=True)
            row = validate_lock_record(build(projection), self.root.tenant_id, integrity=False)
            prospective = LockProjection.from_records(existing, self.root.tenant_id, integrity=True)
            prospective.apply(row, integrity=False)
            encoded = encode_frame(row)
            if len(encoded) > MAX_RECORD_BYTES:
                raise ProtocolRefusal("record_too_large", "Locks record exceeds its byte bound")
            if before_append is not None:
                before_append()
            _append_frame(self._path, encoded)
            return deepcopy(row)

    @staticmethod
    def _row(kind: str, tenant_id: str, now: object, **fields: object) -> dict[str, object]:
        prefixes = {
            "lock_acquired": "lock-acquired-",
            "lock_escalated": "lock-escalated-",
            "lock_announcement_stopped": "lock-announcement-stopped-",
            "lock_announcement_rearmed": "lock-announcement-rearmed-",
            "lock_announcement_delivered": "lock-announcement-delivered-",
            "car_submitted": "car-submitted-",
            "car_measurement_recorded": "car-measurement-",
            "car_reviewed": "car-reviewed-",
            "car_landed": "car-landed-",
            "car_dissolved": "car-dissolved-",
            "seat_provisioned": "seat-provisioned-",
            "review_handoff_queued": "review-handoff-queued-",
            "review_handoff_stopped": "review-handoff-stopped-",
            "review_handoff_rearmed": "review-handoff-rearmed-",
            "review_handoff_delivered": "review-handoff-delivered-",
        }
        return {
            "schema_version": 0,
            "id": prefixes[kind] + uuid7_hex(),
            "tenant_id": tenant_id,
            "timestamp": now,
            "kind": kind,
            **fields,
        }

    def acquire(
        self,
        *,
        lock_id: object,
        holder: object,
        expires_at: object,
        escalation_holder: object,
        now: object,
    ) -> dict[str, object]:
        return self._append(lambda _projection: self._row(
            "lock_acquired", self.root.tenant_id, now,
            lock_id=lock_id,
            holder=holder,
            expires_at=expires_at,
            escalation_holder=escalation_holder,
        ))

    def submit_car(
        self,
        *,
        car_id: object,
        ref: object,
        ref_oid: object,
        witness: object,
        now: object,
    ) -> dict[str, object]:
        validate_full_ref(ref, "ref", integrity=False)
        validate_witness(witness, integrity=False)
        return self._append(lambda _projection: self._row(
            "car_submitted", self.root.tenant_id, now,
            car_id=car_id,
            ref=ref,
            ref_oid=ref_oid,
            witness=witness,
        ))

    def record_measurement(
        self,
        *,
        car_id: object,
        measured_ref: object,
        measured_tree: object,
        test_count: object,
        failure_count: object,
        evidence_digest: object,
        now: object,
    ) -> dict[str, object]:
        validate_full_ref(
            measured_ref, "measured_ref", integrity=False,
            code="measurement_ref_required",
        )
        return self._append(lambda _projection: self._row(
            "car_measurement_recorded", self.root.tenant_id, now,
            car_id=car_id,
            measured_ref=measured_ref,
            measured_tree=measured_tree,
            test_count=test_count,
            failure_count=failure_count,
            evidence_digest=evidence_digest,
        ))

    def escalate(
        self,
        *,
        lock_id: object,
        requested_by: object,
        now: object,
        expires_at: object,
        escalation_holder: object,
        rearm_event: object,
        crash_hook: Optional[Callable[[str], None]] = None,
    ) -> dict[str, object]:
        announcement_id = "lock-announcement-" + uuid7_hex()

        def build(projection: LockProjection) -> dict[str, object]:
            current = projection.snapshot().locks.get(str(lock_id))
            if current is None:
                raise ProtocolRefusal("lock_missing", "escalation requires an existing lock")
            return self._row(
                "lock_escalated", self.root.tenant_id, now,
                lock_id=lock_id,
                prior_holder=current.holder,
                prior_expires_at=current.expires_at,
                requested_by=requested_by,
                holder=requested_by,
                expires_at=expires_at,
                escalation_holder=escalation_holder,
                announcement_id=announcement_id,
                announcement_recipient=current.holder,
                announcement_status="pending",
                announcement_rearm_event=rearm_event,
                copy_key="[[locks.escalation.action_taken_not_role]]",
            )

        before_append = None
        if crash_hook is not None:
            before_append = lambda: crash_hook("after_holder_decision_before_atomic_append")
        return self._append(build, before_append=before_append)

    def record_review(
        self,
        *,
        car_id: object,
        verdict: object,
        rank: object,
        base_ref: object,
        base_oid: object,
        base_tree: object,
        witness_holds: object,
        now: object,
    ) -> dict[str, object]:
        return self._append(lambda _projection: self._row(
            "car_reviewed", self.root.tenant_id, now,
            car_id=car_id,
            verdict=verdict,
            rank=rank,
            base_ref=base_ref,
            base_oid=base_oid,
            base_tree=base_tree,
            witness_holds=witness_holds,
        ))

    def record_landed(
        self,
        *,
        car_id: object,
        target_ref: object,
        target_oid: object,
        target_tree: object,
        method: object,
        witness_holds: object,
        now: object,
    ) -> dict[str, object]:
        return self._append(lambda _projection: self._row(
            "car_landed", self.root.tenant_id, now,
            car_id=car_id,
            target_ref=target_ref,
            target_oid=target_oid,
            target_tree=target_tree,
            method=method,
            witness_holds=witness_holds,
        ))

    def record_dissolved(
        self,
        *,
        car_id: object,
        product_ref: object,
        product_oid: object,
        product_tree: object,
        witness_holds: object,
        now: object,
    ) -> dict[str, object]:
        return self._append(lambda _projection: self._row(
            "car_dissolved", self.root.tenant_id, now,
            car_id=car_id,
            product_ref=product_ref,
            product_oid=product_oid,
            product_tree=product_tree,
            witness_holds=witness_holds,
        ))

    def stop_announcement(
        self,
        lock_id: object,
        *,
        attempts: object,
        stopped_at: object,
        rearm_event: object,
    ) -> dict[str, object]:
        def build(projection: LockProjection) -> dict[str, object]:
            current = projection.snapshot().locks.get(str(lock_id))
            announcement = None if current is None else current.announcement
            if announcement is None:
                raise ProtocolRefusal("announcement_missing", "lock has no announcement obligation")
            return self._row(
                "lock_announcement_stopped", self.root.tenant_id, stopped_at,
                lock_id=lock_id,
                announcement_id=announcement.announcement_id,
                attempts=attempts,
                stopped_at=stopped_at,
                rearm_event=rearm_event,
            )
        return self._append(build)

    def record_seat_provisioned(
        self,
        *,
        seat_id: object,
        hook_names: object,
        manifest_digest: object,
        now: object,
    ) -> dict[str, object]:
        return self._append(lambda _projection: self._row(
            "seat_provisioned", self.root.tenant_id, now,
            seat_id=seat_id,
            hook_names=hook_names,
            manifest_digest=manifest_digest,
        ))

    def rearm_announcement(
        self,
        lock_id: object,
        *,
        observed_event: object,
        now: object,
    ) -> dict[str, object]:
        def build(projection: LockProjection) -> dict[str, object]:
            current = projection.snapshot().locks.get(str(lock_id))
            announcement = None if current is None else current.announcement
            if announcement is None:
                raise ProtocolRefusal("announcement_missing", "lock has no announcement obligation")
            if observed_event != announcement.rearm_event:
                raise ProtocolRefusal(
                    "announcement_rearm_event_mismatch",
                    "observed event does not match the named re-arm event",
                )
            return self._row(
                "lock_announcement_rearmed", self.root.tenant_id, now,
                lock_id=lock_id,
                announcement_id=announcement.announcement_id,
                observed_event=observed_event,
            )
        return self._append(build)

    def record_announcement_delivery(
        self,
        lock_id: object,
        *,
        receipt_id: object,
        receipt_digest: object,
        now: object,
    ) -> dict[str, object]:
        if type(receipt_id) is not str or not receipt_id.startswith("delivery-"):
            raise ProtocolRefusal(
                "delivery_receipt_required",
                "announcement delivery requires one explicit delivery receipt",
            )

        def build(projection: LockProjection) -> dict[str, object]:
            current = projection.snapshot().locks.get(str(lock_id))
            announcement = None if current is None else current.announcement
            if announcement is None:
                raise ProtocolRefusal("announcement_missing", "lock has no announcement obligation")
            return self._row(
                "lock_announcement_delivered", self.root.tenant_id, now,
                lock_id=lock_id,
                announcement_id=announcement.announcement_id,
                receipt_id=receipt_id,
                receipt_digest=receipt_digest,
            )
        return self._append(build)

    def queue_review_handoff(
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
        return self._append(lambda _projection: self._row(
            "review_handoff_queued", self.root.tenant_id, now,
            handoff_id=handoff_id,
            recipient=recipient,
            car_id=car_id,
            ref=ref,
            base_ref=base_ref,
            witness=witness,
            rearm_event=rearm_event,
            status="pending",
            copy_key="[[locks.handoff.pending]]",
        ))

    @staticmethod
    def _handoff_binding(handoff: object) -> dict[str, object]:
        return {
            "handoff_id": getattr(handoff, "handoff_id"),
            "recipient": getattr(handoff, "recipient"),
            "car_id": getattr(handoff, "car_id"),
            "ref": getattr(handoff, "ref"),
            "base_ref": getattr(handoff, "base_ref"),
            "witness": dict(getattr(handoff, "witness")),
            "rearm_event": getattr(handoff, "rearm_event"),
        }

    def stop_review_handoff(
        self,
        handoff_id: object,
        *,
        attempts: object,
        stopped_at: object,
        rearm_event: object,
    ) -> dict[str, object]:
        def build(projection: LockProjection) -> dict[str, object]:
            handoff = projection.snapshot().handoffs.get(str(handoff_id))
            if handoff is None:
                raise ProtocolRefusal("handoff_missing", "review handoff does not exist")
            if rearm_event != getattr(handoff, "rearm_event"):
                raise ProtocolRefusal(
                    "handoff_rearm_event_mismatch",
                    "stop event does not match the handoff's named re-arm event",
                )
            return self._row(
                "review_handoff_stopped", self.root.tenant_id, stopped_at,
                **self._handoff_binding(handoff),
                status="stopped",
                copy_key="[[locks.handoff.stopped]]",
                attempts=attempts,
                stopped_at=stopped_at,
            )
        return self._append(build)

    def rearm_review_handoff(
        self,
        handoff_id: object,
        *,
        observed_event: object,
        now: object,
    ) -> dict[str, object]:
        def build(projection: LockProjection) -> dict[str, object]:
            handoff = projection.snapshot().handoffs.get(str(handoff_id))
            if handoff is None:
                raise ProtocolRefusal("handoff_missing", "review handoff does not exist")
            if observed_event != getattr(handoff, "rearm_event"):
                raise ProtocolRefusal(
                    "handoff_rearm_event_mismatch",
                    "observed event does not match the handoff's named re-arm event",
                )
            return self._row(
                "review_handoff_rearmed", self.root.tenant_id, now,
                **self._handoff_binding(handoff),
                status="pending",
                copy_key="[[locks.handoff.pending]]",
                observed_event=observed_event,
            )
        return self._append(build)

    def record_review_handoff_delivery(
        self,
        handoff_id: object,
        *,
        receipt_id: object,
        receipt_digest: object,
        now: object,
    ) -> dict[str, object]:
        if type(receipt_id) is not str or not receipt_id.startswith("delivery-"):
            raise ProtocolRefusal(
                "delivery_receipt_required",
                "review handoff delivery requires one explicit delivery receipt",
            )

        def build(projection: LockProjection) -> dict[str, object]:
            handoff = projection.snapshot().handoffs.get(str(handoff_id))
            if handoff is None:
                raise ProtocolRefusal("handoff_missing", "review handoff does not exist")
            return self._row(
                "review_handoff_delivered", self.root.tenant_id, now,
                **self._handoff_binding(handoff),
                status="delivered",
                receipt_id=receipt_id,
                receipt_digest=receipt_digest,
            )
        return self._append(build)

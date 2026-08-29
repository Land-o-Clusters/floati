"""Pure physical-order replay for the private Locks ledger."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Iterable, Mapping, Optional

from ..errors import IntegrityFailure, ProtocolRefusal
from .contracts import validate_lock_record


@dataclass(frozen=True)
class AnnouncementView:
    announcement_id: str
    recipient: str
    status: str
    rearm_event: str
    copy_key: str
    attempts: int = 0
    stopped_at: Optional[str] = None
    receipt_id: Optional[str] = None
    receipt_digest: Optional[str] = None


@dataclass(frozen=True)
class LockView:
    lock_id: str
    holder: str
    expires_at: str
    escalation_holder: str
    announcement: Optional[AnnouncementView] = None


@dataclass(frozen=True)
class ReviewView:
    verdict: str
    rank: int
    base_ref: str
    base_oid: str
    base_tree: str
    witness_holds: bool


@dataclass(frozen=True)
class CarView:
    car_id: str
    ref: str
    ref_oid: str
    witness: Mapping[str, object]
    measurement: Optional[Mapping[str, object]] = None
    review: Optional[ReviewView] = None
    submission_position: int = 0
    state: str = "queued"
    product_ref: Optional[str] = None


@dataclass(frozen=True)
class HandoffView:
    handoff_id: str
    recipient: str
    car_id: str
    ref: str
    base_ref: str
    witness: Mapping[str, object]
    rearm_event: str
    status: str = "pending"
    attempts: int = 0
    stopped_at: Optional[str] = None
    receipt_id: Optional[str] = None
    receipt_digest: Optional[str] = None


@dataclass(frozen=True)
class LockSnapshot:
    locks: Mapping[str, LockView]
    cars: Mapping[str, CarView]
    announcements: Mapping[str, AnnouncementView]
    seats: Mapping[str, "SeatView"]
    handoffs: Mapping[str, HandoffView]


class LockProjection:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self._records: list[dict[str, object]] = []
        self._seen_ids: set[str] = set()
        self._locks: dict[str, LockView] = {}
        self._cars: dict[str, CarView] = {}
        self._announcements: dict[str, AnnouncementView] = {}
        self._seats: dict[str, SeatView] = {}
        self._handoffs: dict[str, HandoffView] = {}

    @staticmethod
    def _raise(integrity: bool, code: str, detail: str) -> None:
        error = IntegrityFailure if integrity else ProtocolRefusal
        raise error(code, detail)

    @classmethod
    def from_records(
        cls,
        records: Iterable[Mapping[str, object]],
        tenant_id: str,
        *,
        integrity: bool = True,
    ) -> "LockProjection":
        projection = cls(tenant_id)
        for raw in records:
            projection.apply(raw, integrity=integrity)
        return projection

    def apply(self, raw: Mapping[str, object], *, integrity: bool) -> None:
        row = validate_lock_record(deepcopy(raw), self.tenant_id, integrity=integrity)
        record_id = str(row["id"])
        if record_id in self._seen_ids:
            self._raise(integrity, "duplicate_record_id", "Locks prefix repeats a record id")
        kind = str(row["kind"])
        if kind == "lock_acquired":
            lock_id = str(row["lock_id"])
            if lock_id in self._locks:
                self._raise(integrity, "lock_already_active", "lock already has an active holder")
            self._locks[lock_id] = LockView(
                lock_id=lock_id,
                holder=str(row["holder"]),
                expires_at=str(row["expires_at"]),
                escalation_holder=str(row["escalation_holder"]),
            )
        elif kind == "lock_escalated":
            lock_id = str(row["lock_id"])
            current = self._locks.get(lock_id)
            if current is None:
                self._raise(integrity, "lock_missing", "escalation precedes its lock")
            if (
                row["prior_holder"] != current.holder
                or row["prior_expires_at"] != current.expires_at
                or row["requested_by"] != current.escalation_holder
                or row["holder"] != row["requested_by"]
            ):
                self._raise(integrity, "lock_escalation_binding_invalid", "escalation changes its immutable prior binding")
            from .contracts import timestamp_value
            if timestamp_value(str(row["timestamp"])) < timestamp_value(current.expires_at):
                self._raise(integrity, "lock_not_expired", "named escalation cannot act before expiry")
            announcement = AnnouncementView(
                announcement_id=str(row["announcement_id"]),
                recipient=str(row["announcement_recipient"]),
                status="pending",
                rearm_event=str(row["announcement_rearm_event"]),
                copy_key=str(row["copy_key"]),
            )
            if announcement.announcement_id in self._announcements:
                self._raise(integrity, "announcement_already_exists", "announcement id already exists")
            self._announcements[announcement.announcement_id] = announcement
            self._locks[lock_id] = LockView(
                lock_id=lock_id,
                holder=str(row["holder"]),
                expires_at=str(row["expires_at"]),
                escalation_holder=str(row["escalation_holder"]),
                announcement=announcement,
            )
        elif kind == "lock_announcement_stopped":
            lock_id = str(row["lock_id"])
            current = self._locks.get(lock_id)
            announcement = None if current is None else current.announcement
            if (
                announcement is None
                or announcement.announcement_id != row["announcement_id"]
                or announcement.status != "pending"
                or announcement.rearm_event != row["rearm_event"]
            ):
                self._raise(integrity, "announcement_transition_invalid", "only the current pending announcement may stop")
            updated = replace(
                announcement,
                status="stopped",
                attempts=int(row["attempts"]),
                stopped_at=str(row["stopped_at"]),
            )
            self._announcements[updated.announcement_id] = updated
            self._locks[lock_id] = replace(current, announcement=updated)
        elif kind == "lock_announcement_rearmed":
            lock_id = str(row["lock_id"])
            current = self._locks.get(lock_id)
            announcement = None if current is None else current.announcement
            if announcement is None or announcement.announcement_id != row["announcement_id"] or announcement.status != "stopped":
                self._raise(integrity, "announcement_transition_invalid", "only the current stopped announcement may re-arm")
            if announcement.rearm_event != row["observed_event"]:
                self._raise(integrity, "announcement_rearm_event_mismatch", "observed event does not match the named re-arm event")
            updated = replace(announcement, status="pending")
            self._announcements[updated.announcement_id] = updated
            self._locks[lock_id] = replace(current, announcement=updated)
        elif kind == "lock_announcement_delivered":
            lock_id = str(row["lock_id"])
            current = self._locks.get(lock_id)
            announcement = None if current is None else current.announcement
            if announcement is None or announcement.announcement_id != row["announcement_id"] or announcement.status != "pending":
                self._raise(integrity, "announcement_transition_invalid", "only the current pending announcement may be delivered")
            updated = replace(
                announcement,
                status="delivered",
                receipt_id=str(row["receipt_id"]),
                receipt_digest=str(row["receipt_digest"]),
            )
            self._announcements[updated.announcement_id] = updated
            self._locks[lock_id] = replace(current, announcement=updated)
        elif kind == "car_submitted":
            car_id = str(row["car_id"])
            if car_id in self._cars:
                self._raise(integrity, "car_already_submitted", "car id already exists")
            self._cars[car_id] = CarView(
                car_id=car_id,
                ref=str(row["ref"]),
                ref_oid=str(row["ref_oid"]),
                witness=MappingProxyType(deepcopy(row["witness"])),
                submission_position=len(self._cars),
            )
        elif kind == "car_measurement_recorded":
            car_id = str(row["car_id"])
            car = self._cars.get(car_id)
            if car is None:
                self._raise(integrity, "car_missing", "measurement precedes its car")
            measurement = MappingProxyType({
                field: deepcopy(row[field])
                for field in (
                    "measured_ref", "measured_tree", "test_count",
                    "failure_count", "evidence_digest",
                )
            })
            self._cars[car_id] = replace(car, measurement=measurement)
        elif kind == "car_reviewed":
            car_id = str(row["car_id"])
            car = self._cars.get(car_id)
            if car is None:
                self._raise(integrity, "car_missing", "review precedes its car")
            review = ReviewView(
                verdict=str(row["verdict"]),
                rank=int(row["rank"]),
                base_ref=str(row["base_ref"]),
                base_oid=str(row["base_oid"]),
                base_tree=str(row["base_tree"]),
                witness_holds=bool(row["witness_holds"]),
            )
            self._cars[car_id] = replace(car, review=review)
        elif kind == "car_landed":
            car_id = str(row["car_id"])
            car = self._cars.get(car_id)
            if car is None:
                self._raise(integrity, "car_missing", "landing precedes its car")
            if car.state == "dissolved":
                self._raise(integrity, "car_transition_invalid", "dissolved car cannot land again")
            self._cars[car_id] = replace(
                car,
                state="landed",
                product_ref=str(row["target_ref"]),
            )
        elif kind == "car_dissolved":
            car_id = str(row["car_id"])
            car = self._cars.get(car_id)
            if car is None:
                self._raise(integrity, "car_missing", "dissolution precedes its car")
            self._cars[car_id] = replace(
                car,
                state="dissolved",
                product_ref=str(row["product_ref"]),
            )
        elif kind == "seat_provisioned":
            seat_id = str(row["seat_id"])
            if seat_id in self._seats:
                self._raise(integrity, "seat_already_provisioned", "seat id already has provisioning testimony")
            self._seats[seat_id] = SeatView(
                seat_id=seat_id,
                hook_names=tuple(str(name) for name in row["hook_names"]),
                manifest_digest=str(row["manifest_digest"]),
            )
        elif kind == "review_handoff_queued":
            handoff_id = str(row["handoff_id"])
            if handoff_id in self._handoffs:
                self._raise(integrity, "handoff_already_queued", "review handoff id already exists")
            self._handoffs[handoff_id] = HandoffView(
                handoff_id=handoff_id,
                recipient=str(row["recipient"]),
                car_id=str(row["car_id"]),
                ref=str(row["ref"]),
                base_ref=str(row["base_ref"]),
                witness=MappingProxyType(deepcopy(row["witness"])),
                rearm_event=str(row["rearm_event"]),
            )
        elif kind.startswith("review_handoff_"):
            handoff_id = str(row["handoff_id"])
            handoff = self._handoffs.get(handoff_id)
            if handoff is None:
                self._raise(integrity, "handoff_missing", "review handoff transition precedes its queue row")
            binding = (
                row["recipient"] == handoff.recipient
                and row["car_id"] == handoff.car_id
                and row["ref"] == handoff.ref
                and row["base_ref"] == handoff.base_ref
                and row["witness"] == dict(handoff.witness)
                and row["rearm_event"] == handoff.rearm_event
            )
            if not binding:
                self._raise(integrity, "handoff_binding_invalid", "handoff transition changes its immutable binding")
            if kind == "review_handoff_stopped":
                if handoff.status != "pending":
                    self._raise(integrity, "handoff_transition_invalid", "only a pending handoff may stop")
                self._handoffs[handoff_id] = replace(
                    handoff,
                    status="stopped",
                    attempts=int(row["attempts"]),
                    stopped_at=str(row["stopped_at"]),
                )
            elif kind == "review_handoff_rearmed":
                if handoff.status != "stopped":
                    self._raise(integrity, "handoff_transition_invalid", "only a stopped handoff may re-arm")
                if row["observed_event"] != handoff.rearm_event:
                    self._raise(integrity, "handoff_rearm_event_mismatch", "observed event does not match the named re-arm event")
                self._handoffs[handoff_id] = replace(handoff, status="pending")
            else:
                if handoff.status != "pending":
                    self._raise(integrity, "handoff_transition_invalid", "only a pending handoff may be delivered")
                self._handoffs[handoff_id] = replace(
                    handoff,
                    status="delivered",
                    receipt_id=str(row["receipt_id"]),
                    receipt_digest=str(row["receipt_digest"]),
                )
        self._seen_ids.add(record_id)
        self._records.append(deepcopy(row))

    def snapshot(self) -> LockSnapshot:
        return LockSnapshot(
            locks=MappingProxyType(dict(self._locks)),
            cars=MappingProxyType(dict(self._cars)),
            announcements=MappingProxyType(dict(self._announcements)),
            seats=MappingProxyType(dict(self._seats)),
            handoffs=MappingProxyType(dict(self._handoffs)),
        )


@dataclass(frozen=True)
class SeatView:
    seat_id: str
    hook_names: tuple[str, ...]
    manifest_digest: str

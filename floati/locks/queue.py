"""Ref-only, content-witnessed patch queue for the DARK Locks package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..errors import ProtocolRefusal
from .contracts import validate_full_ref, validate_witness
from .git_observer import GitObserver
from .ledger import LockLedger


@dataclass(frozen=True)
class CarSelection:
    car_id: str
    ref: str
    rank: int
    status: str


@dataclass(frozen=True)
class LandingPlan:
    car_id: str
    car_ref: str
    target_ref: str
    method: str


class PatchQueue:
    def __init__(self, ledger: LockLedger, observer: GitObserver) -> None:
        if type(ledger) is not LockLedger or type(observer) is not GitObserver:
            raise ProtocolRefusal("locks_queue_invalid", "patch queue requires exact ledger and Git observer owners")
        self.ledger = ledger
        self.observer = observer

    def submit(
        self,
        *,
        car_id: object,
        ref: object,
        witness: object,
        now: object,
    ) -> dict[str, object]:
        full_ref = validate_full_ref(ref, "ref", integrity=False)
        normalized_witness = validate_witness(witness, integrity=False)
        observed = self.observer.resolve_ref(full_ref)
        return self.ledger.submit_car(
            car_id=car_id,
            ref=full_ref,
            ref_oid=observed.oid,
            witness=normalized_witness,
            now=now,
        )

    def landing_status(self, car_id: str, target_ref: object) -> str:
        car = self.ledger.snapshot().cars.get(car_id)
        if car is None:
            raise ProtocolRefusal("car_missing", "landing status requires a submitted car")
        result = self.observer.verify_witness(target_ref, car.witness)
        return "landed" if result.holds else "not_landed"

    def record_review(
        self,
        car_id: str,
        *,
        verdict: object,
        rank: object,
        base_ref: object,
        now: object,
    ) -> dict[str, object]:
        car = self.ledger.snapshot().cars.get(car_id)
        if car is None:
            raise ProtocolRefusal("car_missing", "review requires a submitted car")
        base = self.observer.resolve_ref(base_ref)
        witness = self.observer.verify_witness(base.ref, car.witness)
        return self.ledger.record_review(
            car_id=car_id,
            verdict=verdict,
            rank=rank,
            base_ref=base.ref,
            base_oid=base.oid,
            base_tree=base.tree_oid,
            witness_holds=witness.holds,
            now=now,
        )

    def review_status(self, car_id: str, base_ref: object) -> str:
        car = self.ledger.snapshot().cars.get(car_id)
        if car is None:
            raise ProtocolRefusal("car_missing", "review status requires a submitted car")
        if car.review is None:
            return "review_required"
        base = self.observer.resolve_ref(base_ref)
        if base.ref == car.review.base_ref and base.oid == car.review.base_oid and base.tree_oid == car.review.base_tree:
            return car.review.verdict
        observed = self.observer.verify_witness(base.ref, car.witness)
        if observed.holds != car.review.witness_holds:
            return "review_required"
        return car.review.verdict + "_rederived"

    def select_next(self, target_ref: object) -> CarSelection:
        cars = tuple(self.ledger.snapshot().cars.values())
        ranked = sorted(
            (car for car in cars if car.review is not None and car.state == "queued"),
            key=lambda car: (-car.review.rank, car.submission_position),
        )
        for car in ranked:
            status = self.review_status(car.car_id, target_ref)
            if status in {"approved", "approved_rederived"}:
                return CarSelection(
                    car_id=car.car_id,
                    ref=car.ref,
                    rank=car.review.rank,
                    status=status,
                )
        raise ProtocolRefusal("merge_queue_empty", "no reviewed car is currently mergeable")

    def land_next(
        self,
        target_ref: object,
        executor: Callable[[LandingPlan], None],
        *,
        method: str,
        now: object,
    ) -> dict[str, object]:
        if method not in {"cherry_pick", "rebase"}:
            raise ProtocolRefusal("landing_method_invalid", "landing method must be cherry_pick or rebase")
        if not callable(executor):
            raise ProtocolRefusal("landing_executor_invalid", "landing executor must be callable")
        full_target = validate_full_ref(target_ref, "target_ref", integrity=False)
        selection = self.select_next(full_target)
        plan = LandingPlan(
            car_id=selection.car_id,
            car_ref=selection.ref,
            target_ref=full_target,
            method=method,
        )
        executor(plan)
        car = self.ledger.snapshot().cars[selection.car_id]
        observed = self.observer.verify_witness(full_target, car.witness)
        if not observed.holds:
            raise ProtocolRefusal("car_content_not_landed", "landing executor returned before the content witness held")
        return self.ledger.record_landed(
            car_id=selection.car_id,
            target_ref=observed.ref.ref,
            target_oid=observed.ref.oid,
            target_tree=observed.ref.tree_oid,
            method=method,
            witness_holds=True,
            now=now,
        )

    def dissolve(self, car_id: str, *, product_ref: object, now: object) -> dict[str, object]:
        car = self.ledger.snapshot().cars.get(car_id)
        if car is None:
            raise ProtocolRefusal("car_missing", "dissolution requires a submitted car")
        product = self.observer.verify_witness(product_ref, car.witness)
        if not product.holds:
            raise ProtocolRefusal(
                "car_not_landed_on_product",
                "car content witness does not hold on the explicit product ref",
            )
        return self.ledger.record_dissolved(
            car_id=car_id,
            product_ref=product.ref.ref,
            product_oid=product.ref.oid,
            product_tree=product.ref.tree_oid,
            witness_holds=True,
            now=now,
        )

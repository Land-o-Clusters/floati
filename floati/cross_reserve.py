"""Control-budget admission that keeps close-out affordable for the whole lifetime.

The close-out plan is derived from committed state. Each step is priced at
its record's maximum framed size:

| step | record | price |
|---|---|---|
| start disable | `cross_optin {disable_started}` | 512 B |
| each open ceremony | `cross_ceremony_closed` | 512 B |
| each active pair | `cross_pair_revoked` | 768 B |
| teardown | `cross_teardown_receipt {outcome: complete}` | 48 KiB |
| finish | `cross_optin {disable_complete}` | 512 B |

On top of the plan sits a headroom of `REPAIR_HEADROOM` tail repairs
(512 B and one slot each). Every step also costs one slot.

Admission, checked in bytes and in slots:

- An **ordinary** control append is charged `max(size, 512)`: its own
  failure leaves at most one tail repair. It must still leave the plan plus
  the full headroom, before and after itself.
- A **data** append never spends control bytes, but its failure would leave
  a control repair. It is admitted only while 512 B and one slot remain
  beyond the plan plus headroom.
- An accepted inbound also reserves three completion records and a
  repair slot. A completion uses that slot for its first torn append
  and needs ordinary repair room for further attempts.
- A **close-out** append retires exactly one plan step, at no more than that
  step's price, and needs only to fit.
- A record that retires no step is ordinary, so an incomplete teardown and its
  receipt can never spend the plan.

Under these rules, by induction from enable, the room left always covers the
plan plus `REPAIR_HEADROOM - h`, where h counts interrupted close-out
attempts since the last full headroom. The failure bound is stated, not
hidden: disable completes through the journal if no more than
`REPAIR_HEADROOM` close-out attempts are interrupted. Compaction restores
the headroom, because it writes no repair record when interrupted.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

from .cross_attended import activation_review_item, approved_completion, review_item
from .cross_coordinates import MAX_INTEGER
from .errors import ProtocolRefusal
from .framing import encode_frame


KIB = 1024
REPAIR_PRICE = 512
REPAIR_HEADROOM = 2
COMPLETION_PRICE = 2048
ACCEPTED_OBLIGATIONS = frozenset({"remote_receipt_confirmed", "remote_presented", "remote_local_ack"})
COMPLETION_KINDS = ACCEPTED_OBLIGATIONS | {"remote_hold_rejected"}
PRICES: Dict[str, int] = {
    "cross_optin": 544,
    "cross_ceremony_closed": 512,
    "cross_pair_revoked": 768,
    "cross_teardown_receipt": 48 * KIB,
}

CONTROL_KINDS = frozenset({
    "cross_optin",
    "cross_inventory_item",
    "cross_ceremony_opened",
    "cross_ceremony_closed",
    "cross_pair_active",
    "cross_pair_revoked",
    "cross_peer_address",
    "cross_teardown_receipt",
})
DATA_KINDS = frozenset({
    "remote_outbound",
    "remote_attempt",
    "remote_inbound",
    "remote_peer_receipt",
    "remote_reconcile_frontier",
    "remote_sender_replay",
    "remote_session_opened",
    "remote_session_refused",
    "remote_transport_attempt",
    "remote_endpoint_registered",
    "remote_unpair_notice_received",
    "cross_pair_seen",
}) | COMPLETION_KINDS
CONTROLLER_KINDS = frozenset({
    "cross_journal_tail_repaired",
    "cross_journal_link",
    "cross_trust_checkpoint",
    "cross_prune_checkpoint",
    "cross_sender_floor",
})
_WIDENING = frozenset({
    "cross_ceremony_opened",
    "cross_pair_active",
    "cross_peer_address",
    "cross_inventory_item",
})
_ENABLE_PHASES = frozenset({"enable_started", "enable_complete"})
_ONCE_PHASES = frozenset({"enable_started", "enable_complete", "disable_started", "disable_complete"})
_TRUST_PHASES = frozenset({"enable_complete", "disable_started", "disable_complete"})
ACTIVATION_PRICE = 65536


@dataclass(frozen=True)
class Budget:
    bytes: int
    slots: int


CONTROL_BUDGET = Budget(8 * KIB * KIB, 10000)
DATA_BUDGET = Budget(56 * KIB * KIB, 90000)
PAIR_DATA_BUDGET = Budget(3670016, 5625)


@dataclass(frozen=True)
class Delivery:
    pair_id: object
    reserved: bool
    kinds: FrozenSet[str]
    remaining: FrozenSet[str]
    repair_spent: bool = False


@dataclass
class ControlState:
    phases: Set[str] = field(default_factory=set)
    open_ceremonies: Set[str] = field(default_factory=set)
    active_pairs: Dict[str, Dict[str, object]] = field(default_factory=dict)
    final_receipt: bool = False
    control_bytes: int = 0
    control_slots: int = 0
    data_bytes: int = 0
    data_slots: int = 0
    deliveries: Dict[str, Delivery] = field(default_factory=dict)
    has_link: bool = False
    quota_day: Optional[str] = None
    quota_count: int = 0
    pair_data: Dict[str, Budget] = field(default_factory=dict)
    sender_coexist: Dict[Tuple[object, ...], int] = field(default_factory=dict)
    activation_holds: Set[str] = field(default_factory=set)
    pair_history: Dict[str, Dict[str, object]] = field(default_factory=dict)
    enable_review_hold: Budget = Budget(0, 0)
    enable_review_bytes: int = 0

    def copy(self) -> "ControlState":
        return ControlState(
            set(self.phases), set(self.open_ceremonies), dict(self.active_pairs),
            self.final_receipt, self.control_bytes, self.control_slots,
            self.data_bytes, self.data_slots, dict(self.deliveries), self.has_link, self.quota_day, self.quota_count,
            dict(self.pair_data), dict(self.sender_coexist),
            set(self.activation_holds), dict(self.pair_history), self.enable_review_hold, self.enable_review_bytes,
        )


def _body(record: Mapping[str, object]) -> Dict[str, object]:
    return {key: value for key, value in record.items() if key not in {"seq", "prev"}}


def inbound_result(record: Mapping[str, object]) -> Optional[str]:
    if record.get("kind") != "remote_inbound":
        return None
    receipt = record.get("receipt_body")
    result = receipt.get("result") if isinstance(receipt, dict) else None
    if isinstance(result, dict) and len(result) == 1:
        for variant in ("accepted", "refused"):
            if isinstance(result.get(variant), dict):
                return variant
    return None


def delivery_key(record_id: str) -> str:
    return hashlib.sha256(record_id.encode("utf-8")).hexdigest()


def completion_id(key: str, kind: str) -> str:
    return f"cross-completion-{kind.replace('_', '-')}-{key}"


def _send_coordinate(record: Mapping[str, object]) -> tuple:
    return tuple(record[key] for key in ("enable_id", "enable_generation", "pair_id", "direction", "delivery_seq"))


def _data_charge(state: ControlState, record: Mapping[str, object], size: int) -> Budget:
    seq = record.get("seq")
    if record.get("kind") == "remote_outbound" and record.get("schema_version") == 2:
        source = size + max(0, 6 - len(str(seq)))
        replay = record["replay_price"]
        if record["replay_retained"]:
            return Budget(source + replay, 2)
        return Budget(max(source, replay), 1)
    if not state.has_link and type(seq) is int:
        size += len(str(seq + 1)) - len(str(seq))
    if record.get("kind") == "remote_sender_replay":
        hold = state.sender_coexist.get(_send_coordinate(record))
        if hold is not None:
            return Budget(size - hold, 0)
    return Budget(size, 1)


def apply(state: ControlState, record: Mapping[str, object], size: Optional[int] = None) -> ControlState:
    """The state after one committed record."""

    after = state.copy()
    _apply(after, record, size)
    return after


def _apply(after: ControlState, record: Mapping[str, object], size: Optional[int] = None) -> None:
    kind = record.get("kind")
    size = len(encode_frame(dict(record))) if size is None else size
    if kind in DATA_KINDS:
        charge = _data_charge(after, record, size)
        after.data_bytes += charge.bytes
        after.data_slots += charge.slots
        if kind == "remote_outbound" and record.get("schema_version") == 2 and record["replay_retained"]:
            after.sender_coexist[_send_coordinate(record)] = record["replay_price"]
        elif kind == "remote_sender_replay":
            after.sender_coexist.pop(_send_coordinate(record), None)
        pair = record.get("pair_id")
        if isinstance(pair, str):
            used = after.pair_data.get(pair, Budget(0, 0))
            after.pair_data[pair] = Budget(used.bytes + charge.bytes, used.slots + charge.slots)
        result = inbound_result(record)
        if result is not None:
            receipt = record["receipt_body"]
            pair = record.get("pair_id", receipt.get("pair_id") if isinstance(receipt, dict) else None)
            remaining = ACCEPTED_OBLIGATIONS if result == "accepted" else frozenset({"remote_receipt_confirmed"})
            kinds = COMPLETION_KINDS if result == "accepted" else remaining
            after.deliveries[delivery_key(str(record["id"]))] = Delivery(
                pair, record.get("completion_reserved") is True, kinds, remaining
            )
        elif kind in COMPLETION_KINDS and isinstance(record.get("inbound_id"), str):
            key = delivery_key(str(record["inbound_id"]))
            delivery = after.deliveries.get(key)
            if delivery is not None:
                discharged = {"remote_local_ack", "remote_presented"} if kind == "remote_hold_rejected" else {str(kind)}
                remaining = delivery.remaining - discharged
                after.deliveries[key] = replace(delivery, remaining=remaining)
        return
    after.control_bytes += _control_charge(record, size)
    after.control_slots += 1
    if kind == "cross_optin":
        if record.get("phase") == "enable_started" and "disable_complete" in after.phases:
            after.phases.clear()
            after.open_ceremonies.clear()
            after.active_pairs.clear()
            after.activation_holds.clear()
            after.final_receipt = False
        phase = record.get("phase")
        if phase == "enable_started" and record.get("schema_version") in (1, 2):
            completion = approved_completion(record)
            fields = dict(prev_trust_digest="0" * 64, trust_count=2, trust_head="0" * 64)
            priced_completion = dict(completion, **fields) if completion["schema_version"] == 2 else completion
            due = _frame_width(priced_completion)
            review = _frame_width(review_item(record, completion))
            after.enable_review_bytes = max(0, review - _control_charge(record, size) - due)
            after.enable_review_hold = Budget(due + after.enable_review_bytes, 1)
        elif phase == "enable_complete":
            after.control_bytes += after.enable_review_bytes
            after.enable_review_hold = Budget(0, 0)
            after.enable_review_bytes = 0
        elif phase == "disable_complete":
            after.enable_review_hold = Budget(0, 0)
            after.enable_review_bytes = 0
        after.phases.add(str(phase))
    elif kind == "cross_ceremony_opened":
        after.open_ceremonies.add(str(record.get("ceremony_id")))
        if record.get("schema_version") == 1 and "enable_id" in record and "enable_generation" in record:
            after.activation_holds.add(str(record["ceremony_id"]))
        day = record.get("utc_day")
        if isinstance(day, str):
            after.quota_count = after.quota_count + 1 if day == after.quota_day else 1
            after.quota_day = day
    elif kind == "cross_ceremony_closed":
        after.open_ceremonies.discard(str(record.get("ceremony_id")))
        after.activation_holds.discard(str(record.get("ceremony_id")))
    elif kind == "cross_pair_active":
        local = str(record.get("opening_id", record.get("ceremony_id")))
        after.open_ceremonies.discard(local)
        after.activation_holds.discard(local)
        after.active_pairs[str(record.get("pair_id"))] = _body(record)
        if record.get("schema_version") == 1:
            after.pair_history[str(record["id"])] = _body(record)
    elif kind == "cross_pair_revoked":
        after.active_pairs.pop(str(record.get("pair_id")), None)
        if record.get("schema_version") == 1 and f"cross-pair-active-{record['pair_id']}" in after.pair_history:
            after.pair_history[str(record["id"])] = _body(record)
    elif kind == "cross_teardown_receipt" and record.get("outcome") == "complete":
        after.final_receipt = True
    elif kind == "cross_journal_link":
        after.has_link = True
    elif kind == "cross_trust_checkpoint" and "quota_count" in record:
        after.quota_day = record.get("quota_day")
        after.quota_count = int(record["quota_count"])
    elif kind == "cross_journal_tail_repaired" and isinstance(record.get("completion_delivery"), str):
        key = str(record["completion_delivery"])
        delivery = after.deliveries.get(key)
        if delivery is not None:
            after.deliveries[key] = replace(delivery, repair_spent=True)


def project(records: Iterable[Mapping[str, object]]) -> ControlState:
    state = ControlState()
    for record in records:
        _apply(state, record)
    return state


def obligations(state: ControlState) -> List[Tuple[str, str]]:
    """The remaining close-out plan, in the order it must commit."""

    if "disable_complete" in state.phases:
        return []
    plan: List[Tuple[str, str]] = []
    if "disable_started" not in state.phases:
        plan.append(("disable_started", "cross_optin"))
    plan += [(f"close:{ceremony}", "cross_ceremony_closed") for ceremony in sorted(state.open_ceremonies)]
    plan += [(f"revoke:{pair}", "cross_pair_revoked") for pair in sorted(state.active_pairs)]
    if not state.final_receipt:
        plan.append(("receipt", "cross_teardown_receipt"))
    plan.append(("disable_complete", "cross_optin"))
    return plan


def check_trust_reserve(state: ControlState, record: Mapping[str, object], count: int) -> None:
    """Preserve enough distinct count values to narrow all current trust."""
    remaining = obligations(apply(state, record))
    shutdown = sum(kind in {"cross_optin", "cross_pair_revoked"} for _, kind in remaining)
    if count + shutdown > MAX_INTEGER:
        raise ProtocolRefusal("cross_trust_counter_exhausted", "the remaining ordered trust counts are reserved for shutdown")


def closeout_cost(state: ControlState, headroom: int = REPAIR_HEADROOM) -> Budget:
    plan = obligations(state)
    if not plan:
        return Budget(0, 0)
    return Budget(
        sum(PRICES[kind] for _, kind in plan) + headroom * REPAIR_PRICE,
        len(plan) + headroom,
    )


def limiting_control(state: ControlState, control: Budget, *, headroom: int) -> Optional[str]:
    reserve = closeout_cost(state, headroom)
    holds, _ = completion_hold(state)
    if state.control_bytes + holds.bytes + reserve.bytes > control.bytes:
        return "control_bytes"
    if state.control_slots + holds.slots + reserve.slots > control.slots:
        return "control_slots"
    return None


def trust_head(state: ControlState) -> str:
    """One digest of what trust depends on: the opt-in phases and active pairs."""

    projection = {
        "phases": sorted(state.phases & _TRUST_PHASES),
        "pairs": sorted(
            [pair, hashlib.sha256(encode_frame(body)).hexdigest()]
            for pair, body in state.active_pairs.items()
        ),
    }
    return hashlib.sha256(encode_frame(projection)).hexdigest()


def retires(state: ControlState, record: Mapping[str, object]) -> Optional[str]:
    """The plan step this record retires, or None."""

    kind = record.get("kind")
    plan = [key for key, _ in obligations(state)]
    if kind == "cross_optin" and record.get("phase") == "disable_started" and "disable_started" in plan:
        return "disable_started"
    if kind == "cross_ceremony_closed" and f"close:{record.get('ceremony_id')}" in plan:
        if record.get("cause") == "disabled" and "disable_started" not in state.phases:
            raise _refuse("cross_transition_invalid", "disable must start before it closes a ceremony")
        return f"close:{record.get('ceremony_id')}"
    if kind == "cross_pair_revoked" and f"revoke:{record.get('pair_id')}" in plan:
        return f"revoke:{record.get('pair_id')}"
    if kind == "cross_teardown_receipt" and record.get("outcome") == "complete" and "receipt" in plan:
        if plan[:2] != ["receipt", "disable_complete"]:
            raise _refuse("cross_closeout_out_of_order", "the teardown receipt follows every revocation")
        return "receipt"
    if kind == "cross_optin" and record.get("phase") == "disable_complete" and "disable_complete" in plan:
        if plan != ["disable_complete"]:
            raise _refuse("cross_closeout_out_of_order", "disable completes after every other close-out step")
        return "disable_complete"
    return None


def _refuse(code: str, detail: str) -> ProtocolRefusal:
    return ProtocolRefusal(code, detail)


def _check_ordinary(state: ControlState, record: Mapping[str, object]) -> None:
    kind = record.get("kind")
    phase = record.get("phase")
    if kind == "cross_optin":
        if phase in _ONCE_PHASES and phase in state.phases:
            raise _refuse("cross_phase_recorded", f"{phase} is already committed")
        if phase == "disable_incomplete" and "disable_started" not in state.phases:
            raise _refuse("cross_transition_invalid", "an incomplete disable follows disable_started")
        if phase not in _ENABLE_PHASES | {"disable_incomplete"}:
            raise _refuse("cross_transition_invalid", f"{phase!r} is not an ordinary opt-in phase")
    elif kind == "cross_pair_active":
        if str(record.get("opening_id", record.get("ceremony_id"))) not in state.open_ceremonies:
            raise _refuse("cross_transition_invalid", "an activation names an open ceremony")
        if (str(record.get("pair_id")) in state.active_pairs or
            any(row["pair_id"] == record.get("pair_id") for row in state.pair_history.values())):
            raise _refuse("cross_transition_invalid", "this pair is already active")
    elif kind == "cross_ceremony_opened":
        if str(record.get("ceremony_id")) in state.open_ceremonies:
            raise _refuse("cross_transition_invalid", "this ceremony is already open")
    elif kind == "cross_peer_address":
        if str(record.get("pair_id")) not in state.active_pairs:
            raise _refuse("cross_transition_invalid", "an address names an active pair")
    elif kind == "cross_teardown_receipt":
        if record.get("outcome") != "incomplete" or "disable_started" not in state.phases:
            raise _refuse("cross_transition_invalid", "an incomplete receipt follows disable_started")
    elif kind in {"cross_ceremony_closed", "cross_pair_revoked"}:
        raise _refuse("cross_transition_invalid", "this record retires nothing that is open or active")


def check_data_scope(state: ControlState, record: Mapping[str, object], pair_id: Optional[str]) -> None:
    """Check the disable fence even when a data caller replays an existing id."""

    if record.get("kind") not in DATA_KINDS or record["kind"] == "remote_hold_rejected":
        return
    if "disable_complete" in state.phases:
        raise _refuse("cross_disabled", "cross-machine is disabled on this root")
    pair = record.get("pair_id", pair_id)
    if "disable_started" in state.phases:
        if record.get("kind") not in COMPLETION_KINDS | {"remote_peer_receipt"}:
            raise _refuse("cross_disable_in_progress", "disable has stopped fresh cross-machine work")
        if not isinstance(pair, str) or pair not in state.active_pairs:
            raise _refuse("cross_pair_scope_refused", "during disable, completion requires a pair that is still active")


def completion_hold(state: ControlState) -> Tuple[Budget, Budget]:
    """Outstanding control and data allowances for committed reservations."""

    repairs = slots = 0
    for delivery in state.deliveries.values():
        if delivery.reserved and delivery.remaining:
            slots += len(delivery.remaining)
            repairs += int(not delivery.repair_spent)
    openings = len(state.activation_holds)
    control = Budget(repairs * REPAIR_PRICE + openings * ACTIVATION_PRICE + state.enable_review_hold.bytes,
                     repairs + openings + state.enable_review_hold.slots)
    return control, Budget(slots * COMPLETION_PRICE, slots)


def _frame_width(body):
    return len(encode_frame(dict(body, seq=100000, prev="0" * 64)))


def _control_charge(record, size):
    if record.get("kind") == "cross_pair_active" and record.get("schema_version") == 1:
        return max(size + max(0, 6 - len(str(record.get("seq", 1)))), _frame_width(activation_review_item(record)))
    if (record.get("kind") == "cross_optin" and record.get("phase") in _ENABLE_PHASES
        and record.get("schema_version") in (1, 2)):
        return size + max(0, 6 - len(str(record.get("seq", 1))))
    return size


def _pair_within(state: ControlState, pair, extra: Budget) -> bool:
    used = state.pair_data.get(pair, Budget(0, 0)) if isinstance(pair, str) else Budget(0, 0)
    held = sum(len(delivery.remaining) for delivery in state.deliveries.values()
               if delivery.reserved and delivery.pair_id == pair)
    return (used.bytes + held * COMPLETION_PRICE + extra.bytes <= PAIR_DATA_BUDGET.bytes
            and used.slots + held + extra.slots <= PAIR_DATA_BUDGET.slots)


def admit(
    state: ControlState,
    record: Mapping[str, object],
    size: int,
    control: Budget = CONTROL_BUDGET,
    data: Budget = DATA_BUDGET,
    *,
    delivery: Optional[Delivery] = None,
    replacement: bool = False,
    rollover: bool = False,
) -> str:
    """Price a scoped record, or refuse it before any write."""

    kind = record.get("kind")
    if "disable_complete" in state.phases and not rollover and kind != "remote_hold_rejected":
        raise _refuse("cross_disabled", "cross-machine is disabled on this root")
    reserve = closeout_cost(state)
    held_control, held_data = completion_hold(state)
    used_control_bytes = state.control_bytes + held_control.bytes
    used_control_slots = state.control_slots + held_control.slots
    if kind in DATA_KINDS:
        repair_fits = (
            used_control_bytes + REPAIR_PRICE + reserve.bytes <= control.bytes
            and used_control_slots + 1 + reserve.slots <= control.slots
        )
        outcome = inbound_result(record)
        slots = {"accepted": len(ACCEPTED_OBLIGATIONS), "refused": 1}.get(outcome, 0)
        price = _data_charge(state, record, size)
        charge = Budget(price.bytes + slots * COMPLETION_PRICE, price.slots + slots)
        pair = record.get("pair_id")
        data_fits = (
            state.data_bytes + held_data.bytes + charge.bytes <= data.bytes
            and state.data_slots + held_data.slots + charge.slots <= data.slots
            and _pair_within(state, pair, charge)
        )
        if replacement:
            replacement_data = (
                state.data_bytes + held_data.bytes + COMPLETION_PRICE <= data.bytes
                and state.data_slots + held_data.slots + 1 <= data.slots
                and _pair_within(state, pair, Budget(COMPLETION_PRICE, 1))
            )
            return "completion" if repair_fits and replacement_data else "completion_deferred"
        if delivery is not None:
            if delivery.reserved and not delivery.repair_spent:
                return "completion_reserved"
            if repair_fits and (delivery.reserved or data_fits):
                return "completion"
            return "completion_deferred"
        if not repair_fits:
            raise _refuse("cross_reserve_protected", "a failed append here would spend the close-out credit")
        if not data_fits:
            raise _refuse("cross_storage_cap", "the data budget is full")
        return "data"
    if "disable_started" in state.phases and not rollover and (
        kind in _WIDENING or (kind == "cross_optin" and record.get("phase") in _ENABLE_PHASES)
    ):
        raise _refuse("cross_disable_in_progress", "disable has started; nothing may widen trust")
    step = retires(state, record)
    if step is not None:
        if limiting_control(state, control, headroom=1) is not None:
            raise _refuse("cross_closeout_record_pending", "the remaining close-out plan needs one possible repair")
        if size > PRICES[str(kind)]:
            raise _refuse("cross_record_over_price", f"{kind} is {size} bytes; its price is {PRICES[str(kind)]}")
        if used_control_bytes + size > control.bytes or used_control_slots + 1 > control.slots:
            raise _refuse("cross_control_budget_exhausted", "the control budget cannot hold this close-out step")
        return "closeout"
    if not rollover:
        _check_ordinary(state, record)
    projected = apply(state, record, size)
    after = closeout_cost(projected)
    projected_hold, _ = completion_hold(projected)
    need = Budget(max(reserve.bytes, after.bytes), max(reserve.slots, after.slots))
    frame_or_repair = max(projected.control_bytes - state.control_bytes, REPAIR_PRICE)
    fits_bytes = state.control_bytes + projected_hold.bytes + frame_or_repair + need.bytes <= control.bytes
    fits_slots = projected.control_slots + projected_hold.slots + need.slots <= control.slots
    if not fits_bytes or not fits_slots:
        if (kind in {"cross_ceremony_opened", "cross_pair_active"} and (state.activation_holds or projected.activation_holds)
            or kind == "cross_optin" and record.get("phase") in _ENABLE_PHASES and record.get("schema_version") in (1, 2)):
            limiting = "control_slots" if fits_bytes else "control_bytes"
            raise _refuse("cross_review_capacity", f"the attended evidence does not fit in {limiting}")
        raise _refuse("cross_reserve_protected", "this append would spend the credit reserved for close-out")
    return "ordinary"


def admit_compaction(
    before: ControlState, after: ControlState, control: Budget = CONTROL_BUDGET, data: Budget = DATA_BUDGET,
    *, resource: Optional[str] = "control",
) -> None:
    """A successor must keep the plan and trust, fit with the full reserve, and gain room."""

    if (obligations(before) != obligations(after) or trust_head(before) != trust_head(after)
        or before.pair_history != after.pair_history or before.activation_holds != after.activation_holds
        or before.enable_review_hold != after.enable_review_hold):
        raise _refuse("cross_compaction_projection_changed", "the successor changes close-out or trust state")
    reserve = closeout_cost(after)
    held_control, held_data = completion_hold(after)
    if (
        after.control_bytes + reserve.bytes + held_control.bytes > control.bytes
        or after.control_slots + reserve.slots + held_control.slots > control.slots
    ):
        raise _refuse("cross_reserve_protected", "the successor cannot hold the close-out reserve")
    if after.data_bytes + held_data.bytes > data.bytes or after.data_slots + held_data.slots > data.slots:
        raise _refuse("cross_storage_cap", "the successor exceeds the data budget")
    pairs = set(after.pair_data) | {delivery.pair_id for delivery in after.deliveries.values()
                                    if isinstance(delivery.pair_id, str)}
    if any(not _pair_within(after, pair, Budget(0, 0)) for pair in pairs):
        raise _refuse("cross_storage_cap", "the successor exceeds an originating pair's data budget")
    if resource is None:
        return  # A verified format/continuity transition claims no reclamation.
    before_held_control, before_held_data = completion_hold(before)
    charges = {
        "control": ((before.control_bytes + before_held_control.bytes, before.control_slots + before_held_control.slots),
                    (after.control_bytes + held_control.bytes, after.control_slots + held_control.slots)),
        "data": ((before.data_bytes + before_held_data.bytes, before.data_slots + before_held_data.slots),
                 (after.data_bytes + held_data.bytes, after.data_slots + held_data.slots)),
    }
    prior, successor = charges[resource]
    if successor[0] >= prior[0] and successor[1] >= prior[1]:
        raise _refuse("cross_compaction_no_gain", "the successor does not release the requested resource")


def retained(records: Iterable[Mapping[str, object]]) -> List[Dict[str, object]]:
    """The records a successor generation keeps, in their committed order."""

    rows = [_body(record) for record in records]
    state = project(rows)
    latest_address: Dict[str, int] = {}
    for index, row in enumerate(rows):
        if row.get("kind") == "cross_peer_address" and str(row.get("pair_id")) in state.active_pairs:
            latest_address[str(row["pair_id"])] = index
    kept: List[Dict[str, object]] = []
    for index, row in enumerate(rows):
        kind = row.get("kind")
        if kind in DATA_KINDS or kind in {"cross_inventory_item", "cross_prune_checkpoint", "cross_sender_floor"}:
            kept.append(row)
        elif kind == "cross_journal_tail_repaired" and isinstance(row.get("completion_delivery"), str):
            delivery = state.deliveries.get(str(row["completion_delivery"]))
            if delivery is not None and delivery.remaining:
                kept.append(row)
        elif kind == "cross_optin" and row.get("phase") in _ONCE_PHASES:
            kept.append(row)
        elif kind == "cross_ceremony_opened" and str(row.get("ceremony_id")) in state.open_ceremonies:
            kept.append(row)
        elif kind in {"cross_pair_active", "cross_pair_revoked"} and row["id"] in state.pair_history:
            kept.append(row)
        elif kind == "cross_pair_active" and state.active_pairs.get(str(row.get("pair_id"))) == row:
            kept.append(row)
        elif kind == "cross_peer_address" and latest_address.get(str(row.get("pair_id"))) == index:
            kept.append(row)
        elif kind == "cross_teardown_receipt" and row.get("outcome") == "complete":
            kept.append(row)
    return kept

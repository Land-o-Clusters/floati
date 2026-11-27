"""The cross journal's writer authority: one capability per entry point.

Each row names the kinds a writer may append and the field values it may
use. The journal refuses anything outside the caller's row before any write.

| writer | appends | maintains | compacts |
|---|---|---|---|
| I ingress (listener, `endpoint --stdio`, frames inside a dialler's session) | its session's data kinds, for its own pair | no: defers while maintenance is due | no |
| A attended (`enable`, `pair show`/`join`) | enable phases, inventory, ceremony open and abort, activation | yes, as M | no |
| L local explicit (`unpair`, `disable`, `peer address`, dialler, presenter, `ack`) | revocation, disable phases, teardown receipt, address, outbound kinds | yes, as M | yes |
| M maintenance | crash closure, reconstructed revocation, reconstructed `disable_started` | yes | yes |
| G governed repair | nothing at this layer: a damaged committed prefix fences the root | no | no |

Restart rules this module and `cross_journal` implement:

- **Repair** (tail, unsealed checkpoint, pending intent) is maintenance.
  An I writer that finds it due is refused `cross_maintenance_due` and
  writes nothing. A or L run it under M's authority, and the census
  records both writers. Repair is deterministic, so any process that
  resumes it reaches the same bytes.
- **Compaction** is L or M. It is intent-first, its successor is a pure
  function of the committed generation, and any lock holder finishes a
  pending one.
- **Partial shutdown** is L only. Each close-out step is decided inside the
  lock from committed state and has an id every process derives the same
  way. A resumed or racing shutdown therefore appends each step exactly
  once. M may record a reconstructed `disable_started`, which fences the
  root, but finishing disable needs L.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Union

from .errors import ProtocolRefusal


@dataclass(frozen=True)
class Capability:
    writer: str
    pair_id: Optional[str] = None
    verb: Optional[str] = None
    ceremony_token: Optional[str] = None
    session: object = None


@dataclass(frozen=True)
class Row:
    kinds: Mapping[str, Mapping[str, FrozenSet[object]]]
    maintains: bool
    compacts: bool
    closes_out: bool = False


_ANY: Dict[str, FrozenSet[object]] = {}


def _only(**fields: object) -> Dict[str, FrozenSet[object]]:
    return {name: frozenset(values) for name, values in fields.items()}  # type: ignore[arg-type]


TABLE: Mapping[str, Row] = {
    "I": Row(
        kinds={
            "remote_inbound": _ANY,
            "remote_peer_receipt": _ANY,
            "remote_receipt_confirmed": _ANY,
            "remote_session_opened": _ANY,
            "remote_session_refused": _ANY,
            "remote_reconcile_frontier": _ANY,
            "remote_unpair_notice_received": _ANY,
            "cross_pair_seen": _ANY,
            "remote_attempt": _ANY,
            "remote_transport_attempt": _ANY,
            "remote_endpoint_registered": _ANY,
        },
        maintains=False,
        compacts=False,
    ),
    "A": Row(
        kinds={
            "cross_optin": _only(phase={"enable_started", "enable_complete"}, cause={None}),
            "cross_inventory_item": _ANY,
            "cross_ceremony_opened": _ANY,
            "cross_ceremony_closed": _only(cause={"abort"}),
            "cross_pair_active": _ANY,
            "cross_pair_seen": _ANY,
        },
        maintains=True,
        compacts=False,
    ),
    "L": Row(
        kinds={
            "cross_pair_revoked": _only(cause={"local", "disable"}),
            "cross_optin": _only(
                phase={"disable_started", "disable_incomplete", "disable_complete"}, cause={"local", None}
            ),
            "cross_ceremony_closed": _only(cause={"disabled"}),
            "cross_teardown_receipt": _ANY,
            "cross_peer_address": _ANY,
            "remote_outbound": _ANY,
            "remote_attempt": _ANY,
            "remote_transport_attempt": _ANY,
            "remote_presented": _ANY,
            "remote_local_ack": _ANY,
        },
        maintains=True,
        compacts=True,
        closes_out=True,
    ),
    "M": Row(
        kinds={
            "cross_ceremony_closed": _only(cause={"crash"}),
            "remote_hold_rejected": _ANY,
            "cross_pair_revoked": _only(cause={"key_absent_reconstructed"}),
            "cross_optin": _only(phase={"disable_started"}, cause={"anchor_absent_reconstructed"}),
        },
        maintains=True,
        compacts=True,
    ),
    "G": Row(kinds={}, maintains=False, compacts=False),
}


_VERBS = {
    "I": {
        "envelope": {"remote_inbound"},
        "receipt": {"remote_peer_receipt"},
        "receipt_ack": {"remote_receipt_confirmed"},
        "admission": {"remote_session_opened", "remote_session_refused", "cross_pair_seen"},
        "open": {"remote_session_opened"},
        "endpoint_start": {"remote_endpoint_registered"},
        "reconcile": {"remote_attempt", "remote_transport_attempt", "remote_reconcile_frontier"},
        "refusal": {"remote_transport_attempt"},
        "notice": {"remote_unpair_notice_received"},
    },
    "A": {
        "enable": {"cross_optin", "cross_inventory_item"},
        "pair_show": {"cross_ceremony_opened", "cross_ceremony_closed", "cross_pair_active", "cross_pair_seen"},
        "pair_join": {"cross_ceremony_opened", "cross_ceremony_closed", "cross_pair_active"},
    },
    "L": {
        "send": {"remote_outbound", "remote_attempt", "remote_transport_attempt"},
        "ask": {"remote_outbound", "remote_attempt", "remote_transport_attempt"},
        "present": {"remote_presented"},
        "ack": {"remote_local_ack"},
        "peer_address": {"cross_peer_address"},
        "unpair": {"cross_pair_revoked"},
        "disable": {"cross_optin", "cross_pair_revoked", "cross_ceremony_closed", "cross_teardown_receipt"},
    },
    "M": {"maintain": {"cross_ceremony_closed", "cross_pair_revoked", "cross_optin"},
          "hold_accounting": {"remote_hold_rejected"}},
    "G": {},
}


def _nested_pairs(value):
    if isinstance(value, Mapping):
        if "pair_id" in value:
            yield value["pair_id"]
        for child in value.values():
            yield from _nested_pairs(child)
    elif isinstance(value, list):
        for child in value:
            yield from _nested_pairs(child)


def as_capability(value: Union[str, Capability]) -> Capability:
    capability = value if isinstance(value, Capability) else Capability(str(value))
    if capability.writer not in TABLE:
        raise ProtocolRefusal("cross_capability_refused", f"{capability.writer!r} is not a cross writer")
    return capability


def row(capability: Capability) -> Row:
    return TABLE[capability.writer]


def check(capability: Capability, record: Mapping[str, object]) -> None:
    """Refuse a record outside the writer's row, on kind and on field values."""

    kind = record.get("kind")
    constraints = row(capability).kinds.get(str(kind))
    if constraints is None or kind not in _VERBS[capability.writer].get(capability.verb, ()):
        raise ProtocolRefusal("cross_capability_refused", f"writer {capability.writer} may not append {kind}")
    for name, allowed in constraints.items():
        if record.get(name) not in allowed:
            raise ProtocolRefusal(
                "cross_capability_refused",
                f"writer {capability.writer} may not append {kind} with {name}={record.get(name)!r}",
            )
    if capability.writer == "L" and kind == "cross_pair_revoked":
        cause = "local" if capability.verb == "unpair" else "disable"
        if record.get("cause") != cause:
            raise ProtocolRefusal("cross_capability_refused", "the revocation cause must match the local verb")
    scoped_ingress = capability.writer == "I" and capability.verb != "endpoint_start"
    if scoped_ingress or capability.pair_id is not None:
        if (
            not capability.pair_id or record.get("pair_id") != capability.pair_id
            or any(pair != capability.pair_id for pair in _nested_pairs(record))
        ):
            raise ProtocolRefusal("cross_pair_scope_refused", "a scoped writer writes only for its own pair")
    elif capability.writer == "I" and any(_nested_pairs(record)):
        raise ProtocolRefusal("cross_pair_scope_refused", "endpoint registration is root-scoped")
    if capability.writer == "A" and capability.verb in {"pair_show", "pair_join"}:
        if not capability.ceremony_token or record.get("ceremony_token") != capability.ceremony_token:
            raise ProtocolRefusal("cross_ceremony_token_invalid", "the attended action requires its live ceremony token")


def check_committed(
    capability: Capability, record: Mapping[str, object], committed: Iterable[Mapping[str, object]]
) -> None:
    """Ingress can record an attempt only for its own committed outbound."""

    if capability.writer != "I" or record.get("kind") != "remote_attempt":
        return
    pair = record.get("pair_id", capability.pair_id)
    envelope = record.get("envelope_id")
    if isinstance(pair, str) and pair and isinstance(envelope, str) and envelope and any(
        row.get("kind") == "remote_outbound"
        and row.get("pair_id") == pair
        and row.get("envelope_id") == envelope
        for row in committed
    ):
        return
    raise ProtocolRefusal(
        "cross_capability_refused", "an ingress attempt requires a committed outbound for its pair and envelope"
    )


def require(capability: Capability, power: str) -> None:
    if not getattr(row(capability), power):
        raise ProtocolRefusal("cross_capability_refused", f"writer {capability.writer} may not {power}")


__all__ = ["Capability", "Row", "TABLE", "as_capability", "check", "check_committed", "require", "row"]

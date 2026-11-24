"""NET-HOLD-INBOUND-1: hold a node's inbound mail until a person decides each item.

An operator starts an inbound hold on one node. Every message envelope to that
node whose timestamp falls inside an open or closed hold interval is a held
item. A held item is withheld from every presentation path until a person
approves it at a controlling terminal; a rejected item is never presented, and
releasing the hold never presents an item that was held. Mail sent before a
hold started, or after it was released, is ordinary mail.

The presentation gate is the four functions named in ``PRESENTATION_GATES``.
Each presenter primitive calls one of them in one line; the derived test in
``tests/test_net_hold_inbound_1.py`` proves every writer of a presentation
record reaches one.

Decisions, and releasing a hold, are human acts. They are taken only through
this module's operator entry point, never through the seat-reachable CLI, a
gateway, MCP, a hook, a waiter or a wake daemon:

    python3 -m floati.inbound_hold approve --root ROOT --node NODE --message ID --idempotency-key KEY

Each act renders exactly what it decides on the controlling terminal and
records the person's typed confirmation of a one-time code shown there, with
the digest of the rendered panel. No flag, environment variable,
configuration row or test seam stands in for that act, and it is refused from
inside a declared seat workspace. Starting a hold only withholds mail, so it
needs no terminal and automation may start one.

Every row is validated on read exactly as the writer validates it. A row the
writer would refuse makes the node's whole history uncertain, and uncertain
history withholds all of that node's mail until a person repairs it.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import sys
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .framing import FrameError, decode_frames, encode_frame
from .ids import uuid7_hex
from .inbound_hold_records import (DECISIONS, HUMAN_ACT_CHANNEL, KIND, PREFIX,
                                   validate_inbound_hold_record)
from .jsonl import (MAX_LEDGER_BYTES, MAX_LEDGER_RECORDS, MAX_RECORD_BYTES,
                    _append_frame, _encode_record, _locked_path)
from .registry import Registry, utc_now
from .root import FloatiRoot, validate_identifier


PRESENTATION_GATES = (
    "withhold_unapproved",
    "withhold_unapproved_selection",
    "withhold_unapproved_states",
    "require_presentable",
    "mask_held_envelopes",
)
LEDGER = Path("receipts/inbound-holds")
OPERATOR_ENTRY = ("python3", "-m", "floati.inbound_hold")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_ANSWER_BYTES = 256


def _relative(node: str) -> Path:
    return LEDGER / (validate_identifier(node, "node") + ".jsonl")


def _time(value: object) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError):
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo is not None else None


def envelope_digest(message: Dict[str, object]) -> str:
    """SHA-256 of the exact native frame of one envelope."""
    return hashlib.sha256(encode_frame(message)).hexdigest()


def _invalid(code, detail):
    raise ValueError(code)


def _read(root: FloatiRoot, node: str, *, through=None) -> Tuple[List[dict], int, bool]:
    """No lock creation, no rewrite. A row the writer would refuse is counted
    unreadable, and any unreadable row makes the whole node's mail withheld."""
    path = root.resolve_relative(_relative(node))
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_LEDGER_BYTES + 1)
    except FileNotFoundError:
        return [], 0, False
    if through is None and len(data) > MAX_LEDGER_BYTES:
        return [], 1, True
    rows, partial, ids = [], 0, set()
    for index, raw in enumerate(data.splitlines(keepends=True)):
        if through is not None and index >= through:
            break
        if index >= MAX_LEDGER_RECORDS:
            partial += 1
            break
        try:
            if len(raw) > MAX_RECORD_BYTES:
                raise ValueError("oversize")
            row, = decode_frames(raw)
            validate_inbound_hold_record(row, root.tenant_id, _invalid)
            if row["id"] in ids or row["node_id"] != node:
                raise ValueError("unattributable inbound hold row")
            ids.add(row["id"])
            rows.append(row)
        except (FrameError, ValueError, TypeError):
            partial += 1
    return rows, partial, True


@dataclass(frozen=True)
class _Policy:
    intervals: Tuple[Tuple[datetime, Optional[datetime], str], ...]
    decisions: Dict[str, Tuple[dict, ...]]
    uncertain: bool

    def interval(self, message: Dict[str, object]) -> Optional[str]:
        moment = _time(message.get("timestamp"))
        for start, end, hold_id in self.intervals:
            if moment is None or (start <= moment and (end is None or moment < end)):
                return hold_id
        return None

    def withheld(self, message: Dict[str, object]) -> bool:
        if self.uncertain:
            return True
        hold_id = self.interval(message)
        if hold_id is None:
            return False
        return self.decision(message, hold_id) != "approved"

    def decision(self, message, hold_id):
        decided = self.decisions.get(str(message.get("id")), ())
        if (not self.uncertain and hold_id is not None and len(decided) == 1
                and decided[0]["hold_id"] == hold_id
                and decided[0]["envelope_digest"] == envelope_digest(message)):
            return decided[0]["phase"]
        return None


def _policy_from(rows: Sequence[dict], partial: int) -> _Policy:
    starts = [row for row in rows if row["phase"] == "held"]
    releases = {}
    for row in rows:
        if row["phase"] == "released":
            releases.setdefault(row.get("hold_id"), row)
    intervals, uncertain = [], bool(partial)
    for start in starts:
        begin = _time(start.get("started_at"))
        release = releases.get(start["id"])
        end = None if release is None else _time(release.get("released_at"))
        if begin is None or (release is not None and end is None):
            uncertain = True
            continue
        intervals.append((begin, end, start["id"]))
    decisions: Dict[str, list] = {}
    for row in rows:
        if row["phase"] in DECISIONS:
            decisions.setdefault(str(row.get("message_id")), []).append(row)
    return _Policy(tuple(intervals), {key: tuple(value) for key, value in decisions.items()},
                   uncertain)


def _policy(root: FloatiRoot, node: str) -> Optional[_Policy]:
    """None when the node has never been held; uncertain history withholds all."""
    try:
        rows, partial, exists = _read(root, str(node))
    except (OSError, ProtocolRefusal):
        return _Policy((), {}, True)
    if not exists:
        return None
    return _policy_from(rows, partial)


def terminal_rejections(root, node, messages, *, cutoff=None):
    """Prove exact rejected envelopes from one preserved inbound-hold prefix."""
    try:
        rows, partial, _exists = _read(root, node, through=None if cutoff is None else cutoff["count"])
    except (OSError, ProtocolRefusal):
        rows, partial = [], 1
    digest = hashlib.sha256()
    for row in rows:
        digest.update(encode_frame(row))
    proof = {"count": len(rows), "digest": digest.hexdigest()}
    if cutoff is not None and (partial or proof != cutoff):
        raise IntegrityFailure("seat_teardown_native_invalid",
            "inbound hold ledger does not preserve the saved rejection cutoff")
    policy = _policy_from(rows, partial)
    rejected = {message["id"] for message in messages
                if message["recipient"] == node
                and policy.decision(message, policy.interval(message)) == "rejected"}
    return rejected, proof


# ---------------------------------------------------------------------------
# The presentation gate. One line at each presenter primitive.
# ---------------------------------------------------------------------------

def withhold_unapproved(root: FloatiRoot, node: str, messages: Sequence[dict]) -> List[dict]:
    """Return the messages a presenter may show; held unapproved items drop out."""
    policy = _policy(root, node)
    if policy is None:
        return list(messages)
    return [message for message in messages if not policy.withheld(message)]


def withhold_unapproved_selection(root: FloatiRoot, node: str, selection: dict) -> dict:
    """Filter one conversation-plane selection, keeping its eligible order exact."""
    messages = selection["fresh_messages"]
    if not messages:
        return selection
    kept = withhold_unapproved(root, node, messages)
    if len(kept) == len(messages):
        return selection
    return dict(selection, fresh_messages=kept, fresh_total=len(kept),
                _eligible_ids=tuple(message["id"] for message in kept))


def withhold_unapproved_states(root: FloatiRoot, node: str, *groups):
    """Filter wake item states (fresh, held) before a wake decision is made."""
    policy = _policy(root, node)
    if policy is None:
        return groups
    return tuple([item for item in group if not policy.withheld(item.message)]
                 for group in groups)


def require_presentable(root: FloatiRoot, node: str, messages: Sequence[dict]) -> None:
    """Refuse before any presentation record names a held unapproved item."""
    policy = _policy(root, node)
    if policy is None:
        return
    held = [str(message.get("id")) for message in messages if policy.withheld(message)]
    if held:
        raise ProtocolRefusal(
            "inbound_hold_unapproved",
            "an inbound hold withholds " + ", ".join(held[:10])
            + (" and more" if len(held) > 10 else "") + " until a person decides each item",
            remedy="A person approves or rejects each held item at a terminal: "
            + shlex.join([*OPERATOR_ENTRY, "show", "--root", str(root.path), "--node", str(node)]))


# ---------------------------------------------------------------------------
# The display gate: read views show a held item as HELD, never its body.
# ---------------------------------------------------------------------------

def _unframed(message: dict) -> dict:
    """Presentation decorations are not envelope bytes."""
    from .colleague_frame import unframed_messages

    return unframed_messages([message])[0]


def _held_view(message: dict, policy: _Policy) -> dict:
    envelope = _unframed(message)
    decided = policy.decisions.get(str(envelope.get("id")), ())
    last = decided[-1] if decided else None
    state = ("history_uncertain" if policy.uncertain
             else "rejected" if last is not None and last["phase"] == "rejected" else "held")
    return {
        "kind": "message_envelope", "id": envelope.get("id"),
        "sender": envelope.get("sender"), "recipient": envelope.get("recipient"),
        "timestamp": envelope.get("timestamp"),
        "inbound_hold": {
            "state": state, "hold_id": None if policy.uncertain else policy.interval(envelope),
            "envelope_digest": envelope_digest(envelope),
            "decision_id": None if last is None else last["id"],
            "decided_at": None if last is None else last["decided_at"],
        },
    }


def mask_held_envelopes(roots, value):
    """Return ``value`` with every held, rejected or unprovable envelope replaced
    by its held view: id, sender, recipient, time, digest and hold, never its
    note or other body fields.

    ``roots`` is one root, or a callable mapping an envelope's tenant to the
    roots it may come from. An envelope whose hold state cannot be read (no
    root for its tenant, an invalid recipient, unreadable history) is shown as
    unprovable: the gate fails closed."""
    unprovable = _Policy((), {}, True)
    policies: Dict[Tuple[str, str], List[_Policy]] = {}

    def candidates(tenant):
        if isinstance(roots, FloatiRoot):
            return [roots] if roots.tenant_id == tenant else []
        return list(roots(tenant)) if callable(roots) else []

    def policy_list(tenant, node):
        key = (tenant, node)
        if key not in policies:
            selected = []
            found = candidates(tenant)
            try:
                node = validate_identifier(node, "node")
            except ProtocolRefusal:
                found = []
            for root in found:
                current = _policy(root, node)
                if current is not None:
                    selected.append(current)
            policies[key] = selected if found else [unprovable]
        return policies[key]

    def walk(item):
        if isinstance(item, dict):
            if (item.get("kind") == "message_envelope" and isinstance(item.get("id"), str)
                    and isinstance(item.get("recipient"), str)):
                envelope = _unframed(item)
                for selected in policy_list(str(item.get("tenant_id")), item["recipient"]):
                    if selected.withheld(envelope):
                        return _held_view(item, selected)
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, tuple):
            return tuple(walk(child) for child in item)
        return item

    return walk(value)


def held_item_marks(root: FloatiRoot) -> List[dict]:
    """Every item an inbound hold withholds, for audit exports that keep raw bytes."""
    try:
        directory = root.resolve_relative(LEDGER)
        paths = sorted(directory.glob("*.jsonl")) if directory.exists() else []
    except (OSError, ProtocolRefusal):
        return [{"message_id": None, "recipient": None, "state": "history_uncertain",
                 "hold_id": None, "envelope_digest": None, "decision_id": None}]
    marks = []
    for path in paths:
        node = path.stem
        try:
            selected = _policy(root, validate_identifier(node, "node"))
            envelopes = _envelopes(root, node) if selected is not None else []
        except (OSError, ProtocolRefusal, IntegrityFailure):
            marks.append({"message_id": None, "recipient": node, "state": "history_uncertain",
                          "hold_id": None, "envelope_digest": None, "decision_id": None})
            continue
        for message in envelopes:
            if selected.withheld(message):
                view = _held_view(message, selected)["inbound_hold"]
                marks.append({"message_id": message["id"], "recipient": node,
                              "state": view["state"], "hold_id": view["hold_id"],
                              "envelope_digest": view["envelope_digest"],
                              "decision_id": view["decision_id"]})
    return marks


def rejected_message_ids(root: FloatiRoot, node: str) -> frozenset:
    """Items a person rejected: terminal, never presented, never acknowledged.
    Unprovable history rejects nothing, so a drain stays refused (fail closed)."""
    policy = _policy(root, node)
    if policy is None or policy.uncertain:
        return frozenset()
    return frozenset(message_id for message_id, rows in policy.decisions.items()
                     if len(rows) == 1 and rows[0]["phase"] == "rejected")


def with_inbound_hold(root: FloatiRoot, node: dict, now: Optional[datetime] = None) -> dict:
    """Board model: add the node's inbound hold summary only when it has a ledger."""
    node_id = str(node.get("node_id"))
    try:
        path = root.resolve_relative(_relative(node_id))
        if not path.exists():
            return node
        projection = inbound_hold_projection(root, node_id, now)
    except (OSError, ProtocolRefusal, IntegrityFailure):
        return dict(node, inbound_holds={"holds": [], "awaiting": 0, "history_uncertain": True})
    return dict(node, inbound_holds={
        "holds": [hold for hold in projection["holds"] if hold["open"]],
        "awaiting": len(projection["awaiting_decision"]),
        "history_uncertain": projection["history_uncertain"]})


def inbound_hold_lines(summary: Optional[dict]) -> List[str]:
    if not isinstance(summary, dict):
        return []
    if summary.get("history_uncertain"):
        return ["INBOUND HOLD history unreadable; all mail to this node is withheld"]
    waiting = str(summary.get("awaiting", 0)) + " awaiting a person"
    lines = ["INBOUND HOLD " + json.dumps(hold["id"]) + " "
             + ("unknown age" if hold["age_seconds"] is None else str(hold["age_seconds"]) + "s old")
             + " " + json.dumps(hold["reason"], ensure_ascii=True) + " " + waiting
             for hold in summary.get("holds", ())]
    if not lines and summary.get("awaiting"):
        lines.append("INBOUND HOLD released; " + waiting)
    return lines


# ---------------------------------------------------------------------------
# Read-only projection and doctor findings.
# ---------------------------------------------------------------------------

def _envelopes(root: FloatiRoot, node: str) -> List[dict]:
    from .inbox_prefix import recipient_frames
    frames = recipient_frames(root, node, read_only=True)
    retracted = {str(row.get("retracted_message_id")) for row in frames
                 if row.get("kind") == "message_retracted"}
    return [row for row in frames if row.get("kind") == "message_envelope"
            and row.get("recipient") == node and row.get("id") not in retracted]


def inbound_hold_projection(root: FloatiRoot, node: str, now: Optional[datetime] = None) -> dict:
    current = datetime.now(timezone.utc) if now is None else now
    node = validate_identifier(node, "node")
    rows, partial, exists = _read(root, node)
    policy = _policy_from(rows, partial)
    released = {row.get("hold_id"): row for row in rows if row["phase"] == "released"}
    holds = []
    for row in rows:
        if row["phase"] != "held":
            continue
        started = _time(row.get("started_at"))
        holds.append({
            "id": row["id"], "reason": row.get("reason"), "started_at": row.get("started_at"),
            "expected_release": row.get("expected_release"),
            "released_at": released.get(row["id"], {}).get("released_at"),
            "open": row["id"] not in released,
            "age_seconds": (None if started is None or started > current
                            else int((current - started).total_seconds())),
        })
    awaiting, decided = [], []
    if exists:
        for message in _envelopes(root, node):
            hold_id = policy.interval(message)
            if hold_id is None:
                continue
            rows_for = policy.decisions.get(str(message["id"]), ())
            if rows_for:
                decided.append({"message_id": message["id"], "phase": rows_for[-1]["phase"],
                                "decision_id": rows_for[-1]["id"],
                                "decided_at": rows_for[-1].get("decided_at")})
            else:
                awaiting.append({"message_id": message["id"], "hold_id": hold_id,
                                 "sender": message.get("sender"),
                                 "timestamp": message.get("timestamp"),
                                 "envelope_digest": envelope_digest(message)})
    return {"node_id": node, "holds": holds, "awaiting_decision": awaiting,
            "decided": decided, "partial_records": partial,
            "history_uncertain": policy.uncertain}


def _finding(code, severity, node, detail, remedy=None):
    return {"code": code, "severity": severity, "subject": node,
            "detail": detail, "remediation": remedy}


def project_inbound_hold_findings(root: FloatiRoot) -> List[dict]:
    """Doctor's view: open holds, items waiting for a person, unreadable history."""
    try:
        directory = root.resolve_relative(LEDGER)
        if not directory.exists():
            return []
        paths = sorted(directory.glob("*.jsonl"))
    except (OSError, ProtocolRefusal):
        return [_finding("inbound_hold_ledger_partial", "warning", "inbound-holds",
                         "inbound hold history unavailable")]
    findings = []
    for path in paths:
        node = path.stem
        try:
            validate_identifier(node, "node")
            projection = inbound_hold_projection(root, node)
        except (OSError, ProtocolRefusal, IntegrityFailure):
            findings.append(_finding("inbound_hold_ledger_partial", "warning", node,
                                     "inbound hold history unavailable; all mail to this node is withheld"))
            continue
        waiting = [item["message_id"] for item in projection["awaiting_decision"]]
        remedy = None
        if waiting:
            remedy = shlex.join([*OPERATOR_ENTRY, "approve", "--root", str(root.path),
                                 "--node", node, "--message", waiting[0],
                                 "--idempotency-key", "inbound-decision-" + uuid7_hex()])
        opened = [hold for hold in projection["holds"] if hold["open"]]
        detail = "; ".join(
            ["INBOUND HOLD " + json.dumps(hold["id"]) + " "
             + ("unknown age" if hold["age_seconds"] is None else str(hold["age_seconds"]) + "s old")
             + " " + json.dumps(hold["reason"], ensure_ascii=True) for hold in opened]
            + ([str(len(waiting)) + " item(s) awaiting a person: " + ", ".join(waiting[:20])]
               if waiting else []))
        if opened:
            findings.append(_finding("inbound_hold_open", "warning" if waiting else "info",
                                     node, detail, remedy))
        elif waiting:
            findings.append(_finding("inbound_hold_awaiting_decision", "warning", node,
                                     detail, remedy))
        if projection["partial_records"] or projection["history_uncertain"]:
            findings.append(_finding(
                "inbound_hold_ledger_partial", "warning", node,
                str(projection["partial_records"]) + " unreadable inbound hold record(s); "
                "all mail to this node is withheld until the history is readable"))
    return findings


# ---------------------------------------------------------------------------
# Authoring: the operator entry point is the only writer.
# ---------------------------------------------------------------------------

def _row(root, node, phase, key, **fields) -> dict:
    now = utc_now()
    row = {"schema_version": 1, "id": PREFIX + uuid7_hex(), "tenant_id": root.tenant_id,
           "timestamp": now, "kind": KIND, "node_id": node, "phase": phase,
           "hold_id": None, "reason": None, "started_at": None, "expected_release": None,
           "released_at": None, "message_id": None, "envelope_digest": None,
           "decided_at": None, "human_act": None, "idempotency_key": key}
    row.update(fields)
    for field in ("started_at", "released_at", "decided_at"):
        if row[field] == "now":
            row[field] = now
    return row


def _append(root: FloatiRoot, node: str, decide):
    """Run ``decide(rows)`` under the node's ledger lock and append what it returns."""
    relative = _relative(node)
    path = root.resolve_relative(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked_path(path.with_name(path.name + ".lock"), exclusive=True,
                      relative=relative.as_posix() + ".lock"):
        rows, partial, _exists = _read(root, node)
        if partial:
            raise ProtocolRefusal(
                "inbound_hold_history_uncertain",
                "authoring needs complete inbound hold history; reads stay available",
                remedy="Inspect the node's inbound hold ledger and its doctor finding; "
                "preserve the existing history.")
        candidate, replay = decide(rows)
        if replay is not None:
            return replay
        if len(rows) >= MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", "inbound hold ledger record bound reached")
        encoded = _encode_record(candidate, root.tenant_id, frozenset({KIND}),
                                 max_bytes=MAX_RECORD_BYTES)
        if path.exists() and path.stat().st_size + len(encoded) > MAX_LEDGER_BYTES:
            raise ProtocolRefusal("ledger_byte_limit", "inbound hold ledger byte bound reached")
        _append_frame(path, encoded)
        return candidate


def _replay(rows, key, matches):
    prior = next((row for row in rows if row.get("idempotency_key") == key), None)
    if prior is None:
        return None
    if not matches(prior):
        raise ProtocolRefusal(
            "inbound_hold_key_conflict", "idempotency key already names a different inbound hold act",
            remedy="Replay the original request with this key, or choose a fresh key.")
    return prior


def _require_node(root: FloatiRoot, node: str) -> str:
    node = validate_identifier(node, "node")
    try:
        Registry(root).require_active(node)
    except ProtocolRefusal:
        raise ProtocolRefusal("inbound_hold_node_unknown",
                              "an inbound hold needs a registered active node",
                              remedy="Name a registered active node with --node.") from None
    return node


def _open_holds(rows):
    released = {row.get("hold_id") for row in rows if row["phase"] == "released"}
    return [row for row in rows if row["phase"] == "held" and row["id"] not in released]


def start_hold(root: FloatiRoot, node: str, reason: str, key: str,
               expected_release: Optional[str] = None) -> dict:
    node = _require_node(root, node)
    if expected_release is not None and _time(expected_release) is None:
        raise ProtocolRefusal("inbound_hold_expected_release_invalid",
                              "expected release must be a UTC timestamp",
                              remedy="Pass --expected-release as a UTC timestamp ending in Z, or omit it.")

    def decide(rows):
        prior = _replay(rows, key, lambda row: row["phase"] == "held" and row.get("reason") == reason
                        and row.get("expected_release") == expected_release)
        if prior is not None:
            return None, prior
        opened = _open_holds(rows)
        if opened:
            raise ProtocolRefusal(
                "inbound_hold_already_open", "this node's inbound mail is already held by " + opened[0]["id"],
                remedy="Keep the open hold, or release it first: " + shlex.join(
                    [*OPERATOR_ENTRY, "release", "--root", str(root.path), "--node", node,
                     "--idempotency-key", "inbound-release-" + uuid7_hex()]))
        return _row(root, node, "held", key, reason=reason, started_at="now",
                    expected_release=expected_release), None

    return _append(root, node, decide)


def release_hold(root: FloatiRoot, node: str, key: str, hold: Optional[str] = None,
                 *, cwd: Optional[Path] = None) -> dict:
    """Releasing is a human act too: the held node must never lift its own hold."""
    node = _require_node(root, node)
    _refuse_seat_context(root, cwd)
    rows = _complete_rows(root, node)
    matches = lambda row: row["phase"] == "released" and (hold is None or row["hold_id"] == hold)
    prior = _replay(rows, key, matches)
    if prior is not None:
        return prior
    opened = [row for row in _open_holds(rows) if hold is None or row["id"] == hold]
    if not opened:
        raise ProtocolRefusal("inbound_hold_none_open", "this node has no open inbound hold"
                              + ("" if hold is None else " named " + hold),
                              remedy="Name a node with an open inbound hold, or replay a release by its key.")
    selected = opened[0]
    waiting = len(inbound_hold_projection(root, node)["awaiting_decision"])
    lines = [
        "Inbound hold " + selected["id"] + " has held mail to " + node
        + " since " + str(selected["started_at"]) + ".",
        "Reason: " + json.dumps(selected["reason"], ensure_ascii=True),
        "Releasing it ends the hold for mail sent from now on.",
        "The " + str(waiting) + " item(s) it held still need a decision each.",
    ]

    def decide(current, rendered):
        again = _replay(current, key, matches)
        if again is not None:
            return None, again
        if selected["id"] not in {row["id"] for row in _open_holds(current)}:
            raise ProtocolRefusal("inbound_hold_changed", "the hold changed while it was shown",
                                  remedy="Run the command again to see the current hold.")
        return _row(root, node, "released", key, hold_id=selected["id"], released_at="now",
                    human_act={"channel": HUMAN_ACT_CHANNEL, "rendered_digest": rendered}), None

    return _confirmed_append(root, node, "release", lines, decide)


# ---------------------------------------------------------------------------
# Decisions: a person at a controlling terminal, outside every seat workspace.
# ---------------------------------------------------------------------------

def _seat_context(root: FloatiRoot, cwd: Path) -> Optional[str]:
    from .seat_declaration import SEAT_MARKER
    for directory in (cwd, *cwd.parents):
        marker = directory / SEAT_MARKER
        if marker.exists() or marker.is_symlink():
            return "the working directory is inside a declared seat workspace"
    try:
        from .codex_wait_contract import resolve_workspace_binding
        for directory in (cwd, *cwd.parents):
            if resolve_workspace_binding(root.path, directory, _strict=True) is not None:
                return "the working directory is a mapped waiter workspace"
    except (OSError, ProtocolRefusal, IntegrityFailure):
        return "the waiter workspace map cannot be read, so a seat context cannot be ruled out"
    return None


def _terminal() -> int:
    try:
        descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        descriptor = -1
    if descriptor < 0 or not os.isatty(descriptor):
        if descriptor >= 0:
            os.close(descriptor)
        raise ProtocolRefusal(
            "inbound_hold_human_act_unavailable",
            "a decision on a held item needs a person at a controlling terminal",
            remedy="A person runs this command in their own terminal, outside every seat workspace.")
    return descriptor


def _say(descriptor: int, text: str) -> None:
    data = text.encode("utf-8")
    while data:
        written = os.write(descriptor, data)
        data = data[written:]


def _answer(descriptor: int) -> str:
    data = b""
    while b"\n" not in data and len(data) < _ANSWER_BYTES:
        chunk = os.read(descriptor, _ANSWER_BYTES)
        if not chunk:
            break
        data += chunk
    return data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()


def _refuse_seat_context(root: FloatiRoot, cwd: Optional[Path]) -> None:
    context = _seat_context(root, Path(os.getcwd() if cwd is None else cwd).resolve())
    if context is not None:
        raise ProtocolRefusal(
            "inbound_hold_decision_seat_context", "this act is never taken from a seat: " + context,
            remedy="A person runs this command from their own terminal outside every seat workspace.")


def _complete_rows(root: FloatiRoot, node: str) -> List[dict]:
    rows, partial, _exists = _read(root, node)
    if partial:
        raise ProtocolRefusal("inbound_hold_history_uncertain",
                              "this act needs complete inbound hold history",
                              remedy="Inspect the node's inbound hold ledger and its doctor finding first.")
    return rows


def _confirmed_append(root: FloatiRoot, node: str, verb: str, lines: List[str], decide) -> dict:
    """Show the exact panel at the controlling terminal; append only on its code."""
    descriptor = _terminal()
    try:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        panel = "\n".join(["", *lines, "To " + verb + " it, type this code: " + code,
                           "Anything else changes nothing.", "> "])
        _say(descriptor, panel)
        if _answer(descriptor) != code:
            _say(descriptor, "Not confirmed. Nothing changed.\n")
            raise ProtocolRefusal("inbound_hold_human_act_unconfirmed",
                                  "the typed code did not match the code shown",
                                  remedy="Run the command again and type the code it shows.")
        rendered = hashlib.sha256(panel.encode("utf-8")).hexdigest()
        recorded = _append(root, node, lambda current: decide(current, rendered))
        _say(descriptor, "Recorded " + recorded["phase"] + " " + recorded["id"] + ".\n")
        return recorded
    finally:
        os.close(descriptor)


def decide_item(root: FloatiRoot, node: str, message_id: str, key: str, action: str,
                *, cwd: Optional[Path] = None) -> dict:
    if action not in DECISIONS:
        raise ProtocolRefusal("inbound_hold_decision_invalid", "a decision is approved or rejected")
    node = _require_node(root, node)
    _refuse_seat_context(root, cwd)
    rows = _complete_rows(root, node)
    matches = lambda row: row["phase"] == action and row["message_id"] == message_id
    prior = _replay(rows, key, matches)
    if prior is not None:
        return prior
    message = next((row for row in _envelopes(root, node) if row.get("id") == message_id), None)
    if message is None:
        raise ProtocolRefusal("inbound_hold_item_unknown", "no envelope to this node has that id",
                              remedy="List held items: " + shlex.join(
                                  [*OPERATOR_ENTRY, "show", "--root", str(root.path), "--node", node]))
    policy = _policy_from(rows, 0)
    hold_id = policy.interval(message)
    if hold_id is None:
        raise ProtocolRefusal("inbound_hold_item_not_held", "this item was not sent during an inbound hold",
                              remedy="No decision is needed for this item; only items sent while a hold was open need one.")
    if policy.decisions.get(message_id):
        raise ProtocolRefusal("inbound_hold_already_decided", "this item already has a decision",
                              remedy="A decision is final; replay it by its original key.")
    digest = envelope_digest(message)
    lines = [
        "Inbound hold " + hold_id + " is holding this item for " + node + ":",
        json.dumps(message, ensure_ascii=True, sort_keys=True, indent=2),
        "Envelope sha256 " + digest,
        ("Approving presents this item to " + node + " at its next presentation."
         if action == "approved" else "Rejecting means this item is never presented to " + node + "."),
    ]

    def decide(current, rendered):
        again = _replay(current, key, matches)
        if again is not None:
            return None, again
        if any(row["phase"] in DECISIONS and row["message_id"] == message_id for row in current):
            raise ProtocolRefusal("inbound_hold_already_decided", "this item already has a decision",
                                  remedy="A decision is final; replay it by its original key.")
        if _policy_from(current, 0).interval(message) != hold_id:
            raise ProtocolRefusal("inbound_hold_changed", "the hold changed while the item was shown",
                                  remedy="Run the command again to see the current hold.")
        return _row(root, node, action, key, hold_id=hold_id, message_id=message_id,
                    envelope_digest=digest, decided_at="now",
                    human_act={"channel": HUMAN_ACT_CHANNEL, "rendered_digest": rendered}), None

    return _confirmed_append(root, node, "approve" if action == "approved" else "reject", lines, decide)


# ---------------------------------------------------------------------------
# Operator entry point.
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m floati.inbound_hold", allow_abbrev=False,
        description="Hold a node's inbound mail until a person approves or rejects each item.")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("start", "release", "show", "approve", "reject"):
        command = actions.add_parser(action, allow_abbrev=False)
        command.add_argument("--root", required=True, metavar="ROOT")
        command.add_argument("--node", required=True, metavar="NODE")
        if action != "show":
            command.add_argument("--idempotency-key", required=True, metavar="KEY")
        if action == "start":
            command.add_argument("--reason", required=True, metavar="TEXT")
            command.add_argument("--expected-release", metavar="TIMESTAMP")
        elif action == "release":
            command.add_argument("--hold", metavar="HOLD")
        elif action in ("approve", "reject"):
            command.add_argument("--message", required=True, metavar="ID")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    from .root import resolve_command_root
    args = _parser().parse_args(argv)
    command = "inbound-hold " + args.action
    try:
        root = resolve_command_root(args.root)
        if args.action == "start":
            evidence = start_hold(root, args.node, args.reason, args.idempotency_key,
                                  args.expected_release)
        elif args.action == "release":
            evidence = release_hold(root, args.node, args.idempotency_key, args.hold)
        elif args.action == "show":
            evidence = inbound_hold_projection(root, args.node)
        else:
            evidence = decide_item(root, args.node, args.message, args.idempotency_key,
                                   "approved" if args.action == "approve" else "rejected")
        status, code = "ok", 0
    except ProtocolRefusal as exc:
        status, code = "refused", 20
        evidence = {"code": exc.code, "detail": exc.detail, "remedy": exc.remedy}
    except (IntegrityFailure, DurabilityFailure) as exc:
        status, code = "malformed_evidence", 33
        evidence = {"code": getattr(exc, "code", "integrity_failure"), "detail": str(exc)}
    sys.stdout.write(json.dumps({"artifact_version": 0, "command": command, "status": status,
                                 "evidence": evidence}, sort_keys=True) + "\n")
    sys.stdout.flush()
    return code


if __name__ == "__main__":
    raise SystemExit(main())

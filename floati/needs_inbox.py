"""The needs-you inbox (NEEDS-YOU-INBOX-1) and its attention budget (ATTENTION-BUDGET-1).

An agent, started by any orchestrator, a harness's own subagents or a plain
shell, raises an item for a person with ``floati needs file`` (CLI or MCP).
Every item is one bound record (the bound-record contract, version 1): an
append-only row naming at most one coordinate (a ref tip, a commit or a set of
files) whose facts Floati's writer stamps observed or claimed. Its freshness is
derived from that coordinate on every read, never from a timer and never
stored, and a stale item says what moved. A correction is a new item that
supersedes the old one. ``floati needs show`` is the one pull read across every seat of a root.

Three acts belong to the person and are taken only at a controlling terminal,
outside every seat workspace, by typing a code the panel shows: marking items
read, declaring the attention budget, and releasing a held dispatch. The MCP
surface never serves them.

While the unread review queue is over the declared budget, a new dispatch
(any ``floati send`` that is not a reply) is held: the sender
gets a typed refusal naming the budget, the count and the oldest unread
review, and the hold is a receipt. Repeating the same send releases it once
the person has released the hold or the queue is no longer over budget; that
release is a receipt too. Inbound mail that NET-HOLD-INBOUND-1 holds for a
person's approval is listed as one more kind of item; its records are read,
never written, here.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .errors import IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .needs_records import (HUMAN_ACT_CHANNEL, ITEM_TYPES, LEDGER_DIRECTORY, MAX_BUDGET,
                            MAX_ITEMS_PER_READ, NEEDS_KINDS, visible_text)
from .registry import Registry, utc_now
from .root import FloatiRoot

LEDGER = Path(str(LEDGER_DIRECTORY)) / "ledger.jsonl"
INBOUND_HOLD_LEDGER = Path("receipts/inbound-holds")
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LENGTH = 8
_ANSWER_BYTES = 256


# ---------------------------------------------------------------------------
# Bound records (contract version 1) through the one shared implementation,
# floati.bound_record: it resolves and stamps coordinates at write and derives
# their freshness at read. This module adds only what the inbox needs on top:
# which coordinate a filing names, and the seen token a person's read keeps.
# ---------------------------------------------------------------------------

CONTRACT_VERSION = 1


def bound_freshness(coordinates: Sequence[dict]) -> Dict[str, object]:
    """The shared derived freshness, plus the token a read receipt keeps as what was seen.

    The token is the SHA-256 of the canonical JSON of the derived per-coordinate
    verdicts, which carry each moved value; an unknown read has no token.
    """
    from . import bound_record
    fresh = bound_record.freshness(list(coordinates))
    token = None if not coordinates or fresh["state"] == "unknown" else hashlib.sha256(
        json.dumps(fresh["coordinates"], sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"state": fresh["state"], "coordinates": fresh["coordinates"], "token": token}


def resolve_coordinates(repository: Optional[str], ref: Optional[str], commit: Optional[str],
                        paths: Optional[Sequence[str]]) -> List[dict]:
    """Bind the one coordinate a filing names, resolved and stamped by the shared writer."""
    from . import bound_record
    paths = list(paths or ())
    if repository is None:
        if ref is not None or commit is not None or paths:
            raise ProtocolRefusal("needs_coordinate_invalid", "a ref, commit or path needs its repository",
                                  remedy="Pass --repository with the absolute path of the checkout the item is about.")
        return []
    if commit is not None and ref is None and paths:
        raise ProtocolRefusal("needs_coordinate_invalid", "a commit coordinate cannot name paths",
                              remedy="Pass --ref with --path to bind files on a branch, or --path alone to bind working-tree files.")
    if ref is None and commit is None and not paths:
        raise ProtocolRefusal("needs_coordinate_invalid", "a coordinate names a ref, a commit or files",
                              remedy="Pass --ref, --commit or at least one --path with --repository.")
    repo = bound_record.resolve_repository(repository)
    if ref is not None:
        coordinate = bound_record.observe_ref(repo, ref, paths)
        if commit is not None:
            bound = bound_record.bind_commit(repo, commit)
            coordinate = bound_record.stamp_fact(bound_record.CLAIMED, kind="ref", repository=bound["repository"],
                                                 ref=coordinate["ref"], commit=bound["commit"],
                                                 paths=coordinate["paths"])
        return [coordinate]
    if commit is not None:
        return [bound_record.bind_commit(repo, commit)]
    return [bound_record.observe_files(repo, paths)]


# ---------------------------------------------------------------------------
# The ledger and its projection.
# ---------------------------------------------------------------------------

def _rows(root: FloatiRoot) -> List[dict]:
    """A lock-free snapshot; any row the writer would refuse is malformed evidence."""
    return read_records_snapshot(root, LEDGER, allowed_kinds=set(NEEDS_KINDS))


def _append(root: FloatiRoot, decide):
    return transact(root, LEDGER, decide, allowed_kinds=set(NEEDS_KINDS))


def _base(kind: str, prefix: str, root: FloatiRoot) -> dict:
    return {"schema_version": 1, "id": prefix + uuid7_hex(), "tenant_id": root.tenant_id,
            "timestamp": utc_now(), "kind": kind}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _state(rows: Sequence[dict]) -> dict:
    items, reads, closes, holds, superseded = {}, {}, {}, {}, {}
    observations, wakes = [], []
    budget = None
    for row in rows:
        kind = row["kind"]
        if kind in ("needs_item", "needs_observed"):
            items[row["id"]] = row
            if row["supersedes"] is not None:
                superseded[row["supersedes"]] = row["id"]
        elif kind == "needs_observation":
            observations.append(row)
        elif kind == "needs_wake":
            wakes.append(row)
        elif kind == "needs_read":
            for view in row["seen"]:
                reads[view["item_id"]] = (row, view)
        elif kind == "needs_close":
            closes.setdefault(row["item_id"], row)
        elif kind == "needs_budget":
            budget = row
        else:
            holds.setdefault(row["id"] if row["phase"] == "held" else row["hold_id"], []).append(row)
    return {"items": items, "reads": reads, "closes": closes, "budget": budget, "holds": holds,
            "superseded": superseded, "observations": observations, "wakes": wakes}


def _live(state: dict, ident: str) -> bool:
    """An item at the head of its chain and not closed."""
    return ident not in state["closes"] and ident not in state["superseded"]


def _unread_reviews(state: dict) -> List[dict]:
    """Open, head review items the person has never marked read, oldest first."""
    return [item for ident, item in state["items"].items()
            if item["item_kind"] == "review" and _live(state, ident) and ident not in state["reads"]]


def _budget_view(state: dict) -> dict:
    """The readout: unread reviews and the oldest one, against the declared budget."""
    budget = state["budget"]
    limit = None if budget is None else budget["limit"]
    hold = budget is not None and budget["hold_dispatches"]
    unread = _unread_reviews(state)
    oldest = None if not unread else {"item_id": unread[0]["id"], "filed_at": unread[0]["timestamp"],
                                      "title": unread[0]["statement"]["title"]}
    over = limit is not None and len(unread) > limit
    line = (str(len(unread)) + " unread review item(s)"
            + ("" if oldest is None else ", oldest " + oldest["item_id"] + " filed " + oldest["filed_at"])
            + ("; no attention budget declared" if limit is None else
               "; budget " + str(limit) + (" (over)" if over else " (within)")
               + ("; dispatch hold on" if hold else "; dispatch hold off")))
    return {"limit": limit, "hold_dispatches": hold,
            "declared_at": None if budget is None else budget["timestamp"],
            "receipt_id": None if budget is None else budget["id"],
            "unread_reviews": len(unread), "oldest": oldest, "over": over, "line": line}


def _hold_view(chain: Sequence[dict]) -> dict:
    held = chain[0]
    released = next((row for row in chain if row["phase"] == "released"), None)
    dispatched = next((row for row in chain if row["phase"] == "dispatched"), None)
    if dispatched is not None or (released is not None and released["release_cause"] != "owner"):
        phase = "dispatched"
    elif released is not None:
        phase = "released"
    else:
        phase = "held"
    return {"id": held["id"], "phase": phase, "held_at": held["timestamp"], "sender": held["sender"],
            "request": held["request"], "reason": held["reason"],
            "release": None if released is None else {
                "id": released["id"], "at": released["timestamp"],
                "cause": released["release_cause"]},
            "receipt_ids": [row["id"] for row in chain]}


def _held_mail(root: FloatiRoot) -> dict:
    """Inbound mail held for a person's approval, read from NET-HOLD-INBOUND-1's records."""
    try:
        directory = root.resolve_relative(INBOUND_HOLD_LEDGER)
        ledgers = sorted(directory.glob("*.jsonl")) if directory.is_dir() else []
    except (OSError, ProtocolRefusal) as exc:
        return {"state": "unknown", "items": [], "cause": "inbound hold records could not be listed: "
                + type(exc).__name__}
    if not ledgers:
        return {"state": "none", "items": [], "cause": None}
    try:
        from .inbound_hold import inbound_hold_projection
    except ImportError:
        return {"state": "unknown", "items": [],
                "cause": "inbound hold records exist but this build has no reader for them"}
    items = []
    try:
        for ledger in ledgers:
            projection = inbound_hold_projection(root, ledger.stem)
            for waiting in projection.get("awaiting_decision", ()):
                items.append({"item_kind": "held_mail", "node_id": ledger.stem, **waiting})
    except (OSError, ProtocolRefusal, IntegrityFailure, ValueError) as exc:
        return {"state": "unknown", "items": [], "cause": "inbound hold records could not be read: "
                + type(exc).__name__}
    return {"state": "read", "items": items, "cause": None}


def _read_view(state: dict, ident: str, fresh: dict) -> dict:
    entry = state["reads"].get(ident)
    if entry is None:
        return {"state": "unread", "at": None, "receipt_id": None, "seen_coordinate": None}
    row, view = entry
    moved = (fresh["token"] is not None and view["coordinate"] is not None
             and fresh["token"] != view["coordinate"]) or (
             view["freshness"] == "current" and fresh["state"] == "stale")
    return {"state": "read_before_move" if moved else "read", "at": row["timestamp"],
            "receipt_id": row["id"], "seen_coordinate": view["coordinate"]}


def _entry(state: dict, item: dict) -> dict:
    """One bound record as the contract's read surface shows it, with its source and read state beside."""
    from . import needs_observed
    close = state["closes"].get(item["id"])
    successor = state["superseded"].get(item["id"])
    if item["kind"] == "needs_observed":
        fresh = needs_observed.freshness(item, None if successor is None else state["items"][successor])
    else:
        fresh = bound_freshness(item["coordinates"])
    return {"record": item,
            "source": needs_observed.source_of(item),
            "freshness": {"state": fresh["state"], "coordinates": fresh["coordinates"]},
            "chain": {"head": successor is None, "superseded_by": successor},
            "closed": None if close is None else {"at": close["timestamp"], "by": close["actor"],
                                                  "reason": close["reason"], "receipt_id": close["id"]},
            "read": _read_view(state, item["id"], fresh),
            "_token": fresh["token"]}


def _public(entry: dict) -> dict:
    return {key: value for key, value in entry.items() if key != "_token"}


def show(root: FloatiRoot, *, include_closed: bool = False) -> dict:
    """The one pull read of what needs a person, across every seat of this root.

    Observed items (floati.needs_observed) live in the same ledger as raised
    ones, so a read, a close and the budget treat them alike; each entry says
    its source, and ``observed_sources`` says whether and since when each
    orchestrator was last read, never an empty list standing for "unread".
    """
    from .needs_observed import observed_sources
    state = _state(_rows(root))
    entries = [_public(_entry(state, item)) for ident, item in state["items"].items()
               if include_closed or _live(state, ident)]
    holds = [_hold_view(chain) for chain in state["holds"].values()]
    held_mail = _held_mail(root)
    live = [entry for entry in entries if entry["closed"] is None and entry["chain"]["head"]]
    return {
        "contract": "bound-record",
        "contract_version": CONTRACT_VERSION,
        "root": str(root.path),
        "observed_at": utc_now(),
        "records": entries,
        "held_dispatches": [hold for hold in holds if hold["phase"] in ("held", "released")],
        "held_mail": held_mail,
        "observed_sources": observed_sources(state["observations"], state["items"]),
        "budget": _budget_view(state),
        "counts": {
            "open": len(live),
            "unread": sum(1 for entry in live if entry["read"]["state"] == "unread"),
            "stale": sum(1 for entry in live if entry["freshness"]["state"] == "stale"),
            "unknown": sum(1 for entry in live if entry["freshness"]["state"] == "unknown"),
            "held_dispatches": sum(1 for hold in holds if hold["phase"] == "held"),
            "held_mail": len(held_mail["items"]),
        },
    }


# ---------------------------------------------------------------------------
# What agents do: file and close.
# ---------------------------------------------------------------------------

def _require_node(root: FloatiRoot, node: str) -> str:
    try:
        Registry(root).require_active(node)
    except ProtocolRefusal:
        raise ProtocolRefusal("needs_node_unknown", "this act needs a registered active node",
                              remedy="Select a registered active node with --as.") from None
    return node


def _key(key: str) -> str:
    if not isinstance(key, str) or not (1 <= len(key) <= 128) or not key[0].isalnum() or not all(
            c.isascii() and (c.isalnum() or c in "._:-") for c in key):
        raise ProtocolRefusal("needs_key_invalid", "the idempotency key is 1 to 128 letters, digits, '.', '_', ':' or '-'",
                              remedy="Pass --idempotency-key as a short stable key for this exact request.")
    return key


def file_item(root: FloatiRoot, node: str, item_kind: str, title: str, key: str, *,
              detail: Optional[str] = None, repository: Optional[str] = None,
              ref: Optional[str] = None, commit: Optional[str] = None,
              paths: Optional[Sequence[str]] = None, supersedes: Optional[str] = None) -> dict:
    node = _require_node(root, node)
    key = _key(key)
    if item_kind not in ITEM_TYPES:
        raise ProtocolRefusal("needs_item_kind_invalid", "an item is a review, an approval or a question",
                              remedy="Pass --kind review, --kind approval or --kind question.")
    if not visible_text(title, 256) or (detail is not None and not visible_text(detail, 2048)):
        raise ProtocolRefusal("needs_text_invalid", "title and detail are one line of visible text",
                              remedy="Keep --title within 256 and --detail within 2048 UTF-8 bytes, on one line.")
    request_digest = _digest({"actor": node, "item_kind": item_kind, "title": title, "detail": detail,
                              "repository": repository, "ref": ref, "commit": commit,
                              "paths": sorted(set(paths or ())), "supersedes": supersedes})
    coordinates = resolve_coordinates(repository, ref, commit, paths)

    def decide(rows):
        state = _state(rows)
        for row in rows:
            if row["kind"] == "needs_item" and row["actor"] == node and row["idempotency_key"] == key:
                if row["request_digest"] != request_digest:
                    raise ProtocolRefusal("idempotency_conflict", "this key already names a different item",
                                          remedy="Replay the original request with this key, or choose a fresh key.")
                return row, None
        if supersedes is not None:
            prior = state["items"].get(supersedes)
            if prior is None:
                raise ProtocolRefusal("needs_item_unknown", "no item in this root has that id",
                                      remedy="List items with floati needs show --root ROOT.")
            if prior["kind"] == "needs_observed":
                raise ProtocolRefusal("needs_observed_not_correctable",
                                      "an observed item is superseded only by a later observation of its thread",
                                      remedy="File a new item without --supersedes, or close the observed item "
                                             "as the node that observed it.")
            if prior["actor"] != node:
                raise ProtocolRefusal("needs_not_filer", "only the node that filed an item corrects or closes it",
                                      remedy="File the correction as the node that filed the item.")
            if supersedes in state["superseded"]:
                raise ProtocolRefusal("supersedes_not_head", "that item is already superseded",
                                      remedy="Supersede the head of its chain: " + state["superseded"][supersedes] + ".")
        record = {**_base("needs_item", "needs-item-", root), "actor": node, "item_kind": item_kind,
                  "statement": {"title": title, "detail": detail, "provenance": "claimed"},
                  "coordinates": coordinates, "supersedes": supersedes,
                  "idempotency_key": key, "request_digest": request_digest}
        return record, record

    return _append(root, decide)


def close_item(root: FloatiRoot, node: str, item: str, reason: str, key: str) -> dict:
    node = _require_node(root, node)
    key = _key(key)
    if not visible_text(reason, 1024):
        raise ProtocolRefusal("needs_text_invalid", "the reason is one line of visible text",
                              remedy="Keep --reason within 1024 UTF-8 bytes, on one line.")

    def decide(rows):
        state = _state(rows)
        filed = state["items"].get(item)
        if filed is None:
            raise ProtocolRefusal("needs_item_unknown", "no item in this root has that id",
                                  remedy="List items with floati needs show --root ROOT.")
        prior = state["closes"].get(item)
        if prior is not None:
            if prior["actor"] == node and prior["idempotency_key"] == key:
                return prior, None
            raise ProtocolRefusal("needs_item_closed", "this item is already closed",
                                  remedy="A close is final; replay it with its original key.")
        if filed["actor"] != node:
            raise ProtocolRefusal("needs_not_filer", "only the node that filed an item corrects or closes it",
                                  remedy="Close the item as the node that filed it.")
        record = {**_base("needs_close", "needs-close-", root), "item_id": item, "actor": node,
                  "reason": reason, "idempotency_key": key}
        return record, record

    return _append(root, decide)


# ---------------------------------------------------------------------------
# What only a person does: at a controlling terminal, outside every seat.
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
        raise ProtocolRefusal("needs_human_act_unavailable", "this act needs a person at a controlling terminal",
                              remedy="A person runs this command in their own terminal, outside every seat workspace.")
    return descriptor


def _say(descriptor: int, text: str) -> None:
    data = text.encode("utf-8")
    while data:
        data = data[os.write(descriptor, data):]


def _answer(descriptor: int) -> str:
    data = b""
    while b"\n" not in data and len(data) < _ANSWER_BYTES:
        chunk = os.read(descriptor, _ANSWER_BYTES)
        if not chunk:
            break
        data += chunk
    return data.split(b"\n", 1)[0].decode("utf-8", "replace").strip()


def _human_act(root: FloatiRoot, verb: str, lines: List[str], decide, cwd: Optional[Path] = None) -> dict:
    """Show the exact panel at the controlling terminal; append only on its code."""
    context = _seat_context(root, Path(os.getcwd() if cwd is None else cwd).resolve())
    if context is not None:
        raise ProtocolRefusal("needs_human_act_seat_context", "this act is never taken from a seat: " + context,
                              remedy="A person runs this command from their own terminal outside every seat workspace.")
    descriptor = _terminal()
    try:
        code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
        panel = "\n".join(["", *lines, "To " + verb + ", type this code: " + code,
                           "Anything else changes nothing.", "> "])
        _say(descriptor, panel)
        if _answer(descriptor) != code:
            _say(descriptor, "Not confirmed. Nothing changed.\n")
            raise ProtocolRefusal("needs_human_act_unconfirmed", "the typed code did not match the code shown",
                                  remedy="Run the command again and type the code it shows.")
        act = {"channel": HUMAN_ACT_CHANNEL,
               "rendered_digest": hashlib.sha256(panel.encode("utf-8")).hexdigest()}
        recorded = _append(root, lambda rows: decide(rows, act))
        _say(descriptor, "Recorded " + recorded["id"] + ".\n")
        return recorded
    finally:
        os.close(descriptor)


def _replay(rows: Sequence[dict], kind: str, key: str, matches) -> Optional[dict]:
    prior = next((row for row in rows if row["kind"] == kind and row.get("idempotency_key") == key), None)
    if prior is not None and not matches(prior):
        raise ProtocolRefusal("needs_key_conflict", "this key already names a different act",
                              remedy="Replay the original act with this key, or choose a fresh key.")
    return prior


def mark_read(root: FloatiRoot, items: Sequence[str], key: str, *, cwd: Optional[Path] = None) -> dict:
    """The person marks items read, recording the freshness each one had when seen."""
    key = _key(key)
    items = list(dict.fromkeys(items))
    if not 1 <= len(items) <= MAX_ITEMS_PER_READ:
        raise ProtocolRefusal("needs_read_invalid", "a read names 1 to 64 items",
                              remedy="Pass --item once per item, at most 64 times.")
    rows = _rows(root)
    prior = _replay(rows, "needs_read", key, lambda row: row["item_ids"] == items)
    if prior is not None:
        return prior
    state = _state(rows)
    unknown = [item for item in items if item not in state["items"]]
    if unknown:
        raise ProtocolRefusal("needs_item_unknown", "no item in this root has id " + unknown[0],
                              remedy="List items with floati needs show --root ROOT.")
    views = [_entry(state, state["items"][item]) for item in items]
    seen = [{"item_id": view["record"]["id"], "freshness": view["freshness"]["state"],
             "coordinate": view["_token"]} for view in views]
    lines = ["Marking " + str(len(views)) + " item(s) read in " + str(root.path) + ":"]
    for view in views:
        record, moved = view["record"], [m for c in view["freshness"]["coordinates"] for m in c["moved"]]
        lines.append("- " + record["id"] + " " + record["item_kind"] + " from " + record["actor"] + ": "
                     + json.dumps(record["statement"]["title"], ensure_ascii=True))
        lines.append("  " + view["freshness"]["state"].upper()
                     + ("" if not moved else " moved: " + json.dumps(moved, ensure_ascii=True, sort_keys=True)))

    def decide(current, act):
        again = _replay(current, "needs_read", key, lambda row: row["item_ids"] == items)
        if again is not None:
            return again, None
        record = {**_base("needs_read", "needs-read-", root), "item_ids": items, "seen": seen,
                  "human_act": act, "idempotency_key": key}
        return record, record

    return _human_act(root, "mark them read", lines, decide, cwd)


def declare_budget(root: FloatiRoot, limit: Optional[int], key: str, *, hold: bool = False,
                   cwd: Optional[Path] = None) -> dict:
    """The person declares how many unread review items they can carry, or clears it.

    Holding new dispatches while over budget is a separate choice, off unless
    the person switches it on here.
    """
    key = _key(key)
    if limit is not None and (type(limit) is not int or not 0 <= limit <= MAX_BUDGET):
        raise ProtocolRefusal("needs_budget_invalid", "the budget is a whole number from 0 to 10000",
                              remedy="Pass --limit N, or --clear to remove the budget.")
    if hold and limit is None:
        raise ProtocolRefusal("needs_budget_invalid", "holding dispatches needs a declared budget",
                              remedy="Pass --hold-dispatches together with --limit N.")
    rows = _rows(root)
    matches = lambda row: row["limit"] == limit and row["hold_dispatches"] == hold
    prior = _replay(rows, "needs_budget", key, matches)
    if prior is not None:
        return prior
    state = _budget_view(_state(rows))
    lines = [("Clearing the attention budget" if limit is None else
              "Declaring an attention budget of " + str(limit) + " unread review item(s)")
             + " for " + str(root.path) + ".",
             "Unread review items now: " + str(state["unread_reviews"]) + ".",
             ("While the unread review count is over the budget, new dispatches are held." if hold
              else "Every new dispatch shows this count; none is held.")]

    def decide(current, act):
        again = _replay(current, "needs_budget", key, matches)
        if again is not None:
            return again, None
        record = {**_base("needs_budget", "needs-budget-", root), "limit": limit,
                  "hold_dispatches": hold, "human_act": act, "idempotency_key": key}
        return record, record

    return _human_act(root, "clear it" if limit is None else "declare it", lines, decide, cwd)


def release_hold(root: FloatiRoot, hold: str, key: str, *, cwd: Optional[Path] = None) -> dict:
    """The person releases one held dispatch; the sender's repeated send then goes out."""
    key = _key(key)
    rows = _rows(root)
    matches = lambda row: row["phase"] == "released" and row["hold_id"] == hold
    prior = _replay(rows, "needs_dispatch_hold", key, matches)
    if prior is not None:
        return prior
    chain = _state(rows)["holds"].get(hold)
    if chain is None:
        raise ProtocolRefusal("needs_hold_unknown", "no held dispatch in this root has that id",
                              remedy="List held dispatches with floati needs show --root ROOT.")
    view = _hold_view(chain)
    if view["phase"] != "held":
        raise ProtocolRefusal("needs_hold_not_held", "this dispatch is no longer held",
                              remedy="Only a held dispatch is released; replay a release with its original key.")
    lines = ["Releasing held dispatch " + hold + " from " + view["sender"] + ":",
             json.dumps(view["request"], ensure_ascii=True, sort_keys=True, indent=2),
             "Held because: " + json.dumps(view["reason"], ensure_ascii=True, sort_keys=True),
             "The sender's next identical send goes out."]

    def decide(current, act):
        again = _replay(current, "needs_dispatch_hold", key, matches)
        if again is not None:
            return again, None
        live = _state(current)["holds"].get(hold)
        if live is None or _hold_view(live)["phase"] != "held":
            raise ProtocolRefusal("needs_hold_not_held", "this dispatch is no longer held",
                                  remedy="Run floati needs show to see the current holds.")
        record = {**_base("needs_dispatch_hold", "needs-dispatch-hold-", root), "phase": "released",
                  "hold_id": hold, "request_digest": chain[0]["request_digest"], "sender": chain[0]["sender"],
                  "request": None, "reason": None, "release_cause": "owner", "human_act": act,
                  "idempotency_key": key}
        return record, record

    return _human_act(root, "release it", lines, decide, cwd)


# ---------------------------------------------------------------------------
# Dispatch admission against the declared budget.
# ---------------------------------------------------------------------------

def _decision(state: dict) -> dict:
    """Admit, unless the person switched dispatch holding on and the queue is over budget."""
    budget = _budget_view(state)
    return {"admission": "hold" if budget["over"] and budget["hold_dispatches"] else "admit",
            "budget": budget["limit"], "unread_reviews": budget["unread_reviews"],
            "oldest": budget["oldest"], "over": budget["over"],
            "hold_dispatches": budget["hold_dispatches"], "line": budget["line"]}


def admission_preview(root: FloatiRoot) -> dict:
    """What dispatch admission would decide for a new dispatch now; writes nothing."""
    return {"root": str(root.path), "observed_at": utc_now(), **_decision(_state(_rows(root)))}


def dispatch_request(args) -> Optional[dict]:
    """The closed request a send would dispatch, or None for a reply.

    Every send that is not a reply is a new dispatch, whatever its message
    class: an fyi message can carry new work as well as a normal one. A reply
    is bounded by the send writer itself, which refuses a reply_to that names
    no message the recipient sent to this sender.
    """
    if getattr(args, "reply_to", None) is not None:
        return None
    return {"recipient": args.recipient, "repo": args.repo, "sha": args.sha, "doc": args.doc,
            "note_sha256": hashlib.sha256(args.note.encode("utf-8")).hexdigest(),
            "message_class": getattr(args, "message_class", "normal"),
            "idempotency_key": args.idempotency_key}


def admit_dispatch(root: FloatiRoot, args) -> Tuple[Optional[dict], Optional[dict]]:
    """Admit one send or hold it: (refusal evidence when held, attention readout).

    The readout rides on every new dispatch of a root that has an inbox. A
    dispatch is held only while the person has switched holding on and the
    unread review queue is over budget; with holding off nothing is delayed.
    """
    request = dispatch_request(args)
    if request is None:
        return None, None
    try:
        if not root.resolve_relative(LEDGER).exists():
            return None, None
    except (OSError, ProtocolRefusal):
        pass
    digest = hashlib.sha256(json.dumps({"sender": args.sender, **request}, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8")).hexdigest()

    def decide(rows):
        state = _state(rows)
        decision = _decision(state)
        chains = [chain for chain in state["holds"].values() if chain[0]["request_digest"] == digest
                  and chain[0]["sender"] == args.sender]
        live = next((chain for chain in chains if _hold_view(chain)["phase"] in ("held", "released")), None)
        base = lambda: {**_base("needs_dispatch_hold", "needs-dispatch-hold-", root),
                        "request_digest": digest, "sender": args.sender}
        if live is not None:
            view = _hold_view(live)
            if view["phase"] == "released":
                record = {**base(), "phase": "dispatched", "hold_id": live[0]["id"], "request": None,
                          "reason": None, "release_cause": None, "human_act": None, "idempotency_key": None}
                return (None, decision), record
            if decision["admission"] == "admit":
                cause = "under_budget" if decision["hold_dispatches"] else "hold_off"
                record = {**base(), "phase": "released", "hold_id": live[0]["id"], "request": None,
                          "reason": {"budget": decision["budget"], "unread_reviews": decision["unread_reviews"]},
                          "release_cause": cause, "human_act": None, "idempotency_key": None}
                return (None, decision), record
            return (live[0], decision), None
        if decision["admission"] == "admit":
            return (None, decision), None
        record = {**base(), "phase": "held", "hold_id": None, "request": request,
                  "reason": {"budget": decision["budget"], "unread_reviews": decision["unread_reviews"],
                             "oldest": decision["oldest"]},
                  "release_cause": None, "human_act": None, "idempotency_key": None}
        return (record, decision), record

    held, decision = _append(root, decide)
    attention = {key: decision[key] for key in ("line", "budget", "unread_reviews", "oldest",
                                                "over", "hold_dispatches", "admission")}
    if held is None:
        return None, attention
    reason = held["reason"]
    release = shlex.join(["floati", "needs", "release", "--root", str(root.path), "--hold", held["id"],
                          "--idempotency-key", "release-" + held["id"][-12:]])
    return {
        "code": "dispatch_held_for_attention",
        "attention": attention,
        "detail": ("the person's unread review queue is over their attention budget: "
                   + str(reason["unread_reviews"]) + " unread review item(s), budget "
                   + str(reason["budget"]) + "; this dispatch is held, not sent"),
        "remedy": ("The person reads or the filers close review items until the queue is within budget, "
                   "or the person releases this hold at their terminal (" + release + "); "
                   "then repeat this exact send."),
        "hold": held,
        "preview": {"sender": args.sender, **{key: value for key, value in request.items()
                                              if key != "note_sha256"}, "note": args.note},
    }, attention


# ---------------------------------------------------------------------------
# CLI registration.
# ---------------------------------------------------------------------------

def register_cli(commands) -> None:
    needs = commands.add_parser("needs")
    actions = needs.add_subparsers(dest="needs_action", required=True)
    filing = actions.add_parser("file", floati_mcp_exposure="governed")
    filing.add_argument("--root", required=True, metavar="ROOT")
    filing.add_argument("--as", dest="actor", required=True, metavar="NODE")
    filing.add_argument("--kind", dest="item_kind", required=True, choices=sorted(ITEM_TYPES))
    filing.add_argument("--title", required=True, metavar="TEXT")
    filing.add_argument("--detail", metavar="TEXT")
    filing.add_argument("--repository", metavar="PATH")
    filing.add_argument("--ref", metavar="REF")
    filing.add_argument("--commit", metavar="SHA")
    filing.add_argument("--path", dest="paths", action="append", metavar="PATH")
    filing.add_argument("--supersedes", metavar="ID")
    filing.add_argument("--idempotency-key", required=True, metavar="KEY")
    filing.set_defaults(handler=_handle)
    closing = actions.add_parser("close", floati_mcp_exposure="governed")
    closing.add_argument("--root", required=True, metavar="ROOT")
    closing.add_argument("--as", dest="actor", required=True, metavar="NODE")
    closing.add_argument("--item", required=True, metavar="ID")
    closing.add_argument("--reason", required=True, metavar="TEXT")
    closing.add_argument("--idempotency-key", required=True, metavar="KEY")
    closing.set_defaults(handler=_handle)
    showing = actions.add_parser("show", floati_mcp_exposure="read")
    showing.add_argument("--root", required=True, metavar="ROOT")
    showing.add_argument("--all", dest="include_closed", action="store_true")
    showing.set_defaults(handler=_handle)
    observing = actions.add_parser("observe")
    observing.add_argument("--root", required=True, metavar="ROOT")
    observing.add_argument("--as", dest="actor", required=True, metavar="NODE")
    observing.add_argument("--source-config", required=True, metavar="PATH")
    observing.set_defaults(handler=_handle)
    admission = actions.add_parser("admission", floati_mcp_exposure="read")
    admission.add_argument("--root", required=True, metavar="ROOT")
    admission.set_defaults(handler=_handle)
    reading = actions.add_parser("read")
    reading.add_argument("--root", required=True, metavar="ROOT")
    reading.add_argument("--item", dest="items", action="append", required=True, metavar="ID")
    reading.add_argument("--idempotency-key", required=True, metavar="KEY")
    reading.set_defaults(handler=_handle)
    budget = actions.add_parser("budget")
    budget.add_argument("--root", required=True, metavar="ROOT")
    budget_value = budget.add_mutually_exclusive_group(required=True)
    budget_value.add_argument("--limit", type=int, metavar="N")
    budget_value.add_argument("--clear", action="store_true")
    budget.add_argument("--hold-dispatches", action="store_true")
    budget.add_argument("--idempotency-key", required=True, metavar="KEY")
    budget.set_defaults(handler=_handle)
    release = actions.add_parser("release")
    release.add_argument("--root", required=True, metavar="ROOT")
    release.add_argument("--hold", required=True, metavar="ID")
    release.add_argument("--idempotency-key", required=True, metavar="KEY")
    release.set_defaults(handler=_handle)


def _handle(args):
    from .cli import _root
    root = _root(args.root)
    action = args.needs_action
    if action == "file":
        evidence = file_item(root, args.actor, args.item_kind, args.title, args.idempotency_key,
                             detail=args.detail, repository=args.repository, ref=args.ref,
                             commit=args.commit, paths=args.paths, supersedes=args.supersedes)
    elif action == "close":
        evidence = close_item(root, args.actor, args.item, args.reason, args.idempotency_key)
    elif action == "show":
        evidence = show(root, include_closed=args.include_closed)
    elif action == "observe":
        from .needs_observed import observe
        evidence = observe(root, args.actor, args.source_config)
        if evidence["observation"]["state"] != "read":
            return "degraded", evidence, 35
    elif action == "admission":
        evidence = admission_preview(root)
    elif action == "read":
        evidence = mark_read(root, args.items, args.idempotency_key)
    elif action == "budget":
        evidence = declare_budget(root, None if args.clear else args.limit, args.idempotency_key,
                                  hold=args.hold_dispatches)
    else:
        evidence = release_hold(root, args.hold, args.idempotency_key)
    return "ok", evidence, 0


def with_attention(result, attention: Optional[dict]):
    """Carry the attention readout on one dispatch's artifact, unchanged otherwise."""
    status, evidence, code = result
    if attention is None or not isinstance(evidence, dict):
        return result
    return status, {**evidence, "attention": attention}, code

"""Author fleet presence once and project it at each seat presentation."""
from __future__ import annotations

from importlib import import_module
import shlex

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records_snapshot, transact
from .registry import Registry, utc_now


KIND = "fleet_presence_receipt"
LEDGER = "receipts/fleet-presence.jsonl"
MODES = ("AWAY", "PRESENT")
AWAY_LINE = "Fleet AWAY. Work silently and report to files. Answer direct questions normally."
RETURN_LINE = "Fleet PRESENT. Review your receipts since AWAY with `{command}`."
NEEDS_YOU_LINE = "See what needs your attention with `{command}`."
STATUS_LINE = "Fleet {mode} since {since} by {actor}."
ABSENT_LINE = "Fleet presence {mode}."


def _latest(rows):
    previous = None
    keys = set()
    for row in rows:
        if (row["idempotency_key"] in keys
                or row["predecessor_receipt_id"] != (previous and previous["id"])
                or row["away_since"] != (previous["since"] if previous and previous["mode"] == "AWAY" else None)
                or (previous and row["mode"] == previous["mode"])):
            raise IntegrityFailure("fleet_presence_history_invalid", "Fleet presence receipts do not form a valid transition history.")
        previous = row
        keys.add(row["idempotency_key"])
    return previous


def set_presence(root, actor, mode, key):
    Registry(root).require_active(actor)

    def decide(rows):
        latest = _latest(rows)
        for prior in rows:
            if prior["idempotency_key"] == key:
                if prior["actor"] != actor or prior["mode"] != mode:
                    raise ProtocolRefusal("fleet_presence_key_conflict", "This key already names another fleet presence declaration.")
                return prior, None
        if latest and latest["mode"] == mode:
            raise ProtocolRefusal("fleet_presence_unchanged", "The fleet is already in the requested mode.")
        now = utc_now()
        row = {
            "kind": KIND, "schema_version": 1, "id": "fleet-presence-" + uuid7_hex(),
            "tenant_id": root.tenant_id, "timestamp": now, "since": now, "actor": actor,
            "mode": mode, "idempotency_key": key,
            "predecessor_receipt_id": latest and latest["id"],
            "away_since": latest["since"] if latest and latest["mode"] == "AWAY" else None,
        }
        return row, row

    return transact(root, LEDGER, decide, allowed_kinds={KIND})


def presence_status(root):
    try:
        row = _latest(read_records_snapshot(root, LEDGER, allowed_kinds={KIND}))
    except (IntegrityFailure, DurabilityFailure, ProtocolRefusal, OSError) as exc:
        return {"mode": "UNKNOWN", "reason": getattr(exc, "code", "fleet_presence_io_error")}
    return row if row is not None else {"mode": "UNDECLARED"}


def presence_status_line(presence):
    if presence["mode"] in MODES:
        return STATUS_LINE.format_map(presence)
    return ABSENT_LINE.format_map(presence)


def needs_you_return_reference(root):
    """Point at the optional fleet-wide inbox after its read succeeds."""
    module_name = __package__ + ".needs_inbox"
    try:
        inbox = import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        return ""
    try:
        inbox.show(root)
    except (IntegrityFailure, DurabilityFailure, ProtocolRefusal, OSError):
        return ""
    command = shlex.join(["floati", "needs", "show", "--root", str(root.path)])
    return NEEDS_YOU_LINE.format(command=command) if command.isprintable() else ""


def presence_line(root, node, *, presence=None, needs_you=None):
    if presence is None:
        presence = presence_status(root)
    mode = presence["mode"]
    if mode == "AWAY":
        return AWAY_LINE
    if mode == "PRESENT" and presence["away_since"] is not None:
        command = shlex.join(["floati", "receipts", node, "--root", str(root.path),
                              "--since", presence["away_since"]])
        # Wake adapters accept one line. Keep unusual path characters visible.
        command = command.replace("\r", "\\r").replace("\n", "\\n")
        receipts = RETURN_LINE.format(command=command)
        if needs_you is None:
            needs_you = needs_you_return_reference(root)
        return receipts + " " + needs_you if needs_you else receipts
    if mode == "UNDECLARED":
        return ""
    return presence_status_line(presence)


def with_presence(root, node, text, *, separator="\n", presence=None):
    line = presence_line(root, node, presence=presence)
    return line + separator + text if line else text


def register_cli(commands):
    command = commands.add_parser("set")
    command.add_argument("--root", required=True, metavar="ROOT")
    command.add_argument("--as", dest="actor", required=True, metavar="NODE")
    command.add_argument("--mode", required=True, choices=MODES)
    command.add_argument("--idempotency-key", required=True, metavar="KEY")
    command.set_defaults(handler=_handle)


def _handle(args):
    from .cli import _root

    return "ok", set_presence(_root(args.root), args.actor, args.mode, args.idempotency_key), 0

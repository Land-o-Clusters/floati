"""Local T3 dispatch grants and readouts."""
import argparse
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets

from .errors import DurabilityFailure, IntegrityFailure, ProtocolRefusal
from .inbound_hold import (
    _answer, _CODE_ALPHABET, _CODE_LENGTH, _say, _seat_context, _terminal,
)
from .registry import utc_now
from .root import FloatiRoot
from .t3_dispatch import record, transaction
from .t3_dispatch_hook import coordinate
from .t3_dispatch_records import GRANT_KIND, _utc


def _grant(args):
    root = FloatiRoot.open_direct_home(Path(args.root))
    if not all(coordinate(value) for value in (args.provider, args.idempotency_key)) or (
            args.model is not None and not coordinate(args.model)):
        raise ProtocolRefusal("t3_grant_scope_invalid", "Use bounded provider, model and grant identifiers.")
    if not 1 <= args.expires_in <= 31 * 86400 or (
            args.count is not None and not 1 <= args.count <= 10000):
        raise ProtocolRefusal("t3_grant_scope_invalid", "Supply a duration of 1 to 2678400 seconds and, optionally, a count from 1 to 10000.")
    context = _seat_context(root, Path.cwd().resolve())
    if context is not None:
        raise ProtocolRefusal("t3_grant_seat_context", "Run this command outside a seat workspace.")

    code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))
    count = "any number of" if args.count is None else str(args.count)
    panel = (f"Fleet: {json.dumps(str(root.path))}\n"
             f"Grant {count} T3 dispatches on {args.provider} "
             f"({args.model or 'all models'}) for {args.expires_in} seconds.\n"
             f"To grant, type this code: {code}\n> ")
    try:
        terminal = _terminal()
    except ProtocolRefusal as exc:
        raise ProtocolRefusal("t3_grant_terminal_required", "Use a controlling terminal to issue a T3 grant.") from exc
    try:
        _say(terminal, panel)
        if not secrets.compare_digest(_answer(terminal).encode(), code.encode()):
            raise ProtocolRefusal("t3_grant_unconfirmed", "The typed code did not match.")
    finally:
        os.close(terminal)

    def decide(rows):
        scope = {"provider_instance": args.provider, "model": args.model, "count": args.count,
                 "idempotency_key": args.idempotency_key}
        for row in rows:
            if row["kind"] == GRANT_KIND and row["idempotency_key"] == args.idempotency_key:
                duration = (_utc(row["expires_at"]) - _utc(row["timestamp"])).total_seconds()
                if duration != args.expires_in or any(row[k] != v for k, v in scope.items()):
                    raise ProtocolRefusal("t3_grant_key_conflict", "The key already names another grant.")
                return row, None
        now = utc_now()
        expiry = (_utc(now) + timedelta(seconds=args.expires_in)).isoformat().replace("+00:00", "Z")
        grant = record(root, GRANT_KIND, now, **scope, expires_at=expiry,
                       human_act={"channel": "controlling_terminal",
                                  "rendered_digest": hashlib.sha256(panel.encode()).hexdigest()})
        return grant, grant

    return transaction(root, decide)


def _route(args, read_inventory, at):
    from .t3_quota_readout import candidate_quota

    if (not coordinate(args.environment)
            or len(args.provider) != len(set(args.provider))
            or not all(coordinate(provider) for provider in args.provider)):
        raise ProtocolRefusal("t3_route_targets_invalid", "Supply an environment and distinct provider instance IDs.")
    return candidate_quota(FloatiRoot.open_direct_home(Path(args.root)), args.puddle_quota_snapshot,
                           args.environment, args.provider, read_inventory, at=at or _utc(utc_now()))


def main(argv=None, *, read_inventory=None, at=None):
    parser = argparse.ArgumentParser(prog="python3 -m floati.t3_gate")
    commands = parser.add_subparsers(dest="verb", required=True)
    grant = commands.add_parser("grant")
    grant.add_argument("--root", required=True)
    grant.add_argument("--provider", required=True)
    grant.add_argument("--model")
    grant.add_argument("--expires-in", required=True, type=int)
    grant.add_argument("--count", type=int)
    grant.add_argument("--idempotency-key", required=True)
    board = commands.add_parser("board")
    board.add_argument("--root", required=True)
    board.add_argument("--repository", required=True)
    board.add_argument("--base-ref", required=True)
    board.add_argument("--bindings", required=True)
    board.add_argument("--producer-tip")
    route = commands.add_parser("route", help="show advisory headroom for T3 provider instances")
    route.add_argument("--root", required=True)
    route.add_argument("--environment", required=True)
    route.add_argument("--provider", action="append", required=True)
    route.add_argument("--puddle-quota-snapshot")
    args = parser.parse_args(argv)
    try:
        if args.verb == "grant":
            evidence = _grant(args)
        elif args.verb == "route":
            evidence = _route(args, read_inventory, at)
        else:
            from .t3_shared_base import board as shared_base_board
            evidence = shared_base_board(FloatiRoot.open_direct_home(Path(args.root)),
                                         args.repository, args.base_ref, args.bindings, args.producer_tip)
        status, exit_code = "ok", 0
    except ProtocolRefusal as exc:
        evidence, status, exit_code = {"code": exc.code, "detail": exc.detail}, "refused", 20
    except IntegrityFailure as exc:
        evidence, status, exit_code = {"code": exc.code, "detail": exc.detail}, "malformed_evidence", 33
    except (DurabilityFailure, OSError):
        failure = {
            "grant": "t3_grant_write_failed",
            "board": "t3_board_read_failed",
            "route": "t3_route_read_failed",
        }[args.verb]
        evidence, status, exit_code = {"code": failure}, "degraded", 35
    print(json.dumps({"artifact_version": 0, "command": f"t3 {args.verb}", "status": status,
                      "evidence": evidence}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

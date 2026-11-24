"""CLI surface for local dispatch evidence."""

from pathlib import Path

from .dispatch_records import DispatchRecords
from .root import resolve_command_root
from .seat_declaration import require_declared_coordinate


BRIEF_NOTE = "Keep secrets out of briefs. Floati stores the full text locally."


def _dispatch(args):
    root = resolve_command_root(args.root)
    records = DispatchRecords(root)
    command = args.dispatch_command
    if command == "show":
        return "ok", records.show(args.dispatch_id), 0
    require_declared_coordinate(Path.cwd(), args.actor, root)
    if command == "brief":
        return "ok", records.brief(args.dispatch_id, args.revision, args.actor), 0

    common = (args.dispatch_id, args.actor, args.idempotency_key)
    if command == "record":
        row = records.record(
            *common, args.recipient, args.branch, args.brief_file, external=args.external,
            envelope_id=args.envelope_id, supersedes=args.supersedes,
        )
        return "ok", {"record": row, "brief_note": BRIEF_NOTE}, 0
    if command == "return":
        row = records.record_return(*common, args.revision, args.repository, args.ref,
                                    args.artifact, args.statement)
    else:
        row = records.close(*common, args.reason)
    return "ok", {"record": row}, 0


def add_dispatch_parser(commands):
    group = commands.add_parser("dispatch")
    verbs = group.add_subparsers(dest="dispatch_command", required=True)
    for name in ("record", "return", "close", "show", "brief"):
        exposure = {"floati_mcp_exposure": "read"} if name == "show" else {}
        parser = verbs.add_parser(name, **exposure)
        parser.add_argument("--root", required=True)
        parser.add_argument("--id", dest="dispatch_id", required=name != "show", metavar="ID")
        if name != "show":
            parser.add_argument("--as", dest="actor", required=True, metavar="NODE")
        if name in {"record", "return", "close"}:
            parser.add_argument("--idempotency-key", required=True, metavar="KEY")
        if name in {"show", "brief"}:
            parser.add_argument("--json", action="store_true")
        if name in {"return", "brief"}:
            parser.add_argument("--revision", type=int, required=name == "brief", metavar="N")
        if name == "record":
            parser.add_argument("--to", dest="recipient", required=True, metavar="NODE")
            parser.add_argument("--branch", required=True, metavar="BRANCH")
            parser.add_argument("--brief-file", required=True, metavar="PATH")
            carrier = parser.add_mutually_exclusive_group(required=True)
            carrier.add_argument("--external", metavar="LABEL")
            carrier.add_argument("--envelope-id", metavar="ID")
            parser.add_argument("--supersedes", metavar="ID")
        if name == "return":
            parser.add_argument("--repository", required=True, metavar="PATH")
            parser.add_argument("--ref", required=True, metavar="REF")
            parser.add_argument("--artifact", required=True, metavar="PATH")
            parser.add_argument("--statement", metavar="TEXT")
        if name == "close":
            parser.add_argument("--reason", required=True, metavar="TEXT")
        parser.set_defaults(handler=_dispatch)

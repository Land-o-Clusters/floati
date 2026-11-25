"""Local topology declarations written through the native registry transaction."""

from .admin_cli import OK, _root
from .admin_registry import ARCHITECT_REMEDY, RegistryAdminBackend
from .bus_epoch import epoch_guard
from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import transact_records
from .registry import REGISTRY_KINDS, Registry, read_registry_compatible, utc_now
from .root import validate_identifier


def _require_active(records, node):
    entry = RegistryAdminBackend._latest_registry(records, node)
    if entry is None or entry["state"] != "active":
        raise ProtocolRefusal("unknown_node", f"node {node!r} is not active",
                              remedy="choose an active registered node")


def _record_change(root, kind, prefix, key, coordinates, check):
    def decide(existing):
        for row in existing:
            if row["kind"] != kind or row.get("idempotency_key") != key:
                continue
            if any(row.get(field) != value for field, value in coordinates.items()):
                raise ProtocolRefusal("idempotency_conflict",
                                      "the key already names a different change",
                                      remedy="use a fresh --idempotency-key for a different change")
            return {"receipt": row, "replayed": True}, ()

        record = {
            "schema_version": 1,
            "kind": kind,
            "id": prefix + uuid7_hex(),
            "tenant_id": root.tenant_id,
            "timestamp": utc_now(),
            "idempotency_key": key,
            **coordinates,
            **check(existing),
        }
        return {"receipt": record, "replayed": False}, (record,)

    with epoch_guard(root):
        return transact_records(root, Registry(root).relative_path, decide,
                                allowed_kinds=REGISTRY_KINDS)


def clear_node_role(root, node, key):
    node = validate_identifier(node, "node")

    def check(existing):
        _require_active(existing, node)
        role = next((row for row in reversed(existing)
                     if row["kind"] == "registry_role_record" and row["node_id"] == node), None)
        if role is None or role["state"] != "active":
            raise ProtocolRefusal("role_assignment_missing", "the node has no assigned role",
                                  remedy="choose an assigned node from a v3 Confluence roster")
        if role["template_role"].casefold() == "architect":
            raise ProtocolRefusal("role_architect_invalid", "transfer the architect role before clearing it",
                                  remedy=ARCHITECT_REMEDY)
        return {"state": "retired", "predecessor_role_record_id": role["id"]}

    return _record_change(root, "registry_role_record", "registry-role-",
                          key, {"node_id": node}, check)


def _latest_routes(records):
    return {(row["from_node"], row["to_node"]): row for row in records
            if row["kind"] == "declared_route_receipt"}


def _route_write(args):
    root = _root(args.root)
    source = validate_identifier(args.from_node, "from_node")
    target = validate_identifier(args.to_node, "to_node")
    if source == target:
        raise ProtocolRefusal("route_endpoints_invalid", "a route needs two distinct nodes",
                              remedy="pass distinct node IDs to --from and --to")

    def check(existing):
        _require_active(existing, source)
        _require_active(existing, target)
        latest = next((row for row in reversed(existing)
                       if row["kind"] == "declared_route_receipt"
                       and (row["from_node"], row["to_node"]) == (source, target)), None)
        declared = latest is not None and latest["action"] == "add"
        if declared and args.route_action == "add":
            raise ProtocolRefusal("route_already_declared", "this route is already declared",
                                  remedy="run net route list to see the existing route")
        if not declared and args.route_action == "remove":
            raise ProtocolRefusal("route_not_declared", "this route is not declared",
                                  remedy="run net route list and choose a declared route")
        return {}

    result = _record_change(root, "declared_route_receipt", "declared-route-",
                            args.idempotency_key,
                            {"from_node": source, "to_node": target, "action": args.route_action},
                            check)
    return "ok", result, OK


def _route_list(args):
    records, _unknown, _versions = read_registry_compatible(_root(args.root))
    registrations = {row["node_id"]: row["state"] for row in records
                     if row["kind"] == "registry_entry"}
    routes = [
        record for (source, target), record in sorted(_latest_routes(records).items())
        if record["action"] == "add" and registrations.get(source) == "active"
        and registrations.get(target) == "active"
    ]
    return "ok", {"routes": routes}, OK


def register_route_commands(commands):
    net = commands.add_parser("net").add_subparsers(dest="net_command", required=True)
    routes = net.add_parser("route").add_subparsers(dest="route_action", required=True)
    for action in ("add", "remove"):
        change = routes.add_parser(action)
        change.add_argument("--root", required=True)
        change.add_argument("--from", dest="from_node", required=True, metavar="NODE")
        change.add_argument("--to", dest="to_node", required=True, metavar="NODE")
        change.add_argument("--idempotency-key", required=True, metavar="KEY")
        change.set_defaults(handler=_route_write)
    listing = routes.add_parser("list")
    listing.add_argument("--root", required=True)
    listing.set_defaults(handler=_route_list)

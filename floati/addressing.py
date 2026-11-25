"""Local group addresses bound to an explicit recipient preview.

The digest is a content binding, not an authentication credential. Consumers
must show the artifact's recipient list before passing it back to send or ask.
"""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import FloatiError, ProtocolRefusal
from .registry import Registry
from .root import validate_identifier


def is_group(address):
    return address == "everyone" or address.startswith(("repo:", "person:"))


def _refuse(code, detail, remedy="preview the group again and review its recipients"):
    raise ProtocolRefusal(code, detail, remedy)


def _repository_name(address):
    name = address[5:]
    if len(name) > 128 or re.fullmatch(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)?", name) is None:
        _refuse("address_invalid", "repository address requires repo:NAME or repo:OWNER/NAME")
    return name


def resolve_recipients(root, sender, address):
    registry = Registry(root)
    sender = registry.resolve_node_id(sender, field="sender", unknown_code="unknown_sender")
    active = set(registry.active_node_ids()) - {sender}
    if address.startswith("person:"):
        from .persons import person_records

        handle = validate_identifier(address[7:], "person")
        records = person_records(root)
        if not records:
            _refuse("address_person_source_missing",
                    "person groups need an operator-recorded person source in registry/persons.jsonl",
                    "record a membership with floati node person set --root ROOT --node NODE --person HANDLE")
        members = sorted(node for node in active
                         if node in records and records[node]["person"] == handle)
        if not members:
            _refuse("address_person_empty", f"person {handle!r} has no active recipients other than the sender")
        return members
    if address == "everyone":
        return sorted(active)
    if not address.startswith("repo:"):
        _refuse("address_invalid", "group address must be everyone or repo:NAME")
    name = _repository_name(address)
    from .consumption import ConsumptionLedger
    from .lane_workspaces import LaneWorkspaces

    members = set()
    lanes = LaneWorkspaces(root)
    for node in sorted(active):
        if any(row["repo"] == name for row in lanes._open_records(node)):
            members.add(node)
    for item in ConsumptionLedger(root).project().values():
        binding = item.get("car_binding")
        if (item.get("state") == "claimed" and item.get("holder") in active
                and isinstance(binding, dict) and binding.get("repository") == name):
            members.add(item["holder"])
    return sorted(members)


def _digest(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(b"floati-recipient-preview-v1\0" + encoded).hexdigest()


def recipient_preview(root, sender, address):
    recipients = resolve_recipients(root, sender, address)
    if not recipients:
        _refuse("address_empty", "group address resolves to no recipients other than the sender")
    payload = {"preview_version": 1, "tenant_id": root.tenant_id,
               "sender": sender, "address": address, "recipients": recipients}
    if address.startswith("person:"):
        from .persons import person_records

        # Bind all latest testimony, including its record identities, without
        # growing each envelope with unrelated or retired nodes' history.
        payload["person_source_digest"] = _digest(person_records(root))
    return dict(payload, digest=_digest(payload))


def validate_preview(value, refuse):
    """Validate saved metadata without reading today's roster or mutating it."""
    fields = {"preview_version", "tenant_id", "sender", "address", "recipients", "digest"}
    def invalid():
        refuse("address_preview_invalid", "recipient preview is malformed or its digest does not match")
    if not isinstance(value, dict) or set(value) not in (fields, fields | {"person_source_digest"}):
        invalid()
    if type(value["preview_version"]) is not int or value["preview_version"] != 1:
        invalid()
    try:
        validate_identifier(value["sender"], "sender")
        validate_identifier(value["tenant_id"], "tenant_id")
        recipients = value["recipients"]
        if not isinstance(recipients, list) or not 1 <= len(recipients) <= 4096:
            invalid()
        for node in recipients:
            validate_identifier(node, "recipient")
        if recipients != sorted(set(recipients)) or value["sender"] in recipients:
            invalid()
        address = value["address"]
        if not isinstance(address, str) or not is_group(address):
            invalid()
        if address.startswith("repo:"):
            _repository_name(address)
        if address.startswith("person:"):
            validate_identifier(address[7:], "person")
            basis = value.get("person_source_digest")
            if not isinstance(basis, str) or re.fullmatch(r"[0-9a-f]{64}", basis) is None:
                invalid()
        elif "person_source_digest" in value:
            invalid()
    except (ProtocolRefusal, TypeError, ValueError):
        invalid()
    payload = {key: item for key, item in value.items() if key != "digest"}
    if value["digest"] != _digest(payload):
        invalid()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_recipient_preview(path, *, commands=frozenset({"send", "ask"})):
    try:
        source = Path(path)
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
            raise ValueError("not a bounded ordinary file")
        raw = source.read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("preview too large")
        artifact = json.loads(raw, object_pairs_hook=_unique_object)
        if (not isinstance(artifact, dict) or type(artifact.get("artifact_version")) is not int
                or artifact.get("artifact_version") != 0 or artifact.get("status") != "ok"
                or not isinstance(artifact.get("command"), str)
                or artifact.get("command") not in commands):
            raise ValueError("not a preview artifact")
        return artifact.get("evidence")
    except (OSError, ValueError, UnicodeError, RecursionError):
        _refuse("address_preview_invalid", "recipient preview must be a saved JSON preview artifact")


def bound_preview(root, sender, address, path):
    """Caller holds the epoch barrier until all publications are finished."""
    if address.startswith("person:"):
        validate_identifier(address[7:], "person")
    if path is None:
        # Resolve first so a person address names its missing recorded source.
        resolve_recipients(root, sender, address)
        _refuse("address_preview_required", "a group send requires --recipient-preview PATH",
                "run with --preview, review and save the artifact, then pass --recipient-preview PATH")
    saved = load_recipient_preview(path)
    validate_preview(saved, _refuse)
    if (saved["tenant_id"], saved["sender"], saved["address"]) != (root.tenant_id, sender, address):
        _refuse("address_preview_invalid", "recipient preview names a different fleet, sender, or address")
    try:
        current = recipient_preview(root, sender, address)
    except ProtocolRefusal as exc:
        if exc.code not in {"address_empty", "address_person_empty", "address_person_source_missing"}:
            raise
        _refuse("address_preview_changed", "group source changed since the preview; nothing was sent")
    if current["digest"] != saved["digest"]:
        _refuse("address_preview_changed", "group recipients changed since the preview; nothing was sent")
    return saved


def group_key(key, recipient):
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        _refuse("idempotency_key_invalid", "group operations require an idempotency key of 1 to 128 characters",
                "pass --idempotency-key for this group operation")
    return hashlib.sha256((key + "\0" + recipient).encode("utf-8")).hexdigest()


@contextmanager
def group_write_guard(root):
    """An audience barrier grants no authority to write through epoch recovery."""
    from .bus_epoch import epoch_guard, _epoch_transition_pending

    with epoch_guard(root, exclusive=True):
        if _epoch_transition_pending(root):
            _refuse("epoch_roll_recovery_required",
                    "an incomplete epoch roll must be reconciled before group writes",
                    "reconcile the incomplete epoch roll before sending this group")
        yield


def send_group(root, args, claim):
    # Individual sends are nested inside the audience barrier. Preserve the
    # native policy preflight and maintenance boundary outside that barrier.
    from .epoch_rotation import observe_policy, maintenance

    with group_write_guard(root):
        hint = observe_policy(root, now=datetime.now(timezone.utc))
        status, evidence, exit_code = _send_group(root, args, claim)
    if hint["due"]:
        try:
            evidence["epoch_maintenance"] = maintenance(root, actor=args.sender, trigger="send")
        except Exception as exc:
            # Maintenance cannot undo committed send receipts. Keep the
            # result and name the failure without leaking exception details.
            evidence["epoch_maintenance"] = {
                "status": "maintenance_failed", "code": getattr(exc, "code", "epoch_maintenance_unavailable"),
            }
    return status, evidence, exit_code


def _send_group(root, args, claim):
    from .events import EventLog

    results = []
    with group_write_guard(root):
        preview = bound_preview(root, args.sender, args.recipient, getattr(args, "recipient_preview", None))
        for node in [args.sender, *preview["recipients"]]:
            Registry(root).require_protocol_lease(node, act="group send")
        # Fix the concrete audience before any send. No per-recipient call
        # re-resolves the address, even if an external reader observes a prefix.
        for node in preview["recipients"]:
            key = group_key(args.idempotency_key, node)
            try:
                receipt = EventLog(root).send(
                    args.sender, node, args.repo, args.sha, args.doc, args.note,
                    idempotency_key=key, claim=claim, address_preview=preview,
                    message_class=getattr(args, "message_class", "normal"),
                )
            except FloatiError as exc:
                if not results:
                    raise
                return "degraded", dict(preview, results=results, code="address_send_incomplete",
                                        failed_recipient=node, reason=exc.code), 35
            results.append({"recipient": node, "receipt": dict(receipt)})
    return "ok", dict(preview, results=results), 0

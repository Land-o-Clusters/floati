"""Recipient-authored ACTED testimony, independent of mail delivery and ACK."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from .bus_epoch import epoch_guard
from .cursor import SparseCursor
from .errors import IntegrityFailure, ProtocolRefusal
from .events import EVENT_KINDS, validate_event_records
from .ids import uuid7_hex
from .jsonl import (_decode_path_records, read_records_snapshot, read_records_compatible_snapshot,
                    read_records_sealed_snapshot, read_records_compatible_sealed_snapshot,
                    _transact_consumption_records)
from .records import WAKE_HOLD_KINDS, validate_record
from .registry import Registry
from .root import FloatiRoot
from .wake_control import validate_session_id
from .wake_hold import wake_coordination_guard


CONSUMPTION_KINDS = frozenset({"consumption_receipt"})


class MessageConsumption:
    def __init__(self, root: FloatiRoot) -> None:
        self.root = root
        self.cursor = SparseCursor(root)

    def _relative(self, node, worker_session_id=None):
        ack = self.cursor._relative_path_for(node, worker_session_id=worker_session_id)
        return Path("receipts/consumed").joinpath(*ack.relative_to("receipts/acks").parts)

    def _archive_records(self, prefix, relative, allowed_kinds, *, compatible=False, sealed=False):
        archive = self.root.tenant_home / prefix
        if prefix != Path() and archive.is_file() and not archive.is_symlink():
            from .epoch_history import read_archived_bytes
            from .jsonl import _unrecognized_rows
            raw = read_archived_bytes(archive, Path(relative).as_posix())
            unrecognized = {} if compatible else None
            rows = [] if not raw else _decode_path_records(
                archive / relative, self.root.tenant_id, frozenset(allowed_kinds), raw,
                unrecognized=unrecognized)
            if compatible:
                return rows, _unrecognized_rows(unrecognized or {})
            return rows
        if compatible:
            reader = (read_records_compatible_sealed_snapshot
                      if sealed and prefix == Path() else read_records_compatible_snapshot)
            return reader(
                self.root, prefix / relative, allowed_kinds=set(allowed_kinds))
        reader = read_records_sealed_snapshot if sealed and prefix == Path() else read_records_snapshot
        return reader(
            self.root, prefix / relative, allowed_kinds=set(allowed_kinds))

    def _frames(self, prefix=Path(), *, sealed=False):
        frames, _unrecognized = self._archive_records(
            prefix, "events.jsonl", EVENT_KINDS, compatible=True, sealed=sealed)
        validate_event_records(frames)
        return frames

    def _raw(self, node, *, sources=None, sealed=False):
        if sources is None:
            paths = [self._relative(node)]
            directory = self.root.resolve_relative(Path("receipts/consumed") / node)
            if directory.is_dir():
                paths.extend(path.relative_to(self.root.tenant_home) for path in sorted(directory.glob("*.jsonl")))
            reader = read_records_sealed_snapshot if sealed else read_records_snapshot
            batches = ((path, reader(self.root, path,
                        allowed_kinds=set(CONSUMPTION_KINDS))) for path in paths)
        else:
            # Native semantic parsing of caller-admitted bytes, with no path
            # resolution or glob. Supplying data grants no authoring authority.
            batches = ((Path(path), _decode_path_records(self.root.tenant_home / Path(path),
                        self.root.tenant_id, CONSUMPTION_KINDS, raw)) for path, raw in sources)
        result = []
        for path, rows in batches:
            for row in rows:
                if row["recipient"] != node or path != self._relative(node, row["worker_session_id"]):
                    raise IntegrityFailure("consumption_evidence_invalid", "consumption belongs to another recipient or worker plane")
                result.append(row)
        return result

    def _ack_reference(self, node, message, frames, *, prefix=Path(), snapshot=False, sealed=False):
        from .archive_consumption_replay import ack_references
        plane = message.get("worker_session_id")
        messages = [row for row in frames if row["kind"] == "message_envelope"]
        if prefix == Path():
            # This validates the entire native delivery/ACK plane, not only
            # the selected ACK's syntactic message membership.
            if sealed:
                self.cursor.state_for(node, messages, worker_session_id=plane, sealed=True)
            elif snapshot:
                self.cursor.state_for(node, messages, worker_session_id=plane, snapshot=True)
            else:
                self.cursor.state_for(node, messages, worker_session_id=plane)
        ack_path = self.cursor._relative_path_for(node, worker_session_id=plane)
        acks = self._archive_records(prefix, ack_path, {"ack_receipt"}, sealed=sealed)
        deliveries = self._archive_records(
            prefix, self.cursor._delivery_relative_path_for(node, worker_session_id=plane),
            set(WAKE_HOLD_KINDS), sealed=sealed,
        )
        return ack_references(self.root.tenant_id, node, message, messages, deliveries, acks)

    def _validated_history(self, node, frames, *, snapshot=False, raw_sources=None, sealed=False):
        from .archive_consumption_replay import (validate_consumed_envelope,
                                                validate_consumed_ack, validate_consumed_work)
        from .snapshot import _owned_epoch_archives

        messages = {row["id"]: row for row in frames if row["kind"] == "message_envelope"}
        archives = None
        seen = set()
        result = []
        raw = ((self._raw(node, sealed=True) if sealed else self._raw(node))
               if raw_sources is None else self._raw(node, sources=raw_sources))
        for receipt in raw:
            message_id = receipt["message_id"]
            if message_id in seen:
                raise IntegrityFailure("consumption_evidence_invalid", "duplicate consumption for one recipient envelope")
            seen.add(message_id)
            message = messages.get(message_id)
            source_frames, prefix = frames, Path()
            current = message is not None
            if message is None:
                if archives is None:
                    archives = [(path.relative_to(self.root.tenant_home), self._frames(path.relative_to(self.root.tenant_home)))
                                for path in _owned_epoch_archives(self.root)]
                for archive_prefix, archive_frames in archives:
                    found = next((row for row in archive_frames if row["kind"] == "message_envelope" and row["id"] == message_id), None)
                    if found is not None:
                        message, source_frames, prefix = found, archive_frames, archive_prefix
                        break
            validate_consumed_envelope(self.root.tenant_id, node, receipt, message)
            if sealed:
                acks = self._ack_reference(node, message, source_frames, prefix=prefix, sealed=True)
            else:
                acks = (self._ack_reference(node, message, source_frames, prefix=prefix, snapshot=True)
                        if snapshot else self._ack_reference(node, message, source_frames, prefix=prefix))
            validate_consumed_ack(self.root.tenant_id, receipt, acks)
            if receipt["source"] == "work_complete":
                from .consumption import ConsumptionLedger
                item = ConsumptionLedger(self.root).project().get(receipt["work_item_id"])
                validate_consumed_work(self.root.tenant_id, node, receipt, item)
            if current:
                result.append(receipt)
        return result

    def records(self, node, *, sealed=False):
        node = Registry(self.root).resolve_node_id(node)
        with epoch_guard(self.root):
            if sealed:
                return self._validated_history(node, self._frames(sealed=True), sealed=True)
            return self._validated_history(node, self._frames())

    def progress(self, node, item_ids=None, *, sealed=False, mail=None):
        # Historical cross-ledger gaps are observation evidence, not a new
        # authoring rule retroactively applied to every fleet reader.
        try:
            if mail is None and item_ids is None:
                from .mail_summary import observe
                from .send_prefix import _CACHE_ERRORS
                observed = observe(self.root)
                if observed is not None:
                    try:
                        return self.progress(node, sealed=sealed, mail=observed)
                    except _CACHE_ERRORS:
                        pass
            if mail is False:
                mail = None
            if mail is not None and item_ids is None:
                node = Registry(self.root).resolve_node_id(node)
                return dict(mail.node(node)["message_progress"])
            if sealed:
                return self._progress(node, item_ids, sealed=True)
            return self._progress(node, item_ids)
        except IntegrityFailure as error:
            return self.unknown_progress(error)

    @staticmethod
    def unknown_progress(error):
        return {"state": "unknown", "reason_code": error.code,
                "delivered": None, "acknowledged": None, "consumed": None}

    def _empty_progress_frames(self, node, *, sealed):
        """Borrow certified recipient absence only without consumption history.

        Empty requested output still validates the event source and every
        native receipt plane below. Existing recipient mail or consumption
        testimony needs the original complete source and history joins.
        """
        from .inbox_prefix import recipient_frames
        from .send_prefix import _CACHE_ERRORS
        try:
            frames = recipient_frames(self.root, node, read_only=True)
            if any(row["kind"] == "message_envelope" and row["recipient"] == node
                    for row in frames) or self._raw(node, sealed=sealed):
                return None
            return frames
        except _CACHE_ERRORS + (IntegrityFailure, ProtocolRefusal):
            # Speculation cannot reorder native source, receipt or history
            # failures. The unchanged full reader chooses their precedence.
            return None

    def _progress(self, node, item_ids=None, *, sealed=False):
        node = Registry(self.root).resolve_node_id(node)
        with epoch_guard(self.root):
            frames = self._empty_progress_frames(node, sealed=sealed) if item_ids is not None and not item_ids else None
            if frames is None:
                frames = self._frames(sealed=True) if sealed else self._frames()
            messages = [row for row in frames if row["kind"] == "message_envelope" and row["recipient"] == node]
            selected = {row["id"] for row in messages}
            if item_ids is not None:
                selected.intersection_update(item_ids)
            delivered, acknowledged = set(), set()
            # Plain receipt history exists independently of current envelopes.
            # Worker filenames are one-way session hashes: a nonempty receipt
            # plane with no current envelope cannot establish its identity.
            planes = {None} | {row.get("worker_session_id") for row in messages}
            for family, code, kinds in (
                ("acks", "ack_evidence_invalid", {"ack_receipt"}),
                ("deliveries", "delivery_evidence_invalid", set(WAKE_HOLD_KINDS)),
            ):
                known_paths = {
                    (self.cursor._relative_path_for(node, worker_session_id=plane)
                     if family == "acks" else self.cursor._delivery_relative_path_for(node, worker_session_id=plane))
                    for plane in planes
                }
                directory = self.root.resolve_relative(Path("receipts") / family / node)
                if directory.is_dir():
                    for path in sorted(directory.glob("*.jsonl")):
                        relative = path.relative_to(self.root.tenant_home)
                        if relative in known_paths:
                            continue
                        reader = read_records_sealed_snapshot if sealed else read_records_snapshot
                        rows = reader(self.root, relative, allowed_kinds=kinds)
                        if any(row.get("item_ids") or row.get("recipient") != node for row in rows):
                            raise IntegrityFailure(code, "receipt plane has no matching current recipient envelope")
            for plane in planes:
                if sealed:
                    _presented, acked = self.cursor.state_for(node, messages, worker_session_id=plane, sealed=True)
                    _shown, ackable = self.cursor.validate_deliveries(node, messages, worker_session_id=plane, sealed=True)
                else:
                    _presented, acked = self.cursor.state_for(node, messages, worker_session_id=plane)
                    _shown, ackable = self.cursor.validate_deliveries(node, messages, worker_session_id=plane)
                delivered.update(ackable)
                acknowledged.update(acked)
            history = (self._validated_history(node, frames, sealed=True) if sealed
                       else self._validated_history(node, frames))
            consumed = {row["message_id"] for row in history}
            return {"delivered": len(delivered & selected), "acknowledged": len(acknowledged & selected), "consumed": len(consumed & selected)}

    def consume(self, node, message_id, session_id, *, idempotency_key=None, now=None):
        return self.consume_many(node, [message_id], session_id, idempotency_key=idempotency_key, now=now)[0]

    def _automatic_source(self, message_id, frames):
        """Resolve the latest exact source without crossing a manual roll.

        Maintenance may run between ACK and ACTED. It grants no new authority:
        the archived native cut must still prove the recipient/session and
        complete delivery/ACK/hold/retraction contracts. A manual roll retains
        the existing explicit archive boundary for new consumption.
        """
        from .epoch_carry import ArchiveProofResolver, verified_hold_proofs
        from .archive_consumption_replay import automatic_predecessor_open
        from .wake_hold import project_wake_items
        head = frames[0] if frames else None
        if not automatic_predecessor_open(frames):
            return None
        resolver = ArchiveProofResolver(self.root, head=head)
        for archive in resolver.archives():
            if not automatic_predecessor_open([head] if head is not None else []):
                break
            prefix = archive.relative_to(self.root.tenant_home)
            archived = self._frames(prefix)
            message = next((row for row in archived if row["kind"] == "message_envelope"
                            and row["id"] == message_id), None)
            if message is not None:
                node, worker = message["recipient"], message.get("worker_session_id")
                source = resolver.source(archive, node, worker)
                proofs = verified_hold_proofs(self.root, source, node, worker, home=archive, resolver=resolver)
                project_wake_items(events=source[0], deliveries=source[2], acknowledgments=source[4],
                    recipient=node, worker_session_id=worker, tenant_id=self.root.tenant_id,
                    event_prefix_digests=source[1], delivery_prefix_digests=source[3],
                    acknowledgment_prefix_digests=source[5], _hold_proofs=proofs)
                return message, source[0], prefix
            head = archived[0] if archived else None
        return None

    def consume_many(self, node, message_ids: Sequence[str], session_id, *, work_item_id=None,
                     idempotency_key=None, now=None, ack_receipt_ids=None, node_lease_id=None):
        node = Registry(self.root).resolve_node_id(node)
        Registry(self.root).require_active(node)
        session = validate_session_id(session_id)
        if not isinstance(message_ids, (list, tuple)) or not 1 <= len(message_ids) <= 1000 or any(not isinstance(item, str) for item in message_ids) or len(set(message_ids)) != len(message_ids):
            raise ProtocolRefusal("consume_items_invalid", "consume requires 1 to 1000 unique message IDs", remedy="name 1 to 1000 unique envelope IDs; omit duplicate or empty consume arguments")
        exact_acks = ack_receipt_ids is not None
        if (exact_acks and (type(ack_receipt_ids) is not dict
                or set(ack_receipt_ids) != set(message_ids)
                or any(type(value) is not str or not value for value in ack_receipt_ids.values())
                or type(node_lease_id) is not str or not node_lease_id)) or (not exact_acks and node_lease_id is not None):
            raise ProtocolRefusal("consume_ack_binding_mismatch",
                "exact consumption requires one saved ACK reference per envelope and its node lease",
                remedy="use the exact saved native ACK map and original active node lease for this frozen batch")
        if exact_acks:
            ack_receipt_ids = dict(ack_receipt_ids)
        if idempotency_key is not None and len(message_ids) != 1:
            raise ProtocolRefusal("consume_idempotency_conflict", "one explicit consumption key cannot name multiple envelopes", remedy="use one explicit key for one envelope, or omit the key for a native completion batch")
        current = datetime.now(timezone.utc) if now is None else now
        if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
            raise ProtocolRefusal("time_invalid", "consumption requires an aware datetime")
        current = current.astimezone(timezone.utc)
        with epoch_guard(self.root), wake_coordination_guard(self.root, node):
            if exact_acks:
                lease = Registry(self.root).node_lease_state(node, now=current)
                if lease["node_lease_id"] != node_lease_id or lease["state"] != "active":
                    raise ProtocolRefusal("consume_ack_binding_mismatch",
                        "exact consumption requires its current active node lease",
                        remedy="retain the existing receipts; a replacement or expired lease cannot consume this dispatch")
            if work_item_id is not None:
                from .consumption import ConsumptionLedger
                item = ConsumptionLedger(self.root).project().get(work_item_id)
                if item is None:
                    raise ProtocolRefusal("work_unknown", "consumption work item does not exist")
                if item["state"] != "claimed" or item["holder"] != node:
                    raise ProtocolRefusal("work_holder_mismatch", "only the exact claim holder may consume for work completion")
            from .inbox_prefix import selected_frames
            frames = selected_frames(self.root, message_ids)
            from .consumed_prefix import history, selection
            certified = history(self.root, node)
            messages = {row["id"]: row for row in frames if row["kind"] == "message_envelope"}
            candidates = []
            for message_id in message_ids:
                message = messages.get(message_id)
                source_frames, prefix = frames, Path()
                if message is None:
                    automatic_source = self._automatic_source(message_id, frames)
                    if automatic_source is not None:
                        message, source_frames, prefix = automatic_source
                    else:
                        from .snapshot import _owned_epoch_archives
                        archived = any(any(row["kind"] == "message_envelope" and row["id"] == message_id for row in self._frames(path.relative_to(self.root.tenant_home))) for path in _owned_epoch_archives(self.root))
                        if archived:
                            raise ProtocolRefusal("consume_item_archived", f"message {message_id} is archived", remedy="do not consume archived mail; name an acknowledged envelope from the current epoch")
                        raise ProtocolRefusal("consume_item_unknown", f"message {message_id} is unknown", remedy="name an envelope listed by inbox for this recipient")
                if message["recipient"] != node:
                    raise ProtocolRefusal("consume_recipient_mismatch", f"message {message_id} belongs to another recipient", remedy="consume as the envelope's exact recipient in its declared workspace")
                if any(row["kind"] == "message_retracted" and row["retracted_message_id"] == message_id for row in source_frames):
                    raise ProtocolRefusal("consume_item_retracted", f"message {message_id} has been retracted", remedy="do not consume retracted mail; act on its current replacement envelope")
                plane = message.get("worker_session_id")
                if plane is not None and plane != session:
                    raise ProtocolRefusal("consume_session_mismatch", f"message {message_id} belongs to another worker session", remedy="consume only within the exact worker session bound to the envelope")
                if exact_acks and prefix == Path():
                    acks = self._ack_reference(node, message, self._frames(sealed=True), sealed=True)
                else:
                    acks = (self.cursor.ack_references_for(node, message) if prefix == Path()
                        else self._ack_reference(node, message, source_frames, prefix=prefix, sealed=True))
                if not acks:
                    raise ProtocolRefusal("consume_item_not_acknowledged", f"message {message_id} for {node} requires an explicit ACK first", remedy="explicitly acknowledge the named envelope, then retry consume")
                selected_ack = acks[0]
                if exact_acks:
                    selected_ack = next((ack for ack in acks if ack["id"] == ack_receipt_ids[message_id]), None)
                    if (selected_ack is None or selected_ack.get("acting_session_id") != session
                        or selected_ack.get("node_lease_id") != node_lease_id
                        or selected_ack.get("node_lease_state_at_ack") != "active"):
                        raise ProtocolRefusal("consume_ack_binding_mismatch",
                            "saved ACK differs from the exact envelope, acting session or node lease",
                            remedy="select the authenticated provider session ACK saved by this dispatch, without substituting another session")
                receipt = {
                    "schema_version": 1, "kind": "consumption_receipt", "id": "consume-" + uuid7_hex(),
                    "tenant_id": self.root.tenant_id, "timestamp": current.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "recipient": node, "message_id": message_id, "worker_session_id": plane,
                    "acting_session_id": session, "ack_receipt_id": selected_ack["id"],
                    "source": "work_complete" if work_item_id is not None else "explicit", "work_item_id": work_item_id,
                    "idempotency_key": idempotency_key if idempotency_key is not None else message_id,
                }
                candidates.append(validate_record(receipt, self.root.tenant_id, CONSUMPTION_KINDS, integrity=False))
            # Validate every envelope before the first write. Partial durable
            # consumption after a storage failure is legal; retry dedupes.
            binding_fields = ("recipient", "message_id", "worker_session_id", "acting_session_id",
                              "ack_receipt_id", "source", "work_item_id", "idempotency_key")

            def require_exact_prior(existing, candidate):
                if exact_acks and any(existing[field] != candidate[field] for field in binding_fields):
                    raise ProtocolRefusal("consume_idempotency_conflict",
                        "prior consumption differs from its exact saved ACK, session or work binding",
                        remedy="retain the conflicting consumption for investigation; do not append replacement evidence")

            for receipt in candidates:
                certified.check_key(receipt)
                existing = None
                for ledger in certified.ledgers.values():
                    existing = ledger.get("message", receipt["message_id"])
                    if existing is not None:
                        break
                if existing is not None:
                    require_exact_prior(existing, receipt)
            result = []
            for receipt in candidates:
                def decide(rows, candidate=receipt):
                    existing = next((row for row in rows if row["message_id"] == candidate["message_id"]), None)
                    if existing is not None:
                        require_exact_prior(existing, candidate)
                    return (existing, []) if existing is not None else (candidate, [candidate])
                result.append(_transact_consumption_records(
                    self.root, self._relative(node, receipt["worker_session_id"]), decide,
                    _consumption_selection=selection(certified,
                        self._relative(node, receipt["worker_session_id"]), receipt)))
            return result

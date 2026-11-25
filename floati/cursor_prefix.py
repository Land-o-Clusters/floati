"""Disposable authenticated delivery joins and selected ACK transactions.

Only complete native validation creates a checkpoint. A changed event
generation or unproved delivery source replays every delivery reference.
Full cursor projections continue to consume their authoritative ledgers.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import jsonl, send_prefix
from .errors import IntegrityFailure, ProtocolRefusal
from .key_prefix import KeyPrefix, bucket_index
from .records import WAKE_HOLD_KINDS


ACK_KINDS = frozenset({"ack_receipt"})
CACHE_NAME_PREFIX = ".cursor-prefix-"


def _retry_key(session, item_ids):
    # Item order and session spelling are part of the native retry contract.
    return json.dumps([session, list(item_ids)], ensure_ascii=True, separators=(",", ":"))


class _CursorPrefix(KeyPrefix):
    def __init__(self, root, relative, kinds, name, *, revision=None, read_only=False):
        relative = Path(relative)
        super().__init__(root, kinds, revision=revision, relative=relative,
            cache_name=CACHE_NAME_PREFIX + hashlib.sha256(relative.as_posix().encode()).hexdigest(),
            format=name, folds={"mail": 1}, read_only=read_only)
        self.binding["relative"] = relative.as_posix()

    def rebuild(self):
        before = send_prefix._identity(self.path)
        jsonl._read_path_records(self.path, self.root.tenant_id, self.kinds)
        data = self.path.read_bytes() if before is not None else b""
        if before != send_prefix._identity(self.path) or len(data) != (0 if before is None else before[4]):
            raise IntegrityFailure("ledger_identity_changed_during_read", "cursor ledger changed during validation")
        rows = jsonl._decode_path_records(self.path, self.root.tenant_id, self.kinds, data)
        entries = self.entries(rows)
        if before != send_prefix._identity(self.path):
            raise IntegrityFailure("ledger_identity_changed_during_read", "cursor ledger changed during validation")
        self.check_sources()
        self.rebuild_entries(rows, entries, before, data)

    def check_sources(self):
        pass

    def _mail_envelopes(self, rows):
        from .mail_summary import event_item
        proof = self.proof if hasattr(self, "proof") else self.delivery.proof
        prefix = proof._prefix
        if prefix is None:
            raise send_prefix.CacheMiss()
        return {identity: event_item(prefix, identity) for row in rows for identity in row["item_ids"]}

    def _mail_entries(self, rows, entries):
        from .mail_summary import cursor_fold
        self._mail_rebuilt = cursor_fold(rows, self._mail_envelopes(rows), entries)
        return entries

    def _fold(self, rows, prior):
        from .mail_summary import cursor_fold, bank_tree
        if not prior:
            return {"mail": self._mail_rebuilt}
        return {"mail": cursor_fold(rows, self._mail_envelopes(rows), {},
            prior["mail"], bank_tree(self))}

    def _set_first(self, domain, value, row):
        # During appended(), the native ledger has already advanced while
        # this generation deliberately retains its old sealed identity until
        # publication. Use the same authenticated bucket/member operations as
        # set(), rather than get()'s pre-append source-identity assertion.
        key = send_prefix._key(domain, value)
        members = self._read_bucket(bucket_index(self.secret, self.payload["generation"], key))
        if key in members:
            self._read_entry(key)
        else:
            self.set(domain, value, row)

    def _existing_during_append(self, domain, value):
        key = send_prefix._key(domain, value)
        members = self._read_bucket(bucket_index(self.secret, self.payload["generation"], key))
        if key not in members:
            raise send_prefix.CacheMiss()
        return self._read_entry(key)


class AckPrefix(_CursorPrefix):
    def __init__(self, root, relative, *, revision=None, worker_session_id=None,
                 sources=None, read_only=False):
        from .cursor import SparseCursor
        relative = Path(relative)
        if (len(relative.parts) not in {3, 4}
                or relative.parts[:2] != ("receipts", "acks")
                or relative.suffix != ".jsonl"):
            raise send_prefix.CacheMiss()
        self.node = relative.stem if len(relative.parts) == 3 else relative.parts[2]
        self.worker_session_id = worker_session_id
        if relative != SparseCursor(root)._relative_path_for(
                self.node, worker_session_id=worker_session_id):
            raise send_prefix.CacheMiss()
        self.delivery = (_ack_sources(root, self.node, worker_session_id, revision)
                         if sources is None else sources)
        if self.delivery is None:
            raise send_prefix.CacheMiss()
        super().__init__(root, relative, ACK_KINDS, "ack-prefix-v4", revision=revision,
            read_only=read_only)
        self.binding.update(node=self.node, worker_session_id=worker_session_id,
            delivery=self._delivery_binding())

    def _delivery_binding(self):
        self.delivery.check_sources()
        self.delivery._assert_current()
        return {"generation": self.delivery.payload["generation"],
                "binding": self.delivery.binding}

    def check_sources(self):
        if self.binding["delivery"] != self._delivery_binding():
            raise send_prefix.CacheMiss()

    def validate_rows(self, rows):
        # Certification covers the complete native ACK plane, including
        # references unrelated to the selected consumption candidate.
        for row in rows:
            if row.get("recipient") != self.node:
                raise IntegrityFailure("ack_evidence_invalid", "ack receipt belongs to another contract")
            for item_id in row["item_ids"]:
                if self.delivery.get("ackable", item_id) is not True:
                    raise IntegrityFailure("ack_evidence_invalid",
                        "ack does not correspond to delivered session evidence")
        self.check_sources()

    def _consumption_complete(self, rows):
        return all(self.delivery.get("delivered", item_id) is not None
            for row in rows for item_id in row["item_ids"])

    def _mail_entries(self, rows, entries):
        from datetime import datetime
        entries = super()._mail_entries(rows, entries)
        # This is observation testimony, never an ACK authoring condition.
        # A cold rebuild has the complete native delivery transcript and can
        # certify the exact minimum-per-item temporal health comparison.
        self.delivery._assert_current()
        delivery_rows = jsonl._read_path_records(self.delivery.path, self.root.tenant_id,
            self.delivery.kinds)
        self.delivery._assert_current()
        delivered, acknowledged = {}, {}
        for source, target in ((delivery_rows, delivered), (rows, acknowledged)):
            for row in source:
                stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                for identity in row["item_ids"]:
                    if identity not in target or stamp < target[identity]:
                        target[identity] = stamp
        self._mail_rebuilt["health_safe"] = all(
            identity in delivered and int((stamp-delivered[identity]).total_seconds()) >= 0
            for identity, stamp in acknowledged.items())
        return entries

    def _fold(self, rows, prior):
        from datetime import datetime
        result = super()._fold(rows, prior)
        if prior:
            # False means that temporal safety is unproved, not that the
            # native history is invalid. Only a complete cold rebuild can
            # recertify a false value; readers fall back to native health.
            latest = self.delivery.payload["folds"]["mail"]["latest_utc"]
            upper = None if latest is None else datetime.fromisoformat(latest.replace("Z", "+00:00"))
            result["mail"]["health_safe"] = prior["mail"].get("health_safe") is True and all(
                not row["item_ids"] or upper is not None and int((datetime.fromisoformat(
                    row["timestamp"].replace("Z", "+00:00"))-upper).total_seconds()) >= 0
                for row in rows)
        return result

    def entries(self, rows):
        self.validate_rows(rows)
        entries = {send_prefix._key("id", row["id"]): row for row in rows}
        for row in rows:
            # Last match wins, as in SparseCursor's reversed native scan.
            key = _retry_key(row.get("acting_session_id"), row["item_ids"])
            entries[send_prefix._key("retry", key)] = row
            for item_id in row["item_ids"]:
                # New consumption has always selected acks[0], in physical
                # order. Historical consumption retains the full native join.
                entries.setdefault(send_prefix._key("message", item_id), row)
        entries[send_prefix._key("consumption", "complete")] = self._consumption_complete(rows)
        return self._mail_entries(rows, entries)

    def _advance(self, batch, encoded):
        self.validate_rows(batch)
        complete = self._existing_during_append("consumption", "complete")
        if type(complete) is not bool:
            raise send_prefix.CacheMiss()
        # Holds remain lawful ACK evidence. A new hold-only ACK cannot turn
        # an immutable true consumption certificate into false in this bank;
        # leave its seal stale and retain the already durable native ACK.
        self.set("consumption", "complete", complete and self._consumption_complete(batch))
        for row in batch:
            self.set("id", row["id"], row)
            self.set("retry", _retry_key(row.get("acting_session_id"), row["item_ids"]), row)
            for item_id in row["item_ids"]:
                self._set_first("message", item_id, row)
        for frame in encoded:
            self.payload["head"] = hashlib.sha256(bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        self.payload["count"] += len(batch)


def _validated_membership(rows, events, node, worker_session_id):
    events = {str(row["id"]): row for row in events}
    ackable = set()
    for row in rows:
        if row.get("recipient") != node:
            raise IntegrityFailure("delivery_evidence_invalid", "delivery belongs to another recipient")
        for item_id in row["item_ids"]:
            event = events.get(str(item_id))
            if (event is None or event.get("recipient") != node
                    or event.get("worker_session_id") != worker_session_id):
                raise IntegrityFailure("delivery_evidence_invalid",
                    "delivery references missing or wrong-session recipient evidence")
            ackable.add(str(item_id))
    return ackable


def _event_binding(root, proof):
    if type(proof) is not send_prefix._SelectedRows or proof._prefix is None:
        raise send_prefix.CacheMiss()
    prefix = proof._prefix
    identity = send_prefix._identity(root.resolve_relative("events.jsonl"))
    if prefix.root != root or prefix.payload["identity"] != identity:
        raise send_prefix.CacheMiss()
    generation = prefix.payload.get("generation")
    if not isinstance(generation, str) or send_prefix._GENERATION.fullmatch(generation) is None:
        raise send_prefix.CacheMiss()
    return {"generation": generation, "binding": prefix.binding}


class DeliveryPrefix(_CursorPrefix):
    def __init__(self, root, relative, node, worker_session_id, proof, *, revision=None,
                 read_only=False):
        super().__init__(root, relative, WAKE_HOLD_KINDS, "delivery-join-prefix-v4", revision=revision,
            read_only=read_only)
        self.node, self.worker_session_id, self.proof = node, worker_session_id, proof
        self.binding.update(node=node, worker_session_id=worker_session_id,
            events=_event_binding(root, proof))

    def check_sources(self):
        if self.binding["events"] != _event_binding(self.root, self.proof):
            raise send_prefix.CacheMiss()

    def entries(self, rows):
        wanted = sorted({item for row in rows for item in row["item_ids"]})
        events, _retracted, proof = send_prefix.event_references(self.root, wanted, previous=self.proof)
        if self.binding["events"] != _event_binding(self.root, proof):
            raise send_prefix.CacheMiss()
        ackable = _validated_membership(rows, events, self.node, self.worker_session_id)
        entries = {send_prefix._key("id", row["id"]): row for row in rows}
        entries.update({send_prefix._key("ackable", item): True for item in ackable})
        for row in rows:
            if row["kind"] in {"delivery_receipt", "delivered_unpresented"}:
                for item_id in row["item_ids"]:
                    entries.setdefault(send_prefix._key("delivered", item_id), row)
        return self._mail_entries(rows, entries)

    def _advance(self, batch, encoded):
        for row in batch:
            self.set("id", row["id"], row)
            for item_id in row["item_ids"]:
                self.set("ackable", item_id, True)
                if row["kind"] in {"delivery_receipt", "delivered_unpresented"}:
                    self._set_first("delivered", item_id, row)
        for frame in encoded:
            self.payload["head"] = hashlib.sha256(bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        self.payload["count"] += len(batch)


def _open(factory):
    try:
        prefix = factory()
        try:
            prefix.load()
        except send_prefix._CACHE_ERRORS:
            prefix.rebuild()
        prefix.check_sources()
        return prefix
    except send_prefix._CACHE_ERRORS:
        return None


def _revision():
    try:
        return send_prefix._revision()
    except send_prefix._CACHE_ERRORS:
        return None


def _ack_sources(root, node, worker_session_id, revision):
    """Open the already admitted delivery join before taking the ACK lock."""
    from .cursor import SparseCursor
    _events, _retracted, proof = send_prefix.event_references(root, [])
    relative = SparseCursor(root)._delivery_relative_path_for(
        node, worker_session_id=worker_session_id)
    path, _tenant = jsonl._resolve(root, relative, write=False)
    lock, lock_relative = jsonl._lock_beside(path, relative)
    with jsonl._epoch_writer_guard(root, relative, path=path), jsonl._locked_path(
            lock, exclusive=True, relative=lock_relative):
        return _open(lambda: DeliveryPrefix(root, relative, node,
            worker_session_id, proof, revision=revision))


def selected_ack_references(root, node, message):
    """Return the first real current ACK after complete plane certification."""
    from .archive_consumption_replay import ack_references
    from .cursor import SparseCursor
    worker = message.get("worker_session_id")
    cursor = SparseCursor(root)
    relative = cursor._relative_path_for(node, worker_session_id=worker)
    path, _tenant = jsonl._resolve(root, relative, write=False)
    lock, lock_relative = jsonl._lock_beside(path, relative)
    revision = _revision()
    delivery = _ack_sources(root, node, worker, revision)
    with jsonl._epoch_writer_guard(root, relative, path=path), jsonl._locked_path(
            lock, exclusive=True, relative=lock_relative):
        prefix = (_open(lambda: AckPrefix(root, relative, revision=revision,
            worker_session_id=worker, sources=delivery)) if delivery is not None else None)
        for attempt in range(2):
            if prefix is None:
                break
            try:
                if prefix.get("consumption", "complete") is not True:
                    # False may become true after an actual delivery append.
                    # Re-evaluate the complete native ACK plane before either
                    # reusing or refusing its consumption testimony.
                    prefix.rebuild()
                    if prefix.get("consumption", "complete") is not True:
                        raise IntegrityFailure("consumption_evidence_invalid",
                            "ACK has no matching delivery")
                acknowledgment = prefix.get("message", message["id"])
                delivered = delivery.get("delivered", message["id"])
                deliveries = {} if delivered is None else {delivered["id"]: delivered}
                # Native receipts retain their entire batch. Every item in
                # the selected ACK needs its real delivery, and every item in
                # those delivery rows needs its actual envelope. Selecting one
                # candidate never grants authority to narrow item_ids.
                if acknowledgment is not None:
                    for item_id in acknowledgment["item_ids"]:
                        row = delivery.get("delivered", item_id)
                        if row is not None:
                            deliveries[row["id"]] = row
                wanted = {message["id"]}
                if acknowledgment is not None:
                    wanted.update(acknowledgment["item_ids"])
                for row in deliveries.values():
                    wanted.update(row["item_ids"])
                events, _retracted, proof = send_prefix.event_references(
                    root, sorted(wanted), previous=delivery.proof)
                if delivery.binding["events"] != _event_binding(root, proof):
                    raise send_prefix.CacheMiss()
                prefix.check_sources()
                prefix._assert_current()
                # The original native candidate join still judges the real
                # selected receipt rows. A hold is ackable, but is not an
                # actual delivery for MessageConsumption's own ACK reference.
                return ack_references(root.tenant_id, node, message, events,
                    list(deliveries.values()),
                    [] if acknowledgment is None else [acknowledgment])[:1]
            except send_prefix._CACHE_ERRORS:
                if attempt == 0:
                    try:
                        prefix.rebuild()
                    except send_prefix._CACHE_ERRORS:
                        prefix = None
                else:
                    prefix = None
    # No missing cache proof establishes absence. Preserve the original full
    # native cursor validation and matching rules if selected evidence cannot
    # be certified; the historical consumption path continues to use it too.
    from .message_consumption import MessageConsumption
    consumption = MessageConsumption(root)
    return consumption._ack_reference(node, message, consumption._frames(sealed=True), sealed=True)[:1]


def _assert_transaction_prefix(prefix):
    if prefix is not None:
        try:
            prefix._assert_current()
        except send_prefix._CACHE_ERRORS as error:
            raise IntegrityFailure("cursor_prefix_changed_during_transaction",
                "the certified ledger or exact cache namespace changed before the transaction boundary") from error


def delivery_membership(root, node, requested, *, worker_session_id=None, proof=None):
    """Return selected membership only after certifying the entire delivery plane."""
    from .cursor import SparseCursor
    coordinate = SparseCursor(root)._delivery_relative_path_for(node, worker_session_id=worker_session_id)
    path, tenant = jsonl._resolve(root, coordinate, write=False)
    lock, lock_relative = jsonl._lock_beside(path, coordinate)
    revision = _revision()
    with jsonl._epoch_writer_guard(root, coordinate, path=path), jsonl._locked_path(
            lock, exclusive=True, relative=lock_relative):
        prefix = _open(lambda: DeliveryPrefix(root, coordinate, node, worker_session_id, proof, revision=revision))
        for attempt in range(2):
            if prefix is None:
                break
            try:
                selected = frozenset(item for item in requested if prefix.get("ackable", item) is True)
                prefix.check_sources()
                prefix._assert_current()
                return selected
            except send_prefix._CACHE_ERRORS:
                if attempt == 0:
                    try:
                        prefix.rebuild()
                    except send_prefix._CACHE_ERRORS:
                        prefix = None
        # Missing authenticated paths never prove absence. Replay every native
        # reference, including historical items outside the requested batch.
        rows = jsonl._read_path_records(path, tenant, WAKE_HOLD_KINDS)
        wanted = sorted({item for row in rows for item in row["item_ids"]})
        events, _retracted, _proof = send_prefix.event_references(root, wanted, previous=proof)
        return frozenset(_validated_membership(rows, events, node, worker_session_id).intersection(requested))


def transact_delivery(root, node, requested, decide, *, worker_session_id=None, _before_append=None):
    """Append a native plain/unpresented receipt and preserve certified joins.

    The caller still owns presenter/bridge admission. This closed transaction
    supplies only the exact physical count needed to construct that receipt;
    it grants no authority to author controller-held delivery evidence.
    """
    from .cursor import SparseCursor
    coordinate = SparseCursor(root)._delivery_relative_path_for(node, worker_session_id=worker_session_id)
    # Certify events before recipient evidence, preserving ACK's integrity
    # precedence and retaining current selected references for this append.
    events, _retracted, proof = send_prefix.event_references(root, requested)
    path, tenant = jsonl._resolve(root, coordinate, write=True)
    lock, lock_relative = jsonl._lock_beside(path, coordinate)
    revision = _revision()
    with jsonl._epoch_writer_guard(root, coordinate, path=path), jsonl._locked_path(
            lock, exclusive=True, relative=lock_relative):
        prefix = _open(lambda: DeliveryPrefix(root, coordinate, node, worker_session_id, proof, revision=revision))
        for attempt in range(2):
            if prefix is None:
                break
            try:
                count = prefix.payload["count"]
                result, row = decide(count)
                duplicate = row is not None and prefix.get("id", row["id"]) is not None
                prefix.check_sources()
                break
            except send_prefix._CACHE_ERRORS:
                if attempt == 0:
                    try:
                        prefix.rebuild()
                    except send_prefix._CACHE_ERRORS:
                        prefix = None
                else:
                    prefix = None
        if prefix is None:
            prior = jsonl._read_path_records(path, tenant, WAKE_HOLD_KINDS)
            all_items = sorted({item for receipt in prior for item in receipt["item_ids"]})
            prior_events, _retracted, _proof = send_prefix.event_references(root, all_items, previous=proof)
            _validated_membership(prior, prior_events, node, worker_session_id)
            count = len(prior)
            result, row = decide(count)
            duplicate = row is not None and any(item["id"] == row["id"] for item in prior)
        if row is None:
            _assert_transaction_prefix(prefix)
            return result
        if count >= jsonl.MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {jsonl.MAX_LEDGER_RECORDS} records")
        encoded = jsonl._encode_record(row, tenant, frozenset(WAKE_HOLD_KINDS))
        if (row["kind"] not in {"delivery_receipt", "delivered_unpresented"}
                or row["recipient"] != node or row["item_ids"] != requested
                or row["presentation_count"] != count + 1):
            raise ProtocolRefusal("delivery_transaction_mismatch",
                "native presentation append must match its selected items, recipient and physical count")
        selected_events, _retracted, _proof = send_prefix.event_references(root, requested, previous=proof)
        _validated_membership([row], selected_events, node, worker_session_id)
        if duplicate:
            raise ProtocolRefusal("duplicate_record_id", f"record id {row['id']} already exists")
        _assert_transaction_prefix(prefix)
        if _before_append is not None:
            _before_append()
        identity = jsonl._append_frame(path, encoded)
        if prefix is not None:
            try:
                prefix.check_sources()
                prefix.appended([row], [encoded], identity)
            except send_prefix._CACHE_ERRORS:
                pass  # Durable native evidence remains; an old seal forces full recovery.
        jsonl.publish_ledger_hints(root, coordinate, (row,))
        return result


def transact_ack(root, relative, requested, acting_session, decide, *, worker_session_id=None):
    """The native ACK transaction, selecting only its exact retry and ID paths."""
    coordinate = Path(relative)
    if coordinate.parts[:2] != ("receipts", "acks"):
        raise ValueError("indexed ACK transaction requires its native receipt plane")
    path, tenant = jsonl._resolve(root, coordinate, write=True)
    lock, lock_relative = jsonl._lock_beside(path, coordinate)
    revision = _revision()
    from .cursor import SparseCursor
    node = coordinate.stem if len(coordinate.parts) == 3 else coordinate.parts[2]
    sources = _ack_sources(root, node, worker_session_id, revision)
    with jsonl._epoch_writer_guard(root, coordinate, path=path), jsonl._locked_path(
            lock, exclusive=True, relative=lock_relative):
        prefix = (_open(lambda: AckPrefix(root, coordinate, revision=revision,
            worker_session_id=worker_session_id, sources=sources)) if sources is not None else None)
        for attempt in range(2):
            if prefix is None:
                break
            try:
                existing = prefix.get("retry", _retry_key(acting_session, requested))
                result, row = decide([] if existing is None else [existing])
                duplicate = row is not None and prefix.get("id", row["id"]) is not None
                count = prefix.payload["count"]
                break
            except send_prefix._CACHE_ERRORS:
                if attempt == 0:
                    try:
                        prefix.rebuild()
                    except send_prefix._CACHE_ERRORS:
                        prefix = None
                else:
                    prefix = None
        if prefix is None:
            prior = jsonl._read_path_records(path, tenant, ACK_KINDS)
            existing = next((item for item in reversed(prior)
                if item.get("acting_session_id") == acting_session and item["item_ids"] == requested), None)
            result, row = decide(prior)
            duplicate = row is not None and any(item["id"] == row["id"] for item in prior)
            count = len(prior)
        if row is None:
            _assert_transaction_prefix(prefix)
            return result
        if count >= jsonl.MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {jsonl.MAX_LEDGER_RECORDS} records")
        encoded = jsonl._encode_record(row, tenant, ACK_KINDS)
        expected_plane = SparseCursor(root)._relative_path_for(row["recipient"], worker_session_id=worker_session_id)
        if (row.get("acting_session_id") != acting_session or row["item_ids"] != requested
                or coordinate != expected_plane or existing is not None):
            raise ProtocolRefusal("ack_transaction_mismatch",
                "selected ACK append must match one new exact retry in its recipient and worker plane")
        if duplicate:
            raise ProtocolRefusal("duplicate_record_id", f"record id {row['id']} already exists")
        if prefix is not None:
            prefix.validate_rows([row])
        _assert_transaction_prefix(prefix)
        identity = jsonl._append_frame(path, encoded)
        if prefix is not None:
            try:
                prefix.appended([row], [encoded], identity)
            except send_prefix._CACHE_ERRORS:
                pass  # Native append is durable; its stale checkpoint forces recovery.
        jsonl.publish_ledger_hints(root, coordinate, (row,))
        return result


def warm(root, node, *, worker_session_id=None):
    """Certify restored fixture inputs outside measurement without appending."""
    from .bus_epoch import epoch_guard
    from .cursor import SparseCursor
    from .wake_hold import wake_coordination_guard
    with epoch_guard(root, exclusive=False), wake_coordination_guard(root, node, worker_session_id=worker_session_id):
        _events, _retracted, proof = send_prefix.event_references(root, [])
        revision = _revision()
        cursor = SparseCursor(root)
        delivery = cursor._delivery_relative_path_for(node, worker_session_id=worker_session_id)
        ack = cursor._relative_path_for(node, worker_session_id=worker_session_id)
        result = {}
        for name, coordinate, factory in (
                ("delivery", delivery, lambda: DeliveryPrefix(root, delivery, node,
                    worker_session_id, proof, revision=revision)),
                ("ack", ack, lambda: AckPrefix(root, ack, revision=revision,
                    worker_session_id=worker_session_id))):
            path, _tenant = jsonl._resolve(root, coordinate, write=True)
            lock, lock_relative = jsonl._lock_beside(path, coordinate)
            with jsonl._epoch_writer_guard(root, coordinate, path=path), jsonl._locked_path(
                    lock, exclusive=True, relative=lock_relative):
                try:
                    prefix = factory()
                    # Fixture warming explicitly certifies the entire map,
                    # even when an old checkpoint has missing unselected nodes.
                    prefix.rebuild()
                    result[name] = {"identity": prefix.payload["identity"],
                        "count": prefix.payload["count"], "head": prefix.payload["head"]}
                except send_prefix._CACHE_ERRORS:
                    return None
        return result

"""Disposable testimony that one consumption ledger passed its native joins.

Only a complete native history join certifies a source identity. Later event,
delivery and ACK appends do not invalidate those append-monotone facts; new
candidates still pass their own native admission before this writer is used.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import jsonl, records, send_prefix
from .errors import IntegrityFailure, ProtocolRefusal
from .key_prefix import KeyPrefix


KINDS = frozenset({"consumption_receipt"})
FORMAT = "consumed-prefix-v2"


def cache_name(relative):
    return ".consumed-prefix-" + hashlib.sha256(
        Path(relative).as_posix().encode("utf-8")).hexdigest()


def _revision():
    return {"source": send_prefix._revision(), "reader_version": records.READER_VERSION}


def _mail_source_vector(root, node):
    """Capture the complete current native join inputs without reading them."""
    from .cursor import SparseCursor
    cursor = SparseCursor(root)
    paths = {Path("events.jsonl"), Path("work/items.jsonl"),
        cursor._relative_path_for(node), cursor._delivery_relative_path_for(node)}
    for family in ("acks", "deliveries"):
        directory = root.resolve_relative(Path("receipts") / family / node)
        if directory.is_dir():
            paths.update(path.relative_to(root.tenant_home) for path in directory.glob("*.jsonl"))
    return {relative.as_posix(): send_prefix._identity(root.resolve_relative(relative))
        for relative in sorted(paths)}


def _native(root, relative, node):
    """Read exact detached bytes and complete the native join before sealing."""
    from .message_consumption import MessageConsumption
    path = root.resolve_relative(relative)
    before = send_prefix._identity(path)
    rows = jsonl.read_records_sealed_snapshot(root, relative, allowed_kinds=set(KINDS))
    raw = path.read_bytes() if before is not None else b""
    if before != send_prefix._identity(path):
        raise IntegrityFailure("ledger_identity_changed_during_read",
            "consumption ledger changed during native validation")
    controller = MessageConsumption(root)
    frames = controller._frames(sealed=True) if rows else []
    controller._validated_history(node, frames,
        raw_sources=[(Path(relative), raw)], sealed=True)
    if before != send_prefix._identity(path):
        raise IntegrityFailure("ledger_identity_changed_during_read",
            "consumption ledger changed during native validation")
    return rows, before, raw


class ConsumedPrefix(KeyPrefix):
    def __init__(self, root, relative, node, *, revision=None, read_only=False):
        self.relative, self.node = Path(relative), node
        super().__init__(root, KINDS, relative=self.relative,
            cache_name=cache_name(self.relative), format=FORMAT,
            revision=_revision() if revision is None else revision, folds={"mail": 1},
            read_only=read_only)

    def rebuild(self):
        source_vector = _mail_source_vector(self.root, self.node)
        rows, before, raw = _native(self.root, self.relative, self.node)
        entries = {}
        for row in rows:
            entries[send_prefix._key("id", row["id"])] = row
            entries[send_prefix._key("message", row["message_id"])] = row
            entries.setdefault(send_prefix._key("key", row["idempotency_key"]), row)
        from .mail_summary import _load_events, memory_tree, event_item
        events = _load_events(self.root)
        tree = memory_tree(entries)
        members = None
        for row in rows:
            if events.get("id", row["message_id"]) is not None:
                members = tree.add(members, event_item(events, row["message_id"]))
        self._mail_rebuilt = {"members": members,
            "event_generation": events.payload["generation"],
            "dependencies": self._mail_dependencies(events, rows[0]["worker_session_id"],
                any(row["source"] == "work_complete" for row in rows)) if rows else None}
        if source_vector != _mail_source_vector(self.root, self.node):
            raise send_prefix.CacheMiss()
        self.rebuild_entries(rows, entries, before, raw)

    def _mail_dependencies(self, events, plane, with_work):
        from .cursor import SparseCursor
        from .cursor_prefix import DeliveryPrefix, AckPrefix
        cursor = SparseCursor(self.root)
        proof = send_prefix._SelectedRows([], events, None, None)
        delivery = DeliveryPrefix(self.root,
            cursor._delivery_relative_path_for(self.node, worker_session_id=plane),
            self.node, plane, proof, read_only=True)
        delivery.load()
        delivery.check_sources()
        ack = AckPrefix(self.root, cursor._relative_path_for(self.node, worker_session_id=plane),
            worker_session_id=plane, sources=delivery, read_only=True)
        ack.load()
        ack.check_sources()
        def binding(prefix):
            return {"generation": prefix.payload["generation"], "binding": prefix.binding}
        return {"worker_session_id": plane, "events": binding(events),
            "delivery": binding(delivery), "ack": binding(ack),
            "work": {"path": "work/items.jsonl", "identity": send_prefix._identity(
                self.root.resolve_relative("work/items.jsonl"))} if with_work else None}

    def _check_mail_dependencies(self, events):
        if self.payload["folds"]["mail"]["event_generation"] != events.payload["generation"]:
            raise send_prefix.CacheMiss()
        dependencies = self.payload["folds"]["mail"]["dependencies"]
        if dependencies is not None and dependencies != self._mail_dependencies(events,
                dependencies["worker_session_id"], dependencies["work"] is not None):
            raise send_prefix.CacheMiss()
        if dependencies is None and self.payload["count"]:
            raise send_prefix.CacheMiss()

    def check_mail_sources(self, events):
        self._check_mail_dependencies(events)
        self._assert_current()

    def check_sources(self):
        from .mail_summary import _load_events
        # KeyPrefix invokes this cross-source barrier before publishing the
        # rebuilt directory. Its new own-directory identity does not exist
        # yet; the ordinary reader checks that identity after publication.
        self._check_mail_dependencies(_load_events(self.root))

    def _fold(self, rows, prior):
        if not prior:
            return {"mail": self._mail_rebuilt}
        from .mail_summary import _load_events, bank_tree, event_item
        events = _load_events(self.root)
        if prior["mail"]["event_generation"] != events.payload["generation"]:
            raise send_prefix.CacheMiss()
        value = dict(prior["mail"])
        dependencies = value["dependencies"]
        if dependencies is not None and dependencies != self._mail_dependencies(events,
                dependencies["worker_session_id"], dependencies["work"] is not None):
            raise send_prefix.CacheMiss()
        if rows:
            value["dependencies"] = self._mail_dependencies(events, rows[0]["worker_session_id"],
                dependencies is not None and dependencies["work"] is not None
                or any(row["source"] == "work_complete" for row in rows))
        tree = bank_tree(self)
        for row in rows:
            value["members"] = tree.add(value["members"], event_item(events, row["message_id"]))
        return {"mail": value}

    def _advance(self, batch, encoded):
        for row in batch:
            self.set("id", row["id"], row)
            self.set("message", row["message_id"], row)
            self.set("key", row["idempotency_key"], row)
        for frame in encoded:
            self.payload["head"] = hashlib.sha256(
                bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        self.payload["count"] += len(batch)


class _Ledger:
    def __init__(self, root, relative, node, revision):
        self.root, self.relative, self.node = root, Path(relative), node
        self.path = root.resolve_relative(self.relative)
        self.revision = revision
        self.prefix, self.rows, self.identity = None, None, None
        self.refresh()

    def refresh(self):
        try:
            prefix = self.prefix or ConsumedPrefix(
                self.root, self.relative, self.node, revision=self.revision)
            try:
                prefix.load()
                from .mail_summary import _load_events
                prefix.check_mail_sources(_load_events(self.root))
            except send_prefix._CACHE_ERRORS:
                prefix.rebuild()
            self.prefix, self.rows = prefix, None
            self.identity = prefix.payload["identity"]
            return
        except send_prefix._CACHE_ERRORS:
            self.prefix = None
        self.rows, self.identity, _raw = _native(self.root, self.relative, self.node)

    def current(self):
        if self.identity != send_prefix._identity(self.path):
            return False
        if self.prefix is not None:
            try:
                self.prefix._assert_current()
            except send_prefix._CACHE_ERRORS:
                return False
        return True

    def get(self, domain, value):
        for attempt in range(2):
            if self.prefix is None:
                field = {"id": "id", "message": "message_id", "key": "idempotency_key"}[domain]
                return next((row for row in self.rows if row[field] == value), None)
            try:
                return self.prefix.get(domain, value)
            except send_prefix._CACHE_ERRORS:
                if attempt == 0:
                    try:
                        self.prefix.rebuild()
                        self.identity = self.prefix.payload["identity"]
                        continue
                    except send_prefix._CACHE_ERRORS:
                        pass
                self.prefix = None
                self.rows, self.identity, _raw = _native(self.root, self.relative, self.node)
        return self.get(domain, value)

    @property
    def count(self):
        return self.prefix.payload["count"] if self.prefix is not None else len(self.rows)


def _paths(root, node):
    from .message_consumption import MessageConsumption
    selected = [MessageConsumption(root)._relative(node)]
    directory = root.resolve_relative(Path("receipts/consumed") / node)
    if directory.is_dir():
        selected.extend(path.relative_to(root.tenant_home)
            for path in sorted(directory.glob("*.jsonl")))
    return [path for path in selected if root.resolve_relative(path).exists()]


class _History:
    def __init__(self, root, node):
        self.root, self.node = root, node
        try:
            self.revision = _revision()
        except send_prefix._CACHE_ERRORS:
            self.revision = None
        self.ledgers = {path: _Ledger(root, path, node, self.revision)
            for path in _paths(root, node)}

    def refresh(self):
        for relative in _paths(self.root, self.node):
            if relative not in self.ledgers:
                self.ledgers[relative] = _Ledger(
                    self.root, relative, self.node, self.revision)
        for ledger in self.ledgers.values():
            if not ledger.current():
                ledger.refresh()

    def ledger(self, relative):
        relative = Path(relative)
        if relative not in self.ledgers:
            self.ledgers[relative] = _Ledger(
                self.root, relative, self.node, self.revision)
        return self.ledgers[relative]

    def check_key(self, candidate):
        for ledger in self.ledgers.values():
            prior = ledger.get("key", candidate["idempotency_key"])
            if prior is not None and prior["message_id"] != candidate["message_id"]:
                raise ProtocolRefusal("consume_idempotency_conflict",
                    "consumption key already names another envelope",
                    remedy="use a fresh key for this envelope, or replay the envelope already bound to the key")

    def current(self):
        return (set(_paths(self.root, self.node)).issubset(self.ledgers)
            and all(ledger.current() for ledger in self.ledgers.values()))


def history(root, node):
    """Certify existing planes without creating an absent consumption plane."""
    return _History(root, node)


class _Selection:
    def __init__(self, history, relative, candidate):
        self.history, self.relative, self.candidate = history, Path(relative), candidate


def selection(history, relative, candidate):
    history.ledger(relative)
    return _Selection(history, relative, candidate)


def transact(root, relative, decide, selected):
    """Selected native transaction; expensive recovery stays outside its lock."""
    if (type(selected) is not _Selection or selected.history.root != root
            or selected.relative != Path(relative)):
        raise ProtocolRefusal("consumption_controller_only",
            "indexed consumption requires its exact native selection")
    path, tenant = jsonl._resolve(root, relative, write=True)
    lock, lock_relative = jsonl._lock_beside(path, relative)
    candidate, histories = selected.candidate, selected.history
    for _attempt in range(8):
        histories.refresh()
        histories.check_key(candidate)
        ledger = histories.ledger(relative)
        existing = ledger.get("message", candidate["message_id"])
        result, candidates = decide([] if existing is None else [existing])
        batch = tuple(candidates)
        if batch and (len(batch) != 1 or batch[0] is not candidate):
            raise ProtocolRefusal("consumption_controller_only",
                "indexed consumption may append only its admitted candidate")
        if ledger.count + len(batch) > jsonl.MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit",
                f"ledger maximum is {jsonl.MAX_LEDGER_RECORDS} records")
        encoded = tuple(jsonl._encode_record(row, tenant, KINDS) for row in batch)
        duplicate = bool(batch) and ledger.get("id", candidate["id"]) is not None
        with jsonl._locked_path(lock, exclusive=True, relative=lock_relative):
            if not histories.current():
                continue
            if duplicate:
                raise ProtocolRefusal("duplicate_record_id",
                    f"record id {candidate['id']} already exists")
            if batch:
                identity = jsonl._append_frame(path, b"".join(encoded))
                if ledger.prefix is not None:
                    try:
                        ledger.prefix.appended(batch, encoded, identity)
                        ledger.identity = ledger.prefix.payload["identity"]
                    except send_prefix._CACHE_ERRORS:
                        pass  # Durable source remains; its old seal forces recovery.
            return result
    raise ProtocolRefusal("consumption_snapshot_contention",
        f"{Path(relative).as_posix()} changed during eight consumption captures",
        remedy={"kind": "transient", "action": "retry_once",
                "delay_seconds": 1, "holder": "unknown"})


def warm(root, node):
    """Certify copied ledger identities without appending authoritative bytes."""
    from .bus_epoch import epoch_guard
    from .wake_hold import wake_coordination_guard
    with epoch_guard(root), wake_coordination_guard(root, node):
        histories = history(root, node)
        return {relative.as_posix(): {"identity": ledger.identity,
            "count": ledger.count, "head": (None if ledger.prefix is None
                else ledger.prefix.payload["head"])}
            for relative, ledger in histories.ledgers.items()}

"""Disposable authenticated paths for native waiter exhaustion transactions.

The complete native decoder certifies a new or changed ledger. Only an exact
identity-bound checkpoint can replace that replay with selected ID/key proofs.
Neither a missing cache nor a missing proof node establishes key absence.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import jsonl, send_prefix
from .errors import IntegrityFailure, ProtocolRefusal
from .key_prefix import KeyPrefix


KINDS = frozenset({"codex_wait_exhaustion_receipt"})


def relative(node):
    return Path("receipts/codex-wait-exhaustion") / (node + ".jsonl")


class ReceiptPrefix(KeyPrefix):
    def __init__(self, root, node, *, revision=None):
        # A separate seal/bank per ledger prevents one node's append from
        # evicting another node's certified prefix or the event prefix.
        super().__init__(root, KINDS, revision=revision, relative=relative(node),
            cache_name=".receipt-prefix-" + hashlib.sha256(node.encode("utf-8")).hexdigest(),
            format="wait-exhaustion-prefix-v3")

    def rebuild(self):
        before = send_prefix._identity(self.path)
        jsonl._read_path_records(self.path, self.root.tenant_id, self.kinds)
        data = self.path.read_bytes() if before is not None else b""
        if before != send_prefix._identity(self.path) or len(data) != (0 if before is None else before[4]):
            raise IntegrityFailure("ledger_identity_changed_during_read", "receipt ledger changed during validation")
        rows = jsonl._decode_path_records(self.path, self.root.tenant_id, self.kinds, data)
        # First key wins, exactly as record_exhaustion's native linear scan.
        keys = {}
        for row in rows:
            keys.setdefault(row["idempotency_key"], row)
        entries = {send_prefix._key("id", row["id"]): row for row in rows}
        entries.update({send_prefix._key("receipt", key): row for key, row in keys.items()})

        self.rebuild_entries(rows, entries, before, data)

    def _advance(self, batch, encoded):
        for row in batch:
            self.set("id", row["id"], row)
            self.set("receipt", row["idempotency_key"], row)
        for frame in encoded:
            self.payload["head"] = hashlib.sha256(bytes.fromhex(self.payload["head"]) + frame).hexdigest()
        self.payload["count"] += len(batch)


def _open(root, node, revision):
    try:
        prefix = ReceiptPrefix(root, node, revision=revision)
        try:
            prefix.load()
        except send_prefix._CACHE_ERRORS:
            prefix.rebuild()
        return prefix
    except send_prefix._CACHE_ERRORS:
        return None


def transact_exhaustion(root, node, key, decide):
    """Closed exhaustion-only native transaction under the original guards."""
    coordinate = relative(node)
    path, tenant = jsonl._resolve(root, coordinate, write=True)
    lock, lock_relative = jsonl._lock_beside(path, coordinate)
    # Source identity depends on shipped implementation, never ledger length;
    # collect it before the ledger lock, just as the native send transaction.
    try:
        revision = send_prefix._revision()
    except send_prefix._CACHE_ERRORS:
        revision = None
    with jsonl._epoch_writer_guard(root, coordinate, path=path), jsonl._locked_path(
            lock, exclusive=True, relative=lock_relative):
        # Invalid key shapes retain the native record validator's typed
        # refusal instead of leaking an index hashing exception.
        prefix = _open(root, node, revision) if isinstance(key, str) else None
        for attempt in range(2):
            if prefix is None:
                break
            try:
                existing = prefix.get("receipt", key)
                prior = [] if existing is None else [existing]
                result, row = decide(prior)
                # A damaged ID proof cannot authorize appending a duplicate.
                duplicate = row is not None and prefix.get("id", row["id"]) is not None
                count = prefix.payload["count"]
                prefix._assert_current()
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
            prior = jsonl._read_path_records(path, tenant, KINDS)
            result, row = decide(prior)
            duplicate = row is not None and any(item["id"] == row["id"] for item in prior)
            count = len(prior)
        if row is None:
            return result
        if count >= jsonl.MAX_LEDGER_RECORDS:
            raise ProtocolRefusal("ledger_record_limit", f"ledger maximum is {jsonl.MAX_LEDGER_RECORDS} records")
        encoded = jsonl._encode_record(row, tenant, KINDS)
        # Only one new native key may advance this index. Its first matching
        # row is immutable, including when a future caller supplies its own
        # callback or the optimization is unavailable.
        if (row["node_id"] != node or row["idempotency_key"] != key
                or any(item["idempotency_key"] == key for item in prior)):
            raise ProtocolRefusal("codex_wait_exhaustion_idempotency_conflict",
                "indexed exhaustion append must name its selected node and a fresh selected key")
        if duplicate:
            raise ProtocolRefusal("duplicate_record_id", f"record id {row['id']} already exists")
        if prefix is not None:
            try:
                prefix._assert_current()
            except send_prefix._CACHE_ERRORS as exc:
                raise IntegrityFailure("ledger_identity_changed_during_read", "receipt ledger or key bank changed before append") from exc
        identity = jsonl._append_frame(path, encoded)
        if prefix is not None:
            try:
                prefix.appended([row], [encoded], identity)
            except send_prefix._CACHE_ERRORS:
                pass  # Native fsync already succeeded; the stale seal forces replay.
        return result


def warm(root, node):
    """Certify a copied native ledger outside measurement, without appending."""
    coordinate = relative(node)
    path, _ = jsonl._resolve(root, coordinate, write=True)
    lock, lock_relative = jsonl._lock_beside(path, coordinate)
    revision = send_prefix._revision()
    with jsonl._epoch_writer_guard(root, coordinate, path=path), jsonl._locked_path(
            lock, exclusive=True, relative=lock_relative):
        prefix = _open(root, node, revision)
        return None if prefix is None else {"identity": prefix.payload["identity"],
            "count": prefix.payload["count"], "head": prefix.payload["head"]}

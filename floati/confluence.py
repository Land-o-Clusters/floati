"""Confluence phases 2-3 — the consent surface and the export verb.

Charter: `docs/design/confluence-phase2plus-charter-2026-08-29.md`
(floati half only). The seam between a fleet root and a consuming
observer app: explicit per-root, per-consumer read grants, and one
export verb that materializes the v0 receipts-read bundle under the
grant it was produced under.

Fences, restated first (charter + v0): no discovery, no resident
watcher, no network, no mutation API. The grant names the exact root —
never a home scan, never a glob. Reads by an ungranted consumer refuse
typed, naming the grant act. Bundle reads use the existing physically
read-only semantics; missing allowlisted files are empty evidence, not
errors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

from .errors import ProtocolRefusal
from .ids import uuid7_hex
from .jsonl import read_records, transact
from .records import validate_record
from .registry import utc_now
from .root import FloatiRoot

GRANT_KINDS = frozenset({"confluence_grant"})
GRANT_LEDGER = Path("confluence/grants.jsonl")

_GRANT_SOURCES: tuple[tuple[str, frozenset[str]], ...] = (
    ("work/items.jsonl", frozenset({"work_item", "work_transition"})),
    ("receipts/workers.jsonl", frozenset({"worker_receipt"})),
    ("receipts/worker-refusals.jsonl", frozenset({"worker_refusal"})),
    ("receipts/denials.jsonl", frozenset({"denial_receipt"})),
)
_DELIVERY_SOURCES = ("receipts/deliveries", "receipts/acks")
_BUNDLE_ENTRY_CEILING = 100000

_GRANT_ACT = (
    "run: floati confluence grant --root ROOT --consumer CONSUMER "
    "(one grant = one root = one consumer identity)"
)


def _consumer(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or any(not (c.isalnum() or c in "-_.") for c in value)
    ):
        raise ProtocolRefusal(
            "confluence_consumer_invalid",
            "consumer identity must be bounded, safe text",
        )
    return value


class ConfluenceGrantLedger:
    """Receipted, per-root, per-consumer read grants in one ledger."""

    def __init__(self, root: FloatiRoot) -> None:
        if not isinstance(root, FloatiRoot):
            raise ProtocolRefusal(
                "confluence_root_invalid",
                "the confluence seam requires one validated direct-home root",
            )
        self.root = root

    def _records(self) -> List[Dict[str, object]]:
        return read_records(
            self.root, GRANT_LEDGER, allowed_kinds=set(GRANT_KINDS))

    def _append(
        self,
        *,
        consumer: str,
        state: str,
        predecessor_receipt_id: Optional[str],
        idempotency_key: str,
    ) -> Dict[str, object]:
        row = {
            "schema_version": 1,
            "id": "confluence-grant-" + uuid7_hex(),
            "tenant_id": self.root.tenant_id,
            "timestamp": utc_now(),
            "kind": "confluence_grant",
            "consumer": consumer,
            "state": state,
            "predecessor_receipt_id": predecessor_receipt_id,
            "idempotency_key": idempotency_key,
        }
        validate_record(row, self.root.tenant_id, GRANT_KINDS, integrity=False)

        def choose(existing: List[Dict[str, object]]):
            for prior in existing:
                if prior.get("idempotency_key") == idempotency_key:
                    return prior, None  # idempotent replay
            return row, row

        # transact returns the decide() result: the stored record on a
        # fresh append, the matching prior record on an idempotent replay.
        return transact(
            self.root, GRANT_LEDGER, choose,
            allowed_kinds=set(GRANT_KINDS))

    def _active(self, consumer: str) -> Optional[Dict[str, object]]:
        active: Optional[Dict[str, object]] = None
        for record in self._records():
            if record.get("consumer") != consumer:
                continue
            if record.get("state") == "granted":
                active = record
            elif record.get("state") == "revoked":
                active = None
        return active

    def grant(self, consumer: object, idempotency_key: object) -> Dict[str, object]:
        consumer = _consumer(consumer)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ProtocolRefusal(
                "confluence_idempotency_invalid",
                "grant requires a non-empty idempotency key")
        if self._active(consumer) is not None:
            raise ProtocolRefusal(
                "confluence_grant_active",
                "an active grant already covers this consumer on this root")
        return self._append(
            consumer=consumer, state="granted",
            predecessor_receipt_id=None, idempotency_key=idempotency_key)

    def revoke(self, consumer: object, idempotency_key: object) -> Dict[str, object]:
        consumer = _consumer(consumer)
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ProtocolRefusal(
                "confluence_idempotency_invalid",
                "revoke requires a non-empty idempotency key")
        active = self._active(consumer)
        if active is None:
            raise ProtocolRefusal(
                "confluence_grant_absent",
                "no active grant covers this consumer on this root")
        return self._append(
            consumer=consumer, state="revoked",
            predecessor_receipt_id=str(active["id"]),
            idempotency_key=idempotency_key)

    def require_active(self, consumer: object) -> Dict[str, object]:
        consumer = _consumer(consumer)
        active = self._active(consumer)
        if active is None:
            raise ProtocolRefusal("confluence_grant_required", _GRANT_ACT)
        return active

    def grants(self) -> List[Dict[str, object]]:
        return list(self._records())


def _bundle_sources(root: FloatiRoot) -> List[tuple[str, frozenset[str]]]:
    """The v0 allowlist, in fixed order, with per-node ledgers enumerated
    from the root's own declared receipts directories (own-root structure,
    never discovery beyond it)."""
    sources: List[tuple[str, frozenset[str]]] = []
    for relative, kinds in _GRANT_SOURCES:
        sources.append((relative, kinds))
    for directory in _DELIVERY_SOURCES:
        absolute = root.resolve_relative(Path(directory))
        if not absolute.is_dir():
            continue
        for path in sorted(absolute.glob("*.jsonl")):
            relative = path.relative_to(root.path).as_posix()
            kind = "delivery_receipt" if "deliveries" in directory\
                else "ack_receipt"
            sources.append((relative, frozenset({kind})))
    return sources


def confluence_adopt(
    root: FloatiRoot,
    *,
    consumer: object,
    session: object,
    manager: object,
    authority_subject: object,
    authority_epoch: int,
    authority_expires_at: str,
) -> Dict[str, object]:
    """Phase 4, dispatch-authorized: record one MANAGED-mode session
    adoption through the seam. The trust class is TWO-gated — the
    consumer's active confluence grant (this module's read-seam consent)
    AND the manager's exact active authority lease (the L1 operator
    class, enforced by the dark ManagedSessions machinery). Neither gate
    substitutes for the other."""
    consumer = _consumer(consumer)
    ledger = ConfluenceGrantLedger(root)
    ledger.require_active(consumer)
    from .managed import ManagedSessions

    return ManagedSessions(root).adopt(
        str(session), str(manager), str(authority_subject),
        authority_epoch, str(authority_expires_at))


def confluence_release(
    root: FloatiRoot,
    *,
    consumer: object,
    session: object,
    manager: object,
    authority_epoch: int,
) -> Dict[str, object]:
    """Release one adopted session through the seam; both trust gates
    apply here exactly as on adopt."""
    consumer = _consumer(consumer)
    ledger = ConfluenceGrantLedger(root)
    ledger.require_active(consumer)
    from .managed import ManagedSessions

    return ManagedSessions(root).release(
        str(session), str(manager), authority_epoch)


def materialize_bundle(
    root: FloatiRoot, *, consumer: object, out: object
) -> Path:
    """Materialize the receipts-read bundle under the consumer's active
    grant. Physically read-only against the root: every ledger read uses
    the existing read-only semantics, and the only write is the
    operator-specified output path — never a file inside the fleet root."""
    if not isinstance(root, FloatiRoot):
        raise ProtocolRefusal(
            "confluence_root_invalid",
            "the confluence seam requires one validated direct-home root",
        )
    consumer = _consumer(consumer)
    ledger = ConfluenceGrantLedger(root)
    grant = ledger.require_active(consumer)

    staged: List[Dict[str, object]] = []
    for relative, kinds in _bundle_sources(root):
        records = read_records(root, Path(relative), allowed_kinds=set(kinds))
        for ordinal, record in enumerate(records, start=1):
            staged.append({
                "source": relative,
                "source_ordinal": ordinal,
                "record": record,
            })
    if len(staged) > _BUNDLE_ENTRY_CEILING:
        raise ProtocolRefusal(
            "confluence_bundle_oversized",
            "the root's evidence exceeds the bundle entry ceiling")
    staged.sort(key=lambda entry: (
        str(entry["record"]["timestamp"]),
        str(entry["record"]["id"]),
        str(entry["source"]),
        int(entry["source_ordinal"]),
    ))
    entries = [
        {
            "sequence": position,
            "source": entry["source"],
            "source_ordinal": entry["source_ordinal"],
            "record": entry["record"],
        }
        for position, entry in enumerate(staged, start=1)
    ]
    snapshot_at = max(
        (str(entry["record"]["timestamp"]) for entry in entries),
        default=str(grant["timestamp"]),
    )
    document = {
        "schema_version": 1,
        "kind": "receipts_read_bundle",
        "tenant_id": root.tenant_id,
        "snapshot_at": snapshot_at,
        "grant_id": str(grant["id"]),
        "entries": entries,
    }
    if not isinstance(out, Path) or not out.is_absolute():
        raise ProtocolRefusal(
            "confluence_out_invalid",
            "the bundle output must be one explicit absolute path")
    out = Path(out.resolve())
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return out

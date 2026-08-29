# Confluence read contract v0

Status date: 2026-08-01. This is the read-only Floati surface prepared for a
consuming observer app integration. It defines data contracts only. It does
not authorize or implement code for a consuming observer app, discovery, a
watcher, networking, installation, process inspection, credentials, or writes
into a fleet root.

## Consent and root selection

The consumer receives one explicit absolute direct-home path through a future
consent surface. It never scans a home directory, project collection, process
table, or network. `FloatiRoot.open_direct_home(path)` remains the path and
tenancy validator. Downstream consumers render evidence; Floati owns protocol
truth.

## Fleet status

The stable command is:

```text
floati status --root /explicit/fleet --json
```

It emits the compact artifact described by
`schemas/v0/fleet-status-artifact.schema.json`. The outer
`artifact_version` is the CLI envelope version. The inner
`status_schema_version` is the Confluence payload version. Both are integer
zero. `kind` is `fleet_status`; `mode` is `report_only`.

`observed_at` is the only observation-clock field. Nodes keep `liveness`,
`authority`, and `mutex` separate. Work and worker state is projected from
validated records, never processes. `stale_lease_count` must equal the length
of `stale_leases`; consumers should reject disagreement rather than repair it.

## Receipt snapshot

The materialized interchange shape is
`schemas/v0/receipts-read-bundle.schema.json`. A reader may include only:

- `work/items.jsonl` — `work_item`, `work_transition`;
- `receipts/workers.jsonl` — `worker_receipt`;
- `receipts/worker-refusals.jsonl` — `worker_refusal`;
- `receipts/denials.jsonl` — `denial_receipt`;
- `receipts/deliveries/<node>.jsonl` — `delivery_receipt`;
- `receipts/acks/<node>.jsonl` — `ack_receipt`.

Each entry carries the contained source path and one-based source ordinal.
Bundle sequence is deterministic by `(record.timestamp, record.id, source,
source_ordinal)`. Existing durable record schemas remain authoritative; the
bundle does not copy or weaken their definitions.

Snapshot reads use Floati's existing physically read-only semantics: read the
current bytes without creating a lock file, validate complete framed JSONL,
enforce 64 MiB and 100,000-record per-ledger ceilings, and fail the whole read
on a partial frame, malformed record, wrong tenant, duplicate record ID, or
unruled kind. Missing allowlisted files are empty evidence, not errors.

## Fixtures and compatibility

`tests/fixtures/confluence/v0/fleet-status.json` and
`tests/fixtures/confluence/v0/receipts-read.json` are fixed consumer fixtures.
Additive or breaking changes require a new schema version and new fixtures;
version zero does not acquire undocumented fields. `floati status` without
`--json` remains compatible, while `--json` is the explicit declaration that
the caller relies on this contract.

## Ownership boundary

This seam grants no mutation API. A future integration with a consuming
observer app may request an action through a separately ruled surface, but it
cannot append, acknowledge, claim, complete, install, discover, or manufacture
Floati state through v0.

## Managed-session adoption seam

`schemas/v0/session-adoption-record.schema.json` and
`schemas/v0/session-release-record.schema.json` define the opt-in seam for a
future managed mode for a consuming observer app. An adoption is explicitly
`MANAGED`, names the manager node, and binds the exact active authority
subject, epoch, and expiry.
A release binds the adoption receipt, manager, subject, and epoch it closes.
`ManagedSessions` is a dark Floati implementation over
`managed/sessions.jsonl`; no code for a consuming observer app or implicit
session discovery exists.

## Harbor Chart topology

```text
floati graph --root /explicit/fleet --json
```

The version-zero topology contains sorted typed fleet nodes, projected worker
sessions, work-DAG dependency edges, and local bridge stubs. It has no
observation clock, filesystem mtime, process ID, or inferred directory. The
graph is a data feed only; the drag-and-drop GUI remains with downstream
consumers.

## Doctor artifact

`floati doctor --root ROOT --source SOURCE [--ref REF]` performs physically
read-only checks for direct-home validity, registry/liveness-directory
agreement, exact manifest set and digests, named-ref deployment currency,
lexical symlink identity, and the sole `work/items.jsonl` consumption
coordinate. Return codes distinguish healthy `0`, configuration refusal `20`,
malformed evidence `33`, and diagnosed degradation `35`. Remediation text is
omitted when the relevant source currency is not established.

## Local bridge v0

Exactly two direct-home roots record consent independently, then mirror an
active bridge record naming both consent receipt IDs. Every successful
direction writes paired `bridge_forward` receipts stamped
`advisory_not_consumption`; it never appends mail or work. Missing, revoked,
mismatched, same-root, inactive-actor, and non-local transport paths fail
closed with denial receipts on both roots. Bridge v0 has no socket, URL,
remote transport, discovery, or mutation surface for a consuming observer app.

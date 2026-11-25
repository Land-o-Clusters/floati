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

The operator's `floati confluence bundle --root ROOT --consumer CONSUMER
--out /absolute/output.json` retains the grant-stamped v1 bundle by default.
For edge receipts on a live bus, run the same verb with `--schema-version 2`.
That writes `schemas/v0/receipts-read-bundle-v2.schema.json` to the operator's
absolute output path outside the fleet root. An active per-root, per-consumer
grant is required before either version writes anything. The v2 bundle adds
`events.jsonl` message envelopes, carrying message id, sender, recipient, and
appended timestamp. It also carries `message_retracted` records with their
`retracted_message_id`, so a consumer can close a withdrawn envelope rather
than display it as waiting forever. Delivery and acknowledgment records retain
their recipient
and `item_ids`, so consumers can join them to envelopes by id. Actor-bound v1
acknowledgments validate in v2. A `wake_hold_receipt` sharing a delivery ledger
is validated, excluded from entries by kind, and counted in
`excluded_wake_hold_receipt_count`; zero means none were excluded. Other event
kinds are validated but not exported. The v0 and v1 schemas remain frozen.

For a directory-based launch export, use `--schema-version 2 --out
/absolute/bundle-directory/receipts-read.json`. This also writes
`codex-launches.json` in the selected directory under the same grant.
Select that directory in the consumer to read the launch projection.
The projection carries native session IDs, dispatch and testimony IDs, the
launch directory, and the request and observation times. It omits launches
without authenticated sender testimony and sets `complete: false` when any
launch in the admitted source set cannot be joined. A missing sidecar or one
with `complete: false` leaves launcher attribution unavailable.
Exported files have owner-only permissions. During replacement or after an
interrupted export, the sidecar can be absent. Rerun the export to restore it.

The v2 bundle also exports an optional `roster` array, projected by
`floati/roster.py` for active nodes. Each row names `node_id` and carries
`owner`, `harness`, `repository_branch`, `activity`, `current_action`, and
`liveness` cells. Every cell has a `stamp` (`MEASURED`, `DERIVED`, `ESTIMATE`,
or `UNKNOWN`), `value`, `observed_at`, and `age_seconds`. Missing evidence
remains UNKNOWN with null value, observation time, and age; owner currently
has no measured source. Ages use the roster's own observation clock. Expired presence carries
`value: no_report_since` and its expiry as `named_time`; it never implies
idle activity. Consumers must retain the stamps and ages when displaying cells.

This optional v2 extension follows the governed loosening rule: bundles
without `roster` still validate, and consumers may ignore the block without
changing their interpretation of existing fields. Existing required fields
and entry constraints are unchanged.

An optional v2 `fleet_state` object adds `observed_at`, `archive_retention`,
and `waiters` for this granted root only. Archive retention lists epochs by
id, physical retained `size_bytes`, roll time, 30-day `expires_at`, `expired`
eligibility, and `removed` testimony. Keep-forever suppresses expiry and uses
null `expires_at`. A missing unreceipted payload has null size and
`size_reason: archive_payload_absent`; a receipted removal with no payload
has zero size. `last_removal_receipt` is the latest validated native removal
record, or null. A removal can outlive its roll receipt; then `rolled_at`
and `expires_at` remain null. A removal receipt proves the removal intent
was recorded, not that no payload currently exists; size is observed separately.

Each active node has a waiter row. `mode` is a stamped `blocking` or `polling`
observation of the backend actually selected by the shared Codex/Cursor wait
path, with observation time, age, and `valid_until` (at most 120 seconds).
It is backend testimony, not process-liveness or session-authority proof.
Missing, malformed, future, or expired observations are UNKNOWN with a reason.
Other waiter implementations have no backend observation yet and remain
UNKNOWN. `last_wake` uses only a `woke` native wake-attempt receipt, retaining
its timestamp and receipt id; queued/refused attempts do not prove a wake.
`idle_cpu` is UNKNOWN with `reason: idle_cpu_not_measured`; cycle counters and
backend choice do not measure idle CPU.

The optional `fleet_state.load_hold` reports whether Floati can confirm an
effective build or test hold in the granted fleet. It contains
`schema_version: 1`, `state` (`active` or `none`), and the RFC 3339 times
`observed_at` and `valid_until`. Floati checks the live quiet-window driver
and the recorded identity and stopped state of a paused process before
reporting `active`. It reports `none` after a complete quiet-state read
confirms the absence of a hold. Partial history and indeterminate recovery
leave the field absent.

Validity ends at the quiet-window cap or 120 seconds after observation,
whichever comes first. When the export includes hold testimony, v2
`snapshot_at` also covers the quiet-state observation time. Rerun
`confluence bundle` against the same output path to publish a fresh
observation, including after release. Consumers can read earlier v2
exports without the field.

`fleet_state` is optional: every previous v2 schema constraint remains intact,
and old bundles without the block still validate. The branch supplies the
pre-change schema and an old-valid compatibility fixture; publication into
the cut contract remains the architect's ratify-versus-recut decision.

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
Version zero does not acquire undocumented fields; its additive or breaking
changes require a new schema version and new fixtures. The optional v2
roster extension above is a governed loosening under the 2026-09-01
ratify-versus-recut ruling.
`floati status` without
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

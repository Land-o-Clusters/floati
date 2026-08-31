# DRAFT — Floati serial-bus layer 1 specification

Status: **DRAFT — NOT A FINAL STANDARD OR ACTIVATION CONTRACT**

This document describes the implemented version-zero local serial-bus layer.
The JSON Schemas in `schemas/v0/` are normative for machine shape; this prose
defines ordering and safety relationships. A passing implementation is not a
publication, interoperability, network, or HM-4 claim.

## HM-3I charter mirror — bounded local run graph

The binding authority for this scope is `HARBOR_MASTER.md` from the upstream
product this contract was designed against at
`a111202b228d34c2b371bcc5e2c4798206474439`. The lawful HM-3I scope is a
**bounded local run graph** with durable run-truth records: it is finite and
acyclic, local, and data-only. It permits no arbitrary embedded code, no
general condition-expression language, no distributed scheduling, and no
claim to replace Temporal, LangGraph, or similar workflow engines; no model-authored graph mutation without a durable plan_amendment record is permitted. The general workflow engine remains fenced.

**Product boundary (verbatim, binding):** Floati is the deterministic local
operating kernel for heterogeneous coding-agent fleets: it may admit, schedule,
fence, suspend, cancel, reconcile, verify, and prove. It is never the reasoning framework, a hosted control plane, a general workflow engine, a secret vault,
an account-rotation system, a remote multi-tenant scheduler, or an authority
that converts model confidence into truth.

## 1. Scope and transport boundary

Layer 1 is an append-only local filesystem bus. One prepared direct-home tenant
root contains JSON Lines ledgers and their receipts. Each frame is one UTF-8
JSON object followed by LF, contains at most 65,536 bytes, and validates before
append. A ledger declares its allowed record kinds; a reader rejects malformed,
oversized, duplicate-id, cross-tenant, and unexpected-kind evidence.

Layer 1 defines no HTTP endpoint, socket, listener, daemon, discovery system,
credential broker, remote transport, or hosted service. Cross-root bridge v0 is
local-filesystem-only and stamps forwarded material
`advisory_not_consumption`. HM-4 remains fenced.

## 2. Identity and message ingress

A sender and recipient exist only through active
`registry-entry.schema.json` records. A message append uses
`message-envelope.schema.json` and binds sender, recipient, repository, exact
40- or 64-character Git object id, repository-relative document path, bounded
note, and idempotency key. Message bodies and provider transcripts do not enter
the envelope.

`unknown_sender` and `unknown_recipient` are pre-write typed refusals. They
expose the complete lexically sorted active roster, or `(none)` when no node is
active, and create or change no root entry, lock, or denial receipt. By
contrast, the registered-party refusal reasons `idempotency_conflict`,
`reply_to_unknown`, and `reply_to_parties_mismatch` produce
`denial-receipt.schema.json` records. A delivery receipt states only that an
item was presented; `ack-receipt.schema.json` is a later explicit
acknowledgment. Delivery is not acknowledgment, consumption, completion,
publication, or release.

## 3. Ordering, atomicity, and planes

Writers acquire the ledger lock, validate the existing bounded snapshot,
validate the candidate, append one complete LF frame, and `fsync` the file.
New ledgers also `fsync` their parent directory. Record ids are UUIDv7-prefixed
and unique within a ledger. Readers fail on incomplete final frames instead of
silently ignoring them. Lock acquisition is bounded to one second. A contended
ledger lock refuses as `ledger_lock_timeout`; a contended outer authority or
mutual-exclusion compare-and-swap lock refuses as `cas_lock_timeout`. Neither
timeout is evidence that the holder completed, failed, or released its work.

Physical LF-frame ordinal is the ordering authority within a ledger. Wall-clock
timestamps, including future or backward-skewed values, never reorder frames.
Replay combines ledgers by fixed source precedence (work, worker receipt,
worker refusal, denial) and then source ordinal. It makes no cross-ledger
chronology claim. Its displayed elapsed time is monotonic and nonnegative even
when recorded clocks move backward. A reply must follow its referenced original
in physical message-ledger order and reverse the original parties.

Liveness, authority, and mutual exclusion remain distinct record planes:

- `liveness-presence-record.schema.json` describes observed presence.
- `authority-grant-record.schema.json` names the exact holder, epoch, TTL, and
  deadline.
- `mutual-exclusion-hold-record.schema.json` protects one resource coordinate.

No plane implies either of the other two. Work is consumed only from the exact
`work/items.jsonl` coordinate using `work-item-record.schema.json` and
`work-transition-record.schema.json`.

## 4. Worker boundary

A worker may launch only after an active exact authority check and atomic work
claim. `worker-receipt-record.schema.json` records the finite sequence:

```text
claim -> spawn -> drive -> bind_artifact -> complete
```

Any terminal fault produces `degrade` with a finite outcome code. A refusal
before eligible launch uses `worker-refusal-record.schema.json`. Headless child
permission requests fail closed; unattended adapters never grant or select an
automatic approval. Effective execution deadlines are clipped to authority TTL
minus the standing margin. Artifact bindings contain only repository, exact
Git SHA, and repository-relative document path.

## 5. Local gateway v0

The local gateway surface is dark and explicit. It consumes
`local-gateway-config.schema.json`, which permits only:

```json
{
  "schema_version": 0,
  "kind": "local_gateway_config",
  "transport": "stdio",
  "network": "disabled",
  "workspace_root": "/absolute/path",
  "approval_mode": "forward_fail_closed"
}
```

No config is discovered from cwd, home, or environment. A supplied path must be
an absolute non-symlink file.

Gateway records append to `gateway/events.jsonl` in this order:

1. `gateway-session-ingress-record.schema.json` binds one UUIDv7 session,
   actor, lexically confined absolute workspace, and `stdio` transport.
2. `gateway-capability-declaration-record.schema.json` binds that session to a
   sorted, unique, non-empty capability set.
3. `gateway-approval-forward-record.schema.json` binds an existing approval
   request id, a declared capability, a bounded sorted scope, and the only v0
   state `forwarded_unresolved`.

Approval forwarding is not an approval decision. It creates no authority
grant, work transition, delivery acknowledgment, or child-process response.
Resolution requires a separately governed approval contract; absent resolution
remains fail-closed.

## 6. Capability and approval relationship

The bus-wide capability/approval contracts remain
`capability-record.schema.json`, `approval-request-record.schema.json`, and
`approval-decision-record.schema.json`. They are TTL- and authority-bound.
Gateway capability declarations describe only what a local session says it can
surface. They do not replace bus capability observation or confer permission.

## 7. Conformance and refusal

A conforming layer-1 implementation must:

- reject unknown fields and unsupported schema versions;
- preserve tenant and lexical-path boundaries;
- append no partial or unvalidated frame;
- distinguish delivery, acknowledgment, work consumption, authority, and
  completion;
- retain typed degradations and denials instead of silent fallback;
- make gateway configuration explicit and physically read-only in doctor;
- keep approval forwarding unresolved until a governed decision exists; and
- make zero network calls in bus, worker-adapter orchestration, or gateway code.

Filesystem failures use a separate durability family. `disk_full` means an
append or flush encountered ENOSPC/EDQUOT; `root_read_only` means the selected
root refused mutation; `root_deleted` means the validated root disappeared;
`short_write` means one append syscall reported fewer bytes than its frame; and
`storage_unavailable` retains other filesystem failures without guessing.
Write and flush failures attempt rollback to the prior complete length. These
modes are CLI `degraded` exit 35, distinct from configuration refusal exit 20
and malformed durable evidence exit 33.

The standalone conformance harness in `docs/CONFORMANCE.md` tests adapter
behavior. `python3 -m floati.selftest` tests the current implementation. Neither
command finalizes this draft or authorizes publication.

## 8. HM-3I bounded orchestration truth — local draft disposition

The HM-3I brief's Item 10 disposition in this draft is an exact-candidate,
bounded-local verification contract. The canonical durable run coordinate is
`runs/events.jsonl`; physical frame order is its only state-order authority.
Its literal run families are `run_created`, `task_contract`,
`plan_amendment`, `run_policy_bound`, `worker_pool_bound`,
`dispatch_decision`, `result_produced`, `result_verified`,
`acceptance_receipt`, `result_accepted`, `run_terminal`, `attempt_opened`,
`attempt_started`, `attempt_terminal`, `retry_scheduled`, `retry_exhausted`,
`cancel_requested`, `cancel_scope_resolved`, `cancel_observed`,
`cancel_signal_sent`, `cancel_terminal`, `cancel_unconfirmed`,
`stale_attempt_evidence`, `stale_evidence_adopted`,
`attempt_harness_session_bound`, and `supervisor_orphaned`.

`FLOATI.toml` is an explicit repository policy input rather than a run record.
An explicit admission plan evaluates to the immutable, read-only
`plan_admission` artifact; it is not an append, worker launch, cache, or
authority grant. Repository decision records use their explicit
repository-scoped coordinate
`repositories/<repository-coordinate>/decisions.jsonl` below the supplied
tenant root. Their source evidence is a closed durable taxonomy; `doc:` proof
requires an injected read-only repository/SHA/path resolver and never cwd,
remote, or checkout discovery. A terminal accepted or rejected decision
requires `operator` or `architect` authority. A non-null task-contract join
requires the optional immutable same-repository binding on that contract;
legacy or unavailable binding proof fails closed. `decision_digest` covers the
entire validated record except that digest field itself. A handoff capsule is
a deterministic read artifact containing only current accepted frames in their
accepted-frame physical order, rather than an authoritative append. Raw worker
receipts remain evidence; only their valid durable references participate in
run truth.

Item 10 verification exercises these coordinates through crash, hostile-input,
timestamp, recovery, and twelve-process contention cases at one exact candidate
tip. It preserves the frozen retry, cancellation, and logical-outcome
vocabularies; it also preserves stable attempt, claim, lease, and
worker-session joins. Passing those local tests does not publish, install,
deploy, or activate Floati, does not establish an architect gate or human review,
and does not make this draft final.

## 9. C7.1 candidate read bundle — local draft disposition

`bundle/c7.1/` is a new, read-only `c7.1-candidate` package beside the frozen
version-zero Confluence contract. Its own `bundle-index.json` requires a
reader to select only its highest understood version and fail closed at an
unknown index. C7.1 explicitly records `approvals: "excluded-c7.1"`; it does
not infer a run/item/attempt/claim/lease approval join. The exact schema IDs,
versions, files, and RFC 6901 pointers for run, work item, attempt, claim,
lease, retry, cancellation, result, logical outcome, run outcome, task
contract, and decision evidence are in `bundle/c7.1/schema-catalog.json`.
Claim and lease values remain opaque stable identifiers, not an invented
lifecycle.

An explicit caller may materialize a C7.1 snapshot only to an explicit
fresh destination outside the selected tenant root and the checked-in package.
It copies the exact run, worker-receipt, work-item, registry, and repository
decision raw bytes, plus every catalog-named schema, so the read bundle is
self-contained. The source tenant is never written. The projection names its
explicit `tenant_id` and repository coordinate; a reader uses those identities
only to validate the copied records and decision path, never to discover an
ambient source root.

The projection is normative when its C7.1 schema, captured bytes, and digests
validate. Its `raw_source_digest` hashes the exact raw run-ledger bytes;
per-family current-state maps use physical frame order only. Its
`auxiliary_sources` records the separate worker, registry, decision, and work
item raw digests and physical frames without merging them into run causality.
Its `semantic_digest` covers a timestamp-free canonical projection domain,
excluding both output digest fields and every raw-byte digest. Its
`self_digest` covers the emitted artifact with only its own field excluded. A
reader verifies contained regular snapshot paths and deterministically
reprojects those exact captured bytes before returning a projection. Missing
facts are typed `absent` values; malformed or unrepresentable facts are typed
errors with raw frame-range fallback pointers. No null value means unknown.

There is no cross-ledger timestamp merge. Frames in every ledger retain their
own physical order. Worker receipts may corroborate an explicit reference but
cannot reorder run state. The decision register is exposed only as raw,
read-only physical frames at
`repositories/<repository-coordinate>/decisions.jsonl`; C7.1 creates neither
a decision capsule nor a consolidation.

The existing `attempt_harness_session_bound` receipt is the only worker and
harness-session binding. It exposes exact attempt, claim, lease,
worker-session, and harness-session fields. C7.1 names the closed future
relation vocabulary `resume`, `fork`, and `handoff`, but the frozen source
record carries neither a relation nor a predecessor segment ID. Accordingly,
each C7.1 segment has an artifact-local `(binding_record_id, ordinal)` source
reference and typed absent relation/predecessor fields. It never infers either
from time or order. Compatible later bindings supersede earlier ones only by
physical binding-frame order; overlapping harness sessions or incompatible
binding keys become `conflicting_binding` with no selected winner.

`supervisor_orphaned` is read as evidence only. Its projection retains its run
frame position and independent `registry/entries.jsonl` physical registration
lineage for `floati-supervisor`; it creates no capability or authority token.
The local C7.1 reader bank contains thirty-nine executable vectors covering
this package. That local result remains separate from publication, external
consumer activation, and every architect or human gate.

## 10. C7.2 candidate segment amendment — local draft disposition

`bundle/c7.2/` is an additive sibling read contract. It does not alter any
byte in `bundle/c7.1/` or `schemas/v0/`. Its index is
`c7.2-candidate`, records `approvals: "excluded-c7.2"`, uses the same exact
`{"highest_understood":true,"unknown":"fail_closed"}` reader-upgrade rule,
and names `bundle/c7.1/bundle-index.json` as its immutable predecessor.

The existing `attempt_harness_session_bound` family is the only source family
amended. Schema version one adds a durable `segment_id` using `seg-` plus the
governed UUIDv7 grammar, and a closed `segment_kind` vocabulary of `initial`,
`resume`, `fork`, and `handoff`. An `initial` segment must omit
`predecessor_segment_id`; every transition must name one. Segment identifiers
are unique within one attempt lineage. A predecessor must belong to that same
attempt and occupy an earlier physical binding position. A same-record
predecessor is legal only at a lower ordinal. The reader never derives any of
these fields from timestamps, identifiers, or ambient state.

C7.2 continues to read version-zero bindings and projects typed absence for
`segment_id`, `segment_kind`, and `predecessor_segment_id`. C7.1 remains a
version-zero source reader and fails closed on the newer binding version.
Schema version one remains invalid for every other record kind. Local schema,
projection, snapshot, and regression evidence does not publish, activate,
deploy externally, or establish an architect or human gate.

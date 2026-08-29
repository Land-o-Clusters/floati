# Floati C7.1 Read Bundle

`c7.1-candidate` is a versioned, read-only snapshot contract. Its index is
`bundle-index.json`; its complete source and pointer catalog is
`schema-catalog.json`. A reader understands only versions it names. An unknown
index version fails closed before any family is selected. C7 v0 is separate and
unchanged.

## Ordering

`raw/runs/events.jsonl` is the sole causal stream for run state. Decode its
complete frames in physical file order and number them from one. Do not sort or
merge on timestamp, identifier, session name, or filesystem metadata.

Each other ledger also retains its own physical order. There is no
cross-ledger causal merge in C7.1. Worker receipts, the decision register, and
registry lineage may corroborate an explicit identifier reference but cannot
reorder run state. Timestamps remain testimony only.

## Normative canonical projection

`families/run-projection.json` is normative when it validates. It records the
exact SHA-256 of the raw run bytes in `raw_source_digest`, then exposes
per-family current-state maps derived solely from the physical run frames.
The projection also names its `tenant_id` and repository coordinate explicitly.
Those identities bind every captured raw record and the decision-ledger path;
they are never discovered from the reader's environment. Raw frames remain the
fallback evidence.

`semantic_digest` is SHA-256 over canonical compact I-JSON of the
timestamp-free projection domain. Its domain excludes every raw-byte digest
(`raw_source_digest`, including each non-causal auxiliary source),
`semantic_digest`, and `self_digest`; changing only testimony timestamps must
not change semantic state or this digest. `self_digest` is SHA-256 over the
emitted projection with only `self_digest` excluded. It therefore covers every
raw digest and the semantic digest without a self-reference.

An unavailable fact is a typed `{"state":"absent",...}` value. A family
failure is a typed `{"state":{"kind":"error","code":...,"offending_frame_range":...},...}`
value. Both carry a raw frame-range pointer; neither uses null to mean
unknown.

Each snapshot is self-contained: it copies the exact catalog-named schemas and
the advertised run, worker, work-item, registry, and decision raw ledgers.
`auxiliary_sources` preserves each non-causal source's physical frames and raw
digest without merging it into run causality. A reader verifies those copied
bytes, refuses symlink or escaping paths, and deterministically reprojects the
captured sources before returning the projection.

## Stable joins and deliberate limits

The catalog names each schema identifier, version, file, and RFC 6901 pointer
used for run, work item, attempt, retry, cancellation, result, logical
outcome, run outcome, task contract, decision, and supervisor evidence.
`claim_id` and `lease_id` are exposed exactly as opaque stable identifiers;
the bundle does not create a claim or lease lifecycle.

Approvals are `excluded-c7.1`. The index records that exclusion explicitly;
readers do not infer an approval join.

An `attempt_harness_session_bound` frame is the only worker/harness binding.
Every projected segment carries an artifact-local source reference
`{binding_record_id, ordinal}`. The frozen source frame does not carry a
durable segment relation or predecessor, so C7.1 exposes typed absent values
for `segment_kind` and `predecessor_segment_id`. It never guesses `resume`,
`fork`, or `handoff` from order or time. Multiple binding frames with
overlapping harness sessions or incompatible claim/lease/worker keys project
as `conflicting_binding`, preserve candidate frames, and select no winner.
Compatible later binding frames supersede earlier frames only by physical
frame order.

`supervisor_orphaned` is evidence, not a capability or authority token. Its
projection retains its run-frame position and an independently ordered
`registry/entries.jsonl` lineage for `floati-supervisor`. It constructs no
capability or authority chain.

The decision register is exposed as raw, read-only physical frames at
`repositories/<repository-coordinate>/decisions.jsonl`; C7.1 does not build a
capsule or consolidate decisions.

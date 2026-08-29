# Floati C7.2 Read Bundle

`c7.2-candidate` is a versioned, read-only snapshot contract. Its index is
`bundle-index.json`; its complete source and pointer catalog is
`schema-catalog.json`. C7.2 understands only the version it names. Unknown
index versions fail closed before any family is selected. `bundle/c7.1/` and
all v0 source schemas remain frozen.

## Ordering and segment lineage

`raw/runs/events.jsonl` is the sole causal stream for run state. Decode its
complete frames in physical file order and number them from one. Do not sort
or merge on timestamp, identifier, session name, or filesystem metadata.

A legacy v0 binding has no durable segment identity and C7.2 projects typed
absence for `segment_id`, `segment_kind`, and `predecessor_segment_id`. A v1
binding carries all three fields explicitly: an `initial` segment omits its
predecessor; `resume`, `fork`, and `handoff` name a predecessor in the same
attempt lineage. Segment IDs are unique only within that attempt lineage. A
predecessor must occupy an earlier physical binding position; a same-record
predecessor is legal only at a lower ordinal. No timestamp or identifier
creates, replaces, or reorders a relation.

## Normative projection and verification

`families/run-projection.json` records exact raw-source digests and a
canonical projection. `semantic_digest` excludes raw-byte digests and thus
remains stable when timestamp testimony alone changes. `self_digest` covers
the emitted projection except itself.

A materializer captures source bytes before writing, copies this package plus
every catalog-named source schema, writes the raw ledgers unchanged, and then
emits the projection. A reader validates the package, catalog, copied schema
digests, raw digests, projection shape, and a deterministic re-projection of
the captured bytes. Invalid or incomplete segment lineage refuses closed; it
is never inferred.

# Demo/UAT corpus foundations — Slice 1 evidence

Status: **GATE REQUESTED — NO PASS CLAIM**

Date: 2026-08-16

## Identity and authority

- Floati worktree: `<temp>/spawn-groups`
- Branch: `codex/herdr-adapter-source`
- Clean pre-evidence HEAD:
  `1094cf5bd0a3e2a3a980a76507f9f5186c72210a`
- Governing charter SHA:
  `203f2397d8b43434267a488c6cb3e7d48b6d03f3`
- Governing Puddle task-pack SHA:
  `8b8f7e2e11b68d17f5e5772b8cd4eb5be2343e47`
- Task-pack path:
  `docs/design/demo-uat-task-pack-1-2026-08-16.md`
- Implementation plan commit:
  `75eda2c`
- Corpus contract/bootstrap commit:
  `e895967`
- Capture inventory commit:
  `1094cf5`

The first task-pack envelope carried a fabricated full SHA expanded from an
eight-character prefix. The lane refused the unresolvable identifier instead
of guessing. The correction is durable:

- Initial task-pack envelope: `msg-01a008d599ee7fd591c95e6891a3b9ac`
- Initial delivery: `delivery-01a008d65fab764e88dcb94357f91a3a`
- Initial acknowledgment: `ack-01a008d683a777648ba580ea205a08da`
- Publication blocker report: `msg-01a008d777ef721bb9204f821eced429`
- Corrected-SHA envelope: `msg-01a008d847f47950b753f67e6727163f`
- Corrected delivery: `delivery-01a008d88ecf7485a5fd4737d5914adb`
- Corrected acknowledgment: `ack-01a008d8b4b97a8297a5fd2d361d022e`

Only the corrected, Git-resolvable task-pack commit governed implementation.

## Ruled artifact boundary

`docs/demo/corpus.v0.jsonl` contains six `capture_ref` rows. It references
durable identifiers and never copies or redacts ledger JSON into the
repository.

The old root is frozen. Each old-root row covers one physical ledger file and
carries that file's SHA-256 freeze witness:

- `events.jsonl`:
  `1b63aad8c700bca0ecd486eb90e610befa509e1a122320c9f9e14010a0dd225d`
- `receipts/deliveries/build lane.jsonl`:
  `5f69ea1c4ec17e2b7a3298bd05dee8228612c5dda821f865bb29f77916faee5a`
- `receipts/acks/build lane.jsonl`:
  `2ad0f17f536faaff4df8bba499cadc95273be2a5f641af0e66cd4dcd10330168`

Referenced old-root IDs:

- `msg-01a0088b4614704cba93aecce322c67a`
- `msg-01a0088b76c8796c9fb86c2d49d24b3f`
- `delivery-01a0088c310c7aa9bd7a529327b010de`
- `ack-01a0088c6f677def9f5e18808184998a`

The successor root is live. Its three rows set `frozen:false` and
`ledger_sha256:null`; a hash of a growing ledger would become stale.

Referenced successor-root IDs:

- `msg-01a0088c08207e31b874d517621e164b`
- `delivery-01a0088cb1e0792eaef6ebb881124680`
- `ack-01a0088d18e77c9e9fa3599f20038f9d`

Exact-field searches confirmed every durable record ID once in its
authoritative ledger family. A broad preliminary text search also found the
opener ID quoted by a later incident note; the final probe correctly matched
the JSON `id` field, not prose references.

## RED / GREEN evidence

Baseline before Slice 1:

- `python3 -m unittest discover`
- Exit: 0
- Result: 1,460 tests, 0 failures, 0 errors; no skips reported.
- Duration: 165.012 seconds.

The first focused attempt was not accepted as RED: the absent corpus escaped
as an unhandled `FileNotFoundError`. The test boundary was corrected to assert
that the committed corpus file exists.

Accepted RED:

- `PYTHONPYCACHEPREFIX=<temp>/floati-demo-uat-pycache python3 -m unittest -v tests.test_demo_corpus`
- Exit: 1
- Result: 3 tests; one expected assertion failure,
  `committed v0 corpus must exist`; both mutation guards passed.

GREEN after the minimal six-row manifest:

- Same focused command.
- Exit: 0
- Result: 3 tests, 0 failures, 0 errors; no skips reported.

The guard rejects unknown keys, non-object rows, unruled kind/class values,
relative roots, empty tenants, empty/duplicate/malformed ID lists,
non-boolean `frozen`, invalid frozen hashes, hashes on live rows, and empty or
multiline notes. The committed bootstrap additionally requires exactly three
frozen and three live rows.

## Capture inventory

`docs/demo/CAPTURE-INVENTORY.md` lists:

- the ruled dogfood bus-evidence corpus as included;
- the 16-SVG/16-text TUI excellence wall as inventory-only, pending architect
  eye review and a later ruled corpus class;
- six previously banked text captures as inventory-only and not ingested.

No screenshot, cast, hero loop, README media, caption, LoC page asset, public
copy, or copied/redacted bus JSONL entered Slice 1.

## Verification bank

Focused gate:

- `PYTHONPYCACHEPREFIX=<temp>/floati-demo-uat-pycache python3 -m unittest -v tests.test_demo_corpus`
- Exit: 0
- Result: 3 tests, 0 failures, 0 errors; no skips reported.

Full suite:

- `PYTHONPYCACHEPREFIX=<temp>/floati-demo-uat-pycache python3 -m unittest discover`
- Exit: 0
- Result: 1,463 tests, 0 failures, 0 errors; no skips reported.
- Duration: 165.621 seconds.

Bundle self-test:

- `PYTHONPYCACHEPREFIX=<temp>/floati-demo-uat-pycache python3 -m floati.selftest`
- Exit: 0
- Result: 1,463 tests, 0 failures, 0 errors; no skips reported.
- Duration: 165.325 seconds.
- Artifact: `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`

Static hygiene before this evidence file:

- `git diff --check`: exit 0.
- `git status -sb`: clean on `codex/herdr-adapter-source`, ahead 30.

## Gate boundary

This packet requests only the Demo/UAT Slice-1 corpus and inventory verdict.
It does not claim architect review, public asset approval, README voice
approval, capture ingestion, or authorization for Workstreams 2–5. No push is
included.

## ARCHITECT GATE VERDICT — PASS (2026-08-16, at 21b83a58)

Independently verified, unmasked exits:
- Suite 1,463 OK exit 0 · selftest 1,463 OK exit 0, `bundle_verified`.
- **The corpus contract verified against REALITY, not tests:** all
  three frozen-row hashes recomputed by my own hasher against the
  actual tombstoned root's files — 3/3 exact; all three live-row IDs
  located in the successor ledger/receipts; frozen⇔hash invariant
  holds on every row; no ingestion, references only.
- No push before verdict. Task-pack SHA correctly the rev-parsed one.

Slice 1 CLOSED. The migration's full round-trip (opener → delivery →
ack) is now corpus rows — the demo pipeline's first assets are the
product's own birth certificates. Next per pack №1: capture inventory
dispositions come to me with slice 2; rounds 2+3 remain the build
priority.

— the architect (the architect), independent gate. Owner overrules
explicitly; silence = consent.

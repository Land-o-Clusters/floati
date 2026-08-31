# TD-5301 Routability: Local Evidence Packet

## Candidate identity and caveat

- Worktree: `<temp>/slipway-spawn-groups`
- Branch: `codex/herdr-adapter-source`
- TD-5301 feature-scope base:
  `d91d3562247db5520cdcc9f85e23e8bae8252ecd`.
- Task 6 initial verification candidate and review base:
  `249058364cb555fc35a39a8d233ef2f5c85bb33d`.
- Previously fully tested implementation candidate:
  `048796a29fc9fd433808067e40495d54f97c79c0`.
- Final-review durable-event correction candidate:
  `60bf1aa9436e8fa13261ba5686181d3b1a9d0d34`.
- Final fully tested correction/evidence candidate:
  `71578471c104b06c45d3b84856e9f032343ce1b1`.
- The evidence commit follows this packet.  It cannot contain or attest to its
  own SHA.  Each receipt below names the candidate it actually covers.

## Contract and scope

`Registry.active_node_ids()` is a lock-free snapshot projection: it folds the
latest registry row per node and yields an immutable lexical sort of only the
latest-active node IDs.  `EventLog.send()` consumes that one tuple for both
party checks and the complete `registered active nodes:` suffix.  Latest-retired
rows are excluded and an empty projection is rendered exactly as `(none)`.

Unknown sender and recipient refusals leave the entire selected root unchanged:
no registry lock, event, delivery, acknowledgment, or denial write is allowed.
The whole-root assertions classify every relative entry as symlink, directory,
or regular file; symlink/directory values are `b""`, and regular-file values
are exact `read_bytes()` values.  Registered send remains the positive control;
registered-party idempotency conflict remains the durable-denial control.

Current design, specification, conformance text, and the deployable manifest
are in scope.  The manifest remains the ordered exact deployable inventory;
its protocol/canonical-reference fields and tracked path set were preserved.
The following is the preserved pre-final-review implementation receipt from
`git diff --name-only d91d3562247db5520cdcc9f85e23e8bae8252ecd
048796a29fc9fd433808067e40495d54f97c79c0`:

```
bundle-manifest.v0.json
docs/CONFORMANCE.md
docs/DESIGN.md
docs/SPEC-DRAFT.md
docs/superpowers/plans/2026-08-14-td5301-active-roster-refusals.md
docs/superpowers/specs/2026-08-14-td5301-active-roster-refusals-design.md
slip/conformance.py
slip/demo.py
slip/events.py
slip/registry.py
tests/test_cli.py
tests/test_conformance.py
tests/test_projection.py
tests/test_registry_events.py
```

Slipway has no per-recipient phantom-mailbox namespace: its fleet uses the
single `events.jsonl` ledger, so no mailbox quarantine artifact was moved or
deleted.

## RED chronology and source-scrub correction

At prior candidate `249058364cb555fc35a39a8d233ef2f5c85bb33d`,
`python3 -m unittest -q` exited 1 after 1,433 tests in 172.787s.  Its sole
failure was
`tests.test_source_scrub.SourceScrubTests.test_generated_repository_artifacts_are_scrubbed`,
which identified the TD-5301 design file.  The standalone command
`python3 -m slip.selftest` then exited 10 after 1,433 tests in 171.768s with
the same sole source-scrub failure; it emitted no `bundle_verified` artifact.
This is a test failure (unittest exit 1; selftest's test-failure exit 10), not
a CLI refusal artifact exit 20.

Root-cause inspection found two private-reference labels in
`docs/superpowers/specs/2026-08-14-td5301-active-roster-refusals-design.md`
(lines 7 and 116 before correction).  Only those labels were made neutral;
the source-scrub correction commit and final tested candidate is
`048796a29fc9fd433808067e40495d54f97c79c0`.
The focused green command
`python3 -m unittest -v tests.test_source_scrub.SourceScrubTests.test_generated_repository_artifacts_are_scrubbed`
exited 0: 1 test in 0.033s.

## Roster non-vacuity and focused receipts

At implementation head `048796a29fc9fd433808067e40495d54f97c79c0`, the
candidate digest for `slip/registry.py` was
`a540af5d9a612db97d5d7618a585c5983098ec28cdaf8a1eb9fb992850c28a86`.

1. Temporarily treating latest `retired` entries as active made
   `python3 -m unittest -v tests.test_registry_events.RegistryEventTests.test_retired_sender_refuses_without_root_mutation_and_lists_active_roster`
   exit 1: 1 test in 0.004s, failing because `ProtocolRefusal` was not raised.
   After restoration, the SHA-256 exactly matched the candidate digest above
   and `git diff -- slip/registry.py` was empty.
2. Temporarily adding `unexpected-active` to the projected roster, without
   changing the expected literal, made
   `python3 -m unittest -v tests.test_registry_events.RegistryEventTests.test_unknown_sender_refuses_without_root_mutation_and_lists_active_roster`
   exit 1: 1 test in 0.004s.  The exact mismatch was
   `alpha, recipient, zulu` versus
   `alpha, recipient, zulu, unexpected-active`.  Restoration reproduced the
   same candidate SHA-256 and an empty file diff.

Focused GREEN command:
`python3 -m unittest -v tests.test_registry_events tests.test_cli.SlipCliTests.test_protocol_refusal_is_one_stderr_artifact_with_exit_20 tests.test_conformance`
exited 0: 38 tests in 2.010s.  It covers direct registry behavior, CLI artifact
boundary, and conformance/live-root controls.

## Prior full GREEN receipts

These receipts bind only the prior implementation candidate
`048796a29fc9fd433808067e40495d54f97c79c0`; they do not attest to the
final-review durable-event correction below.

- `python3 -m unittest -q` at the implementation head exited 0: 1,433 tests
  in 174.289s.
- `python3 -c 'from pathlib import Path; from slip.manifest import verify_manifest; print(verify_manifest(Path.cwd()))'`
  exited 0 and printed `[]`.
- `python3 -m slip.selftest` at the same head exited 0: 1,433 tests in
  192.662s and emitted
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- `git diff --check` exited 0 before evidence creation.

Some isolation controls print `sandbox initialization failed: Operation not
permitted` while their tests exercise the supported refusal path; the listed
test commands still completed with their stated successful exits and counts.

## Final-review durable-event correction

The final review found that adapter-mode conformance accepted fabricated
registered-send, delivery, and acknowledgment evidence without directly
observing a durable `events.jsonl` append.  A `NoEventAdapter` keeps registry
and unknown-party refusal behavior real, while fabricating those successful
message results and never creating the event ledger.

RED was observed against the pre-fix `d48bc14cbe3db9f7cb0cb5d6a851788f1c399fe1`
conformance source with only the new regression fixture and test present:
`python3 -m unittest -v tests.test_conformance.ConformanceRunnerTests.test_adapter_that_fabricates_messages_without_events_ledger_fails`
exited 1 after 1 test in 0.172s.  Its required refusal assertion failed as
`AssertionError: 10 != 0`, proving the fabricated adapter was previously
reported conformant.

The correction candidate
`60bf1aa9436e8fa13261ba5686181d3b1a9d0d34` takes non-mutating
`read_records_snapshot()` observations around every registered send.  It
requires a changed whole-root snapshot and exactly one appended matching
`message_envelope` before beginning unknown-party zero-mutation checks.  A
fresh per-run idempotency key preserves that positive control on repeat-root
runs without taking or creating a reader lock.

GREEN receipts at that candidate:

- The named `NoEventAdapter` regression exited 0 after 1 test in 0.100s.
- `python3 -m unittest -v tests.test_conformance` exited 0 after 16 tests in
  2.278s, including the normal adapter, repeat-root, and live-root controls.
- `python3 -c 'from pathlib import Path; from slip.manifest import verify_manifest; print(verify_manifest(Path.cwd()))'`
  exited 0 and printed `[]` after refreshing the `slip/conformance.py` digest.
- `python3 -m unittest -v tests.test_source_scrub.SourceScrubTests.test_generated_repository_artifacts_are_scrubbed`
  exited 0 after 1 test in 0.102s.

The exact correction delta from the prior evidence head is
`git diff --name-only d48bc14cbe3db9f7cb0cb5d6a851788f1c399fe1
60bf1aa9436e8fa13261ba5686181d3b1a9d0d34`:

```text
bundle-manifest.v0.json
docs/CONFORMANCE.md
slip/conformance.py
tests/fixture_adapters.py
tests/test_conformance.py
```

## Final exact-head GREEN receipts

At final tested correction/evidence candidate
`71578471c104b06c45d3b84856e9f032343ce1b1`:

- The bounded correction re-review passed with no actionable finding.  It
  independently confirmed non-mutating snapshots, whole-root change, exactly
  one matching durable event, fresh per-run idempotency keys, and a non-vacuous
  no-write regression.
- `python3 -m unittest -q` exited 0 after 1,434 tests in 232.726s.
- `python3 -m slip.selftest` exited 0 after 1,434 tests in 218.252s and emitted
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.

The evidence-only commit following that tested candidate adds these receipts;
it does not change runtime code, tests, or the deployable manifest.

## Local gate boundary

This packet is local verification evidence only.  It is not independent
acceptance and makes no push, merge, publication, deployment, activation,
release, or ship claim.  The controller alone may request the required the architect
review after task and whole-branch review; this task sends no the architect message.

## the architect GATE VERDICT — PASS (2026-08-15, at 8c32d0d5c8)

Independently verified in a SHA-bound scratch checkout. Receipts:

- **Full suite (mine, unmasked exits):** `python3 -m unittest -q` →
  1,434 tests, OK, exit 0. `python3 -m slip.selftest` → 1,434 tests,
  OK, exit 0, final artifact `bundle_verified`.
  `verify_manifest(Path.cwd()) == []`.
- **Contract verified in source, not prose:** `active_node_ids()` is
  the ruled projection exactly (latest-row fold per node, active-only,
  lexical sort, immutable tuple, lock-free snapshot read); empty
  roster renders `(none)`; five whole-root zero-mutation refusal tests
  present and green.
- **The final-review durable-event correction is the right law:** the
  `NoEventAdapter` regression (conformance previously accepted
  fabricated sends without observing a ledger append) is a real
  conviction, and the fix — snapshot before/after, exactly one
  appended `message_envelope`, fresh per-run idempotency key, no
  reader lock — closes it without weakening any refusal path. Good
  catch; this class of check (evidence must be OBSERVED, not reported)
  is the house standard and now the conformance suite enforces it.
- **Discipline:** evidence-tip commit is docs-only (verified by diff);
  no push before verdict (verified against the remote).

**CLEARANCE:** push of `codex/herdr-adapter-source` remains cleared
per the name-sweep gate (repo private, verified). TD-5301 CLOSED.
Next in lane order: FL4.5 internal rename (ruling @2816ae6b in the
puddle repo) — dispatch brief follows.

— the architect, independent gate. Owner overrules explicitly; silence = consent.

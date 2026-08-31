# Post-v1 Thread Observer evidence

Date: 2026-08-13

Branch: `codex/thread-observer`

Frozen comparison base: `a15fe6f5a8ae7cabd2e9518cad55fc4409ca801c`

Task 4 input and tested committed HEAD:
`8b01854` (`feat: expose registered thread observations`)

This document describes that exact committed input plus the Task 4 candidate
paths listed below. A commit cannot contain its own object ID, so the final
Task 4 commit is recorded in the ignored Task 4 report after it exists. No
merge, `main` mutation, installation, activation, release, live provider
mutation, or publication is claimed here.

## Exact candidate scope

The complete candidate delta from the frozen base contains these 33 tracked
paths, including this evidence file. Task 4 itself changes this evidence file,
the manifest, three v1 integration schemas, three production modules, and nine
test modules.

```text
bundle-manifest.v0.json
docs/COPY-LEDGER.md
docs/evidence/POST-V1-THREAD-OBSERVER.md
docs/superpowers/plans/2026-08-13-thread-observer.md
docs/superpowers/specs/2026-08-13-thread-observer-design.md
schemas/v1/fleet-status-artifact.schema.json
schemas/v1/thread-attachment-detached-record.schema.json
schemas/v1/thread-attachment-registered-record.schema.json
schemas/v1/thread-observation-recorded-record.schema.json
schemas/v1/thread-observation-status-artifact.schema.json
schemas/v1/watch-artifact.schema.json
slip/cli.py
slip/helptext.py
slip/jsonl.py
slip/projection.py
slip/records.py
slip/runtruth.py
slip/thread_observations.py
slip/thread_source.py
tests/fixtures/codex-thread-observer/reference_harness.py
tests/schema_validation.py
tests/test_codex_adapter_contract.py
tests/test_gauntlet_concurrency.py
tests/test_gauntlet_crash.py
tests/test_gauntlet_fuzz.py
tests/test_manifest.py
tests/test_record_validation.py
tests/test_runtruth.py
tests/test_schemas.py
tests/test_sequencer_epoch.py
tests/test_thread_cli.py
tests/test_thread_observations.py
tests/test_thread_source.py
```

No file under `schemas/v0`, `bundle/c7.1`, or `bundle/c7.2` differs from the
exact frozen base.

## Contract and authority established

- Three closed schema-v1 durable records register one explicit Codex thread,
  append normalized read-only observation testimony, and detach it. Provider
  coordinates are canonical lowercase hyphenated UUIDv7 values; Slipway's
  compact internal identifiers remain unchanged.
- Runtime and Draft 2020-12 schemas agree on exact actors, terminal-unsafe
  strings, integral provider timestamps, evidence classes, normalized flags,
  attention, outcomes, and reasons. Canonical observation digests normalize
  integral JSON numbers before hashing.
- One controller-owned Thread ledger transaction is the only append authority.
  Generic JSONL append and transaction APIs refuse the reserved path, and
  replay refuses reused physical IDs, changed coordinates, unchanged duplicate
  testimony, observation after detachment, and invalid orderings.
- Thread status is physically read-only. It retains every closed
  `{value,evidence_class}` object, redacts provider coordinates from plural and
  fleet artifacts, sorts attention deterministically, and participates in
  snapshot invalidation. Attach, observe, detach, and show all return the same
  v1 status artifact rather than raw ledger rows.
- The source launches one exact command, sends only `initialize` request ID 1,
  `initialized`, and `thread/read` request ID 2 with
  `{"threadId":<canonical UUIDv7>,"includeTurns":false}`. It accepts one exact
  response ID, ignores only a bounded `thread/status/changed` notification
  envelope and uses none of its parameters as testimony, refuses every other
  notification plus duplicate keys/trailing bytes/nonempty turns and extra
  response/status fields, ignores unruled Thread content such as
  title/preview/cwd, caps public deadlines at 60 seconds, and accounts for
  TERM/KILL/reap and pipe-close cleanup.
- Production source inventory contains the single provider read method
  `thread/read`. It contains no thread list, mutation, title, prompt, preview,
  text, liveness, or task-inventory classifier.

## RED and review chronology

All production work was preceded by its corresponding failing test with a
lawful control.

1. Task 1 began with 15 missing-contract failures. Semantic, replay, and
   schema/runtime tests then exposed wrong derived attention, duplicate
   testimony, flag-closure drift, numeric digest drift, actor/schema drift,
   terminal-newline admission, reused physical IDs, and nested unhashable
   values. Independent review found four Important issues; each was fixed
   RED-first. The canonical Codex UUID compatibility round began with three
   lawful-row errors. Final Task 1 and cross-fix commits are `0ccb22b` and
   `d5a6b39`; both rereviews were READY.
2. Task 2 began with 22 missing source/file errors across eight tests. Further
   REDs covered non-string methods, duplicate JSON members, extreme finite
   deadlines escaping cleanup, reader exceptions, float response IDs, null
   error objects, unproven pipe closure, and standard JSON-RPC error `data`.
   Final required source/contract/adapter bank was 46/46; commit `f513a66` was
   independently READY.
3. Task 3 began with four missing-controller failures, followed by artifact
   coordinate, privacy, empty-result, evidence-class, generic-write-authority,
   and raw-record-output failures. Final required bank was 100/100. Commit
   `8b01854` was independently READY with no Critical, Important, or Minor
   findings.
4. Task 4's new crash/concurrency/fuzz slice passed 8/8 after its tests-only
   REDs. Manifest inventory RED named six absent deployable paths before the
   manifest was mechanically regenerated. The first full bank exposed the
   watch-v1 schema omitting the Thread summary; the exact selector failed and
   then passed after the shared summary schema repair. A later full bank
   exposed one loaded 250 ms reference-harness timeout; its exact selector and
   full source module passed immediately.
5. Complete gauntlets preserved a historical 12-writer acceptance-fence
   intermittent. It passed one fresh 82/82 run, but later complete/full runs
   repeatedly produced `ledger_lock_timeout: acceptance.lock lock remained
   contended for 1 second`. A deterministic RED held only the truth-free
   acceptance fence for 1.25 seconds and failed at the generic one-second
   budget. The repair gives only `effects/acceptance.lock` a bounded five-second
   wait; every generic JSONL lock retains its one-second refusal. The exact
   regression, both 12-writer controls, and the generic timeout control passed
   4/4. An old lock-instrumentation wrapper then failed on the new optional
   timeout keyword; its exact selector and full Sequencer epoch module passed
   after forwarding the ruled parameter.
6. An existing Effect Worker crash gauntlet sometimes timed out before its
   provider PID fixture existed under whole-suite load. Its test timeout was
   strengthened from 0.4 to 2.0 seconds so the same real provider-group cleanup
   assertions remain load-bearing; its exact selector passed. No worker
   production code changed.
7. Task 4 review found that the original changed-observation concurrency test
   did not invert testimony time against physical transaction order, did not
   force both observe/detach schedules, and fuzzed only selected string fields.
   A lock-level test seam now forces both children to reach the exact real
   transaction while preserving controller authority; inverted timestamps and
   both schedules pass. Recursive hostile testing now covers every string leaf
   in all three records plus every identifier field. The final review-fix slice
   passed 6/6.
8. Whole-branch review found two schema/runtime gaps: fleet Thread attention
   accepted duplicate states, and a non-active provider status could carry
   nonempty active flags. The first exact schema RED failed because five
   duplicate rows were accepted; the second exact artifact RED failed because
   `idle` plus a measured flag was accepted. Draft 2020-12 tuple validation now
   requires the five runtime states in exact order, and every nonempty flag
   branch requires provider status `active`.
9. Whole-branch review also found self-referential HOME/CODEX_HOME symlinks
   escaping as raw `RuntimeError`. The exact lawful/symlink/loop selector errored
   on the loop before production changed; canonical directory and executable
   resolution now map both OS and symlink-loop failures to typed
   `unknown/provider_unavailable` before process launch.

No non-green result above is relabeled as a pass. The detailed commands,
counts, and durations remain in the Task 4 report.

## Installed app-server schema receipt

This receipt binds the local protocol claim to the installed provider bytes; it
does not claim that a live registered thread was observed.

```text
/opt/homebrew/bin/codex --version
codex-cli 0.147.0

/opt/homebrew/bin/codex app-server generate-json-schema --experimental \
  --out <temp>/slipway-thread-observer-schema-final-20260813
```

Both commands exited 0. They emitted only the local sandbox warning that PATH
aliases could not be created. The five generated schema digests were:

```text
6f0094be9a65242ec779a40794cbd4fdfa32fca1e45084a16adfb50501d33ea2  v1/InitializeParams.json
62ad689c2cb6379913c1d72749cfd8de5089d35760214123518eb92eef11acc9  v1/InitializeResponse.json
7222da641029c071811f6bcb651de347fe037e6689db22b3fad0c5b17b7f1c21  v2/ThreadReadParams.json
4529adb9f247118dd743bb1a276eb43377310efa550c101d573b7629649fc0f9  v2/ThreadReadResponse.json
26f3c60c1b73f7fa2d31c74429cdc36f8746c76c33e3d314b3fb61d3661f05f6  v2/ThreadStatusChangedNotification.json
```

The generated `ThreadReadParams` requires `threadId` and defines optional
boolean `includeTurns`; it does not close extensions. `ThreadReadResponse`
requires one Thread whose provider shape includes `id`, `status`, `turns`, and
`updatedAt`; the source extracts only those four coordinates, requires empty
turns, and ignores other content. The generated active flags are exactly
`waitingOnApproval` and `waitingOnUserInput`. The status notification requires
`threadId` and `status` but likewise does not close extensions. Runtime accepts
only the ruled status-change method with a bounded object envelope and uses no
notification parameter as testimony.

## Final verification receipts

- Pre-review affected JSONL/Run/complete-gauntlet bank: 146/146 in 36.937s.
- Pre-review Sequencer epoch bank: 20/20 in 3.229s.
- Final review-fix slice: 6/6 in 0.775s.
- Final Thread/source/CLI/projection/schema/manifest bank: 140/140 in 21.406s.
- Final complete crash/concurrency/fuzz bank: 82/82 in 29.089s.
- Final full suite: 1,325/1,325 in 177.923s, exit 0.
- Final selftest: 1,325/1,325 in 175.612s, exit 0, followed by
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- Final source/copy ledger bank: 10/10 in 0.563s.
- Python 3.9-compatible `py_compile`, `git diff --check`, direct manifest
  verification (`[]`), `python3 -m slip.copy`, and the exact frozen-base diff
  for `schemas/v0`, `bundle/c7.1`, and `bundle/c7.2`: exit 0.

The repeated `sandbox initialization failed: Operation not permitted` lines
were emitted only by conditional unsupported-host isolation controls whose
assertions passed. They are not supported-host kernel-enforcement proof.

## Evidence boundary and limitations

- The reference harness is deterministic local protocol evidence. It is not a
  Codex app-server implementation, inventory contract, live registered-thread
  observation, or provider availability proof.
- One bounded installed app-server lookup against a deliberately nonexistent
  UUID returned typed `unknown/provider_unavailable`; it proves only honest
  absence handling. No live registered thread was read in this work.
- The observer has no list/discovery operation and no mutation operation. A
  user must explicitly register the provider thread coordinate.
- macOS nested Seatbelt on this host yields honest typed unsupported outcomes;
  it does not prove supported enforcement on a detached host.
- Final independent scoped and whole-branch rereviews are READY with no
  Critical or Important findings; their exact dispositions are recorded in
  the Task 4 report. No push or publication claim is made here.

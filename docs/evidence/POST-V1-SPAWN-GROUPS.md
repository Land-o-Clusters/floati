# Post-v1 Spawn Groups evidence

Date: 2026-08-11
Branch: `codex/spawn-groups`
Frozen publication base: `14c4987be28ef05483f1c5ee867fd3215e83c14c`
Task 5 starting HEAD: `d475bfb1e16e352782e573e351bbdb4c1ca65069`

The governed Spawn Groups implementation range before this proof task is:

```text
294ea62 rebuild: recover governed spawn groups
4e5efb8 fix: remove spawn testimony from socket
0cdda25 fix: align managed spawn transport semantics
1eb6d31 fix: bound sequencer request buffers
29ca0ed fix: serialize sequencer shutdown state
d475bfb test: make sequencer shutdown proof deterministic
```

The Task 5 evidence document and tests are carried by the commit containing this
file; a commit cannot truthfully embed its own SHA-1 in its contents.

## Binding pre-implementation baseline

Before Spawn Groups implementation, the binding complete-suite baseline was:

```text
python3 -m unittest -q
Ran 817 tests in 61.684s
FAILED (failures=1)
```

The only failure was
`ConcurrentWriterGauntletTests.test_twelve_owner_built_runs_keep_per_run_projection_and_ids_distinct_under_contention`.
The isolated exact test then passed 1/1 in 1.129 seconds. The contemporaneous
record does not preserve the isolated rerun's command text, so none is invented
here. That isolated pass does not replace or relabel the failed binding
complete-suite baseline.

## Durable and authority boundaries proved

- Run-ledger physical order is the only Spawn Groups lifecycle truth. The proof
  adds no snapshot authority, side registry, bearer token, or second sequencer.
- Group creation is a recoverable created/amendment pair. Exact response-loss
  retry after local sequencer restart returns the same pair without rewriting
  run-ledger bytes. An injected short write rolls back to the prior complete
  ledger, after which exact retry commits one pair.
- Child admission, rejection, cancellation, join close, parent result and
  terminal transitions remain causally fenced by the durable group prefix.
  Hostile graph, plan digest, admission-chain, membership, resource, join-set,
  retry, and missing-close testimony cannot append a competing truth.
- Descendant events and observation close remain direct in-process
  `WorkerRunner`/controller testimony under a live nonserializable launch
  capability. They are not socket operations. Forged capability objects,
  disabled-mode updates, post-close events, raw private records, and a fabricated
  evaluated descendant operation refuse without changing the run ledger.
- The shared sequencer accepts at most 64 MiB per frame, reads at a 64 KiB
  quantum, and caps aggregate request buffers at
  `MAX_FRAME_BYTES + (MAX_CLIENTS - 1) * SOCKET_READ_BYTES` = 134,152,192 bytes
  for 1,024 clients. Close is synchronized and idempotent. Durable JSONL records
  retain their separate 65,536-byte ceiling.
- `bundle/c7.1`, `bundle/c7.2`, and `schemas/v0` are byte-identical to
  `14c4987be28ef05483f1c5ee867fd3215e83c14c`.

## Hostile coverage

Every new Spawn Groups fuzz family begins with a lawful full group lifecycle,
observation close, and a complete accepted-result parent lifecycle. Each new
crash family also runs that control first. The existing sealed-segment family
executes a lawful segmented writer/reader control for every hostile subcase.
The combined exact hostile bank executes:

- duplicate-key, truncated, oversized, non-UTF8, and non-finite run frames,
  with exact refusal codes, failed-writer refusal, and byte preservation;
- graph and digest tamper, causal reorder, changed membership, missing full-plan
  enablement, pre-admission launch, admission-chain drift, and limit,
  capability, workspace, and budget widening, with real governed-writer calls
  and exact raw-ledger byte snapshots at each applicable hostile boundary;
- pending activation/terminal and request/create/activation races, forged join
  sets, dependency-expanded sibling cancellation refusal, zero-attempt run/item
  cancellation, missing cancellation-driven close, post-cancel retry opening,
  parent acceptance before join, late-result reopening, forged operator
  capability, and missing observation close. Each of those five added fuzz
  families is self-contained and starts with a lawful full-lifecycle control;
- post-close descendant evidence, launch-mode and pipe/capability bypass, raw
  private socket frames, and forbidden evaluated descendant operations;
- response loss/restart, short-write rollback, altered seal testimony,
  immutable sealed-segment tamper, and exact writer-byte preservation.

## RED evidence

No production or manifest file changed before the hostile RED. The first exact
bank was intentionally retained because it exposed test-fixture defects:

```text
python3 -m unittest -v tests.test_spawn_groups tests.test_gauntlet_fuzz \
  tests.test_gauntlet_crash tests.test_manifest
Ran 154 tests in 22.605s
FAILED (failures=67, errors=10)
```

The new lawful helper had omitted accepted parent result truth, the old HM3I
inventory control omitted the two additive cancellation kinds, and the crash
interceptor did not forward the current `resolve_existing` keyword. Those
test-only defects cascaded through crash subtests and were not treated as
product gaps. Subsequent seven-test diagnostics honestly retained one failure
and ten errors in 0.866 seconds, then three failures and one error in 1.120
seconds, then two failures in 1.491 seconds. The corrected diagnostics passed
7/7 in 1.538 seconds.

The clean exact RED, still with unchanged production and manifest, was:

```text
python3 -m unittest -v tests.test_spawn_groups tests.test_gauntlet_fuzz \
  tests.test_gauntlet_crash tests.test_manifest
Ran 154 tests in 16.584s
FAILED (failures=11)
```

All 137 non-manifest tests passed. The eleven failures were manifest-only: the
complete sixteen-path Spawn Groups runtime/schema set was missing; aggregate
currency reported `tracked_set_mismatch`; and current digests were absent for
`schemas/v1/run-dispatch-decision-record.schema.json`, `slip/admission.py`,
`slip/cancellation.py`, `slip/capability_binding.py`, `slip/records.py`,
`slip/runtruth.py`, `slip/scheduler.py`, `slip/sequencer.py`, and
`slip/workers.py`. No production correction was proved or made.

## Manifest correction and hostile GREEN

`bundle-manifest.v0.json` was regenerated mechanically from
`slip.manifest._deployable_paths(root)`: every canonical deployable path was
read from the repository, SHA-256 hashed, and emitted in generator order. No
unrelated entry was hand-edited. A manifest regression asserts the sorted set
and exact bytes for `slip/spawn_groups.py` plus all fifteen new v1 Spawn Groups
schemas.

After that sole non-test correction, the exact hostile bank passed 154/154 in
17.911 seconds.

## Ordered verification

The first ordered sequence is retained as failed evidence:

1. Required Spawn Groups regression bank: 373/373 in 27.555 seconds, `OK`.
2. Full `python3 -m unittest -q`: 936/936 in 97.490 seconds, `OK`.
3. `python3 -m slip.selftest`: 936 tests in 92.774 seconds,
   `FAILED (errors=1)`. The other 935 passed; the sequencer round-robin test's sequential
   noisy client timed out and surfaced `sequencer_unavailable`.

The failed sequencer test then passed once in isolation in 0.400 seconds and
five consecutive selections in 2.098 seconds. No source or test threshold was
changed. The complete ordered sequence was restarted from gate one:

1. Required Spawn Groups regression bank: 373/373 in 25.152 seconds, `OK`.
2. Full `python3 -m unittest -q`: 936/936 in 84.360 seconds, `OK`.
3. `python3 -m slip.selftest`: 936/936 in 95.472 seconds, `OK`, followed by
   `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
4. Direct manifest verification emitted no output and exited zero.
5. Source scrub: 8/8 in 0.361 seconds, `OK`.
6. Frozen `bundle/c7.1`, `bundle/c7.2`, and `schemas/v0` diff from
   `14c4987be28ef05483f1c5ee867fd3215e83c14c` emitted no output and exited zero.
7. `/usr/bin/git diff --check` emitted no output and exited zero.

After adding this tracked evidence artifact, direct manifest verification was
again silent/zero, source scrub passed 8/8 in 0.748 seconds, and the frozen diff
and `/usr/bin/git diff --check` were again silent/zero.

Independent review then requested five additional self-contained hostile fuzz
families and stronger actual-writer byte-preservation proof. The test/evidence
fix changed no production, shared fixture, schema, bundle, or manifest file.
After two fixture-only focused failures (2 errors, then 1 error), the focused
Spawn hostile class passed 9/9 in 2.090 seconds. The complete hostile bank
passed 159/159 in 18.944 seconds, and the exact ordered Spawn regression gate
passed 378/378 in 24.491 seconds. On the exact final test tree, the complete
suite passed 941/941 in 102.520 seconds and `python3 -m slip.selftest` passed
941/941 in 88.815 seconds followed by
`{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
Direct manifest verification was silent/zero, source scrub passed 8/8 in 0.335
seconds, and the frozen-surface diff plus `/usr/bin/git diff --check` were
silent/zero. The manifest remained unchanged.

## Final whole-branch fix wave

The final wave began from exact clean tracked/index state at
`b15ac3da0bc75e26aad194180365180ebcafaeee` on `codex/spawn-groups`. Every
finding control was authored before any production, schema, or manifest edit.
Each hostile cluster has a lawful positive; descendant testimony positives use
the real `WorkerRunner` fork/private-pipe path rather than minting authority
from durable state.

The first exact combined nine-test RED ran in 222.075 seconds and exited one
with 144 subtest failures and 408 subtest errors. That result retained two
test-harness defects: the conforming schema validator attempted remote `$ref`
retrieval, and the two-member managed-close fixture supplied an invalid merge
gate. Only the tests were corrected, using a local Draft 2020-12 registry and a
lawful two-member plan. Fixture-corrected diagnostics then had no harness
errors and proved only the requested gaps:

- managed satisfied close refused `ledger_lock_timeout`;
- a crash-reserved retry refused cancellation at `cancel_request_fence`;
- exact child-only cancellation fenced unrelated parent creation;
- synchronized identical exact callers returned different durable winners;
- whole-run Spawn cancellation projected `failed`;
- direct launch minting was the only path to the testimony positive;
- shutdown retained one cached response and allowed in-flight repopulation;
- the all-fifteen runtime/schema matrix recorded 242 conforming-schema
  acceptances of runtime-illegal terminal-control strings in 0.280 seconds.

With production still untouched at that checkpoint, the exact focused
nine-test selection was the implementation gate. After the integrated fix it
passed 9/9 in 1.499 seconds. The first affected 15-module bank then ran 369
tests in 31.375 seconds and had one test-only error: its unknown-descendant
fixture expected a completion even though canonical projection correctly
refused `untracked_descendant_unknown`. The test was corrected to retain the
lawful terminated/adopted controls and assert unknown refusal plus durable
unknown testimony. The exact bank then passed 369/369 in 25.459 seconds. One
subsequent four-test selection used a nonexistent method name and failed at
test loading; the corrected four-test authority/race/crash selection passed
4/4 in 0.583 seconds. No product defect was inferred from either fixture or
selection error.

The integrated corrections preserve the governed boundaries:

- live descendant and observation-close testimony receives one process-local,
  nonserializable capability only after the actual adapter process starts and
  its parent pipe exists; the former free-standing runner/controller mint
  seams refuse, capability lifetime ends in runner cleanup, and the ledger
  retains no side authority registry;
- the method-local managed evaluation ledger bridges scheduler and
  cancellation appends through its live service lease, so direct and managed
  satisfied-close paths produce the same durable semantics without a second
  ledger;
- cancellation may consume either a materialized retry schedule or the exact
  retry coordinates reserved by the preceding terminal into one
  `attempt_cancelled_before_start`; exact retry aliases the physical winner and
  changed coordinates refuse;
- exact-item fencing tests only physical request membership, identical exact
  cancellation contenders resolve semantic aliases under the existing writer
  transaction, and divergent coordinates append nothing;
- the whole-run cancellation override applies only to complete run coverage
  with at least one applicable non-aborted group and every applicable group
  closed cancelled under the same resolution; unrelated `fail_run` behavior
  remains frozen;
- all runtime-lexical string locations, including nested map keys, in the
  eleven principal Spawn v1 schemas reject C0/DEL terminal controls; all
  fifteen Spawn schemas pass lawful runtime/conforming Draft 2020-12 parity;
- terminal sequencer close clears the response cache under the state lock, and
  closed-aware `_remember` cannot repopulate it from an in-flight request.

After source and schema bytes were final, `bundle-manifest.v0.json` was
regenerated mechanically from `slip.manifest._deployable_paths(root)`, hashing
each canonical path in generator order. Its focused contract bank passed 17/17
in 0.053 seconds.

The exact Task 5 ordered gates then passed in order on the final implementation
bytes:

1. Required verbose Spawn regression bank: 386/386 in 24.824 seconds, `OK`.
2. Complete `python3 -m unittest -q`: 949/949 in 69.938 seconds, `OK`.
3. `python3 -m slip.selftest`: 949/949 in 72.333 seconds, `OK`, followed by
   `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
4. Direct manifest verification emitted no output and exited zero.
5. Source scrub: 8/8 in 0.281 seconds, `OK`.
6. Frozen `bundle/c7.1`, `bundle/c7.2`, and `schemas/v0` diff from
   `14c4987be28ef05483f1c5ee867fd3215e83c14c` emitted no output and exited zero.
7. `/usr/bin/git diff --check` emitted no output and exited zero.

After adding this final-wave tracked evidence, publication-sensitive gates were
repeated on its exact bytes: manifest verification was silent/zero, source
scrub passed 8/8 in 0.331 seconds, and the frozen-surface diff plus
`/usr/bin/git diff --check` were silent/zero.

The commit containing this final-wave evidence cannot truthfully embed its own
SHA-1. The ignored chronological report and progress ledger retain the more
detailed command-by-command history and are not staged publication inputs.

## Operator-authorized breaker closure

This bounded breaker wave started from the exact clean linked-worktree identity
`codex/spawn-groups` at
`621faf4e0cd761bd9b6334a149bde5bcdd4745f1`. It addresses only the two final
scoped residuals: caller-forgeable descendant launch testimony and incomplete
Spawn v1 runtime/schema terminal-control parity.

Before any production, schema, or manifest byte changed, the fixture-corrected
combined breaker bank retained exactly the two finding failures:

```text
Ran 4 tests in 12.529s
FAILED (failures=2)
```

The real `WorkerRunner.run()` fork/private-pipe testimony control and the
process-start-failure zero-append control passed. The importable factory/class
probe used fake process and connection objects to append observed, terminated,
and observation-close records, and copied the authority across a real fork.
The all-fifteen conforming Draft 2020-12 matrix counted 3,662 guard/path
acceptances of runtime-illegal strings. There were no remaining fixture errors.

The live-launch correction removes the module-level factory and capability
class entirely. Only the active `WorkerRunner.run()` stack creates the opaque
identity, after the actual private pipe exists and `Process.start()` succeeds.
Its closure binds the exact runner/controller, real started `BaseProcess`, real
open `Connection`, active frame, owner/parent process identity, and governed
run coordinates. Direct mint/event/close seams refuse. The identity is never
returned, serialized, registered, sent through a socket, cached in durable
state, or retained after the launch. Import/fake, closed/dead/wrong-pipe,
stale, cross-run, fork-copy, unrelated real-process/real-pipe, and start-failure
hostiles all refuse with zero run-ledger append. Lawful observed testimony and
observation close use the real fork/private-pipe path.

An aggressive intermediate review caught an initially insufficient check: an
unrelated genuinely started process and genuine pipe supplied by a caller
closure appended one observation. Binding the opaque identity to the exact
active `WorkerRunner.run()` frame closed that path. A test-only interception of
the actual live identity now proves wrong real pipe, dead real process,
cross-run, fork-copy, and post-lifecycle stale use all refuse without append.
The same review caught a handshake regression in which a
governed `disabled` context waited for an observation-close acknowledgment.
Its clean direct-pipe RED was one test with one failure; mode-scoping the wait
made disabled and observed real-pipe controls pass 2/2 while preserving the
adapter's disabled context.

The lexical correction mechanically replaces exactly 262 old terminal guards
across all fifteen Spawn v1 schemas with one identical repository-established
complete runtime guard. A structural HEAD/current comparison proves those are
the only parsed schema value changes. The test enumerates all 2,248 configured
runtime-unsafe Unicode points at every discovered guard and exercises every
fixture string value and governed map key with C0, DEL, C1 (including U+0085),
the full configured bidi set (including U+202E), and surrogate representatives.
Runtime-lawful non-ASCII controls remain positive controls. The expanded
breaker bank passed 5/5 in 12.490 seconds.

After final source and schema bytes, the manifest was refreshed mechanically.
Direct verification returned `[]`, and the manifest bank passed 17/17 in 0.050
seconds. The exact final ordered gates were:

1. Required verbose Spawn bank: 389/389 in 33.959 seconds, `OK`.
2. Complete suite: 952/952 in 78.109 seconds, `OK`.
3. `python3 -m slip.selftest`: 952/952 in 79.345 seconds, `OK`, followed by
   `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
4. Mandated manifest module invocation: silent, exit zero; non-vacuous direct
   verification and the manifest/selftest banks are recorded above.
5. Source scrub: 8/8 in 0.263 seconds, `OK`.
6. Frozen `bundle/c7.1`, `bundle/c7.2`, and `schemas/v0` diff from
   `14c4987be28ef05483f1c5ee867fd3215e83c14c`: silent, exit zero.
7. `/usr/bin/git diff --check`: silent, exit zero.

The unchanged 12-process concurrency gauntlet surfaced nondeterministically in
one earlier complete-suite attempt (952 tests in 79.354 seconds) and one later
selftest attempt (952 tests in 78.595 seconds). A 20-run direct diagnostic
reproduced it once and exposed the exact child result as
`ProtocolRefusal:ledger_lock_timeout`; the other 19 passed. No source or fixture
change was made for that out-of-scope contention result before the clean
complete-suite and selftest reruns above. The ignored chronological breaker
report retains every intermediate command and result and is not a publication
input.

## Limits of this evidence

- Tests use local temporary roots, local Unix sockets, controlled fork/process
  fixtures, and injected response-loss/write failures. They are not real power
  loss, a live multi-host deployment, or live provider-native descendant proof.
- No external provider parking/continuation, activation, publication, merge,
  release, push, or bus checkpoint is claimed.
- No independent review dispatch was performed in this final fix wave; the
  required aggressive implementation self-review is local evidence only.

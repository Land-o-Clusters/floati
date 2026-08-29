# Post-v1 Sequencer Scale Evidence

Date: 2026-08-09

Branch: `codex/sequencer-scale`

Exact plan base: `4f1de17f5e68b6bc792e6716e593ca0e90f7ac68`

Task 6 implementation base: `07a09e8f337e5a07218bfbe3f6246a22574fc8b4`

Candidate identity: this evidence is bound by its containing commit. Re-derive
the immutable candidate with `git rev-parse HEAD`; no self-referential commit
SHA is predicted inside the commit that contains it.

## Scope

This checkpoint proves a deterministic local hostile-scale fixture for the
optional Unix-socket sequencer, immutable segmented run storage, daemonless
replay equality, strict framing, deployable-manifest currency, and the pinned
publication surfaces. It does not claim live fleet scale, launchd installation
or activation, publication, release, suspension, spawn groups, or hosted CI.
Peak RSS is testimony from this one local fixture process, not a fleet limit.

## RED witnesses

Before production edits, the combined Task 6 bank ran 20 tests in 0.456
seconds with four expected failures:

- `ScaleConfig` and `run_scale_fixture` were absent;
- a duplicate-key immutable segment frame was accepted;
- the manifest omitted post-v1 runtime and schema entries; and
- the tracked manifest contained stale deployable digests.

After the first full-scale attempt exposed restart latency, the focused test
`SequencerScaleTests.test_restarted_exact_retry_uses_validated_id_index_without_projection_replay`
failed in 0.008 seconds because `RunProjection.empty` was reached. The exact
retry path was rebuilding the entire durable projection before finding the
already-committed record, causing the first 200,000-record restart retry to
time out. The production correction performs the identical-or-divergent
decision inside the existing validated writer-locked snapshot before semantic
replay; no timeout or workload threshold was changed.

The full suite also honestly exposed a stale generated copy ledger. Its focused
test failed 1/1 in 0.008 seconds, then passed 2/2 in 0.035 seconds after the
canonical `python3 -m slip.copy` regeneration.

Independent review of commit
`4211b016cbcced284a2b281147a9907788ebf8c2` found two cache-identity defects,
an O(n) retry lookup, and evidence that modeled rather than exercised socket
fairness and response loss. Before the review fixes, focused regressions
witnessed these exact failures:

- caller mutation poisoned cached record identity: 1 test in 0.004 seconds,
  failed because an unequal same-ledger retry did not raise
  `duplicate_record_id`;
- a same-count valid active-segment replacement reused a stale projection: 1
  test in 0.005 seconds, failed because the invalid dependent task record was
  accepted;
- an old exact retry after intervening commits reverse-scanned the snapshot: 1
  test in 0.012 seconds, failed at the instrumented reverse-scan boundary; and
- the fast scale fixture lacked a real socket fairness source: 1 test in 7.859
  seconds, errored on the absent `fairness.source` field.

The first full-client gate attempt then exposed the host listen-backlog edge:
it exited 1 after 0.046 seconds with `ConnectionRefusedError: [Errno 61]`
before the lifecycle bulk. A focused 100-client regression reproduced the same
error in 0.007 seconds. The corrected phase queues one response-loss request
and all 99 quiet peers before service starts, then continuously submits the
remaining noisy-client requests while service is live; no service/client
timeout or acceptance threshold changed.

Self-review added a lock-boundary regression after noticing that a second
transaction could sample cache identity after the append lock was released.
That regression failed 1/1 in 0.005 seconds because an intervening durable
replacement could bind a stale projection to new bytes. Cache identity is now
returned atomically with the append under the existing writer lock; the focused
regression bank passed 3/3 in 0.015 seconds.

Re-review then found that a public transaction callback could still mutate a
`LocatedRecord.record` reached through `snapshot._located`, because the tuple
and ID-map containers owned their shells but not their record mappings. The
exact public-callback regression failed 1/1 in 0.005 seconds when an unequal
same-ledger retry did not raise `duplicate_record_id`, while durable bytes and
a fresh ledger retained the original identity. Callback-visible `_located`
and `_known` views are now lazy snapshot-owned copies; lookup and iteration
continue returning per-record copies from private validated state. This keeps
the managed cache-hit path from eagerly copying the full durable prefix. The
focused ownership/cache/index bank passed 6/6 in 0.046 seconds and the broader
segment/runtruth/sequencer/scale bank passed 84/84 in 17.403 seconds.

## Pre-setter deterministic scale result

After the lazy owned-view/getter implementation and before the later
compatibility setter-only consistency edits,
`python3 scripts/sequencer-scale-gate.py` completed with exit 0 and emitted a
`status: passed` artifact in 1,263.114 seconds. This is path evidence for the
managed batch implementation at that point, not an exact-tip execution of the
final setter-consistency commit:

- requested lifecycle records: 1,000,000;
- valid run records: 1,010,829;
- admitted items: 10,000 across 100 fixture client principals;
- batch size: 50; segment record bound: 10,000;
- restart batch ordinals: 4,000, 10,000, 16,000;
- restart record offsets: 200,000, 500,000, 800,000;
- duplicate record IDs: 0;
- lost acknowledged records: 0;
- unknown responses: 4; exact retry resolutions: 4;
- real socket response losses: 1; real exact-retry resolutions: 1;
- injected restart unknowns: 3;
- ledger-lock timeouts: 0;
- real Unix-socket service turns: 201;
- maximum measured service turns: 100, bounded by 101;
- segment files: 102; sealed digest failures: 0;
- peak process RSS: 5,981,863,936 bytes;
- sequencer projection digest:
  `84d295962384dabaf399c45a73313f210eec97fcf8ed484f8b69fc3528fdacd2`;
- daemonless projection digest:
  `84d295962384dabaf399c45a73313f210eec97fcf8ed484f8b69fc3528fdacd2`;
- streaming/direct replay equality: true.

The interrupted pre-restart gate had no recoverable output channel and was
classified `UNVERIFIED`. A subsequent fresh run failed after approximately
137.855 seconds at the first restart with typed `sequencer_unavailable`; it is
recorded as failed evidence, not counted as the passing run above.

## Final-code verification

After the compatibility setters were made to copy every supplied
`LocatedRecord.record` and their lazy-view consistency was finalized, the exact
final code state produced these fresh results:

- focused ownership/cache/index controls: 6 tests in 0.035 seconds, all
  passed; the post-commit rerun passed the same 6 tests in 0.032 seconds;
- focused sequencer/segment/admission/limits/epoch/wake/scale bank:
  101 tests in 23.598 seconds, all passed;
- full suite: 763 tests in 59.530 seconds, all passed;
- selftest: 763 tests in 64.212 seconds, all passed, followed by
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
- manifest verification: `[]`;
- source/history scrub: 8 tests in 0.261 seconds, all passed;
- frozen publication diff: exit 0 with no output and equal base/current tree
  identities for `bundle/c7.1`, `bundle/c7.2`, and `schemas/v0`;
- `/usr/bin/git diff --check`: exit 0 with no output.

The compatibility setters exist for tests that replace `_located` or `_known`
to instrument O(1) lookup. Managed batches neither assign those attributes nor
read the lazy public views: they use the private validated tuple/ID map through
`lookup` and `iter_records`. The setter-only consistency edits therefore did
not enter the managed million-record path. The pre-setter artifact above is
retained as bounded path evidence and is not presented as an exact-tip run;
the final-code gates listed here are the exact executions after those edits.

The first post-fix selftest ran 763 tests in 84.641 seconds and errored once
when the existing reconnecting-client fairness test hit its socket timeout.
That same test had passed in the required bank and immediately preceding full
suite; it then passed an isolated 1/1 run in 1.532 seconds and an immediate
three-repeat run in 4.597 seconds. No timeout changed. The complete passing
selftest above was rerun from the beginning after the final setter audit.

One earlier full-suite rerun failed after 94.742 seconds when a forked legacy
writer received the existing typed one-second `ledger_lock_timeout`. Isolated
diagnostics reproduced the child payload intermittently while the same test
also passed repeatedly. The Task 6 active segmented-state cache is not reached
on that inactive legacy path. The failed run remains recorded and was not
relabeled; the final complete suite and selftest were fresh executions.

## Frozen publication proof

`/usr/bin/git diff --exit-code 4f1de17f5e68b6bc792e6716e593ca0e90f7ac68 -- bundle/c7.1 bundle/c7.2 schemas/v0`
returned no differences. Git tree identities at the exact base and candidate
parent are equal:

- `bundle/c7.1`: `fcd7b7c08ee2fe608b366fcf2c6e342d06f6aba9`;
- `bundle/c7.2`: `1e40504f6d3c1380c0524f8418cbcf207b788515`;
- `schemas/v0`: `79169fb02dcbfa6b64152515b07d04530d73741c`.

The sorted manifest now names every post-v1 runtime and v1 schema dependency,
and `verify_manifest(Path.cwd())` returned no errors. No frozen C7 or v0
publication byte was rewritten.

## Review boundary

The hostile fixture uses injected batched ledger writes for the million-record
bulk path. Separately, a bounded 201-record phase crosses the real Unix-socket
service with one continuously submitting noisy principal and 99 quiet peers,
measures their physical service order, closes one client after request send,
and resolves that unknown result through an exact socket retry. This is real
bounded service/fairness/response-loss testimony, not one million socket round
trips, live fleet throughput, or a production memory ceiling. Independent
exact-tip re-review remains required before push or checkpoint.

## Post-scale final-fix wave

The final whole-branch review at clean exact HEAD
`56655e2a1156c97120a04e5088890e20058a4584` found two blocking defects. This
containing commit fixes only those findings:

- launchd `StartCalendarInterval` now receives host-local calendar components
  for the requested instant, using the host zone at that instant by default
  and an injected `tzinfo` for deterministic non-UTC tests. The request,
  label, and callback `--wake-at` coordinate retain the canonical UTC
  timestamp, while the preview digest continues to bind the exact plist bytes;
- the sequencer's recursive I-JSON validator now rejects positive and negative
  numeric overflow decoded as non-finite floats with typed `frame_not_ijson`.
  A finite `1e308` control passes, while literal `NaN`, `Infinity`, and
  `-Infinity` and duplicate keys retain their existing typed refusals.

Strict RED was witnessed before production edits. The injected non-UTC wake
test ran one test and errored on the absent `calendar_timezone` seam. The
numeric framing test ran one test and failed two subtests because nested
`1e309` and `-1e309` raised no refusal; its finite control passed. Focused
GREEN then ran two wake controls in 0.016 seconds and two sequencer framing
controls in 0.016 seconds, all passing.

Fresh downstream evidence for this wave:

- wake, attempt, time, sequencer, and CLI regression set: 74 tests in 7.394
  seconds, all passed;
- Task 6 command-1 affected bank: 103 tests in 19.579 seconds, all passed;
- the first complete-suite run executed 765 tests in 63.221 seconds and failed
  only the manifest-current test, which reported the two intentionally stale
  deployable digests for `slip/sequencer.py` and `slip/wake.py`; it is retained
  as failed pre-refresh evidence;
- after refreshing only those two deployable entries, the complete suite ran
  765 tests in 61.469 seconds, all passed;
- selftest ran 765 tests in 62.426 seconds, all passed, then emitted
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
- standalone manifest verification returned `[]`;
- source/history scrub ran 8 tests in 0.281 seconds, all passed;
- the frozen publication diff exited 0 with no output; base/current tree IDs
  remain equal at `fcd7b7c08ee2fe608b366fcf2c6e342d06f6aba9` for `bundle/c7.1`,
  `1e40504f6d3c1380c0524f8418cbcf207b788515` for `bundle/c7.2`, and
  `79169fb02dcbfa6b64152515b07d04530d73741c` for `schemas/v0`;
- final `/usr/bin/git diff --check` exited 0 with no output.

The million-record gate was not rerun. Its 1,263.114-second result remains
bounded pre-setter managed-path evidence, not exact-tip execution. Live
launchd installation/activation remains an external unexecuted gate. The
review's three non-blocking Minors are explicitly deferred: v1 records using
v0 refusal wording, the correctness-preserving exclusive `project()` read
transaction, and the takeover test's missing simultaneous-arrival barrier.
No protocol, schema, durable record family, public help, activation, or
publication surface was broadened in this fix wave.

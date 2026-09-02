# FLOATI HM-3H gauntlet evidence

Status date: 2026-08-01.

This is the append-only evidence ledger for `HM3H_GAUNTLET_BRIEF.md`. A command
listed as RED, GREEN, passed, failed, or skipped has that exact meaning. Later
phases append below; this file does not manufacture evidence for work not yet
run.

## Boot

- Branch: `lane/hm0`.
- Freshly fetched local and `origin/lane/hm0` SHA:
  `6ee8582995d79bff4b37587eb4328ab82aa4196b`.
- `git merge-base --is-ancestor 6ee8582 origin/lane/hm0`: exit 0.
- Boot fleet poll: `intentional_silence`; no message and no delivery receipt.
- Untouched baseline:
  `PYTHONPYCACHEPREFIX=<temp>/hm3h-baseline python3 -m unittest discover -v`
  ran 324 tests in 17.752 seconds, exit 0, `OK`.

## A — Crash-point injection

The durable writer inventory resolves every record append and transaction to
`<retired>.jsonl._append_unlocked`. The crash test kills real forked children at:

- before, during, and after the single append write;
- before and after file `fsync`;
- before and after a new ledger's parent-directory `fsync`;
- before and after short-write rollback truncation; and
- before and after rollback `fsync`.

`PYTHONPYCACHEPREFIX=<temp>/hm3h-crash python3 -m unittest -v tests.test_gauntlet_crash`
ran 3 tests in 0.090 seconds, exit 0, `OK`.

Observed laws:

- mid-append and pre-truncate crashes retain a torn tail that every read and
  retry refuses as `incomplete_jsonl_line`; it is never silently dropped;
- pre-write and completed rollback crashes leave the prior complete ledger;
- post-write and file-fsync crashes leave one complete candidate frame;
- retrying a stable send idempotency key produces exactly one durable effect;
- a kill between work claim and worker receipt leaves exactly one claim, and a
  retry refuses as `worker_work_absent` rather than claiming the item twice.

## B — Concurrent-writer torture

### Finding and RED

`docs/FINDINGS.md` F.1 records the reproduced unbounded lock wait. Before the
fix, each focused test held the relevant lock in one process and joined the
contender for two seconds. Both failed because the contender was still alive:

```text
AssertionError: True is not false : ledger lock acquisition exceeded the two-second gauntlet bound
AssertionError: True is not false : CAS lock acquisition exceeded the two-second gauntlet bound
```

### GREEN

The shared ledger lock and the separate CAS lock now have a one-second
implementation deadline and stable refusal codes. Focused plus adjacent
verification:

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-cas-green python3 -m unittest -v \
  tests.test_gauntlet_concurrency tests.test_planes tests.test_process_atomicity
Ran 17 tests in 2.215s
OK
```

The 12-process hammer ran sends, registrations, acknowledgments, and work
claims against one root:

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-hammer python3 -m unittest -v \
  tests.test_gauntlet_concurrency.ConcurrentWriterGauntletTests.test_twelve_process_send_register_ack_and_claim_torture_has_no_double_effects
Ran 1 test in 1.091s
OK
```

It proved 120 unique registrations, 120 unique idempotent sends, one receipt
from 12 simultaneous identical acknowledgment attempts, and 120 unique claims
over 120 work items. No ledger corruption and no duplicate claim occurred.

### Filesystem coverage

`df` resolved the checkout to `/dev/disk3s5`. Host `diskutil info` reported
`File System Personality: APFS`. A temporary `CaseProbe` / `caseprobe` inode
probe produced one directory entry and one inode, proving the exercised APFS
volume is case-insensitive. The exact temporary probe file and directory were
removed after the readout. No second-volume skip is claimed or needed: the
available exercised volume satisfies both named properties.

### A/B checkpoint verification

`PYTHONPYCACHEPREFIX=<temp>/hm3h-ab-full python3 -m unittest discover -v`
ran 330 tests in 25.847 seconds, exit 0, `OK`. Direct manifest verification
returned `[]`; generated copy-ledger equality and `git diff --check` both
exited 0.

## C — Reader fuzz

F.2 and F.3 record the two reproduced defects. The hostile-text RED supplied
ANSI ESC plus U+202E to all seven named readers; all seven accepted it, replay
and board rendered terminal controls raw, and every reader emitted U+202E raw.
After shared validation hardening, the focused hostile test plus record
validation ran 9 tests in 0.551 seconds, `OK`.

The causal-reorder RED showed inbox/log accepting reply-before-original and
replay accepting transition-before-item; the other four readers already
refused the work reorder. After reader-specific causal validation, 39 focused
and adjacent tests ran in 3.615 seconds, `OK`.

The final matrix command was:

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-fuzz-full python3 -m unittest -v tests.test_gauntlet_fuzz
Ran 3 tests in 2.370s
OK
```

Those three tests contain 49 reader/mutation cases spanning malformed,
truncated, duplicated, causally reordered, exact 1 MiB hostile note, invalid
UTF-8, ANSI control, and bidi override inputs across inbox, log, replay, board,
graph, doctor, and status. Every case is typed; none crashes or renders hostile
bytes raw.

## D — Soak and performance budgets

The first harness launch stopped before fixture generation with
`ModuleNotFoundError: <retired>`; it is recorded as **not executed**, not a budget
result. Anchoring the standalone harness to its own repository path fixed only
the harness invocation. The executed command then exited 1 with
`status=budget_failed` as required:

| Reader | Budget | Samples (ms) | Median | Gate |
| --- | ---: | --- | ---: | --- |
| status | <150 | 1527.251, 1480.884, 1515.810 | 1515.810 | FAIL |
| inbox | <100 | 2571.022, 2646.223, 2974.129 | 2646.223 | FAIL |
| replay render start | <300 | 942.518, 948.167, 1038.098 | 948.167 | FAIL |
| board full redraw | <250 | 2884.598, 2885.054, 2721.854 | 2884.598 | FAIL |
| doctor | <2000 | 103.968, 108.678, 112.781 | 108.678 | PASS |

No tolerance changed. F.4 remains open and
`RULING-REQUEST-HM3H-SCALE-READ-PATH.md` files the required feature request.

## E — Time hostility

The RED run failed all three invariants: replay timestamp-sorted future/skew/DST
frames, status selected the numerically largest time, and the receipt ticker
timestamp-sorted its rows. The correction makes physical ordinal authoritative
under fixed source precedence and keeps replay elapsed time monotonic and
nonnegative.

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-time-green2 python3 -m unittest -v \
  tests.test_gauntlet_time tests.test_replay tests.test_supervisor tests.test_tui_render
Ran 26 tests in 0.329s
OK
```

## F — Recovery drills

The RED run failed all three drills: partial ENOSPC escaped raw and retained a
torn tail; chmod read-only escaped raw; deleted-root watch emitted an empty
fleet change and exited 0. F.6 records the shared cause and correction.

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-recovery-green2 python3 -m unittest -v \
  tests.test_gauntlet_recovery tests.test_root_jsonl tests.test_cli tests.test_watch
Ran 42 tests in 7.529s
OK
```

The disk-full drill proves prior bytes and one valid record remain after
rollback. The read-only drill proves no append. The live-watch drill deletes
only its temporary root after the initial frame, then observes degraded exit 35
with `root_deleted` and no traceback.

## Remaining phases

- C reader fuzz: passed after F.2/F.3 corrections.
- D 10k-item / 100k-event soak: **FAILED** four budgets; F.4 open.
- E time hostility: passed after F.5 correction.
- F recovery drills: passed after F.6 correction.
- G final findings/evidence/push: full-suite verification complete; checkpoint
  commit, the architect gate, push, and stand-down poll remain.

## G — Full-suite verification

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-final-full python3 -m unittest discover -v
Ran 339 tests in 32.239s
OK
```

This result includes the A–F gauntlet suites and the existing source-scrub,
manifest, copy-ledger, JSONL, reader, projection, CLI, watch, and worker
coverage. It does not convert the failed D budgets into a pass.

The repository gate was then run directly against the same working tree:

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-final-selftest python3 -m <retired>.selftest
Ran 339 tests in 26.827s
OK
{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}
```

## the architect gate — A/B checkpoint (2026-08-01)

Independent re-verification in a SHA-bound scratch checkout at
`5830ef9d60aaa1cde4dea89c15d682b8afa73362` (never the live worktree):

- Full suite: `python3 -m unittest discover` ran 330 tests in 21.324 s, `OK`
  (MEASURED, my run).
- Crash matrix + concurrency focused re-run (`tests.test_gauntlet_crash` +
  `tests.test_gauntlet_concurrency`, 12-process hammer included): 6 tests,
  `OK` (MEASURED).
- F.1 present in `docs/FINDINGS.md` (unbounded lock wait).
- Fix inspected at source: non-blocking `flock` with 1.0 s monotonic deadline
  (`LOCK_TIMEOUT_SECONDS`), 10 ms poll, raises a stable refusal on expiry —
  the bound is real, not a test artifact.
- `bundle-manifest.v0.json` sha256 sweep: zero mismatches (MEASURED).
- `git diff --check`: clean.

**VERDICT: PUSH GO at exact SHA 5830ef9.** Phases C–G remain open; this GO
covers the A/B checkpoint only. — the architect

## the architect gate — C–G checkpoint (2026-08-01)

Independent re-verification in a SHA-bound scratch checkout at
`f543beb` (full id in git):

- Full suite: 339 tests, `OK` (MEASURED, my run, 26.493 s).
- Focused gauntlet re-run (`test_gauntlet_fuzz` + `test_gauntlet_time` +
  `test_gauntlet_recovery`): 9 tests, `OK` (MEASURED).
- `python3 -m <retired>.selftest`: 339 OK + `bundle_verified` (MEASURED).
- F.2–F.6 all filed in `docs/FINDINGS.md`; D budget table preserved as FAIL
  with no tolerance change — the honest RED is exactly the required behavior.
- `RULING-REQUEST-HM3H-SCALE-READ-PATH.md` present; ruled this session (see
  RULING appended in that file).

**VERDICT: PUSH GO at exact SHA f543beb.** The D performance gate remains
FAILED and F.4 remains OPEN — this GO banks the honest evidence and the
C/E/F corrections; it does not convert D. Publication performance gate stays
closed until the authorized read-acceleration feature lands and D re-runs
green. — the architect

## Fix-round — anchored snapshot projection

Implementation checkpoint:
`6e3fe180cd27a0eedbb6b4c5ee267700aa14dec4`. This section is bound by its
containing evidence commit, which is the exact SHA submitted for the architect
re-verification; the implementation checkpoint identifies the source and test
state measured before this evidence-only append.

The fix implements the authorized version-zero derived snapshot per read path.
Every source prefix is anchored by byte offset and physical record ordinal,
with root, tenant, source-set, prefix digest, and record-ID fingerprints bound
into a checksummed envelope. Valid tails are decoded and record-validated in
append order. Writers do not import, inspect, wait on, refresh, or invalidate
snapshot state. Any snapshot doubt falls back to the original authoritative
full scan and a best-effort read-side rewrite.

### D rerun — MEASURED

The harness and budgets were unchanged: exact 10,000-work-item / 100,000-event
profiles, one warmup, three measured samples, median statistic.

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-fix-soak python3 scripts/hm3h-soak.py
```

| Reader | Budget | Samples (ms) | Median | Gate |
| --- | ---: | --- | ---: | --- |
| status | <150 | 97.840, 76.175, 76.209 | 76.209 | PASS |
| inbox | <100 | 33.279, 34.251, 35.348 | 34.251 | PASS |
| replay render start | <300 | 46.902, 46.851, 45.213 | 46.851 | PASS |
| board full redraw | <250 | 92.155, 92.634, 90.952 | 92.155 | PASS |
| doctor | <2000 | 116.261, 117.309, 115.690 | 116.261 | PASS |

The command exited 0 with `status=passed`. No budget, scale, warmup count,
sample count, or statistic changed.

### Hostile snapshot matrix — MEASURED

`tests.test_gauntlet_snapshot` permanently crosses all four accelerated readers
(status, inbox, replay render, and board) with seven derived-state attacks:

- torn snapshot bytes;
- stale anchor at a non-frame byte boundary;
- anchor pointing past ledger EOF;
- record ordinal mismatch;
- snapshot bound to a different root path;
- unsupported snapshot version; and
- hand-corrupted payload bytes with a stale checksum.

All 28 reader/mutation combinations returned the authoritative full-rescan
answer. Snapshot deletion remains lossless by construction: a missing snapshot
uses the same typed fallback path.

An additional boundary case replaces the tenant-local snapshot directory with
a symlink to another path. Status still returns the authoritative answer and
writes no file through the symlink. The envelope race test also appends to a
source between the pre-scan capture and refresh; refresh refuses with
`snapshot_source_changed` and persists no stale projection.

The permanent hostile matrix was then run with the existing crash, fuzz, time,
and recovery gauntlets:

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-fix-gauntlets python3 -m unittest -v \
  tests.test_gauntlet_snapshot tests.test_gauntlet_crash \
  tests.test_gauntlet_fuzz tests.test_gauntlet_time \
  tests.test_gauntlet_recovery
Ran 14 tests in 5.632s
OK
```

This command includes valid warmed snapshots, injected hostile snapshots,
writer crash seams, all seven malformed-reader surfaces, ordinal-over-time
hostility, and recovery failures. A snapshot never converted malformed ledger
evidence into an answer.

### Full-suite verification — MEASURED

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-fix-full python3 -m unittest discover -v
Ran 354 tests in 28.795s
OK
```

The 354 tests include the new envelope, inbox, status, replay, board, and
hostile-snapshot suites plus every pre-existing contract and gauntlet. Local D
and the publication performance gate now pass. F.4 awaits the architect's independent
exact-SHA re-verification; this local evidence does not manufacture that
external verdict.

The governed repository gate was then run against the same implementation and
evidence worktree:

```text
PYTHONPYCACHEPREFIX=<temp>/hm3h-fix-selftest3 python3 -m <retired>.selftest
Ran 354 tests in 30.241s
OK
{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}
```

## the architect gate — fix-round (2026-08-01)

Independent re-verification in a SHA-bound scratch checkout at
`3ed8f846180038405f63a736f7b90cad23c9934f`:

- `python3 -m <retired>.selftest`: 354 OK + `bundle_verified` (MEASURED, my run).
- All five gauntlet suites (snapshot + crash + fuzz + time + recovery):
  14 tests, `OK` (MEASURED).
- **D soak re-measured by me, same harness, budgets unchanged** — all five
  PASS on my machine (load avg 4.77 at run time, i.e. NOT quiet — passed
  anyway with wide margin): status 58.807 ms (<150) · inbox 38.761 ms
  (<100) · replay render start 55.223 ms (<300) · board full redraw
  94.955 ms (<250) · doctor 146.446 ms (<2000). Medians, one warmup, three
  samples. (MEASURED)
- Ruling-term audit at source: `<retired>/snapshot.py` imported ONLY by read-side
  modules (events, projection, replay, tui); the writer module's two
  "snapshot" hits are a pre-existing read helper and comment — writer
  isolation holds. Hostile matrix (28 reader/mutation combinations incl.
  cross-root, symlink, version-skew, torn bytes) returns authoritative
  full-rescan answers per my gauntlet re-run.

**VERDICT: PUSH GO at exact SHA 3ed8f84. F.4 is CLOSED on this evidence;
the publication performance gate is no longer held open by HM-3H.** The
Gauntlet (A–G + fix-round) is complete. Lane stands down for re-charter per
the succession ruling. — the architect

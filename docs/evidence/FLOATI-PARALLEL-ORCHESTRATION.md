# FLOATI parallel orchestration evidence

Status date: 2026-07-31. Branch: `lane/hm0`. Baseline before this working
checkpoint: `f74bbe0`. The exact review tip is the containing commit and must
be derived with `git rev-parse HEAD` after commit.

This document separates deterministic local evidence, installed-provider
evidence, and external release gates. It does not turn local execution into
hosted CI, deployment, activation, owner-use, or a Fable verdict.

## A — concurrent DAG consumption

The one consumption coordinate remains `work/items.jsonl`. `work_item.needs`
contains only bounded earlier work IDs. Projection rejects unknown/future
edges, and blocked work cannot be claimed. A four-process contention test over
two ready items produced exactly two unique claims and two refusals with no
double consumption.

The installed Codex proof used N=3 distinct controllers over M=5 items. A, B,
and C reached durable DRIVE at 02:37:07.889Z, .890Z, and .928Z; the first did
not complete until 02:37:41.546Z, establishing real three-worker overlap.
D claimed only after A+B completed; E claimed only after C+D completed. All
five items ended `done` with complete five-receipt chains.

## B — distinguishable degradation and cleanup

The deterministic drill acted only after the exact durable DRIVE boundary:

- controller termination: `process_cancelled`;
- explicit expiry of the exact authority epoch:
  `authority_expired_mid_claim`;
- a causatively signaled non-returning descendant: `process_timeout`.

All actions report `triggered=true`; the artifact is `degraded`, RC 35. The
board and receipt ticker show distinct labels. Independent `kill -0` checks
found no controller, adapter, or registered grandchild. A zero-delay worker
regression proves a requested drill cannot race past DRIVE. Drain also requires
one clean process audit per completed item and an empty aggregate survivor set.

Raw drill evidence: `docs/evidence/captures/floati-orchestrate-drill.txt`.

## C — receipt-derived board

The Harbor Board renders DAG states `BLOCKED`, `READY`, `CLAIMED`, and `DONE`,
dependency edges, exact worker work IDs, terminal outcomes, and a receipt
ticker. Its state signature excludes the observation clock, so unchanged
frames are suppressed. The maximum scheduling interval is 250 ms.
`--no-animation` emits distinguishable plain frames; interactive stderr uses
bounded terminal frames. Copy is registered once and mechanically equal to
`docs/COPY-LEDGER.md`.

The copy ledger remains `PROVISIONAL — FABLE VOICE PASS PENDING`. Local tests
prove registration/equality, not Fable voice approval.

## D — one-command orchestration

`slip orchestrate --root R --plan P --adapter codex --deadline S
[--no-animation]` preflights registrations and exact live authority, seeds the
DAG, launches one controller per worker, streams frames on stderr, and emits
one JSON artifact on stdout. Final states are `drained`/0, `deadline`/34, and
`degraded`/35. Completed work without its terminal receipt, any nonzero
controller exit, incomplete process-audit coverage, a survivor, or an
untriggered requested drill cannot report drained.

The first installed proof attempt ran inside the managed filesystem sandbox.
All three children honestly degraded `process_died`; private stderr showed the
same cause for each: the sandbox prevented initialization of the normal SQLite
state under `~/.codex`. No provider turn completed, and no process survived.
The exact governed command was then rerun with approved normal app-server
access; that run completed five live Codex turns in 92.442069 seconds with RC
0. The five retained repositories were clean, each contained exactly its one
requested `FLOATI X\n` file, and each binding matched `git rev-parse HEAD`.
All 13 recorded controller, adapter, and app-server PIDs were independently
absent after completion.

Raw live evidence: `docs/evidence/captures/floati-orchestrate-live.txt`.
Private transcript content remains outside the bus; the capture records only
the ruled workspace pointers.

## E — verification and release boundary

Executed before this evidence edit:

```text
python3 -m unittest discover
Ran 258 tests in 20.194s — OK

git diff --check
clean
```

An independent implementation review first found a no-op hang drill and a
false-drain receipt gap; RED tests and fixes closed both. Its second pass found
survivor audits absent from the drain predicate and a fast-worker DRIVE race;
both received reproductions, drive-gate/audit fixes, and a third review that
reported no actionable findings. Focused orchestration, worker, process,
board, CLI, schema, and copy tests then passed 104/104.

The post-evidence gate then observed 258/258 tests passing; selftest emitted
`{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
live-root conformance emitted `{"cases":5,"status":"conformant"}`; source
scrub emitted no findings; direct manifest verification printed `[]`; and
`git diff --check` was clean. The same gates will be rerun after any Fable
verdict commit.

Fable voice review, exact-tip `PUSH GO`, push, local/origin equality, and
hosted CI are pending and must not be inferred from the local or live-provider
evidence above.

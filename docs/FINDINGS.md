# HM-3H gauntlet findings

This corpus numbers only reproduced gauntlet findings. A phase with no finding
is recorded in `docs/evidence/HM3H-GAUNTLET.md`; absence of a numbered row is
not presented as evidence that a phase ran.

## F.1 — Ledger and CAS lock contention was unbounded

- **Phase:** B — concurrent-writer torture
- **Status:** CLOSED — pushed after architect `PUSH GO` at exact checkpoint
  `5830ef9d60aaa1cde4dea89c15d682b8afa73362`
- **Severity:** hardening gate failure
- **Reproduction:** a separate process held `events.jsonl.lock` or
  `authority-grants/work-claims.jsonl.cas.lock`; a contender remained blocked
  beyond the gauntlet's two-second test ceiling and emitted no typed result.
- **Evidence:** both focused RED tests failed with `contender.is_alive()` after
  two seconds. The child was terminated by the test cleanup after attribution.
- **Root cause:** both lock contexts called blocking `flock` without
  `LOCK_NB`, a condition poll, or a deadline.
- **Resolution:** both contexts now poll nonblocking acquisition for at most
  one second. Ledger contention refuses as `ledger_lock_timeout`; outer CAS
  contention refuses as `cas_lock_timeout`.
- **Verification:** the two focused contention tests and the pre-existing
  JSONL, plane, and four-process atomicity suites pass. The 12-process hammer
  also passes with zero double effects.

## F.2 — Durable free-text fields admitted terminal controls and bidi overrides

- **Phase:** C — reader fuzz
- **Status:** CLOSED — held on `origin/main` by `tests/test_gauntlet_fuzz.py` `test_hostile_control_and_bidi_strings_are_typed_before_every_reader_renders`; the "push gate pending" line stood for a month after the tests landed and was struck by `docs/rulings/2026-09-02-gate1-two-release-gates-discharged-late.md` §3.
- **Severity:** terminal integrity
- **Reproduction:** valid framed note, title, role, and claimed-identity fields
  containing ANSI ESC plus U+202E were supplied to inbox, log, replay, board,
  graph, doctor, and status. All seven accepted the evidence; replay and board
  rendered terminal controls raw, and every surface emitted U+202E raw.
- **Root cause:** shared durable-record validators bounded free text only by
  length.
- **Resolution:** shared validation now rejects C0/C1 control characters and
  Unicode bidi-control classes before any reader projection or render.
- **Verification:** all seven readers return typed malformed evidence with no
  traceback and neither hostile byte sequence in either output stream.

## F.3 — Three readers accepted causally reordered evidence

- **Phase:** C — reader fuzz
- **Status:** CLOSED — held on `origin/main` by `tests/test_gauntlet_fuzz.py` `test_causally_reordered_mail_and_work_records_are_typed_by_every_reader`; "pending" struck by `docs/rulings/2026-09-02-gate1-two-release-gates-discharged-late.md` §3.
- **Severity:** durable-order integrity
- **Reproduction:** inbox and log accepted a reply frame placed before its
  referenced original; replay accepted a work transition placed before its
  work item. Board, graph, doctor, and status already refused the work reorder.
- **Root cause:** mail reads validated individual frames but not reply causality;
  replay normalized work frames without running the consumption projector.
- **Resolution:** the shared event reader validates prior-original and reversed
  parties in physical ledger order; replay validates the work projection before
  normalization.
- **Verification:** all seven named readers return typed malformed evidence for
  their causally invalid reorder fixture.

## F.4 — Four published 10k-item / 100k-event budgets are exceeded

- **Phase:** D — soak and performance budgets
- **Status:** CLOSED — architect PUSH GO at exact SHA `3ed8f846` (`docs/evidence/HM3H-GAUNTLET.md` "the architect gate — fix-round", all five budgets re-measured under budget); this line was never updated to say so and was reconciled by `docs/rulings/2026-09-02-gate1-two-release-gates-discharged-late.md` §3. The hostile-snapshot matrix lives in `tests/test_gauntlet_snapshot.py`.
- **Severity:** publication performance gate
- **Reproduction:** exact-scale mail and replay profiles, one warmup and three
  measured samples, median statistic.
- **Measured medians:** status 1515.810ms (budget <150ms); inbox 2646.223ms
  (budget <100ms); replay render start 948.167ms (budget <300ms); board full
  redraw 2884.598ms (budget <250ms); doctor 108.678ms (budget <2000ms, PASS).
- **Root cause:** status, inbox, replay, and board synchronously decode,
  validate, project, and in some cases render their full allowlisted ledgers on
  each read. The current layer defines no governed index or materialized read
  coordinate.
- **Resolution:** The architect authorized a derived anchored snapshot per read path,
  bound to each ledger prefix by byte offset, physical record ordinal, prefix
  digest, root, and tenant. Writers remain snapshot-free. Snapshot parse,
  identity, version, anchor, source-set, or payload doubt is a typed internal
  refusal followed by the original authoritative full scan.
- **Fix-round measurement:** the unchanged exact-scale harness passed all five
  budgets: status 76.209ms (<150ms), inbox 34.251ms (<100ms), replay render
  start 46.851ms (<300ms), board full redraw 92.155ms (<250ms), and doctor
  116.261ms (<2000ms). Each result is the median of three samples after one
  warmup.
- **Verification:** the permanent hostile-snapshot matrix covers 28
  reader/mutation combinations plus a symlinked-path boundary case; the named
  snapshot/crash/fuzz/time/recovery command passed 14 tests and the full suite
  passed 354 tests. Evidence is in
  `docs/evidence/HM3H-GAUNTLET.md` under “Fix-round.” F.4 is not marked
  architect-closed until that evidence is independently re-verified at its exact
  containing commit.

## F.5 — Replay, status, and receipt ticker trusted timestamps over ordinals

- **Phase:** E — time hostility
- **Status:** CLOSED — held on `origin/main` by `tests/test_gauntlet_time.py` (`…source_ordinal_beats_skew…`, `…latest_append_not_largest_timestamp`, `…reverse_append_ordinal_not_timestamp`); "pending" struck by `docs/rulings/2026-09-02-gate1-two-release-gates-discharged-late.md` §3.
- **Severity:** ordering integrity
- **Reproduction:** future, backward-skewed, and UTC records spanning the
  2026-11-01 DST boundary caused replay to reorder frames, status to choose the
  numerically largest time as last activity, and the ticker to choose display
  order by time.
- **Root cause:** explicit timestamp sort/max operations in those projections.
- **Resolution:** replay uses fixed source precedence then physical source
  ordinal; status uses the last observed append under fixed source precedence;
  the ticker uses reverse append ordinal per fixed receipt source. Replay
  elapsed time is clamped monotonic and nonnegative.
- **Verification:** three hostility tests and 23 adjacent replay, supervisor,
  and TUI tests pass.

## F.6 — Recovery failures escaped raw or erased live root state silently

- **Phase:** F — recovery drills
- **Status:** CLOSED — held on `origin/main` by `tests/test_gauntlet_recovery.py` (`…disk_full…rolls_back…`, `…read_only_root…typed…`, `…deleted_root…exits_typed…`); "pending" struck by `docs/rulings/2026-09-02-gate1-two-release-gates-discharged-late.md` §3.
- **Severity:** durability integrity
- **Reproduction:** injected ENOSPC after a partial write escaped as `OSError`
  and left a torn tail; a chmod read-only root escaped as `PermissionError`; a
  root deleted after watch's initial frame reprojected as an empty fleet and
  exited 0.
- **Root cause:** filesystem exceptions had no protocol error family, partial
  exception writes were not rolled back, and missing root directories were
  treated like absent optional ledgers.
- **Resolution:** `DurabilityFailure` maps disk full, read-only root, deleted
  root, short write, and unknown storage failure distinctly. Write/fsync errors
  attempt prior-length rollback; every ledger resolution confirms the selected
  root still exists. The CLI reports durability failures as degraded exit 35.
- **Verification:** all three drills and 39 adjacent JSONL, CLI, and watch tests
  pass; the deleted root test removes only its temporary fixture.

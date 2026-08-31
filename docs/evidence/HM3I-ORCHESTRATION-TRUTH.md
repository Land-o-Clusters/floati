# HM-3I orchestration truth evidence

## Task 1 — charter mirror and scope contract

- Branch: `codex/hm3i-orchestration-truth`
- Base SHA: `b10d4b05a5482fbef95a26546d3610c241833ca3`
- the architect message ID: **UNOBSERVED**. No fleet message was inspected or sent for
  this task; the Fable-filed charter authority is Puddle
  `HARBOR_MASTER.md` at `a111202b228d34c2b371bcc5e2c4798206474439`.
- RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item1-red python3 -m unittest -v tests.test_hm3i_contract`
- Honest RED result: exit 1; the sole test failed as intended because
  `docs/DESIGN.md` did not contain `bounded local run graph` before this
  charter mirror was added.

The binding scope mirror is in `docs/DESIGN.md` and `docs/SPEC-DRAFT.md`.
This evidence file is append-only for later HM-3I checkpoints.

## Task 1 — local GREEN and static check

- GREEN command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item1-green python3 -m unittest -v tests.test_hm3i_contract tests.test_phase1_contract`
- Observed result: exit 0; 6 tests ran and all passed.
- Static command: `git diff --check`
- Observed result: exit 0; no whitespace errors reported.

An initial GREEN invocation failed because the test-required charter phrase was
split by a newline in the mirror. The phrase was made contiguous in both
documents and the recorded GREEN invocation above is the subsequent passing
result.

## Task 1 — controller-authorized authority finalization

This append records authority observations that were unavailable when the
initial Task 1 evidence was written; it does not rewrite that prior honest
state.

- the architect authority message: `msg-019fc085367479e2903af0cd63ceb9e1`
- the architect acknowledgment: `ack-019fc0860f7e711e8c4d29e5db4f0877`
- Charter SHA: `a111202b228d34c2b371bcc5e2c4798206474439`
- Task 1 implementation commit:
  `9095853f4becabce77531968ab128cc6b2ccd1e2`
- Independent review: **PASS**; no findings.
- Planning correction: `6471dcc`

No push, Item 1 checkpoint, or the architect Item 1 gate is claimed by this evidence
append.

## the architect gate — item 1 (2026-08-02)

Scratch checkout at `ac45cee`: charter mirror verified in SPEC-DRAFT +
DESIGN citing puddle authority `a111202` with the bounded-local-run-graph
scope and product-boundary sentence; full suite 355 tests `OK` (MEASURED,
my run). **Item 1 GATE GREEN at ac45cee.** Proceed item 2. — the architect

## Task 2 — canonical run truth

- RED command: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item2-red python3 -m
  unittest -v tests.test_runtruth tests.test_record_validation tests.test_schemas
  tests.test_graph`
- Honest RED result: exit 1; 11 new run-truth tests failed because
  `slip.runtruth` did not yet exist. The pre-existing 26 tests passed.
- GREEN command: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item2-green python3
  -m unittest -v tests.test_runtruth tests.test_record_validation
  tests.test_schemas tests.test_graph tests.test_root_jsonl
  tests.test_process_atomicity`
- GREEN result: exit 0; 67 tests passed. This includes malformed/truncated/
  oversize ledger fuzz refusal and idempotent run-created retry coverage.
- Static result: `git diff --check` and Python 3.9 compilation passed with
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item2-static`.
- Manifest: regenerated from `slip.manifest._deployable_paths()`; direct
  `verify_manifest(Path("."))` returned `[]`.

Task 2 persists sorted canonical run dependencies (defaulting omitted
`requires` to `accepted`), keeps worker receipts raw and external, and makes
causal validation plus append one `jsonl.transact()` operation. Harbor Graph
uses canonical run edges first and labels legacy work dependencies `accepted`.

## Task 2 — final local verification stamp

- Fix round 2 commit: `84bc5d2ab4e113163ed5a71fe0ec54b00fcce500`.
- Independent scoped re-review: **ACCEPTED**. The review confirmed both the
  legacy-only graph regression (bare `needs` renders `requires: "accepted"`)
  and exact tenant-schema parity with the root validator.
- Controller exact-tip covering verification: `56 tests OK` (measured by the
  controller at the exact tip), including the Task 2 focused/static, crash,
  fuzz, process-atomicity, graph, schema, and manifest surfaces.
- Controller exact-tip full verification: `python3 -m slip.selftest` reported
  `375 tests OK` with `bundle_verified` (measured by the controller at the
  exact tip).
- Static verification: `git diff --check` clean.

This is a local evidence stamp only: no push and no the architect Item 2 gate are
claimed here.

## the architect gate — item 2 (2026-08-02)

Scratch checkout at `5cec0d4`: `slip.selftest` 375 OK + bundle_verified;
crash + fuzz gauntlets re-run over the new run-truth families, 8 OK (all
MEASURED, my runs). **Item 2 GATE GREEN at 5cec0d4.** Item 3 unfenced. — the architect

## Task 3 — first-class attempts and scheduler-owned retry

- RED command: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item3 python3 -m
  unittest -v tests.test_attempts tests.test_orchestrate tests.test_runtruth
  tests.test_record_validation tests.test_schemas`
- Honest RED result: exit 1. New attempt tests raised
  `ModuleNotFoundError: No module named 'slip.scheduler'`; direct attempt
  candidates were still `record_kind_invalid` rather than scheduler-only; and
  all five durable attempt/retry schemas were absent. These failures were the
  intended missing Task 3 behavior, not a passing baseline claim.
- GREEN command: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item3 python3 -m
  unittest -v tests.test_attempts tests.test_orchestrate tests.test_runtruth
  tests.test_record_validation tests.test_schemas tests.test_gauntlet_crash
  tests.test_gauntlet_fuzz`
- GREEN result: exit 0; 68 tests passed. This includes physical-order attempt
  projection, scheduler-only appends, literal SHA-256 jitter values, retry
  reservation consumption, forbidden retry table, terminal-before-closure
  crash/restart reconciliation, and malformed attempt-family frames.

Task 3 adds only the bounded local scheduler authority. It retains one
`runs/events.jsonl` ledger and reserves retry closure data in its terminal
frame before reconciliation appends the corresponding retry frame. No push or
the architect gate is claimed by this local evidence append.

## Task 3 — sealed scheduler authority and controller verification

This append preserves the earlier measured implementation result and records
the independent review/fix cycle without rewriting the first evidence stamp.

- Initial implementation commit: `a63838ab3f4a56f341ed49fbf938447db06b602f`.
- Independent review: **FAIL** at the initial commit. The scheduler append
  path accepted direct callers without an opaque authority capability.
- Fix commit: `029b986`. The scheduler now owns an unforgeable, per-ledger
  capability required for every attempt/retry append.
- Scoped independent re-review: **APPROVE**. The authority-bypass finding was
  addressed with no new Critical or Important breakage.
- Controller covering verification at `029b986`: 82 tests passed, including
  attempt, orchestration, run-truth, record/schema, crash, fuzz,
  process-atomicity, and manifest banks.
- Controller full verification at `029b986`: 386 tests passed and
  `bundle_verified` was emitted.
- Static verification at `029b986`: `git diff --check` and Python compilation
  of `slip/records.py`, `slip/runtruth.py`, and `slip/scheduler.py` passed.

These are MEASURED local results. This append does not claim a push or a the architect
Item 3 gate.

## the architect gate — item 3 (2026-08-06)

Scratch checkout at `c68e4ae`: `slip.selftest` 386 OK + bundle_verified;
attempts + run-truth focused suites 21 OK (MEASURED, my runs). Attempt
records, scheduler-owned retry, policy classes, and forbidden-auto-retry
fences verified present in the evidence doc's contract table. **Item 3
GATE GREEN at c68e4ae.** Item 4 (durable cancellation) unfenced — Terra
architecture contract already banked; RED next. — the architect

## Task 4 — durable cancellation and late-result fencing

- RED command: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item4-red python3 -m
  unittest -v tests.test_cancellation`
- Honest RED result: exit 1; 2 tests failed as intended because
  `slip.cancellation` did not yet provide `CancelMode` or
  `CancellationCoordinator`.
- GREEN command: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item4-green python3
  -m unittest -v tests.test_cancellation tests.test_attempts
  tests.test_workers tests.test_orchestrate tests.test_runtruth
  tests.test_schemas`
- GREEN result: exit 0; 82 tests passed. The controlled cancellation tests
  verify scope resolution is durable before native/local process actions and
  preserve `cancel_unconfirmed` for unavailable adapters. The fence tests
  retain superseded raw receipts, refuse canonical advancement, and require a
  coordinator-authored operator adoption record naming the current fence.
- Additional schema/worker/manifest command: `PYTHONPYCACHEPREFIX=<temp>/slipway-
  hm3i-item4-green-b python3 -m unittest -v tests.test_cancellation
  tests.test_schemas tests.test_workers`; exit 0; 48 tests passed. Direct
  `verify_manifest(Path("."))` returned `[]`; `git diff --check` returned 0.

Task 4 adds only durable cancellation, stale-evidence fencing, strict schemas,
and the FOC-required explicit harness-session and typed Floati orphaning joins.
No push or the architect Item 4 gate is claimed by this local evidence append.

## Task 4 — review and controller verification

- Initial implementation commit:
  `34c00d82b7c39c3f7bdbf1c61b7045de6b7f1371`.
- Independent task review: **0 Critical, 3 Important, 1 Minor**; the task
  initially needed fixes. The Minor remains deferred: no real
  native-process/session adapter integration test.
- Fix commit: `f6bee387cec91260e4082063f82f119bd9b0b0a3`.
- Scoped re-review: all three Important findings were **ADDRESSED**, with no
  new Critical or Important breakage. The addressed findings were
  operator-authority adoption, cancellation-exception durable unconfirmed,
  and supervisor-owned typed orphaning.
- The controller's first focused run at `f6bee387` exited 0 with 89 tests
  `OK`, but a child lock/termination traceback made that output non-pristine;
  it is not used as the clean proof.
- Controller isolated rerun:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item4-controller-rerun python3 -m unittest tests.test_cancellation tests.test_attempts tests.test_workers tests.test_orchestrate tests.test_runtruth tests.test_schemas tests.test_approvals`
  exited 0 with 89 tests and clean `OK` output.
- Controller compile, manifest, and diff check exited 0; manifest output was
  `[]`. At that time the worktree was clean and `lane/hm0` was ahead of origin
  by two commits.
- Controller full selftest:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item4-controller-selftest python3 -m slip.selftest`
  exited 10 after 392 tests, with exactly one failure:
  `test_generated_repository_artifacts_are_scrubbed`, because `HM3I_BRIEF.md`
  was flagged. The same pre-existing condition existed at the pulled baseline
  (386 tests, one failure), so the full selftest is **NOT green**.

This is an evidence/controller-verification stamp only. No push, bus
checkpoint, or the architect Item 4 gate is claimed.

## the architect gate — item 4 (2026-08-08)

Scratch checkout at `05023f0`: cancellation-family focused controller
suite consistent with the lane's 89 OK; full selftest reproduced the
lane's honest report exactly — 392 tests, ONE failure, and that failure
was MINE: Addendum 2 of HM3I_BRIEF.md carried the forbidden upstream
project name into scrub scope. Corrected the brief wording in place;
full selftest re-run in scratch WITH the fix: **392 OK** (MEASURED).
Durable cancellation lifecycle, physical-order closure, late-evidence
fencing, and Floati-owned orphaning receipts accepted.
**VERDICT: item 4 GATE GREEN at 05023f0 + brief fix. The blemish is
charged to the architect, not the lane** — and the scrub caught an architect
doc, which is the scrub working. Item 5 unfenced. — the architect

## Task 5 — typed logical outcomes and per-edge failure policy

- Base SHA: `de69c8c1f8c5b6b30661bad937b37c24e2812a7e` on `lane/hm0`.
- RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item5-red python3 -m unittest -v tests.test_outcomes`
- Honest RED result: exit 1; all three new outcome tests errored at
  `dependency_edges_invalid` because the durable edge contract did not yet
  accept `failure_policy`.
- Additional RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item5-red-retry python3 -m unittest -v tests.test_outcomes.LogicalOutcomeTests.test_scheduled_retry_remains_uncertain_until_its_reserved_attempt_opens`
- Honest RED result: exit 1; the new retry regression observed
  `{'...': 'failed'}` where the hand-derived logical outcome was
  `{'...': 'uncertain'}`.
- GREEN command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item5-green python3 -m unittest -v tests.test_outcomes tests.test_runtruth tests.test_graph tests.test_orchestrate`
- GREEN result: exit 0; 38 tests passed.
- Static command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item5-compile python3 -m py_compile slip/runtruth.py slip/records.py slip/graph.py tests/test_outcomes.py && git diff --check`
- Static result: exit 0.
- Manifest: regenerated from `slip.manifest._deployable_paths()`; direct
  `verify_manifest(Path("."))` returned `[]`.

`RunProjection` now derives item and run outcomes solely from canonical run
frames in physical append order. Its item vocabulary is exactly
`succeeded`, `failed`, `cancelled`, `skipped`, `needs_operator`, and
`uncertain`; run projection additionally emits `partially_succeeded`.
Unopened items and scheduled retries remain `uncertain`; unknown-effect
attempts are never collapsed. Immutable dependency edges expose closed
`failure_policy` values `fail_run`, `skip_dependent`, and `continue`, with
omission physically canonicalized to `fail_run`. Harbor Graph renders both
`requires` and this policy without examining process state or worker health.

No push, bus checkpoint, or the architect Item 5 gate is claimed by this local
evidence append.

## Task 5 — fix round 1: no-success partial-outcome fence

- RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item5-fix1-red python3 -m unittest -v tests.test_outcomes.LogicalOutcomeTests.test_cancelled_source_with_skipped_dependent_remains_cancelled_not_partial`
- Honest RED result: exit 1; 1 test failed because the canonical
  `{cancelled, skipped}` set fell through to `partially_succeeded`.
- GREEN outcome command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item5-fix1-green-outcomes python3 -m unittest -v tests.test_outcomes`
- GREEN outcome result: exit 0; 5 tests passed.
- Covering command after manifest regeneration:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item5-fix1-final python3 -m unittest -v tests.test_outcomes tests.test_runtruth tests.test_graph tests.test_orchestrate tests.test_schemas tests.test_manifest`
- Covering result: exit 0; 64 tests passed. Compile and `git diff --check`
  exited 0; direct manifest verification returned `[]`.

The reducer now returns `cancelled` when cancellation and dependent skips are
the complete terminal set. `partially_succeeded` still requires at least one
`succeeded` item; pure `skipped` remains `skipped`. Existing precedence stays
unchanged: `uncertain`, then `fail_run`/`failed`, then `needs_operator`.

## Task 6 — immutable task contracts and acceptance provenance

- Content base assigned by the controller:
  `4f1443cb2ee6c60aab050dac79a8920622e9dd9a`; this checkout's read-only Git
  metadata reported `de69c8c1f8c5b6b30661bad937b37c24e2812a7e` and was not
  used to infer source content.
- RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-red python3 -m unittest -v
  tests.test_contracts tests.test_runtruth tests.test_record_validation
  tests.test_schemas`
- Honest RED result: exit 1; 43 tests ran with 9 expected schema/provenance
  failures and 3 expected missing-feature errors. `slip.contracts` was absent;
  result acceptance had neither a receipt field nor receipt lookup; and the
  three provenance schemas were absent.
- GREEN command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-green python3 -m unittest -v
  tests.test_contracts tests.test_runtruth tests.test_record_validation
  tests.test_schemas`
- GREEN result: exit 0; 45 tests passed. The focused suite includes a
  hand-derived literal compact-I-JSON SHA-256 contract digest, append-only
  prior-digest amendment refusal, semantic-score refusal, and receipt-bound
  verified acceptance. `accepted_unverified` remains the explicit no-receipt
  no-verifier path.
- Static command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-compile python3 -m py_compile
  slip/contracts.py slip/records.py slip/runtruth.py && git diff --check`;
  exit 0. Manifest regeneration was followed by direct
  `verify_manifest(Path("."))`, which returned `[]`.

Task contracts canonicalize objective, non-goals, exact file/region avoid
areas, input hashes, named acceptance checks, constraints, risk class, retry
policy, and dependencies before digesting. Plan amendments name their prior
digest and exact replacements without rewriting historic contracts. Durable
acceptance receipts bind the run/item/attempt, contract digest, named checks,
reviewer, worker-receipt evidence, deviations, and result. No semantic score
is an acceptance authority. This local evidence entry claims no commit, push,
bus checkpoint, or the architect gate.

## Task 6 — fix round 1/5 durable provenance repair

- RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix1-red python3 -m unittest
  -v tests.test_runtruth tests.test_contracts tests.test_record_validation
  tests.test_schemas`
- Honest RED result: exit 1; 47 tests ran with 3 expected errors. Root task
  contracts with empty dependencies were refused; task_contract and
  plan_amendment were not durable `runs/events.jsonl` transitions; and the
  projector could not return a current per-item contract.
- GREEN command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix1-green-final python3 -m
  unittest -v tests.test_contracts tests.test_runtruth
  tests.test_record_validation tests.test_schemas tests.test_workers
  tests.test_manifest`
- GREEN result: exit 0; 84 tests passed. Python compilation of contracts,
  records, and run truth plus `git diff --check` also exited 0.

`task_contract` and `plan_amendment` now bind an exact run and item and live in
the canonical run ledger. Physical replay recomputes the initial digest from
the governed compact-I-JSON payload, retains append-only per-item history, and
refuses stale, cross-item, unknown, empty, or digest-mismatched amendments.
Receipt validation uses the current item contract (not `run_created`'s plan
digest), declared check IDs, and raw worker evidence matching the dispatched
worker. This evidence remains local only; no commit, push, checkpoint, or gate
is claimed.

## Task 6 — fix round 2/5 contract causality and typed nested refusal

- RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix2-red python3 -m unittest
  -v tests.test_runtruth tests.test_record_validation tests.test_contracts
  tests.test_schemas`
- Honest RED result: exit 1; 51 tests ran with 2 expected failures and 1
  expected error. It showed a post-attempt amendment was accepted, an attempt
  opened without a contract, and `non_goals=[[]]` escaped as raw `TypeError`.
- GREEN command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix2-green python3 -m unittest
  -v tests.test_contracts tests.test_runtruth tests.test_record_validation
  tests.test_schemas tests.test_workers tests.test_manifest`
- GREEN result: exit 0; 87 tests passed. Compilation of contracts, records,
  and run truth plus `git diff --check` exited 0.

Task contracts now freeze before the first item attempt opens. Attempts,
dispatches, starts, produced/verified/accepted results—including
`accepted_unverified`—require a bound contract. Nested task-contract and
receipt collection validation validates elements before uniqueness/sorting so
unhashable malformed values remain typed refusals. A separate probe of legacy
`tests.test_attempts tests.test_orchestrate` exited 1 with 15
`task_contract_missing` errors in pre-Item-6 attempt fixtures that do not
append task-contract frames; that suite is explicitly **not green** and is not
counted as verification. No commit, push, checkpoint, or gate is claimed.

## Task 6 — fix round 3/5 integration closure and retry authority

- RED: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix3-red python3 -m
  unittest -v tests.test_attempts` exited 1 after 8 tests with the expected
  frozen-contract retry-policy mismatch failure. A second RED,
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix3-red-enum python3 -m
  unittest -v tests.test_attempts.AttemptLifecycleTests.test_retry_policy_unhashable_strategy_is_a_typed_refusal`,
  exited 1 after 1 test with the expected raw `TypeError`.
- GREEN: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix3-green-final2
  python3 -m unittest -v tests.test_contracts tests.test_runtruth
  tests.test_record_validation tests.test_schemas tests.test_attempts
  tests.test_cancellation tests.test_outcomes tests.test_orchestrate
  tests.test_gauntlet_crash tests.test_workers tests.test_manifest` exited 0;
  125 tests passed. Compilation and `git diff --check` exited 0;
  `verify_manifest(Path('.'))` returned `[]` after manifest regeneration.
- An attempt must now exactly use the frozen per-item task contract's max
  attempts, base delay, cap delay, and strategy at both scheduler admission
  and durable projection. The fixed `sha256_25pct` jitter remains scheduler
  governance. Legacy lifecycle fixtures now bind matching contracts before
  attempts, preserving the strict pre-attempt amendment freeze.
- Full selftest was run and exited 10: 412 tests with one unrelated generated
  source-scrub failure for `.worktrees/hm3i/HM3I_BRIEF.md`; this is not a
  passing gate. No commit, push, checkpoint, or gate is claimed.

## Task 6 — fix round 4/5 scheduler policy boundary

- RED: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix4-red python3 -m
  unittest -v tests.test_attempts.AttemptLifecycleTests.test_scheduler_refuses_non_retry_policy_before_any_run_append`
  exited 1 after 1 test with the expected raw `AttributeError` boundary leak.
- GREEN: the attempts bank exited 0 with 10 tests; the Item 6 covering bank
  exited 0 with 126 tests. `RunScheduler.open_attempt` now rejects non-
  `RetryPolicy` inputs before reconcile or dereference as
  `retry_policy_required`, with no durable run append. Manifest was
  regenerated; compile, manifest verification, and diff check are recorded in
  the Item 6 report. No commit, push, checkpoint, or gate is claimed.

## Task 6 — controller clean-candidate verification and review closure

- The implementation checkout's single source-scrub failure was isolated to
  an unrelated generated `.worktrees/hm3i` copy. The controller transferred
  only the exact Item 6 paths onto clean content base
  `4f1443cb2ee6c60aab050dac79a8920622e9dd9a`; no generated worktree exists in
  that candidate.
- Final covering command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-fix4-controller
  python3 -m unittest -v tests.test_attempts tests.test_manifest`; exit 0,
  18 tests passed. The implementer covering bank independently passed 126
  tests, and `git diff --check` was clean.
- Final clean-candidate command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item6-final-selftest python3
  -m slip.selftest`; exit 0, 413 tests passed, followed by
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- Independent scoped re-review closed the post-attempt amendment,
  contractless acceptance, nested unhashable input, frozen retry-policy, and
  non-`RetryPolicy` boundary findings. Final verdict: 0 Critical and 0
  Important findings; Item 6 is review-clean for controller commit.

This section records local exact-candidate verification only. It does not
claim a push, origin parity, the architect gate, live deployment, activation, or
release.

## Task 7 — finite canonical repository policy

- Content base assigned by the controller:
  `a607a406b2347fb0732df4afd1352a4840598044`. This implementation checkout's
  Git metadata remains read-only/stale and is not used to infer that base.
- RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-red python3 -m unittest -v
  tests.test_policy tests.test_manifest tests.test_source_scrub`.
  Honest RED result: exit 1; 22 tests ran with 10 failures. Eight failures
  were the expected missing `slip.policy` surface, one was the expected
  absent `slip/policy.py` manifest entry, and one was the unrelated generated
  `.worktrees/hm3i/HM3I_BRIEF.md` source-scrub hit already present in this
  implementation checkout.
- A lexical-dot-component hardening RED ran after the initial loader:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-lexical-red python3 -m unittest
  -v tests.test_policy.RepositoryPolicyTests.test_policy_path_requires_one_regular_absolute_lexical_floati_file`.
  It exited 1 after one test because an absolute path containing `..` was
  accepted. The matching GREEN command with
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-lexical-green` exited 0; one
  test passed.
- Local policy/manifest GREEN:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-green-precheck python3 -m
  unittest -v tests.test_policy tests.test_manifest` exited 0; 17 tests
  passed. The exact full focused command on the contaminated implementation
  checkout exited 1 after 22 tests with **only** the unrelated generated-tree
  scrub failure; its Item 7 policy and manifest tests all passed. A clean
  controller candidate remains required for a full focused GREEN claim.
- Static/identity checks: Python compilation of `slip/policy.py`, the policy
  tests, and manifest tests exited 0; direct `verify_manifest(Path.cwd())`
  returned `[]`; and `git diff --check` exited 0 after manifest regeneration.
  The deployable `slip/policy.py` is manifested; repository-root
  `FLOATI.toml` is deliberately outside the bundle.
- Initial policy baseline: **ABSENT** before this item. Candidate canonical
  compact-I-JSON SHA-256:
  `23e5ec9d826d91c8b51d42be1a521d1c572287f04003baeb454b2b1b1c765924`.
  Review authority is **UNOBSERVED**: the architect's security-review checkpoint is
  pending, and the four-state checker does not infer or persist review. Its
  immutable comparison basis is only an explicitly supplied 64-hex reviewed
  digest against the canonical semantic bytes of the lexical regular policy
  file.

`FLOATI.toml` accepts only the closed version-0 policy tables and bounded
scalars/arrays. `RepositoryPolicy` freezes validated profiles, conjunction
selectors, hard limits/budgets, the exact retry and approval vocabularies,
ordered verification argv, merge gates, and explicit ranked routes. Its digest
is presentation-independent compact sorted I-JSON. Loading is data-only; it
does not execute argv or launch a process. `PolicyDeploymentChecker` reports
only `DEPLOYED`, `DRIFTED`, `ABSENT`, or `CANNOT_SPEAK` for an explicit review
baseline. No installer shadow enumeration, durable review record, admission
binding, CLI surface, deployment, activation, or the architect gate is claimed.

## Task 7 — fix round 1/5 parser and path boundary closure

- RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-fix1-red python3 -m unittest
  -v tests.test_policy.RepositoryPolicyTests.test_verification_argv_refuses_dynamic_text_as_data
  tests.test_policy.RepositoryPolicyTests.test_policy_path_requires_one_regular_absolute_lexical_floati_file
  tests.test_policy.RepositoryPolicyTests.test_path_boundary_exceptions_become_typed_refusal_and_cannot_speak
  tests.test_policy.RepositoryPolicyTests.test_document_source_refuses_noncanonical_whitespace_and_controls_before_parse`.
  Honest RED result: exit 1; four tests reported 12 failures and 2 errors.
  The failures proved dynamic argv markers and noncanonical whitespace/control
  spellings were accepted. The errors proved embedded NUL and a custom
  `__fspath__` exception leaked from the path boundary; a malformed path could
  also render `ABSENT` instead of `CANNOT_SPEAK`.
- GREEN rerun of that exact four-test slice with
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-fix1-green-rerun` exited 0;
  all 4 tests passed. Raw string spellings with `.` are now refused before
  `Path` normalizes them. A preconstructed `Path` is honestly checked only for
  lexical components it still exposes; discarded literal dots cannot be
  reconstructed. Parent-directory symlinks, embedded NUL, invalid UTF-8 path
  text, and path-like conversion failures become typed refusal / checker
  `CANNOT_SPEAK`.
- Covering primary command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-fix1-focused python3 -m
  unittest -v tests.test_policy tests.test_manifest tests.test_source_scrub`
  exited 1 after 25 tests with exactly one failure, the unrelated generated
  `.worktrees/hm3i/HM3I_BRIEF.md` source-scrub hit. The 11 policy and 9
  manifest tests passed; clean-candidate proof remains required before a full
  focused GREEN claim.
- Python compilation, direct manifest verification (`[]`), and
  `git diff --check` each exited 0 after the final manifest regeneration.

Policy source now allows only literal ASCII space and LF/CRLF structural
whitespace before parsing comments; Unicode letters remain permitted in quoted
semantic strings, while controls and Unicode separators refuse. Dynamic
interpolation/template markers are rejected in verification argv as well as
other policy strings. No wider Item 7 surface, deployment behavior, durable
review record, or human review claim was added.

### Task 7 clean-controller and independent-review acceptance

- The controller copied exactly `FLOATI.toml`, `slip/policy.py`,
  `tests/test_policy.py`, `tests/test_manifest.py`,
  `bundle-manifest.v0.json`, and this evidence document onto exact base
  `a607a406b2347fb0732df4afd1352a4840598044` in the clean candidate clone.
- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-controller-focused
  python3 -m unittest -v tests.test_policy tests.test_manifest
  tests.test_source_scrub` exited 0; **25/25 passed**.
- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item7-controller-selftest
  python3 -m slip.selftest` exited 0; **425/425 passed** in 23.784 seconds and
  emitted `bundle_verified`.
- The canonical policy digest remained
  `23e5ec9d826d91c8b51d42be1a521d1c572287f04003baeb454b2b1b1c765924`;
  direct manifest verification returned `[]`; Python compilation with an
  explicit `<temp>` bytecode prefix and `git diff --check` exited 0.
  A first compile attempt without that prefix was sandbox-denied while trying
  to create a macOS Python cache outside the writable roots; it was not a code
  or test failure and is not counted as a pass.
- Scoped independent Terra re-review returned **APPROVED** with 0 Critical,
  0 Important, and 0 Minor findings. Narrow probes independently reproduced
  typed refusal plus `CANNOT_SPEAK` for malformed path boundaries, refusal of
  raw `/./`, all four dynamic argv marker spellings, and all six hostile
  whitespace/control spellings. The reviewer made no source, Git, or bus
  mutation.

This is exact-candidate local evidence. Commit identity, origin parity, the architect
checkpoint, and gate remain controller steps and are not claimed here.

## Task 8 — pure immutable plan admission

- Assigned content base: `605b9428fd0a774fd520754301ddf7906b0e63fb` on
  `lane/hm0`. The local Git metadata was explicitly stale/read-only and was
  not used as implementation identity.
- Initial RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item8-red python3 -m unittest -v
  tests.test_admission tests.test_cli tests.test_cli_workflows` exited 1;
  42 tests ran with 3 failures and 18 errors from the absent admission module
  and plan CLI surface. Expanded RED2 with the same bank and
  `<temp>/slipway-hm3i-item8-red2` prefix exited 1; 43 tests ran with 4
  failures and 1 error. It isolated the absent plan/helper seams and an
  operator-only plan incorrectly classified as `refused`.
- Diagnosis and narrow repair: the hard-invalid predicate treated every
  non-`merge_gate_pending` reason as hard-invalid, including `operator`.
  Restricting the predicate to non-operator reasons (and merge reasons other
  than valid pending gates) restored the required `needs_operator` result.
  A final API-boundary RED,
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item8-red3 python3 -m unittest -v
  tests.test_admission.AdmissionPlanTests.test_current_admission_rejects_tampered_stale_or_nonadmitted_artifacts`,
  exited 1 after one test with 2 expected raw `AttributeError` errors. The
  cause was digest dereference before loaded-plan/policy type validation; the
  matching `<temp>/slipway-hm3i-item8-green2` command exited 0; 1 test passed.
- Final covering command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item8-cover python3 -m unittest -v
  tests.test_admission tests.test_contracts tests.test_policy tests.test_runtruth
  tests.test_orchestrate tests.test_cli tests.test_cli_workflows
  tests.test_copy_ledger tests.test_manifest` exited 0; **104/104 passed**.
  It covers strict JSON/path refusal, canonical plan digest sensitivity,
  deterministic category ordering, all completed CLI outcomes, zero-effect
  behavior, the invocation-time gate ordering, legacy orchestration
  compatibility, generated copy, and the bundle manifest.
- Static checks: `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item8-compile
  python3 -m py_compile slip/admission.py slip/orchestrate.py slip/cli.py
  slip/helptext.py tests/test_admission.py tests/test_cli.py
  tests/test_cli_workflows.py` exited 0. Direct
  `verify_manifest(Path.cwd())` printed `[]` and exited 0 after manifest
  regeneration. Full selftest was not run for this scoped item.
- The new `slip plan` surface is explicit-input, read-only, and reports only
  `admitted`, `refused`, or `needs_operator`; it does not discover a root,
  run verification, launch a worker, or append durable evidence. The legacy
  `slip orchestrate` v0 path remains unchanged. The only run seam is the
  in-process `require_current_admission()` check immediately before its own
  `run_created` append, followed by existing `run_policy_bound` ordering.
- the architect request `msg-019fe034bd66729e924938f2d15dfc93` remains unanswered.
  Therefore this item does **not** claim durable exact admitted plan/policy
  pair proof, universal low-level `RunLedger` enforcement, an admission
  record, a capability, or any schema/runtruth extension. Commit, push,
  checkpoint, the architect gate, deployment, and activation remain controller work.

### Task 8 independent review and fix round 1

- Independent Terra review returned **BLOCKED** with 1 Critical and 1
  Important finding. A loaded high-risk plan or policy could have public
  semantic fields changed while retaining its cached canonical bytes and
  digest, causing evaluation and the invocation-time gate to describe a
  different plan/policy pair. Public `AdmissionReason` and
  `AdmissionArtifact` constructors also performed closed-vocabulary
  membership before string validation and leaked raw `TypeError` for
  unhashable inputs.
- RED was measured with
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item8-fix1-red python3 -m unittest
  -v tests.test_admission.AdmissionPlanTests.test_evaluator_rejects_mutated_plan_and_policy_values_with_stale_cached_digests
  tests.test_admission.AdmissionPlanTests.test_public_artifact_and_reason_reject_unhashable_closed_fields_as_typed_refusals`:
  exit 1; 2 tests ran with 3 expected assertion failures and 4 expected raw
  `TypeError` errors.
- The repair validates public value types before vocabulary membership,
  reconstructs current plan and policy canonical semantics before evaluation,
  compares those bytes and digests to their caches, and derives routing order
  from the validated canonical routing map. Semantic/cache divergence now
  refuses before a result or append.
- The exact two-test GREEN rerun exited 0; **2/2 passed**. The covering bank
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item8-fix1-cover-root
  python3 -m unittest -v tests.test_admission tests.test_contracts
  tests.test_policy tests.test_runtruth tests.test_orchestrate tests.test_cli
  tests.test_cli_workflows tests.test_copy_ledger tests.test_manifest` exited
  0; **106/106 passed**. Python compilation, direct manifest verification
  (`[]`), and `git diff --check` each exited 0.
- This repair proves only current field-to-cache consistency at the narrow
  invocation boundary. It does **not** prove loader origin, resist a caller
  that coherently constructs both semantics and caches, create a security
  token/capability, or add durable admitted-pair authority. Those claims stay
  fenced with the unanswered the architect request above.
- The clean controller candidate repeated the covering bank at **106/106**
  and ran
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item8-controller-fix1-selftest
  python3 -m slip.selftest`: exit 0; **438/438 passed** in 25.503 seconds and
  emitted `bundle_verified`. Controller compilation, direct manifest
  verification (`[]`), and `git diff --check` also exited 0.
- Scoped independent Terra re-review returned **APPROVED** with both prior
  findings resolved and 0 new Critical, Important, or Minor findings. The
  reviewer independently ran 3/3 narrow tests and confirmed stale plan and
  policy fields now produce typed integrity refusals. The reviewer also
  independently confirmed that a coherently caller-constructed policy can
  still pass, consistent with the explicit provenance/security/durable
  authority fence rather than contrary to it.

## Task 9 — repository decision register and deterministic handoff capsule

- Assigned content base: exact pushed Item 8
  `a3118481655088729e79689bd999433cf955b0cc` on `lane/hm0`. This checkout's
  Git metadata is stale/read-only and was not used as implementation identity.
- Initial RED command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-red-core python3 -m unittest -v
  tests.test_decisions tests.test_record_validation tests.test_schemas` exited
  1; **37 tests ran, 10 expected failures**. The failures were the absent
  decision module, absent `decision_record` validator kind, and absent
  decision/capsule schemas.
- Core GREEN command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-green-core python3 -m unittest
  -v tests.test_decisions tests.test_record_validation tests.test_schemas`
  exited 0; **37/37 passed**. A first GREEN invocation stopped at a new-module
  annotation syntax error; the exact parser error was isolated, corrected as
  one bracket, and no test result was counted from that interrupted run.
- Idempotent corrupt-tail RED:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-idempotent-tail-red python3 -m
  unittest -v tests.test_decisions.DecisionRegisterTests.test_idempotent_retry_refuses_a_persisted_semantic_corrupt_tail`
  exited 1; **1 expected failure**. A matching proposal retry returned before
  replaying a later schema-valid but semantically invalid terminal frame. The
  matching GREEN command with the `-green` prefix exited 0; **1/1 passed**.
  `DecisionRegister.append` now projects the existing physical sequence with
  `integrity=True` before an idempotent return; candidate-only causal errors
  remain `ProtocolRefusal` and persisted semantic errors are
  `IntegrityFailure`.
- Added decision-family gauntlet rows and executed their direct slices:
  crash **1/1**, fuzz **1/1**, time **1/1**, recovery **1/1**, and twelve-process
  single-proposal contention **1/1**, all exit 0. The time slice first exposed
  a fixture tenant mismatch before product execution; its same-tenant rerun
  passed and the mismatch is not counted as a product failure.
- Final focused command:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-final-focused python3 -m
  unittest -v tests.test_decisions tests.test_record_validation
  tests.test_schemas tests.test_root_jsonl tests.test_tenancy
  tests.test_gauntlet_crash tests.test_gauntlet_fuzz tests.test_gauntlet_time
  tests.test_gauntlet_recovery tests.test_gauntlet_concurrency
  tests.test_manifest` exited 0; **91/91 passed**. Python compilation of the
  changed decision/record/test modules exited 0; direct
  `verify_manifest(Path.cwd())` returned `[]`; and `git diff --check` exited 0.

The sole writable decision coordinate is the explicit,
lexically-constrained `repositories/<repository>/decisions.jsonl` below a
supplied `SlipRoot`. Proposal record IDs and logical decision IDs are separate
UUIDv7 domains. Replay derives `proposed`, `accepted`, `superseded`, and
`rejected` only from physical append order; timestamps are testimony. A
capsule is a byte-stable read artifact containing only current accepted frames
in accepted-frame ordinal order and their recomputable semantic digests. It
contains no ID, timestamp, cache, memory, summary, inference, ranking, or
score.

the architect request `msg-019fe049a5007a748ba006f4de24587e` remains unanswered.
Therefore terminal disposition writer authority is deliberately unavailable:
the public append path authorizes proposals only, while accepted/rejected
frames are validated and replayed only as supplied durable evidence.
`superseded` is projection-only; nonempty `source_artifact_ids`, source
taxonomy/lookup, repository-bound task-contract authority, closed scope
applicability, and Item 11 C7 inclusion are not invented. This is local
implementation evidence only: no commit, push, checkpoint, the architect gate,
publication, deployment, or activation is claimed.

### Task 9 fix round 1 — caller snapshot, independent IDs, and lexical integrity

- Reviewer RED for the two original findings:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-fix1-red python3 -m unittest -v
  tests.test_decisions.DecisionRegisterTests.test_append_snapshots_hostile_and_ordinary_caller_mappings
  tests.test_decisions.DecisionRegisterTests.test_append_and_project_refuse_a_shared_physical_logical_uuid_component`
  exited 1; **2/2 failed as expected**. A stateful `dict` subclass passed the
  proposal checks then persisted `accepted`, and a direct row reused one UUIDv7
  component for physical and logical identities. The extended hostile-mapping
  RED with the `-red-unserializable` prefix exited 1; **3/3 failed as
  expected**, including a raw `RuntimeError` escaping the encoder boundary.
- `DecisionRegister.append()` now first serializes and parses a compact
  I-JSON plain-data snapshot, then validates, projects, and writes only that
  detached value. Hostile or unserializable caller mappings return typed
  `ProtocolRefusal`, and an ordinary caller mutation after return cannot alter
  the returned/persisted proposal. The decision validator rejects a shared
  physical/logical UUIDv7 component; this is `ProtocolRefusal` for a candidate
  and `IntegrityFailure` for a persisted frame. All core and five scoped
  gauntlet decision fixtures now use independent components; the fixed capsule
  digest was recomputed from the changed immutable IDs.
- Reviewer Unicode/schema RED:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-fix1-red-unicode-schema-assertive
  python3 -m unittest -v
  tests.test_decisions.DecisionRegisterTests.test_candidate_and_persisted_unpaired_surrogates_keep_typed_boundaries
  tests.test_schemas.SchemaContractTests.test_decision_and_capsule_schemas_pin_expressible_lexical_safety`
  exited 1; **2/2 failed as expected**. A persisted escaped unpaired surrogate
  escaped capsule projection as `ProtocolRefusal`, and the decision timestamp
  schema had no exact UTC lexical pattern.
- Scalar Unicode validation now rejects surrogate code points directly; the
  projector also reclassifies any canonicalization refusal from persisted data
  as `IntegrityFailure`. Candidate input remains `ProtocolRefusal`. Decision
  and capsule schemas now pin the UTC timestamp grammar, reject controls/Bidi
  controls/surrogates in visible strings, and explicitly reject newlines in
  repository and ledger strings. Standard JSON Schema cannot compare the two
  UUID fields or derive capsule `ledger` from `repository`; those relations
  remain runtime/projector-enforced and are covered by direct append/project
  and emitted-capsule tests rather than custom schema extensions.
- Narrow GREENs: the three original repair tests with
  `<temp>/slipway-hm3i-item9-fix1-green-narrow` passed **3/3**; the surrogate and
  schema pair with `<temp>/slipway-hm3i-item9-fix1-green-unicode-schema` passed
  **2/2**. Core
  `<temp>/slipway-hm3i-item9-fix1-green-core` passed **43/43**. The pre-evidence
  focused bank
  `<temp>/slipway-hm3i-item9-fix1-focused-pre-evidence` passed **96/96**.
  A fresh post-evidence focused/compile/manifest/diff run remains the final
  controller handoff step.

### Task 9 fix round 2 — typed public proposal boundary and bound operation coordinate

- Public `DecisionRegister.propose()` boundary RED:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-fix2-red python3 -m unittest -v
  tests.test_decisions.DecisionRegisterTests.test_propose_types_hostile_explicit_values_without_coercion_or_append
  tests.test_decisions.DecisionRegisterTests.test_public_relative_path_mutation_never_retargets_append
  tests.test_decisions.DecisionRegisterTests.test_public_repository_mutation_never_writes_to_the_retained_old_coordinate
  tests.test_decisions.DecisionRegisterTests.test_observation_coordinate_is_equally_bound`
  exited 1; **4 tests produced 9 expected assertion failures**. Explicit ID
  objects could leak from `str()`, hostile tuple/list subclasses could leak
  while iterating sources, and mutable public coordinate attributes could
  redirect the durable path or split a record repository from its ledger.
- The repair accepts an explicit record or decision UUID component only as a
  plain `str`, and source IDs only as an ordinary `tuple` or `list`, before
  constructing the candidate. Other hostile or coercible public values now
  return typed `ProtocolRefusal` with no append. It does not manufacture a
  source authority: the existing nonempty-source and proposal-only writer
  fences remain unchanged.
- Coordinate virtual-dispatch RED:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-fix2-c1-red python3 -m unittest -v
  tests.test_decisions.DecisionRegisterTests.test_writable_subclass_properties_cannot_retarget_or_mismatch_the_bound_coordinate
  tests.test_decisions.DecisionRegisterTests.test_observation_subclass_properties_cannot_retarget_capsule_projection`
  exited 1; **2 expected failures**. The observation fixture was then
  corrected to persist through its `bravo` writer rather than the test's
  `alpha` helper before its GREEN proof. A frozen private coordinate plus
  slots now binds the canonical repository and path; reassignment is blocked,
  and records, projection, append, proposal construction, and capsule output
  use that private coordinate rather than overrideable public display
  properties.
- Tenant virtual-dispatch RED:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-fix2-tenant-red python3 -m unittest -v
  tests.test_decisions.DecisionRegisterTests.test_writable_subclass_tenant_property_cannot_rewrite_bound_tenant_validation_or_proposal
  tests.test_decisions.DecisionRegisterTests.test_observation_subclass_tenant_property_cannot_rewrite_bound_replay`
  exited 1; **2 expected errors**: a writable override caused candidate
  validation against `bravo`, and an observation override caused persisted
  replay against `alpha`. Operational validation, projection, and proposal
  construction now read the private authority tenant directly; `tenant_id`
  is display-only.
- GREENs: the four initial public-boundary tests passed **4/4** with
  `<temp>/slipway-hm3i-item9-fix2-green-narrow`; the coordinate rerun plus the
  earlier boundary cases passed **6/6** with
  `<temp>/slipway-hm3i-item9-fix2-c1-green-rerun`; and the full coordinate,
  tenant, and hostile boundary set passed **8/8** with
  `<temp>/slipway-hm3i-item9-fix2-tenant-green`. Core
  `<temp>/slipway-hm3i-item9-fix2-c1-tenant-core` passed **51/51**. The
  pre-evidence scoped bank
  `<temp>/slipway-hm3i-item9-fix2-c1-tenant-focused-pre-evidence` passed
  **104/104**; the count increases from 96 solely because this repair adds
  eight direct boundary tests.
- The manifest digest for `slip/decisions.py` was updated. Direct
  `verify_manifest(Path.cwd())` returned `[]` before the final post-evidence
  rerun. This evidence remains local only: no terminal decision writer,
  source taxonomy/lookup, repository-bound task-contract authority, Item 11
  inclusion, commit, push, bus, deployment, or activation is claimed while
  the architect request `msg-019fe049a5007a748ba006f4de24587e` remains unanswered.
- Final ordinary-root-binding RED:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-fix2-root-binding-red python3 -m unittest -v
  tests.test_decisions.DecisionRegisterTests.test_private_writable_root_bindings_cannot_retarget_append
  tests.test_decisions.DecisionRegisterTests.test_private_observation_root_bindings_cannot_gain_writer_authority`
  exited 1; **2 expected failures**. Reassigning both `_authority` and
  `_write_root` changed an alpha writer into a bravo writer, and made an
  observation writable. `__setattr__` now permits each constructor-bound
  authority, write-root capability, and coordinate slot exactly once. This
  closes ordinary assignment only; `object.__setattr__` is not represented as
  a public security-token boundary.
- The ten-test root-boundary GREEN
  `<temp>/slipway-hm3i-item9-fix2-root-binding-green` passed **10/10** and its
  core bank `<temp>/slipway-hm3i-item9-fix2-root-binding-core` passed
  **53/53**. The two root-binding cases increase the final scoped-bank count
  to **106**. The manifest was regenerated again for the final
  `slip/decisions.py` digest; fresh post-evidence bank/compile/manifest/diff
  evidence follows the final controller handoff.

### Task 9 clean-controller and independent-review acceptance

- The clean controller clone repeated the complete scoped bank at
  **106/106** and ran
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item9-controller-fix2-selftest
  python3 -m slip.selftest`: exit 0; **468/468 passed** in 26.799 seconds and
  emitted `bundle_verified`. Fresh Python compilation, direct manifest
  verification (`[]`), and `git diff --check` each exited 0.
- Independent Terra review returned **APPROVED_SCOPED** with 0 active
  Critical, Important, or Minor findings. The reviewer reran the scoped bank
  at **106/106**, compiled the changed modules, obtained manifest `[]`, and
  independently reprobed hostile proposal inputs, immutable caller snapshots,
  physical/logical UUID separation, persisted-surrogate integrity typing,
  whole-ledger idempotent replay, timestamp-hostile physical ordering,
  capsule digest/order, symlink confinement, and read-only observation.
- The reviewer also independently verified that ordinary mutation, subclass
  property overrides, tenant overrides, and authority/write-root slot
  reassignment cannot retarget a decision write or grant an observation
  writer authority. The register remains an ordinary API object rather than a
  security token; deliberate `object.__setattr__`/memory tampering is not
  claimed as an authority boundary.
- The proposal-only terminal-authority fence, empty unruled source binding,
  opaque task-contract context, projection-only `superseded`, and Item 11
  exclusion remain exactly as stated above pending the architect request
  `msg-019fe049a5007a748ba006f4de24587e`.

## 2026-08-08 Item 8 durable-pair ruling supersession

This section supersedes only the pre-ruling Item 8 statement that no durable
admitted plan/policy pair was authorized. It does not rewrite that historical
fence. the architect ruling
`15cac5ad178d9dc8ae1cb5ccdea8e77f63662845` selected Option B: the existing
`run_created` family may carry an optional `policy_digest`; an admitted run
persists `plan_digest` and `policy_digest` in its first frame; the later
`run_policy_bound` frame must match; and legacy rows without the field remain
readable but project typed pair-proof `unavailable`, with no inference,
backfill, or new record family.

The RED-first additive implementation now projects `pending` during the
permitted first-frame/binding gap, `bound` after an equal policy binding, and
`unavailable` for legacy rows. A new unequal binding refuses before append as
`admitted_pair_policy_mismatch`; a persisted unequal pair is an integrity
failure. The clean controller focused bank passed **90/90**. The independent
Terra pair review approved runtime behavior with **0 Critical and 0 runtime
Important** findings after its own 90-test bank, 27-test post-drift pair bank,
four crash/fuzz/process slices, and hostile schema/API/replay probes.

This is durable pair evidence only. It is not loader provenance, a security
token or capability, a universal low-level ledger admission gate, a new
approval authority, or permission to infer evidence for legacy rows.

## 2026-08-08 Item 10 gauntlet and publication-contract receipt

This receipt is local shared-tree evidence for Item 10 only. The controller
designated pushed Item 9 content
`01404ac1a130e2d95e8c0eb90cb765ce5b52f77c` as the base; this shared checkout's
Git metadata is stale and is **not** used to identify an Item 10 candidate.
Accordingly, no exact committed-candidate, the architect final gate, publication,
installation, deployment, activation, commit, push, or bus action is claimed
here. A clean controller snapshot still has to repeat the generated-tree scrub
and full selftest before anyone may make an exact-tip claim.

### RED-first record

- TD1 hostile lexical node spellings first ran
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-td1-red python3 -m unittest -v tests.test_gauntlet_fuzz.ReaderFuzzGauntletTests.test_hostile_cli_node_spellings_refuse_before_every_durable_entrypoint_writes`;
  it exited 1 after one test with four expected failures in 1.157 seconds.
  Sender/recipient lexical validation now occurs before registry or denial
  persistence.
- TD3/TD4 first ran
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-td3-td4-red-exact python3 -m unittest -v tests.test_gauntlet_fuzz.ReaderFuzzGauntletTests.test_session_scoped_ack_and_append_only_retraction_do_not_cross_parties tests.test_gauntlet_fuzz.ReaderFuzzGauntletTests.test_legacy_attempt_binding_is_literal_and_dead_holder_projects_stale_send`;
  it exited 1 with two expected errors in 0.004 seconds because `send` had no
  `worker_session_id` boundary. The corresponding validator/gauntlet GREEN
  passed 4/4 in 0.014 seconds, and the later compatibility bank passed 56/56
  in 0.141 seconds.
- The twelve-process receipt/result race first refused an otherwise unrelated
  `run_created` append: the two-case contention RED exited 1 in 0.158 seconds.
  Moving the receipt snapshot inside the run-ledger locked decision closure
  produced the three-case GREEN at 3/3 in 1.408 seconds; it does not widen the
  lock beyond that receipt-read seam.
- Versioned schemas were first absent from the exact deployable population:
  `tests.test_manifest.ManifestTests.test_versioned_schema_families_are_all_deployable`
  failed once. The manifest population is now `slip/**/*.py`,
  `schemas/v[0-9]*/*.json`, and `scripts/slip`; it names all three ruled v1
  presenter schemas. The current manifest suite passed 10/10 in 0.022 seconds
  and direct `verify_manifest(Path.cwd())` returned `[]`.
- The decision timestamp disagreement was not weakened in code. the architect
  `msg-019fe1d9ed9f7f00979e625dd59e4175` at `35e5a31` ruled that timestamp
  invariance applies to state selection, not the timestamp-bound durable
  decision digest or capsule bytes. The time comparator therefore uses
  accepted decision id, state, and physical ledger ordinal only.

### Ruled bindings retained by the Item 10 harness

- TD3 consumes the architect `msg-019fe1b00c07719ba028a57f2140bdde` / ruling
  `f0319e9d57d884a0bf4f7c85cdbf0d5277b437bf`: append-only
  `message_retracted`, `ret-` IDs, its six exact fields, and the closed reasons
  `sent_in_error`, `superseded_by_correction`, `stale_recipient`, and
  `security_scrub`.
- TD4 preserves literal `absent_legacy` or a complete opaque
  `attempt_id`/`claim_id`/`lease_id`/`worker_session_id` binding; partial input
  collapses to the literal. A dead holder projects only ruled `stale_send`
  fields and never renews.
- TD2 continues explicit `--root`, then `SLIP_BUS_ROOT`, then no fallback.
  TD5 consumes `msg-019fe1cbf1a3751981c53cba5bd58f66`: v0 remains byte-frozen;
  v1 doctor/status/watch artifacts carry the shared `installer_shadow` fact;
  absent destination is explicit `cannot_speak`/22 rather than an invented
  clean path result. Item 10's TD5 integration passed 2/2 in 0.269 seconds.
- Item 7's SECURITY-REVIEW is Fable-authored checkpoint evidence, not a lane
  self-attestation: ruling `e4034d26` sections 1--2, the architect Item 7 GREEN
  `605b942`, policy digest
  `23e5ec9d826d91c8b51d42be1a521d1c572287f04003baeb454b2b1b1c765924`.
  the architect's caveat remains: digest-sensitivity tests covered it, but the digest
  was not independently recomputed here. This supersedes only the earlier
  local unobserved-review fence; it adds no durable security capability,
  loader provenance, or new record kind.
- Item 9's schema-helper parity correction is authorized by the architect
  `msg-019fe1fdcc8d795b862b02237a73c00c` / `3d501dcedc27d97787424610b8dd29b169d562e6`.
  The permanent helper-versus-Draft parity case remains in the stdlib-only
  harness; no third-party test dependency is required.

### Literal inventory and coverage matrix

The fixture asserts exactly these 26 physical run kinds, no aliases:

```text
run_created, task_contract, plan_amendment, run_policy_bound,
worker_pool_bound, dispatch_decision, result_produced, result_verified,
acceptance_receipt, result_accepted, run_terminal, attempt_opened,
attempt_started, attempt_terminal, retry_scheduled, retry_exhausted,
cancel_requested, cancel_scope_resolved, cancel_observed, cancel_signal_sent,
cancel_terminal, cancel_unconfirmed, stale_attempt_evidence,
stale_evidence_adopted, attempt_harness_session_bound, supervisor_orphaned
```

It freezes Item 5 item outcomes `succeeded`, `failed`, `cancelled`, `skipped`,
`needs_operator`, and `uncertain`, with only `partially_succeeded` added at
the run level. It joins the explicit `FLOATI.toml` Item 7 policy surface, Item
8 read-only admission artifact, and Item 9 durable proposal/accepted capsule
without treating any of them as an extra run kind.

| Axis | Exercised local proof |
| --- | --- |
| Crash | Every literal run kind is cut at the append/durability seam; recovery observes only a prefix or typed torn truth. |
| Fuzz | Literal inventory, malformed prefix, frozen vocabulary, policy/admission/decision/capsule boundaries fail closed. |
| Time | Physical frame order selects run and decision state under future, backward, equal, and DST testimony. |
| Recovery/snapshot | Retry and FOC traces reopen stable physical ids and joins from authoritative frames, not a cache. |
| Twelve-process contention | Run-created idempotence, independent owned runs, policy/admission reads, decision proposals, and receipt/result races leave no partial/cross-run truth. |

FOC coverage exposes only its ruled orphan-source and orphan-class facts,
preserves stable attempt/claim/lease/worker-session joins and physical source
order, retains frozen vocabularies, and makes timestamps non-authoritative for
projection. It makes no canonical Item 11 read bundle or canonical-projection
artifact.

### Current local verification

- Complete gauntlet:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-fix3-final-gauntlets python3 -m unittest -v tests.test_gauntlet_crash tests.test_gauntlet_fuzz tests.test_gauntlet_time tests.test_gauntlet_recovery tests.test_gauntlet_concurrency tests.test_gauntlet_snapshot`
  exited 0: **41/41** in **12.253 seconds**.
- Architecture-contract bank (run truth, attempts, cancellation, outcomes,
  contracts, policy, admission, decisions, deploy, validators/schemas,
  manifest/conformance/CLI/root/tenancy/cursor/events/doctor/orchestrate/copy,
  HM3I contract, JSONL/process atomicity/managed/source scrub) exited 1 after
  **296** tests in **17.614 seconds**: **295 passed**, and the one failure is
  the generated-tree scrub named below.
- Full direct selftest:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-fix3-final-selftest python3 -m slip.selftest`
  exited **10** after **522** tests in **42.023 seconds**: **521 passed**, one
  generated-tree scrub failure. It therefore did not emit or justify a
  `bundle_verified` completion claim.
- Direct manifest verification returned `[]`; history-note scrub returned
  `[]`; and `git diff --check` exited 0 before this evidence append.

### Remaining nonpass and parent action

The only observed final-bank nonpass is
`['.worktrees/hm3i/HM3I_BRIEF.md']` from
`scan_generated_tree(Path.cwd())`; the direct command exits 1 and the same
path is the sole assertion failure in both scoped and full banks. It is an
unresolved nested-worktree artifact in the shared checkout, not a GREEN result
and not a product repair authorization. The controller must prepare a clean
candidate, remove or isolate that artifact under the governing process, rerun
the source scrub and full selftest there, assign the exact candidate identity,
and obtain the separate the architect disposition. Item 11 and post-HM3I work remain
fenced.

## 2026-08-08 Item 10 controller clean-candidate closure

This appends the controller-clean-candidate result without rewriting the
historical shared-checkout nonpass above. The controller isolated that checkout
artifact in `<temp>/slipway-hm3i-resume.LhUPUF`, built from base
`8d80ca086e9f90452bb1cf20e60445134d554a94` with the current Item 9/10
changes. The prior `.worktrees/hm3i/HM3I_BRIEF.md` source-scrub finding is not
present in that candidate.

- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-controller-item10-selftest python3 -m slip.selftest`
  exited 0: **522/522** in **37.718 seconds**, emitting
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-controller-item10-selftest python3 -m unittest -v tests.test_manifest tests.test_source_scrub tests.test_hm3i_contract`
  exited 0: **17/17** in **0.255 seconds**.
- `python3 -m compileall slip tests` exited 0; direct
  `verify_manifest(Path.cwd())` returned `[]`; and `git diff --check` exited
  0 in the controller candidate.

This is a clean-candidate local verification closure only. It does not claim a
commit, candidate SHA, push, publication, activation, deployment, or the architect
final gate. Item 11 and post-HM3I scope remain fenced.

## 2026-08-08 Item 11 C7.1 read-bundle implementation receipt

This is local shared-worktree implementation evidence for the additive
`bundle/c7.1/` candidate only. It records no commit, candidate SHA, push, bus,
publication, deployment, activation, external-consumer execution, or the architect
final gate. C7 v0 remains frozen, and the ruled post-Item-11, prepublication
C7.2 segment-identity debt remains in the publication checklist.

### RED-first and review seams

- The initial reader-integrity RED was:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item11-review-red-a python3 -m unittest -v tests.test_c7_bundle.C7ReadBundleTests.test_reader_refuses_a_digest_consistent_projection_with_a_null_family tests.test_c7_bundle.C7ReadBundleTests.test_reader_refuses_a_digest_consistent_empty_present_run_map tests.test_c7_bundle.C7ReadBundleTests.test_reader_refuses_an_index_with_an_extra_or_malformed_family_shape tests.test_c7_bundle.C7ReadBundleTests.test_malformed_unreferenced_worker_source_is_isolated_from_run_state tests.test_c7_bundle.C7ReadBundleTests.test_malformed_referenced_worker_source_errors_only_dependent_families tests.test_c7_bundle.C7ReadBundleTests.test_c7_1_preflights_every_existing_output_symlink_before_any_write tests.test_c7_bundle.C7ReadBundleTests.test_duplicate_harness_session_inside_one_binding_is_conflicting_without_winner tests.test_c7_bundle.C7ReadBundleTests.test_segment_typed_absence_fallbacks_are_bundle_relative_raw_paths tests.test_c7_bundle.C7ReadBundleTests.test_malformed_run_projection_keeps_the_closed_q5_vocabulary_schema_valid tests.test_c7_bundle.C7ReadBundleTests.test_public_projector_refuses_non_bytes_raw_run_override`.
  It exited 1 with 10 tests, 11 expected failures, and 3 expected errors.
- The self-contained-source/installed-layout RED was:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item11-review-red-b python3 -m unittest -v tests.test_c7_bundle.C7ReadBundleTests.test_materialized_snapshot_contains_hashed_catalog_schemas_and_advertised_raw_ledgers tests.test_c7_bundle.C7ReadBundleTests.test_reader_verifies_copied_catalog_schemas_and_auxiliary_raw_digests tests.test_c7_bundle.C7ReadBundleTests.test_manifest_installed_layout_materializes_with_its_c7_static_package tests.test_c7_bundle.C7ReadBundleTests.test_auxiliary_timestamp_testimony_does_not_change_semantic_digest`.
  It exited 1 with 4 tests, 36 expected failures, and 6 expected errors.
- The single-record duplicate-session Q5 RED ran
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item11-red-duplicate-session python3 -m unittest -v tests.test_c7_bundle.C7ReadBundleTests.test_duplicate_harness_session_inside_one_binding_is_conflicting_without_winner`;
  it exited 1 after 1 test with a selected-binding shape error. These REDs are
  local regression records, not the architect execution or acceptance.

### Implemented bounded contract

- The checked-in C7.1 index, schema catalog, schemas, and Slipway-only README
  are copied into a fresh explicit destination. The catalog carries exact
  SHA-256 values for every named v0 or C7.1 schema; materialization copies each
  one and the advertised raw work-item ledger. The manifest includes all C7.1
  runtime-loaded assets, so an installed layout contains the same package.
- The materializer captures run, worker-receipt, registry, decision, and work
  item bytes once before output. It writes those exact bytes and projects from
  that same capture. Destination roots, ancestors, children, static targets,
  raw targets, and schema targets are preflighted against symlink redirection;
  the selected source tenant and checked-in contract package are fenced.
- The normative projection keeps `raw_source_digest` for exact run bytes and
  adds closed `tenant_id`, repository, and non-causal `auxiliary_sources`
  identity. `semantic_digest` excludes all raw-byte digests at every nesting
  level; `self_digest` covers the emitted artifact except itself. Worker,
  registry, decision, and work evidence keeps its own physical order and
  cannot timestamp-merge into run state.
- A malformed unreferenced worker ledger becomes its own typed auxiliary error
  without poisoning run state. When run result conclusions explicitly depend
  on invalid worker evidence, only result/logical/run outcome families carry
  that worker-ledger typed error. Registry lineage and raw decision exposure
  likewise remain isolated to their evidence surfaces.
- Claim/lease physical frames, task-contract amendment frames, binding frames,
  Q5 artifact-local segment references, decision frames, and registry lineage
  are bound to captured source-frame identities. Q5 remains typed absent for
  relation/predecessor; duplicate or cross-record session overlap and
  incompatible binding keys become `conflicting_binding` with candidates and
  no winner. No v0 binding schema, durable family, route, capability, or token
  was added.
- The reader validates its closed index/catalog/projection shapes and every
  contained regular file, copied source digest, catalog-schema hash, and raw
  fallback. It then decodes the snapshot only through its stated tenant and
  repository identities, deterministically reprojects the captured bytes, and
  compares the canonical emitted artifact before returning it.

### Pre-evidence local verification

- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item11-full-pre-evidence python3 -m unittest -v tests.test_c7_bundle tests.test_c7_static tests.test_manifest tests.test_deploy tests.test_hm3i_contract`
  exited 0: **60/60** in **4.873 seconds**.
- That command is a local reader/static/manifest/deploy/document bank only. It
  does not execute an external the architect bank, a publication gate, an activation
  gate, or an external-consumer integration.

### Post-evidence verification

- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item11-post-doc-compile python3 -m py_compile slip/c7_bundle.py tests/test_c7_bundle.py tests/test_c7_static.py tests/test_manifest.py tests/test_hm3i_contract.py`
  exited 0.
- Direct `verify_manifest(Path.cwd())` returned `[]`. The post-evidence exact
  local bank
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item11-full-post-evidence python3 -m unittest -v tests.test_c7_bundle tests.test_c7_static tests.test_manifest tests.test_deploy tests.test_hm3i_contract`
  exited 0: **60/60** in **4.951 seconds**. `git diff --check` exited 0.
- The I11 FOC reader-route RED was
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-i11-red python3 -m unittest -v tests.test_c7_bundle.C7ReadBundleTests.test_c7_1_foc_bank_has_thirty_nine_executable_reader_vectors`.
  It exited 1 in **1.391 seconds**: the new per-vector real-reader assertion
  expected 39 routes and observed 0 because the old bank made only two direct
  reader calls outside that route. The GREEN after routing every vector through
  its relevant materialized or mutated snapshot (or its expected reader
  refusal) was **1/1** in **2.130 seconds**. The final post-I11 bank was
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item11-full-i11 python3 -m unittest -v tests.test_c7_bundle tests.test_c7_static tests.test_manifest tests.test_deploy tests.test_hm3i_contract`;
  it exited 0: **60/60** in **4.290 seconds**. The matching compile command,
  direct `verify_manifest(Path.cwd())`, and `git diff --check` each exited 0;
  manifest verification returned `[]`.

### I1--I11 local review addendum

The review labels below name distinct defects. The earlier RED commands group
overlapping seams, so their aggregate counts are not individual counts for each
label.

- **I1:** the reader accepted digest-consistent null/empty/extra-shape state;
  index, catalog, projection, and physical-state shape validation are now
  closed before reprojection.
- **I2:** malformed auxiliary worker evidence could be blamed on run frames or
  poison independent state; captured auxiliary ledgers now have their own
  digest, physical pointer, and narrowly dependent typed errors.
- **I3:** a destination, ancestor, or output-child symlink could redirect a
  write into the source; every output seam is preflighted before any write.
- **I4:** a duplicate `harness_session_id` in one binding was set-collapsed to
  a selected relationship; it is now `conflicting_binding` with no winner.
- **I5:** Q5 typed-absence pointers named a source-relative run ledger; they
  now name the bundle-relative `raw/runs/events.jsonl` artifact.
- **I6:** a generic malformed-run error dropped the required closed Q5
  vocabulary; session-binding error paths retain it.
- **I7:** deployment inventory omitted static C7.1 assets used by the runtime
  reader; the manifest and installed-layout test bind that package.
- **I8:** the snapshot mixed contract-relative catalog references with missing
  copied schemas and advertised raw work-item evidence; the materialized
  bundle is self-contained and checks those files and hashes.
- **I9:** claim, lease, task-contract, decision, and lineage references could
  name unbound frames; reader validation now binds them to captured physical
  source identities.
- **I10:** a shape-valid, digest-consistent current state or outcome could be
  semantically invented; the reader deterministically reprojects captured
  bytes and requires canonical artifact equality.
- **I11:** the declared 39-vector local FOC bank directly read only two
  branches. Each vector now exercises `read_c7_1_bundle` on its relevant
  materialized or mutated snapshot, or asserts its required reader refusal.

RED evidence for I1--I10 remains the 10-test reader-integrity aggregate
(**11 expected failures**, **3 expected errors**), the 4-test
self-contained/installed-layout aggregate (**36 expected failures**, **6
expected errors**), and the separate I4 duplicate-session RED (**1 test**).
The I11 route-accounting RED was **1 test**, failing `39 != 0` in **1.391
seconds**. The post-fix focused local review command was
`PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item11-i1-i11-focused python3 -m unittest -v tests.test_c7_bundle.C7ReadBundleTests.test_reader_refuses_a_digest_consistent_projection_with_a_null_family tests.test_c7_bundle.C7ReadBundleTests.test_reader_refuses_a_digest_consistent_empty_present_run_map tests.test_c7_bundle.C7ReadBundleTests.test_reader_refuses_an_index_with_an_extra_or_malformed_family_shape tests.test_c7_bundle.C7ReadBundleTests.test_malformed_unreferenced_worker_source_is_isolated_from_run_state tests.test_c7_bundle.C7ReadBundleTests.test_malformed_referenced_worker_source_errors_only_dependent_families tests.test_c7_bundle.C7ReadBundleTests.test_auxiliary_timestamp_testimony_does_not_change_semantic_digest tests.test_c7_bundle.C7ReadBundleTests.test_c7_1_preflights_every_existing_output_symlink_before_any_write tests.test_c7_bundle.C7ReadBundleTests.test_c7_1_refuses_a_destination_with_a_caller_controlled_symlink_ancestor tests.test_c7_bundle.C7ReadBundleTests.test_duplicate_harness_session_inside_one_binding_is_conflicting_without_winner tests.test_c7_bundle.C7ReadBundleTests.test_segment_typed_absence_fallbacks_are_bundle_relative_raw_paths tests.test_c7_bundle.C7ReadBundleTests.test_malformed_run_projection_keeps_the_closed_q5_vocabulary_schema_valid tests.test_c7_bundle.C7ReadBundleTests.test_manifest_installed_layout_materializes_with_its_c7_static_package tests.test_c7_bundle.C7ReadBundleTests.test_materialized_snapshot_contains_hashed_catalog_schemas_and_advertised_raw_ledgers tests.test_c7_bundle.C7ReadBundleTests.test_reader_verifies_copied_catalog_schemas_and_auxiliary_raw_digests tests.test_c7_bundle.C7ReadBundleTests.test_reader_binds_claim_frames_and_decision_path_to_captured_identities tests.test_c7_bundle.C7ReadBundleTests.test_reader_reprojects_captured_bytes_before_accepting_state_or_outcome_claims tests.test_c7_bundle.C7ReadBundleTests.test_c7_1_foc_bank_has_thirty_nine_executable_reader_vectors`;
it exited 0: **17/17** in **7.989 seconds**. Its final member executes the 39
local C7.1 reader sub-vectors; it is not an external the architect execution.

The controller separately reported the upstream pre-C7.1 Puddle baseline
`python3 validate_bundle.py` as **Schemas 7**, **Examples checked 39**,
**failures 0**. That upstream 39-example result is not this local C7.1
39-reader bank, does not exercise `read_c7_1_bundle`, and is not a the architect,
publication, activation, or external-consumer gate.

This remains a SHA-less shared-worktree checkpoint for controller review. No
commit, push, bus message, the architect gate, publication, deployment, activation, or
external-consumer acceptance is claimed.

## 2026-08-08 Item 10 review fix round 1

This append records five independently reproduced Item 10 review findings and
one documentation Minor. It does not rewrite the earlier controller
clean-candidate closure, claim a new candidate identity, or claim a commit,
push, publication, deployment, activation, or the architect final gate.

### RED-to-GREEN repairs

- **I1 dead-holder full-run projection.**
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-events-red python3 -m unittest -v tests.test_gauntlet_fuzz.ReaderFuzzGauntletTests.test_dead_holder_send_projects_over_a_complete_foc_run_prefix`
  failed as expected: **1/1** in **0.017 seconds**, because an otherwise
  lawful FOC prefix beginning with `run_created` was rejected as
  `record_kind_invalid`. `EventLog._dead_holder_state` now validates the
  complete `RUN_KINDS` ledger and then filters only matching
  `supervisor_orphaned` evidence; it does not relax malformed-frame integrity.
  The matching focused GREEN was **2/2** in **0.020 seconds**.
- **I2 command-root precedence.**
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-cli-red python3 -m unittest -v tests.test_cli.SlipCliTests.test_init_refuses_a_legacy_positional_root_even_when_environment_is_set`
  failed as expected: **1/1** in **0.131 seconds**; positional `init` chose a
  legacy root ahead of `SLIP_BUS_ROOT`. `init` now has only `--root`, so every
  entry point observes ruled `--root` then `SLIP_BUS_ROOT` precedence with no
  positional or configuration fallback. The focused precedence/root bank was
  **7/7** in **0.532 seconds** and the CLI/workflow/watch/phase-one/copy bank
  was **48/48** in **8.087 seconds**.
- **I3 semantic retraction replay.**
  The cursor RED
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-cursor-red python3 -m unittest -v tests.test_cursor.SparseCursorTests.test_forged_nonparty_retraction_fails_closed_before_ack_mutation`
  failed as expected: **1/1** in **0.008 seconds**. The writer RED
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-retract-red python3 -m unittest -v tests.test_registry_events.RegistryEventTests.test_retract_fails_closed_on_a_prior_nonparty_retraction_before_append`
  failed as expected: **1/1** in **0.004 seconds**. `SparseCursor` now consumes
  `EventLog`'s canonical semantic event records, and `EventLog.retract` replays
  those records within its existing transaction decision closure before it can
  append. A prior schema-valid wrong-party retraction now fails closed as
  `message_retraction_party_invalid` without acknowledgment or retraction
  mutation. The focused cursor GREEN was **3/3** in **0.027 seconds**; the
  event/cursor/fuzz/process/concurrency compatibility bank was **45/45** in
  **9.745 seconds**.
- **I4 executable literal-kind coverage.** Recovery RED
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-recovery-coverage-red python3 -m unittest -v tests.test_gauntlet_recovery.RecoveryGauntletTests.test_hm3i_retry_and_foc_reopen_with_stable_physical_ids_and_observation`
  failed as expected: **1/1** in **0.062 seconds**; time RED
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-time-coverage-red python3 -m unittest -v tests.test_gauntlet_time.TimeHostilityGauntletTests.test_hm3i_run_projection_is_timestamp_invariant_for_contract_retry_and_foc_traces`
  failed as expected: **1/1** in **0.085 seconds**. Test-only fixtures now
  derive each axis from real owner-built success, retry/stale, all three
  cancellation adapter, and FOC traces. Crash, fuzz, time, recovery, and
  twelve-process contention each observe all 26 literal `RUN_KINDS`; time
  rewrites cancellation testimony and compares timestamp-invariant physical
  projection. GREENs: time **1/1** in **0.103 seconds**, recovery **1/1** in
  **0.098 seconds**, fuzz **1/1** in **0.116 seconds**, crash **1/1** in
  **1.433 seconds**, and actual twelve-process all-family contention **2/2**
  in **0.845 seconds**. The complete six-suite gauntlet is **44/44** in
  **12.214 seconds**.
- **I5 TD1 zero-state `init`.** The CLI RED
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-init-zero-cli-red python3 -m unittest -v tests.test_cli.SlipCliTests.test_init_rejects_invalid_solo_inputs_before_creating_a_root`
  and fuzz RED
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-init-zero-fuzz-red python3 -m unittest -v tests.test_gauntlet_fuzz.ReaderFuzzGauntletTests.test_hostile_init_solo_inputs_leave_a_nonexistent_root_at_zero_state`
  each had **1 test with 2 expected subfailures**, in **0.224 seconds** and
  **0.150 seconds** respectively: hostile `--solo` or invalid `--harness`
  left a newly created root. Shared lexical solo bootstrap validation now runs
  before `create=True` root resolution. GREENs: CLI **1/1** in **0.220
  seconds** and fuzz **2/2** in **1.338 seconds**.

### Coverage, publication boundary, and remaining nonpass

- The 26 run kinds and frozen Item 5 outcomes are checked against the actual
  physical fixture union for every Item 10 gauntlet axis. The Item 7 policy,
  Item 8 admission, and Item 9 decision/capsule checks remain auxiliary
  evidence surfaces, not new run kinds. Stable physical order, FOC orphan
  authority/classes, frozen vocabulary, and timestamp-invariant state
  selection remain explicit.
- **M1 addressed (documentation only).** `SPEC-DRAFT.md` now names the exact
  Item 9 coordinate, closed source taxonomy and injected document resolver,
  `operator|architect` terminal authority, optional same-repository
  task-contract proof with legacy fail-closed behavior, full-record digest,
  and accepted-only physical-order capsule. It makes no Item 11 bundle or
  projection claim. Its direct HM3I/copy check was **5/5** in **0.046
  seconds**.
- The review-round architecture-contract command ran **327** tests in
  **16.535 seconds**: **326 passed**. Its only failure was
  `tests.test_source_scrub.SourceScrubTests.test_generated_repository_artifacts_are_scrubbed`, reporting exactly
  `['.worktrees/hm3i/HM3I_BRIEF.md']`. This shared-checkout artifact is the
  same boundary previously isolated by the controller clean candidate; it is
  not counted as green or repaired here. Final selftest/static results are
  appended only after this evidence text is itself checked.

### Post-evidence verification

- The same architecture-contract bank, after the M1 document assertion and
  this append, ran **328** tests in **16.116 seconds**: **327 passed** and the
  sole failure remained the exact generated-tree path above.
- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-final-selftest-final python3 -m slip.selftest`
  ran **530** tests in **31.900 seconds** and exited **10**: **529 passed** and
  the sole failure was the same generated-tree scrub. It did not emit and does
  not justify a `bundle_verified` completion claim.
- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-r1-final-doc-scrub python3 -m unittest -v tests.test_manifest tests.test_source_scrub tests.test_hm3i_contract`
  ran **18** tests in **0.512 seconds**: **17 passed** and that same scrub
  assertion failed. `python3 -m compileall -q slip tests` exited 0; direct
  `verify_manifest(Path.cwd())` returned `[]`; generated-tree scan returned
  only `['.worktrees/hm3i/HM3I_BRIEF.md']`; history-note scan returned `[]`;
  and `git diff --check` exited 0.

The shared generated-tree nonpass remains an explicit controller action, not
an Item 10 product alteration: prepare and verify a clean current candidate
under the governing process before any separate disposition. No commit, push,
publication, deployment, activation, or the architect final gate is claimed here.

## 2026-08-08 Item 10 review fix round 1 — adjacent harness zero-state

This append records the adjacent re-review finding without revising the prior
round-1 evidence. On a nonexistent direct home,
`python3 -m slip init --root <root> --solo=valid-node --harness=$'bad\x1brole'`
returned `role_invalid` only after creating `registry/entries.jsonl.lock`.
The root cause was a length-only solo/registry preflight while the durable
`registry_entry.role` boundary separately rejected terminal-unsafe Cc/Cs/Bidi
characters.

- RED:
  `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-harness-zero-red python3 -m unittest -v tests.test_cli.SlipCliTests.test_init_rejects_terminal_unsafe_harness_before_creating_a_root tests.test_gauntlet_fuzz.ReaderFuzzGauntletTests.test_terminal_unsafe_solo_harnesses_leave_a_nonexistent_root_at_zero_state tests.test_registry_events.RegistryEventTests.test_terminal_unsafe_role_refuses_before_the_registry_ledger_exists`
  ran **3** tests with **6 expected failures** in **0.217 seconds**: control
  and Bidi harnesses created the direct home, and direct registry registration
  created its ledger directory before refusing.
- Repair: one `records.validate_role` lexical boundary now backs durable
  `registry_entry` validation, `Registry.register` preflight, solo bootstrap,
  and persisted solo configuration validation. It preserves valid role strings
  and fails unsafe role input before any root or registry mutation.
- GREEN: the same focused command passed **3/3** in **0.230 seconds**. The
  requested CLI/fuzz/solo/registry/record/copy/manifest bank had **91** product
  passes; its only initial failure was the expected three changed deployable
  manifest digests. After rehashing `slip/records.py`, `slip/registry.py`, and
  `slip/solo.py`, `tests.test_manifest` passed **10/10** in **0.024 seconds**
  and direct manifest verification returned `[]`. Complete Item 10 gauntlets
  passed **45/45** in **12.049 seconds**.

Final broad-bank and selftest results are appended after this evidence text is
checked. This is local implementation evidence only: no commit, push, bus,
publication, deployment, activation, candidate identity, or the architect final gate
is claimed.

### Post-evidence verification

- The architecture-contract bank ran **331** tests in **17.179 seconds**:
  **330 passed** and the only assertion failure was the known shared
  generated-tree path `['.worktrees/hm3i/HM3I_BRIEF.md']`.
- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-item10-harness-final-selftest python3 -m slip.selftest`
  ran **534** tests in **34.634 seconds** and exited **10**: **533 passed** and
  the sole failure was the same generated-tree scrub. It did not emit or
  justify `bundle_verified`.
- `python3 -m compileall -q slip tests` exited 0; direct
  `verify_manifest(Path.cwd())` returned `[]`; generated-tree scan returned
  only `['.worktrees/hm3i/HM3I_BRIEF.md']`; history-note scan returned `[]`;
  and `git diff --check` exited 0.

The outstanding generated-tree result remains controller-owned clean-candidate
work. No commit, push, bus, publication, deployment, activation, or the architect
final disposition is claimed by this append.

## 2026-08-08 Item 10 final controller exact-clean closure

This appends the controller's clean-candidate result after the adjacent harness
zero-state evidence. The controller prepared
`<temp>/slipway-hm3i-resume.LhUPUF` from the supplied base and the final
uncommitted candidate changes; this lane did not create a commit or candidate
identity.

- `PYTHONPYCACHEPREFIX=<temp>/slipway-hm3i-controller-item10-final-selftest python3 -m slip.selftest`
  passed **534/534** in **37.828 seconds** and emitted
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- The controller's clean focused CLI/fuzz/registry/record/manifest/source bank
  passed **80/80** in **12.148 seconds**. `scan_generated_tree` returned `[]`,
  direct manifest verification returned `[]`, and the candidate diff check was
  clean.
- Final independent review recorded **109/109** plus **80/80** passing and
  `APPROVED_SCOPED` with **0 C / 0 I / 0 M**.

This is controller-provided exact-clean local evidence only. It does not claim
a commit, SHA, push, publication, deployment, activation, or the architect final gate;
Item 11 and post-HM3I scope remain fenced.

## 2026-08-08 post-HM3I Item 1 — C7.2 segment amendment

This append records the authenticated additive C7.2 segment amendment on the
exact `225610134a9f87bfd1673ac3433f173f121b724b` base. It does not alter the
frozen version-zero or C7.1 package bytes and does not claim publication,
activation, external-consumer acceptance, or a the architect final publication gate.

### RED and repair evidence

- The initial C7.2 package/reader bank ran **13** tests: **1 passed**, **1
  failed**, and **11 errored** on the intentionally absent v1 writer selector,
  `slip.c7_2_bundle` module, C7.2 package, and v1 binding schema.
- The initial record/schema seam ran **6** tests: **1 passed**, **1 failed**,
  and **4 errored** on the same missing v1 contract. The implementation then
  admitted schema version one only for `attempt_harness_session_bound`, kept
  legacy writer calls on version zero, and enforced explicit segment identity,
  kind, root/transition shape, uniqueness, attempt scope, and earlier physical
  predecessor position.
- Independent review produced three reproducible Minor findings. Exact ordinal
  validation RED ran **1** test with **3 failures** because `True` and `1.0`
  passed candidate and replay boundaries. Catalog-completeness RED failed
  **1/1** after removing the `run` family. Reader-upgrade RED ran **1** test
  with **2 failures** because `1` and `1.0` compared equal to `true`. Repairs
  require exact non-boolean integer ordinals, compare the complete normalized
  C7.2 catalog to the frozen C7.1 inventory plus the one ruled v1 source, and
  require the upgrade boolean by identity.

### Final local evidence

- Hostile C7.2 coverage passed **13/13**, including truncated and non-UTF8
  frames, hostile segment fields, timestamp inversion versus physical order,
  and a bounded concurrent post-capture append.
- Independent terminal re-review passed its focused C7.1/C7.2,
  record/schema, and manifest bank **107/107** in **4.084 seconds** and returned
  `APPROVED_SCOPED` with no remaining Critical, Important, or Medium findings.
- `PYTHONPYCACHEPREFIX=<temp>/slipway-c72-final-selftest python3 -m slip.selftest`
  passed **594/594** in **40.691 seconds** and emitted
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- Direct manifest verification returned `[]`; generated-tree and history-note
  scrubs each returned `[]`; `git diff --check` passed; and the exact diff
  against the base under `schemas/v0/` and `bundle/c7.1/` was empty.

This remains local engineering evidence. Commit, push, bus checkpoint,
publication, deployment, activation, and external-consumer proof are reported
only if separately completed.

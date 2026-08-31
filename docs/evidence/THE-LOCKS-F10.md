# THE LOCKS F10 evidence

Date: 2026-08-26 (America/New_York)

## Scope and identity

- Repository: `Land-o-Clusters/floati`
- Branch: `codex/the-locks-f10`
- Verified pre-evidence candidate: `9ccceae3e78b041a87446b32d2e1eab979103e47`
- Candidate remote parity: local and `origin/codex/the-locks-f10` both resolved
  to that exact 40-hex commit before this evidence file was added.
- Architect ruling: `0fc8d2d6d52a21cb21e014b40317421ad54e2624`
- Puddle charter source: `a4916519f8c18328b4032e216bd93caa7bc487ac`
- Puddle `docs/reference/THE_LOCKS.md` blob:
  `28d7f917aee5bb4e349bc2052abe597d9aa42481`
- Original Floati delivery-doctrine blob at
  `8465d7a2c8d1e5df1dae935916813a9b331ab7a3`:
  `24748a97faaecda99cf520c633a65eb9192779f6`
- Candidate delivery-doctrine blob:
  `24748a97faaecda99cf520c633a65eb9192779f6`

This document cannot contain its own commit identity. The governed fleet
receipt sent after commit and exact-head re-verification binds the final pushed
SHA.

## Delivered lock sequence

1. LK-1: append-only lock/car ledger, pure physical replay, atomic expiry
   escalation plus pending outbox, explicit stop/re-arm/delivery receipts, and
   no silence inference.
2. LK-2: read-only worktree cleanup eligibility, canonical common-Git identity,
   and refusal naming every commit reachable only from the candidate worktree.
3. LK-3: full-ref-only cars, content witnesses, base-bound review
   re-derivation, ranked blocker-skipping selection, post-executor landing
   verification, and product-ref dissolution.
4. LK-4: controller-owned staged provisioning, reverse rollback including a
   partially failing hook, absence verification, external/symlink resource
   refusal, and seat testimony only after resources publish.
5. LK-5: exact-recipient review handoffs with immutable car/ref/base/witness
   binding and explicit pending, stopped, re-armed, and receipt-delivered rows.
6. DARK fence: the candidate `locks` CLI is refused, README command blocks do
   not resolve it, `bundle-manifest.v0.json` excludes `floati/locks/`, and
   public scripts do not resolve the package. `LOCKS_EXPECTED_WIRED` remains
   exactly `False`; `floati/locks/WIRING.md` names the separate future seam.

## RED-first and perturbation receipts

- LK-1 began with an absent `floati.locks` import; escalation crash injection
  proved neither holder nor announcement survived the pre-append crash.
- LK-2 began with absent cleanup/Git observer modules and used a real detached
  worktree plus positive-control rescue ref.
- LK-3 began with absent queue module and real Git refs; SHA-different
  cherry-pick landing was accepted only by declared content.
- LK-4 began with absent provisioning module; half-failure and rollback-failure
  fixtures preceded implementation.
- LK-5 began with absent handoff module; delivery without a receipt remained a
  named refusal.
- The DARK test was deliberately perturbed to
  `LOCKS_EXPECTED_WIRED = True`; the exact test failed with observed
  `{cli: False, readme: False, bundle: False, scripts: False}`. Restoring only
  the constant to `False` made the test pass.
- Whole-branch review added and observed RED for Git reflog pseudo-refs,
  partially allocating failing hooks, symlinked external resources, and the
  deployable-manifest DARK exclusion before the fixes were applied.

## Verification receipts

The repository contains no `./run_tests.sh`. Per the ruled runner law, every
full-suite result below uses the explicit fallback:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

- Baseline at `27dffbe586637c72c841d23cc8688c51540d4d8a`:
  1,519 tests, 0 failures, exit 0, 228.103s.
- First feature-head full run: 1,550 tests, exit 1, with one failure and three
  dependent errors. Root cause was `tracked_set_mismatch`: the manifest's
  deployable-set scanner treated the deliberately DARK package as shipped.
- After the RED manifest exclusion test and fix, exact candidate
  `9ccceae3e78b041a87446b32d2e1eab979103e47` ran 1,551 tests with 0 failures,
  exit 0, in 283.171s.
- Focused Locks plus public-source/copy gate at the reviewed candidate:
  54 tests, 0 failures, exit 0, in 7.476s.
- Manifest, committed-tree demo capture, and DARK regression gate after the
  manifest commit: 36 tests, 0 failures, exit 0, in 9.936s.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m floati.selftest` initially exposed
  three non-repeating short-deadline failures across two verbose runs. Each
  exact failed selector passed direct repetition (the first four times; the
  later two together once). The unchanged prescribed selftest was then rerun
  with only its verbose stream redirected to a temporary evidence log: 1,551
  tests, 0 failures, exit 0, 206.780s, followed by
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- `git diff --check`: exit 0 before every feature/review commit.

The sandbox-initialization denial text printed during several tests is expected
negative-fixture testimony; the terminal unittest summaries above are the
classification authority.

## Review disposition

The whole diff from `origin/main` through the candidate was reviewed against
the design and execution plan. Three important findings were repaired before
the green full run:

1. `refs/heads/main@{0}` could pass the lexical full-ref validator and select a
   reflog entry. The contract now closes Git ref grammar including `@{`, dot
   components, `.lock` suffixes, and trailing dots.
2. A hook that allocated and then raised was not in the rollback list. Hooks
   now enter the unwind set before `prepare`, and the regression proves the
   failing hook's own abort runs.
3. A relative resource manifest could name a symlink escaping staging. The
   controller now revalidates staging identity, rejects symlink components,
   requires the claimed resource to exist, and proves containment.

The manifest mismatch was then fixed by explicitly excluding the permanent
DARK prefix from the deployable-set calculation, with a repository regression
and refreshed digest for `floati/manifest.py`. No Critical or Important review
finding remains open in the F10 scope.

## Boundaries

- No public activation, live delivery transport, installation, deployment,
  merge, release, account/limit work, telemetry, or deletion was performed.
- Delivery/wake coverage remains declared pull-only and unprobed because the
  package is DARK. Unit testimony is not a live delivery claim.
- The branch is pushed for architect consumption and is intentionally preserved
  as a named linked-worktree branch; no PR or merge was inferred.

---

# GATE — F10 @ `aeb7cb79`: PASS, DARK, COMPLETE (the architect, 2026-08-26)

**All four R-bindings verified, every count my own run, and the lane's acceptance caveat DISSOLVED
under measurement rather than waved.**

- **R1** — doctrine content identity proved at the BLOB: `24748a97…` at the original `8465d7a2` and
  identical on this branch. The binding was "content-identical at merge, say so in the receipt"; the
  receipt cited the blob hash unprompted — the strongest possible form of saying so.
- **R2** — the law's fallback runner used and named; my run: **1,551/1,551 OK**.
- **R3** — dark from the public side: no CLI verb, README, or Makefile surface names the package;
  `floati.locks` imports as a private package; `WIRING.md` present.
- **R4** — both bindings are TESTS, not prose: `test_crash_after_holder_selection_persists_neither_
  holder_nor_obligation` (the atomicity crash-injection, exactly as bound) and
  `test_expired_escalation_changes_holder_and_exposes_pending_announcement` (the projection-visible
  debt); handoff recipient-identity refuses fail-closed WITHOUT appending. Ledger 7/7, LK battery
  31/31, my runs.
- **THE CAVEAT, DISSOLVED BY MEASUREMENT:** the lane reported `floati.selftest` variably red in
  pre-existing effect-reconciliation timing tests. My run on a quieter machine: **1,551 OK +
  `bundle_verified`.** Third load-sensitive timing family surfaced tonight (Puddle's Codex cadence,
  the BSD CPU-stat contract, and this) — the pattern is the machine under fleet load, not the trees.
  Each repo's hardening row covers its own.

F10 remains DARK per its own charter's phase order (observe, then refuse, then act) — the wiring
order's exception is recorded in W-0: the charter IS the dissolving condition here.

— the architect, 2026-08-26

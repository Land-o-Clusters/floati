# Post-v1 approval suspension evidence

Date: 2026-08-10
Branch: `codex/approval-suspension`
Task 5 starting HEAD: `081e8993f7f5e04236bfe05f6108f3cffdc00fa1`

## Implemented durable behavior

- Approval requests and decisions are exact-action-bound v1 records. A started attempt can durably suspend on its exact fence and workspace checkpoint, after which its old execution authority is durably released or confirmed inactive.
- Approval consumption requires the exact approved action, exact suspended scope and checkpoint, decision time testimony no earlier than the suspension, and a strictly newer live authority on the same subject. Scope-tampered and causally early decisions refuse without adding a second durable truth.
- The four new frame readers fail closed for malformed, truncated, duplicate-key, oversized, and non-UTF8 bytes. Hostile causal reorder, exact-ID divergence, request/decision tamper, old-authority replay, newer-epoch release attack, workspace reservation collision, raw private socket appends, and interrupted-response retries have lawful positive controls and byte-preservation assertions.
- The manifest is mechanically regenerated from the sorted deployable set and current SHA-256 bytes. It includes `<retired>/suspension.py` and the four approval request, decision, suspension, and consumption schemas. `bundle/c7.1`, `bundle/c7.2`, and `schemas/v0` remain byte-identical to `4f1de17f5e68b6bc792e6716e593ca0e90f7ac68`.

## RED evidence

No production or manifest file changed before these runs.

First test-only attempt:

```text
python3 -m unittest -v tests.test_approval_suspension tests.test_gauntlet_fuzz tests.test_gauntlet_crash tests.test_manifest
Ran 78 tests in 8.569s
FAILED (failures=4, errors=1)
```

The one error was a hostile-fixture construction defect: an active-old-authority mutation retained `released_at` and failed strict record validation with `release_state_invalid`. It was corrected test-only and was not relabeled as a product failure.

Corrected hostile RED:

```text
python3 -m unittest -v tests.test_approval_suspension tests.test_gauntlet_fuzz tests.test_gauntlet_crash tests.test_manifest
Ran 78 tests in 8.611s
FAILED (failures=4)
```

The four intended failures were: broadened decision scope accepted; a decision earlier than its durable suspension accepted; all five approval-suspension manifest assets absent; and repository manifest currency reporting `tracked_set_mismatch` plus stale digests for `<retired>/approvals.py`, `<retired>/planes.py`, `<retired>/records.py`, `<retired>/runtruth.py`, and `<retired>/sequencer.py`.

After the narrow controller correction and mechanical manifest regeneration, the same hostile bank ran 78 tests in 8.531s and passed.

## Ordered verification

1. Required focused bank: 230 tests in 14.681s, `OK`.
2. Full `python3 -m unittest -q`: 813 tests in 61.502s, `OK`.
3. `python3 -m <retired>.selftest`: 813 tests in 62.328s, `OK`, followed by `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`. An expected orchestration fault-drill child emitted a traceback while its enclosing test remained `ok`; this output is preserved rather than hidden or relabeled.
4. Direct manifest verification printed `[]` and exited zero.
5. Source scrub: 8 tests in 0.222s, `OK`.
6. Frozen `bundle/c7.1`, `bundle/c7.2`, and `schemas/v0` diff exited zero.
7. `/usr/bin/git diff --check` exited zero.

## Gates not executed or claimed

- Current Codex continuation is checkpoint restart, not native continuation.
- Claude continuation is unsupported.
- Live provider parking and native provider continuation were not executed.
- No effect confirmation exists yet.
- No activation, publication, merge, or release occurred.

## Final whole-review fix wave

Final-fix starting HEAD:
`c077b1c395752d93191ba6d4c8a87d9777232f6c`.

The final whole-item review found two defects. First, a resumed attempt accepted
worker-receipt testimony from its pre-suspension authority epoch because result
and acceptance projection checked only item and node identity. Second, exact
simultaneous suspension or consumption callers generated different record IDs
before the run-writer lock, so the loser received a duplicate refusal instead
of the winner's durable record.

### Final-wave RED

The focused baseline passed 20/20 before any final-wave edit. Four test controls
were then added without changing production: a complete resumed-result and
acceptance positive using the consumption's exact authority subject/epoch, an
old-epoch refusal, and deterministic two-contender exact suspend and consume
races.

```text
python3 -m unittest -v \
  tests.test_approval_suspension.ApprovalSuspensionProjectionTests.test_resumed_result_accepts_receipt_from_exact_consumed_authority \
  tests.test_approval_suspension.ApprovalSuspensionProjectionTests.test_resumed_result_refuses_pre_suspension_authority_receipts \
  tests.test_approval_suspension.ApprovalSuspensionControllerTests.test_concurrent_exact_suspend_returns_one_durable_record_to_both_callers \
  tests.test_approval_suspension.ApprovalSuspensionControllerTests.test_concurrent_exact_consume_returns_one_durable_record_to_both_callers
Ran 4 tests in 0.095s
FAILED (failures=3)
```

The exact-new-authority positive passed. Unchanged production accepted the
epoch-7 pre-suspension receipt after epoch-8 consumption, and the two exact
races each produced one success plus respectively
`attempt_suspension_duplicate` and `approval_consumption_duplicate`. Each race
still had exactly one durable record.

### Final-wave correction

- Result and acceptance projection now requires every worker receipt used
  after consumption to repeat the durable consumption's exact
  `resume_authority_subject` and `resume_authority_epoch`. Ordinary
  pre-suspension results remain lawful and suspended attempts remain fenced.
- Suspension append now performs semantic existing-record resolution inside
  the existing run-store writer transaction. Exact contenders receive the same
  durable record; changed coordinates retain the existing typed divergence
  refusals. Managed service reconstruction forwards the same capability-gated
  resolver.
- The transaction-local resolver reads only the replayed run projection. Run
  append/fsync completes and the run lock is released before authority-tail
  observation or release. Concurrent release of the same old epoch is treated
  as idempotent only after the durable authority tail is re-confirmed.
- No v0 or suspension record field changed. No second ledger, response cache,
  receipt family, poller, wake record, or widened raw append surface was added.
  The deployable manifest changed only the SHA-256 entries for the three
  modified runtime files.

### Final-wave verification

1. Exact focused GREEN: 4/4 in 0.095 seconds, `OK`.
2. Task 3 affected bank: 107/107 in 4.268 seconds, `OK`.
3. Task 4 affected bank: 138/138 in 10.545 seconds, `OK`.
4. First hostile-bank attempt: 82 tests in 8.847 seconds, four manifest-only
   failures for the three changed runtime digests and aggregate manifest
   currency. All 78 non-manifest hostile tests passed. This attempt is retained
   as failed evidence.
5. Hostile bank after mechanical digest refresh: 82/82 in 8.748 seconds,
   `OK`.
6. Combined affected bank: 234/234 in 14.809 seconds, `OK`.
7. A first quiet full-suite invocation returned no usable terminal exit receipt
   and is `UNVERIFIED`; it is not counted. Fresh full suite: 817/817 in 61.316
   seconds, `OK`.
8. Selftest: 817/817 in 61.730 seconds, `OK`, followed by
   `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
9. Direct manifest verification printed `[]` and exited zero.
10. Source scrub: 8/8 in 0.229 seconds, `OK`.
11. Frozen `bundle/c7.1`, `bundle/c7.2`, and `schemas/v0` diff from
    `4f1de17f5e68b6bc792e6716e593ca0e90f7ac68` exited zero.
12. `/usr/bin/git diff --check` exited zero.

The external truth boundary is unchanged: Codex continuation is checkpoint
restart, Claude continuation is unsupported, live provider parking/native
continuation was not executed, and no effect confirmation, activation,
publication, merge, release, push, or bus checkpoint is claimed.

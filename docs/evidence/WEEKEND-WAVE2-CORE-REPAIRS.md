# DRAFT - Weekend Wave 2 core repair evidence

Date: 2026-08-28

Branch: `repair/weekend-wave2-core-20260828`

Repair parent: `0701f4d88f5a1dfb638e3550b2b0e489bddcf894`

Dispatch: `msg-01a046107a2577b8af928dfab2ea2c83`

## DRAFT - Repair rows

1. Deleted the unreachable legacy `_uninstall` handler from `floati/cli.py`.
   The public parser remains owned by `floati.uninstall._handle`; no purge
   capability was restored to uninstall.
2. Changed the seat-fence committer email from a literal tenant to
   `<node>@<root.tenant_id>`. The test fixture now derives the valid address
   from its real direct-home root, so a different tenant cannot pass by
   repeating the production pin.

## DRAFT - RED and GREEN receipts

The tenant-derived test ran before the production fix. Two real validation
paths failed with `seat_fence_identity_mismatch`, demanding the stale literal
`lane-current@the fleet` instead of the fixture root's
`lane-current@fleet`.

After the minimal production correction:

- seat-fence tests: 4/4 green;
- uninstall tests: 6/6 green;
- combined CLI, seat-fence, and uninstall bank: 47/47 green; and
- manifest bank after regeneration: 25/25 green.

The first full discovery was intentionally run before manifest regeneration
and remained red: 1,817 tests in 194.568 seconds with six manifest-only
failures naming the two changed deployable files. Those failures are not
reported as passed. After manifest-last regeneration, the frozen candidate
ran 1,817 tests in 198.760 seconds with zero failures and zero errors, exit 0.

## DRAFT - Boundary closure

Final manifest SHA-256:
`121aaccf6aad824a6a5f874087caf133c0ca154979f0a27b3748f1c9600b567c`

No README, live bus root, hook registration, daemon state, wake state, flip,
release, or publication surface was changed by these repair rows.

## the architect GATE VERDICT: PASS AND MERGED (2026-08-28)

Re-derived, not read: `_uninstall` and every `purge` reference gone from
`floati/cli.py` (grep empty) · `seat_fence.py:45` derives
`{node}@{root.tenant_id}` and the fixture derives its address from a real
root with a non-production tenant — the RED ran before the fix, exactly
right · manifest exact after the merge auto-composed both regenerations
(verified, not assumed) · full suite at the merge tip: **1,817 tests, OK,
exit 0 (pipestatus-captured), frozen tree.** Both reconcile-gate repair rows
are closed. The DRAFT stamps in this doc are frozen evidence from before the
restamp wave and stay verbatim.

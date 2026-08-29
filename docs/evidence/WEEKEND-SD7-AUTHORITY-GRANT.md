# DRAFT — Weekend SD-7 authority grant evidence

Date: 2026-08-28

Lane: `lane-floati`

Branch: `repair/sd7-authority-grant-20260828`

Current-main refresh: `b22c2b1`

Cumulative prerequisite repair after rebase: `6f7712b`

## DRAFT — Contract delivered

The ruled `docs/design/authority-grant-brief-2026-08-28.md` now has both
public acts:

```text
floati grant --root ROOT --as GRANTOR --holder NODE --subject SUBJECT --epoch N
floati grant revoke --root ROOT --as GRANTOR --holder NODE --subject SUBJECT --epoch N
```

- The grantor must be an active node with the active shipped `architect` role
  record. A registered non-architect, a node without a role, and a fleet with
  no architect role all refuse `grant_requires_architect` with the documented
  `node role --template architect` remedy.
- Holder, subject, and epoch are exact. Identifier validation rejects wildcard
  and glob spellings before an authority row is appended.
- The first exact epoch is 1. The next grant must be the next epoch; it may
  supersede the active prior coordinate. Exact active-coordinate replay
  returns the existing record.
- Public grants reuse the existing bounded authority interval contract at its
  86,400-second maximum; no new unbounded authority shape was introduced.
- Revoke uses the same coordinate and architect gate, does not require the
  holder to remain active, and exact repeated revocation returns the durable
  released record.
- Manual `work claim` absence now names `(holder, subject, epoch)`. Inactive,
  expired, holder-mismatched, and epoch-mismatched refusals name the exact
  authority record; a post-revoke claim therefore names the revoking record.
- Solo bootstrap and orchestration-seeded authority paths are unchanged.
- `AGENTS.md` documents the ordered non-solo lifecycle. All eight commands in
  that example round-trip through the live parser.

All new help and refusal prose is `DRAFT - ` stamped and the generated copy
ledger matches it exactly.

## DRAFT — RED-first and regression evidence

- The first six-case SD-7 bank against production was RED: **5 failures and 1
  error**. It reproduced the missing command, coordinate-free
  `authority_missing`, absent AGENTS flow, absent architect gate, absent
  supersession, and absent revocation testimony.
- Completed SD-7 bank: **7 tests, OK in 0.146s**. It includes a true no-
  architect fleet and a non-architect revoke attempt with zero extra append.
- Existing authority/work/approval/effect/spawn/orchestration regression bank:
  **431 real tests passed**. The invocation also named a nonexistent
  `tests.test_solo` module; its loader error is not reported as execution or
  gate evidence.
- Post-rebase grant, purge, copy, manifest, help, and parser bank: **42 tests,
  OK in 3.046s**.
- The first current-main canonical run found four stale integration callers:
  three demo-capture tests still passed removed SD-4 `install --root`, and one
  governance assertion required obsolete literal DCO wording. Result:
  **2,042 tests, 1 failure, 3 errors in 208.481s**.
- Focused repairs: **27 tests, OK in 3.423s**. The capture script now invokes
  the live lifecycle parser, and the governance test proves the current
  `DCO sign-off` plus `git commit -s` contract without changing governance
  copy.
- Final canonical command:
  `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover` — **2,042 tests,
  OK in 209.330s, exit 0**.
- `git diff --check`: silent, exit 0.
- Manifest verification: `[]`.
- `bundle-manifest.v0.json` SHA-256:
  `879a8dd71dac0e805f5e461114667359555d5d3fd5ece99a5af511d46a8783aa`.

## DRAFT — Fences

No README file, hook registration, trust setting, release state, or public
activation was changed. No foreign-project artifact or literal was added.

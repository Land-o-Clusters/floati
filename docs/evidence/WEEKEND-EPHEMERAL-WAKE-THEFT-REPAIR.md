# Weekend ephemeral wake-theft repair evidence

Date: 2026-08-28

Branch: `repair/ephemeral-wake-theft-20260828`

Ruling source: `docs/design/ephemeral-thread-wake-theft-ruling-2026-08-28.md`
from `2e3e52b7ec7ced432720519fdf1b3ee89072cec2`

## Scope and fences

This row makes one exact harness session the armed actor for one mapped Codex
workspace. A co-resident, non-armed Stop invocation now exits successfully and
silently before daemon binding, pause-state lookup, breaker mutation, wake-hold
evaluation, or wake-attempt/exhaustion receipts. A legacy consented binding
with no session receipt is claimed atomically by its first organic Stop.

The row also adds explicit `wake arm` and takeover receipts, replaces exactly
one prior immutable Floati waiter block in place during installation, and
adds actor attribution to new acknowledgment receipts. Historical v0
acknowledgments remain readable; new acknowledgments are closed schema v1
records with `acting_session_id`.

The installer never adds a waiter to `SubagentStop` and its regression test
pins that hook collection unchanged. There is no README edit, flip, release,
publication action, or foreign-project artifact. Changed public help and
refusal strings remain `DRAFT - ` stamped.

## Durable contract

- `codex_wait_session_receipt` is append-only schema v1 under
  `receipts/codex-wait-session/<node>.jsonl`.
- The identity is the exact node, canonical workspace, workspace-map digest,
  current consent receipt, and `acting_session_id`.
- Initial explicit ownership records `operation=arm`; a replacement records
  `operation=takeover` and names its predecessor; legacy organic migration
  records `operation=claim`.
- A non-armed actor returns no receipt and appends no session row.
- Installer session idempotency is session-derived, so the same immutable
  bundle can be explicitly taken over by a newly launched session.
- New `ack_receipt` rows use schema version 1 and name the acting session.
  The validator and readers retain the exact v0 field set for historical rows.

## RED-first receipts

- Armed-session ledger tests initially failed because the contract and ledger
  did not exist. The closed v1 schema test separately failed with
  `armed-session ledger absent` until the schema landed.
- The live theft reproduction initially observed the non-armed session emit a
  wake/block response, and the breaker-isolation test observed its bytes
  change after non-armed invocations.
- Explicit arm and installer migration began with one CLI failure
  (`wake arm` was not a command) and one installer error (no `session_id`
  contract).
- A same-bundle/new-session installer test then exposed an idempotency conflict
  before the key was bound to the explicit session.
- Actor-bound acknowledgment began with two API errors (no
  `acting_session_id`) and one missing-schema failure.
- Independent review added three accepted REDs: map growth reopened organic
  claiming, a malformed single waiter match was overwritten instead of
  refused before writes, and whole-document hook serialization changed the
  byte spelling of `SubagentStop`.

## GREEN receipts before final freeze

- Theft and breaker pair: **2 tests, OK**.
- Waiter/wake-control/daemon bank: **48 tests, OK**.
- Arm, installer, waiter, wake-control, and daemon bank: **59 tests, OK**.
- Installer bank after same-bundle takeover repair: **5 tests, OK**.
- Cursor, CLI, schema, TUI, projection, snapshot, process-atomicity, wake-hold,
  and fuzz bank: **196 tests, OK** in 33.246 seconds.
- Post-review waiter, hook, CLI, schema, demo, copy, scrub, and naming bank:
  **160 tests, OK** in 27.006 seconds.

## Independent review

The read-only reviewer found two critical and five important closure issues.
Accepted findings were repaired as follows:

- Refresh onto current `origin/main` before the final frozen run, preserving
  the already-landed purge work.
- Add the missed demo acknowledgment actor and regenerate the deployable
  manifest last.
- Preflight hook JSON and strictly validate zero or one legacy Floati waiter
  block before installing a bundle, changing the workspace map, or appending
  consent/session receipts.
- Rewrite only the `Stop` JSON value span, preserving the original bytes of
  every sibling hook collection, including noncanonical `SubagentStop` bytes.
- Match prior session authority by canonical workspace across workspace-map
  digest evolution, so map growth cannot reopen first-organic claim.
- Bind installer arm idempotency to the exact session and add DRAFT-stamped
  `wake arm` root/subcommand help plus generated copy-ledger rows.

The reviewer also noted that the evidence document and same-bundle takeover
fix were absent from its initial snapshot; both were already present before
this disposition was written. Final readiness is determined only after the
current-main refresh and frozen verification below.

## Final frozen verification

- Current-main refresh preserved the landed purge repair; the first frozen
  discovery then ran **1,966 tests in 207.564 seconds** and failed only the two
  governed frozen-protocol rebaseline assertions. The two new v1 schemas had
  increased the measured inventory from 141 to 143 assets; no product test
  failed.
- The rebaseline was updated to the measured 143-asset path digest
  `e6eff4279c7b34f3058615f80300da3148adfab492120ec341b5f4317bebc856`
  and content digest
  `64e1595380d738d295fe9bc7bf505807d4b3d15c187b423ebf9cacf5b78e7567`.
- Final manifest bank: **25 tests, OK** in 0.111 seconds.
- Pre-rebase canonical discovery: **1,966 tests, OK** in 213.158 seconds.
- After linearizing the three lane commits onto the latest `origin/main`, the
  exact focused freeze ran **140 tests, OK** in 16.737 seconds and final
  canonical discovery ran **1,966 tests, OK** in 224.267 seconds, process
  exit 0. Expected refusal-path argparse/sandbox diagnostics and existing
  `ResourceWarning` lines appeared on stderr; unittest's authoritative verdict
  was `OK`.
- Final source/name/copy bank: **30 tests, OK** in 2.099 seconds.
- Direct manifest verification returned `[]`. The manifest SHA-256 is
  `a9e0e80f70f44c657519792fcc7a0458bd539d3a6969e140d9f2838a68824f2a`.
- The immutable waiter runtime bundle digest is
  `fedb63257007c5e12937339499ca1d1ccb97d6786ae90fae19713b9ae74f0766`.

## Activation boundary

This evidence banks the immutable-bundle and in-place hook-rewrite mechanism.
It does not claim a live user hook was rewritten or a running pre-install
session became reachable. Live activation requires the exact governed root,
hooks path, destination, and session identity and remains separately
receipted.

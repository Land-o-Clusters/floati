# Wake Identity Canonicalization Evidence

Date: 2026-08-25

Authority:

- Puddle ruling `docs/rulings/2026-08-25-one-seat-four-wake-identities.md` at `b6d7aed68165bd2cf503f0b86c539d80f42b7652`;
- dispatch `msg-01a03b497c477daf820cb0cf284b5d20`;
- duplicate release-blocker dispatch `msg-01a03b49e74f74aab7877a53762e17f1`.

## Pre-repair live measurement

Read-only inspection of `~/.floati-bus/puddle-fleet` found:

- 17 distinct registry lineages;
- 20 `receipts/wake-coordination/` directories at inspection time;
- exactly four coordinator names outside registry lineage:
  `alice_city`, `city`, `lane-puddle-city`, and `puddle-alice-city`.

This reproduces the ruled defect shape. The count differs from the ruling's 21
directories because one canonical registry identity had no coordinator at the
later inspection; the four unlawful aliases are unchanged.

## RED receipts

The first focused Python run executed four tests and failed all four:

- unregistered wake evaluation returned normally and minted `bob_alias`;
- unregistered delivery refused only after minting `recipient_alias`;
- unregistered acknowledgment reached `ack_recipient_mismatch` instead of the registry boundary;
- two worker sessions for one node acquired distinct coordination locks concurrently.

The vendored watcher verifier then reproduced the product-level failure with
two idle sessions for one repository-routed seat and one envelope:

```text
AssertionError: one envelope woke more than one session for one seat
2 !== 1
```

## Implemented contract

- `Registry.resolve_node_id` is the single node resolution function used by
  registration, send, delivery, acknowledgment, and wake entry points.
- Every mutation-capable consumer resolves an exact active registry identity
  before a coordination path is derived.
- All message sessions for one node share
  `receipts/wake-coordination/<node>/lane.lock`; session-specific delivery and
  acknowledgment ledgers remain distinct.
- `wake_attempt_receipt` is a closed schema-v1 action record carrying canonical
  node, acting session, nullable message-bound session, physical envelope IDs,
  decision receipt, idempotency key, and `woke|refused` outcome.
- A wrong session leaves durable `wake_envelope_not_owned` evidence and never
  acknowledges the envelope.
- The OpenCode watcher calls the canonical Floati executable for wake
  evaluation and action recording. It has no registry parser fallback.
- A delivery tombstone is written only after both the host prompt and the
  `woke` receipt are durable.
- Doctor emits `wake_namespace_registry_mismatch` when the coordination
  namespace is not a subset of complete registry lineage. Inspection is
  physically read-only.

## Focused GREEN receipts

- Identity, cursor, wake-hold, CLI, doctor, and source-scrub selection:
  100 tests, all passed.
- Central identity/lease regression selection: 75 tests, all passed.
- Wake attempt record selection: 2 tests, all passed.
- Doctor suite: 19 tests, all passed.
- Vendored watcher verifier: identity, delivery, exhaustion, and
  single-consumer all passed. Single-consumer measured one prompt, one
  `wake_attempt_receipt`, and canonical coordinator `alice-city`.
- Python compilation succeeded with cache routed to `/tmp`.
- `git diff --check` passed.
- Manifest focused checks passed after adding the new schema and updating exact
  deployable digests.

## Full-suite and reconciliation receipts

- Full repository suite: 1,526 tests executed, zero failures, `OK`.
- Repository self-test: 1,526 tests executed, zero failures, `OK`, followed by
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.
- The four exact legacy coordinator directories were moved without globbing or
  deletion to `/tmp/floati-wake-alias-quarantine-01a03b49`. Each
  quarantined directory contains its original zero-byte `default.lock`.
- A source-tree doctor re-read of the live bus root reported
  `wake_namespace_registry_subset` with 16 wake identities across 17 registry
  lineages. The doctor artifact remained globally degraded because the source
  tree was not yet committed and no installer destination was named; neither
  condition changes the namespace-subset result.

## Activation boundary

Repository implementation does not install or activate the watcher. Copying
the plugin and restarting OpenCode remain separate owner-tier actions. Until
that boundary is explicitly authorized and independently observed, this
evidence proves source behavior, not live activation or release readiness.

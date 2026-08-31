# Weekend train Car 2 — wake identity reconciliation

Status: **DRAFT — INTEGRATOR EVIDENCE, FABLE RESTAMP PENDING**

Date: 2026-08-27

## Identity

- Node: `lane-floati`
- Branch: `integrate/weekend-20260828`
- Ratified branch-cut base: `932e377e9b88d801dfd545e1c238c50af5ec58ba`
- Fleet operations source ref: `origin/lane/fleet-ops-window`
- Fleet operations source tip: `8ffcb4ac6a5a3f89e0d54b901da5c6387da6e71c`
- Canonical identity source ref: `origin/codex/wake-identity-canonicalization`
- Canonical identity source tip: `e5818a59ce8c67cb8b1d34883ecc13860b5bb399`
- Source common base: `a691f9b4ab294f8657484fe58700d406e85d007b`
- Fleet operations merge commit: `1cb4c3ee4232d008928ecd9cdd932ade0d2f2f4c`
- Canonical identity merge commit: `6d3517b57031f6d5c6bb4f5a0bb415d2b8474f2a`
- One-resolver repair commit: `9d854002fb05cfff6a43cc25c1fbb4efeb47344e`

The two source branches were merged in the ratified order: fleet operations
first, then canonical identity. Conflict resolution selected the canonical
registry-backed identity path, lane-level wake coordination lock, durable wake
attempt receipt, and canonical test testimony. No legacy wake log was retained.

## Reconciliation and RED-first repair

The first merged full suite exposed a real semantic overlap rather than a
manifest-only problem. The fleet merge had retained `canonical_active_node`
beside the canonical branch's `resolve_node_id`, leaving two registry identity
resolvers and changing the digest of `floati/registry.py`.

A focused structural test, `test_registry_exposes_one_canonical_node_resolver`,
was added first and observed failing. The unused legacy resolver was then
removed. The new test passed, and the merged send, delivery, wake, and
acknowledgment paths continue to use `Registry.resolve_node_id`.

The first default compile attempt was denied only because the process tried to
write Python bytecode beneath the user cache directory. Repeating the same
compile with `PYTHONPYCACHEPREFIX` confined to `/tmp` passed. This was a
cache-location denial, not a source defect.

## Executed verification

Focused merged bank after the one-resolver repair:

- Exact wake, delivery, registry, event, and manifest test modules plus the new
  structural test.
- Result: 164 tests, 0 failures in 30.178 seconds, exit 0.

Repository watcher receipt:

- `node scripts/bus-watch/verify-floati-bus-watch.mjs scripts/bus-watch/floati-bus-watch.ts all`
- Identity scenario: 1 prompt.
- Delivery scenario: 2 prompts, with `failure_unwound=true` and
  `retry_delivered=true`.
- Exhaustion scenario: 11 attempts, `retained_and_rearmed=true`.
- Single-consumer scenario: 1 prompt, 1 wake receipt, coordinator `alice-city`.
- Result: exit 0.

First full merged-tip suite before the repair:

- `python3 -m unittest discover`
- Result: 1,530 tests, 2 failures and 3 errors in 251.807 seconds, exit 1.
- Classification: every failure reported the same stale
  `digest_mismatch:floati/registry.py`; this led to the duplicate-resolver RED
  test and minimal repair above.

Final full merged-tip suite:

- `python3 -m unittest discover`
- Result: 1,531 tests, 0 failures in 262.834 seconds, exit 0.

Independent bundle self-test:

- `python3 -m floati.selftest`
- Result: 1,531 tests, 0 failures in 230.125 seconds, exit 0.
- Terminal receipt:
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.

Manifest-last gate:

- Regenerated `bundle-manifest.v0.json` mechanically from sorted
  `floati.manifest._deployable_paths(Path.cwd())` entries with SHA-256 digests,
  preserving all top-level fields.
- Regeneration produced no byte diff.
- Direct `verify_manifest(Path.cwd())` result: `[]`, exit 0.

## Fences

- No flip, publication, release, or owner-tier action occurred.
- No foreign-bus artifact was touched.
- No OpenCode restart occurred.
- No README edit was made by the integrator.
- This evidence copy remains DRAFT-stamped for Fable's gate.

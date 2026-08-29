# Weekend P0 — Floati Codex Stop waiter

Status: **DRAFT — INTEGRATOR EVIDENCE, FABLE RESTAMP PENDING**

Date: 2026-08-27

## Identity

- Node: `lane-floati`
- Branch: `integrate/weekend-20260828`
- Ratified branch-cut base: `932e377e9b88d801dfd545e1c238c50af5ec58ba`
- Car 2 banked tip: `6fa5af3f4ec867168b967316e117f8f4a99dbc20`
- P0 implementation commit: `94f19c9aacc9dc19ec6a2d9fbc29b81883671075`
- Live Floati root: `~/.floati-bus/puddle-fleet`

## RED-first safety proof

The first production edit followed two named tests and their observed failures:

1. `test_unbound_workspace_is_silent_instant_exit_without_node_state`
2. `test_waiter_never_opens_foreign_bus_root`

Both initially errored because `floati.codex_wait` did not exist. The minimal
participation boundary then made both green in 0.007 seconds. The final tests
prove that an unmapped workspace exits 0 with empty stdout and stderr in under
0.25 seconds, creates no node state, ignores ambient identity and transport
variables, and never opens a path beneath the injected foreign root.

Subsequent RED/GREEN cycles covered active-registry longest-prefix resolution,
missing consent, strict `deadline < timeout`, immediate wake, held retry,
deadline exhaustion, per-thread escape, the 20-per-minute breaker, additive
installation, and exact installer retry.

## Implemented contract

- `codex-wait/workspaces.v0.json` is the only workspace identity map. It is a
  closed, versioned document inside the explicit Floati root.
- The selected node is re-resolved through `Registry.resolve_node_id`, the same
  canonical resolver used by send, delivery, acknowledgment, and wake.
- No environment variable can supply identity or redirect the root.
- Each mapped root requires a current version-1 consent receipt bound to the
  exact workspace-map SHA-256 digest.
- The waiter uses `WakeHoldController.evaluate` for non-consuming decisions.
  It records `wake_attempt_receipt` only after the block decision has been
  written and flushed. It does not manufacture delivery or acknowledgment.
- A clean deadline appends a visible `codex_wait_exhaustion_receipt` with
  outcome `rearmed`. A held retry stays silent.
- A circuit breaker permits at most 20 invocations in 60 seconds. A per-thread
  escape marker is hashed beneath `state/codex-wait/disabled/` in the Floati
  root. Failure paths return no decision.

## Executed verification

Focused contract, schema, wake, and registry bank:

- 77 tests, 0 failures in 0.533 seconds, exit 0.

Focused rebaseline, waiter, installer, and schema bank:

- 18 tests, 0 failures in 0.228 seconds, exit 0.
- Repository source scrub: `[]`.
- Direct manifest verification: `[]`.

First full suite after adding three closed schemas:

- 1,547 tests in 231.025 seconds, exit 1.
- Exactly two failures: the frozen protocol inventory still named 122 JSON
  assets and the prior two measured digests.
- The measured inventory was rebaselined to 125 assets with path digest
  `6d4a92e7cd2ce16a6ac0fe54febcce2942e70184d38ad6dd872590f1620bb743`
  and snapshot digest
  `bc7d1d33ad07bfd6663a72fa16a03a9bbe8027d170e0cbfde4baa98a722e79f9`.

Final full suite:

- `python3 -m unittest discover`
- Result: 1,547 tests, 0 failures in 191.940 seconds, exit 0.

Independent bundle self-test:

- `python3 -m floati.selftest`
- Result: 1,547 tests, 0 failures in 208.205 seconds, exit 0.
- Terminal receipt:
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`.

Manifest-last gate:

- Added the waiter launcher and three versioned schemas to the deployable set.
- Regenerated `bundle-manifest.v0.json` mechanically after all deployable bytes
  were final.
- Direct `verify_manifest(Path.cwd())` result: `[]`, exit 0.

## Live install receipt

The first installation attempt was denied at the workspace sandbox boundary
before creating the destination. The separately authorized retry succeeded.

- Installer state: `installed`
- Installed bundle:
  `~/.codex/floati-wake/08265accd12167c9e2859ce9ddbed769a833223678c029b51c2cdcc22d483f81`
- Source bundle SHA-256:
  `08265accd12167c9e2859ce9ddbed769a833223678c029b51c2cdcc22d483f81`
- Installed bundle SHA-256:
  `08265accd12167c9e2859ce9ddbed769a833223678c029b51c2cdcc22d483f81`
- Command SHA-256:
  `e978233449383bb38968b304220a203cd791cde733b39445ff38b1eded021851`
- Hooks SHA-256 before:
  `8519032fdef4767155924410361acfdde455d075c2f36e8ae3a39e58a12f047d`
- Hooks SHA-256 after/readback:
  `96e9dbe0af9c16bd0482305b835cb2e151812daaeb9fcdd22411535006a59f21`
- Workspace-map SHA-256:
  `e3d2ac0452e6a578832dff5dcb0dfad952fbe1d3f8e818c923291c3282227508`
- Consent receipt:
  `codex-wait-consent-01a0455e62f87abc89804e769477eefe`
- Deadline/timeout: 1,700 / 1,800 seconds.

Readback found four Stop blocks total and exactly one Floati block. The three
pre-existing blocks remained in their original order and content.

## Live wake proof

The direct ping request to an unregistered display identity was correctly
refused as `unknown_recipient` without appending a message. The active architect
node then supplied the governed live ping.

- Ping envelope: `msg-01a0455f28207904b17fd18f6e946947`
- Dispatch: `2026-08-27T22:37:39.232Z`
- Installed wake path:
  `~/.codex/floati-wake/08265accd12167c9e2859ce9ddbed769a833223678c029b51c2cdcc22d483f81/scripts/floati-codex-wait`
- Decision receipt: `wake-hold-01a0455f831a721ea8ec9cb69cb2b4e8`
- Wake-attempt receipt: `wake-attempt-01a0455f84157d969a0c1b42e0d5c301`
- Wake receipt time: `2026-08-27T22:38:02.773Z`
- Observed dispatch-to-wake latency: 23.541 seconds, below the 1,700-second
  deadline.
- Later inbox delivery: `delivery-01a0455fb6fc7599bb397a3001bd7650` at
  `2026-08-27T22:38:15.804Z`.
- Ping acknowledgment: `ack-01a0455fd1837b1c96f03d9889ca25f2`.
- Reply-bound proof envelope: `msg-01a0455ff5a97af094854ef668a6a26a`.

The waiter observed and emitted the ping before the later inbox delivery. This
closes the hand-delivery era for `lane-floati`.

## Fences

- No flip, publication, release, or owner-tier restart occurred.
- No foreign-bus artifact, target, marker, or backup was modified.
- No OpenCode restart occurred.
- No README edit was made by the integrator.
- This evidence copy remains DRAFT-stamped for Fable's gate.

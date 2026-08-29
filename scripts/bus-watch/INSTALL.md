# floati-bus-watch — install, activation, verification

The puddle-fleet bus watcher: an OpenCode plugin that wakes a seat's session
when a floati envelope lands for it. **Floati's product is delivery between
agents; this is our own delivery component, vendored here so it has history,
diff, author and revert** (gate ruling 2026-08-23, Finding 3 — committed ≠
banked applies to infrastructure too).

## Files

- `floati-bus-watch.ts` — the plugin. Implements the EXHAUSTED-IS-NOT-DELIVERED
  ruling (`puddle docs/rulings/2026-08-23-exhausted-is-not-delivered.md`):
  exhaustion is backpressure with a visible receipt (D1/D2/D3), identity
  resolves by git-common-dir so worktrees match (D4), and delivery is recorded
  only after the prompt resolves (E1/E2). Superset of necro's repair and
  lane-floati's coverage.
  It also routes every wake through the installed Floati controller: registry
  resolution and the node-wide lane lease authorize at most one session, and a
  successful prompt must append a node+session wake-attempt receipt before the
  watcher writes its delivery tombstone.
- `verify-floati-bus-watch.mjs` — the REQUIRED runner for any future change to
  the plugin (lane-floati, gated 2026-08-23 19:46Z). Scenarios: identity ·
  delivery · exhaustion.

## Install

The canonical installed executable must exist at
`~/.local/share/floati/scripts/floati`, or `FLOATI_EXECUTABLE` must name the
exact regular launcher. A missing or refusing controller leaves the envelope
pending and emits `wake_refused`; the watcher never falls back to its own
registry parser.

    cp scripts/bus-watch/floati-bus-watch.ts \
       ~/.config/opencode/plugins/floati-bus-watch.ts

Activation requires an OpenCode restart (owner-tier: it kills every live seat
on the machine — Law 27).

## Verify (run before any gate request that touches the plugin)

    node scripts/bus-watch/verify-floati-bus-watch.mjs \
      scripts/bus-watch/floati-bus-watch.ts all

Expect identity, delivery, exhaustion, and single-consumer GREEN against the
repaired artifact. The single-consumer scenario uses two idle sessions for one
seat and one envelope and requires exactly one prompt plus one durable wake
receipt.

## Self-identifying liveness check

The repair renamed its failure event, so the journal answers "which version is
live?" without restarting:

    grep -c wake_failed_retryable \
      ~/.floati-bus/puddle-fleet-watch/logs/opencode_floati_bus_watch.jsonl

`wake_failed_retryable` present = repaired code running. Only plain
`wake_failed` = pre-repair code; do not trust exhaustion behaviour until the
restart lands.

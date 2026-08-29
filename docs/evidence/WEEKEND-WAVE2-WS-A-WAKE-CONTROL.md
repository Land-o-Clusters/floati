# DRAFT - Weekend Wave 2 WS-A exact-session wake-control evidence

Date: 2026-08-28

Branch: `reconcile/weekend-20260828`

Controller parent: `0e6797162b5325d919fba787e0f27b5bf27dee69`

## DRAFT - Contract landed

The public surface is:

```text
floati wake pause --root ROOT --as NODE --session SESSION
floati wake resume --root ROOT --as NODE --session SESSION
floati wake status --root ROOT --as NODE --session SESSION
```

Every invocation names one active registry node and one exact bounded session.
`all`, `global`, wildcard, traversal, empty, and omitted selectors refuse. There
is no global mode and no wildcard parser path.

Pause commits one node-scoped marker at
`state/wake-control/<node>/<session-sha256>.json` and appends one closed
`wake_control_receipt`. Resume appends its predecessor-bound symmetric receipt
and removes only that exact marker. Neither operation edits hook registration.
Status performs a read-only marker observation and reports either `active` or
`paused`; paused output says `DRAFT - paused by you at T` and never substitutes
absence or deafness. Status explicitly reports both the running session cache
and harness trust gate as `unknown`.

The Codex Stop waiter checks the node/session marker before breaker or delivery
evaluation. A paused session returns intentional silence without consuming
mail, appending wake evidence, or claiming a delivery outcome. The legacy
escape-marker path remains supported for already installed sessions.

WS-D boot prompts and `AGENTS.md` now carry the exact pause, resume, and status
verbs. All new visible copy remains `DRAFT -` stamped.

## DRAFT - RED and GREEN receipts

The mandatory RED was observed first: four imports failed with
`ModuleNotFoundError: No module named 'floati.wake_control'`, and the public CLI
refused `wake` as an unknown command.

Focused closure:

- 6/6 controller tests green, including exact-session isolation, closed schema,
  no-hook-mutation, wildcard/global refusals, waiter intentional silence, and
  real CLI artifacts;
- 51/51 controller + waiter + projection + record + schema + help bank green;
- 33/33 copy, manifest, controller, waiter, and projection bank green after the
  ruled 132-to-133 frozen protocol rebaseline; and
- repository-wide discovery: **1,801 tests in 201.919s, 0 failures, 0 errors,
  exit 0**; and
- design-row exact-candidate discovery: **1,801 tests in 188.882s, 0 failures,
  0 errors, exit 0**.

The full run printed the repository's known sandbox diagnostics,
ResourceWarnings, and one expected orchestrator child traceback
(`worker_claim_missing`); the authoritative unittest result remained `OK`.

## DRAFT - Manifest and boundary closure

Final manifest SHA-256:
`d7fff5b80ddf137b03fc66e8bb08e867cd79df2b5b95def6ba1fa9e853bd692a`

Source-tree scrub: `[]`, exit 0.

Git-history-note scrub: `[]`, exit 0.

No live session was paused or resumed by this implementation gate. No hook was
installed or edited, no README was changed, no foreign bus artifact was read or
written, and no flip, activation, release, or publication is claimed.

## DRAFT - Daemon design closure

`docs/design/wake-daemon-design-2026-08-28.md` is accepted as a later
implementation design, not as shipped capability. It specifies opt-in/off by
default consent, one root/node/harness owner coordinate, no listener or root
discovery, exact-session pause integration, delivery-truth receipts,
backpressure, per-harness no-fallback adapters, and a Cursor acceptance run
longer than 35 minutes with at least three complete deadline cycles.

The design explicitly records that sessions predating adapter install/trust may
require relaunch, and that a manual or synthetic wake row is not organic
longevity evidence. No daemon command, process, supervisor, consent record, or
live activation was implemented by the design row.

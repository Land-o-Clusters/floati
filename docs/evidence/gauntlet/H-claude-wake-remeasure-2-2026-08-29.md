# Claude/CLI wake re-measurement 2 (Fable, 2026-08-29)

Owner-ordered completion of the re-measure RP-3 Am.1 opened; supersedes the staleness of
`H-claude-wake-remeasure.md` (measured 2.1.231; could not run a live Claude probe).

## Measured, this machine, today

- **Version re-derived:** `claude --version` → `2.1.238 (Claude Code)`.
- **Surface enumeration at 2.1.238** (`--help`): `-p/--print` per-invocation path ·
  `--bg/--background` background agent · `--continue`/`--resume`/`--from-pr` session
  resume · hooks exist (named by `--bare`'s "skip hooks") · **no serve/listen/watch
  surface** — nothing a waiter can subscribe to from outside a session.
- **Live headless invoke exercised:** `claude -p … --output-format json` returned a TYPED
  refusal — `type: result, subtype: success, is_error: true`, "Failed to authenticate:
  OAuth session expired and could not be refreshed", session_id
  `ecca1b3d-c92e-47f8-846a-36f6078f77b2`, 4.1s. This reproduces the 08-28 gap at the
  current version and is a machine-state fact (CLI re-auth is an owner act), not a
  harness fact.
- **In-session event surface, adjacent row:** the architect session itself (desktop/agent
  surface — a DIFFERENT matrix row) held a persistent Monitor on the fleet bus all day and
  woke on 10+ real events with no daemon. It arms per session and dies with the session —
  the architect's own boot discipline re-arms it every boot.

## Conclusion

**claude / cli · wake = daemon — CONFIRMED at 2.1.238.** A cold seat has nothing external
to subscribe to; waking it means starting/resuming a process, which is a daemon's job (the
same shape the grok-build adapter binds). The in-session monitor is real and event-driven
while a session lives, and cannot substitute for cold wake. Grade: the surface enumeration
and the refusal are measured; the one unexercised probe is the *authenticated* headless
invoke — it runs the moment the owner re-auths the CLI, and its success/failure cannot
change the cell's value, only complete the daemon path's live certification.

## Completion (same day, post re-auth)

The owner re-authed the CLI and the authenticated headless invoke ran live:
`claude -p 'Reply with exactly: WAKE-PROBE-OK' --output-format json` → `is_error: false`,
result exactly `WAKE-PROBE-OK`, session `ec65bb57-09b8…`, 3224 ms. The daemon-shaped
invocation path (the mechanism the cell names) is now exercised end-to-end at 2.1.238.
**Grade: MEASURED.** The cell value is unchanged — daemon — and now carries live proof.

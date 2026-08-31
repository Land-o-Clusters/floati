# MX1 M4 — grok/cli wake measurement (2026-08-29)

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** grok / cli / wake — claimed `daemon`, grade `classified` at seed.
**Base:** harbor main `49d7ff8c`.

## Measured, this machine, today

- **Executable named and launched:** `/opt/homebrew/bin/grok` →
  `../lib/node_modules/@xai-official/grok/bin/grok`. `grok --version` →
  `grok 1.0.5 (5115b46bc909) [stable]` — same version the posture photograph classified.
- **Surface enumeration at 1.0.5** (`--help`, 171 lines, captured whole): interactive
  session with `[PROMPT]` · `-p/--single` "Single-turn prompt. Prints the response to
  stdout and exits" (plus `--prompt-file`, `--prompt-json`) · `--continue`/`--resume`
  session resume · `--worktree` · `--session-id`/`--fork-session`.
  **Zero matches for `hook`. Zero wake-relevant matches for `listen`/`serve`/`watch`** —
  the single grep hit is `mcp  Manage MCP server configurations` (the substring `serve`
  inside "server"), a config subcommand, not a subscription surface. Nothing exists for a
  waiter to subscribe to from outside a session.
- **Live headless invoke exercised:** `grok -p 'Reply with exactly: WAKE-PROBE-OK'`
  exited 1 in 0.66 s with a TYPED refusal on both streams: "Not signed in. To
  authenticate without a browser, run: grok login --device-code" (stdout names the
  XAI_API_KEY alternative). `~/.grok/` exists with prior session state — this is an
  expired/absent sign-in on the reference machine, a machine-state fact and an owner act
  to remedy, not a harness fact. (Same shape as the claude/cli re-measure: the
  unauthenticated refusal reproduces cleanly; only the authenticated probe is missing.)

## Captures (sha256-pinned, committed under `captures/mx1-grok-cli-wake/`)

- `grok-help.txt` a0e05fe31356e5c06d79354c19bc9a2d7a8abeaef6de15dc3287c7194a4cad9e
- `grok-version.txt` 8d21e37ac44a7c832d1be0ba517a6c47518ca703ba49cd868329e2652b55cef6
- `grok-probe-stdout.txt` e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 (empty — refusal wrote to stderr; retained as the honest empty)
- `grok-probe-stderr.txt` cf2314f13372137b01af5bae128d27ecbe9ccda4699b55a13f80a12fa2e99b9e
- `grok-probe-time.txt` 97bdeaadf0b28528bb62523dbdf2383484037712fa1d5df89566a4b07856c486

## Conclusion — TYPED ABSENCE, cell stamp UNCHANGED

**grok / cli · wake = daemon is CONSISTENT with everything measured** — a cold grok seat
has no external surface to subscribe to (measured zero at 1.0.5), so waking it means
starting/resuming a process, a daemon's job. **But the campaign contract requires the
wake path OBSERVED firing, and an auth refusal is not a wake that arrives.** Per the
brief's fence, this receipt records a **typed absence naming its prerequisite: the grok
CLI is not signed in on the reference machine; `grok login` is an owner act.**

**The cell stays `classified`.** No matrix edit rides this commit — a stamp that flips on
consistency instead of observation is fabrication. The completion probe is one command
once the owner signs in:

    grok -p 'Reply with exactly: WAKE-PROBE-OK'

A non-error response completes the daemon path live; its success or failure cannot change
the cell's value, only certify it. Completion lands as an addendum here plus the
`classified → measured` stamp edit and re-render, one commit.

## Completion (2026-08-29, post owner sign-in)

The owner authorized and completed the grok sign-in. The recorded completion probe ran
exactly as written: `grok -p 'Reply with exactly: WAKE-PROBE-OK'` → exit 0 in 7.6 s wall,
stdout exactly `WAKE-PROBE-OK`. Version re-derived unchanged: `grok 1.0.5 (5115b46bc909)`.
The daemon-shaped invocation path — the mechanism the cell names — is now exercised
end-to-end at the same version the surface enumeration measured.

Completion captures (sha256-pinned, same directory):
- `grok-probe-authed-stdout.txt` 16f18ee472b2ec9c305765d0d315ce162ae6f078bcfaa7a682995545aab6b8b9
  (byte-identical to the M1/M2 probe stdout — exactly `WAKE-PROBE-OK\n`, as expected)
- `grok-probe-authed-stderr.txt` e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 (empty)
- `grok-probe-authed-time.txt` b6efe0b04bb5129bc2f36c26e81d286c65085b9fbeee33d038ee6762d59099de
- `grok-version-recheck.txt` 8d21e37ac44a7c832d1be0ba517a6c47518ca703ba49cd868329e2652b55cef6

**Grade: MEASURED. grok / cli · wake = daemon at 1.0.5** — value unchanged, now with live
proof. The typed absence above is discharged; stamp edit rides this commit.

# MX1 M2 — cursor/cli wake measurement (2026-08-29)

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** cursor / cli / wake — claimed `daemon`, grade `classified` at seed.
**Base:** harbor main `d18d039f`.

## Measured, this machine, today

- **Executable named and launched:** `~/.local/bin/cursor-agent` → `2026.08.25-3e8eec8`.
  **Version drift from the classified cell** (2026.07.09-a3815c0) — the cell's `versions`
  field is updated to the measured version in this commit.
- **Surface enumeration at 2026.08.25** (`--help`, captured whole): `-p/--print`
  headless path with `--output-format` · `--mode plan|ask` read-only execution modes ·
  `--resume`/`--continue`/`resume`/`ls` session resume · `mcp` config subcommand.
  **Zero `hook` words in the CLI help** — the officially documented Cursor stop hook
  (cursor.com/docs/hooks.md, per the posture photograph) is a per-turn window, not a
  CLI-help surface, and nothing in the help lets a waiter subscribe to wake a COLD seat.
  **Workspace Trust is a live gate:** the first probe in an untrusted scratch directory
  was REFUSED with a typed prompt (captured; `--trust`/`-f`/interactive are the named
  exits) — the daemon path requires the target directory trusted first.
- **Auth state:** `cursor-agent status` reports logged in. The account identity string is
  deliberately NOT committed — owner-identity byte fence (CI-1 export train); the fact
  "logged in" is the measurement, the identity is not.
- **Live daemon-shaped invoke exercised:**
  `cursor-agent -p --output-format text --mode ask --trust 'Reply with exactly: WAKE-PROBE-OK'`
  in an empty trusted scratch directory → exit 0 in 12.7 s wall, stdout exactly
  `WAKE-PROBE-OK`. Honest noise, named: stderr logged one transient
  "Connection lost, reconnecting…" retry that succeeded.

## Captures (sha256-pinned, committed under `captures/mx1-cursor-cli-wake/`)

- `cursor-agent-help.txt` 7ae7f9bea180e909d3e39b3d240e4fcca560d7fe4774566f0c588d9340179f93
- `cursor-agent-version.txt` d78c1f837b9037628be50d2de8fbc667368cfbf02568d9f0182236c73bff3d0c
- `cursor-trust-refusal.txt` 2bd721ef8d82e9ee5ed8dc09362123c6b94a4f075f2c57d353d68021ea70e40d
- `cursor-probe-stdout.txt` 16f18ee472b2ec9c305765d0d315ce162ae6f078bcfaa7a682995545aab6b8b9
  (identical bytes to the M1 probe stdout — both are exactly `WAKE-PROBE-OK\n`; same hash
  is the expected consequence, not an error)
- `cursor-probe-stderr.txt` 48ed9f9e933cc59260ef9ddacc0b5c179742d2b774f58465507a3a4a67f93ad4
- `cursor-probe-time.txt` d2670fbba72ff5c41592ef35cd4d8cb07c0bb864e17784420acba2f410b06186

## Conclusion

**cursor / cli · wake = daemon — MEASURED at 2026.08.25-3e8eec8.** A cold cursor-agent
seat has no external subscription surface (zero hook words in the CLI; the documented
stop hook is per-turn; workspace trust gates every cold start); waking it means
starting/resuming a process, and the daemon-shaped invocation was exercised live
end-to-end with the probe answered exactly. Cell value unchanged — `daemon` — now with
live proof. Stamp edit rides this commit: `classified → measured`, receipt_path here,
`measured_at 2026-08-29`, versions updated to measured, grids re-rendered from the dataset.

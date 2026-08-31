# MX1 M1 — codex/cli wake measurement (2026-08-29)

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** codex / cli / wake — claimed `daemon`, grade `classified` at seed.
**Base:** harbor main `d18d039f`.

## Measured, this machine, today

- **Executable named and launched:** `/opt/homebrew/bin/codex` → `codex-cli 0.150.0`
  (same version the posture photograph classified).
- **Surface enumeration at 0.150.0** (`--help` + `exec --help`, captured whole): `exec`
  headless path with `--sandbox read-only|workspace-write|danger-full-access` and
  `--skip-git-repo-check` · `resume` session resume · `agents` "Browse all agent sessions
  on the shared local app-server daemon" · `app-server` (experimental) · `remote-control`
  (experimental app-server daemon management) · `mcp-server` (stdio) · hooks exist and
  carry a TRUST gate (`--dangerously-bypass-hook-trust` "run enabled hooks without
  requiring persisted hook trust"). The Stop-hook waiter was already fixture-proven
  3× `rearmed` in `H-wake-hook-run.json` (posture photograph): the hook window is
  per-turn deadline-re-arm, not a cold-wake subscription. Nothing here lets a waiter
  wake a COLD seat from outside — the app-server daemon is itself a resident process
  something must start, which is the daemon shape the cell claims.
- **Auth state:** `codex login status` → "Logged in using ChatGPT" (captured in probe
  environment; no credentials recorded).
- **Live daemon-shaped invoke exercised:**
  `codex exec --sandbox read-only --skip-git-repo-check 'Reply with exactly: WAKE-PROBE-OK'`
  in an empty scratch directory → exit 0 in 9.8 s wall, stdout tail exactly
  `WAKE-PROBE-OK`. Honest noise, named: stderr carries one unrelated ERROR from a
  configured MCP server (`mcp.cloudflare.com` AuthRequired — the owner's codex MCP
  config, orthogonal to the wake path and not consulted by the probe), plus
  "Reading additional input from stdin..." from the closed stdin.

## Captures (sha256-pinned, committed under `captures/mx1-codex-cli-wake/`)

- `codex-help.txt` e8ecd554e6e3e870a55e540f1a21598c085cfee237f9c735ff9b5ba4ac4cf08a
- `codex-exec-help.txt` e504bac5a6364566fbe408132dec7993639def9258ece34e8352f51f8d43687c
- `codex-version.txt` 22c3a470a09f98a30e23cb3eac0efb697a01d2ae8869f49660550ed599648961
- `codex-probe-stdout.txt` 16f18ee472b2ec9c305765d0d315ce162ae6f078bcfaa7a682995545aab6b8b9
- `codex-probe-stderr.txt` 598021245c9f1f3eebae6db064af173ee3b8806c3368efec76f8358b9ede5ce8
- `codex-probe-time.txt` 293b20bd886e0591f57b974fc9b19b89434315dcb39ba74782a287875999d481

## Conclusion

**codex / cli · wake = daemon — MEASURED at 0.150.0.** A cold codex seat has no external
subscription surface (the hook window is per-turn and deadline-re-armed; the app-server
is a resident process, not a cold-wake path); waking it means starting/resuming a
process, and the daemon-shaped invocation was exercised live end-to-end with the probe
answered exactly. The cell value is unchanged — `daemon` — and now carries live proof.
Stamp edit rides this commit: `classified → measured`, receipt_path here,
`measured_at 2026-08-29`, README + full grid re-rendered from the dataset.

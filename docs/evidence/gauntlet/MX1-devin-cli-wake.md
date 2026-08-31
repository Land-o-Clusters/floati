# MX1 M7 — devin/cli wake measurement (2026-08-29)

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** devin / cli / wake — claimed `event-driven`, grade `classified` at seed.
**Base:** harbor main `d18d039f`.

## Measured, this machine, today

- **Executable named and launched:** `~/.local/bin/devin` → `devin 3000.6.7 (260a97c8)`.
  **Version drift from the classified cell** (3000.2.17) — and the drift was OBSERVED
  LIVE: a probe ~30 minutes earlier in this same sitting reported `3000.6.2 (ce8ebcc1)`;
  the CLI self-updated between two invocations. The cell's `versions` field is updated to
  the version that ran the measurement.
- **Surface enumeration at 3000.6.7** (`--help` + `acp --help`, captured whole): zero
  `hook` words · `acp` — "Run as an ACP (Agent Client Protocol) server over stdio", with
  `--agent-type summarizer|review` · `plugins`, `mcp`, `cloud`, `list`/`rm` sessions,
  `ssh`. The ACP stdio server is the event surface the posture classified.
- **Auth state:** `devin auth status` → "Logged in (via Devin)" (fact recorded;
  credential output not captured).
- **The event-driven wake exercised end-to-end, push observed:** spawned
  `devin acp --agent-type summarizer` (the cheapest turn path), spoke JSON-RPC over
  stdio: `initialize` → full agentCapabilities · `session/new` → session granted with
  modes · `session/prompt` "Reply with exactly: WAKE-PROBE-OK" → **145 server-initiated
  JSON-RPC notifications** (no `id` — true pushes) streamed over the living channel:
  `session/update` config/mode/command updates and `agent_message_chunk` frames, the
  probe text arriving verbatim in a pushed chunk, terminal `stopReason: end_turn` in
  1.4 s with usage 1319 tokens (deliberately tiny — one summarizer turn was the whole
  spend).
- **Scope honesty:** this proves the event channel for the PROCESS LIFETIME — a client
  attached to a living `devin acp` process is woken by pushes. A cold devin seat has
  nothing running to push, which is what `event-driven` has always scoped in this matrix
  (same qualifier as t3/cli).

## Captures (sha256-pinned, committed under `captures/mx1-devin-cli-wake/`)

- `devin-help.txt` 9049c5f488686c2493c3caff5929d6ce50c359cd1f428b344d0d4ff660fdcfc5
- `devin-version.txt` 6a75269dee07bc3157c6b097c8c0efb9571fc650ffb49ca10409cf7aa6ce9a49
- `devin-acp-help.txt` c38dd13a69e87ca7a6a9e7fafaa5027ca9696b0900a23d5306623ddaf8ea3f92
- `devin-acp-handshake.txt` 031fd7230dd9a15263265eeb2be9e2716e195136232b8f82b73f7e50e0cdd4d8
- `devin-acp-push-observation.txt` 0683a3ef7e5aef81541c51cc89b105923283e3a484a2980089ae6d1ac6f48a53

## Conclusion

**devin / cli · wake = event-driven — MEASURED at 3000.6.7.** The ACP stdio server pushes
server-initiated notifications to its attached client for the life of the process,
observed live with the probe answer arriving inside a pushed frame. Cell value unchanged;
stamp edit rides this commit: `classified → measured`, receipt_path here, `measured_at
2026-08-29`, versions updated to measured, grids re-rendered from the dataset.

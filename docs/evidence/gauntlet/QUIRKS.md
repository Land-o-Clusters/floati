# WS-H QUIRKS ledger (grok)

A quirk closes only with a hardening receipt or a documented refusal. This file is the gauntlet photograph, not a product fix list.

## Seed (program)

| id | harness | quirk | status |
|---|---|---|---|
| Q-cursor-28m | cursor | Cursor auto-wake dies after ~28 minutes (owner type specimen; grok seat is the live subject) | open — daemon half |
| Q-codex-turn | codex | Codex Stop is one deadline window; re-arm requires a new turn | open — needs_daemon |
| Q-opencode-lease | opencode | OpenCode lease/restart semantics (program seed; not re-measured here) | open |

## Photographed this matrix

| id | harness | quirk | status |
|---|---|---|---|
| Q-cursor-agent-brew | cursor | `/opt/homebrew/bin/cursor-agent --help`/`--version` still fail (65KiB stderr dump); `~/.local/bin/cursor-agent --version` = `2026.07.09-a3815c0` | open |
| Q-claude-dual | claude | PATH `2.1.231` vs `~/.local/bin/claude` `2.1.238` | open |
| Q-grok-build-name | grok-build | Adapter default `grok-build` ABSENT; live binary is `grok 1.0.5` | open |
| Q-herdr-not-agent | herdr | Persistent server is a workspace multiplexer, not an LLM session | documented `not_applicable` |

## Photographed dual-surface sweep (2026-08-28)

| id | harness | quirk | status |
|---|---|---|---|
| Q-agy-dual | antigravity | PATH `/opt/homebrew/bin/agy` is Homebrew cask **1.1.5**; `~/.local/bin/agy` is **1.1.22**. Same dual-copy shape as claude / cursor-agent | CLOSED 2026-09-02 — MX1-antigravity measured the bound user-local 1.1.22 live (`rulings/2026-09-02-gate1-two-release-gates-discharged-late.md` §1) |
| Q-chatgpt-is-codex-desktop | codex | `/Applications/ChatGPT.app` bundle id `com.openai.codex` is the Codex desktop surface (photographed **26.820.80927** on wake-family close; prior C0-DELTA cited **26.820.60940**). `/Applications/ChatGPT Classic.app` `com.openai.chat` is consumer chat, not Codex | documented |
| Q-claude-app-not-code | claude | `/Applications/Claude.app` is desktop chat (`com.anthropic.claudefordesktop`), not Claude Code. Claude Code also appears as a Cursor extension (`anthropic.claude-code-2.1.246` / `2.1.247`) distinct from PATH CLI `2.1.231` | documented |
| Q-desktop-load-unproven | dual-surface | Codex/OpenCode/T3/Antigravity desktop + Claude Code IDE extension: app/ext present but GUI/extension-host not driven; wake cells are `typed_refuse_unproven` until organic surface receipts exist | open — campaign/organic |


## Ruling 2026-09-02 (GATE-1, retroactive discharge of the WS-H flip gate for v0.1.0)

Every non-closed row above is classified in `docs/rulings/2026-09-02-gate1-two-release-gates-discharged-late.md` §1: not release-blocking with its reason (Q-cursor-28m, Q-codex-turn, Q-opencode-lease, Q-cursor-agent-brew, Q-claude-dual), parked by the owner (Q-desktop-load-unproven), folded into an ordered row (Q-grok-build-name → HB-1-R1), or closed (Q-agy-dual). Statuses in the tables are the photograph and are not rewritten; this section is the ledger's first amendment.

## Photographed by the build lane soaks (2026-09-08, receipts: docs/evidence/fq-{5,6,7}-2026-09-08.md)

| id | harness | quirk | status |
|---|---|---|---|
| Q-cursor-28m | cursor | 45-min soak on live grok seat (daemon `1dccc174`, cursor-agent `2026.09.02-c22c1a3`): 4 wakes sent T0+3.7/+10/+27.4/+40m, 4 acked (451.4s/53.5s/82.4s/315.3s, read from receipts/acks). Death-at-~28m NOT reproduced. Real adjacent mode: `wake_daemon_adapter_timeout` (21:52:11.629Z) leaves mail to the NEXT wake — eventually-reliable, not deadline-reliable | measured 2026-09-08 — not reproduced |
| Q-codex-turn | codex | Live thread held by shipped Stop waiter (600s armed window, exhausts `rearmed` at 22:42:05.002Z and 22:52:24.136Z, self-perpetuates while the codex process lives). Process death → daemon defers on stale `armed` claim (`wake_daemon_work_already_held`, 13+ cycles) then fires `codex queue` (23:11:21.397Z) which exits 0 into the dead thread: no new turn, no ack. Both soak wakes undelivered | measured 2026-09-08 — daemon cannot re-arm a dead seat; queued ≠ woken |
| Q-opencode-lease | opencode | No opencode surface in floati at all: no adapter (codex/cursor/grok-build/zcode only), no receipts plane, no wake surface. Seat boarding to a leasable session is wizard-only (SEAT.json via `node add`+governance; `register` blocks `node add`; non-interactive `node add` publishes nothing) — 3-refusal chain photographed. Lease TTL: zero leased acks in 937 fleet acks — no data exists. CLI 1.18.27 session = {list, delete} only | measured 2026-09-08 — unmeasurable as shipped; row should become diagnosis |

Amendment to Q-cursor-28m (2026-09-08T23:3xZ, architect correction `msg-01a0832dcb0a`): the row above photographs the observer's clock and is superseded — grok was mid-build (FQ-4 commit 22:25:24Z) at the first two acks, so those two sends (22:17:56.398Z, 22:24:34.342Z) are DISCARDED as data. Against NEW T0 = 22:38:50Z (grok stop-ack, answer-only) exactly one clean datum exists: sent 22:54:13.982Z (+15.4m), acked 22:59:29.279Z (+20.6m, inside the ~28m mark); the +30m/+40m slots were never flown (correction sat undrained until 23:26Z — lane inbox-discipline gap). The ~28m idle-decay claim is NEITHER REPRODUCED NOR REFUTED; the row stays OPEN for a corrected soak. Full accounting: docs/evidence/fq-5-2026-09-08.md §AMENDMENT.

## Photographed 2026-09-11 (CUR-2 Am.3)

| id | harness | quirk | status |
|---|---|---|---|
| Q-cursor-move-root | cursor | Cursor's `move_agent_to_root` aborts the running turn and is delivered to the stop hook as `status: aborted`; a project-scoped hook does not follow the chat to the new root. Dated 2026-09-11; journal timestamps 02:54Z (loop 5) and 13:42Z (loop 0). | open — product: error re-arms; first abort re-arms with a human escape; second consecutive abort in the same conversation_id yields `{}` |

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

# T1 — tide survey (derivable-metrics table)

**Spec:** `docs/design/tide-tables-spec-2026-08-28.md` @ `41cfa9c5257f42a15a6b4cf0292686f3d645ea42` (class A/B/C amendment).
**Seat:** grok (live Cursor agent session — class-B specimen for Cursor / desktop).
**Branch:** `refs/heads/lane/grok-gauntlet` after fast-forward to `origin/main` `41cfa9c`.
**Product source:** not edited. Live fleet root: not written. Tokens / sign-in: not touched. GUI apps: not opened.
**E1 sibling:** E1 remains correct as a **class-A CLI-flag** photograph. This row adds disk structure, in-session commands, and per-surface cells.

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/gauntlet/captures/T1-tide-survey.json` | 97563 | `692dc70e2fa2effe82557423fb335204aa6fde829babefb387dc684af571d8bc` |
| `docs/evidence/gauntlet/captures/T1-tide-survey-extra.json` | 1299 | `0a6f9e9095329a24bfffaf1aee8b5f488d4a32b161d44cc9b11b14151baa505e` |

Control: `/usr/bin/python3 --version` → `Python 3.9.6` exit 0.

Housekeeping (architect, no action required of this row, re-photographed anyway): PATH `agy` is now `~/.local/bin/agy` **1.1.22** (Homebrew cask gone). OpenCode CLI is **1.18.23**, matching the desktop app. Older C12/sweep cells stay verbatim.

## Versions (this photograph)

| harness | surface | version |
|---|---|---|
| codex | cli | `codex-cli 0.150.0` `/opt/homebrew/bin/codex` |
| claude | cli | `2.1.231 (Claude Code)` |
| opencode | cli | `1.18.23` |
| cursor | cli (`cursor-agent`) | Homebrew `--version` still exit 1; `~/.local/bin/cursor-agent` `2026.07.09-a3815c0` |
| cursor | desktop | this grok seat (editor `3.17.21` from prior inventory; not re-plisted) |
| cline | cli | `3.0.60` |
| grok | cli | `grok 1.0.5 (5115b46bc909)` |
| pi | cli | `0.84.3` |
| herdr | cli | `0.8.2` |
| t3 | cli | `t3 v0.0.35` |
| devin | cli | `devin 3000.2.17` (desktop still absent) |
| antigravity | cli | PATH `agy` **1.1.22** |

JSON samples are **key names only**. Tokenish paths (`auth.json`, oauth stores) were skipped. A private-name path skip kept `scan_generated_tree` clean.

## Access classes

- **A** — floati can read it without a seated agent (files, `opencode stats`, sqlite schema, CLI `--help`).
- **B** — in-session slash/command; SELF-REPORTED if a seat invokes it. This grok Cursor seat **cannot** run composer slash commands as tools.
- **C** — typed absence, cited.

**NO GAUGES** still bans fabricated fractions. A class-A derivation or class-B testimony, stamped, is honest.

## Derivable-metrics table

One row = one harness × one metric. Desktop siblings are separate. A metric not in this table cannot ship.

| harness / surface | metric | class | what exists (cited) | compact verb |
|---|---|---|---|---|
| **codex / cli** | session window field | **A** | `~/.codex/sessions/**/rollout-*.jsonl` first-line keys include `context_window`, `session_id`, `window_id` (`T1-tide-survey-extra.json`). Values not copied. | |
| **codex / cli** | remaining-context fraction | **B** docs / **C** live from this seat | Vendor: `/status` “remaining context capacity”, `/usage` account tokens, `/compact` summarizes ([developers.openai.com/codex/cli/slash-commands](https://developers.openai.com/codex/cli/slash-commands)). No Codex TUI was driven; no number captured. | **B** `/compact` (interactive; confirm step in vendor doc). **C** as a non-interactive one-shot: `codex --help` and `codex app-server --help` do not name compact. |
| **codex / cli** | turn / size | **A** | jsonl growth on disk; keys `timestamp`, `turn_id` (larger files; extra probe used a 22224-byte file). Formula if shipped: file bytes or event count / cited `context_window` field — DERIVED, sources named. | |
| **codex / desktop** (`ChatGPT.app`) | all tide metrics | **C** this surface | App Support/Codex is Chromium cache (prior sweep). Session jsonl lives under `~/.codex` with **writer surface not distinguished**. Do not inherit CLI `/status`. GUI not opened. | **C** on desktop |
| **claude / cli** | remaining-context | **B** docs / **C** live here | Vendor: `/context` grid, `/usage` (`/cost` alias), `/compact`, `/autocompact` ([code.claude.com/docs/en/commands](https://code.claude.com/docs/en/commands)). Live CLI: `--autocompact <auto\|tokens>` (100k–1M) is a **setting**, not a readout. | **B** `/compact`. **A** `--autocompact` threshold only. **C** non-interactive compact of an existing session (no such argv in `--help`). |
| **claude / cli** | transcript structure | **A** | `~/.claude/projects/-Users-penguinspecz-Projects-floati-grok/*.jsonl` keys: `sessionId`, `timestamp`, `type`, `cwd` — **no token-count field**. Hooks in `~/.claude/settings.json` **names**: `PreCompact`, `PostCompact`. | |
| **claude / cli** | model window | vendor-cited, model-dependent | Anthropic platform docs: Sonnet 4.5 **200k**; Sonnet 4.6 / several current models **1M** ([platform.claude.com/docs/en/build-with-claude/context-windows](https://platform.claude.com/docs/en/build-with-claude/context-windows)). Not a live remaining number. | |
| **claude / desktop-chat** (`Claude.app`) | all | **C** | Chat GUI ≠ Claude Code (prior surface sweep). Not inventoried as Code transcripts. | **C** |
| **claude / ide-extension** | remaining-context | **B** docs / **C** live | Same `/context` family if the extension composer implements it; **not** invoked from this Cursor grok seat. | unproven on the extension |
| **opencode / cli** | session tokens | **A** | `~/.local/share/opencode/opencode.db` table `session` columns: `cost`, `tokens_input`, `tokens_output`, `tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`, `time_compacting`. **Schema only** (no SELECT of values). Also `opencode stats` lifetime aggregates (Sessions 49, Avg Tokens/Session 1.2M) — historical, not remaining-window. | |
| **opencode / cli** | remaining-context | **C** class A; **B** if TUI `/compact` | `--help` names `stats`, not remaining-window. Third-party docs name TUI `/compact`; **not** in live `--help`. Serve `--help` has no token route named. | **C** non-interactive compact argv; TUI `/compact` **not live-probed** |
| **opencode / desktop** | all | **C** this surface | CLI/app now both 1.18.23. Token columns are in `opencode.db` (shared home). Desktop load of that DB **not** proven. GUI not opened. | **C** |
| **cursor / desktop** (this seat) | remaining-context | **B** docs / **C** agent-tools | Vendor CLI changelog: `/usage`, `/context`, `/summarize` (`/compact` `/compress` aliases) — [cursor.com/docs/cli/changelog](https://cursor.com/docs/cli/changelog). **Live specimen:** this grok agent has **no tool** that runs those composer commands. No remaining-context number is available in this tool loop. Human-typed slash is the surface. | **B** `/summarize` in composer (not run; would mutate). **C** non-interactive. |
| **cursor / desktop** | transcript | **A** | `~/.cursor/projects/.../agent-transcripts/*.jsonl` keys: `role`, `message`, `type`, `todos` — **no token field**. `~/.cursor/acp-sessions/*/meta.json` keys: `cwd`, `schemaVersion` only. | |
| **cursor / cli** (`cursor-agent`) | all | **C** | Homebrew help still a JS dump. No usage readout. | **C** |
| **cline / cli** | remaining-context | **C** class A; **B** TUI | `--compaction agentic\|basic\|off` is a **mode**. Vendor PRs name `/compact` in the TUI/extension. Not invoked. `~/.cline/data/settings/providers.json` has `model`, `tokenSource` — not a window gauge. | **A** mode flag; **C** one-shot compact argv |
| **grok / cli** | remaining-context | **B** docs (shipped locally) / **C** live TUI | `~/.grok/docs/user-guide/04-slash-commands.md` names `/compact`, `/context` (category breakdown + free space), `/session-info` (aliases `/status` `/info`), `/usage` (`/cost`). Auto-compact at **85%** (`[session] auto_compact_threshold_percent`). TUI not driven. | **B** `/compact`. **C** non-interactive argv (`grok --help` has `du` / `export`, not compact). |
| **grok / cli** | disk / turns | **A** | `grok du`: `129.1 MB` total; `sessions` `308.0 KB`. `summary.json` keys: `num_messages`, `num_chat_messages`, `current_model_id` — **no token field**. | |
| **grok / desktop** (`Grok Bot.app`) | all | **C** | Different product (prior sweep). | **C** |
| **pi / cli** | catalog window | **A** | `~/.pi/agent/models.json` keys include `contextWindow`, `maxTokens`, `cost` (catalog; values not copied). | |
| **pi / cli** | remaining-context / compact | **C** stock | `--no-context-files` is AGENTS.md loading. Compact is an **extension** (`pi-smart-compact` `/smart-compact`) — not proven installed. | **C** unless that plugin is present (not checked beyond `--help`) |
| **herdr / cli** | all tide metrics | **C** | `~/.config/herdr/session.json` is multiplexer layout (`panes`, `tabs`) — not LLM tokens. `herdr status` has no token fields. | **C** (`not_applicable` as an LLM turn) |
| **t3 / cli** | remaining-context | **C** | `--help` has pairing token (auth), not context. | **C** |
| **t3 / desktop** | all | **C** | App Support/t3code is Chromium cache. GUI not opened. | **C** |
| **devin / cli** | remaining-context / compact | **C** | `--help`: `rules` (context blobs), `skills` (slash commands) — no `/usage` or compact named. `~/.config/devin/config.json` **not read**. | **C** |
| **antigravity / cli** (`agy` 1.1.22) | remaining-context / compact | **C** | `--help` / `plugin --help`: `--continue`, plugins; no compact/usage/hook. | **C** |
| **antigravity / desktop** | all | **C** | `app_storage.json` keys: wizard/project id only. GUI not opened. | **C** |

## What this table authorizes (and refuses)

**May be offered later (T2) only with the stamp named:**

- Codex **A**: DERIVED from jsonl `context_window` + growth/turn counts (formula + paths).
- OpenCode **A**: DERIVED from `session.tokens_*` / `cost` in `opencode.db` (schema receipted; a future evaluator may SELECT).
- Grok **A**: DERIVED from `summary.json` `num_messages` or `grok du` bytes (proxy, not remaining-window).
- Pi **A**: catalog `contextWindow` as the **cited constant**, not remaining.
- Any harness **B**: SELF-REPORTED `/context` `/status` `/usage` from **that node’s own seat**, never as a fleet-measured gauge.

**Must refuse:** a 70%-of-window policy on herdr, t3, agy, Devin, Cursor-agent Homebrew, Claude.app chat, or any surface whose remaining-context cell is **C** with no A recipe and no B testimony.

**Native `--action compact`:** no harness has a **non-interactive** compact argv receipted this row. Class-B `/compact` exists (docs) for Codex, Claude Code, Cursor composer, Grok TUI, and (unproven here) OpenCode/Cline TUIs. T4 may offer compact only after a later row launches that verb without a TTY confirm, per harness.

## Class-B specimen (this Cursor grok seat)

Commands the human can type in the composer are documented (`/usage`, `/context`, `/summarize`). This agent loop has no matching tool. **Printed remaining-context from this turn: none.** That is a scope fact, not a claim that Cursor has no usage surface.

## Not claimed

- No daemon, no T2 policy records, no live-root writes.
- No GUI `open`. No `devin auth`. No SELECT of token **values**.
- Wake-family daemon drills and chaos campaign not started.

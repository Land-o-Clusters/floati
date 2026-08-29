# C0-DELTA — surface axis (cli / desktop / IDE extension)

**Row:** dual-surface law `docs/design/dual-surface-testing-2026-08-28.md` + roster cut `docs/design/harness-market-roster-2026-08-28.md`.
**Seat:** grok. Branch: `refs/heads/lane/grok-gauntlet` after fast-forward to `origin/main` `8e51583be531085b8c6b6553b4bc6de320818c75`.
**Probe UTC:** from capture `started_utc`.
**Product source:** not edited. Tokens and sign-in flows: not touched. GUI apps: not opened.

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/gauntlet/captures/H-surface-sweep.json` | 23197 | `b0098847f9e206435be9707ae924dda6802de5531ef20f727504fb2831371f70` |
| `docs/evidence/gauntlet/captures/H-surface-sweep-extra.json` | 18257 | `cb4f28d4a08865280cb56f3869c8c1479aecfe38144381913bce119ba8bfbe26` |

Control: `/usr/bin/python3 --version` → `Python 3.9.6` exit 0.

`brew list --cask` (7 names, exit 0): `antigravity`, `antigravity-cli`, `claude-code`, `codex`, `cursor-cli`, `devin-cli`, `xdeck`.

## Surface photograph

| harness | cli | desktop app | IDE extension |
|---|---|---|---|
| codex | `/opt/homebrew/bin/codex` `codex-cli 0.150.0` | `/Applications/ChatGPT.app` `com.openai.codex` **26.820.60940** (Mach-O). Adjacent `/Applications/ChatGPT Classic.app` is `com.openai.chat` **1.2026.160** (consumer chat, not Codex) | none in Cursor extensions |
| claude | `/opt/homebrew/bin/claude` `2.1.231 (Claude Code)` | `/Applications/Claude.app` `com.anthropic.claudefordesktop` **1.37937.3** (desktop chat, not the Code CLI) | **PRESENT** in Cursor: `anthropic.claude-code-2.1.246-darwin-arm64` and `…2.1.247…` |
| opencode | `/opt/homebrew/bin/opencode` `1.18.9` | `/Applications/OpenCode.app` `ai.opencode.desktop` **1.18.23** (CLI/app skew) | none |
| cursor | `cursor` ABSENT on PATH. `/opt/homebrew/bin/cursor-agent` exists (Homebrew `--version` still fails). `~/.local/bin/cursor-agent --version` = `2026.07.09-a3815c0` | `/Applications/Cursor.app` `com.todesktop.230313mzl4w4u92` **3.17.21** | editor is the IDE |
| cline | `/opt/homebrew/bin/cline` `3.0.60` | **none** | none named in 18 Cursor extensions; `~/.vscode/extensions` absent |
| grok-build | `grok-build` ABSENT. `/opt/homebrew/bin/grok` `grok 1.0.5 (5115b46bc909)` | `/Applications/Grok Bot.app` `com.anysphere.sand` **0.29.0** (not the `grok` TUI binary) | none |
| pi | `/opt/homebrew/bin/pi` `0.84.3` | **none** | none |
| herdr | `/opt/homebrew/bin/herdr` `herdr 0.8.2` | **none** | none |
| t3 | `/opt/homebrew/bin/t3` `t3 v0.0.35` | `/Applications/T3 Code (Nightly).app` `com.t3tools.t3code` **0.0.35-nightly.20260826.1195** | none |
| **devin (C11)** | `/opt/homebrew/bin/devin` `devin 3000.2.17 (2c489dfc)` | **none** (`/Applications/Devin.app` absent). Single local surface | none |
| **antigravity (C12)** | PATH `/opt/homebrew/bin/agy` **1.1.5** (cask `antigravity-cli`). Second copy `~/.local/bin/agy` **1.1.22** | `/Applications/Antigravity.app` `com.google.antigravity` **2.11.0** | none named in Cursor extensions. `antigravity` PATH name ABSENT |

Dual-surface (both cli + desktop/IDE installed): **codex, claude (cli+chat-app+extension), opencode, cursor, t3, antigravity**. Single local surface: **cline, pi, herdr, devin**. Grok TUI vs Grok Bot.app are different products (two surfaces, not a shared core).

## Config presence (paths only; no token files read)

Present: `~/.codex/hooks.json`, `~/.claude/settings.json`, `~/.cursor/hooks.json`, `~/.config/opencode` (includes `plugins/`), `~/.config/herdr`, `~/.config/devin` (names: `cli`, `config.json`, backups — contents not read), Application Support for Claude, Codex, Cursor, Antigravity, t3code.

GUI binaries were identified with `/usr/bin/file` (Mach-O). Apps were **not** `open`ed.

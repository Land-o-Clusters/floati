# H-wake — posture matrix SURFACE ADDENDUM

**Law:** `docs/design/dual-surface-testing-2026-08-28.md`. A CLI cell certifies nothing about desktop.
**Inventory:** `docs/evidence/conformance/C0-DELTA-surface-axis.md`.
**CLI baseline (not repeated):** `docs/evidence/gauntlet/H-wake-posture-matrix.md`.

Desktop/IDE cells below were measured on this machine. GUI apps were not launched (sign-in risk). Wake surfaces for desktop are therefore **not inherited** from CLI cells; unproven load is stated as unproven.

## Per-surface posture (dual / new only)

| harness / surface | version | resident? | wake surface | lifetime | verdict |
|---|---|---|---|---|---|
| codex / cli | `codex-cli 0.150.0` | yes | Stop `hooks.json` (CLI cell) | deadline-re-arm | **needs_daemon** (existing) |
| codex / desktop (`ChatGPT.app` / `com.openai.codex`) | `26.820.60940` | **yes** — Mach-O + Application Support/Codex present | `~/.codex/hooks.json` **exists** on disk; desktop **load not live-probed** (app not opened). Bundle `hooks` grep was npm noise, not a Stop schema | unknown on this surface | **needs_daemon** |
| claude / cli | `2.1.231` | yes | CLI hooks (`--bare`) | per-turn Stop | **needs_daemon** (existing) |
| claude / desktop-chat (`Claude.app`) | `1.37937.3` | **yes** — chat GUI | not Claude Code; no Stop waiter photographed in the app | n/a | **not_applicable** |
| claude / ide-extension | Cursor `anthropic.claude-code-2.1.247-darwin-arm64` (also 2.1.246) | **yes** inside a Cursor session | extension ≠ CLI; hook load **not** live-probed here | unknown | **needs_daemon** |
| opencode / cli | `1.18.9` | yes | plugin events + measured `serve` | process lifetime | **event_driven** (existing) |
| opencode / desktop | `1.18.23` | **yes** — app present | `~/.config/opencode/plugins` exists (CLI config dir). Desktop load **unproven** | unknown | **event_driven** |
| cursor / cli (`cursor-agent`) | `2026.07.09-a3815c0` local; Homebrew wrapper still fails | agent CLI | not the editor Stop hook | unmeasured on the CLI binary | **needs_daemon** |
| cursor / desktop (editor) | `3.17.21` | **yes** — this grok seat | official `stop` + `~/.cursor/hooks.json` present | dies ~28m (owner type specimen) | **needs_daemon** |
| t3 / cli | `t3 v0.0.35` | when `serve` | WebSocket push | server lifetime | **event_driven** (existing) |
| t3 / desktop | `0.0.35-nightly.20260826.1195` | **yes** — app present, not launched | no hook files in bundle json grep | unknown | **event_driven** |
| grok / cli | `grok 1.0.5` | yes TUI | no Stop in `--help` | TUI until exit | **needs_daemon** (existing) |
| grok / desktop (`Grok Bot.app`) | `0.29.0` | **yes** if used | different bundle (`com.anysphere.sand`); not the TUI | unknown | **not_applicable** |
| **devin / cli** | `3000.2.17` | **yes** — `--help` sessions, `list` sessions, `plugins` | live: `devin plugins`; no desktop | plugin/process lifetime; Stop waiter **not** in `--help` | **event_driven** |
| **antigravity / cli** (`agy`) | PATH **1.1.5**; `~/.local/bin/agy` **1.1.22** | **yes** — `--continue` / `--conversation` | live: `agy plugin` / `plugins`; `--help` has no `hook` word | session until exit; 3-cycle Stop **not** run | **event_driven** |
| **antigravity / desktop** | `2.11.0` | **yes** — app + Application Support | vendor claims shared agent core with CLI; **not inherited**. No hooks.json in app json grep | unknown | **needs_daemon** |

Single-surface harnesses (cline, pi, herdr) keep their CLI cells. No desktop sibling.

**hook_sufficient: still none**, including new surfaces.

No live-root waiter. No GUI `open`. No token or `auth` subcommand was run.

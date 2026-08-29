# Wake posture matrix — photograph (grok, 2026-08-28)

**Spec:** `docs/design/wake-posture-matrix-2026-08-28.md`
**Seat:** grok. Branch: `refs/heads/lane/grok-gauntlet` (merged `origin/main` `2fcf6152a067ab80abe13e79623e9c4b60246f68` before probes).
**Product source:** not edited. Live fleet root: not used as a waiter target. No synthetic outcomes.

This is a photograph of the binaries on this machine at probe time. A version bump invalidates the cell, not the method.

## Captures

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/gauntlet/captures/H-wake-posture-probe.json` | 133319 | `e3ea00107c564ba85992ea8951dd2d406709f8daedbf11d46eacf1be7382f5b9` |
| `docs/evidence/gauntlet/captures/H-wake-posture-extra.json` | 8679 | `89360d60bb580b1a1469478c65205cf2dbb00181a23b4b31c59e78591237d28b` |
| `docs/evidence/gauntlet/captures/H-wake-posture-opencode-3cycle.json` | 631 | `c5125cfcde20e1c24352c285d62783429976cd30cd3e32b0e1216a8f04da10f6` |
| Codex 3-cycle Stop re-arm (fixture waiter) | 29344 | `fb5232494f05aa5656a3cffe009dbbc742b862e76e3d60a915e32d0c844be3c0` (`H-wake-hook-run.json`) |

Control: `/usr/bin/python3 --version` exit 0 stdout `Python 3.9.6`.

## Matrix

| harness | version (photographed) | resident session? | wake surface (docs + live) | hook lifetime | verdict |
|---|---|---|---|---|---|
| codex | `codex-cli 0.150.0` (`/opt/homebrew/bin/codex`) | **yes** — interactive session; `--help` names `app-server` daemon and `resume` | Official Stop hook (`https://developers.openai.com/codex/hooks`, `hooks.json` `Stop` + `timeout`). Live: `--help` names `plugin` / `resume` / app-server; fixture Stop waiter 3× `rearmed` in `H-wake-hook-run.json` | **deadline-re-arm** — vendor `timeout` on Stop; three fixture cycles waited 2s each, outcome `rearmed` | **needs_daemon** |
| claude | `2.1.231 (Claude Code)` PATH `/opt/homebrew/bin/claude` (second copy `2.1.238` at `~/.local/bin/claude`, not PATH) | **yes** — `--help`: "starts an interactive session by default"; `-p/--print` is the per-invocation path; `--bg` background agent | Official SessionStart/Stop hooks. Live: `--help` names `--bare` "skip hooks, LSP, plugin" | per-turn Stop (vendor); 3-cycle Stop wait **not** installed on this seat | **needs_daemon** |
| opencode | `1.18.9` (`/opt/homebrew/bin/opencode`) | **yes** — TUI; `opencode session`; `opencode serve` listened | Official plugin event bus (`https://dev.opencode.ai/docs/plugins/` `session.idle`). Live: `opencode plugin --help`, `opencode serve` bound `http://127.0.0.1:4098` | process/server lifetime, no Stop deadline in plugin API. **Measured ≥3 cycles:** HTTP 200 ×3 against `opencode serve --port 4098` | **event_driven** |
| cursor | editor `3.17.21`; `cursor-agent` `2026.07.09-a3815c0` (`~/.local/bin`; Homebrew `--help` still dumps) | **yes** — this grok seat is a live Cursor agent session | Official `stop` hook (`https://cursor.com/docs/hooks.md`; `timeout`, `loop_limit` default 5). Live: this session exists | **dies** — owner type specimen ~28 min (WS-A). Stop wait not re-timed this turn (daemon half) | **needs_daemon** |
| cline | `3.0.60` (`/opt/homebrew/bin/cline`) | **yes** — `--id` resume; `--zen` background hub; `cline hub` "Manage the local hub daemon" | Official hooks + live `--hooks-dir` (default `~/.cline/hooks`) and `cline hook` "Handle a hook payload from stdin" | hub daemon + hook scripts; 3-cycle Stop-wait **not** run (would require hook install) | **needs_daemon** |
| grok-build | named `/opt/homebrew/bin/grok-build` **ABSENT**. Override `grok 1.0.5 (5115b46bc909)` | **yes** — "Grok Build TUI"; `--continue` / `--resume` | Live `--help`: no hook/plugin/Stop surface. `-p` "Single-turn prompt… exits" is the per-invocation path | TUI until exit; no documented wait hook | **needs_daemon** |
| pi | `0.84.3` (`/opt/homebrew/bin/pi`) | **yes** — `--continue` / `--resume` / `--session`. Also ephemeral: `--no-session` (adapter argv uses `--mode rpc --no-session`) | Official extensions (`pi.on("session_start"|…)`). Live: `--help` has session flags and `pi install` extensions; `--help` does not name a Stop waiter | RPC/extension events while the process lives; `--no-session` has nothing to resume | **event_driven** |
| herdr | `herdr 0.8.2` | **yes** — `herdr status`: `server: status: running` socket `~/.config/herdr/herdr.sock` (harness daemon, not the floati fleet root) | `--help`: terminal workspace manager; `herdr server`; no Stop/plugin wake for an LLM turn. Floati herdr client is observation-only | server until `herdr server stop` | **not_applicable** |
| t3 | `t3 v0.0.35` (`/opt/homebrew/bin/t3`) | **yes** when `t3 serve` / `t3 start` — `--help` "HTTP/WebSocket server". One-shot commands otherwise | Live: `--log-websocket-events` "outbound WebSocket push traffic". C9 bounded `t3 serve` listened on `127.0.0.1:3773` | server process lifetime (push) | **event_driven** |

## Verdict meanings as applied

- **needs_daemon:** a resident agent session exists, and the documented/live wait surface is a per-turn Stop/hook window that is not load-bearing across long idle (Codex+Cursor already v1 regardless). Cline's hub is *their* daemon, not Floati's; the cell still says needs_daemon for a Floati adapter until the hub is proven as the wake path. Grok TUI has no wait hook at all.
- **event_driven:** the harness pushes (OpenCode plugin events + measured `serve`; pi RPC/extension events; t3 WebSocket). Nothing to poll.
- **not_applicable:** herdr is a multiplexer/server for *other* agents' panes, not an LLM turn to wake. `--no-session` on pi is a path, not the whole harness.
- **hook_sufficient:** no cell. Nothing photographed as a Stop/plugin wait that is load-bearing without extra machinery.

## Not claimed

- Cursor 28-minute death was not re-timed this turn (daemon-half longevity).
- No Claude/Cline Stop hook was installed to cycle.
- No live-root waiter.
- No daemon adapter assignment beyond the matrix rule (Codex+Cursor stay v1 by prior ruling).

Quirks: `docs/evidence/gauntlet/QUIRKS.md`.

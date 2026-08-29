# Claude wake re-measurement (grok, 2026-08-28)

**Spec:** `docs/design/capability-matrix-spec-2026-08-28.md` @ `ad62e46a7f039e6e1ee4c88bc2c890162d663715` (rewritten main tip; dead SHAs `3e010c6` / kin not used).
**Dispatches:** `msg-01a046690a817e94a2d1d2bb72ad9cfc`, `msg-01a04669e3dd7c5a99cd32c154a89b94`.
**Seat:** grok (Cursor). **Not** a Claude seat.
**Incumbent bus:** READ-ONLY. No drain, ack, send, lock, or hook edit on that bus. Paths redacted in the capture (`<incumbent-bus-root>`).
**Depth-2:** already gated PASS (`docs/evidence/gauntlet/T1-depth2.md`); folded by citation, not re-run.

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/gauntlet/captures/H-claude-wake-remeasure.json` | 11245 | `80fe91af81ca0851af523144b5a524d01c4289f072d1f7d8fbf7e74b73944583` |

Control: `/usr/bin/python3 --version` Python 3.9.6. Origin re-fetched after the tip rewrite; `lane/grok-gauntlet` fast-forwarded to `origin/main` `ad62e46` before this photograph.

Preference order (spec, binding): `event_driven` > `hook_sufficient` > `needs_daemon`. A flip still needs a receipt, not testimony.

## Live ≥3-cycle organic hold (opencode-shaped)

**Not run.** This session is Cursor. `claude -p` print-mode previously failed OAuth on C2; not retried. No Claude TUI or GUI launched. No Stop-hook script from `~/.claude/settings.json` was executed (would be a live wait against the incumbent bus).

Opencode’s instrument was HTTP 200 ×3 against `opencode serve`. Claude has no equivalent serve probe in `--help`.

## Claude Code hooks on this machine (class A, config)

`~/.claude/settings.json` **Stop** and **SessionStart** (`matcher: startup|resume|clear`) are present. Stop/SessionStart command basenames include `vibe-island-bridge` and `AgentPeekBridge`. **AgentPeekBridge timeout = 600** (seconds) on those events — a **deadline window**, Codex-shaped, not a push socket.

## Historical incumbent-bus evidence (READ-ONLY)

Engineer **mail** (not wake receipts):

| ledger | lines | first `ts` | last `ts` |
|---|---:|---|---|
| `to_sre.jsonl` | 1141 | 2026-06-24T20:56:28Z | 2026-08-22T13:50:54Z |
| `to_ghops.jsonl` | 1055 | 2026-06-24T20:56:28Z | 2026-08-22T13:50:54Z |
| `to_ohe.jsonl` | 485 | 2026-06-24T20:56:28Z | 2026-08-15T23:53:12Z |
| `to_cis.jsonl` | 687 | 2026-06-24T20:56:28Z | 2026-08-15T23:53:12Z |

That is ~eight weeks of **deliveries**. It does not name harness or prove Stop-hook re-arm.

**Wake journal** `logs/wake_hook_journal.jsonl`: 4860 lines, 2026-07-26 .. 2026-08-28. Top hooks: `codex_bus_wait`, `cursor_stop`. Nodes: sol, grok, grok2, sol2. **Engineer node rows: 0.** **Claude harness field rows: 0.** 98 lines contain the substring `claude` and are `codex_bus_wait` / `exit_empty` with null harness/node (incidental path text), not Claude wakes.

**Identity sessions** (filename + `harness`/`node` fields only):

| harness | files | node |
|---|---:|---|
| `claude-code` | 13 | **architect** only (`architect-claude-0811a` … `0818a` plus digest-named) |
| `opencode-desktop` | 14 | alice3..alice6 |
| `codex` | 3 | sol |

No `sre` / `ghops` identity session files. Alice session files are **opencode-desktop** (owner correction: alice is not Claude — **matches** this photograph).

## Verdict

| claim | result |
|---|---|
| Live ≥3-cycle Claude hold, this sitting | **NOT MEASURED** |
| Claude wake rows in the incumbent wake journal | **ABSENT** (0) |
| Engineer-seat Claude identity sessions | **ABSENT** (architect-only `claude-code`) |
| Engineer-seat mail over weeks | **PRESENT** (delivery, not wake) |
| Stop/SessionStart hooks installed | **PRESENT**; AgentPeekBridge **600s** deadline |
| Flip `needs_daemon` → `event_driven` or `hook_sufficient` | **REFUSED** — would repeat the docs/testimony-as-verdict defect |

**claude / cli wake cell stays `needs_daemon`.** Daemon-adapter queue: Claude **stays**. A later Claude **seat** can earn `hook_sufficient` or `event_driven` with the opencode-style ≥3-cycle receipt or a Claude-named wake journal. This grok sitting cannot mint that receipt without inventing it.

Wake family remains **not green**. Product source not edited. README not edited.

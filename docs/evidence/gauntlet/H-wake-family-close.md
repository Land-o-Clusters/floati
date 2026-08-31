# H-wake — family close (per-surface honesty + push paths + herdr N/A)

**Seat:** grok · **Branch:** `refs/heads/lane/grok-gauntlet` off `origin/main`
`f2b587634cfc6d6a52cc24bd02bfd978919c359b` (wake-daemon acceptance PASS).
**Order:** architect `msg-01a048782f3d775382a558d56c68506f` — wake-family drills;
daemon half already GREEN; close the family.
**Product source:** not edited. Live fleet root: not used as a waiter target.
GUI apps: **not** `open`ed. No synthetic wake rows.

## Captures

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/gauntlet/captures/H-wake-family-close.json` | 30630 | `ba2eaa042fcb8124c6e5fcdfe7b63fa8c8bb3ce3bbea008cec6d9a99b4e482f7` |
| Runner | — | `docs/evidence/gauntlet/run_wake_family_close.py` |

Control: `/usr/bin/python3 --version` exit 0.

**Banked cells that stand (not re-litigated):** hook+controller baseline
`docs/evidence/gauntlet/H-wake-hook.md`; CLI posture photograph
`docs/evidence/gauntlet/H-wake-posture-matrix.md`; surface inventory
`docs/evidence/conformance/C0-DELTA-surface-axis.md` + prior surface addendum
(load-unproven rows closed here).

## What this row closes

1. **Per-surface honesty** for the six dual-surface harnesses — desktop/IDE
   cells prove on their own evidence or are **typed-refused**; never inherit
   the CLI verdict.
2. **Event-driven trio push-path proofs** (opencode · pi · t3) on the CLI
   surfaces that claim `event_driven`.
3. **herdr-class not_applicable wake drill** — proof of non-applicability,
   not a skipped row.

## Event-driven push paths (fixtures)

| harness / surface | instrument | result |
|---|---|---|
| **opencode / cli** | `opencode serve --port <ephemeral>`; HTTP GET `/` ×3 | ready; **200 / 200 / 200**; server killed SIGTERM |
| **t3 / cli** | `t3 serve --no-browser --port <ephemeral> --base-dir .gauntlet-scratch/wake-family-t3-home`; HTTP GET `/` ×3 | ready (`Listening on http://127.0.0.1:…`); **200 / 200 / 200** |
| **pi / cli** | `--help` surface + `pi list` | `--mode rpc` present; `--extension` / session flags present; `pi list` **exit 0** |

These are push/listen proofs on the harness's own local server or RPC surface.
They are not floati daemon adapters (event_driven harnesses get none).

## herdr not_applicable (wake drill)

Live photograph this turn:

- `herdr --help` opens with **“terminal workspace manager for AI coding
  agents”**; commands are session/pane/tab/workspace/server — not an LLM turn
  to wake.
- `herdr status`: **server: status: running** on
  `~/.config/herdr/herdr.sock` (herdr's own multiplexer daemon, not the
  floati fleet root).
- False-friend: the word `completion` appears as **shell** `herdr completion
  zsh`, not model completion. The N/A cell does not ride that token.

**Verdict: `not_applicable`** — confirmed as a wake drill, not a skip.

## Per-surface wake matrix (every claimed surface)

Vocabulary for this close: banked CLI verdicts keep their names;
desktop/IDE cells that cannot be live-probed without opening a GUI or
driving an extension host are **`typed_refuse_unproven`** (a closed cell).
`not_applicable` remains for surfaces that are the wrong product class.

| harness / surface | version (this capture) | verdict | evidence |
|---|---|---|---|
| codex / cli | banked `0.150.0` | **needs_daemon** | H-wake-posture-matrix + H-wake-hook |
| codex / desktop (`ChatGPT.app` / `com.openai.codex`) | **26.820.80927** (version bump vs C0-DELTA `26.820.60940`) | **typed_refuse_unproven** | app present; `~/.codex/hooks.json` exists; **app not opened** — desktop hook load not live-probed; CLI cell not inherited |
| claude / cli | banked `2.1.231` | **needs_daemon** | H-wake-posture-matrix (+ remeasure refusal stands) |
| claude / desktop-chat (`Claude.app`) | `1.37937.3` `com.anthropic.claudefordesktop` | **not_applicable** | chat GUI, not Claude Code (plist reconfirmed) |
| claude / ide-extension | Cursor ext `anthropic.claude-code-2.1.246` + `2.1.247` present | **typed_refuse_unproven** | dirs present; extension-host hook load **not** live-probed |
| opencode / cli | banked `1.18.9` | **event_driven** | push path 3×200 this row |
| opencode / desktop | `1.18.23` | **typed_refuse_unproven** | app present; `~/.config/opencode/plugins` is the CLI config dir — **not** proof of desktop load |
| cursor / cli | banked local agent | **needs_daemon** | H-wake-posture-matrix |
| cursor / desktop | `3.17.21` | **needs_daemon** | **organic** — this grok seat; `~/.cursor/hooks.json` present; daemon acceptance already witnessed production wakes on this coordinate |
| t3 / cli | banked `0.0.35` | **event_driven** | push path 3×200 this row |
| t3 / desktop | `0.0.35-nightly.20260826.1195` | **typed_refuse_unproven** | app present; **not opened** |
| antigravity / cli | banked (PATH 1.1.5 / `~/.local/bin` 1.1.22) | **event_driven** | prior surface addendum + C12; not re-served this row |
| antigravity / desktop | `2.11.0` | **typed_refuse_unproven** | app present; **not opened**; vendor “shared core” claim **not inherited** |
| grok / cli | banked `1.0.5` | **needs_daemon** | H-wake-posture-matrix |
| grok / desktop (`Grok Bot.app`) | `0.29.0` `com.anysphere.sand` | **not_applicable** | different product from the `grok` TUI |
| cline / cli | banked `3.0.60` | **needs_daemon** | H-wake-posture-matrix (single surface) |
| pi / cli | banked `0.84.3` | **event_driven** | RPC/extension surface + `pi list` exit 0 this row |
| herdr / cli | `0.8.2` | **not_applicable** | wake drill this row |
| devin / cli | banked `3000.2.17` | **event_driven** | C11 / surface addendum (single local surface) |

**hook_sufficient: still none.**

## Family status

| half | status |
|---|---|
| Hook + exact-session controller | GREEN (banked) |
| Daemon (Codex+Cursor adapters, organic acceptance) | GREEN (architect gate @ `f2b5876`) |
| Per-surface honesty + event-driven push + herdr N/A | **CLOSED this row** — every claimed surface is proven or typed-refused |

**Wake-family: GREEN** under the law that a typed refuse is a closed cell.
Desktop/IDE `typed_refuse_unproven` cells flip only when an organic GUI or
extension-host session is driven and receipted — never by inheritance.

## Not claimed

- Opening ChatGPT.app / Claude.app / OpenCode.app / T3.app / Antigravity.app /
  Grok Bot.app
- Extension-host Stop cycles for Claude Code inside Cursor
- New floati daemon adapters for event_driven harnesses
- README matrix swap
- Live-root waiter against the fleet

Quirk note: Codex desktop bundle version moved to `26.820.80927` on this
machine; prior inventory cited `26.820.60940`. Method stands; that cell's
version stamp updates with this photograph.

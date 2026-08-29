# C0 — machine harness inventory (grok, 2026-08-27)

**Row:** C0 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Seat:** `grok` (clean-room, `~/Projects/floati-grok`)
**Branch:** `refs/heads/lane/grok-conformance`
**Tree SHA at probe:** `932e377e9b88d801dfd545e1c238c50af5ec58ba` (`origin/main` tip; Car 4 not landed)
**Probe UTC:** `2026-08-27T21:48:21Z` (JSON `started_utc`)
**Verdict:** inventory complete. Four of eight named harnesses have a real binary on this machine. Four are NOT PRESENT.

Untruncated machine-readable artifacts (counts below are taken from these files, not from `head`):

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/conformance/C0-machine-harness-inventory.json` | 10822 | `b4478e7b14f982f8045aee9542fd6da8f682a9ca2c75bdb555d0e457d0f381bb` |
| `docs/evidence/conformance/C0-machine-harness-inventory-lists.json` | 1238 | `eb59d58e7e4ed942b1bf643509a84666d2ccefea7a860e2742f2560d628c57c7` |

Adapter default paths for rows C2..C8 are taken from `origin/u2/manifest-contract` (Car 4, unread as product source; path-only). Codex/claude/pi defaults also exist on this tip.

## Known-green control

A zero is only evidence beside a measured non-zero. Control executable: `/usr/bin/python3` (realpath `/Library/Developer/CommandLineTools/usr/bin/python3`).

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout_bytes: 13
stdout: Python 3.9.6
stderr_bytes: 0
```

## How this machine was searched

PATH lookup via `/usr/bin/command -v` and bash `type -a` for: `codex`, `claude`, `opencode`, `cursor`, `cline`, `grok-build`, `grok`, `pi`, `herdr`, `claude-code-acp`, `codex-acp`, `acp-agent`, `cursor-agent`.

Fixed locations: `/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`, `~/bin` (absent), `/Applications` name match, Homebrew Caskroom names, `brew list --cask` (7 lines, exit 0), `brew list --formula` (70 lines, exit 0), `npm ls -g --depth=0 --json` (4 dependencies), Cursor extension directory `~/.cursor/extensions` (18 entries, 0 name-hits), `~/.vscode/extensions` (directory absent), `~/.cargo/bin` (absent), `~/.config/herdr` (absent).

`brew list --cask` stdout, untruncated (7 lines, 73 bytes, exit 0, executable `/opt/homebrew/bin/brew`):

```text
antigravity
antigravity-cli
claude-code
codex
cursor-cli
devin-cli
xdeck
```

`npm ls -g --depth=0 --json` dependencies, untruncated (4 names, executable `/opt/homebrew/bin/npm`): `@moonshot-ai/kimi-code@0.30.0`, `@sourcegraph/amp@0.0.1784524244-g589322`, `npm@11.17.0`, `opencode-ai@1.18.9`.

No foreign-bus path was read or written.

## Roster (eight named harnesses)

Roster default executables from Car 4 adapters (`origin/u2/manifest-contract`), except herdr which has no adapter module on that branch; herdr is probed at `/opt/homebrew/bin/herdr` by the same rule as the others.

| Harness | Adapter default path | Present? | Version string | How invoked |
|---|---|---|---|---|
| codex | `/opt/homebrew/bin/codex` | **PRESENT** | `codex-cli 0.150.0` | `/opt/homebrew/bin/codex app-server --stdio` (live adapter default on this tip) |
| claude | `/opt/homebrew/bin/claude` | **PRESENT** | `2.1.231 (Claude Code)` | `/opt/homebrew/bin/claude` plus print-mode flags in `floati/adapters/claude.py` |
| opencode | `/opt/homebrew/bin/opencode` | **PRESENT** | `1.18.9` | `/opt/homebrew/bin/opencode` (Car 4 default) |
| cursor | `/opt/homebrew/bin/cursor-agent` | **PRESENT** (wrapper exists; `--version` on this copy fails — see findings) | local-copy `--version`: `2026.07.09-a3815c0` | `/opt/homebrew/bin/cursor-agent` (Car 4 default). Editor launcher `cursor` is not on PATH. |
| cline | `/opt/homebrew/bin/cline` | **NOT PRESENT** | — | later row: BATTERY-ONLY / fake-harness |
| grok-build | `/opt/homebrew/bin/grok-build` | **NOT PRESENT** | — | later row: BATTERY-ONLY / fake-harness |
| pi | `/opt/homebrew/bin/pi` | **NOT PRESENT** | — | later row: BATTERY-ONLY / fake-harness |
| herdr | `/opt/homebrew/bin/herdr` | **NOT PRESENT** | — | later row: BATTERY-ONLY / fake-harness |

### surface_verified honesty bound for this weekend

`surface_verified: true` requires a real-binary receipt in the same later-row doc. From this inventory that bound is:

- **can be attempted live:** codex (C1), claude (C2), opencode (C3), cursor (C4, with the homebrew `--version` failure recorded below)
- **battery-only unless a binary appears later:** cline (C5), grok-build (C6), pi (C7), herdr (C8)

## PRESENT — exact receipts

### 1. codex — PRESENT

- PATH: `/opt/homebrew/bin/codex` → `/opt/homebrew/Caskroom/codex/0.150.0/bin/codex`
- file: Mach-O 64-bit executable arm64
- realpath size: 229004480 bytes
- realpath SHA-256: `27cc146d8a5781350e232998e12c52e35fe03d5eac945d38d11ca2bb35854c97`
- Homebrew cask: `codex`
- version command executable: `/opt/homebrew/bin/codex`

```text
argv: ["/opt/homebrew/bin/codex", "--version"]
exit: 0
stdout_bytes: 18
stdout: codex-cli 0.150.0
stderr_bytes: 0
```

How invoked by the live adapter on this tip (`floati/adapters/codex_live.py`): `("/opt/homebrew/bin/codex", "app-server", "--stdio")`.

### 2. claude — PRESENT (two copies; PATH prefers Homebrew)

PATH `command -v claude` = `/opt/homebrew/bin/claude`. `type -a` also lists `~/.local/bin/claude`.

**PATH / adapter-default copy** (this is the one C2 would hit unless PATH is rewritten):

- `/opt/homebrew/bin/claude` → `/opt/homebrew/Caskroom/claude-code/2.1.231/claude`
- file: Mach-O 64-bit executable arm64
- realpath size: 294720528 bytes
- realpath SHA-256: `ba790279cab6ef77b713864d4bf5f764fcea87d3a3eb7591a41f741e45212b5c`
- Homebrew cask: `claude-code`

```text
argv: ["/opt/homebrew/bin/claude", "--version"]
exit: 0
stdout_bytes: 22
stdout: 2.1.231 (Claude Code)
stderr_bytes: 0
```

**Second copy (not on adapter default):**

- `~/.local/bin/claude` → `~/.local/share/claude/versions/2.1.238`
- realpath size: 321263536 bytes
- realpath SHA-256: `1c196c456373b57818ae87df84aecee96cb659448c0d6a6bbb401ac5758431b2`

```text
argv: ["~/.local/bin/claude", "--version"]
exit: 0
stdout_bytes: 22
stdout: 2.1.238 (Claude Code)
stderr_bytes: 0
```

How invoked by the adapter on this tip: `("/opt/homebrew/bin/claude",)` plus headless print-mode arguments in `floati/adapters/claude.py`. Adjacent GUI `/Applications/Claude.app` is a different product (`com.anthropic.claudefordesktop`, `CFBundleShortVersionString` `1.37937.0`) and is not the harness binary.

### 3. opencode — PRESENT

- PATH: `/opt/homebrew/bin/opencode` → `/opt/homebrew/lib/node_modules/opencode-ai/bin/opencode.exe`
- file: Mach-O 64-bit executable arm64 (the `.exe` name is the npm layout; `file` reports Mach-O, not PE)
- realpath size: 138509666 bytes
- realpath SHA-256: `2348bf6751a04498090b2a301afcae4ff55785e0502ba76a42acba6617f3a7ea`
- npm global: `opencode-ai@1.18.9` (package.json version `1.18.9`; 1 of 4 global deps)

```text
argv: ["/opt/homebrew/bin/opencode", "--version"]
exit: 0
stdout_bytes: 7
stdout: 1.18.9
stderr_bytes: 0
```

How invoked (Car 4 default): `('/opt/homebrew/bin/opencode',)`. Adjacent GUI `/Applications/OpenCode.app` is `ai.opencode.desktop`, `CFBundleShortVersionString` `1.18.23` — not the same version as the CLI.

### 4. cursor — PRESENT as `cursor-agent` (roster name)

Car 4 `floati/adapters/cursor.py` default is `/opt/homebrew/bin/cursor-agent`, not `cursor`. `command -v cursor` is NOT IN PATH. Homebrew cask: `cursor-cli`.

**Adapter-default wrapper** exists:

- `/opt/homebrew/bin/cursor-agent` → `/opt/homebrew/Caskroom/cursor-cli/2026.07.09-a3815c0/dist-package/cursor-agent`
- file: Bourne-Again shell script text executable, ASCII text (1074 bytes)
- realpath SHA-256: `eed61c5224668c9236334c4c68936a16aecc37374b592f59e31eb50433817831`
- wrapper execs sibling `$SCRIPT_DIR/node` against `$SCRIPT_DIR/index.js`

```text
argv: ["/opt/homebrew/bin/cursor-agent", "--version"]
exit: 1
stdout_bytes: 0
stderr_bytes: 65536
stderr utf8_preview_200: /opt/homebrew/Caskroom/cursor-cli/2026.07.09-a3815c0/dist-package/index.js:414
</html>`;var u=n("./src/utils/open-browser.ts"),d=function(e,t,n,r){return new(n||(n=Promise))((function(s,i){function a(
stderr sha256 of captured blob: a7c510e602fa83bb53ce59e555acf3f39627a29d38920d58a96aa7094c5b1b25
```

**Second copy (same wrapper SHA-256; `--version` succeeds):**

- `~/.local/bin/cursor-agent` → `~/.local/share/cursor-agent/versions/2026.07.09-a3815c0/cursor-agent`

```text
argv: ["~/.local/bin/cursor-agent", "--version"]
exit: 0
stdout_bytes: 19
stdout: 2026.07.09-a3815c0
stderr_bytes: 0
```

**Editor launcher, not the roster binary:** `/Applications/Cursor.app` `CFBundleShortVersionString` `3.17.21` (`com.todesktop.230313mzl4w4u92`). Bundled `/Applications/Cursor.app/Contents/Resources/app/bin/cursor` `--version` stdout (executable named below):

```text
argv: ["/Applications/Cursor.app/Contents/Resources/app/bin/cursor", "--version"]
exit: 0
3.17.21
8f2a112cb2845a97b75fd932ea5c470579ca4060
arm64
```

stderr on that invocation also contained `task_name_for_pid: (os/kern) failure (5)` from Electron codesign (sandbox-visible; recorded, not treated as a version string).

How invoked (Car 4 default): `('/opt/homebrew/bin/cursor-agent',)`. C4 live exercise must receipt whichever executable it actually launches; this inventory does not flip `surface_verified`.

## NOT PRESENT

### 5. cline — NOT PRESENT

- `/opt/homebrew/bin/cline` absent
- `/usr/local/bin/cline` absent
- `~/.local/bin/cline` absent
- `command -v cline`: NOT IN PATH
- Cursor extensions dir exists with **18** entries and **0** name-hits for cline/claude-dev/saoudrizwan
- `~/.vscode/extensions` directory absent

Finding, not a blocker. C5 runs battery-only / fake-harness and says so.

### 6. grok-build — NOT PRESENT

- `/opt/homebrew/bin/grok-build` absent
- `command -v grok-build` and `command -v grok`: NOT IN PATH
- Adjacent GUI `/Applications/Grok Bot.app` is `com.anysphere.sand`, `CFBundleShortVersionString` `0.29.0` — not the named `grok-build` binary and not treated as one

Finding, not a blocker. C6 runs battery-only / fake-harness and says so.

### 7. pi — NOT PRESENT

- `/opt/homebrew/bin/pi` absent (this is the adapter default on this tip and on Car 4)
- `command -v pi`: NOT IN PATH
- `~/.cargo/bin` absent

Finding, not a blocker. C7 runs battery-only / fake-harness and says so.

### 8. herdr — NOT PRESENT

- `/opt/homebrew/bin/herdr` absent
- `command -v herdr`: NOT IN PATH
- `~/.config/herdr` absent
- `~/.config/herdr/herdr.sock` absent

Finding, not a blocker. C8 runs battery-only / fake-harness and says so.

## Findings (filed here; not fixed)

These are machine/install facts that bind later `surface_verified` claims. None were repaired.

1. **Claude dual-version.** Adapter default `/opt/homebrew/bin/claude` is `2.1.231`. A newer copy `2.1.238` sits at `~/.local/bin/claude`. PATH `command -v` returns Homebrew. C2 must name the executable it actually launched.
2. **OpenCode CLI vs app skew.** CLI `/opt/homebrew/bin/opencode --version` = `1.18.9`. `/Applications/OpenCode.app` = `1.18.23`. C3 live claims bind to the CLI path unless a receipt names the app bundle.
3. **cursor-agent Homebrew `--version` fails.** The Car 4 default path exists as a wrapper, but `/opt/homebrew/bin/cursor-agent --version` exits 1 with a 65536-byte stderr blob starting at `.../dist-package/index.js:414`. The `~/.local/bin/cursor-agent` copy of the same wrapper SHA-256 exits 0 with `2026.07.09-a3815c0`. C4 live invocation against the adapter default may fail; that failure would be a receipt, not a patch.
4. **Four named harnesses are absent** (cline, grok-build, pi, herdr). Their later rows are BATTERY-ONLY unless a binary is installed after this inventory.

No product source was edited. C1 (codex conformance, live) is next; C2..C8 wait for Fable's Car 4-landed announcement and a rebase onto that tip.

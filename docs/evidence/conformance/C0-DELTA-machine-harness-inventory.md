# C0-DELTA — machine harness re-probe (grok, 2026-08-27)

**Row:** C0 delta. Same instrument as `docs/evidence/conformance/C0-machine-harness-inventory.md`. Not a rename of binaries. Not C2.
**Seat:** `grok` (clean-room, `~/Projects/floati-grok`)
**Branch:** `refs/heads/lane/grok-conformance`
**Dispatch:** `msg-01a045592a1674429d5d8f8f656b5cd4` (already acked). Luna receipts were a pointer only; every flip below is from this re-probe.
**Probe UTC:** `2026-08-27T22:34:02Z` (JSON `started_utc`)
**Car 4:** still pending. C2 remains next when it lands.

Product source was not edited. No harness binary was renamed.

Untruncated machine-readable artifact (counts below are taken from this file, not from `head`):

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/conformance/C0-DELTA-machine-harness-inventory.json` | 18242 | `3b8e536b5d75facb931954280d65f04003935153cf356ca9989b49c7bd4b6afd` |

## Known-green control

A zero is only evidence beside a measured non-zero. Control executable: `/usr/bin/python3`.

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout_bytes: 13
stdout: Python 3.9.6
stderr_bytes: 0
```

`brew list --cask` exit 0, **7** names (unchanged vs C0). `brew list --formula` exit 0, **71** names (C0 was 70). `npm ls -g --depth=0 --json` exit 0, **8** dependencies (C0 was 4).

`brew list --cask` stdout, untruncated (7 names, executable `/opt/homebrew/bin/brew`):

```text
antigravity
antigravity-cli
claude-code
codex
cursor-cli
devin-cli
xdeck
```

No foreign-bus path was read or written.

## Protected harnesses (must be unchanged)

Compared to C0 JSON `realpath_sha256` and `--version` stdout. All four identities match C0.

| harness | path | `--version` stdout | realpath SHA-256 vs C0 |
|---|---|---|---|
| codex | `/opt/homebrew/bin/codex` | `codex-cli 0.150.0` exit 0, stdout_bytes 18 | `27cc146d8a5781350e232998e12c52e35fe03d5eac945d38d11ca2bb35854c97` **identical** |
| claude | `/opt/homebrew/bin/claude` | `2.1.231 (Claude Code)` exit 0, stdout_bytes 22 | `ba790279cab6ef77b713864d4bf5f764fcea87d3a3eb7591a41f741e45212b5c` **identical** |
| opencode | `/opt/homebrew/bin/opencode` | `1.18.9` exit 0, stdout_bytes 7 | `2348bf6751a04498090b2a301afcae4ff55785e0502ba76a42acba6617f3a7ea` **identical** |
| cursor-agent | `/opt/homebrew/bin/cursor-agent` | Homebrew `--version` exit 1, stdout_bytes 0, stderr_bytes 65536 | `eed61c5224668c9236334c4c68936a16aecc37374b592f59e31eb50433817831` **identical** |

Local copy still present at `~/.local/bin/cursor-agent` (same realpath SHA-256 as the Homebrew wrapper). Second claude copy still at `~/.local/bin/claude` → `2.1.238` SHA-256 `1c196c456373b57818ae87df84aecee96cb659448c0d6a6bbb401ac5758431b2` (same as C0; not the PATH adapter default).

## Newly PRESENT (this measurement)

| roster name | PATH / adapter default | `--version` | how invoked (adapter default or override) | live-eligible? |
|---|---|---|---|---|
| cline (C5) | `/opt/homebrew/bin/cline` PRESENT → `/opt/homebrew/lib/node_modules/cline/bin/cline` SHA-256 `71d1b27aeeebdaa0b91c4babddd1f635eb7f5783257c2b3324a2cb22924cf60f` | `3.0.60` exit 0, stdout_bytes 7 | Car 4 default `('/opt/homebrew/bin/cline',)` | **yes** |
| pi (C7) | `/opt/homebrew/bin/pi` PRESENT → `@earendil-works/pi-coding-agent` SHA-256 `1c3a5094b54aae9ae98c66516ce8c6578140363d081471ca7e91f9cb8c23dc8a` | `0.84.3` exit 0, stdout_bytes 7 | `("/opt/homebrew/bin/pi", "--mode", "rpc", "--no-session")` | **yes** |
| herdr (C8) | `/opt/homebrew/bin/herdr` PRESENT → Cellar `0.8.2` SHA-256 `3e0f0c2d5edc41f592963ef90f5d872db801cc7dbd0e01731023897ee428904a` | `herdr 0.8.2` exit 0, stdout_bytes 12 | `/opt/homebrew/bin/herdr` | **yes** |
| t3 (C9) | `/opt/homebrew/bin/t3` PRESENT → `/opt/homebrew/lib/node_modules/t3/dist/bin.mjs` SHA-256 `a5ad9c28388c835f30e1119502ba5a31613937d2820b281fdf3ba64aff4285b0` | `t3 v0.0.35` exit 0, stdout_bytes 11 | `/opt/homebrew/bin/t3` (no adapter; compatibility session) | **yes** (CLI now exists; C9 bus-verb session still not run) |
| grok-build (C6) | `/opt/homebrew/bin/grok-build` **ABSENT** (`command -v grok-build` empty) | skipped | Car 4 default names `grok-build` | see mismatch |

### C6 name mismatch (measured)

- `/opt/homebrew/bin/grok-build`: NOT PRESENT.
- `/opt/homebrew/bin/grok`: PRESENT → `/opt/homebrew/lib/node_modules/@xai-official/grok/bin/grok` SHA-256 `13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0`
- argv `["/opt/homebrew/bin/grok", "--version"]` exit 0 stdout `grok 1.0.5 (5115b46bc909)` stdout_bytes 26

C6 may go live only by **executable override** to `/opt/homebrew/bin/grok`. Do not rename the vendor binary to `grok-build`. The adapter default path remains absent until Car 4 code is run with that override (or the adapter is changed by a build seat — not this seat).

## Eligibility bound after this delta

| row | C0 bound | C0-DELTA bound |
|---|---|---|
| C2 claude | live-eligible | live-eligible (unchanged) |
| C3 opencode | live-eligible | live-eligible (unchanged) |
| C4 cursor | live-eligible (Homebrew `--version` fail stands) | unchanged |
| C5 cline | BATTERY-ONLY | **live-eligible** |
| C6 grok-build | BATTERY-ONLY | **live-eligible only via `/opt/homebrew/bin/grok` override**; default `grok-build` path still absent |
| C7 pi | BATTERY-ONLY | **live-eligible** |
| C8 herdr | BATTERY-ONLY | **live-eligible** |
| C9 t3 | INVENTORY+PLAN-ONLY (no CLI) | **live-eligible** (CLI `t3 v0.0.35`); prior C9 doc is not a bus-verb receipt and is not rewritten |

C2 is still next, and still waits Car 4. This delta does not start C5–C8 or re-run C9.

## npm global (untruncated names from this probe)

Executable `/opt/homebrew/bin/npm`. Eight dependencies: `@earendil-works/pi-coding-agent@0.84.3`, `@moonshot-ai/kimi-code@0.30.0`, `@sourcegraph/amp@0.0.1784524244-g589322`, `@xai-official/grok@1.0.5`, `cline@3.0.60`, `npm@11.17.0`, `opencode-ai@1.18.9`, `t3@0.0.35`.

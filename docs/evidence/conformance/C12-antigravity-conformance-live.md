# C12 — Antigravity conformance, per surface (grok, 2026-08-28)

**Roster cut:** `docs/design/harness-market-roster-2026-08-28.md`.
**Surfaces:** **cli + desktop** (dual). `antigravity` PATH name ABSENT; CLI name is `agy`.
**Auth:** not touched. No Google sign-in. App not `open`ed.

Product source was not edited. Wiring N/A (Google account, not OpenRouter).

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

## CLI surface (two copies)

PATH / Homebrew cask `antigravity-cli`:

```text
argv: ["/opt/homebrew/bin/agy", "--version"]
exit: 0
stdout: 1.1.5
realpath: /opt/homebrew/Caskroom/antigravity-cli/1.1.5,5958982624477184/antigravity
auth_prompt_suspected: false
```

Second copy (architect-cited path):

```text
argv: ["~/.local/bin/agy", "--version"]
exit: 0
stdout: 1.1.22
```

`--help` (PATH copy) names `--continue`, `--conversation`, `plugin` / `plugins`. No `hook` word. `auth_prompt_suspected: false`.

**`surface_verified` (cli):** **true** — PATH `agy --version` launched. Cells must name **which copy**. This row's live CLI stamp is PATH `1.1.5` unless a later receipt launches `1.1.22`.

## Desktop surface

```text
app: /Applications/Antigravity.app
CFBundleIdentifier: com.google.antigravity
CFBundleShortVersionString: 2.11.0
MacOS: /Applications/Antigravity.app/Contents/MacOS/Antigravity
file: Mach-O 64-bit executable arm64 (52880 bytes)
```

Application Support/Antigravity exists (Chromium cache names listed; no token files read). JSON/plist grep for `hooks` inside the `.app`: no hits.

**GUI not launched** (would be a sign-in surface). **`surface_verified` (desktop inventory):** **true** as a real app bundle + plist + Mach-O. **`surface_verified` (desktop session):** **false**.

## Scratch live-root (floati bus labels)

Same capture as C11: `docs/evidence/gauntlet/captures/C11-C12-live-root.json`.
Scratch: `~/Projects/floati-grok/.gauntlet-scratch/c12-live-root`

| step | exit | result |
|---|---:|---|
| init `--solo grok-c12 --harness Antigravity` | 0 | tenant `c12-live-root` |
| send | 0 | `msg-01a046497d0378d8b9ca79ce0457b051` |
| ack | 0 | `ack-01a046497d9e7eb6b753c7ae4fee31d3` |
| inbox after ack | 31 | intentional silence |

Not an `agy`-driven session.

## Row verdict

| check | verdict |
|---|---|
| CLI PATH `agy` 1.1.5 launched | PASS |
| CLI `~/.local/bin/agy` 1.1.22 launched | PASS (second copy; dual-version finding) |
| desktop bundle + Mach-O | PASS (inventory) |
| desktop GUI session | NOT LAUNCHED |
| credential / sign-in | NOT RUN |
| **surface_verified cli** | **true** (name the copy) |
| **surface_verified desktop session** | **false** |

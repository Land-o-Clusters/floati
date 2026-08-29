# C11 — Devin conformance, CLI live (grok, 2026-08-28)

**Roster cut:** `docs/design/harness-market-roster-2026-08-28.md` (Devin + Antigravity).
**Surface:** **cli only** on this machine (dual-surface law). `/Applications/Devin.app` absent.
**Executable:** `/opt/homebrew/bin/devin`
**Version:** `devin 3000.2.17 (2c489dfc)`
**`surface_verified` (cli):** **true** — `--version` and `--help` launched this binary.
**`surface_verified` (desktop):** **false** — no desktop bundle.
**Auth:** not touched. `devin auth` was not run. `~/.config/devin` directory names were listed; `config.json` contents were not read.

Product source was not edited. Wiring N/A (vendor account, not OpenRouter).

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

## Real-binary receipts

```text
argv: ["/opt/homebrew/bin/devin", "--version"]
exit: 0
stdout: devin 3000.2.17 (2c489dfc)
auth_prompt_suspected: false
```

```text
argv: ["/opt/homebrew/bin/devin", "--help"]
exit: 0
stdout_bytes: 3834
```

`--help` names `auth`, `plugins`, `list` sessions, `cloud`. Only `--help` / `--version` / `plugins --help` were launched. No credential prompt in those outputs.

```text
argv: ["/opt/homebrew/bin/devin", "plugins", "--help"]
exit: 0
```

## Scratch live-root (floati bus labels, not a Devin session)

Capture: `docs/evidence/gauntlet/captures/C11-C12-live-root.json` (13594 bytes, sha256 `3b40060b3c3929bbfd78962fd48aa1225359e2bd36f96aef02869587979d3651`).
Scratch: `~/Projects/floati-grok/.gauntlet-scratch/c11-live-root`

| step | exit | result |
|---|---:|---|
| init `--solo grok-c11 --harness Devin` | 0 | tenant `c11-live-root` |
| send | 0 | `msg-01a046497b7a7cbca299f4b56e43a4cc` |
| inbox | 0 | presentation of that id |
| ack | 0 | `ack-01a046497c117937bdd5bba9ae82e477` |
| inbox after ack | 31 | intentional silence |

This is **not** a Devin-driven session. It proves the floati harness string `Devin` is accepted. `surface_verified` for Devin the product is the `--version` launch above.

## Row verdict

| check | verdict |
|---|---|
| CLI present + launched | PASS (`3000.2.17`) |
| desktop app | ABSENT (measured) |
| credential / sign-in | NOT RUN |
| **surface_verified cli** | **true** |
| **surface_verified desktop** | **false** |

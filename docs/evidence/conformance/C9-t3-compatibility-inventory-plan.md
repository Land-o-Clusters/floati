# C9 — t3 code compatibility: INVENTORY+PLAN-ONLY (grok, 2026-08-27)

**Row:** C9 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md` (owner addition; matrix data is C10).
**Dispatch:** `msg-01a0454c0d0e74df92f4b201e8e881ec` @ `2e86da4c227cf143d9a86a6217efbb83cdee7d3c`.
**Stamp:** **INVENTORY+PLAN-ONLY**. No adapter expected. Bus verbs from a t3-driven session were **not** run. Nothing was simulated.
**`surface_verified`:** **false** (no t3-driven session receipt in this doc).

## Known-green control

Executable: `/usr/bin/python3`

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout_bytes: 13
stdout: Python 3.9.6
```

Non-zero search corpus beside every empty t3 hit: `brew list --cask` 7 names, `brew list --formula` 70 names, `npm ls -g --depth=0 --json` 4 dependencies (same machine as C0).

## Inventory (this machine, 2026-08-27T22:21:53Z)

Untruncated artifacts:

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/conformance/C9-t3-inventory.json` | 2913 | `9af45523308bd1e9db9cbaf484e0bed4774e5922fd5396731add5008cf35e4b6` |
| `docs/evidence/conformance/C9-t3-app-bundle.json` | 4518 | `b478393a9bba8731e8e39cda10810da642efdd99e9823b50c903aeafddb15918` |

### CLI names — NOT PRESENT

`/usr/bin/command -v` for `t3`, `t3-code`, `t3code`, `t3_code`, `t3code-cli`, `t3-cli`: all empty.

Absent at `/opt/homebrew/bin`, `/usr/local/bin`, and `~/.local/bin` for those six names.

Homebrew: `brew list --cask` exit 0, 7 lines, 0 t3-name hits. `brew list --formula` exit 0, 70 lines, 0 t3-name hits. npm global deps: 4 names, 0 t3-name hits.

C0 did not enumerate t3 (the original eight-name list). That omission is why this row exists.

### GUI bundle — PRESENT, not a bus-verb session

Named bundle: `/Applications/T3 Code (Nightly).app`

- identifier: `com.t3tools.t3code`
- `CFBundleShortVersionString`: `0.0.35-nightly.20260826.1195`
- GUI executable: `/Applications/T3 Code (Nightly).app/Contents/MacOS/T3 Code (Nightly)` (53328 bytes, Mach-O 64-bit executable arm64)
- no `Contents/Resources/app/bin` directory (unlike Cursor.app's editor launcher)

This is an editor/GUI install, not a PATH CLI and not a t3-driven floati session. It was **not launched**. It does not flip `surface_verified` and does not stand in for init/register/send/inbox/ack.

## What was not done (by order)

The compatibility proof is: from a t3-code-driven session, init a scratch root, register, send, inbox, ack, and record the wake path for WS-A. Architect: binary NOT PRESENT per C0; row is inventory+plan until one appears; **never simulate**. No fake node, no scripted stand-in for t3, no GUI automation pretending to be a t3 session.

## Plan when a t3-driven session exists

Scratch root under this seat's path only (`…/floati-grok/.conformance-scratch/…`), never the puddle-fleet root.

1. From that session, name the executable actually launched.
2. `floati init --root <scratch> --solo <node> --harness <exact harness string t3 reports>`
3. `floati register` if init did not already register.
4. `floati send` / `inbox --as <node>` / `ack --as <node> --id <msg>` — exact artifacts, exact exits.
5. Wake path for WS-A (observe, do not invent): stop-hook / plugin / daemon / **none**. Receipt must say which, and name the files or configs read. Unobserved stays `unknown`, never `none` guessed.
6. Only then may a C10 matrix cell appear. Stamp `live` only with this session receipt in-doc. Otherwise keep INVENTORY+PLAN-ONLY / no cell.

## Row verdict

| check | verdict |
|---|---|
| PATH CLI `t3` / `t3-code` / aliases | NOT PRESENT |
| GUI `/Applications/T3 Code (Nightly).app` | PRESENT (inventory only; not launched) |
| t3-driven init/register/send/inbox/ack | NOT RUN |
| wake path | NOT OBSERVED |
| `surface_verified` | false |
| **row stamp** | **INVENTORY+PLAN-ONLY** |

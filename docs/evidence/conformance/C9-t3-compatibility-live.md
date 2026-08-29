# C9 — t3 code compatibility, CLI live, no t3-driven bus verbs (grok, 2026-08-27)

**Row:** C9 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Prior stamp:** `docs/evidence/conformance/C9-t3-compatibility-inventory-plan.md` (INVENTORY+PLAN-ONLY; CLI was absent then)
**Car 4 tip:** `c30fdf0699d7471a7e5f04c9b863dd9b103270be`
**Prefix HEAD before this row:** `f60d775916bdbb807144c2c40d9d2ec252599873`
**Seat:** `grok` (clean-room)
**Executable named for Python:** `/usr/bin/python3`
**t3 executable:** `/opt/homebrew/bin/t3`
**Adapter:** none expected
**`surface_verified`:** **false** for t3-driven bus verbs (none were run). The CLI binary itself was launched (receipts below). Nothing was simulated as a t3 session.

Product source was not edited.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

## CLI identity

```text
argv: ["/opt/homebrew/bin/t3", "--version"]
exit: 0
stdout_bytes: 11
stdout: t3 v0.0.35
```

```text
argv: ["/opt/homebrew/bin/t3", "--help"]
exit: 0
stdout_bytes: 2635
```

`t3 --help` names subcommands `start`, `serve`, `pair`, `auth`, `project`, `service`, `triage`, `connect`. It does not name `floati`, `init`, `send`, `inbox`, `ack`, or hooks.

## Wake path observed

Bounded `t3 serve` (scratch data dir; not puddle-fleet):

```text
argv: ["/opt/homebrew/bin/t3", "serve", "--no-browser", "--base-dir", "~/Projects/floati-grok/.conformance-scratch/c9-t3-home"]
cwd: ~/Projects/floati-grok/.conformance-scratch/c9-t3-home
timeout_s: 8.0
timed_out: true
exit: 124
stdout_bytes: 3792
stdout_sha256: 975fa0751ab028a89f6109cc98bceeaf5bbd98b2ef08dd36512b75cfea2f32f4
```

Named fields from that capture (pairing token and QR are **not** copied here and are not committed):

- log line: `Listening on http://127.0.0.1:3773`
- log line: `T3 Code server is ready.`
- `Connection string: http://localhost:3773`
- A pairing URL and QR were printed (secrets redacted)

Wake for WS-A: **HTTP/WebSocket server**, optional `t3 serve` headless pairing, `--no-browser`. No Cursor/launchd hook was observed. No floati waiter was installed.

## Bus verbs from a t3-driven session

**Not run.** t3 exposes a Code server, not floati bus verbs. Running `floati init/send/inbox/ack` from this grok seat would not be a t3-driven session, so it was refused. Nothing was simulated.

## Defects filed (not fixed)

1. **t3 has no floati bus subcommands.** Compatibility row cannot flip `surface_verified` for bus verbs without a t3-driven session.
2. Car 4 generated-tree scrub hit unchanged. Not edited.

## Row verdict

| check | verdict |
|---|---|
| `/opt/homebrew/bin/t3 --version` | PASS (`t3 v0.0.35`) |
| `t3 serve --no-browser` (bounded) | PASS (server listened on 127.0.0.1:3773) |
| t3-driven init/send/inbox/ack | NOT RUN (no floati verbs on t3) |
| **surface_verified** (bus verbs) | **false** |

C10 matrix collection is next. No foreign-bus path was read or written.

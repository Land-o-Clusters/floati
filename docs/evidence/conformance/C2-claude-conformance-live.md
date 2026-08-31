# C2 — claude conformance, live (grok, 2026-08-27)

**Row:** C2 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Dispatch:** `msg-01a04591136d7dbdafcdeb2fdce66e48` (Car 4 landed; rebase then C2..C8 in order)
**Seat:** `grok` (clean-room)
**Branch:** `refs/heads/lane/grok-conformance` rebased onto `c30fdf0699d7471a7e5f04c9b863dd9b103270be` (`origin/integrate/weekend-20260828`)
**Prefix HEAD before this row:** `8fc289d3b0b590eed9b2fdef3ffd47604b10b1f1`
**Executable named for Python:** `/usr/bin/python3`
**Claude executable:** `/opt/homebrew/bin/claude`
**`surface_verified`:** **true** — real `/opt/homebrew/bin/claude` receipts in this doc (`--version` plus print-mode JSON envelope). The print-mode turn itself returned an auth error; that is filed, not repaired.

Product source was not edited.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout_bytes: 13
stdout: Python 3.9.6
stderr_bytes: 0
```

A measured non-zero suite exists beside every later count: claude/roster battery `Ran 20 tests` OK.

## Check 1 — parity battery for this profile

Exact command, cwd repo root:

```text
/usr/bin/python3 -m unittest tests.test_claude_adapter tests.test_roster_adapters
```

Untruncated stderr (119 bytes, sha256 `d9319aa0f9aa6698e069fe95e9439d307e5cffac0ca67fd16a8f0f37e2f70bbf`):

```text
Ran 20 tests in 1.186s

OK
```

exit: 0. stdout_bytes: 0. Started `2026-08-27T23:35:29Z`, ended `2026-08-27T23:35:30Z`.

**Verdict: PASS**

## Check 2 — `python3 -m floati.conformance --live-root-smoke`

Exact command:

```text
/usr/bin/python3 -m floati.conformance --live-root-smoke
```

Untruncated stdout (34 bytes, sha256 `c5fd2646ececd4f3ce87149df9a20daea8cdd240f54800b78deafd3bb9de2187`), copied to `docs/evidence/conformance/C2-live-root-smoke.json`:

```text
{"cases":5,"status":"conformant"}
```

stderr: 0 bytes. exit: 0.

**Verdict: PASS**

## Check 3 — live-root exercise (scratch root created by this seat)

Root (created here, not the puddle-fleet root, not `/tmp`):

`~/Projects/floati-grok/.conformance-scratch/c2-live-root`

| step | executable | exit | result |
|---|---|---:|---|
| init | `/usr/bin/python3 -m floati init --root <root> --solo grok-c2 --harness Claude` | 0 | `status=ok`, `tenant_id=c2-live-root`, `solo.node_id=grok-c2`, `solo.harness=Claude`, `authority_epoch=1` |
| send | `/usr/bin/python3 -m floati send --root <root> --from grok-c2 --to grok-c2 --repo floati --sha 8fc289d3b0b590eed9b2fdef3ffd47604b10b1f1 --doc docs/evidence/conformance/C0-DELTA-machine-harness-inventory.md --note "C2 live-root control envelope"` | 0 | `msg-01a045946b8e7ab68b343b51adc1602d` |
| inbox | `/usr/bin/python3 -m floati inbox --root <root> --as grok-c2` | 0 | 1 message, `delivery-01a0459475ae747e83b315fed9cc3f91`, `presentation_count=1` |
| ack | `/usr/bin/python3 -m floati ack --root <root> --as grok-c2 --id msg-01a045946b8e7ab68b343b51adc1602d` | 0 | `ack-01a045948d767de29cd55eec1642ee41` |
| inbox after ack | same inbox command | 31 | `intentional_silence`, `messages=[]` |

Ledger counts from the untruncated files (byte and newline counts; not `head`):

| file | exists | bytes | lines |
|---|---|---:|---:|
| `events.jsonl` | true | 473 | 1 |
| `receipts/deliveries/grok-c2.jsonl` | true | 259 | 1 |
| `receipts/acks/grok-c2.jsonl` | true | 226 | 1 |

**Verdict: PASS**

## Real-binary receipts (required for `surface_verified: true`)

### Version

```text
argv: ["/opt/homebrew/bin/claude", "--version"]
exit: 0
stdout_bytes: 22
stdout: 2.1.231 (Claude Code)
stderr_bytes: 0
```

### Live print-mode (adapter argv, scratch cwd)

Scratch cwd created here: `~/Projects/floati-grok/.conformance-scratch/c2-claude-workspace`

How invoked by the live adapter on this tip: `("/opt/homebrew/bin/claude",)` plus `_HEADLESS_ARGUMENTS` (`-p --input-format text --output-format json --permission-mode dontAsk --no-session-persistence --tools Read,Write,Edit -- <title>`).

```text
argv: ["/opt/homebrew/bin/claude", "-p", "--input-format", "text", "--output-format", "json", "--permission-mode", "dontAsk", "--no-session-persistence", "--tools", "Read,Write,Edit", "--", "Reply with the single word pong."]
cwd: ~/Projects/floati-grok/.conformance-scratch/c2-claude-workspace
timeout_s: 90.0
timed_out: false
exit: 1
stdout_bytes: 882
stderr_bytes: 0
stdout_sha256: f7c9b3843c8c4106f796cdd6398f148ea2ddbc063357b7d7f7fe586a4bf8e7cc
```

Untruncated stdout copied to `docs/evidence/conformance/C2-claude-print-probe.json` (882 bytes). Parsed fields named from that file:

- `type`: `result`
- `is_error`: true
- `terminal_reason`: `api_error`
- `result`: `Failed to authenticate: OAuth session expired and could not be refreshed`

The real binary started, accepted the adapter argv, and spoke the print-mode JSON envelope. The turn did not succeed. Auth is an environment finding, not an absent-binary finding.

## Defects filed (not fixed)

1. **Live Claude print-mode auth expired.** Exact adapter argv against `/opt/homebrew/bin/claude` exit 1, JSON `is_error=true`, `result` = OAuth session expired and could not be refreshed. Not repaired by this seat.
2. **Generated-tree scrub has 1 hit at this tip.** `floati.scrub.scan_generated_tree(Path.cwd())` returns `docs/evidence/WEEKEND-TRAIN-CAR-4-MANIFEST-CONTRACT-HARNESS-ROSTER.md`. That file arrived with Car 4; C2 did not add it and does not edit it.

## Row verdict

| check | verdict |
|---|---|
| unittest `tests.test_claude_adapter` + `tests.test_roster_adapters` (20) | PASS |
| conformance --live-root-smoke | PASS (5 cases) |
| scratch-root init/send/inbox/ack | PASS |
| real `/opt/homebrew/bin/claude --version` | PASS (`2.1.231 (Claude Code)`) |
| real print-mode JSON envelope | PASS (process spoke `type=result`; turn FAIL auth) |
| **surface_verified** | **true** |

C3 opencode is next. No foreign-bus path was read or written.

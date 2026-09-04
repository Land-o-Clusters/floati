# C2 — claude conformance, live (grok, 2026-09-04)

**Row:** C2, re-measured for MX-3 at declared current `2.1.251 (Claude Code)`.
**Seat:** grok · **Branch:** `lane/grok-mx-3` off main `4167d27c`.
**Product source was not edited.**
The 2026-08-27 receipt `C2-claude-conformance-live.md` is a photograph and
was not rewritten.

Declared executable (canonical file, not a symlink, not PATH-discovered):
the same target named by `C2-claude-cli-version-2026-09-03.md`. Homebrew
`/opt/homebrew/bin/claude` still resolves to Cask `2.1.231` and was not
selected.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
stderr_bytes: 0
```

## Check 1 — parity battery for this profile

```text
/usr/bin/python3 -m unittest tests.test_claude_adapter tests.test_roster_adapters
Ran 20 tests in 1.241s
OK
```

exit: 0. **Verdict: PASS**

## Check 2 — `python3 -m floati.conformance --live-root-smoke`

```text
{"cases":5,"status":"conformant"}
```

exit: 0. **Verdict: PASS**

## Check 3 — live-root exercise (scratch root created by this seat)

Root (created here, not the the fleet root):
`.conformance-scratch/c2-live-root-20260904` (untracked).

Current inbox contract requires `--session` or `--peek`. This run used
`--peek` then an explicit `ack --session mx3-c2-20260904`.

| step | exit | result |
|---|---:|---|
| init `--solo grok-c2 --harness Claude` | 0 | `status=ok`, `tenant_id=c2-live-root-20260904`, `solo.node_id=grok-c2`, `authority_epoch=1` |
| send control envelope at `4167d27c` | 0 | `msg-01a06d4fd89e79409ec816d89717fabb` |
| inbox `--peek` | 0 | 1 message, `delivery-01a06d4fd92678e0a1829a725b2722b3`, `presentation_count=1` |
| ack `--id` that message | 0 | `ack-01a06d4ff40177b6854bb3da6154a42b` |
| inbox `--session` after ack | 31 | `intentional_silence`, `messages=[]` |

Ledger counts:

| file | exists | bytes | lines |
|---|---|---:|---:|
| `events.jsonl` | true | 463 | 1 |
| `receipts/deliveries/grok-c2.jsonl` | true | 268 | 1 |
| `receipts/acks/grok-c2.jsonl` | true | 362 | 1 |

**Verdict: PASS**

## Real-binary receipts (required for `surface_verified: true`)

### Version (FCD20 C2 row)

`python3 -m floati.fcd20_conformance --claude-executable <canonical 2.1.251>`
aggregate `status: degraded` only because C1 and C3–C9 were undeclared.
C2 row: `measured`, exit 0, `2.1.251 (Claude Code)`, stdout 22 bytes,
sha256 `1aaadbe01265e82bd28c2e2639a2a6a0604edbb124f997e9d3a4d09b823c6fb8`
(byte-identical to the 2026-09-03 declared-current photograph).

Direct `--version`: exit 0, stderr 0 bytes, same stdout digest.

### Live print-mode (adapter argv)

Exact adapter argv (`-p --input-format text --output-format json
--permission-mode dontAsk --no-session-persistence --tools Read,Write,Edit --
Reply with the single word pong.`), timeout 90 s, did not time out, exit 0,
stderr 0 bytes, stdout 1692 bytes, sha256
`2aaf8e8710f582e213bb6496b574f2c46a1d3aae8785307d398f372ac00ac51f`.

Parsed envelope fields:

- `type`: `result`
- `subtype`: `success`
- `is_error`: false
- `result`: `pong`
- `session_id`: `44e4f84d-1351-4e15-851f-603c3d83cca9`
- `stop_reason`: `end_turn`

The 2026-08-27 print probe was an OAuth expiry (`is_error: true`). This
re-measure is an authenticated success at 2.1.251. Billing fields from the
CLI envelope are not copied into this class-1 receipt; the digest above
binds the untruncated stdout retained off-tree.

## Row verdict

| check | verdict |
|---|---|
| unittest `tests.test_claude_adapter` + `tests.test_roster_adapters` (20) | PASS |
| conformance --live-root-smoke | PASS (5 cases) |
| scratch-root init/send/inbox/ack | PASS |
| declared 2.1.251 `--version` / FCD20 C2 | PASS (`2.1.251 (Claude Code)`) |
| print-mode JSON envelope | PASS (`type=result`, `is_error=false`, `result=pong`) |
| **surface_verified** | **true** |

No workflow, tag, public issue, or product file was changed by the
measurement. The Homebrew 2.1.231 cask remains installed and unused.

# C3 — opencode conformance, live (grok, 2026-08-27)

**Row:** C3 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Seat:** `grok` (clean-room)
**Branch:** `refs/heads/lane/grok-conformance` on Car 4 tip `c30fdf0699d7471a7e5f04c9b863dd9b103270be`
**Prefix HEAD before this row:** `1a5a9c3f679af7807df86888c9c7f9c9fa36ec92`
**Executable named for Python:** `/usr/bin/python3`
**OpenCode executable:** `/opt/homebrew/bin/opencode`
**`surface_verified`:** **true** — real `/opt/homebrew/bin/opencode` receipts in this doc (`--version` plus a bounded spawn of the adapter argv). `headless_arguments` is empty on this tip; no flags were invented.

Product source was not edited.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout_bytes: 13
stdout: Python 3.9.6
```

A measured non-zero suite exists beside later counts: F3 parity battery `Ran 22 tests` OK.

## Check 1 — F3 roster parity battery for this profile

Exact command, cwd repo root:

```text
/usr/bin/python3 -m unittest tests.test_roster_parity_battery tests.test_roster_adapters
```

Untruncated stderr 10394 bytes, sha256 `2a2374a92aaaad96cd08e1f333876581dbb81898a6ae3e46fead093835580847`. Summary lines from that file:

```text
Ran 22 tests in 1.430s

OK
```

exit: 0. stdout_bytes: 0. Started `2026-08-27T23:38:54Z`, ended `2026-08-27T23:38:56Z`.

Stderr also contains `ResourceWarning: unclosed file` from `tests/test_roster_parity_battery.py` (filed below). Zero FAIL, zero ERROR.

**Verdict: PASS**

## Check 2 — `python3 -m floati.conformance --live-root-smoke`

```text
/usr/bin/python3 -m floati.conformance --live-root-smoke
```

Untruncated stdout (34 bytes, sha256 `c5fd2646ececd4f3ce87149df9a20daea8cdd240f54800b78deafd3bb9de2187`):

```text
{"cases":5,"status":"conformant"}
```

exit: 0.

**Verdict: PASS**

## Check 3 — live-root exercise (scratch root created by this seat)

`~/Projects/floati-grok/.conformance-scratch/c3-live-root`

| step | executable | exit | result |
|---|---|---:|---|
| init | `/usr/bin/python3 -m floati init --root <root> --solo grok-c3 --harness OpenCode` | 0 | `tenant_id=c3-live-root`, `solo.harness=OpenCode` |
| send | `--from grok-c3 --to grok-c3 --sha 1a5a9c3f679af7807df86888c9c7f9c9fa36ec92` | 0 | `msg-01a0459795e5776cbe8b49a29aa6be25` |
| inbox | `--as grok-c3` | 0 | `delivery-01a04597964579fdb76a5885503704cb`, `presentation_count=1` |
| ack | `--id msg-01a0459795e5776cbe8b49a29aa6be25` | 0 | `ack-01a04597b2627aad9cb750afd9022817` |
| inbox after ack | same | 31 | `intentional_silence` |

| file | bytes | lines |
|---|---:|---:|
| `events.jsonl` | 465 | 1 |
| `receipts/deliveries/grok-c3.jsonl` | 259 | 1 |
| `receipts/acks/grok-c3.jsonl` | 226 | 1 |

**Verdict: PASS**

## Real-binary receipts (required for `surface_verified: true`)

### Version

```text
argv: ["/opt/homebrew/bin/opencode", "--version"]
exit: 0
stdout_bytes: 7
stdout: 1.18.9
```

### Bounded spawn of adapter argv (headless_arguments empty)

How invoked by the live adapter on this tip: `('/opt/homebrew/bin/opencode',)` plus `headless_arguments=()`. No extra flags.

```text
argv: ["/opt/homebrew/bin/opencode"]
cwd: ~/Projects/floati-grok/.conformance-scratch/c3-opencode-workspace
timeout_s: 3.0
timed_out: true
exit: 124
stdout_bytes: 3464
stderr_bytes: 0
stdout_sha256: 351f8a28a081ed8a3e52454c704943f72d309de0a2d2ebbb9ccd27fd24a94230
```

The process started and wrote a TUI byte stream (not JSON). It did not exit on its own within 3s. That matches empty `headless_arguments`: this seat does not invent a serve/print spelling. The binary was launched.

## Defects filed (not fixed)

1. **F3 parity battery ResourceWarning.** `tests/test_roster_parity_battery.py` emits unclosed-file ResourceWarnings while still reporting OK. Not repaired.
2. **OpenCode headless_arguments empty.** Adapter spawn of the real binary is an interactive TUI; no cited print-mode spelling exists on this tip. Bounded timeout recorded instead of an invented flag.
3. **Generated-tree scrub still 1 hit** at Car 4 file `docs/evidence/WEEKEND-TRAIN-CAR-4-MANIFEST-CONTRACT-HARNESS-ROSTER.md`. Not edited.

## Row verdict

| check | verdict |
|---|---|
| unittest F3 parity + roster oracle (22) | PASS (ResourceWarnings filed) |
| conformance --live-root-smoke | PASS (5 cases) |
| scratch-root init/send/inbox/ack | PASS |
| real `/opt/homebrew/bin/opencode --version` | PASS (`1.18.9`) |
| real adapter argv spawn | PASS (process started; timed_out as expected) |
| **surface_verified** | **true** |

C4 cursor is next. No foreign-bus path was read or written.

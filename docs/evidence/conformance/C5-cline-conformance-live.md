# C5 — cline conformance, live (grok, 2026-08-27)

**Row:** C5 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Seat:** `grok` (clean-room)
**Branch:** `refs/heads/lane/grok-conformance` on Car 4 tip `c30fdf0699d7471a7e5f04c9b863dd9b103270be`
**Prefix HEAD before this row:** `b4dc9803ef917a74dea181d35361f63ff0e5d464`
**Executable named for Python:** `/usr/bin/python3`
**Cline executable:** `/opt/homebrew/bin/cline`
**`surface_verified`:** **true** — real `/opt/homebrew/bin/cline` receipts in this doc. `headless_arguments` is empty; bare spawn refused for lack of a TTY. No flags invented.

Product source was not edited.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

## Check 1 — F3 roster parity battery

```text
/usr/bin/python3 -m unittest tests.test_roster_parity_battery tests.test_roster_adapters
```

stderr 10394 bytes, sha256 `50f3d9d279290f74b49431100eaa1462a3f62b73c377a877faa6629ea75cb3c2`.

```text
Ran 22 tests in 1.316s

OK
```

exit: 0. Started `2026-08-27T23:41:29Z`.

**Verdict: PASS**

## Check 2 — live-root-smoke

stdout 34 bytes, sha256 `c5fd2646ececd4f3ce87149df9a20daea8cdd240f54800b78deafd3bb9de2187`, `{"cases":5,"status":"conformant"}`. exit 0.

**Verdict: PASS**

## Check 3 — live-root exercise

`~/Projects/floati-grok/.conformance-scratch/c5-live-root`

| step | exit | result |
|---|---:|---|
| init `--solo grok-c5 --harness Cline` | 0 | `tenant_id=c5-live-root` |
| send `--sha b4dc9803ef917a74dea181d35361f63ff0e5d464` | 0 | `msg-01a04599cbcf7bf68ca4328b7972cef6` |
| inbox | 0 | `delivery-01a04599cc197941a252253e6c0bfbd9` |
| ack | 0 | `ack-01a04599e40879eea59e9767842215ef` |
| inbox after ack | 31 | `intentional_silence` |

| file | bytes | lines |
|---|---:|---:|
| `events.jsonl` | 465 | 1 |
| `receipts/deliveries/grok-c5.jsonl` | 259 | 1 |
| `receipts/acks/grok-c5.jsonl` | 226 | 1 |

**Verdict: PASS**

## Real-binary receipts

Adapter default: `('/opt/homebrew/bin/cline',)` plus empty `headless_arguments`.

### Version

```text
argv: ["/opt/homebrew/bin/cline", "--version"]
exit: 0
stdout_bytes: 7
stdout: 3.0.60
```

### Bare spawn (adapter argv)

```text
argv: ["/opt/homebrew/bin/cline"]
cwd: ~/Projects/floati-grok/.conformance-scratch/c5-cline-workspace
timeout_s: 5.0
timed_out: false
exit: 1
stdout_bytes: 0
stderr_bytes: 86
stderr_sha256: b903f349a4cabfcff4cd038813694f91a7e8c80bfaa88dc0ec7b7292c7f3f6f4
```

stderr (ANSI stripped): `error: interactive mode requires a TTY (stdin/stdout must both be terminals)`

## Defects filed (not fixed)

1. **Cline bare spawn needs a TTY.** Adapter `headless_arguments` is empty; without a cited print-mode spelling the real binary exits 1. Not repaired; flags not invented.
2. F3 parity ResourceWarnings. Not repaired.
3. Car 4 generated-tree scrub hit unchanged. Not edited.

## Row verdict

| check | verdict |
|---|---|
| F3 parity + roster oracle (22) | PASS |
| live-root-smoke | PASS |
| scratch-root verbs | PASS |
| `/opt/homebrew/bin/cline --version` | PASS (`3.0.60`) |
| bare adapter argv spawn | FAIL (TTY required; binary launched) |
| **surface_verified** | **true** |

C6 grok-build (override to `/opt/homebrew/bin/grok`) is next. No foreign-bus path was read or written.

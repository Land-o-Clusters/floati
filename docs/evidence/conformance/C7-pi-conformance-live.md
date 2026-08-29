# C7 — pi conformance, live (grok, 2026-08-27)

**Row:** C7 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Car 4 tip:** `c30fdf0699d7471a7e5f04c9b863dd9b103270be`
**Prefix HEAD before this row:** `818f0be9ecd952e63ef173a8905259cfbbbd0ae4`
**Seat:** `grok` (clean-room)
**Executable named for Python:** `/usr/bin/python3`
**F3 observation default:** `/opt/homebrew/bin/pi-observation` — **ABSENT** (`command -v` exit 1)
**Live executable launched:** `/opt/homebrew/bin/pi` (cited argv from `floati/adapters/pi.py`, not invented)
**`surface_verified`:** **true** — real `/opt/homebrew/bin/pi` receipts in this doc. Binary was not renamed to `pi-observation`.

Product source was not edited.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

## Check 1 — profile + F3 parity battery

```text
/usr/bin/python3 -m unittest tests.test_pi_adapter tests.test_roster_parity_battery tests.test_roster_adapters
```

stderr 11014 bytes, sha256 `364aff7eef11b139659f199c73698f1216c6238fcb0b82083f4d682f4b07601b`.

```text
Ran 32 tests in 3.707s
FAILED (failures=1)
```

exit: 1. Started `2026-08-27T23:43:46Z`.

FAIL name (exactly one):

`test_session_distinguishes_malformed_response_and_timeout` (`tests.test_pi_adapter.PiRpcSessionTests`) `(mode='malformed')` — expected `protocol_error`, got `process_timeout`.

F3 parity/oracle tests in the same run were not among the FAIL list.

**Verdict: FAIL** (one pi-adapter contract test; filed, not fixed)

## Check 2 — live-root-smoke

stdout 34 bytes, sha256 `c5fd2646ececd4f3ce87149df9a20daea8cdd240f54800b78deafd3bb9de2187`, `{"cases":5,"status":"conformant"}`. exit 0.

**Verdict: PASS**

## Check 3 — live-root exercise

`~/Projects/floati-grok/.conformance-scratch/c7-live-root`

| step | exit | result |
|---|---:|---|
| init `--solo grok-c7 --harness Pi` | 0 | `tenant_id=c7-live-root` |
| send `--sha 818f0be9ecd952e63ef173a8905259cfbbbd0ae4` | 0 | `msg-01a0459be7557e5d95a6470089d0a4cb` |
| inbox | 0 | `delivery-01a0459be7a873c6936ca3daa8f3e836` |
| ack | 0 | `ack-01a0459c13557802a3afe5c1fb31982c` |
| inbox after ack | 31 | `intentional_silence` |

| file | bytes | lines |
|---|---:|---:|
| `events.jsonl` | 464 | 1 |
| `receipts/deliveries/grok-c7.jsonl` | 259 | 1 |
| `receipts/acks/grok-c7.jsonl` | 226 | 1 |

**Verdict: PASS**

## Real-binary receipts

Cited live argv from `floati/adapters/pi.py`: `("/opt/homebrew/bin/pi", "--mode", "rpc", "--no-session")`.

### Version

```text
argv: ["/opt/homebrew/bin/pi", "--version"]
exit: 0
stdout_bytes: 7
stdout: 0.84.3
```

### Cited RPC argv

```text
argv: ["/opt/homebrew/bin/pi", "--mode", "rpc", "--no-session"]
cwd: ~/Projects/floati-grok/.conformance-scratch/c7-pi-workspace
timeout_s: 5.0
timed_out: false
exit: 0
stdout_bytes: 0
stderr_bytes: 0
```

## Defects filed (not fixed)

1. **`pi-observation` default path absent.** F3 roster adapter names `/opt/homebrew/bin/pi-observation`. Vendor CLI is `/opt/homebrew/bin/pi`. Not renamed.
2. **`test_pi_adapter` malformed-mode expects `protocol_error`, got `process_timeout`.** One FAIL in 32. Not repaired.
3. F3 parity ResourceWarnings. Not repaired.
4. Car 4 generated-tree scrub hit unchanged. Not edited.

## Row verdict

| check | verdict |
|---|---|
| unittest pi + F3 (32) | FAIL (1 pi-adapter) |
| live-root-smoke | PASS |
| scratch-root verbs | PASS |
| `/opt/homebrew/bin/pi --version` | PASS (`0.84.3`) |
| cited `--mode rpc --no-session` | PASS (exit 0) |
| **surface_verified** | **true** |

C8 herdr is next. No foreign-bus path was read or written.

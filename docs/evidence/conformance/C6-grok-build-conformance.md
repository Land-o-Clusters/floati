# C6 — grok-build conformance, live via executable override (grok, 2026-08-27)

**Row:** C6 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Skeleton:** `docs/evidence/conformance/C6-grok-build-override-skeleton.md`
**Car 4 tip:** `c30fdf0699d7471a7e5f04c9b863dd9b103270be`
**Prefix HEAD before this row:** `f7a4b9560a01e321b92bba29fa43adc3d3109447`
**Seat:** `grok` (clean-room)
**Executable named for Python:** `/usr/bin/python3`
**Adapter default path:** `/opt/homebrew/bin/grok-build` — **ABSENT**
**Override executable launched:** `/opt/homebrew/bin/grok`
**`surface_verified`:** **true** — real `/opt/homebrew/bin/grok` receipts in this doc. Binary was not renamed.

Product source was not edited.

## Override (measured this row)

`/usr/bin/command -v grok-build` empty, exit 1.

```text
GrokBuildAdapter.availability()
{"harness": "grok-build", "binary": "/opt/homebrew/bin/grok-build", "present": false, "surface_verified": false}

GrokBuildAdapter.availability(command=("/opt/homebrew/bin/grok",))
{"harness": "grok-build", "binary": "/opt/homebrew/bin/grok", "present": true, "surface_verified": false}
```

The class attribute `surface_verified` stays False (charter override 5 on the adapter). This row's stamp is independent and is true because the override binary was launched here. Constructor used: `GrokBuildAdapter(('/opt/homebrew/bin/grok',))`. `headless_arguments` remain `()`.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

## Check 1 — F3 roster parity battery

Unsandboxed capture (the sandboxed attempt of this row is discarded; it is not a product FAIL):

```text
/usr/bin/python3 -m unittest tests.test_roster_parity_battery tests.test_roster_adapters
```

stderr 10394 bytes, sha256 `8b25924e996dce5b57199c748c1a79a202a2257464b74ebf759c141c402017a4`.

```text
Ran 22 tests in 1.349s

OK
```

exit: 0. Started `2026-08-27T23:42:40Z`.

**Verdict: PASS**

## Check 2 — live-root-smoke

stdout 34 bytes, sha256 `c5fd2646ececd4f3ce87149df9a20daea8cdd240f54800b78deafd3bb9de2187`, `{"cases":5,"status":"conformant"}`. exit 0.

**Verdict: PASS**

## Check 3 — live-root exercise

`~/Projects/floati-grok/.conformance-scratch/c6-live-root`

| step | exit | result |
|---|---:|---|
| init `--solo grok-c6 --harness Grok` | 0 | `tenant_id=c6-live-root` |
| send `--sha f7a4b9560a01e321b92bba29fa43adc3d3109447` | 0 | `msg-01a0459ad8257c188cfdab62adabf9c5` |
| inbox | 0 | `delivery-01a0459ad87a7c638ee29fba067383ba` |
| ack | 0 | `ack-01a0459b394c7422a200946da9a07513` |
| inbox after ack | 31 | `intentional_silence` |

| file | bytes | lines |
|---|---:|---:|
| `events.jsonl` | 464 | 1 |
| `receipts/deliveries/grok-c6.jsonl` | 259 | 1 |
| `receipts/acks/grok-c6.jsonl` | 226 | 1 |

## Real-binary receipts (override target)

### Version

```text
argv: ["/opt/homebrew/bin/grok", "--version"]
exit: 0
stdout_bytes: 26
stdout: grok 1.0.5 (5115b46bc909)
```

### Bare spawn of override argv (empty headless_arguments)

```text
argv: ["/opt/homebrew/bin/grok"]
cwd: ~/Projects/floati-grok/.conformance-scratch/c6-grok-workspace
timeout_s: 5.0
timed_out: false
exit: 1
stderr_bytes: 42
stderr_sha256: 3a3d034c7ce776a805a8bd25f51aed96e3dff14498f5cc8bafcb645f13b53107
stderr: Error: Device not configured (os error 6)
```

## Defects filed (not fixed)

1. **Adapter default `grok-build` is absent.** Live row used executable override to `/opt/homebrew/bin/grok`. Binary was not renamed. Build seat may change the default; this seat does not.
2. **Bare `grok` spawn exits 1** with `Device not configured` when stdout is not a TTY. `headless_arguments` empty; no flags invented.
3. Adapter class `surface_verified` remains False even when `present` is true for the override path.
4. F3 parity ResourceWarnings. Not repaired.
5. Car 4 generated-tree scrub hit unchanged. Not edited.

## Row verdict

| check | verdict |
|---|---|
| F3 parity + roster oracle (22) | PASS |
| live-root-smoke | PASS |
| scratch-root verbs | PASS |
| default `grok-build` present | FAIL (absent; expected) |
| override `/opt/homebrew/bin/grok --version` | PASS (`grok 1.0.5 (5115b46bc909)`) |
| **surface_verified** | **true** (override executable named and launched) |

C7 pi is next. No foreign-bus path was read or written.

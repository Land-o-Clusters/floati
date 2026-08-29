# C4 — cursor conformance, live (grok, 2026-08-27)

**Row:** C4 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Seat:** `grok` (clean-room)
**Branch:** `refs/heads/lane/grok-conformance` on Car 4 tip `c30fdf0699d7471a7e5f04c9b863dd9b103270be`
**Prefix HEAD before this row:** `d27714f2c26c01d1cf5e93bb17a7afaa4792ddfd`
**Executable named for Python:** `/usr/bin/python3`
**Cursor executable launched (adapter default):** `/opt/homebrew/bin/cursor-agent`
**`surface_verified`:** **true** — real `/opt/homebrew/bin/cursor-agent` was launched (receipts below). Homebrew `--version` still fails; that is filed. A second copy at `~/.local/bin/cursor-agent` prints a version string; it is not the adapter default.

Product source was not edited. The 7.3 MiB stderr dump is kept in scratch only (not committed).

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout_bytes: 13
stdout: Python 3.9.6
```

## Check 1 — F3 roster parity battery

```text
/usr/bin/python3 -m unittest tests.test_roster_parity_battery tests.test_roster_adapters
```

Untruncated stderr 10394 bytes, sha256 `2e552b5c8f8d31d582f29a5be9b9c718152efefe6bee2f86f3e6f22038019491`.

```text
Ran 22 tests in 1.583s

OK
```

exit: 0. Started `2026-08-27T23:40:22Z`.

**Verdict: PASS**

## Check 2 — live-root-smoke

```text
/usr/bin/python3 -m floati.conformance --live-root-smoke
```

stdout 34 bytes, sha256 `c5fd2646ececd4f3ce87149df9a20daea8cdd240f54800b78deafd3bb9de2187`, `{"cases":5,"status":"conformant"}`. exit 0.

**Verdict: PASS**

## Check 3 — live-root exercise

`~/Projects/floati-grok/.conformance-scratch/c4-live-root`

| step | exit | result |
|---|---:|---|
| init `--solo grok-c4 --harness Cursor` | 0 | `tenant_id=c4-live-root` |
| send `--sha d27714f2c26c01d1cf5e93bb17a7afaa4792ddfd` | 0 | `msg-01a04598c5e57157875588b430d68984` |
| inbox | 0 | `delivery-01a04598c63475269e16617fa47ffcfe` presentation_count=1 |
| ack | 0 | `ack-01a04598e3e47e6c9cbe6e6e918ae28e` |
| inbox after ack | 31 | `intentional_silence` |

| file | bytes | lines |
|---|---:|---:|
| `events.jsonl` | 467 | 1 |
| `receipts/deliveries/grok-c4.jsonl` | 259 | 1 |
| `receipts/acks/grok-c4.jsonl` | 226 | 1 |

**Verdict: PASS**

## Real-binary receipts

Adapter default on this tip: `('/opt/homebrew/bin/cursor-agent',)` plus empty `headless_arguments`.

### Adapter-default `--version` (Homebrew wrapper)

```text
argv: ["/opt/homebrew/bin/cursor-agent", "--version"]
exit: 1
stdout_bytes: 0
stderr_bytes: 7336599
stderr_sha256: c72c1f1d8854fb91f892b6f3247cc94d196f283f2ad7c5569ce8a54bb1bc3731
timeout_s: 15.0
timed_out: false
```

### Adapter-default spawn (empty headless_arguments)

```text
argv: ["/opt/homebrew/bin/cursor-agent"]
cwd: ~/Projects/floati-grok/.conformance-scratch/c4-cursor-workspace
timeout_s: 5.0
timed_out: false
exit: 1
stdout_bytes: 0
stderr_bytes: 7336599
```

The wrapper was executed. It dumped a JavaScript blob to stderr and exited 1. No flags were invented.

### Second copy (not launched by the adapter)

```text
argv: ["~/.local/bin/cursor-agent", "--version"]
exit: 0
stdout_bytes: 19
stdout: 2026.07.09-a3815c0
```

## Defects filed (not fixed)

1. **Homebrew `cursor-agent --version` and bare spawn both exit 1** with a 7336599-byte stderr dump. Same wrapper SHA as C0/C0-DELTA. Local copy `--version` succeeds. Adapter still points at the Homebrew path. Not repaired.
2. F3 parity ResourceWarnings (same as C3). Not repaired.
3. Car 4 generated-tree scrub hit unchanged. Not edited.

## Row verdict

| check | verdict |
|---|---|
| F3 parity + roster oracle (22) | PASS |
| live-root-smoke | PASS |
| scratch-root verbs | PASS |
| launched `/opt/homebrew/bin/cursor-agent` | PASS (process started; `--version` and spawn both exit 1) |
| **surface_verified** | **true** (executable named and launched) |

C5 cline is next. No foreign-bus path was read or written.

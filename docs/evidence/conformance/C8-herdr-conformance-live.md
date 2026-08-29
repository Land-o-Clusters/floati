# C8 — herdr conformance, live, no adapter (grok, 2026-08-27)

**Row:** C8 of `docs/design/brief-grok-ws-c-conformance-2026-08-27.md`
**Car 4 tip:** `c30fdf0699d7471a7e5f04c9b863dd9b103270be`
**Prefix HEAD before this row:** `3353fed2166c562a5c1dc82cea8ee3af3c08a701`
**Seat:** `grok` (clean-room)
**Executable named for Python:** `/usr/bin/python3`
**Herdr executable:** `/opt/homebrew/bin/herdr`
**Adapter module:** **absent** on this tip (no `floati/adapters/herdr.py`). F3 roster battery does not include herdr.
**`surface_verified`:** **true** — real `/opt/homebrew/bin/herdr` receipts in this doc.

Product source was not edited.

## Known-green control

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

## Check 1 — roster parity battery for this profile

No herdr adapter and no herdr tests on this tip. Not run. Not claimed as PASS.

**Verdict: N/A (no adapter)**

## Check 2 — live-root-smoke

stdout 34 bytes, sha256 `c5fd2646ececd4f3ce87149df9a20daea8cdd240f54800b78deafd3bb9de2187`, `{"cases":5,"status":"conformant"}`. exit 0.

**Verdict: PASS**

## Check 3 — live-root exercise

`~/Projects/floati-grok/.conformance-scratch/c8-live-root`

| step | exit | result |
|---|---:|---|
| init `--solo grok-c8 --harness Herdr` | 0 | `tenant_id=c8-live-root` |
| send `--sha 3353fed2166c562a5c1dc82cea8ee3af3c08a701` | 0 | `msg-01a0459ce9bc7af391c9c0f77aeb7da2` |
| inbox | 0 | `delivery-01a0459cea1a7daaafbed439d07ac1b3` |
| ack | 0 | `ack-01a0459d09157a51a07b66c2f1748de1` |
| inbox after ack | 31 | `intentional_silence` |

| file | bytes | lines |
|---|---:|---:|
| `events.jsonl` | 461 | 1 |
| `receipts/deliveries/grok-c8.jsonl` | 259 | 1 |
| `receipts/acks/grok-c8.jsonl` | 226 | 1 |

**Verdict: PASS**

## Real-binary receipts

### Version

```text
argv: ["/opt/homebrew/bin/herdr", "--version"]
exit: 0
stdout_bytes: 12
stdout: herdr 0.8.2
```

### Vendor `--help` (not adapter argv)

```text
argv: ["/opt/homebrew/bin/herdr", "--help"]
exit: 0
stdout_bytes: 3498
stdout_sha256: 198d82d8e313e4a8394d2bae801eb0077584359c10bb366bd59c6eb3efe1b97a
```

First line: `herdr — terminal workspace manager for AI coding agents`

### Bare spawn

```text
argv: ["/opt/homebrew/bin/herdr"]
cwd: ~/Projects/floati-grok/.conformance-scratch/c8-herdr-workspace
timeout_s: 3.0
timed_out: false
exit: 101
stderr_bytes: 346
stderr_sha256: 2341afb8ac662ec497a63a95d72f532b0b3fe827718805e18d73b5cf1cb9ded3
```

stderr includes: `failed to initialize terminal: Os { code: 6, kind: Uncategorized, message: "Device not configured" }`

## Defects filed (not fixed)

1. **No herdr adapter on Car 4.** Live binary exists; F3 parity battery cannot cover this profile.
2. **Bare `herdr` panics without a TTY.** Not repaired; no adapter headless spelling exists to cite.
3. Car 4 generated-tree scrub hit unchanged. Not edited.

## Row verdict

| check | verdict |
|---|---|
| F3 parity for herdr | N/A (no adapter) |
| live-root-smoke | PASS |
| scratch-root verbs | PASS |
| `/opt/homebrew/bin/herdr --version` | PASS (`herdr 0.8.2`) |
| **surface_verified** | **true** |

C9 t3 live bus-verb session is next. No foreign-bus path was read or written.

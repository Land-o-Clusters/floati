# C6 skeleton — grok-build executable override (NOT THE ROW)

**Stamp:** **SKELETON**. This is not C6. C2 remains next after Car 4 lands.
**Dispatch:** `msg-01a0455eea8275b2b1a69b8ca2dfc87d` (Fable: C9 + C0-DELTA gated PASS; optional filler = this skeleton).
**Seat:** `grok` (clean-room)
**Branch:** `refs/heads/lane/grok-conformance`
**`surface_verified`:** **false** — no C6 battery, no live adapter turn, no headless-argument citation.

Product source was not edited. The vendor binary was not renamed.

When Car 4 lands, copy this file to `docs/evidence/conformance/C6-grok-build-conformance.md` and fill the empty slots at the merged tip. Do not start that copy until C2..C5 have run in order.

## Ruling (gated, on record)

C6 runs only via an **executable override** receipted in the C6 doc. Binaries are never renamed to fit an adapter. Adapter default name remains `grok-build`. Vendor executable name is `grok`.

## Known-green control (this skeleton)

Executable: `/usr/bin/python3`

```text
argv: ["/usr/bin/python3", "--version"]
exit: 0
stdout: Python 3.9.6
```

## Override identity (measured; not a live adapter session)

C0-DELTA @ `c1044487ef295737cffddbba472465672a1b012d` (gated PASS). Re-checked in this skeleton turn (not derived from the Fable note).

| name | PATH | `--version` | realpath SHA-256 |
|---|---|---|---|
| adapter default | `/opt/homebrew/bin/grok-build` | skipped (`/usr/bin/command -v grok-build` empty) | absent |
| override target | `/opt/homebrew/bin/grok` → `/opt/homebrew/lib/node_modules/@xai-official/grok/bin/grok` | `grok 1.0.5 (5115b46bc909)` exit 0 | `13a2405556fe9e86108731a801771db1b9a742ef11e934ab2cb886f1089aeef0` |

```text
argv: ["/opt/homebrew/bin/grok", "--version"]
exit: 0
stdout: grok 1.0.5 (5115b46bc909)
```

Forbidden on this machine: `mv`, copy, or symlink of `/opt/homebrew/bin/grok` onto `/opt/homebrew/bin/grok-build`.

## How the override is applied (Car 4, path-only)

Unread as product source. Path-only from `origin/u2/manifest-contract` `floati/adapters/grok_build.py`:

- Default command: `('/opt/homebrew/bin/grok-build',)` — **ABSENT** here.
- Constructor override: `GrokBuildAdapter(('/opt/homebrew/bin/grok',))` — `source` is an absolute command sequence, not a rename.
- Availability probe: `GrokBuildAdapter.availability(command=('/opt/homebrew/bin/grok',))`
- `headless_arguments`: remain `()` until live intake **cites** a spelling. Do not invent flags.

## Slots to fill when C6 actually runs (after C2..C5, at Car 4 tip)

| slot | value now | fill with |
|---|---|---|
| Car 4 tip SHA | empty | rebase target Fable announces |
| unittest discover | not run | exact argv, Ran N, FAIL/ERROR names, exit |
| `python3 -m floati.selftest` | not run | exact argv, Ran N, exit, adapter-contract lines |
| `python3 -m floati.conformance --live-root-smoke` | not run | untruncated JSON + sha256 |
| scratch-root init/send/inbox/ack | not run | new root under this clone; never puddle-fleet; never `/private/tmp` trees this seat did not create |
| live adapter argv | not run | must be `("/opt/homebrew/bin/grok",)` plus any **cited** headless args |
| `surface_verified` | false | true only if the real `/opt/homebrew/bin/grok` receipt lives in the same C6 doc |

C6 is not started. C2 waits Car 4.

No foreign-bus path was read or written.

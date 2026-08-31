# MX1 — zcode/cli wake measurement (2026-08-30)

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** zcode / cli / wake — claimed `daemon`, grade `classified` at seed.
**Base:** the CI-1 landing merge base.

## Measured, this machine, today

- **Executable named and launched:** `/opt/homebrew/bin/node` running
  `/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs` (zcode CLI
  0.16.5, the version the scoping photograph measured). The pair is the
  pinned adapter command; `zcode.cjs --json --no-color --prompt …` was
  exercised live first (typed artifact, rc=0).
- **Wake surface:** zcode carries Claude-Code's hook event vocabulary,
  but hooks sit behind a trust ceremony (`workspaceHookReview`) that a
  headless turn cannot answer — measured null across four candidate hook
  paths earlier in the campaign. The daemon path — resume the bound
  session headless — is the wake mechanism: `--resume <sessionId>` parses
  (advertised-and-refused sweep found it live), and the resume+prompt
  combination was exercised end-to-end (typed artifact naming the same
  `sessionId`, rc=0).
- **The daemon, armed in a SCRATCH fleet root** (a purpose-built direct
  home; never the operating fleet): exact consent ledger record → exact
  adapter binding (session, workspace, executable digest) → digest-bound
  LaunchAgent installed and started → `status: running`.
- **Wake OBSERVED ON ARRIVAL, twice.** Mail sent to the bound node; the
  daemon observed each arrival on its own poll and fired the resume turn.
  The honest attempt history: the first two attempts refused — the
  provider returned `[1305]`/529 overload and the resume turns died in
  retry; the daemon's backoff cycled and both messages then woke.
  **One refusal is noise; refused-after-retries is signal** — the arrival
  guarantee holds at the daemon level, not the attempt level.
- **The woken seat read its inbox, receipted on the bus.** After each
  wake the delivered mail was drained and acknowledged — delivery
  receipt, then acknowledgment, then the `outcome: woke` attempt receipt,
  in that order, for both wakes. Health surfaces agreed: zero
  undelivered, drain recorded, wake identity inside registry lineage,
  daemon runtime back to idle with no reason code.
- **Idempotency in the ledger:** a message already woken produced no
  further wake attempts; attempts only re-fired for new undelivered
  items.

## Captures (sha256-pinned, committed under `captures/wd-r2-zcode-wake/`)

- `wake-attempts.jsonl` 8a87a45b94af345ba4c5be9af2777f70a7b5c77962cab4dbe4faf33c061b8104
  (refused, refused, woke, woke — the full attempt history)
- `seat-acks.jsonl` 8b59608508dca7a245708e824345ac1c9ae3243749c99c06537df828a3fc9820
- `seat-deliveries.jsonl` b3f9d38b925bc5949ffff01e1bd2f73a77addc4a3cee49a16a8b42f5c2f34171
- `daemon-runtime.json` 9932487531790d91145af5de026f3ebac8a5748cd821592d021710315ae34269
  (idle, reason cleared, after the wakes)
- `seat-turn1-artifact.json` 758ae53e62b947d90274f94d2c7362db8fab12e492a8e2a629c31128e535c497
- `seat-turn2-artifact.json` 6a59c3e73f5b0144948bfffa3d5f43c3c0d6b199ec24e421d0da9a053c3c8830
- `manual-repro-artifact.json` 08d9592954bf35dda54b9e180b0b52406502aaa6a5c037f74a88af64d435632b
- `manual-repro-stderr.txt` aa2a227d6ee0ec610945d5787ae68b8ef164fec4fefdc15bf51e90155b2f2d57
  (the `[1305]` overload signature on a turn that still completed rc=0)
- `sha256s.txt` 5eb55faca14f15296e67b5f8f5bbc5d331faef3d2bd0df64b012e65f70841a40

## Conclusion — cell value `daemon`, GRADE MEASURED

**zcode / cli · wake = daemon is measured, not classified:** the wake was
observed arriving — a message caused a stopped seat to run — with the
receipt chain on the bus and the harness absent from the loop. The hook
path stays the later upgrade behind its ruled preconditions; a hook that
could not activate was never a measured wake surface.

The matrix cell stamp edit rides the ruled matrix commit, not this
receipt; per the campaign contract this document is the observation
receipt that edit cites.

**No seat names, no fleet coordinates, no tenant identifiers appear in
this receipt** — it is written for the published tree.

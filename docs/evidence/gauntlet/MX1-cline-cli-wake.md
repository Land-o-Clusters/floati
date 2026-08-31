# MX1 M3 — cline/cli wake measurement (2026-08-29) — VALUE CHANGED daemon → event-driven

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** cline / cli / wake — claimed `daemon`, grade `classified` at seed.
**Base:** harbor main `90bd2f62`.

## Measured, this machine, today

- **Executable named and launched:** `/opt/homebrew/bin/cline` → `3.0.60` (same version
  the posture photograph classified).
- **Surface enumeration at 3.0.60** (`--help`, captured whole): **`--acp` — "Run in
  Agent Client Protocol (ACP) mode"** — a surface the 08-28 posture row did not name
  (it graded on the `--zen`/`cline hub` daemon and `--hooks-dir` hook scripts) ·
  `-p/--plan` plan mode · `--auto-approve` · `cline hook` (stdin payload handler) ·
  hub verbs · default prompt behavior is ACT MODE WITH AUTO-APPROVE (named here because
  it is the sharp edge of this CLI's headless path; the probe avoided it via ACP with
  fs capabilities declared false and a no-tools instruction).
- **The event channel exercised end-to-end, push observed:** spoke ACP over stdio
  exactly as with devin (M7): `initialize` → agentInfo cline/3.0.60 with authMethods ·
  `session/new` → session with plan/act modes, models listing a configured provider ·
  `session/prompt` "Reply with exactly: WAKE-PROBE-OK. Do not use any tools." →
  **8 server-initiated `session/update` notifications** streaming the probe answer in
  `agent_message_chunk` frames ("WA","KE","-P","RO","BE","-",…) → `stopReason: end_turn`
  in 2.9 s. One configured-provider turn was the whole spend. No files touched.

## The value change, reasoned

The cell claimed `daemon`. The measured fact is a **process-lifetime push channel that
fired**: an attached client is woken by server-initiated notifications — the SAME
mechanism, protocol, and evidence shape that grade devin/cli, pi/cli, and t3/cli as
`event-driven` in this matrix. Grading identical measured mechanisms differently by
harness would make the column meaningless, and the campaign contract is explicit that a
cell changes to what was measured. **Value: `event-driven`.**

Preserved open question (was the classifier's stated hold): whether cline's resident HUB
(`--zen` / `cline hub`) can serve as a COLD-wake path for a Floati adapter remains
unproven — this receipt neither proves nor disproves it. Cold seats still need a starter
for every CLI in this matrix; `event-driven` here scopes, as for devin/pi/t3, to the
process lifetime.

**Gate flag:** this is the campaign's first value CHANGE, and the taxonomy call is
recorded above for the gate to ratify or overrule; the raw evidence stands either way.

## Captures (sha256-pinned, committed under `captures/mx1-cline-cli-wake/`)

- `cline-help.txt` 5eb3b0a80e8dd33501f32f4b4d8996390d21604e8719f39caee8e3ecb1c06242
- `cline-version.txt` f5f15202f18fd38a2a332fd2e44ee6e4fc2357a69b2db25580ccc242b2688a5e
- `cline-acp-handshake.txt` d310d8dc74840a0ea6d31fc49d69dfd32c9ef0a6157912cf57b59b2209873234
- `cline-acp-push-observation.txt` 59b3869041c4a767e0b06033c5e9a8f44a9687c896d722b534d03a2da89be329

## Conclusion

**cline / cli · wake = event-driven — MEASURED at 3.0.60, value changed from the
classified `daemon`.** Stamp edit rides this commit: value + `classified → measured`,
receipt_path here, `measured_at 2026-08-29`, grids re-rendered from the dataset.

# MX1 M8 — antigravity/cli wake measurement (2026-08-29)

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** antigravity / cli / wake — claimed `event-driven`, grade `classified` at seed.
**Base:** harbor main `90bd2f62`.

## Measured, this machine, today

- **Executable named and launched:** `~/.local/bin/agy` → `1.1.22` — the user-local
  install the AD-1 adapter binds (`Path.home()/.local/bin/agy`), not the 1.1.5 cask.
- **Surface enumeration at 1.1.22** (`--help`, captured whole): zero `hook` words ·
  print mode with **`--input-format stream-json` / `--output-format stream-json`** —
  "reads one NDJSON message per line from stdin and runs a turn for each", streamed
  NDJSON events out — the event surface · `--mode accept-edits|plan` ·
  `--continue`/`--conversation` resume · `--dangerously-skip-permissions` (named as the
  sharp edge; NOT used) · plugin verbs. No sign-in verb exists in the help, and no auth
  refusal occurred: the installed CLI ran a live turn as-is.
- **The event stream exercised end-to-end:**
  `agy --mode plan --output-format stream-json -p='Reply with exactly: WAKE-PROBE-OK'`
  → exit 0 in 4.8 s, **5 typed NDJSON events streamed over the living process**:
  `init` (conversation id, tool roster) → `step_update` user DONE → `step_update` agent
  ACTIVE → `step_update` agent DONE → `result` `status: SUCCESS` with `response:
  "WAKE-PROBE-OK\n"` verbatim. Plan mode; no files touched; one turn was the whole spend.
- **Honest negative, named:** `-p` given as a bare flag swallows the next token as its
  prompt — `agy -p --mode plan …` exits 2 with a typed, self-explaining refusal
  (captured): the CLI names the mistake and the fix rather than running the wrong thing.

## Captures (sha256-pinned, committed under `captures/mx1-antigravity-cli-wake/`)

- `agy-help.txt` c26943c81bf16cf55fb35e6152eda42de30f6e09cd671e29dcbc22bc5517fde6
- `agy-version.txt` 9b3b81e5314efb5d2f9c0897cfd1621c3aaa2b83e900ad599d07a28b314b5b80
- `agy-probe-stream.txt` 0b83666e69f1acf0a732aa84fc3c8984edb11841737f9608a0dd62ca6c4a9fe2
- `agy-probe-stderr.txt` e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 (empty)
- `agy-probe-time.txt` a4f99b6762147becda153db62b891c32b2225007dabafd65e9dcf850ac12dbb9
- `agy-argv-refusal.txt` bdd1d5ed0458b4dd2fde447ddeec151e80dce3f4939ef92546be104b65b28d6c

## Conclusion

**antigravity / cli · wake = event-driven — MEASURED at 1.1.22.** The stream-json channel
emits typed events over the living process — init, step transitions, and the final
result carrying the probe answer verbatim — and NDJSON-in/NDJSON-out supports a resident
multi-turn event loop. Same scope qualifier as pi/t3/devin/cline: events flow for the
process lifetime; a cold seat still needs a starter. Cell value unchanged; stamp edit
rides this commit: `classified → measured`, receipt_path here, `measured_at 2026-08-29`,
grids re-rendered from the dataset.

# MX1 M5 — pi/cli wake measurement (2026-08-29)

**Role:** measurement lane · **Brief:** `docs/design/mx1-measurement-campaign-2026-08-29.md`
**Cell:** pi / cli / wake — claimed `event-driven`, grade `classified` at seed.
**Base:** harbor main `d18d039f`.

## Measured, this machine, today

- **Executable named and launched:** `/opt/homebrew/bin/pi` → `0.84.3` (matches the
  classified cell).
- **Surface enumeration at 0.84.3** (`--help`, captured whole): zero `hook` words ·
  `--mode text|json|rpc` output modes · `-p/--print` non-interactive · `--no-session`
  ephemeral · `--no-tools` · extension verbs (`pi install|remove|update|list`) ·
  per-provider auth (`pi auth check`). Provider readiness measured without spending a
  turn: xai / anthropic / openai / openai-codex / google / groq all `not_ready`,
  **openrouter `ready`** — the one live provider carried the probe.
- **The event-driven wake exercised end-to-end, push observed:** spoke the SAME rpc
  dialect the shipped `floati/adapters/pi.py` uses — spawned
  `pi --mode rpc --no-session --no-tools --provider openrouter`, wrote
  `{"id":1,"type":"prompt","message":"Reply with exactly: WAKE-PROBE-OK"}` on stdin, and
  the living process pushed **58 events** over stdout: `response(success)` →
  `agent_start` · `turn_start` · 2×`message_start/end` · **48 `message_update` frames**
  (the probe text arriving verbatim inside the stream) · `turn_end` · `agent_end` ·
  `agent_settled`, complete in 5.5 s. One openrouter turn was the whole spend.
- **Honest negative, named:** `pi --provider openrouter -p --mode rpc '<prompt>'` with
  the prompt on ARGV exits 0 in 0.18 s with zero bytes on both streams — rpc mode reads
  requests from stdin and silently ignores an argv prompt; the empty capture is retained.
  A green exit with empty output here is a no-op, not a cheap probe path.
- **Scope honesty:** same qualifier as t3/devin — events flow for the PROCESS LIFETIME;
  a cold pi seat has nothing running to push, which is what `event-driven` scopes.

## Captures (sha256-pinned, committed under `captures/mx1-pi-cli-wake/`)

- `pi-help.txt` 894957a01dfbf97ec0723edadbbc95094e0f216fc735705f47b94c2ddcb0962a
- `pi-version.txt` e470244f5d207065cacf7314a910c7cff5fc8334840dc42426d62da3e661f580
- `pi-rpc-events.txt` e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 (the argv no-op — empty, retained as the honest negative; same digest for the empty stderr)
- `pi-rpc-stderr.txt` e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
- `pi-rpc-time.txt` 81a45e9b159f2536672ec97f0213d0b0475409b3fca0ec2acb7e1ed40b53899d
- `pi-rpc-push-observation.txt` 3e0f1745594ead9717c3535545e1c7c327bb85a2da6add7c3a74d8ed1609ef4b

## Conclusion

**pi / cli · wake = event-driven — MEASURED at 0.84.3.** The rpc stdio channel pushes
typed events to its attached client for the life of the process — observed live, in the
exact dialect the shipped adapter consumes, with the probe answer arriving inside the
pushed stream. Cell value unchanged; stamp edit rides this commit: `classified →
measured`, receipt_path here, `measured_at 2026-08-29`, grids re-rendered from the dataset.

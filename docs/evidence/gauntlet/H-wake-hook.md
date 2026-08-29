# H-wake — hook waiter + exact-session controller (fixture half)

**Family:** wake. Capture: `docs/evidence/gauntlet/captures/H-wake-hook-run.json` (29344 bytes, sha256 `fb5232494f05aa5656a3cffe009dbbc742b862e76e3d60a915e32d0c844be3c0`).
**Trunk:** `origin/main` `17aaf1162a577889a101569fe65c48fdbdfdabc7` merged into `refs/heads/lane/grok-gauntlet`.
**Scratch:** `~/Projects/floati-grok/.gauntlet-scratch/hwake20260828015923`
**Live fleet root:** not used. No waiter was pointed at a live root. No synthetic `woke` rows.

**Family green: NO.** Daemon drills (including this Cursor seat's >35-minute 3-cycle longevity run) remain queued. Hook-only evidence is a baseline, not a close.

## Controller: pause / status / resume

Exact session `session-h-wake-a` on fixture node `lane-a`:

| step | exit | state |
|---|---:|---|
| `wake pause --session session-h-wake-a` | 0 | `paused`; display `DRAFT - paused by you at 2026-08-28T01:59:23.979Z`; `cached_session_state=unknown`; `harness_trust_gate=unknown` |
| `wake status` | 0 | same paused display |
| `wake resume` | 0 | `active`; display `DRAFT - wake monitoring is active` |

Receipts (pause, resume, pause, resume): `wake-control-01a04617dc4b714582126c1b2610972b` · `…dd687944a0816223658ab6ec` · `…de4d7b06ba29a3c82fe0e1c2` · `…df697a0e980835941916b688`.

`.githooks/pre-commit` bytes were unchanged (marker-only).

## Wildcard / global refuse without state

`wake pause --session` of `*`, `all`, `global`, and empty: each exit 20, `wake_session_invalid`.

## Pause is recorded intentional silence

Mail `msg-01a04617dcea79fd87d0e2b2bc3336ab` was present. Product waiter (`python -m floati.codex_wait --root <scratch>`) on the paused session:

- exit 0, empty stdout
- `receipts/wakes/lane-a.jsonl` absent (0 wake rows)

## Resume: next envelope wakes

Same waiter on the same session after resume:

- stdout `{"decision": "block", "reason": "[floati] 1 new message(s) for lane-a: msg-01a04617dcea79fd87d0e2b2bc3336ab"}`
- organic `wake_attempt_receipt` `wake-attempt-01a04617ddff7a9ebf7d1555a1452cf3`, `outcome=woke`, `acting_session_id=session-h-wake-a`

## Exact-session isolation

Second pause of `session-h-wake-a`, second mail `msg-01a04617de9f7ec9ac2de44c570d5b3f`:

- waiter on `session-h-wake-b` woke that mail
- waiter on paused `session-h-wake-a` stayed silent (empty stdout)

## Three Codex hook deadline cycles

Idle session, empty inbox, product waiter with the same injected monotonic clock the unit tests use (`wait_deadline_seconds=2`):

| cycle | waited_seconds | outcome | receipt |
|---:|---:|---|---|
| 1 | 2 | `rearmed` | `codex-wait-exhaustion-01a04617dbfc7845b2fea02889335ab9` |
| 2 | 2 | `rearmed` | `codex-wait-exhaustion-01a04617dc007efc9a3302a91e14bfa0` |
| 3 | 2 | `rearmed` | `codex-wait-exhaustion-01a04617dc0378b0aa4a6fce7dad142d` |

Each cycle emitted `(floati: wait deadline exhausted; end this turn to re-arm)`. This is the Codex hook re-arm path. It is **not** the Cursor 28-minute death measurement and **not** a daemon longevity run.

## Not claimed

- Live pause/resume of this Cursor chat
- Manual waiter against `~/.floati-bus/puddle-fleet`
- Wake-family GREEN
- Daemon presence (not built)

**Verdict: HOOK+CONTROLLER BASELINE (family not green)**

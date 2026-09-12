# MK-D3 recipe — the transition demonstration

The recording banked beside this recipe shows a seat's session ending
mid-slice and `floati log --replay` playing every recorded event back in
order to `REPLAY COMPLETE`. This recipe reproduces the same surface from
a clean clone: a real fleet, one worker run that ends before its work
does, and the flight recorder's reconstruction of exactly what happened.
The capture is a fixture fleet and says so; this recipe builds the same
kind of fixture with the product's own verbs.

**Limit line (verbatim, as beside the capture):**

> Replay reconstructs what was recorded, in order. It does not finish
> work that was interrupted.

## Run it

From a clean clone of the public repository at the current tag
(v0.1.2 or newer). Absolute paths throughout; there is no default root.

```bash
git clone https://github.com/Land-o-Clusters/floati.git /absolute/floati
git -C /absolute/floati checkout v0.1.2
ROOT=/absolute/mk-d3-fleet

python3 -m floati init --root "$ROOT"
python3 -m floati node add --root "$ROOT" --node architect-codex \
  --harness codex --lifetime permanent
python3 -m floati node add --root "$ROOT" --node builder-claude \
  --harness claude --lifetime permanent

# the architect's authority comes from the shipped role template
python3 -m floati node role --root "$ROOT" --node architect-codex \
  --template architect --answer repo=floati \
  --answer never_touch=foreign-project --answer owner_stops=owner-tier
python3 -m floati grant --root "$ROOT" --as architect-codex \
  --holder builder-claude --subject work-claims --epoch 1

# the row the seat will pick up and end before finishing
python3 -m floati work add --root "$ROOT" \
  --title "carry the handover slice" --owner builder-claude

# a stand-in for a harness session that ends mid-run: the worker claims
# its slice through the shipped authority checks, then the session ends
printf '#!/bin/sh\nexit 1\n' > /absolute/stub-worker
chmod +x /absolute/stub-worker
python3 -m floati worker run --root "$ROOT" --as builder-claude \
  --adapter claude --claude-executable /absolute/stub-worker

# the flight recorder: the recorded events, played back in order
python3 -m floati log --root "$ROOT" --replay --plain
```

`worker run` exits 32 (`no_result`) with artifact status `degraded` here —
the session ending mid-slice is the demonstration: the adapter returned no
result, so the typed degrade in the receipt is the result rather than a
command failure, and the replay below reconstructs it.

## What you should see

The plain replay prints the recorded events in order and stops at the
completion label:

```text
PLAIN REPLAY // v0
◆ +0000.000s  CLAIM     WORK     builder-claude work-… claim
│ +0000.002s  CLAIM     WORKER   builder-claude work-… claim
│ +0000.003s  DEGRADED  WORKER   builder-claude work-… workspace_mapping_missing
! +0000.003s  DENIED    REFUSAL  builder-claude work-… worker_workspace_missing
REPLAY COMPLETE // 4 EVENTS // 3 MS
```

Run without `--plain` in a terminal, `floati log --root "$ROOT" --replay`
plays the same events under the `FLOATI // FLIGHT RECORDER` header with a
progress bar and the buoy mark at `REPLAY COMPLETE`.

The elapsed column is the recorder's own monotonic clock over the
recorded order — the order the ledgers actually hold, never a
reconstructed guess.

## What this demo does not show

- The replay finishes nothing: the slice is still claimed after the
  replay, exactly as the ledger holds it. The next session picks it up;
  the recorder never does.
- No seat wakes on its own; no daemon runs in this recipe.
- The replay does not diagnose why the session ended — it reconstructs
  what was recorded, including the typed degradation, and stops.

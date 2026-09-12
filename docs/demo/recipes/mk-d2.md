# MK-D2 recipe — the failure demonstration

The recording banked beside this recipe shows a worker seat dying mid-run:
its board names the stalled claim and its holder, and `floati doctor` names
the undelivered count and its age. This recipe reproduces the same surfaces
from a clean clone. The capture is a fixture fleet and says so; this recipe
builds the same fixture with the product's own verbs.

**Limit line (verbatim, as beside the capture):**

> The doctor reports what is missing and how old it is. It does not say why
> the seat died.

## Run it

From a clean clone of the public repository at the current tag
(v0.1.2 or newer). Absolute paths throughout; there is no default root.

```bash
git clone https://github.com/Land-o-Clusters/floati.git /absolute/floati
git -C /absolute/floati checkout v0.1.2
ROOT=/absolute/mk-d2-fleet

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

# the row the worker will die holding
python3 -m floati work add --root "$ROOT" \
  --title "carry the stalled slice" --owner builder-claude
python3 -m floati work claim --root "$ROOT" \
  --id <work id printed by work add> --as builder-claude \
  --authority-subject work-claims --authority-epoch 1

# the worker is mid-run: it reports itself alive, briefly
python3 -m floati presence report --root "$ROOT" \
  --as builder-claude --ttl-seconds 1

# the architect hands it the next slice; the worker is already gone,
# so this envelope is never drained - this is the undelivered mail
python3 -m floati send --root "$ROOT" --from architect-codex \
  --to builder-claude --repo floati \
  --sha 0000000000000000000000000000000000000000 \
  --doc docs/demo/recipes/mk-d2.md --note "next slice"

# once the presence TTL lapses (one second), look at the two surfaces:
python3 -m floati board --root "$ROOT"
python3 -m floati doctor --root "$ROOT" --source /absolute/floati
```

`doctor` exits 35 (`degraded`) here — the findings below are the point of
the demonstration, and the exit code says the run completed with findings
rather than that the command failed.

## What you should see

- The board names the seat `EXPIRED` and prints the row as
  `CLAIMED ... holder:builder-claude` — the stalled claim and its holder.
- The doctor's delivery health names the node with
  `1 undelivered, oldest <age>, no drain on record` and a remedy line.
  The count and the age are what floati measured; nothing reports why the
  seat died, because nothing observed that.

## What this demo does not show

- No seat wakes on its own; no daemon runs in this recipe.
- The doctor does not diagnose the death — it reports absence and age.

# MK-D1 recipe — the handoff demonstration

The recording banked beside this recipe shows a handoff with its delivery
record: a Claude Code seat sends a row to a Codex seat, the Codex seat drains
and acks in one command, `floati receipts` prints the receiver's delivery and
acknowledgment history, and one malformed envelope is refused with its typed
code. This recipe reproduces the same surfaces from a clean clone. The capture
is a fixture fleet and says so; this recipe builds the same fixture with the
product's own verbs.

**Limit line (verbatim, as beside the capture):**

> The receiver acknowledged when it next drained. This recording does not
> show a seat waking on its own.

## Run it

From a clean clone of the public repository at the current tag
(v0.1.2 or newer). Absolute paths throughout; there is no default root.

```bash
git clone https://github.com/Land-o-Clusters/floati.git /absolute/floati
git -C /absolute/floati checkout v0.1.2
cd /absolute/floati
ROOT=/absolute/mk-d1-fleet

python3 -m floati init --root "$ROOT" --solo claude-sender --harness claude
python3 -m floati node add --root "$ROOT" --node codex-receiver \
  --harness codex --lifetime permanent

# the Claude Code seat hands its row to the Codex seat
python3 -m floati send --root "$ROOT" --from claude-sender \
  --to codex-receiver --repo floati \
  --sha "$(git rev-parse HEAD)" \
  --doc docs/demo/recipes/mk-d1.md --note "row one: the handoff slice"

# the Codex seat drains: one command presents the envelope, writes the
# delivery receipt, and writes the acknowledgment receipt
python3 -m floati inbox --root "$ROOT" --as codex-receiver \
  --session receiver-session

# the receiver's receipt history
python3 -m floati receipts codex-receiver --root "$ROOT"

# one malformed envelope, refused before any mutation
python3 -m floati send --root "$ROOT" --from claude-sender \
  --to codex-receiver --repo floati --sha 00000 \
  --doc docs/demo/recipes/mk-d1.md --note "row one: the handoff slice"
```

The second `send` exits 20 (`refused`) here — the refusal artifact is the
point of that step, and the exit code says the request was wrong rather than
that the system failed.

## What you should see

- The send artifact carries the `message_envelope` and names the receiver
  readiness `unknown`: this fixture has no recipient hook configuration
  provenance, so it cannot prove absence. The send journals its readiness
  **as observed at** `observed_at`, not as a promise through append. The observation
  is saved beside the envelope. The append is proven; delivery follows the drain.
- The drain artifact carries all three at once: the presented `messages`, the
  `delivery_receipt`, and the `ack_receipt` — the receiver acknowledged when
  it drained.
- `floati receipts codex-receiver` lists one delivery and one acknowledgment
  for the node, and `denials: []`.
- On versions with the send-side commit-resolution fence, the deliberate
  `00000` send refuses with `status: "refused"` and
  `evidence.code: "sha_absent"`: it names no commit in the checked repository.
  Older versions that validate the envelope first report `sha_invalid`.

## Regenerating the published capture

The capture source checkout and the demonstrated Git history can differ.
When running the generator from a development checkout, pass the public clone
above as `--handoff-repository /absolute/floati`. The generator derives the
earliest recipe commit in that clone, runs the real send from there, and
photographs that exact SHA. Its `--source-sha` identifies the generator source
separately. Keep the public clone, font, capture time and source receipt fixed
when comparing output at two development HEADs.

## What this demo does not show

- No seat wakes on its own; the receiver drained because the recipe drained
  it, and no daemon runs in this recipe.
- The send proves the append, never the delivery; the delivery receipt is
  not the acknowledgment; the acknowledgment says SEEN, nothing more.

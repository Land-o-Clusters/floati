# Roadmap

What is queued, as of 2026-09-05, in the order we intend to build it. This is a sequence, not a promise: a row lands when its receipt
exists on `main`, and this page is regenerated from the working board when the sequence changes. Defects live on the
[issue tracker](https://github.com/Land-o-Clusters/floati/issues), filed by us with the measurement that found them; this page is for
what the product does not do yet.

## Now (September 2026)

**Verbs the exits and the wizard still lack**
- `node drain --node X`: drain a fixed node's inbox without retiring it (today only instances drain).
- `role transfer-architect --to NODE`: move the single architect role, receipted, refusing any state with zero or two architects.
- `chart add-root` / `chart remove-root`: a validated writer for the declared-roots file, which is hand-edited today.
- `update rollback --to SHA`: the rollback the updater already performs internally, as a receipted verb.
- `wake status` reports the wake breaker per node: open or closed, threshold, last trip reason. Nothing resets it but a consent re-grant.
- The node-add wizard offers the read-only survey inline when it finds an undeclared bus in scope, and asks before adopting.
- `node add --plan file.json`: the same mutation as the interactive wizard, one engine, two idioms.
- `role new` / `role edit` / `role validate` / `role import` (local file only): role templates on disk with the same validator the loader uses.
- `seat board`: one idempotent verb chaining wake-claim takeover, waiter resume and drain, a receipt per step.
- `floati wait --for CONDITION`: a harness-agnostic wait, retiring the Codex-only helper script.

**Measurements we owe before a mechanism ships**
- The hook-trust preconditions: a behavioural burn record for hook execution, confinement retested per release, and proof a hook session
  cannot write its own trust config. Until all three are measured, hooks stay off by default.
- Codex arrival-path wake: a daemon wake against a running Codex seat proven a receipted no-op before any install; then arrival-grade wake.
- Cursor wake: a decision between polling at a costed cadence and an honest "unwakeable" mark. Cursor's CLI exposes no hook.
- OpenCode lease and restart semantics, characterized from a live seat (issue #16).
- `floati watch` under heavy load: the child can ignore Ctrl-C; the trace seam is in, the mechanism is not yet named.
- The Codex leg of MCP conformance (the Claude leg is done).
- An entropy pass over the exported tree before each publication, by an operator-declared scanner.

**The README and the TUI**
- Captures of the TUI's own palette: the board idle, live and degraded, the graph, a replay in flight, the install moment, the selftest.
- One source for help text, the agent manual's verb tables and `describe --json`, with a drift test.

## Later
- `node prep-clear`: the wind-down act as one verb, after the lifecycle vocabulary is ruled per harness.
- Timing receipts: a durable record of how long an operation class took, queryable by `snapshot` and `chart`. Needs its design first.
- Linux as a measured platform: the same receipts that admit a harness admit a platform, and today the Linux leg runs only in our own CI.
- The public repository becoming the product repository outright, with the operations repository behind it.

## How to read this page
An item moves off this page in one of two ways: it lands, and the receipt is linked from the changelog; or it is struck, and the
ruling says why. Nothing is marked "done" without a test or a receipt. If you want one of these sooner, open an issue and say what
you would use it for.

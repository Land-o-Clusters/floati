# Roadmap

Remaining work, reconciled with the September 2026 command surface, in the order we intend to build it. This is a sequence, not a promise: a row lands when its receipt
exists on `main`, and this page is regenerated from the working board when the sequence changes. Defects live on the
[issue tracker](https://github.com/Land-o-Clusters/floati/issues), filed by us with the measurement that found them; this page is for
what the product does not do yet.

## Now (September 2026)

**Remaining command work**

- `update rollback --to SHA`: expose the updater's rollback mechanism as an explicit receipted verb.

**Available now**

The [generated command reference](../AGENTS.md)
lists the shipped syntax for `node drain`, `role transfer-architect`, `chart add-root` /
`remove-root`, `node add --plan`, role authoring, `seat board`, `wait`, and `node prep-clear`.
The node-add survey and wake-breaker reporting are also implemented. These are no longer
queued features; consult each command's help for its prerequisites and limits.
`chart timings` queries the timing receipts already collected by instrumented verbs.

**Measurements we owe before a mechanism ships**

- The hook-trust preconditions: a behavioural burn record for hook execution, confinement retested per release, and proof a hook session
  cannot write its own trust config. Until all three are measured, hooks stay off by default.
- OpenCode lease and restart semantics, characterized from a live seat (issue #16).
- `floati watch` under heavy load: the child can ignore Ctrl-C; the trace seam is in, the mechanism is not yet named.
- Complete the Codex MCP conformance leg: [five operational rows are recorded](evidence/conformance/M4-codex-mcp-leg-2026-09-05.md), but doctor health remains unmeasured.
- An entropy pass over the exported tree before each publication, by an operator-declared scanner.

**Measurements and documentation already banked**

- Codex and Cursor CLI wake have measured receipts in the [capability matrix](capability-matrix.md).
- The README includes the TUI palette captures; [their manifest](demo/tui/manifest.json) identifies the source runs.
- Help text and the agent command table are generated; `python3 -m floati.command_codegen --check` detects drift.

## Later

- Linux as a measured platform: the same receipts that admit a harness admit a platform, and today the Linux leg runs only in our own CI.
- The public repository becoming the product repository outright, with the operations repository behind it.

## How to read this page
An item moves off this page in one of two ways: it lands, and the receipt is linked from the changelog; or it is struck, and the
ruling says why. Nothing is marked "done" without a test or a receipt. If you want one of these sooner, open an issue and say what
you would use it for.

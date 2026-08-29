# C0 — managed-send surface, measured (Fable, 2026-08-29)

**Claim this receipt states:** the managed-send gateway (the harness-side wrapper that lets a
seat send governed bus envelopes without holding raw bus credentials) exists for **codex/cli
only** at measurement time. Every other harness/surface pair's `managed-send` cell is an honest
absence: no wrapper is installed or shipped for it.

**Measurement (2026-08-29, operator machine):** the codex wrapper is present and executable at
`~/.codex/bin/codex-fleet-bus` (34,097 bytes). A per-harness sweep of the corresponding harness
homes (`~/.claude`, `~/.cursor`, `~/.cline`, `~/.pi`, `~/.grok`, `~/.t3`, `~/.opencode`) finds
no fleet-bus wrapper for any of them. The codex wrapper's live send contract was proven during
the B5 gate (transport note in `docs/evidence/gate-wsb-b5-2026-08-27.md`: two shape refusals,
then a delivered transport-test envelope).

**What this receipt cannot see:** other machines' installs, and any wrapper added after the
measurement date. Vendoring the wrapper into this repository is a filed row (VD-1); when it
lands, this receipt is superseded by the vendored artifact's own tests.

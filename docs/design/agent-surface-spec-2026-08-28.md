# WS-I SPEC — THE AGENT SURFACE: humans and agents as first-class operators (the architect, 2026-08-28)

**Owner mandate 2026-08-28:** many users will point an agent at this repo and say "install this
and run it for me." That must be a seamless end-to-end experience — install AND day-to-day
management — and advertised as such. **The design authority is dogfood: an agent (the architect) operated
floati all night; every friction it hit is a row here, every success is the pitch.**

## What already works (keep, and advertise)

Typed exit codes on every verb · refusal-first with named reasons · idempotency keys ·
`--json` twins on status/graph · **receipts for everything — an agent operator never guesses
whether its own action worked.** These are the moat restated for a new audience.

## Rows (I1–I6)

- **I1 — public `AGENTS.md`** *(the architect authors; the current root AGENTS.md is fleet-internal and
  is superseded/relocated at reconcile)*: copy-pasteable install sequence · full verb contract
  incl. every managed-wrapper shape · exit-code table WITH remedies · standard workflows (solo →
  fleet → doctor → chart) · fences an agent must respect (no home scan, consent receipts, never
  another bus's artifacts) · the relaunch-after-install quirk. No sentence an agent can misread.
- **I2 — `floati describe --json`**: the whole CLI self-describes — verbs, flags, bounded
  grammars, exit codes, schema versions — one machine-readable contract, schema-versioned itself.
- **I3 — non-interactive twins**: every interactive flow gains a declarative form
  (`node add --plan file.json`, `--dry-run --json` previews everywhere). One engine, two idioms.
- **I4 — remedy-typed refusals**: every refusal carries a machine-readable `remedy` field in
  JSON output. An agent self-corrects without a human relay (the 2026-08-27 send-shape stall is
  the binding precedent).
- **I5 — one generated source** for help text · AGENTS.md verb tables · describe output. A
  contract written twice with one copy unowned WILL drift; generation + a drift test.
- **I6 — floati-mcp (next ring, post-core)**: thin LOCAL-STDIO MCP server exposing the verbs as
  tools. Zero network, zero credentials, same consent posture. Chartered here, built after I1–I5.

## Bounds

Floati still makes zero model calls — agent-friendly means OPERABLE BY agents, never containing
one. · The WS-D architect role template doubles as the "run my fleet" agent-operator manual —
one document, both audiences. · README advertises the surface only as rows land (receipts law).
· WS-H gains an agent-operator gauntlet drill: a scripted agent must install, onboard a node,
send/ack, diagnose a deaf node, and uninstall using ONLY AGENTS.md + describe — zero human help.
That drill IS the acceptance test for this whole workstream.

# HM-2S Confluence and operator hardening evidence

Status date: 2026-08-01.

## Identity and authority

- Checkout: `~/Projects/<retired>`.
- Branch: `lane/hm0`.
- Starting local and remote tip:
  `b08ccf20ced67fccdc783df3057c298c6eb8dbef`.
- Boot inbox poll: exit 31, `intentional_silence`, zero messages, and no
  receipt.
- The dispatch authorizes the HM-2S implementation. the architect still gates every
  push and must name the exact committed tip.

## Phase results

### A — ACP adapter live probe

Claude Code ACP, Codex ACP, and the generic `acp-agent` responder were absent.
The installed ordinary Claude and Codex commands were not substituted for ACP.
No provider turn or approval request occurred. The real-turn branch is
**SKIPPED — RESPONDER ABSENT**, not passed. The bounded fixture codec and
absence path passed four focused tests. Full probe evidence is in
`docs/evidence/HM2S-ACP.md`.

### B — Managed Mode data plane

Added strict v0 session-adoption and session-release contracts, canonical
fixtures, record validation, and a dark ledger implementation. Adoption binds
the exact active registry manager plus the active authority lease subject,
holder, epoch, and expiry. Release binds the exact adoption. No UI, automatic
adoption, or Puddle activation was added.

### C — Harbor Chart graph contract

Added `<retired> graph --root ROOT --json` and a strict typed topology contract.
Nodes, workers, work dependency edges, and bridge stubs are deterministic,
sorted projections of allowlisted durable ledgers. Wall clocks, mtimes, PIDs,
filesystem discovery, and process inspection are excluded.

### D — Doctor

Added `<retired> doctor --root ROOT --source SOURCE [--ref REF]`. It checks direct
root identity, registry/liveness agreement, exact manifest membership and
digests, source currency and cleanliness, invoked symlink identities, the sole
work coordinate, and alternate consumption coordinates. Findings are typed and
the artifact return classes are 0 healthy, 20 configuration refusal, 33
integrity failure, and 35 degraded. Remediation appears only when source
currency is known current. The implementation does not create a root lock or
mutate the inspected root.

### E — Cross-bus bridge v0

Added local-filesystem-only contracts and a dark implementation for two
distinct SlipRoots. Both roots require independent, active consent; both store
the active bridge record; and every forward produces paired receipts stamped
`advisory_not_consumption`. Missing, revoked, mismatched, malformed, same-root,
or remote-transport attempts fail closed and write denial evidence to both
roots. Fixture tests exercise round trips in both directions. No remote
transport or consumption mutation exists.

### F — TUI wall punch list

Regenerated the complete wall after closing the demonstrated structure and
semantics items: calm idle collapse, title-before-shortened-ID worker rows,
alert-ladder ordering, duplicate outcome suppression, plain-dump header
separation, and typed replay rail glyphs. `docs/evidence/wall/PUNCH-LIST.md`
keeps the orange-on-cream light-palette judgment open for the the architect polish
drive. It is not claimed as closed.

## Test-first and correction record

Each B-F surface began with a focused failing test before implementation. The
first complete post-implementation self-test then found one regression in the
existing deadline/degradation CLI test: receipt duplicate suppression removed
the only `TURN FAILED` label because the primary worker label map fell back to
`UNKNOWN`. The failure was reproduced in isolation. The minimal correction
made the fallback use the canonical typed outcome string, after which both the
existing test and the new duplicate-suppression test passed. That initial
305-test run was therefore a **FAIL**, not a pass.

## Final local gates

All commands ran from the named checkout on `lane/hm0`:

```text
PYTHONPYCACHEPREFIX=<temp>/hm2s-pycache python3 -m <retired>.selftest
  Ran 305 tests in 21.767s — OK
  {"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}

PYTHONPYCACHEPREFIX=<temp>/hm2s-pycache python3 -m <retired>.conformance --live-root-smoke
  {"cases":5,"status":"conformant"}

PYTHONPYCACHEPREFIX=<temp>/hm2s-pycache python3 -m unittest tests.test_copy_ledger tests.test_manifest
  Ran 10 tests in 0.090s — OK

generated-tree scrub
  scrub_hits=0

git diff --check
  exit 0, no output
```

These are local, fixture, and live-root-smoke results. They do not establish a
live ACP worker turn, hosted CI, Puddle activation, deployment, release, or a
the architect verdict.

## Push boundary

This evidence is prepared for the committed checkpoint. The checkpoint must
not be pushed until the architect issues `PUSH GO lane/hm0 <exact-tip>` for that exact
commit. After any verdict-bearing change, the complete gate must be rerun and
local/origin equality verified.

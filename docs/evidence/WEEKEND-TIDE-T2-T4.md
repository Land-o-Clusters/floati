# DRAFT — Weekend Tide Tables T2–T4 integration evidence

Date: 2026-08-28

Branch: `integrate/tide-tables-20260828`

Exact baseline: `origin/main@f2b587634cfc6d6a52cc24bd02bfd978919c359b`

Authority: `docs/design/tide-tables-spec-2026-08-28.md`, including its T1
gated-pass and depth-2 amendments.

## Scope and fences

This row ships optional, off-by-default per-node Tide policy records; bounded
DERIVED and SELF_REPORTED readings; threshold crossing, rearm, and restart
recovery receipts; DRAFT recommend and direct envelopes; D5 state-flush
completion; same-cycle wake-daemon dispatch hold; context/admin CLI surfaces;
the node-add optional Tide step; JSON status projection; and DRAFT Harbor Board
turnover flags.

The shipped evaluator is restricted to the Codex and Cursor harnesses for
which this tree has an exact-session wake-daemon binding and bounded reader.
T1 catalog entries without a shipped evaluator refuse with
`tide_evaluator_unavailable`; Cline surfaces not authorized by T1 refuse with
`tide_metric_not_derivable`. No policy can silently select a dead evaluator.

This row performs no flip, release, publication, activation, manual waiter
run, or foreign-bus operation. It adds no README edit and no fenced
foreign-project artifact. New operator-visible copy is DRAFT-stamped.

## RED-first receipts

- Policy tests preceded the catalog and ledger implementation and pinned
  off-by-default state, harness-bound metrics, typed refusals, canonical
  thresholds, append-only predecessors, idempotency, and class-B testimony.
- Evaluator tests preceded threshold/action implementation and pinned zero
  reads without policy, one action per crossing epoch, rearm below threshold,
  DRAFT recommend/direct envelopes, direct hold, D5 completion, and status
  projection.
- Reader tests preceded exact-session readers and pinned Codex nested usage,
  Cursor transcript proxies, testimony provenance, and refusal of a matching
  artifact reached through a symlinked ancestor.
- Daemon tests preceded integration and pinned one evaluation per due consented
  cycle, refusal backoff, an existing direct hold, and a new direct crossing
  suppressing dispatch in that same cycle.
- Review regressions pinned interrupted-crossing recovery after pressure fell,
  mutation refusal during a directed turnover, policy replacement after clear,
  unsupported evaluator refusal without writes, canonical tiny-value refusal,
  the optional plain wizard step, and board flags on both sides of D5.

## Automated evidence before the final manifest

- Tide/daemon/wizard/board bank: **66 tests, OK**, 0.271 seconds.
- Context/admin/CLI/supervisor/schema/board neighbor bank: **160 tests, OK**,
  22.687 seconds. Existing argparse refusal diagnostics appeared on stderr;
  the authoritative unittest verdict was `OK`.
- Internal-rename/source-scrub/name-sweep/copy-ledger bank: **30 tests, OK**,
  1.799 seconds.
- Canonical pre-manifest discovery executed **1,930 tests** in 199.876 seconds.
  Its **22 failures were exclusively manifest tracked-set and digest assertions**
  against the intentionally stale manifest. No product, schema, CLI, daemon,
  reader, policy, or projection test failed. Existing sandbox-refusal
  diagnostics and `ResourceWarning` lines appeared on stderr and are not
  reported as passed gates.

## Independent review closure

The review identified one critical same-cycle hold defect and important
restart, policy-lifecycle, catalog-authority, evaluator-availability, board,
and path-boundary gaps. Each applicable finding received a failing regression
test before repair. The final implementation also refuses policy mutation
while a direct turnover awaits D5 and binds D5 completion to the original
policy receipt.

## Final frozen-tree verification

The deployable manifest was updated after every source, schema, and copy
mutation. Its exact verifier returned `[]`, and its dedicated bank ran **25
tests, OK** in 0.075 seconds. Canonical discovery on that frozen deployable
tree ran **1,930 tests, OK** in 196.126 seconds with process exit 0. Existing
argparse refusal diagnostics, sandbox-refusal diagnostics, and
`ResourceWarning` lines appeared on stderr; the authoritative unittest verdict
was `OK`.

The exact pushed SHA and delivery-envelope receipt are bound in the architect
envelope. This document does not claim a Fable verdict, flip, release, or
publication.

## FABLE GATE: PASS AND MERGED (2026-08-28)

Re-derived at the delivered tip: **1,930 tests, OK, exit 0
(pipestatus-captured), frozen tree**; manifest exact. The merge onto main
carries a docs-only delta from that verified tip (verified by path scan),
fences green at the landing. Mechanism spot-checks beyond the suite: the
catalog IS the tide table typed — every metric carries access class, DERIVED
stamp, formula, and receipt path verbatim from T1/depth-2 · un-derivable
metrics refuse with citation · and the row's own best law: derivable does
not mean evaluable — `policy_metric_for` refuses a policy on any harness
without a shipped daemon evaluator, citation in the refusal, closing the
sibling of the matrix's derivability/capability conflation before I could
plant it twice. The owner's threshold feature ships: policies on codex and
cursor coordinates today, widening exactly as evaluators and receipts land.

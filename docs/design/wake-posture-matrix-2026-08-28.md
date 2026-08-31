# WAKE POSTURE MATRIX — the wake family's opening row (the architect, 2026-08-28)

**Owner order (2026-08-28):** the daemon v1 covers Codex + Cursor, which is
fine — but the gauntlet must establish what EVERY claimed harness actually
needs for wake, per measurement, not assumption. Nobody has measured the wake
anatomy of pi, cline, herdr, grok-build, or t3; opencode's hook holds
seemingly indefinitely; claude may be event-driven and need nothing. The
matrix answers this before any harness gets (or is denied) wake machinery.

**Seat: grok, NOW** — this row does not wait on the daemon buildout. It is
E1's sibling: measure what each harness exposes, and let the product's claim
follow the measurement.

## The four questions, per harness (codex · claude · opencode · cursor ·
cline · grok-build · pi · herdr · t3)

1. **Is there a resident session at all?** A per-invocation CLI with no
   resident process has nothing to wake — the verdict is a typed
   `not_applicable`, cited, and that harness gets NO wake machinery ever
   (an absence is a valid cell, not a failure).
2. **What wake surface exists?** Stop/lifecycle hook, plugin, event
   subscription, file watch the harness itself documents — cited from the
   harness's own docs AND confirmed by a live probe (the E1 discipline: a
   doc claim without a live confirmation is not a cell).
3. **What is the hook's lifetime?** Where a hook exists: does it carry a
   deadline window that must re-arm (codex), hold indefinitely (opencode's
   observed posture — MEASURE it, ≥3 cycles), or die (cursor ~28m, the type
   specimen)? Same instrument across harnesses, ≥3 deadline cycles where
   feasible.
4. **Verdict per harness, one of exactly four:** `hook_sufficient` ·
   `needs_daemon` (hook exists but cannot be load-bearing) · `event_driven`
   (the harness pushes; nothing to poll) · `not_applicable` (no resident
   session). Every verdict cites its receipt.

## What the matrix governs

- **Daemon adapters 3..N:** built only for harnesses the matrix rules
  `needs_daemon`. Codex + Cursor stay v1 regardless (already ruled);
  everything else remains typed-absent until its cell says otherwise.
- **The gauntlet wake family:** per-harness drills follow the cell — a
  `not_applicable` harness's wake drill is the PROOF of non-applicability
  (no resident process outlives the invocation), not a skipped row.
- **Copy:** the README/help wake claims may only name postures the matrix
  measured. "Wake for every harness" is not a sentence this product says;
  "wake where a session exists to wake, measured per harness" is.

## Bounds

Fixtures and scratch roots only; no live-root waiters; no synthetic outcomes
(the H-family discipline). Quirks discovered land in the WS-H QUIRKS ledger.
The matrix is a PHOTOGRAPH of current harness versions — each cell records
the version it measured, and a version bump invalidates the cell, not the
method.

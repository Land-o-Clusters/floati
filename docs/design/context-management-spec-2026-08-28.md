# WS-E SPEC — CONTEXT MANAGEMENT, HONESTLY (the architect, 2026-08-28)

Build contract for North Star V5 — the last genuinely-new capability. The governing law is the
honesty boundary: **harnesses differ wildly in what they expose about their context windows, and
floati never invents a number it cannot measure.** If completion cannot be measured, no fraction
is drawn; a proxy is stamped ESTIMATE and the stamp travels with the number everywhere it renders.

## E1 — the context capability inventory (measurement first)

Per harness, MEASURED with receipts (grok-shaped row, gauntlet-adjacent): what does this harness
actually expose — token counts? remaining-context? transcript size on disk? nothing? The adapter
contract gains an optional `context_report` capability; absence is a typed NOT-EXPOSED, never a
zero. This inventory bounds every later claim, exactly as C0 bounded `surface_verified`.

## E2 — the turnover ritual (the practice, productized)

`floati node turnover <id>` composes, from WS-D machinery, the pair we already perform by hand:
the wind-down command (state flush to `STATE.md` → bank-and-envelope unbanked work → DRAINED
receipt) and the successor's boot command, projected fresh with the state file named as the port
vehicle. **The state file IS the context port** — deliberately model-agnostic, which is what
makes "porting between models" honest: compose turnover with a B6 provider switch and the
successor boots on a different model with the same ported state. Nothing more is claimed;
no transcript translation, ever.

## E3 — pressure, stamped

Where E1 found real numbers: context-pressure receipts stamped MEASURED. Where it found nothing:
proxies only (turn count, session age, readable transcript bytes), stamped ESTIMATE, stamp
rendered wherever the number is. The Harbor Board may carry a pressure lamp ONLY for nodes with
a MEASURED source; ESTIMATE proxies render as text, never as a gauge (a gauge is a fraction; a
fraction claims a measured whole).

**E1 VERDICT APPLIED (grok, gated 2026-08-28): remaining-context is NOT-EXPOSED by all eight
live CLIs.** Measured scraps only: opencode historical token stats (not remaining) · grok's
disk footprint · claude autocompact is a setting, not a readout. **Therefore E3 ships NO GAUGES
in v0 — there is no measured whole anywhere in the corpus.** Pressure is proxy-text stamped
ESTIMATE (turns, session age, transcript bytes), E2's turnover ritual is the main event, and E4
thresholds are proxy-based. A gauge returns only if a harness starts exposing remaining-context,
with a receipt.

## E4 — the wind-down posture (mirror of the wake posture)

User-set thresholds per node or role ("offer turnover at 80% MEASURED" / "at N turns ESTIMATE").
At threshold, floati NOTIFIES and OFFERS the turnover pair — **it never kills a session, ever**:
nothing dies without your recorded say-so, exactly as nothing wakes without it. Acting on the
offer is one command; declining is silence.

## Out of scope, by ruling

Mid-session context editing/compaction inside a harness (harness-internal; floati does not reach
in) · transcript translation between models · any "efficient context windows" claim without a
measurement behind it · auto-turnover (offer-only in v0; revisit only with the daemon consent
machinery and an explicit per-node opt-in).

## Build order

E1 inventory (measurement seat) → E2 turnover (needs WS-D D3) → E3 receipts+board (needs E1) →
E4 thresholds (needs E2+E3). First RED: a gauge rendered from an ESTIMATE source must be
impossible to construct. WS-H gains a per-harness context drill once E1 lands.

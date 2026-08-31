# BRIEF — R4 Interactive Approval Panels (NIGHT_HARBOR Part II R4; seat: build lane)

**the architect, 2026-08-28 ~19:40Z. Charter: NIGHT_HARBOR §R4 + Part II addendum. Doctrine:
`tui-research-triage-2026-08-28.md` T-1/T-5/T-6/T-8. Study quarry: grok-build for the working
surface (mechanisms in, expression ours — 1:1 capability, 0:1 expression). Territory:
`floati/tui*`, render modules, demo. Dark until gated; copy DRAFT-stamped for my restamp;
captures re-banked; machine twins byte-identical; keyboard-complete on every tier.**

## What it is

Consent and effect approvals as framed panels: the EXACT record under decision rendered inside the
panel, approve/refuse as an explicit keypress on a focused control. R5.1 by construction — the
panel IS the human act.

## Rulings (not open questions)

- **A-1 THE INITIAL FOCUS IS ALWAYS THE LEAST GRANT.** Refuse-once (or the narrowest allow when
  refuse is not an option) is preselected, every time. No sticky-last-decision preselect, no
  configured-default that can reach a broader row. The studied product can preselect its global
  always-approve row in fallback; that is the anti-pattern this brief exists to invert (T-6, SL-1).
- **A-2 ASYMMETRIC COST BY DESIGN: REFUSING IS ONE KEY, GRANTING IS TWO.** `Esc` = refuse-once,
  immediately, from anywhere in the panel (SL-1: it must always be faster to not-allow). Digits
  and arrows MOVE FOCUS ONLY — a digit never resolves. `Enter` commits the focused option. The
  studied product's digits-resolve-immediately is explicitly rejected.
- **A-3 MOUSE: FIRST CLICK FOCUSES; A SECOND CLICK ON THE SAME OPTION INSIDE A NAMED WINDOW
  COMMITS** (constant, tested, ~300 ms class). **A broad grant (any beyond-this-request scope)
  NEVER commits by mouse — keyboard Enter only.** Stronger than the quarry; deliberate.
- **A-4 THE RECEIPT BINDS WHAT WAS SHOWN.** Every decision appends an approval receipt carrying
  the option's stable id, the acting session, and the DIGEST OF THE RENDERED RECORD the panel
  displayed. What you saw is what you approved — provable later. A refusal receipts the same way
  (typed, with the option id `refuse-once`). This is the row's honesty-brand original; no studied
  product has it.
- **A-5 SCOPE RIDES STABLE OPTION IDS, NEVER PROTOCOL KINDS** (quarry law, kept verbatim as
  mechanism): `refuse-once` · `allow-once` · session-scoped and broader ids as the consent model
  defines them. Two options sharing a kind never share semantics.
- **A-6 QUEUE, NEVER REPLACE.** Later concurrent requests wait FIFO behind the panel under review;
  arrival of a new request never changes the focused option or re-renders the record under
  decision. On queue empty→nonempty the composer text is stashed and restored after (quarry
  mechanism, kept).
- **A-7 NO NON-HUMAN PATH.** No environment variable, flag, config row, or test seam resolves a
  panel in a shipping build (R5.1). The test pins it: every env sweep over the approval path
  refuses.
- **A-8 EVERY TIER IS COMPLETE.** Plain/monochrome/`NO_COLOR` render the full panel with glyph
  twins; focus is unmistakable without color; no mouse requirement anywhere (T-1, existing tier
  law). Ducky never speaks; panel copy obeys first-clause honesty and ships `DRAFT -`.

## RED-first bank (commit the reds before production bytes)

1. Preselect pin: fresh panel → focused option is `refuse-once`; a prior broad grant does NOT
   move the preselect on the next panel.
2. Asymmetry: `Esc` resolves refuse-once from any focus; digit alone never resolves; `Enter`
   resolves only the focused option.
3. Mouse: single click never commits; same-target double click commits narrow options; the broad
   option refuses mouse-commit with a typed note.
4. Receipt: decision receipt's record digest equals the digest of the rendered panel body; a
   mutated record between render and commit refuses (stale-panel law).
5. FIFO: second request arriving mid-panel neither replaces nor refocuses; drains in order.
6. A-7 sweep: no env/flag path resolves.
7. Tier: monochrome + NO_COLOR captures re-banked, byte-stable, focus legible.

Gate = this bank green + captures (color/monochrome pair) + machine twins byte-identical + my
copy restamp of the panel strings before any activation row.

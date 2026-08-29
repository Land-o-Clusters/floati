# RULING — TUI research triage: four briefs, adopted doctrine, one pre-flip row

**Fable, 2026-08-28 ~16:35Z. Inputs: the four owner-run deep-research briefs in
`docs/research/regatta/` (three were already banked byte-identical; the fourth —
`terminal-capability-matrix-2026.md` — closes the README's MISSING slot). Owner's framing stands:
the TUI is the front door, and this research outranks the release clock where it finds defects.
Verdict up front: it found ONE small pre-flip defect and a large, good post-release program —
nothing that invalidates what ships today.**

All four briefs are LEADS, NOT AUTHORITY (the research README's C1 discipline holds): their
citations are research-engine artifacts, and no claim becomes a committed constant in floati until
verified against the pinned source or measured on a live terminal. The matrix brief grades its own
confidence per cell — Terminal.app rows explicitly unverified — treat every cell as
ESTIMATE/DERIVED until the Regatta spike MEASURES it.

## What today's release posture looks like against this research

Floati v0's TUI is plain-first, 256-color indexed with a monochrome tier, glyph twins for every
color meaning, no mouse dependency, deterministic non-TTY output, `TERM=dumb` respected. That is
precisely the degradation-friendly baseline all four briefs converge on as the floor. The research
does not indict the front door; it defines the ladder above it.

## Adopted doctrine — binding on Regatta R3+ and all future interactive TUI work

- **T-1 KEYBOARD-PRIMARY; POINTER IS ENHANCEMENT; SELECTION IS NOT FOCUS.** Cline's big-button
  onboarding is keyboard-first with *no* click handler on its main cards (source-pinned brief, its
  strongest finding — and a correction to the doc-only brief's guess). Selection is a small
  application datum; terminal focus is reserved for text entry.
- **T-2 IDLE DISCIPLINE: NO PERPETUAL FRAME LOOP.** grok-build's model is the one to copy:
  tick demand None/Slow/Fast derived from the model; dirty-flag presenter with single-frame
  backpressure and coalescing; cell-level diff; cursor-command suppression so an idle TUI emits
  ZERO bytes and the user's cursor blink survives. An idle fleet monitor that spins a CPU core is
  a lie about being idle — this is our brand rendered as scheduling.
- **T-3 SYNCHRONIZED OUTPUT (DEC 2026) WRAPS THE WHOLE PRESENTATION** — cells, graphics, cursor —
  emitted optimistically (harmless where unsupported), per grok-build's proven pattern.
- **T-4 A TERMINAL CAPABILITY IS A MEASUREMENT, AND OUR STAMPS APPLY.** DECRQM `?2026/?1006/?1016`
  probes, kitty `a=q`/`?u` queries behind a DA1 barrier, XTGETTCAP `RGB` = **MEASURED**.
  TERM/terminfo/COLORTERM heuristics = **DERIVED**. Brand env vars = **ESTIMATE**. DA2 identifies,
  never negotiates. Inside tmux, tmux IS the endpoint — never bypass its answer to flatter the
  outer terminal. Nobody else's TUI stamps its capability decisions; ours will.
- **T-5 `NO_COLOR` IS POLICY, NOT CAPABILITY.** The capability model stays true
  (`rgb_capable=true, user.no_color=true`); the rendering policy suppresses color. Never poison
  the capability model to implement a preference.
- **T-6 THE DEFAULT ROW IS NEVER THE BROADEST GRANT.** grok-build's permission cursor can, in
  fallback, PRESELECT the global always-approve row — Enter then grants everything in one
  keystroke. That is the anti-pattern; SL-1 binds us the other way: the preselected option
  degrades toward AllowOnce/refuse, always. Its mouse guard IS worth copying: pointer activation
  of any grant requires a same-target second click inside a short window; a first click only
  highlights. And its option-identity law holds: scope semantics ride exact option IDs, never a
  shared protocol kind.
- **T-7 WIZARDS COMMIT ONCE.** Cline persists provider/model/reasoning on intermediate
  transitions; Back does not roll back — the source-pinned brief proves "done" is a signal, not a
  transaction. We deliberately invert: draft state → single receipted commit boundary. (Our
  existing consent flows already obey this; it is now written down for any future onboarding.)
- **T-8 THE EXPRESSION FENCE RESTATED FOR THIS RESEARCH:** mechanisms 1:1, expression 0:1. We
  reimplement diff discipline, hit grids, probe barriers; we never copy visual styling, layout,
  or copy. Both studied products are permissively licensed; the fence is ours regardless.

## The one pre-flip row (small — seat: sol, after its R2 repair)

**NC-1: honor `NO_COLOR`.** Measured today: zero references in `floati/` — a 2026 CLI convention
gap on the front door, and the cheapest kind: the monochrome tier with glyph twins already exists,
so the fix is routing, not rendering. Contract per the convention + T-5: `NO_COLOR` set and
non-empty → color suppressed by default across TUI/replay/demo color paths (monochrome tier),
capability model untouched, explicit user flags may override. RED-first: with `NO_COLOR=1` the
rendered surface contains zero SGR bytes; unset → byte-identical current output. Not
launch-blocking by itself; it IS ship-today-sized, and the front door deserves it.

## The post-release program (Regatta R3+, sized honestly)

The capability layer (T-4 probes + graded matrix + stamped capability artifact) and the
presentation discipline (T-2/T-3) are each real rows — together they are the "cinema quality"
ladder NIGHT_HARBOR already points at, now with mechanisms named and prior art pinned. Graphics
protocols stay OUT until the capability layer exists: the matrix's clearest lesson is that images
are the least portable feature in the stack, and brand-policy allowlists (grok-build's approach)
age badly without receipts.

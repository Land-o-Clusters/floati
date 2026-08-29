# Regatta study-row research (deep-research imports, 2026-08-28)

Three owner-run deep-research reports supporting NIGHT_HARBOR Part II's
study row. **LEADS, NOT AUTHORITY** (the copy-pack C1 discipline): every
mechanism claim here is verified against the actual pinned source before it
enters a design note or a line of floati code; citations inside are the
engines' own artifacts, kept verbatim.

- `cline-tui-mechanisms-dr-a.md` / `-dr-b.md` — two independent engines on
  cline's onboarding TUI. Headline: cline's big-button feel is a React
  reconciler over OpenTUI's native (Zig) renderer — framebuffer diffing,
  layout, mouse hit-testing all live in OpenTUI 0.4.3, pinned and traced in
  report B. Floati stays stdlib: we reimplement the MECHANISMS (diff-only
  writes, box hit-targets, focus model), never the stack.
- `grok-build-tui-mechanisms-dr.md` — grok-build at a pinned commit: the
  pager/render/input crate split, and the modified inline-Ratatui terminal
  that reports whether a flush actually changed cells — the
  redraw-only-on-change law implemented at the flush layer.

- `terminal-capability-matrix-2026.md` — the formerly-missing protocol
  matrix, imported 2026-08-28 ~16:30Z: per-terminal support for kitty
  graphics / SIXEL / OSC 1337 / sync-2026 / SGR+1016 mouse / kitty keyboard /
  truecolor, WITH detection recipes (DECRQM probes, the DA1 barrier,
  XTGETTCAP, tmux mediation, NO_COLOR-is-policy). The brief grades its own
  confidence per cell (†/‡ — Terminal.app cells explicitly unverified),
  which is exactly our stamp discipline arriving from outside: a matrix
  cell is ESTIMATE/DERIVED until our spike MEASURES it on a live terminal.
  The spike still self-measures — now against a graded map instead of blind.

Triage + adopted doctrine: `docs/design/tui-research-triage-2026-08-28.md`.

# DUAL-SURFACE LAW — CLI and desktop surfaces gate separately (Fable, 2026-08-28; owner order)

**Owner order:** final testing gates must cover BOTH the CLI and the
GUI/desktop offering of every harness that ships both — t3, codex, and
claude at minimum, plus any other dual-surface harness the sweep finds.

**The defect this closes:** every conformance cell, posture-matrix cell, and
wake drill to date was earned on a CLI surface. A cell earned on the CLI
certifies NOTHING about the same harness's desktop app — different process
model, different session identity, possibly different hook/config loading —
and a matrix that doesn't say which surface earned the cell overclaims
silently. A SUPPORT CLAIM IS PER SURFACE, NOT PER BRAND.

## The rules

1. **Inventory gains a surface axis** (grok, C0-DELTA-class row, now): per
   harness, photograph which offerings are INSTALLED here — cli · desktop
   app · IDE extension — with versions per surface. Known already: t3 has
   `/Applications/T3 Code (Nightly).app` (C9 inventory) beside its CLI;
   cursor is an editor beside `cursor-agent`; claude and codex desktop
   presence to be measured, not assumed.
2. **Cells become per-surface** wherever both offerings are installed:
   conformance, posture matrix, and wake drills each get a desktop-surface
   sibling cell for dual-surface harnesses. A desktop cell answers its own
   four posture questions (the desktop app may share the CLI's hooks, load
   them differently, or have none — measured, never inherited from the CLI
   cell).
3. **The README matrix names the surface** each cell was earned on. A
   dual-surface harness shows two cells or one cell plus a typed
   not-yet-measured; a single "live" spanning both surfaces is banned.
4. **The chaos campaign** must include at least one desktop-surface node if
   any claimed harness has a measured desktop cell — the operator drives at
   least one node whose session lives in the GUI app.
5. **The market-roster research pass** (harness-market-roster doc) records
   surfaces per candidate from day one.

## Sequencing

The surface sweep (rule 1) runs now — grok, does not wait on the daemon.
Per-surface conformance/posture cells follow the sweep. The flip gate
inherits this: gauntlet-green means green PER CLAIMED SURFACE.

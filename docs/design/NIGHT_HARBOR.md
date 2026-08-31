# NIGHT HARBOR — the floati TUI eye-candy direction (the architect, 2026-08-28; owner-ordered)

The owner asked for a super sexy pass, not a re-cut of what exists. This doc
is the direction; the build row applies it across every TUI surface. The
preserved `codex/tui-excellence` branch is an idea quarry — harvest
intentions, never merge its drifted code.

**The aesthetic in one line: signal lights on dark water.** A harbor at
night is mostly darkness with a few lights that each MEAN something — that
is also floati's honesty brand drawn as pixels. Everything below serves it.

## 1. Ink layers (color is information, luminance is decoration)

Dark-terminal-first. Four ink layers, strictly tiered:

- **Structure** (frames, rules, dividers): dim gray `236–240`. Never brighter
  than content.
- **Body** (names, values): `250–252`. Node ids and holders slightly brighter
  than their labels.
- **Dim facts** (timestamps, paths, counts at rest): `243–245`.
- **SIGNAL — reserved for meaning, never decoration:**
  - green `35/42` — lit lamp, healthy, complete
  - amber `214` — attention: stale, waning, waiting
  - red `160/196` — violation, refusal, dead
  - cyan `45` — activity in motion: driving, replaying, arriving mail
  - brand orange `208` (≈ #E8622C) — the floati identity mark ONLY (the
    buoy, the header slash pair). Today 208 is doing warning duty; it moves
    to identity and amber takes the warnings.

**Law: a colored glyph must answer a question.** If removing its color loses
no information, it should not have been colored.

## 2. The lamp language (three lamps stay three questions)

`●` lit · `◐` waning · `○` dark — colored per §1, one lamp per question
(LIVE / AUTH / MUTEX), never blended. A lamp row that is all green IS the
eye candy: "harbor at dawn," every light steady. No composite health dot,
ever.

## 3. Structure and rhythm

- Panels get rounded frames `╭─╮ │ ╰─╯` in structure ink; the header banner
  carries `⊙ FLOATI // <SURFACE>` with the mini-buoy in brand orange.
- One breath line between panels; section titles in caps, structure-ink
  rule to the right margin: `WORKERS ────────`.
- Columns align on a grid; counts right-aligned; a table never wraps — it
  truncates with `…` and the detail view carries the rest.
- **The waterline:** one subtle row of `~ ≈ ~` in dim cyan directly under
  the header — the identity texture, STATIC at rest.

## 4. Motion (event-sourced, never ambient)

The README's sentence — "nothing here is animated by the demo; it is played
back from the fleet's own records" — is the motion law generalized:

- **Zero idle redraws stands.** No timer loops, no spinners, no breathing.
- Motion happens only ON A STATE CHANGE, as a bounded pulse (≤3 frames,
  ≤150 ms, then still): new mail sends one wave traveling the waterline;
  a lamp changing state crossfades dim→lit via one intermediate glyph; a
  completed work item flashes its row to green once, then settles.
- Replay is the one continuously-moving surface, and its motion is the
  DATA: a tick timeline `├──●──●───●──┤` scrubbing at the chosen speed.
  Playback speed changes the waiting, never the order (existing law).
- `--capture`, CI, non-TTY: pulses disabled, final frames only.

## 5. Per-surface treatments

- **Harbor Board:** the flagship. Framed panels, lamp rows per §2, DAG bar
  in `▰▱` colored by health — a fraction appears ONLY where completion is
  measurable (existing law). Degraded workers lead their panel, each RED
  carrying its receipt id in dim ink — the receipt is part of the candy.
- **Harbor Chart (multi-bus):** the ASCII map earns real cartography —
  buses as piers, nodes as moored vessels `▤`, the architect seat flagged
  `⚑`, last-activity as lamp brightness. A chart, not a table.
- **Doctor:** triage order, worst first. Every red names its receipt and
  its remedy verb on the same line. A fully green doctor prints the calm
  sentence, not an empty table.
- **Wizard (`node add`):** the exact-records preview is the hero — framed,
  syntax-tinted JSON with the identity fields bright. What-you-commit-is-
  what-you-saw, dressed.
- **Replay:** flight-recorder framing — timeline on top, event stream
  below, faults as red ticks you can see approaching.

## 6. Degradation tiers (every meaning survives the fall)

256-color → 16-color (signal hues map to the basic 8+bright) → monochrome
(every color meaning has a glyph twin — lamps already carry shape; add `!`
warning and `x` violation prefixes) → `--plain`/POSIX pipes keep today's
stable output VERBATIM (machine consumers have no reader; the reader law
does not reach them). `NO_COLOR` respected. Everything readable at 80 cols.

## 7. Fences for the build row

- CPU at idle: unchanged from today (zero redraws) — measured before/after.
- Byte-identical `--plain` and `--json` twins across the whole pass — the
  dressing may not touch a machine surface (RED-first: pin them first).
- The copy ledger gains no unreviewed strings: new visible words ship
  `DRAFT -` stamped for restamp.
- Monochrome captures re-banked beside color for every changed surface.
- No fraction, gauge, or percent appears anywhere completion is not
  measurable — the dressing inherits the honesty laws, it does not soften
  them.

Build seat: the floati builder post-daemon, or a dedicated owner-booted Codex build seat in
parallel (territory is disjoint from the daemon: `floati/tui*`, render
modules, demo). The re-cut row and this direction are ONE row — the old
branch's ideas pass through this doc or not at all.

---

# PART II — THE REGATTA (the ambition layer; owner-ordered 2026-08-28)

Part I is discipline. This is the part people screenshot. The bar is
grok-build — a full-screen, mouse-interactive Rust TUI with inline images
and file-level approval panels, open source under Apache 2.0
(`xai-org/grok-build`) — which makes it a STUDY QUARRY, not just a bar:
read its mechanisms (rendering loop, mouse routing, panel system), write
our own expression. The GPL-fence discipline applies even though the
license permits more: mechanisms in, look-and-feel ours.

**The enabling fact (researched 2026-08-28): every innovation below is
escape sequences.** Kitty graphics protocol (adopted by kitty, WezTerm,
Ghostty, Konsole), mouse tracking, kitty keyboard protocol, and
synchronized-output mode 2026 (flicker-free frames) all speak plain
escapes — floati stays dependency-free at full ambition. Graphics degrade
through Part I §6's tiers; a non-kitty terminal gets the braille/Unicode
rendition of the same information, never a hole.

## R1. THE LIVE HARBOR MAP (the flagship screenshot)

A full-screen interactive chart of the whole estate: buses as piers, nodes
as moored vessels, channels between them. **When an envelope lands, you SEE
it** — a pulse travels the channel from sender to recipient (event-sourced,
one bounded animation per event, Part I §4 law). Lamp brightness = last
activity. Mouse: click a vessel → its detail panel (role, inbox, receipts);
click a pier → the bus's ledger summary. Keyboard twins for every mouse
action. This is `floati chart --live` and it is the poster.

## R2. FLIGHT-RECORDER CINEMA

Replay gains the map: watch the night's messages flow across the harbor at
chosen speed, faults flashing red on the vessel that threw them, the
timeline scrubber synced below. Speed changes the waiting, never the order.
The hero GIF records itself.

## R3. GRAPHICS TIER (kitty-class terminals)

The buoy mark rendered as an actual image in the board header; braille
sparklines (`⣀⣠⣤⣶⣿`) for per-node activity everywhere, upgraded to
pixel-resolution charts where the protocol exists. No third-party logos —
our brand only. Detection by terminal response, never user-agent guessing;
absence of support is silent fallback, not a warning.

## R4. INTERACTIVE APPROVAL PANELS

grok-build's most-praised mechanism, floati-shaped: consent and effect
approvals as framed panels with the exact record shown, approve/refuse as
an explicit keypress on a focused control. R5.1 holds by construction —
the panel IS the human act, and nothing auto-focuses the approve key.

## R5. MODERNIZATION FLOOR

Mouse tracking + kitty keyboard protocol + synchronized-output frames
across every full-screen surface; 60fps is not the goal — ZERO TEAR at
event boundaries is. Idle CPU stays zero (Part I §7 fence, re-measured).

## The build shape

1. **SPIKE first (one day, scratch branch):** prove sync-mode frames,
   mouse routing, and one kitty-graphics image in the existing board,
   stdlib-only, all three degradation tiers. The spike's capture pair
   (color + monochrome) is its gate.
2. **Study row:** mechanism notes from `xai-org/grok-build` source —
   rendering loop, panel/focus model, approval flow — filed as a research
   doc with citations before R1 is built.
3. Then R1 → R2 → R4 → R3, each behind the Part I fences (machine twins
   byte-identical, copy DRAFT-stamped, captures re-banked).

Seat: a dedicated owner-booted Codex build seat (territory: `floati/tui*`, render
modules, demo — fully disjoint from the daemon build), or the floati builder
post-daemon. The spike is the boot row either way.

## PART II ADDENDUM (owner, 2026-08-28): CLINE JOINS THE STUDY QUARRY — for the doors

Owner field report: cline's TUI onboarding is the reference experience —
big buttons, responsive, easy. Ruled in: **cline (open source) is the
second study quarry, scoped to the DOORS** — onboarding, first-run, and
setup flows — where grok-build remains the quarry for the working surfaces
(panels, mouse routing, rendering loop). Same fence: mechanisms in,
expression ours.

What "big buttons" means translated to floati's laws: `init --solo`
first-run and the `node add` wizard get LARGE FOCUSABLE CHOICE PANELS —
one decision per screen, the whole option is the click/hit target (not a
one-line prompt), current focus unmistakable at a glance, instant visual
response to every keypress or click. The wizard's exact-records preview
(Part I §5) stays the commit step — cline's ease never dilutes
what-you-commit-is-what-you-saw. The bus setup path (declare root →
first node → first envelope) gets the same door treatment: a newcomer
should reach a working solo fleet without reading anything but the
screens. Study row deliverable: mechanism notes on how cline builds
focus/hit-targets and instant response in a terminal, filed with citations
before the wizard dressing is built.

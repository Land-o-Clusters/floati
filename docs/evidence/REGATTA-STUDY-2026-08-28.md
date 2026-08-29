# DRAFT — REGATTA study quarry (sol seat, 2026-08-28)

**Program:** `docs/design/NIGHT_HARBOR.md`, Part II study row and cline addendum.

**Branch:** `lane/sol-regatta`.

**Fence:** mechanisms studied; layout, copy, symbols, palette, and interaction
expression remain Floati's. No source was copied into the repository.

## Source identity

- grok-build: `xai-org/grok-build` at
  `9684fa3cdbf2995e30ea8b9b637f1db008f144fc`.
- cline: `cline/cline` at
  `ce71fe5eb9807edabbdc64010c22f7cc08cc9201`.

The source links below are commit-pinned primary sources, not screenshots or
third-party descriptions.

## grok-build: working-surface mechanisms

### Rendering loop

grok-build separates event/state work from presentation. Its `Presenter`
coalesces dirty requests, refuses to start another frame while terminal output
is in flight, and records a deferred draw when cadence says "not yet". The view
returns `Changed` or `Unchanged`, so no-op input does not redraw, and tick demand
has explicit none/slow/fast tiers rather than a permanent high-rate timer.
([presenter](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/app/event_loop.rs#L513-L611),
[input outcomes](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/app/app_view.rs#L460-L486),
[tick demand](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/app/event_loop.rs#L3540-L3558))

**Floati expression:** one immutable model snapshot enters one render pass;
events mutate controller state first; only a changed model, focus, viewport, or
bounded animation requests a synchronized frame. R1 envelope pulses and R2
replay motion own finite deadlines. With no deadline and no new ledger fact,
the loop blocks instead of manufacturing frames.

### Panel and focus model

grok-build routes a blocking card before ordinary pane input. Permission state
has an explicit focus mode plus an active option index, and only the FIFO front
request is rendered and interactive. Mouse input hit-tests geometry from the
rendered surface, changes the same active index/focus used by keyboard input,
and returns the same action type when activation occurs.
([permission state](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/views/permission_view.rs#L38-L61),
[FIFO/front-only panel](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/views/permission_view.rs#L193-L220),
[blocking-card routing](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/app/agent_view/input.rs#L823-L884))

**Floati expression:** focus is a small enum, never inferred from color. The
renderer emits bounded hit regions for the exact visible frame. Keyboard and
mouse resolve those regions to one shared semantic action (`select vessel`,
`open vessel`, `select pier`, `open pier`); clipped or stale geometry is inert.
Focus chrome remains legible in monochrome.

### Approval flow

grok-build routes each permission to its owning session, cancels an orphan,
queues concurrent requests instead of replacing them, and redraws only when
the owning view is visible. Enqueue stashes ordinary prompt state on the
empty-to-nonempty transition, establishes panel focus, and creates a structured
record before display. Selection pops only the FIFO front, sends the chosen
option ID and scoped metadata, then performs an explicit queue transition.
([request routing and queueing](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/app/acp_handler/permissions.rs#L20-L95),
[panel construction](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/app/acp_handler/permissions.rs#L170-L226),
[selection dispatch](https://github.com/xai-org/grok-build/blob/9684fa3cdbf2995e30ea8b9b637f1db008f144fc/crates/codegen/xai-grok-pager/src/app/dispatch/permissions.rs#L116-L214))

**Floati expression:** later approval panels will render the exact effect or
consent record and bind activation to that record's immutable identity. Queue
order is ledger order. Reject remains a first-class focused choice. We do not
adopt cursor memory for approvals: Floati's law forbids auto-focusing an
approve action, and the human act must remain distinguishable from arrival.

## cline: door mechanisms

### First-run routing and one decision per screen

Cline chooses onboarding as the root view when provider configuration is
absent, and the onboarding controller owns an explicit step union. The view
renders one screen component for the current step; transition functions update
the step before beginning asynchronous device/auth work, so the visible state
responds immediately rather than waiting on the operation.
([first-run route](https://github.com/cline/cline/blob/ce71fe5eb9807edabbdc64010c22f7cc08cc9201/apps/cli/src/tui/root.tsx#L103-L110),
[step model](https://github.com/cline/cline/blob/ce71fe5eb9807edabbdc64010c22f7cc08cc9201/apps/cli/src/tui/views/onboarding/model.ts#L10-L22),
[screen routing](https://github.com/cline/cline/blob/ce71fe5eb9807edabbdc64010c22f7cc08cc9201/apps/cli/src/tui/views/onboarding/view.tsx#L28-L191),
[transition before async work](https://github.com/cline/cline/blob/ce71fe5eb9807edabbdc64010c22f7cc08cc9201/apps/cli/src/tui/views/onboarding/controller.ts#L405-L451))

**Floati expression:** `init --solo`, bus setup, and `node add` dressing will be
finite state machines with one choice or exact-record preview per screen.
Starting work first enters a named busy/error/success state; completion cannot
silently skip the preview or confirmation state.

### Focus, hit targets, and instant response

Cline's welcome menu makes each option a multi-line bordered card with a
border, text, and arrow all derived from one selected index. Its reusable
searchable/model rows attach mouse activation to the containing row, not only
the label, and use that same selected state for the entire row's background and
foreground. Keyboard handlers update the selected state directly and use the
same selected item for Enter.
([large focus cards](https://github.com/cline/cline/blob/ce71fe5eb9807edabbdc64010c22f7cc08cc9201/apps/cli/src/tui/views/onboarding/screens.tsx#L854-L935),
[whole-row hit target](https://github.com/cline/cline/blob/ce71fe5eb9807edabbdc64010c22f7cc08cc9201/apps/cli/src/tui/components/searchable-list.tsx#L247-L335),
[keyboard selection](https://github.com/cline/cline/blob/ce71fe5eb9807edabbdc64010c22f7cc08cc9201/apps/cli/src/tui/views/onboarding/keyboard.ts#L189-L227))

The current commit's welcome cards themselves do not attach a mouse-down
handler; the whole-row hit-target mechanism is demonstrated by the reusable
lists and model rows. Floati therefore combines the two mechanisms rather than
claiming the source's main menu already does so.

**Floati expression:** every door option owns its full visible rectangle.
Hover/click/arrow changes the one focus index and requests a frame immediately;
Enter/click resolves through the same action. Selected state changes border,
marker, and text treatment so monochrome retains an unmistakable focus signal.
The exact-record preview remains a distinct final step; easy targeting never
widens consent.

### First paint and teardown

Cline configures mouse movement without global auto-focus, paints the selected
background before the first root frame, and makes unmount/destroy idempotent.
([renderer lifecycle](https://github.com/cline/cline/blob/ce71fe5eb9807edabbdc64010c22f7cc08cc9201/apps/cli/src/tui/index.tsx#L11-L99))

**Floati expression:** terminal capability results are explicit inputs to the
first frame; no terminal-name guessing. Setup and cleanup remain one guarded
lifecycle, with every enabled protocol having a reverse action and all text
tiers carrying the same information.

## Build constraints carried forward

- No borrowed look-and-feel, copy, mascot, or layout constants.
- No third-party runtime dependency; stdlib protocol and rendering seams only.
- `--plain` and `--json` machine twins stay byte-identical.
- Visible copy is DRAFT-stamped until the owning voice lane rules it.
- Hit regions are render products, clipped to the viewport, and never durable
  authority.
- Animation is event-sourced and bounded; idle has no animation clock.
- Approval ease cannot weaken exact-record preview, explicit activation, FIFO
  identity, or refusal parity.

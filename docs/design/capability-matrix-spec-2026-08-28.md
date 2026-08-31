# CAPABILITY MATRIX SPEC — the README grid, generated (the architect, 2026-08-28; owner-ordered)

**Owner order:** the README matrix must show capabilities across all
providers — bus support, wake class, auto-turnover — so a reader knows at a
glance what each harness supports. This replaces the staged single-status
column in README-NEXT.

## The grid (reader-facing shape)

One clean table, harness × capability, dual-surface harnesses split per the
dual-surface law (a cell never spans CLI and desktop):

| column | vocabulary | cell authority |
|---|---|---|
| bus | `live` / `CLI` / `—` | conformance receipts C1–C12 |
| wake | `daemon` / `event-driven` / `n/a` | posture matrix + surface addendum |
| auto-turnover | `auto` (class-A derived) / `assisted` (self-report) / `—` (refused) | tide table T1 + amendments |
| compaction | `native` / `—` | T1 compact-verb cells |

Every non-empty cell links the receipt that earned it; an empty cell is a
typed honest dash, never a blank. No cell says more than its receipt (the
existing matrix law, widened to every column).

## Generation, not authorship (I5 law)

A hand-written 12×4×2 grid WILL drift. The grid is GENERATED from
`docs/capability-matrix.v0.json` — one record per harness × surface ×
capability carrying {value, receipt_path, measured_at, versions} — by a
stdlib script, with a drift test asserting the README section byte-equals
generator output (the copy-ledger mechanism reapplied). The dataset cites;
the generator renders; the test pins. Changing a capability means changing
the dataset WITH its new receipt, and the README follows mechanically.

## Sequencing

Gates the README swap (the swap ships the grid). Build is small: dataset
seeded from C-rows + posture matrix + T1 (all on main), generator +
drift test. Seat: first lane with queue room before the swap nears —
or the architect builds it as storefront instrumentation if seats are saturated
(fence/checker precedent). Cell data updates ride each future gate that
changes a capability.

## AMENDMENT (owner, 2026-08-28): full roster, full features, and the Claude re-measurement

1. **Every measured harness × surface is a row** — the full roster (codex,
   claude, opencode, cursor, cline, grok, pi, herdr, t3, devin,
   antigravity) with desktop/extension siblings as their own rows per the
   dual-surface law. No taste-table ships; the generator renders whatever
   the dataset holds, which is everything measured.
2. **Columns widen to the real feature families that VARY per surface:**
   bus verbs · work orchestration (a worker adapter exists) · wake class ·
   auto-turnover class · compaction · role/boot projections (managed verb
   shapes) · managed-send profile support. Features that are UNIVERSAL
   (append-only ledger, replay, doctor, receipts, typed refusals) are
   stated once in a sentence above the grid — an all-identical column is
   noise wearing information's clothes.
3. **The Claude wake cells are RE-OPENED for re-measurement.** Owner
   testimony: Claude behaves event-driven, at least on desktop — and the
   fleet's own record backs it: alice-class Claude seats have auto-woken
   organically for weeks (the one seat family that always did). The
   `needs_daemon` cell was docs-derived with lifetime explicitly
   unmeasured. The re-measurement: a >=3-cycle organic hold on a live
   Claude seat, same instrument as the opencode cycle proof; the fleet's
   own historical wake receipts are admissible evidence. If the hook path
   holds, Claude is the first hook_sufficient/event_driven cell and LEAVES
   the daemon-adapter queue. A CELL DERIVED FROM DOCS WITH AN UNMEASURED
   LIFETIME IS A HYPOTHESIS WEARING A VERDICT'S CLOTHES.

## CORRECTION + PREFERENCE ORDER (owner, 2026-08-28)

My amendment above cited "alice-class Claude seats" as the wake evidence —
WRONG PROVENANCE, owner-corrected: build lane is not Claude. The admissible
historical evidence is the incumbent bus's ENGINEER seats (sre, ghops, and
kin): Claude-hosted, event-driven-woken, weeks of organic operation on the
incumbent bus (read-only evidence, never written). The re-measurement row
stands with this corrected citation.

**And the owner's stated design north star, now binding on the wake
column:** the verdict preference order is `event_driven` >
`hook_sufficient` > `needs_daemon`. Event-driven is the ideal — the
harness pushes, nothing polls, nothing blocks; the daemon is the FALLBACK
for surfaces whose hooks cannot hold, never the default posture. Adapter
investment follows this order.

## T1 DEPTH-2 GATED PASS (same sitting): capture digest matched, merged.
Codex's C FALLS — `payload.info.last_token_usage.*` + `model_context_window`
nest at depth 3+ makes codex class-A rich (same stamp as Claude; T2 must
scan until the nest appears or the file ends). Cursor agent-transcripts C
STANDS at full depth (517/517 rows, zero usage paths — a percent policy
from disk stays refused). Grok disk C stands; its proxies and class-B
family unchanged.

## CLAUDE RE-MEASUREMENT GATED PASS AS A REFUSAL (2026-08-28)

`H-claude-wake-remeasure` @ `9813d715`, digest matched, merged. The flip to
event_driven is REFUSED for lack of a receipt: the incumbent wake journal
(4,860 lines) holds ZERO Claude-harness and ZERO engineer-node wake rows;
the engineer ledgers show eight weeks of DELIVERIES, and delivery is not
wake; claude-code identity sessions there are architect boots only. The
owner's observed behavior is real and its indicated mechanism is
SESSIONSTART DRAIN AT BOOT — per-session seats drain waiting mail on
startup, which reads as event-driven from the operator's chair without any
resident wake existing. Cell stands `needs_daemon` for RESIDENT wake;
boot-time drain is a separate, already-held capability. STANDING OPEN
OFFER: one live >=3-cycle hold on a real Claude seat flips this cell the
day someone runs it — the instrument is specified; the seat does not
currently exist on this machine. TESTIMONY OPENS A MEASUREMENT; ONLY A
RECEIPT FLIPS A CELL.

## CORRECTION AT FIRST SEEDING (the architect, 2026-08-28): DERIVABILITY IS NOT CAPABILITY

The spec's auto-turnover column named "tide table T1" as its authority —
MY conflation, caught at grok's first seed: T1 proves a metric is
DERIVABLE; the auto-turnover CAPABILITY ships only when tide T2–T4 land.
A cell citing a derivability receipt while wearing a capability's value
would tell readers floati auto-turns-over their lane today. Corrected
contract: `auto_turnover.value` is `—` for every surface until the shipped
feature exists; the five derivable surfaces carry their tide receipt in a
separate `derivable` field ("A"), so the ship-day flip is one value change
whose receipts are already seated. Same guard on compaction: the column
means THE HARNESS'S OWN native compact verb, and the rendered header must
say so. A CELL'S RECEIPT MUST PROVE THE CELL'S CLAIM, NOT ITS
PRECONDITION.

## DATASET GATED PASS (the architect, 2026-08-28, third round)

`docs/capability-matrix.v0.json` @ `e4c65667` merged: 133 cells, zero
doc-class citations (verified by scan — COPY-LEDGER and WEEKEND_PROGRAM
absent from the amendment columns), work cells derived from adapter modules
on main citing their C-row receipts, boot honest at one photographed yes,
managed_send citing the live transport test. The seeding took three rounds
and each round's defect was a receipts-law lesson now written in this doc.
Remaining before the swap: the drift test (the architect wires), value flips ride
future gates (tide ship-day, wake family close), and the renderer's grid
lands in README-NEXT only at swap time.

## WAKE FAMILY GREEN (the architect gate, 2026-08-28 morning)

`H-wake-family-close` @ `f729a62a`, capture digest matched, merged. The
family closes on: daemon half (acceptance, three witnessed wakes) · hook +
controller baseline · per-surface cells proven or TYPED-REFUSED (a closed,
honest cell — never inherited; the campaign's GUI-session node is the ruled
live witness for the refused desktop cells) · event-driven push paths
proven on opencode/t3/pi's own surfaces · herdr not_applicable earned as a
drill. Matrix wake cells flip per this evidence. ONE GAUNTLET FAMILY
REMAINS BEFORE THE CAMPAIGN: context, unblocked the moment activation
lands.

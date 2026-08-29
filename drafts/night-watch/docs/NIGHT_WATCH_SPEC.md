# THE NIGHT WATCH — supervisor state-machine SPEC (DRAFT, NOT WIRED)

Typed-contract spec for the Night Watch engine: budget ceilings, wake-loop
detection, pause-at-quota/resume-at-reset, and the morning report — the
charter's overnight guardian for a heterogeneous agent fleet.

Discipline: identical to the HM supervisor track (spec → RED-first build →
WIRING.md → frozen-tree receipts). This doc is the ruling blueprint; the
build implements it verbatim. Drafted under the post-launch build program
(docs/design/post-launch-build-program-2026-08-22.md; laws L1–L5).

Sources: [CHARTER] HARBOR_MASTER.md §scope expansion (THE NIGHT WATCH) +
wake-economics rulings (idle-burn = named defect class; event-driven wakes;
coalescing; idle-soak acceptance; quota-aware wake admission); [BOUNDARY]
the verbatim product boundary (Floati admits/schedules/fences/suspends/
cancels/reconciles/verifies/proves — never converts model confidence into
truth).

## 1. Model

The Watch is a PURE FOLD over an ordered night-log of typed events, with an
INJECTED clock and an INJECTED budget table (no-second-budget-table law:
no default table, no fallback numbers; every ceiling carries its
sourceCitation and renders beside any violation). It owns no processes, no
sockets, no timers; it CLASSIFIES and DIRECTS — pause/resume are emitted as
directives that the operator/wake layer executes.

Night window: `[window_start, window_end]` instants injected per report.

## 2. Event vocabulary (input; closed)

`wake_requested(node, idempotency_key)` · `wake_delivered(node)` ·
`mail_landed(node, count)` · `work_completed(node, items)` ·
`pause_directive(node, reason)` · `resume_directive(node, reason)` ·
`quota_ceiling_hit(node, dimension)` · `reset_observed(node)` ·
`loop_edge(from_node, to_node)`

Unknown event kinds refuse (`unknown_event_kind`) — drift is visible.

## 3. Budget ceilings (injected table; every row cited)

Per node, per night window: `max_wakes`, `max_idle_burn_wakes`,
`max_mail_without_wake_minutes` (deafness analog), `coalesce_window_seconds`.
Exceeding a ceiling emits a typed `BudgetViolation(dimension, observed,
ceiling, citation, ratio)` and — for wake admission — a PAUSE directive at
quota (pause-at-quota), cleared only by `reset_observed` (resume-at-reset).
No silent clamps; no implicit resumes.

## 4. Wake economics laws encoded

- **Event-driven only:** a wake is justified iff mail landed since the
  node's previous wake (or an explicit operator directive). A
  `wake_requested` with zero intervening `mail_landed`/operator cause is
  **idle-burn** — counted per node; reaching `max_idle_burn_wakes` emits a
  violation + PAUSE directive (idle-burn = named defect class).
- **Coalescing:** mails landing inside one coalesce window may produce ONE
  wake; N wakes for mails shareable into one window emit a
  `coalesce_missed` finding (counted, not refused — delivery truth stays).
- **Idle-soak acceptance:** a node with no mail all night producing ZERO
  wakes is HEALTHY silence (stated in the morning report, not an alarm).
- **Quota-aware admission:** `wake_requested` while paused ⇒ refused typed
  `node_paused(reason)`; the refusal is itself recorded (never silent).

## 5. Loop detection

`loop_edge` events form a directed graph over nodes. After each edge, the
Watch tests whether the edge closes a CYCLE within the window (DFS over
window edges) or pushes any chain depth past `max_chain_depth`. Either ⇒
typed `LoopFinding(kind: cycle|depth, chain: [nodes])` naming the exact
chain — never a bare "loop detected". Loops do not block recording; they
are findings + PAUSE directives on every member node (a loop burns fleet
budget by construction).

## 6. Morning report (pure fold output)

Exactly one report per window, deterministic given the log:

```
MorningReport {
  window: {start, end}
  per_node: [{node, wakes, idle_burns, mails, work_items,
              pauses[], resumes[], violations[]}]
  loops: [LoopFinding]
  healthy_silence_nodes: [node]     # zero mail, zero wake — stated, not hidden
  violations: [BudgetViolation]     # fleet-wide, citation carried
}
```

Every user-facing string in the RENDERER (not this core) is a placeholder
`[[key]]`; the core emits data only (standing copy law).

## 7. Typed refusals / findings (closed sets)

Refusals: `unknown_event_kind` · `idempotency_conflict` (replayed wake key
with different payload) · `node_paused(reason)` · `window_inverted`.
Findings: `BudgetViolation` (4 dimensions) · `LoopFinding(cycle|depth)` ·
`coalesce_missed` · `idle_burn_threshold`. Unknown conditions fail closed;
new kinds enter by ruling.

## 8. Refusals carried by the build

The engine's closed sets (§7) are enforced in code and tested; no other
refusal may be emitted. Directives are never refusals — they are records.

## 9. Lease/process non-authority

The Watch holds NO leases and spawns NO processes (supervisor and witness
contracts own those). Its directives are ADVISORY to the wake layer and
BINDING only as recorded decisions in the log — evidence, not force
([BOUNDARY]: prove, never coerce).

## 9. Fixture scenarios (RED-first build list)

1. quiet night → healthy-silence report, zero violations.
2. mail→wake→work happy path per node.
3. wake without intervening mail ⇒ idle-burn count.
4. idle-burn threshold reached ⇒ violation + pause directive.
5. wake while paused ⇒ `node_paused` refusal, recorded.
6. reset_observed after pause ⇒ resume directive emitted once.
7. two mails, one wake inside coalesce window ⇒ clean.
8. two mails, two wakes shareable ⇒ `coalesce_missed`.
9. loop_edge A→B then B→A ⇒ cycle LoopFinding naming [A,B]; both paused.
10. chain depth past bound ⇒ depth LoopFinding.
11. wakes over `max_wakes` ⇒ violation + pause-at-quota.
12. replay of the same log ⇒ byte-identical report (pure fold).
13. unknown event kind ⇒ `unknown_event_kind` refusal.
14. inverted window ⇒ `window_inverted` refusal.
15. budget table missing citation ⇒ construction refuses
    (`budget_citation_required`).
16. morning report renderer emits `[[keys]]` placeholders only.

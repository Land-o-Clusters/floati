DRAFT — NOT WIRED. No merge presumption. Enters via its post-publication gate.

# night-watch

The Night Watch engine — budgets, wake-loop detection,
pause-at-quota/resume-at-reset directives, and the morning report — as a
pure fold over a typed night-log. Spec:
`docs/NIGHT_WATCH_SPEC.md` (read it first).

Laws baked in:

- **No-second-budget-table**: ceilings are injected with a mandatory
  `source_citation`; construction refuses without one
  (`budget_citation_required`). No defaults, no fallback numbers.
- **Wake economics**: event-driven only (a wake with no intervening mail is
  idle-burn); coalescing enforced as typed `coalesce_missed` findings;
  idle-soak (zero mail, zero wakes) is healthy silence, stated honestly.
- **Loops are named**: cycles and depth violations report the exact node
  chain and pause every member.
- **Directives are advisory records**: pause/resume are evidence in the
  log; this package owns no processes, sockets, or leases.

## Run the tests

```sh
cd drafts/night-watch
python3 -m unittest discover -s tests -t . -v
```

Stdlib only; Python >= 3.9. 17 tests (refusal-path weight ≥ happy-path).

## Layout

- `night_watch/events.py` — closed event vocabulary (unknown kinds refuse).
- `night_watch/budget.py` — injected ceiling table + violation record.
- `night_watch/watch.py` — the fold engine, loop finder, directives.
- `night_watch/report.py` / `render.py` — morning report + [[placeholder]]
  renderer (copy is the architect's; placeholders only).
- `docs/NIGHT_WATCH_SPEC.md` — the spec this implements.
- `WIRING.md` — seam, surface, gate, blast radius, receipts, estimate.

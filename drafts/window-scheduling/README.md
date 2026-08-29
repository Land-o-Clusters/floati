DRAFT — NOT WIRED. No merge presumption. Enters via its post-publication gate.

# window-scheduling (F5, dark)

Bounded scheduling against provider windows the system already observes.
**The fence is the feature:** respects provider windows; NEVER account
rotation or limit evasion — terms-respect constitutional.

Laws baked in (brief: window-scheduling-brief-2026-08-22):

- **Windows are MEASURED, never modelled.** Every boundary states how it
  was known (`stated_by_provider` | `observed_in_record`); a boundary with
  no stated source cannot be constructed — no extrapolated window enters
  the scheduler, however plausible.
- **An unknown window schedules NOTHING** — typed `window_unknown`, naming
  its cause. Absence is not permission.
- **An expired window invents nothing** — typed `window_expired`; no later
  window is guessed.
- **Composes with THE NIGHT WATCH** (paused nodes schedule nothing); does
  not build a second pause engine. No wake oracle — this row schedules,
  it does not wake.
- **Every schedule stamps its basis**: which window and how it was known.
- AST-level fence: no rotation/evasion identifier may exist on the
  scheduler surface.

## Run the tests

```sh
cd drafts/window-scheduling && ./run_tests.sh -v
```

The runner sets `PYTHONDONTWRITEBYTECODE=1` (frozen-runtime law: frozen
tree = frozen runtime). Stdlib only; Python >= 3.9.

## Layout

- `window_scheduling/scheduler.py` — Window (construction-refused without
  stated sources), Scheduler (typed refusals: `window_unknown`,
  `boundary_not_stated`, `window_expired`, `node_paused`), Schedule
  (basis-stamped).
- `tests/` — 8 tests; the two named REDs lead.
- `WIRING.md` — L2 contract (seam by file+symbol, ACTIVATION, per-product
  release-binary rows + readout line, receipts, ESTIMATE).

## Status

Post-launch dark under the build program (L1–L4). RED-first proven in the
commit chain; receipts re-runnable from a clean tree at the gate SHA.

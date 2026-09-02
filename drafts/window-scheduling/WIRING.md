# WIRING — window-scheduling (F5, dark)

Program law L2 contract. Template @wiring-contract-template-2026-08-22,
Amendments 1–3 applied; per-file release-binary rows name WHICH product
(Amendment 2); §4 closes with the readout line (Amendment 3).

## 1. The seam

Product call sites, by file and symbol (floati tree):

| # | product file | symbol / call site | what it calls |
|---|---|---|---|
| 1 | `floati/cli.py` | new verb `floati schedule --provider P --as NODE --do ACTION` — handler beside `_doctor`/`_wait` | `Scheduler.schedule(node=…, action=…, provider=…, now=…)`, fed `Window`s from integrator-supplied measured records |
| 2 | `<wake-layer>` (H1 `floati wait`) | consumes emitted `Schedule.run_at` as its deadline input — composition, not a second engine | reads the returned `Schedule` |

DISTRIBUTION REACH (Amendment 3): standalone SwiftPM-style draft directory
`drafts/window-scheduling/` under the floati repo — **dev-only today; no
user disk path exists**. At wiring it is VENDORED into the floati CLI
tree exactly like the night-watch package decision, which is what
activates §4's Floati rows.

## 2. The surface

| element | CopyKey | absent-state |
|---|---|---|
| scheduled-action confirmation | `[[sched.confirmed]]` (names provider + basis) | n/a — a refusal never renders as a confirmation |
| deferral notice | `[[sched.deferred_to_open]]` | row not rendered when action runs now |
| refusal lines | typed cause rendered through the existing doctor-style refusal renderer | refusals always render their cause |

Placeholder keys only; no authored copy exists in this package.

## 3. ACTIVATION (NOT the test gate)

- **Activation:** an operator explicitly runs the schedule verb for a
  named provider with a measured window on record.
- **Default:** OFF — nothing schedules unless invoked.
- **What "off" renders:** ABSENT. No timers exist in the product; the
  engine cannot run uninvoked.
- **Consent:** none needed beyond running the verb — zero network added,
  zero telemetry, windows come from records the operator already holds.

## 4. Blast radius

| file | release-binary? | why it changes |
|---|---|---|
| `floati/cli.py` | **yes (Floati)** | schedule verb + handler (beside `_doctor`) |
| `floati/helptext.py` | **yes (Floati)** | help lines for the verb |
| `floati/window_scheduling/**` (vendored at wiring) | **yes (Floati)** | engine moves into the deployable set |
| `bundle-manifest.v0.json` | n/a — regenerated mechanically | tracks the above |
| `drafts/window-scheduling/**` | no | stays out of the product graph (L1 fence test) |
| Puddle.app sources | no | Puddle-enrichment is optional and absent-tolerant (charter) |

Release-binary rows: 3 (**Floati**), 7 files — rides an owner slot;
0 (**Puddle**) — the Puddle dial reads free.

THE LOCKS territory: untouched (parked with no charter; do not build
toward it). A lock never fails open is inherited if this ever approaches
that territory.

## 5. Receipts to re-run at wiring time (L4)

```
cd drafts/window-scheduling && ./run_tests.sh -v   # expected: 12 tests, OK, exit 0 (PYTHONDONTWRITEBYTECODE=1; cache hygiene named by the runner)
git status --porcelain                              # expected: empty both sides of the run
grep -rn "^import \|^from " window_scheduling/      # expected: stdlib only
```

L4 dependency list: none outside the Python stdlib — re-gate scope is THIS
directory plus the two floati files named in §1. Gate SHA for L4:
recorded in the gate verdict (this draft gates at its own SHA).

## 6. Estimate

ESTIMATE: 2–4 hours wiring (verb + handler + vendoring + manifest regen +
receipts), excluding copy authoring (architect-owned) and any H1 wait-layer
composition decisions.

## 7. Refusals carried by the build (closed set)

`window_unknown` · `boundary_not_stated` · `window_expired` ·
`node_paused` · `window_incoherent` · `timestamp_unreadable`. Unknown
conditions fail closed; new causes enter by ruling
(per the pf-harness refusal law: a closed set stays comparable across
runs).

# WIRING.md — night-watch (DRAFT, NOT WIRED)

Program law L2 contract. Status: READY TO WIRE — seam named, receipts
listed, estimate stamped.

## 1. The seam

- **Primary:** the `slip` CLI/TUI (HM operator surfaces) invoke
  `night_watch.watch.NightWatch(window_start, window_end, budget)` and fold
  each wake/delivery/mail/work event from the fleet's own delivery
  receipts + mail log at night close: exact call site is the morning-close
  verb (`slip watch --night-report`) added beside the existing doctor verb
  in `floati/cli.py` (the `_doctor` handler pattern is the template).
- **Secondary (read-only):** Puddle's Harbor page consumes the rendered
  morning report via the same JSON shape `floati doctor` already emits —
  no new transport.
- The engine itself imports nothing from product code (verified by grep in
  this draft's gate receipt); wiring adds ONE import site in product code,
  never the reverse.

## 2. The surface

Harbor board section + morning digest line. Every user-facing string binds
a placeholder key emitted by `render_morning_report`:
`[[morning.header]] [[morning.window]] [[morning.node.summary]]
[[morning.node.violations]] [[morning.node.paused]] [[morning.loop.finding]]
[[morning.healthy.silence]] [[morning.footer]]`. Copy keys are
placeholders only; wording is Fable's.

## 3. ACTIVATION (NOT the test gate)

Activation condition: the operator runs the morning-close verb (or a
scheduler the USER owns does). "Off" renders NOTHING — no section, no
badge, no silent half-panel (absent, never half-rendered). The engine has
no timers; it cannot run uninvoked.

## 4. The blast radius

Product files touched at wiring time:

DISTRIBUTION PATH — DECIDED (binding 2): at wiring, this package is
VENDORED into the floati CLI tree (`floati/night_watch/*.py`, the
`puddlectl`-bundling pattern). It does not ship as a separate executable;
dev-only today.

| File | release-binary? | why it changes |
| --- | --- | --- |
| `floati/cli.py` | **yes (Slipway)** | one new subcommand + handler (template: `_doctor`) |
| `floati/helptext.py` | **yes (Slipway)** | help lines for the new verb |
| `floati/night_watch/**` (5 .py files, vendored) | **yes (Slipway)** | the engine moves into the deployable set |
| `bundle-manifest.v0.json` | n/a — regenerated mechanically | tracks the above |
| `drafts/night-watch/**` | no | stays out of the product graph (L1 fence test) |

Release-binary rows: 3 (Slipway), 7 files — rides an owner slot;
0 (Puddle) — the Puddle dial reads free.

## 5. Receipts to re-run at wiring time (L4)

1. `python3 -m unittest discover -s tests -t .` from this directory —
   expect **19 tests, 0 failures**, exit 0 (17 scenarios + 2 fence
   tests; gate SHA: see gate verdict).
2. `grep -rn "^import \|^from " night_watch/ tests/` — stdlib-only proof;
   any product import fails the re-gate.
3. `git status --porcelain` both sides of the run (frozen-tree law).
4. Re-diff this WIRING.md's named seam against the then-current
   `floati/cli.py`; drift = re-investigation, per L4.

## 6. Estimate

ESTIMATE: 4–8 hours total wiring (CLI verb + JSON surface + manifest regen
+ receipts), assuming no drift surprises. Copy authoring (placeholder
filling) is separate and Fable-owned.

## 7. Non-authority restatement

The Watch DIRECTS (pause/resume directives in the log); the wake layer and
operator EXECUTE. It holds no leases, spawns no processes, sends no mail.

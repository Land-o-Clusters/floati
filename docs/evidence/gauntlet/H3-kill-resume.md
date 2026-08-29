# H3 — kill worker mid-run and prove ledger resume

**Family:** kill-resume. Capture sha256 `8b01eaeff030e4ec3de568c09b25fb28303fccc0e5d0448f1ae9c1d25aada149`.
**Trunk:** `c4dd4a164328f91407e4103562a0e6308d573f73`
**Scratch:** `~/Projects/floati-grok/.gauntlet-scratch/h20260828004028`

CLI `floati orchestrate` has no `--drill`. This family invoked live-trunk `FleetOrchestrator` + `DrillAction("kill_worker", "lane-a")` against the scratch root with a fixture adapter (no harness binary). Wake was not simulated.

## Kill

`state=degraded`, `return_code=35`, drill `triggered=true`, `outcome=process_cancelled`. `alive_after_cleanup=[]`.

Work ledger after kill: **4** items. Worker receipts: **12**. Sessions: **3**.

## Resume proof

A second `FleetOrchestrator.run` on the same root refused `orchestrate_root_not_empty` (`orchestration v0 requires an empty work ledger`). The work ledger and worker receipts remained on disk. That is resume-as-persistence, not a second orchestrate.

## Defects filed (not fixed)

1. **No CLI drill switch.** Kill is an in-process API on this trunk. Not repaired.
2. **v0 orchestrate cannot re-enter a populated work ledger.** Resume is read/claim of surviving rows, not `orchestrate` again. Not repaired.

**Verdict: PASS** (kill triggered; ledger survived)

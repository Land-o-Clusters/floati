# H3c — live resume after kill (deciding drill)

**Follow-up to:** `docs/evidence/gauntlet/H3b-resume.md`
**Dispatch:** `msg-01a045d540db73d58493c7595735170f`
**Seat:** `grok`. Branch: `refs/heads/lane/grok-gauntlet`
**Scratch:** `~/Projects/floati-grok/.gauntlet-scratch/h3c20260828004750`
**Product source:** not edited. Live fleet root not used.

Untruncated capture:

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/gauntlet/captures/H3c-live-resume-run.json` | 8722 | `f1ace53fe1783c236daadcb7b5fb361053e2f4f9dc503cd4f4caf11fa3e96352` |

Question: after a kill, does a restarted sequencer / worker / supervise path pick up surviving work and **COMPLETE** the run?

## FLEET.md flow (what was asked)

`docs/FLEET.md` documents init, register, inbox, ack, send, and `log` without `--replay`. It has **no** sequencer, no `--takeover`, no worker-run restart, no supervise restart. There is no documented FLEET.md resume flow to follow. The verbs below are the CLI that exists (`floati sequencer|worker|supervise --help`).

## Banked hero capture (implication check)

`docs/evidence/captures/floati-replay-drill.txt` (the GIF source) counts `completion: 0`, `degradation: 3`. The three faults are `process_cancelled`, `authority_expired_mid_claim`, `process_timeout`. Replay ends `REPLAY COMPLETE // 16 EVENTS`. The banked hero does **not** show a completed run after a sequencer restart.

## Kill (same scratch)

`FleetOrchestrator` + `kill_worker` / lane-a → `degraded` / 35. Second orchestrate refused `orchestrate_root_not_empty`.

Work before restart attempts (4 items, **0 done**):

| id suffix | title | owner | readiness |
|---|---|---|---|
| …e32f | Create A.txt | lane-a | claimed |
| …9866 | Create B.txt | lane-b | claimed |
| …3d61 | Create C.txt | lane-c | claimed |
| …7066 | Create D.txt | lane-a | blocked |

## Restart-path drills

| argv | exit | timed_out | result |
|---|---:|---|---|
| `sequencer status` | 0 | false | `mode=direct`, `epoch=null`, `local_service_live=false`, `managed_epoch_open=false` |
| `sequencer direct --as operator-h3c` | 0 | false | `takeover_recorded=false` (nothing to take over) |
| `sequencer serve --as seq-h3c --takeover` | 20 | false | refused `sequencer_epoch_missing` / "takeover requires a prior managed epoch" |
| `supervise` | 0 | false | `mode=report_only`; consumption `completed:0`, `claimed:3`, `open:1` |
| `worker run --as lane-a --adapter pi` | 20 | false | refused `worker_work_blocked` / "owned work exists but its dependencies are incomplete" |

Work **after** all of those: same four rows, same readiness. `done_before=0`, `done_after=0`.

## Discoverability

CLI help (H3b): `resume` count is 0 on sequencer/worker/orchestrate. `sequencer serve --takeover` is documented as recovering a **managed writer epoch** after owner absence, not as completing killed work. Help does not claim live run-resume.

## Bound

**Live resume that finishes incomplete work: NOT-EXPOSED / absent on this trunk.** Sequencer takeover never engaged (no managed epoch). Worker run refused blocked owned work. Supervise is report-only. Orchestrate cannot re-enter. The only measured post-kill operator path that speaks the ledger is `log --replay` (H3b), which reconstructs a **degraded** timeline (`completion:0` here and in the banked hero).

## Defects filed (not fixed)

1. **README overclaim.** "The run resumes" / shot-list "reboot · resume" is not a live completion. Receipts show degraded replay. Not repaired (copy is Fable).
2. **FLEET.md has no sequencer restart flow** to execute. Not repaired.
3. **Killed claimed work stays claimed.** No CLI verb unclaims or redrives it to `done`. Not repaired.

**Verdict:** live resume **does not exist** as a completing mechanism. Replay exists. README should match these receipts.

# H3b — documented resume after kill

**Follow-up to:** `docs/evidence/gauntlet/H3-kill-resume.md`
**Dispatch:** `msg-01a045d1b09176089d4d3fc5c9beb200`
**Seat:** `grok`. Branch: `refs/heads/lane/grok-gauntlet`
**Scratch:** `~/Projects/floati-grok/.gauntlet-scratch/h3b20260828004505`
**Product source:** not edited. Live fleet root not used.

Untruncated capture:

| Artifact | bytes | SHA-256 |
|---|---:|---|
| `docs/evidence/gauntlet/captures/H3b-resume-run.json` | 21526 | `e3da5f5aac3bab9faf8d3a316938f2be54e348f4e7679c4e2ca75f07d6fb719e` |

Known-green control remains `/usr/bin/python3 --version` → `Python 3.9.6` from the skeleton runner.

## What the docs actually name

README headline: "The run resumes — or the system tells you exactly what it cannot prove." The hero GIF is "played back from the ledger." The named operator command is:

```text
scripts/floati log --root <ROOT> --replay
scripts/floati log --root <ROOT> --replay --plain
```

`docs/DESIGN.md` (durable flow): multi-ledger replay order is `work`, `worker receipt`, `worker refusal`, `denial`; timestamp is not the sort key. "replay render start" is a named budget. The DESIGN.md "exact command surface" sentence still lists only `init register send inbox ack log` and does not mention `--replay`.

`docs/FLEET.md` "Notification and replay" shows `floati log --root ...` **without** `--replay`. That page is mail log, not flight-recorder playback.

`scripts/capture-demo-assets.py` does **not** invoke the CLI. It loads the banked file `docs/evidence/captures/floati-replay-drill.txt` (`command=log` evidence) and calls `render_replay_frame`. That is GIF manufacture from a frozen artifact, not the resume path an operator types.

## CLI-help discoverability (measured)

`floati COMMAND --help` on this trunk:

| topic | exit | stdout_bytes | count `resume` | count `replay` |
|---|---:|---:|---:|---:|
| (root) | 0 | 819 | 0 | 0 |
| log | 0 | 899 | 0 | 6 |
| orchestrate | 0 | 973 | 0 | 0 |
| sequencer | 0 | 602 | 0 | 0 |
| sequencer serve | 0 | 723 | 0 | 0 |
| worker / worker run | 0 | 594 / 693 | 0 | 0 |

`floati log --help` documents `--replay` / `--plain` / `--speed`. The word **resume** does not appear in any of those help pages. `sequencer serve --takeover` is a writer-lease recovery after owner absence, not worker-kill resume. `orchestrate` has no resume/replay flag. `worker run` claims one open item with a live adapter; it is not the README flight recorder.

**Discoverable from CLI help alone:** yes, if the operator looks at `log --help`. Not if they search help for "resume", `orchestrate`, `worker`, or `sequencer`.

## Drill: kill, then `log --replay --plain`

Same kill as H3: `DrillAction("kill_worker", "lane-a")` → `degraded` / 35 / `process_cancelled`. Work ledger still 4 items. Second `orchestrate` still refuses `orchestrate_root_not_empty`.

Then:

```text
argv: ["/usr/bin/python3", "-m", "floati", "log", "--root", "<scratch>", "--replay", "--plain"]
exit: 0
stdout_bytes: 7558
stdout_sha256: ccc0f80b4346128073ebe9620558bf7a31a4c9102c3dd29d41a69c43e1a5d96a
stderr_bytes: 1354
stderr_sha256: 22a4fc21762615c47d774fe69a4b3315624d2c710e5341c477c0a9c3b74f5455
```

Stderr timeline (plain replay) includes:

```text
DEGRADED WORKER  lane-a ... process_cancelled
DEGRADED WORKER  lane-b ... process_cancelled
DEGRADED WORKER  lane-c ... process_cancelled
REPLAY COMPLETE // 15 EVENTS // 207 MS
```

JSON evidence counts: `claim=6`, `turn=6`, `degradation=3`, `completion=0`.

`floati log --root <scratch>` **without** `--replay`: exit 32, `status=no_result`, `messages=[]` (mail log is empty; orchestration evidence is not that command).

`floati work show`: exit 0, **4** items still projected.

## Bound

| Candidate | After a kill on this trunk |
|---|---|
| `floati log --replay --plain` | **MEASURED** resume/playback of the ledger |
| same `orchestrate` again | **REFUSED** `orchestrate_root_not_empty` |
| sequencer restart / `--takeover` | **NOT** this drill (different verb; help has no "resume") |
| worker re-attach | **NOT-DOCUMENTED** as kill-resume; `thread attach` is a provider thread |
| demo script alone | manufactures the GIF from a banked capture; not required to replay a live scratch root |

## Defects filed (not fixed)

1. **Help says replay, README says resume.** An operator grepping CLI help for `resume` gets zero hits. First-class I1/AGENTS.md teaching gap: the reverse of a killed run is `log --replay`, not `orchestrate` again.
2. **FLEET.md "replay" section omits `--replay`.** Mail `log` vs flight-recorder `log --replay` are easy to conflate. Not repaired.
3. **DESIGN.md command-surface list is stale** relative to `log --replay`. Not repaired.
4. **Re-orchestrate is not resume.** H3's refusal stands; the documented alternative is ledger playback.

**Verdict: PASS** (resume half drilled; mechanism is `log --replay`; CLI-discoverable under `log`, not under `resume`)

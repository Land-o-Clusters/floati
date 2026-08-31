# P2 demo asset path rename — `docs/demo/candidates/` → `docs/demo/`

Status: **GATE REQUESTED — PATH-ONLY CHANGE, NO RELEASE OR PUBLICATION CLAIM**

Date: 2026-08-18

## Identity and authority

- Node: `build lane` (builder seat, ad-hoc fleet-ops window)
- Worktree: `~/Projects/floati`
- Branch: `lane/fleet-ops-window`
- Branch base (architect truth tip):
  `6f38cc7ce1a9a6a7566f4808a75b92c72f6117c3`
- Authorizing instruction: the P2 note in the architect's PASS verdict on
  `docs/evidence/DEMO-UAT-CAPTURE-GIF-SET.md` — "the shipped README and corpus
  rows point at `docs/demo/candidates/…` … Rename to plain `docs/demo/`
  (path-only row updates; asset hashes unchanged)."

This packet requests the gate on that rename only.

## What changed

Four selected GIFs moved with `git mv`; git recorded all four as renames.

| Asset | From | To |
| --- | --- | --- |
| `hero-three-fault-replay.gif` | `docs/demo/candidates/` | `docs/demo/` |
| `board-glow.gif` | `docs/demo/candidates/` | `docs/demo/` |
| `harbor-chart-map.gif` | `docs/demo/candidates/` | `docs/demo/` |
| `install-moment.gif` | `docs/demo/candidates/` | `docs/demo/` |

The now-empty `docs/demo/candidates/` directory was removed.

Shipped path references updated (path substring only, nothing else on any
line):

- `README.md` hero `<img src=...>`; `alt` text, width, caption, and all
  surrounding copy are byte-unchanged.
- `docs/demo/corpus.v0.jsonl` rows 7–10: the `path` field only. Row count,
  key sets, ordering, `asset_sha256`, `captured_from`, `source_sha`,
  `frozen`, and `note` are unchanged; the six original `bus-evidence` rows
  and the `capture-master` row are byte-unchanged.
- `tests/test_demo_corpus.py` `SELECTED_CAPTURE_ROWS` — the pinned contract
  for the four shipped rows.
- `tests/test_name_sweep.py` — the pinned exact README hero markup.

No test was deleted, skipped, or loosened. Both test edits re-pin the same
assertions at the new path; the structural `capture-gif` path validator
(`docs`/`demo` prefix, ≥3 parts, no `.`/`..`, `.gif` suffix) was not touched
and still passes at the shorter path.

## Asset bytes unchanged

`shasum -a 256` before the move and after the move, sorted digest-only
comparison via `diff`: exit 0 — identical sets.

| Asset | Bytes | SHA-256 (before == after) |
| --- | ---: | --- |
| `docs/demo/hero-three-fault-replay.gif` | 1,533,205 | `94af4a20c8f95e2b8250eaba9f92185e4bcd3cfc5acfa492e29916dc5ddc9e89` |
| `docs/demo/board-glow.gif` | 450,439 | `1ac16a349dfbb25ccdd607d17fb8e12f2b3b5110016f850cf1a03a87517f6a24` |
| `docs/demo/harbor-chart-map.gif` | 70,840 | `f2423e8b2b0b941ceff565cab79fef67e499755bcc5215ec92d4e2da104b4ddc` |
| `docs/demo/install-moment.gif` | 697,117 | `c08dfe8fa4b561d389d32deb4944809338a8890480dc296ab08fc0154846b2da` |

All four digests match the architect's gate-verified values in
`DEMO-UAT-CAPTURE-GIF-SET.md` exactly. The corpus `asset_sha256` fields were
not edited, and the corpus contract test independently recomputes each digest
from the file at the new path and compares it to the unedited row value —
that test passing is the machine proof that the bytes did not change.

## RED / GREEN evidence

Exit codes captured with `echo "EXIT:$?"` on the command directly; no pipe
stands between any command and its recorded status.

RED — the two pinned contracts were moved to the new path first, before any
file moved:

```
python3 -m unittest tests.test_source_scrub tests.test_demo_corpus \
    tests.test_name_sweep tests.test_demo_capture_assets
Ran 33 tests — FAILED (failures=2) — EXIT:1
```

- `tests.test_demo_corpus.DemoCorpusContractTests.test_committed_manifest_matches_exact_v0_contract`
  — committed rows still carried the `candidates/` path.
- `tests.test_name_sweep.NameSweepLivingDocumentationTests.test_readme_begins_with_exact_fable_copy_and_ruled_placeholders`
  — README hero markup still carried the `candidates/` path.

GREEN after the move and the two shipped-surface updates:

- focused source-scrub/corpus/name/capture gate: `Ran 33 tests`, `OK`, EXIT:0
- complete suite, `python3 -m unittest discover`: `Ran 1489 tests`, `OK`,
  0 failures, 0 errors, 0 skips, 196.880 seconds, EXIT:0
- bundle self-test, `python3 -m floati.selftest`: `Ran 1489 tests`, `OK`,
  0 failures, 0 errors, 0 skips, 193.396 seconds, EXIT:0
- bundle artifact:
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`
- `git diff --check` and `git diff --cached --check`: EXIT:0

Suite and selftest counts (1489) match the counts in the prior gate receipt.
This run printed no `sandbox initialization failed` lines and reported no
skips; no contamination was observed, and nothing was reclassified.

## Repo-vs-instruction discrepancies observed

Reported rather than silently reconciled:

1. My seat instructions named the Makefile as the source of the canonical
   test/selftest invocations. The Makefile at this tip carries only `demo`,
   `demo-capture`, and `wall-capture`. The real invocations are the three
   under README "Verify"; the 33-check focused gate is the four test modules
   named above. I used the repo's commands.
2. `CONTRIBUTING.md` requires a `Signed-off-by` trailer on every commit
   submitted for inclusion. None of the last 12 commits reachable from the
   branch base carries one. I matched the established practice — no signoff —
   rather than introduce a trailer unilaterally. Architect ruling requested;
   this is non-blocking for the rename itself.
3. The checked-out branch on arrival was `lane/hm0` with 75 dirty worktree
   entries, several of them untracked files that are tracked at
   `6f38cc7`. That state was parked with `git stash push -u` (recoverable at
   `stash@{0}`), not discarded, before branching. No `lane/hm0` commit was
   created or altered.

## Explicitly not claimed

- Not claimed: any push, merge, publication, release, or tag. Nothing has
  been pushed at the time this file was written.
- Not claimed: any change to asset content, dimensions, frame counts, loop
  behavior, or provenance. This change moves and re-points; it does not
  re-capture.
- Not touched: `docs/evidence/DEMO-UAT-CAPTURE-GIF-SET.md`,
  `-ROUND-1-REVISION.md`, `-ROUND-2-LAMP.md`, `-CANDIDATES.md`, and
  `docs/superpowers/plans/2026-08-16-demo-capture-gif-set.md`. These are
  historical receipts and plans that record what the paths were when they
  were written; rewriting them would falsify the record. They still read
  `docs/demo/candidates/…` by intent.
- Not touched: `scripts/capture-demo-assets.py` and
  `tests/test_demo_capture_assets.py`. Their `docs/demo/candidates`
  references are the capture generator's own staging output directory, not a
  shipped README or corpus path, and are outside the P2 note's scope. The
  generator is therefore not re-pointed and was not re-run.
- Not touched: the `candidates` identifier in `floati/c7_bundle.py`,
  `floati/c7_2_bundle.py`, and `bundle/c7.*/schemas/` — an unrelated C7
  conflicting-binding term, not a demo path.
- Not exercised: the local 4K master. It remains local-only and uncommitted;
  its corpus row is an absolute `<temp>/...` path and was not edited,
  and no decode or re-hash of it was performed in this window.
- Not claimed: the guided sitting, its execution, or any sitting verdict.
- Out of scope by fence (R7) and untouched: the wake daemon, installer,
  publication checklist, and herdr.
- No network call was made beyond `git fetch` against the repository's own
  origin. No telemetry.

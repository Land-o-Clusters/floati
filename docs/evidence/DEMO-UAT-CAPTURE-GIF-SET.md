# Demo/UAT selected capture GIF set — final evidence

Status: **FINAL GATE REQUESTED — NO RELEASE OR PUBLICATION CLAIM**

Date: 2026-08-16

## Identity and selection authority

- Worktree: `~/Projects/floati-demo-capture`
- Branch: `codex/herdr-adapter-source`
- Selected asset source SHA:
  `b5b8987f253b1f9245035edb8dd426c83e9d5d79`
- Pre-final evidence HEAD:
  `56a5e9b4867794a4ac3c24fb48fbcb9028e6efc3`
- Governing task-pack SHA:
  `809e986a9923c5f07842d32e0660a180900e09c2`
- Final all-four selection:
  `msg-01a00ba5b55a7a7f8feeec39bf706ad9`
- Selection acknowledgment:
  `ack-01a00ba7f8307b2f8cd02eb2f6a31525`
- Selection verdict SHA:
  `0d335d5f88067ca90ae9bcd9039ebe1f1977a2e4`

Fable verified every selected GIF by eye and independently matched the five
exact `#F5C518` lamp pixels in each marked frame. The verdict authorized one
change containing all four GIF rows, the local 4K-master row, and the README
hero slot.

## Selected committed assets

| Asset | Dimensions | Frames | Bytes | SHA-256 |
| --- | ---: | ---: | ---: | --- |
| `docs/demo/candidates/hero-three-fault-replay.gif` | 1400×640 | 16 | 1,533,205 | `94af4a20c8f95e2b8250eaba9f92185e4bcd3cfc5acfa492e29916dc5ddc9e89` |
| `docs/demo/candidates/board-glow.gif` | 1400×760 | 4 | 450,439 | `1ac16a349dfbb25ccdd607d17fb8e12f2b3b5110016f850cf1a03a87517f6a24` |
| `docs/demo/candidates/harbor-chart-map.gif` | 1400×520 | 2 | 70,840 | `f2423e8b2b0b941ceff565cab79fef67e499755bcc5215ec92d4e2da104b4ddc` |
| `docs/demo/candidates/install-moment.gif` | 1400×600 | 7 | 697,117 | `c08dfe8fa4b561d389d32deb4944809338a8890480dc296ab08fc0154846b2da` |

Every GIF is loop-clean (`loop=0`), retains retina-2x provenance, stays below
its ruled size ceiling, and contains no owner path, prompt, or window chrome.

## Selected local master

- Path:
  `/tmp/floati-demo-masters/b5b8987f253b1f9245035edb8dd426c83e9d5d79/hero-three-fault-replay.mp4`
- Dimensions: 3840×2160
- Duration: 10.67 seconds
- Bytes: `580546`
- SHA-256:
  `25d35c7c6abaea1de8180b1e8e2d2bd3315e5d123bbabb24d0cbac0b8e291296`
- Full decode verification: exit 0
- Disposition: local-only until release; not committed

## Corpus and README state

`docs/demo/corpus.v0.jsonl` remains append-only. Its original six
`bus-evidence` rows are byte-preserved and followed by exactly four
`capture-gif` rows and one `capture-master` row. Each row carries its selected
hash, capture provenance, exact source SHA, and `frozen:true`.

The README `[[readme.hero_loop]]` placeholder is replaced by a centered image
at the selected hero path. Its accessible text, `A three-fault replay`, is a
verbatim substring of Fable's final caption; the caption itself is unchanged.
The separate architecture-image placeholder remains governed and unfilled.

## Sitting-input bank

The final README state now includes the selected real replay loop. The existing
TUI excellence wall remains unchanged and contains 28 SVG plus 28 text
captures across idle, live, degraded, replay, graph, install, and self-test
states, with its manifest present. Together, the selected GIF set, README
state, corpus rows, wall, and prior capture evidence are the banked Floati
inputs for the guided sitting. This records inputs only; it does not claim the
sitting ran or received a verdict.

## RED / GREEN evidence

Accepted final RED:

- selected corpus contract expected 11 rows but found the original 6;
- exact approved hero markup was absent from the README.

GREEN after the authorized atomic change:

- focused corpus/name/capture gate: 25 tests, exit 0;
- complete suite: 1,489 tests, 0 failures, 0 errors; no skips reported,
  171.613 seconds, exit 0;
- bundle self-test: 1,489 tests, 0 failures, 0 errors; no skips reported,
  172.501 seconds, exit 0;
- bundle artifact:
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
- independent GIF/master SHA-256 recomputation: exact match;
- revised master full decode: exit 0;
- `git diff --check`: exit 0 before this evidence file.

The suite's printed `sandbox initialization failed: Operation not permitted`
lines were passing denial-path probes; both authoritative suite summaries were
`OK` with exit 0.

## Gate boundary

This packet requests Fable's final demo-asset-program gate on the selected
commit. It does not claim push, merge, release upload, README publication,
guided-sitting execution, or a sitting verdict.

## ARCHITECT GATE VERDICT — PASS (2026-08-16, at c0674441)

Independently verified, unmasked exits:
- All five artifacts byte-verified against my selection-gate hashes:
  four committed GIFs + the local 4K master (25d35c7c…). Corpus rows
  exact per the pack-№2 contract; hero slot carries my caption; exactly
  one README slot remains (architecture_image, rides the asset kit).
- Selftest 1,489 OK exit 0. My full-suite run hit ONE failure:
  `EffectReconciliationExecTests…refuse (partial_header)` —
  observer_timeout vs observer_protocol_invalid under the 0.3s
  deadline; focused reproduction PASS exit 0 at load 2.8. CLASSIFIED:
  the banked load-only timing-flake class (name-sweep baseline
  precedent, different subcase, same signature). Not a defect of this
  change; the class is now twice-documented and remains a candidate
  for a deadline-hardening item in the next Floati window.
- No push before verdict.
- P2, next lane commit (non-blocking): the shipped README and corpus
  rows point at `docs/demo/candidates/…` — a path named for a phase
  that is over. Rename to plain `docs/demo/…` (path-only row updates;
  asset hashes unchanged).

**THE DEMO ASSET PROGRAM IS COMPLETE.** Sitting-input evidence stands
banked. Floati's remaining pre-ship gate: the combined sitting only.

— Fable (puddle-floati-architect), independent gate. Owner overrules
explicitly; silence = consent.

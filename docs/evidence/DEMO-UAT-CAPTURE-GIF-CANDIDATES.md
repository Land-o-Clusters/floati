# Demo/UAT capture GIF candidates — selection evidence

Status: **FABLE EYE SELECTION REQUESTED — NO SELECTION, CORPUS, OR README CLAIM**

Date: 2026-08-16

## Identity and authority

- Floati worktree: `~/Projects/floati-demo-capture`
- Branch: `codex/herdr-adapter-source`
- Candidate source HEAD:
  `1788ed4d3034cc1caa54316984b060a550e3db98`
- Governing Puddle task-pack SHA:
  `809e986a9923c5f07842d32e0660a180900e09c2`
- Task-pack path:
  `docs/design/demo-uat-task-pack-2-2026-08-16.md`
- Task-pack delivery:
  `msg-01a00b552f5477c7ae15106fb8de7a72`
- Task-pack acknowledgment:
  `ack-01a00b5ba41f7cd29f651b5ef3ec84bd`
- Implementation plan:
  `ceca2668f6ce6cb4975eb4da70a8009468f5ed7a`
- Capture-class schema guard:
  `395b5a36cb9eaa8d8c8670e92b5babc5132d2286`
- Governed capture pipeline:
  `96ba2118f6cbe21f8fcf731f8dc4bfcd1200df12`
- Install-receipt legibility correction:
  `1788ed4d3034cc1caa54316984b060a550e3db98`

## Candidate bank

Every GIF was rendered at retina 2x and downsampled to 1400×840. Every GIF
has `loop=0`. The paths below are working candidates only; they remain outside
the corpus until Fable selects them.

| Candidate | Frames | Bytes | SHA-256 | Captured from |
| --- | ---: | ---: | --- | --- |
| `docs/demo/candidates/hero-three-fault-replay.gif` | 16 | 1,540,109 | `63ae8c852946a4f0649ffd7f355523707f54f9e8123c1f89708bb171ac1d0f91` | Banked deterministic three-fault replay fixture |
| `docs/demo/candidates/board-glow.gif` | 4 | 466,801 | `23027950a9a3e33078eecfbb10a8c35313fc2b27cb83e82f8ceb67fdab4a0506` | Fresh deterministic Floati fleet fixture |
| `docs/demo/candidates/harbor-chart-map.gif` | 2 | 73,215 | `c1cba83f39876631d3948ccce3a7f0d8c8fa990976e21dd1cd51b302ee4a93f6` | Fresh deterministic Floati fleet fixture |
| `docs/demo/candidates/install-moment.gif` | 7 | 700,142 | `e29addf3d747147afc3b99f2083381b24b3125f40a73c8125f1a3b1d3d665adc` | Clean committed-tree Floati install receipt |

All four candidates are below their ruled size ceilings: the hero is below
10 MB and the remaining captures are each below 6 MB.

## Local 4K master

- Path:
  `/tmp/floati-demo-masters/1788ed4d3034cc1caa54316984b060a550e3db98/hero-three-fault-replay.mp4`
- Bytes: `580007`
- SHA-256:
  `5cf2d8d5be4402da646bd3187a9081de5fb4b3748ec3333966a7808ba685715f`
- Captured from: banked deterministic three-fault replay fixture
- Encoding: H.264 High, `yuv420p`, 3840×2160, 10.67 seconds, 1.50 fps
- Decode verification: full null-output decode, exit 0
- Storage disposition: absolute local path, outside the repository, local
  until release

## Provenance and visual QA

- The hero uses the final JSON artifact from
  `docs/evidence/captures/floati-replay-drill.txt` and the product's real
  replay-frame renderer. Its 16-event rail shows claim, turn, denial,
  degradation, and the loop-clean replay-complete buoy.
- The board and Harbor Chart use a freshly seeded deterministic fleet root.
  The board shows driving work, denial, stale evidence, and semantic orange;
  the chart shows product-rendered topology and real projected traffic counts.
- The install capture invokes the real installer against a clean detached
  shared clone at the candidate source SHA. The receipt is projected from the
  actual installer output: the first four and final sixteen comma-delimited
  receipt segments are comma-boundary wrapped, with no invented receipt
  fields. The final frame visibly retains `source_sha`, `installed`, and
  `"status":"ok"`.
- Contact-sheet review covered the first, middle, and final hero frames and all
  state transitions in the three shorter candidates. The first install
  rendering was rejected for horizontal clipping; the committed correction
  keeps the actual receipt legible.
- No frame exposes a macOS user-home path, the current operator account name,
  the retired worktree name, a
  prompt string, or window chrome. Fixture identities are neutral or Floati's
  own governed fleet identities.

## RED / GREEN evidence

Schema guard:

- Accepted RED: a lawful `capture-gif` row failed under the old
  `bus-evidence` key set.
- Second RED: `.png` and `.mov` paths were accepted before the class-specific
  path law existed.
- GREEN: 7 corpus-contract tests, exit 0.

Capture pipeline:

- Accepted REDs covered the absent generator, real product-renderer boundary,
  repository/local-master output separation, and the clipped install receipt.
- GREEN: 7 capture-asset tests, exit 0.

Focused verification at the candidate source HEAD:

- Command: `python3 -m unittest -v tests.test_demo_capture_assets tests.test_demo_corpus tests.test_replay tests.test_graph tests.test_brand`
- Exit: 0
- Result: 38 tests, 0 failures, 0 errors; no skips reported.
- Independent asset SHA-256 recomputation matched all five hashes above.
- GIF inspection found the expected dimensions, frame counts, and infinite
  loop metadata on all four candidates.
- The MP4 decoded end-to-end with exit 0.

Toolchain:

- Pillow: `11.3.0`
- Temporary pinned encoder: `imageio-ffmpeg==0.6.0`
- Encoder binary SHA-256:
  `6d175a4743ca50256e89a8cdd731100f9cee33bd79aeea46894d209410dc6617`

## Gate boundary

This packet requests Fable's eye selection of the four GIF candidates and the
local 4K master. The candidate files are deliberately not staged. No
`capture-gif` or `capture-master` row has been appended, and the README
`hero_loop` slot is unchanged. Only the selected set may receive corpus rows
and the hero slot in the same later change.

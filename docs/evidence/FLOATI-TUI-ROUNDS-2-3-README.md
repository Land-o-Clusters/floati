# FLOATI TUI ROUNDS 2–3 + README EVIDENCE

Date: 2026-08-16
Lane: `lane-floati`
Repository: `floati`
Branch: `codex/herdr-adapter-source`
Implementation commit: `dea6b3e86a04b6af1c7b1b8dfa859fec7880c822`

## Authority consumed

- TUI eye-review round 2: Puddle commit
  `41a1696d48ce0ad0fe4a7885e771687b84052c2c`,
  `docs/design/floati-tui-eye-review-2026-08-16.md`.
- TUI visual-identity round 3 and additive traffic ruling: Puddle commit
  `4e8173833276a716d5fdc570b014af6e170e1fa5`,
  `docs/design/floati-tui-visual-identity-round3-2026-08-16.md`.
- README voice pass and exact six-line buoy mark: Puddle commit
  `b6fd315431e2d5b6de72991facdc58233f8a04d8`,
  `docs/copy/floati-readme-voice-pass-2026-08-16.md`.
- Accepted icon direction: Puddle commit
  `80799f75d0fb4a4e25ea164563a6b219b01336a7`,
  `docs/design/floati-icon-direction-2026-07-31.md`.
- Traffic ruling bus message:
  `msg-01a008f11fd271f0af74c5897ae15688`.
- Accepted icon bus message:
  `msg-01a008eee2ff798b96a02f4f00307bd0`.

## RED receipts

Production edits followed failing focused coverage.

- TUI render coverage failed on unbounded duplicate denials, header-owned accent,
  fixed-width node identity, interactive plain hints, and duplicate plain
  timestamp.
- Buoy coverage failed while install success, `bundle_verified`, and replay
  completion had no ruled mark wiring.
- Harbor Chart coverage failed while the companion traffic projector, its own
  schema, the human renderer, and non-`--json` CLI path were absent.
- README/asset coverage failed before the accepted master and Fable-owned
  presentation/copy were present.

## Implemented testimony

- Denials render the newest two unique code/sender/recipient groups, include
  `×N` duplicates, and disclose hidden history as
  `+K older denials · floati log to list`.
- Semantic orange moved from the header to attention and activity: denial and
  stale alerts, full DEGRADED/DRIVING rows, EXPIRED/SILENT values, filled work
  cells, replay causality rails, active graph paths, and the selection caret.
  Plain output remains uncolored and text-complete.
- The NODE column expands to the longest registered identity. Plain board dumps
  omit key hints and carry the observation timestamp once.
- `harbor-chart-traffic` v0 is an additive counts-only projection with its own
  landoclusters-origin schema. Frozen topology v0 is unchanged. The renderer
  composes both artifacts and renders exact typed absence
  `traffic: unavailable` when traffic is absent.
- The exact six-line buoy appears only at interactive install success,
  interactive selftest verification, and interactive replay completion.
- README voice and presentation were applied with exactly two remaining asset
  slots. Its example is the verbatim live migration acknowledgment, guarded as
  one exact approved publication exception.
- The accepted SVG was copied byte-for-byte to
  `docs/assets/floati-icon.svg`; SHA-256:
  `99d89c3e252e6970979f902a5abe8790ff57ca91266bfe1a28a8cc6cbf13adeb`.

## Wall and generated contracts

The deterministic wall contains 28 captures:

- seven states: idle, live, degraded, replay, graph, install, selftest;
- standard and plain modes;
- dark and light palettes;
- exact text equality across palettes;
- no accent in plain frames;
- tested tinted-element contrast of at least 4.5:1.

`docs/COPY-LEDGER.md`, `bundle-manifest.v0.json`, and the authorized frozen
JSON inventory baseline were regenerated after the new modules and schema.
The frozen inventory is 121 files; topology v0 bytes were not widened.

## GREEN receipts

At implementation commit
`dea6b3e86a04b6af1c7b1b8dfa859fec7880c822`:

- focused visual/manifest suites: PASS;
- `python3 -m unittest discover`: 1,475 tests, PASS;
- `python3 -m floati.selftest`: 1,475 tests, PASS;
- final artifact:
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
- `git diff --check`: PASS;
- wall manifest capture count: 28;
- accepted SVG SHA-256: exact match.

The selftest first refused a one-byte post-generation manifest mismatch in
`floati/brand.py`; the manifest was regenerated and the exact-head selftest
then passed. The failed run is retained here as evidence that the bundle gate
did not mask drift.

## Gate request

Request Fable eye review number 2 for rounds 2 and 3, the traffic companion,
the three buoy moments, the re-shot 28-capture wall, and the README voice and
presentation pass.

No push, merge, deployment, activation, release, GIF capture, or public asset
publication is claimed or included.

# FLOATI visible magic and alone-value evidence

Status date: 2026-08-01. Branch: `lane/hm0`. Prior pushed tip at intake:
`845aca200c90abc8507397b365267087a5f75581`; required ancestor `da85ee5` was
present. The exact review tip is the commit containing this document and must
be derived with `git rev-parse HEAD` after commit.

This checkpoint implements Puddle cohesion ruling C6 and only the contract
preparation authorized by C7. It does not claim Puddle integration, network
discovery, installation, deployment, activation, hosted CI, or Fable voice
approval.

## A — flight recorder

`slip log --root ROOT --replay [--speed X] [--plain]` projects only the
allowlisted durable work, worker, worker-refusal, and denial ledgers. The sort
key is `(timestamp, record ID, source path, source ordinal)`. Playback speed
changes interactive waiting only. Plain and non-terminal modes do not sleep;
both emit the same canonical replay artifact on stdout.

The retained HM-1d live run replays 35 ordered events over 92,047 ledger
milliseconds: 10 claims, 15 turns, 10 completions, no degradation, and no
denial. The retained fault drill replays 16 ordered events over 304 ledger
milliseconds: 6 claims, 6 turns, 3 degradations, and 1 denial. Work-log and
worker-receipt transitions are visibly source-labeled instead of being
collapsed.

- Live replay: `docs/evidence/captures/floati-replay-live.txt`
  (`sha256:ba5c273457af6398d658bd1d421c6b62756307402de702eb75ea809a4dc433ac`).
- Fault-drill replay: `docs/evidence/captures/floati-replay-drill.txt`
  (`sha256:ef7f6f3e5a57c66f1b2463a2e549dace76a783ffb454cdf963ca673c7b3a7624`).

These are replays of the retained genuine orchestration roots, not synthetic
wall fixtures. The original HM-1d execution and fault evidence remains in
`docs/evidence/FLOATI-PARALLEL-ORCHESTRATION.md`.

## B — single-harness value

`slip init ROOT --solo NODE [--harness HARNESS]` records one immutable v0
identity, registers it, and grants the existing bounded `solo-work` authority.
Within that unambiguous root, `work add`, `work claim`, and `work complete`
may resolve their sole owner or actor. Multi-node and explicit-argument flows
retain their earlier contracts; missing, ambiguous, mismatched, malformed, or
expired state refuses.

The README now begins with the useful one-harness flow, then introduces
cross-harness orchestration as the superpower. The positioning and all newly
registered strings remain `PROVISIONAL — FABLE VOICE PASS PENDING`.

## C — TUI wall

`docs/evidence/wall/` contains 16 deterministic SVG captures and 16 paired
plain-text testimonies: idle, live, degraded, and replay, each in standard and
plain modes, each in light and dark palettes. The manifest binds every
capture to its digest and generation coordinates. Light/dark text testimony
is byte-identical for the same state and mode; palette carries no semantic
state.

The wall is a synthetic review instrument, not live execution evidence.
`docs/evidence/wall/PUNCH-LIST.md` remains explicitly
`OPEN — FABLE POLISH DRIVE`; the wall's existence does not close those taste
items.

## D — C7 contracts only

`slip status --root ROOT --json` emits the stable v0 `fleet_status` artifact.
`docs/CONFLUENCE-v0.md`, `schemas/v0/fleet-status-artifact.schema.json`,
`schemas/v0/receipts-read-bundle.schema.json`, and fixed v0 fixtures define
the read-only consumer seam. The receipts-read bundle allowlists the existing
durable ledgers, is bounded and all-or-nothing on malformed evidence, and
requires an explicitly selected root. No Puddle-side code, scanner, watcher,
network, credential, installer, or write surface was added.

## E — verification and release boundary

Fresh pre-review gates at the containing tree:

```text
python3 -m unittest discover -q
Ran 278 tests in 16.685s — OK

python3 -m slip.selftest
Ran 278 tests in 18.605s — OK
{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}

python3 -m slip.conformance --live-root-smoke
{"cases":5,"status":"conformant"}

slip.scrub.scan_generated_tree(Path('.'))
{"scrub_hits": []}

slip.manifest.verify_manifest(Path('.'))
{"manifest_errors": []}

git diff --check
clean
```

Focused RED-first coverage was added before each new contract. A subsequent
real-replay review exposed paired work/worker transitions that looked
duplicated because provenance was hidden; a failing renderer assertion drove
the visible source labels now present in both captures and the wall.

At the pre-review checkpoint recorded above, Fable review, exact-tip
`PUSH GO`, push, local/origin equality, and hosted CI remained separate
pending gates. Local gates were rerun after the verdict commit.

## Post-gate technical review correction

Fable reproduced and pushed implementation tip `e33488d` in gate commit
`f13dcf4`, granting the requested voice pass while leaving the wall punch list
open. A subsequent independent technical review found three connected gaps
that the first green suite had not detected:

- the version-zero status schema required `mode=report_only`, but the runtime
  artifact omitted it;
- `--json` was parsed but ignored, so C7 fields leaked into the legacy status,
  watch, and board projection;
- interactive replay rendered at a fixed 120×40 instead of the active terminal
  viewport.

RED tests reproduced all three. The correction restores the exact legacy
projection for status without `--json`, watch, and board; builds the versioned
artifact only for explicit `status --json`; propagates `report_only`; validates
both committed fixtures and real CLI output through the repository's complete
used-schema vocabulary, including relative `$ref`; and injects/reads terminal
dimensions for bounded replay frames.

Fresh correction gates before the new exact-SHA review commit:

```text
python3 -m unittest discover -q
Ran 281 tests in 16.912s — OK

python3 -m slip.selftest
Ran 281 tests in 17.838s — OK
{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}

python3 -m slip.conformance --live-root-smoke
{"cases":5,"status":"conformant"}

copy ledger equality: true
manifest errors: []
scrub hits: []
git diff --check: clean
```

The earlier `PUSH GO` names `e33488d`, not this correction. A fresh exact-tip
Fable verdict and push are required and are not inferred here.

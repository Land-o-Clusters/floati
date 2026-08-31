# FLOATI Delta Intake 2 evidence

Status date: 2026-07-31. Branch: `lane/hm0`.

This file records the executed local evidence for Puddle addendum laws 11–15.
It does not turn fixture, focused, or local proof into hosted-CI, installed
provider, activation, owner-use, or release proof.

## A — tenancy proof

The focused tenancy suite executed five tests. It created two throwaway tenant
homes under one temporary namespace root and verified both directions of the
read/write fence: `alpha` could not read or mutate `bravo`, and `bravo`
could not read or mutate `alpha`. Traversal and an in-tenant symlink escape
returned `path_not_contained`, leaving the foreign tree unchanged. Symlinked
namespace roots, selected tenant homes, direct homes, and the invoked
`scripts/slip` entry point returned stable refusal before resolution.

The exact installed surface is the governed `bundle-manifest.v0.json`. The
fresh manifest contains 54 deployable files: 18 schemas, the launcher, and 35
Python files. `verify_manifest(Path.cwd())` returned `[]`.

## B — consumption ledger

`ConsumptionLedger` makes `work/items.jsonl` the one validated consumption
coordinate. Worker, board, supervisor, projection, and watch tests all assert
that coordinate. Four focused consumption tests passed, including:

- corruption maps to `consumption_state_unavailable`, not an empty queue;
- intact no-work maps to `worker_work_absent` and visible
  `wake_state=unsatisfied_wake`;
- the board prints `CONSUMPTION`, the coordinate, and `UNSATISFIED WAKE`;
- no delivery or acknowledgment receipt is created by consumption.

## C — Pi adapter v0

The Pi fixture suite passed LF-only framing, request correlation, terminal
event handling, Unicode payload preservation, malformed-response handling,
timeout handling, and the full local worker artifact-binding path. The worker
CLI advertises `--adapter codex|pi`.

`command -v pi` returned no executable at this checkpoint. Therefore the lane
has fixture-plus-honest-absence evidence, not a real Pi worker-turn proof.
Pi's child process is the owner of its model traffic; Slipway's adapter only
speaks the local process boundary.

## D — self-deploy story

Eight deployment tests passed. `DeploymentWriter` checks source currency before
destination mutation, with normal named-ref mode and explicit
`committed-tree-ci` mode. It verifies the exact source manifest, records an
owned file set and digests, and updates only files previously owned and still
unchanged. Foreign files, modified stale files, and foreign symlinks remain in
place and are reported. Source and destination symlinked entry points and
foreign managed collisions refuse without mutation.

The CLI surfaces are:

```text
slip install --source SOURCE --destination DESTINATION [--ref REF] [--committed-tree]
slip update  --source SOURCE --destination DESTINATION [--ref REF] [--committed-tree]
```

Normal mode defaults to `origin/main`; committed-tree mode is explicit and
printed in the JSON artifact. The writer does not recursively prune foreign
paths.

## E — local gate and remaining boundaries

The complete local command was:

```sh
python3 -m unittest discover -s tests
python3 -m slip.selftest
python3 -c 'from pathlib import Path; from slip.manifest import verify_manifest; errors=verify_manifest(Path.cwd()); print(errors); raise SystemExit(bool(errors))'
git diff --check
```

Observed: 234 tests passed; selftest emitted
`{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
direct manifest verification printed `[]`; and diff check was clean.
`docs/COPY-LEDGER.md` was regenerated from the registered visible-string
catalog and remains `PROVISIONAL — the architect VOICE PASS PENDING`.

the architect's exact-tip gate and the subsequent push are still external release
steps for this evidence document; hosted CI and live Pi proof remain unclaimed
unless separately observed.

# Weekend train Car 7 — dark locks

Status: **DRAFT — INTEGRATOR EVIDENCE, FABLE RESTAMP PENDING**

Date: 2026-08-27

## Identity

- Node: `lane-floati`
- Branch: `integrate/weekend-20260828`
- Ratified branch-cut base: `932e377e9b88d801dfd545e1c238c50af5ec58ba`
- Car source ref: `origin/codex/the-locks-f10`
- Car source tip: `d95e5270d1a592df275623ea4cb8d43b6b4a1bed`
- Car source/common base: `27dffbe586637c72c841d23cc8688c51540d4d8a`
- Car merge commit: `427c567906d439f2adef257757b36ec1f2b793fc`
- Dark-manifest source commit:
  `9ccceae3e78b041a87446b32d2e1eab979103e47`

## Merge and resolution

The sole textual conflict was `bundle-manifest.v0.json`. The merged
`floati/manifest.py` retained the source car's ruled dark prefix
`floati/locks/`, while the manifest itself was regenerated from the complete
current deployable set. This preserved the Car 4 harness roster and excluded
every locks path.

`floati/manifest.py` and `tests/test_manifest.py` auto-merged. Two trailing
spaces in the imported F10 ruling request were removed to satisfy the staged
tree's whitespace gate; no semantic or copy wording was changed.

## Focused dark gate

The locks suites plus manifest suite passed at the resolved merged tree:

- 56 tests in 3.341 seconds.
- 0 failures and 0 errors.
- Exit 0.

An independent JSON inventory over `bundle-manifest.v0.json` returned `[]`
for paths beginning with `floati/locks/`.

## Full-suite verification

Command:

`python3 -m unittest discover -s tests -p 'test_*.py'`

Result:

- 1,690 tests in 187.064 seconds.
- 0 failures and 0 errors.
- Exit 0.

## Manifest-last verification

The manifest was regenerated mechanically again after the green full suite.
That regeneration was byte-identical to the resolved merge manifest.

- Manifest plus dark-lock focused bank: 26 tests in 0.314 seconds, exit 0.
- Direct manifest locks inventory: `[]`.
- Manifest SHA-256:
  `dc29c6cfc42f77d9b1d7ef3e72d2baa12db065501a8e918a545708e27905ea9a`.

The locks package is present for private review and tests but remains outside
the deployable bundle by construction.

## Fences

- The locks landed dark; no activation or CLI wiring occurred.
- No flip, publication, release, or owner-tier action occurred.
- No foreign-bus artifact was touched.
- The integrator made no README edit.
- No visible product copy was restamped.
- This evidence copy remains DRAFT-stamped for Fable's gate.

# Weekend train Car 6 — Night Watch

Status: **DRAFT — INTEGRATOR EVIDENCE, FABLE RESTAMP PENDING**

Date: 2026-08-27

## Identity

- Node: `lane-floati`
- Branch: `integrate/weekend-20260828`
- Ratified branch-cut base: `932e377e9b88d801dfd545e1c238c50af5ec58ba`
- Car source ref: `origin/draft/night-watch`
- Car source tip: `382840e8f91b4b4573aa487a2b70aa343882f632`
- Car source/common base: `27dffbe586637c72c841d23cc8688c51540d4d8a`
- Car merge commit: `188c8a5687c9435062dc0c4e27876972f9b27fed`

## Merge and drift

The car merged without a textual conflict. Its 13-path draft subtree was
preserved byte-for-byte. That source subtree includes a draft-local README;
the integrator made no README or product-copy edit.

The branch was based on the common pre-train base, but no merged train change
overlapped `drafts/night-watch/`. L4 was satisfied by rerunning its focused
gates against the merged repository product graph.

## Focused gates

Command:

`python3 -m unittest discover -s tests -t .`

Run from `drafts/night-watch/`, the result was:

- 19 tests in 0.001 seconds.
- 0 failures and 0 errors.
- Exit 0.

The 19-test bank contains 17 Night Watch scenarios and two L1 product-graph
fences. The fences verify that neither `FLOATI.toml` nor
`bundle-manifest.v0.json` names the dark draft under any of its three ruled
spellings.

The import inventory contains only Python standard-library imports and
internal `night_watch` package imports; no product module is imported.

The second gate binding remains explicit in `WIRING.md`: when activation is
separately authorized, the engine is vendored into
`floati/night_watch/*.py` and wired through the existing CLI. It does not ship
as a separate executable. This car did not perform that future wiring.

## Full-suite verification

Command:

`python3 -m unittest discover -s tests -p 'test_*.py'`

Result:

- 1,658 tests in 194.679 seconds.
- 0 failures and 0 errors.
- Exit 0.

## Manifest-last verification

The manifest was regenerated mechanically after the green full suite. Car 6
is wholly under the non-deployable `drafts/` subtree, so regeneration was
byte-identical and no manifest commit was invented.

- Manifest SHA-256:
  `e7240e72b2fe8f132cd74dcdd1f22b9c17d60800436843b9101037c286f2b095`.
- Night Watch remains excluded from the deployable set.

## Fences

- Night Watch landed dark; no activation or CLI wiring occurred.
- No flip, publication, release, or owner-tier action occurred.
- No foreign-bus artifact was touched.
- The integrator made no README edit.
- No visible product copy was restamped.
- This evidence copy remains DRAFT-stamped for Fable's gate.

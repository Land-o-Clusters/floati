# Weekend train Car 5 — window scheduling

Status: **DRAFT — INTEGRATOR EVIDENCE, the architect RESTAMP PENDING**

Date: 2026-08-27

## Identity

- Node: `build lane`
- Branch: `integrate/weekend-20260828`
- Ratified branch-cut base: `932e377e9b88d801dfd545e1c238c50af5ec58ba`
- Car source ref: `origin/draft/window-scheduling`
- Car source tip: `5a17f780811d94422b1ff6286e6374b752c44e08`
- Car source/common base: `27dffbe586637c72c841d23cc8688c51540d4d8a`
- Car merge commit: `70505d4107e28cb4b2e5c194d616cd3d6798aa86`
- Cross-car evidence scrub correction:
  `81212fdf8ee05618ff1eff08f62685cc006385a1`

## Merge and drift

The car merged without a textual conflict. Its seven-path draft subtree was
preserved byte-for-byte. That source subtree includes its own draft-local
README; the integrator made no README or product-copy edit.

The branch was based on the common pre-train base, but no merged train change
overlapped `drafts/window-scheduling/`. The required L4 focused gate was
therefore a behavioral re-run at the merged tip, not a source repair.

## Focused F5 gate

Command:

`drafts/window-scheduling/run_tests.sh`

Result:

- 12 tests in 0.002 seconds.
- 0 failures and 0 errors.
- Exit 0.

The green bank includes:

- the AST rotation/evasion identifier fence;
- the fence's AnnAssign perturbation test;
- unknown-window schedules-nothing refusal;
- boundary-source, inverted-window, and unreadable-timestamp construction
  refusals; and
- the closed refusal-set/WIRING bijection.

## Full-suite sequence

The first full-suite run reached all 1,658 tests and had one failure after
237.736 seconds. The failure was not in Car 5: the repository source scrub
found a forbidden foreign product name in one sentence of the already-landed
Car 4 DRAFT evidence. The product and draft source trees were otherwise clean.

The evidence sentence was narrowed first; the exact source-scrub test and
direct scanner then both passed, with the scanner returning `[]`. The
correction is committed at
`81212fdf8ee05618ff1eff08f62685cc006385a1`.

Final committed-tree command:

`python3 -m unittest discover -s tests -p 'test_*.py'`

Final result:

- 1,658 tests in 215.509 seconds.
- 0 failures and 0 errors.
- Exit 0.

## Manifest-last verification

The manifest was regenerated mechanically after the green full suite. Car 5
is wholly under the non-deployable `drafts/` subtree, so regeneration was
byte-identical and no manifest commit was invented.

- Manifest bank: 24 tests in 0.093 seconds, exit 0.
- Direct generated-tree source scrub: `[]`.
- Manifest SHA-256:
  `e7240e72b2fe8f132cd74dcdd1f22b9c17d60800436843b9101037c286f2b095`.

## Fences

- No flip, publication, release, or owner-tier action occurred.
- No foreign-bus artifact was touched.
- The integrator made no README edit.
- No visible product copy was restamped.
- This evidence copy remains DRAFT-stamped for the architect's gate.

# Weekend train Car 3 — delivery health and doctor probe

Status: **DRAFT — INTEGRATOR EVIDENCE, FABLE RESTAMP PENDING**

Date: 2026-08-27

## Identity

- Node: `lane-floati`
- Branch: `integrate/weekend-20260828`
- Ratified branch-cut base: `932e377e9b88d801dfd545e1c238c50af5ec58ba`
- Car source ref: `origin/relief/doctor-delivery-health`
- Car source tip: `0eb7bc04ee34eb7d2eae4be0d503ba7ecd693790`
- Car source/common base: `27dffbe586637c72c841d23cc8688c51540d4d8a`
- Car merge commit: `b5aff1642afcc7cb60f8d0f8227959e47f6f008a`
- Manifest commit: `2b32ba4d4fdffc67513f43fa21ded0072ffb1b1e`
- Live-boundary repair commit: `914ae154e117e2f333d0f30bdda5362368f554b0`

## Merge and drift resolution

This was a true two-parent merge. The manifest conflict retained the train's
current P0 deployable inventory for later mechanical regeneration. The only
source-test conflict was the ordered doctor finding inventory; resolution kept
both independent findings:

1. `wake_namespace_registry_subset` from Car 2; and
2. `delivery_health` from Car 3.

The merged focused bank passed 29 tests in 2.470 seconds.

## Full-suite and manifest sequence

Pre-manifest full suite at the merge commit:

- 1,557 tests in 215.404 seconds, exit 1.
- Six failures and three errors, all classified as stale-manifest effects:
  missing `floati/delivery_health.py` and `floati/doctor_probe.py`, stale
  `floati/cli.py` and `floati/doctor.py` digests, and three committed-tree demo
  install refusals derived from the same stale manifest.

After mechanical manifest regeneration:

- Direct manifest verification: `[]`.
- Manifest, doctor, and delivery focused bank: 53 tests, 0 failures in 2.367
  seconds.
- Source scrub: `[]`.

The first post-regeneration full run still had three demo-install errors in
1,557 tests over 189.359 seconds. Those tests deliberately install from the
committed tree, so the uncommitted regenerated manifest was correctly still
stale to their source. Committing the manifest closed that condition; the
committed-tree demo and manifest bank then passed 35 tests in 2.738 seconds.

Committed candidate full suite before the live probe:

- 1,557 tests, 0 failures in 178.468 seconds, exit 0.

## Live-only bug and RED-first repair

The first live command reached the delivery scoreboard before probe mail and
raised:

`TypeError: unsupported operand type(s) for -: 'str' and 'datetime.datetime'`

Root cause: `Doctor.artifact` supplied its RFC3339 string helper to
`DeliveryHealthAnalyzer`, whose declared and tested clock contract is an aware
`datetime`. The analyzer unit tests injected a `datetime` directly and therefore
did not exercise the Doctor boundary.

The integration test
`test_doctor_artifact_supplies_a_datetime_to_live_delivery_health` was added
first and observed failing with the exact live traceback. `Doctor._utc_now`
was then narrowed to return an aware UTC `datetime`. The merged doctor bank
passed 30 tests after the repair.

Final committed full suite:

- `python3 -m unittest discover`
- Result: 1,558 tests, 0 failures in 177.055 seconds, exit 0.
- Direct manifest verification remained `[]` after the final mechanical
  regeneration.

## Live probe readout

Command:

`python3 -m floati doctor --root ~/.floati-bus/puddle-fleet --source ~/Projects/floati --ref HEAD --probe --probe-budget 2`

The ordinary sandbox invocation was refused as `root_read_only` before probe
append. The authorized identical retry completed with exit 35 and a degraded
artifact. That is live fleet evidence, not a source-test failure.

Probe roster:

- PASS in one tick: `lane-puddle`.
- DEAF at the two-second budget: `alice`, `alice-city`, `alice-necro`,
  `floati-observer`, `floati-witness`, `grok`, `lane-floati`,
  `lane-puddle-crossconnection`, `lane-puddle-menubar`,
  `lane-puddle-plumbing`, and `puddle-floati-architect`.

`lane-floati` was in this long-running integration turn and had not reached a
Stop boundary during its two-second probe window. Its installed P0 waiter was
already independently live-proven by the earlier 23.541-second wake receipt;
the probe correctly reports only what occurred inside this command's budget.

Delivery scoreboard RED nodes:

- `alice-necro`: 19 undelivered, oldest 5,550 minutes.
- `floati-witness`: 23 undelivered, oldest 5,827 minutes.
- `lane-puddle-menubar`: 2 undelivered, oldest 11,416 minutes.
- `lane-puddle-plumbing`: 2 undelivered, oldest 9,939 minutes, no drain on
  record.
- `puddle-floati-architect`: 203 undelivered, oldest 5,425 minutes.

Additional live findings:

- Root valid; manifest exact; deploy currency current at
  `914ae154e117e2f333d0f30bdda5362368f554b0`.
- Wake identities were a registry-lineage subset, reported as 16/17.
- Registry/live-presence directories mismatched: 12 active nodes and no live
  presence directories.
- Installer-shadow remained `cannot_speak` because no install destination was
  supplied to this probe command.

The probe appended only its own loopback envelopes. It did not drain or
acknowledge another node's mail.

## Fences

- No flip, publication, release, or owner-tier action occurred.
- No foreign-bus artifact was touched.
- No OpenCode restart occurred.
- No README edit was made by the integrator.
- This evidence copy remains DRAFT-stamped for Fable's gate.

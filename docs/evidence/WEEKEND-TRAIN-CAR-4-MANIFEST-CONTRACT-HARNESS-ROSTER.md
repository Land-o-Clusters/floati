# Weekend train Car 4 — manifest contract and harness roster

Status: **DRAFT — INTEGRATOR EVIDENCE, the architect RESTAMP PENDING**

Date: 2026-08-27

## Identity

- Node: `build lane`
- Branch: `integrate/weekend-20260828`
- Ratified branch-cut base: `932e377e9b88d801dfd545e1c238c50af5ec58ba`
- Car source ref: `origin/u2/manifest-contract`
- Car source tip: `2befe967101ba005c2d6fa8edcaf968d1a49e332`
- Car source/common base: `27dffbe586637c72c841d23cc8688c51540d4d8a`
- Car merge commit: `77cfd7c87018b6b3d41211afaa670dc84da5a511`
- Non-isolated cancellation repair:
  `28029037adc953b482d6f60fa58cc61b05f65acf`
- Absolute-deadline repair:
  `28067ab2d9b99020b2a648b98d2601daf464d769`

## Merge and manifest sequence

This was a true two-parent merge. The sole textual conflict was
`bundle-manifest.v0.json`; the train version was retained temporarily so the
complete merged deployable inventory could be regenerated mechanically last.
`floati/cli.py` merged automatically.

The initial focused Car 4 bank passed 98 tests in 1.589 seconds under the
required host access. Its sandbox-only attempt had 96 passes and two
`PermissionError` errors because the uninstall contract deliberately writes
the user-visible tombstone at `~/floati-uninstalled-<ts>.json`.

The pre-manifest fail-fast full-suite run reached test 759 and failed only on
the expected stale `floati/cli.py` digest. The manifest was then regenerated
from the complete sorted deployable path set with SHA-256 digests.

Post-regeneration gates:

- Manifest bank: 24 tests, 0 failures.
- Direct generated-tree foreign-identity scrub: `[]`.
- Final manifest SHA-256:
  `e7240e72b2fe8f132cd74dcdd1f22b9c17d60800436843b9101037c286f2b095`.

## RED-first integration repairs

### Non-isolated cancellation

The first committed-tree full runs ended with exit 143 at
`test_disable_isolation_shares_parent_group`. The roster adapter always used
`killpg` during cancellation, even when constructed with
`isolate_process_group=False`; a live child in that mode shares the unittest
runner's process group.

A mocked regression was added first and observed two unlawful calls:
SIGTERM and SIGKILL to process group 4242. The implementation now uses direct
child `terminate()` and `kill()` calls only in non-isolated mode, while the
default isolated mode retains process-group cleanup. The focused regression
and the nine-test roster parity module passed after the repair.

### Relative timeout used as an absolute deadline

After the signal repair, the full suite completed rather than terminating and
reported 11 order-dependent `deadline_exceeded` errors, all in the roster
parity module. That module passed alone. A reproducing `g-i` module-order slice
showed the failure begins once process monotonic age exceeds 30 seconds.

The RED test fixed the clock at 100 seconds and proved `drive()` passed the
relative value `30` to helpers that compare it as an absolute timestamp. The
repair now computes `min(handle.deadline, monotonic_now + bounded_timeout)`.
The formerly failing ordered slice then passed 157 tests in 31.388 seconds.

## Final committed-tree verification

Command:

`python3 -m unittest discover -s tests -p 'test_*.py'`

Result:

- 1,658 tests in 196.893 seconds.
- 0 failures and 0 errors.
- Exit 0.

The run emitted `ResourceWarning` diagnostics for roster subprocess stdout
pipes. They were warnings, not failures, and are not represented here as a
clean-resource claim.

## Fences

- No flip, publication, release, or owner-tier action occurred.
- No foreign-bus artifact was touched.
- No README edit was made by the integrator.
- The roster surfaces remain honestly unverified pending live vendor intake.
- This evidence copy remains DRAFT-stamped for the architect's gate.

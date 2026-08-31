# THE LOCKS — future wiring contract

## 1. Future seam

A future, separately authorized wiring commit may add a private `locks`
controller in `floati/cli.py`. That controller is the only intended composition
seam. It will call `PatchQueue`, `CleanupInspector`,
`ProvisioningController`, and `ReviewHandoffController`; those components must
not reach into one another or acquire a second persistence or delivery path.

The F10 package is DARK. It has no public command, launcher route, README
command, bundle entry, packaging entry, installed hook, or live transport.

## 2. Placeholder copy keys

Future visible output may resolve only these architect-owned placeholders:

- `[[locks.escalation.action_taken_not_role]]`
- `[[locks.handoff.pending]]`
- `[[locks.handoff.stopped]]`
- `[[locks.cleanup.refused]]`
- `[[locks.provisioning.refused]]`

This receipt supplies no authored surface prose. Missing keys remain absent;
they do not fall back to improvised text or internal exception detail.

## 3. Activation default

Activation defaults OFF, and OFF renders ABSENT rather than disabled-looking UI.
The permanent public-side test constant is `LOCKS_EXPECTED_WIRED = False`.
Only the future wiring commit may invert it to `True`, in the same diff that
adds and proves every intended public route. The fence is inverted, never
deleted, weakened, skipped, or converted into environment-dependent behavior.

## 4. Blast radius

The future wiring change is expected to touch exactly these public surfaces:

- `floati/cli.py`, for the private controller and command dispatch;
- `scripts/floati`, if launcher routing changes are actually necessary;
- the README command section;
- `bundle-manifest.v0.json`;
- the repository packaging manifest; and
- `tests/test_locks_dark.py`, solely to invert the fence constant and assert
  that all four surfaces become present together.

Any broader runtime, persistence, delivery, account, or release change requires
a new scoped ruling.

## 5. Re-gate receipts

Before activation, bind every receipt to one exact 40-hex Floati head and run:

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_locks_ledger tests.test_locks_cleanup tests.test_locks_queue tests.test_locks_provisioning tests.test_locks_handoffs tests.test_locks_dark
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_source_scrub tests.test_name_sweep tests.test_copy_ledger
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
PYTHONDONTWRITEBYTECODE=1 python3 -m floati.selftest
```

The repository has no `./run_tests.sh`; the full-suite receipt must explicitly
name the `unittest discover` fallback. Recompute and record blob identity for
`docs/reference/THE_LOCKS.md`: the ruled Puddle source blob and the Floati copy
must both remain `24748a97faaecda99cf520c633a65eb9192779f6`.

The delivery-path gate must use the future live transport to send a
self-addressed handoff to every supported recipient family, observe the exact
durable delivery receipt, acknowledge it, and replay the ledger to the same
delivered state. A send return, timeout, retry cap, queue depth, exception, or
silence is not delivery proof.

## 6. Estimate

Budget 10–14 hours for integration and re-gating, plus 3–4 hours for the live
self-addressed delivery-path proof. The latter is a separate activation gate,
not time hidden inside implementation or inferred from unit tests.

## 7. Deliberate omissions

This F10 slice performs no deletion, live transport, Puddle write, general
workflow orchestration, network operation, account or limit work, telemetry,
authored surface copy, installation, deployment, merge, release, or release
claim. None of those omissions may be inferred from the private package or its
tests; each needs separate authority and evidence.

# Truth Guarantees

Every promise on this page is demonstrated by a test, a receipt, or a
measurement named beside it. If a promise cannot be demonstrated, it does not
belong here — and a promise you find here without its instrument is a defect
worth reporting.

## Nothing leaves this machine

Floati sends no telemetry and phones no home. Measured (2026-08-28, source
audit of `floati/`): every socket in the product is a local AF_UNIX pipe
between floati's own processes, with one exception — the herdr adapter's
consent-gated, client-only loopback connection, which dials literal
`127.0.0.1`/`::1` on your machine, performs zero name resolution, and
contains zero `listen`/`bind` calls (the five ratified loopback conditions,
verified in code: `docs/evidence/wave2-r3-herdr-loopback-client-2026-08-27.md`).
The installed child harnesses own their normal provider traffic; floati reads
no credential and adds no traffic of its own.

## The ledger is append-only and survives faults

Kill a worker, kill the sequencer, reboot: the run reconstructs from receipts,
in order, on demand (`floati log --replay`). Demonstrated live by the
three-fault replay capture and drilled by the gauntlet: after `kill_worker`
the ledger survived with its work and receipt rows intact, and replay
reconstructed every event (`docs/evidence/gauntlet/H3-*` receipts). What
replay does NOT do is also written down: a killed step is never silently
completed by resume — reconstruction is of the record, not of the work
(`docs/evidence/gauntlet/H3c` and the gate at
`docs/evidence/gate-gauntlet-h-family-2026-08-28.md`).

## Refusal before mutation, with a named reason

Malformed envelopes, unknown identities, foreign tenants, wildcard selectors,
undeclared roots: refused with a stable machine-readable code before the
primary mutation, exit 20. The refusal vocabulary is exercised across the
test suite (1,800+ tests, `python3 -m unittest discover`); the exit-code
contract with remedies is in `AGENTS.md`.

## A receipt says exactly what happened — never more

An append is not a delivery. A delivery is not an acknowledgment. A poll is
not a wake. A wake attempt is not a successful prompt. Each of these is a
separate receipt, and floati's own diagnostics (`doctor`, the board's three
lamps) refuse to blend them into one green dot. Enforced by the delivery and
wake-receipt test banks (`tests/test_wake_control.py`, the doctor battery)
and drilled in `docs/evidence/gauntlet/H-wake-hook.md`.

## Nothing wakes without your recorded say-so

Wake machinery is opt-in, armed by a consent receipt in the ledger, off by
default. Pausing is a recorded state, not deafness: `wake pause` writes a
receipt and an exact-session marker; a paused session's mail waits, receipted,
and resuming exactly that session wakes it organically — proven with zero
synthetic rows in `docs/evidence/gauntlet/H-wake-hook.md`. Global and
wildcard selectors do not exist, by construction
(`tests/test_wake_control.py` reds if the refusal is removed).

## Your disk is not floati's territory

No home scan, no root discovery, ever: every durable verb requires an
explicit absolute root, and that directory is the blast radius. `chart` reads
only roots you declared; `survey` reports foreign buses read-only and never
writes, drains, acks, registers, or locks one
(`tests/test_foreign_bus_survey.py`, `tests/test_multi_bus_chart.py`).
Uninstall removes only unchanged manifest-owned tool files — your ledgers
and records outlive the tool (`tests/test_uninstall.py`).

## No number is invented

Floati never renders a measurement it did not take. Remaining-context is not
exposed to external probes of any supported harness's CLI surface (the E1
receipts, rescoped 2026-08-28 — in-session command surfaces exist and are
being measured separately), so floati shows no context gauge today — a typed
absence citing its measurement — rather than an estimated bar. A future gauge
may be fed only by derivation from a harness's own written records or by a
session's own stamped self-report, never by estimation.
The same law governs every surface: measured values carry their receipt;
absences say so in the sentence.

## Security claims are provenance claims

Floati does not claim to detect prompt injection — that is a classifier claim
no receipt can back. It proves provenance and flags boundary violations:
unregistered senders, foreign tenant ids, SHAs on no ref, envelopes outside
the registry grammar. Mechanical, testable, and exactly as far as the
receipts go (`docs/NORTH_STAR.md`, ruling 3).

## Every door has an exit

Wake → pause/resume. Register → retire. Install → uninstall. Orchestrate →
drain. Consent → revocation. Each reverse is receipted at the same polish as
its forward verb, and none of them destroys your records
(`docs/NORTH_STAR.md`, ruling 6; the uninstall and wake-control test banks).

---

The no-non-loopback-network property now has a standing whole-product source
fence: `tests/test_no_listener_fence.py` confines network-capable imports,
allows socket imports only in the ruled local transports, and permits
`bind`/`listen` only in the AF_UNIX sequencer. The herdr adapter's dedicated
tests separately pin literal `127.0.0.1`/`::1` targets and forbid DNS.

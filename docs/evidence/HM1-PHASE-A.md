# HM-1 Phase A evidence

Status date: 2026-07-31.

This is the authoritative repository ledger for HM-0 phase 2: shared framing,
the distinct orchestration work log, capability and approval records, and the
read-only supervisor projection. Git remains authoritative; local execution
does not establish hosted CI, deployment, activation, or a live operator UI.

## Identity

- Branch: `lane/hm0`.
- HM-1 brief input: `37d3aeb2bc21b4114ec307769c3d5681ec4c6dca`.
- Canonical framing/work implementation: `53cc66c`.
- Approval/supervision implementation: `81e4af2`.
- Evidence-binding commit: not predicted by this document.

## RED-first ledger

### Shared framing and orchestration work

RED command:

```sh
python3 -m unittest -v tests.test_framing_work tests.test_schemas
```

Observed before implementation: exit 1; 12 tests ran with five errors and
five failures. The errors were the absent `slip.framing` and `slip.work`
modules; the failures named the two absent work schemas.

Focused GREEN command:

```sh
python3 -m unittest -v tests.test_framing_work tests.test_root_jsonl tests.test_record_validation tests.test_schemas
```

Observed: exit 0; 33 tests ran; `OK`. Tests prove byte-identical canonical
framing across mail/work record shapes, separate durable ledgers, append-only
state transitions, sparse completion, and exact authority-bound claims.

### Capability, approval, and supervision

RED command:

```sh
python3 -m unittest -v tests.test_approvals tests.test_supervisor tests.test_schemas
```

Observed before implementation: exit 1; 15 tests ran with seven errors and
seven failures. The errors named the absent approval and supervisor modules;
the failures named the three absent schemas.

Focused GREEN command:

```sh
python3 -m unittest -v tests.test_approvals tests.test_supervisor tests.test_planes tests.test_schemas
```

The first GREEN attempt ran 25 tests with one failure: boolean capability mode
was correctly refused under generic `mode_invalid`, while the contract required
the distinguishable `capability_mode_invalid`. After the minimal reason-code
fix, the same command exited 0; 25 tests ran; `OK`.

The supervisor tests compare a recursive tree digest before and after the
snapshot. The digest is identical, proving the pass created no file or lock;
the snapshot also renders liveness, authority, and mutex separately and names
an expired persisted hold as a stale mutex lease.

## Complete local gate

Fresh gate commands after the manifest, design, evidence, and ruling-request
drafts were present:

```sh
python3 -m slip.selftest
python3 -m slip.conformance --live-root-smoke
python3 -c 'from pathlib import Path; from slip.scrub import scan_generated_tree; hits=scan_generated_tree(Path.cwd()); print("scrub_hits="+str(len(hits))); raise SystemExit(bool(hits))'
git diff --check
```

Observed results: selftest exit 0, 120 tests, `OK`, followed by
`{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`;
live-root smoke exit 0 with five conformant cases; scrub exit 0 with
`scrub_hits=0`; diff check exit 0 with no output.

## Boundaries

- Standard-library unit/integration tests: executed locally.
- Bundle manifest: refreshed for the exact deployable source/schema set.
- Live-root conformance smoke: executed locally; five conformant cases.
- Generated-artifact scrub: executed locally; zero hits.
- Hosted CI: unobserved.
- External deployment or activation: unobserved and unclaimed.
- Operator CLI and TUI surfaces: later phases; not claimed here.
- Fable push verdict, checkpoint notification, push, and local/origin parity:
  pending.

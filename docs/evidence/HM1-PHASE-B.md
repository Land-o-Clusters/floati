# HM-1 Phase B evidence

Status date: 2026-07-31.

This ledger records the operator-grade CLI workflow checkpoint. It does not
claim the harbor-board TUI, hosted CI, deployment, or activation.

## Identity

- Branch: `lane/hm0`.
- Phase A evidence tip: `4a2c7902da1839d11684191e70ec1a96f3b7565c`.
- Phase B implementation: `396ca1e`.
- Evidence-binding commit: not predicted by this document.

## RED-first ledger

The Phase B RED command was:

```sh
python3 -m unittest -v tests.test_cli tests.test_cli_workflows tests.test_projection tests.test_watch tests.test_copy_ledger tests.test_record_validation tests.test_registry_events tests.test_schemas
```

Observed before implementation: exit 1; 48 tests ran with 24 failures and six
errors. Missing surfaces included reply binding, the shared projection,
status/watch/receipts/work/supervise parsers, static help, and the generated
copy ledger.

After implementation, one focused idempotency test initially reached the
`reply_to_unknown` guard before the intended conflict branch. Its fixture was
corrected to use a real reversed-party message, making the test exercise the
idempotency behavior rather than malformed input. The focused workflow command
then exited 0 with 47 tests and `OK`.

The copy generator's first real module invocation exposed a two-module-instance
bug: `python3 -m <retired>.copy` printed an empty table although imported generation
worked. A subprocess regression failed against the empty output, the canonical
module entry was fixed, and that regression then passed. The generated
`docs/COPY-LEDGER.md` exactly matches the catalog.

Final focused command:

```sh
python3 -m unittest -v tests.test_cli tests.test_cli_workflows tests.test_projection tests.test_watch tests.test_copy_ledger tests.test_framing_work tests.test_supervisor tests.test_registry_events tests.test_record_validation tests.test_schemas
```

Observed: exit 0; 56 tests ran; `OK`.

## Complete local gate

Fresh gate commands after the manifest, design, README, evidence, and ruling
request were present:

```sh
python3 -m <retired>.selftest
python3 -m <retired>.conformance --live-root-smoke
python3 -c 'from pathlib import Path; from <retired>.scrub import scan_generated_tree; hits=scan_generated_tree(Path.cwd()); print("scrub_hits="+str(len(hits))); raise SystemExit(bool(hits))'
python3 -m unittest -v tests.test_copy_ledger
git diff --check
```

Observed: selftest exit 0, 132 tests, `OK`, then `bundle_verified` naming
`refs/heads/lane/hm0`; smoke exit 0 with five conformant cases; scrub exit 0
with zero hits; copy-ledger focused check exit 0 with two tests; diff check
exit 0 with no output.

## Boundaries

- Operator workflows and static help: locally executed.
- Copy ledger: generated and equality-tested; the architect voice approval pending.
- Legacy on-disk message compatibility: covered by mixed legacy/reply rows.
- Phase A notification: `msg-019fb9bea8187bb3a977b6346aeca474` sent;
  the architect response remains unobserved at this checkpoint.
- Hosted CI, deployment, activation, and TUI live polish: unobserved.
- the architect Phase B verdict, push, and local/origin parity: pending.

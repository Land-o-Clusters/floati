# WS-B3 node wizard wiring

Status: **DRAFT - dark until the integration train supplies the existing-verb commit adapter**

## Wizard delivered

`floati/node_wizard.py` is a deterministic interaction and preview state
machine for `node add` and `node retire`. Keyboard answers and the faithful
plain-input fallback feed the same validation and plan builder. Node ids use the
existing grammar; harnesses use the existing role validator.

Permanent onboarding previews one exact registry row. Temporary onboarding
also previews an exact lease row, always selects the B2 nested workspace, and
returns one-line boot and teardown commands bound to that lease. Retirement
reads the existing active role, previews the exact retirement rows, and states
that the workspace remains.

Every exact JSON record is written and flushed to the output stream before the
backend commit method is called. The wizard does not open, append, lock, or
otherwise mutate a ledger itself.

## Integration seam and refusal boundary

The current core `Registry.register` and `Registry.retire` methods create their
record id and timestamp inside the append. Calling those APIs today cannot
truthfully promise the exact future row before mutation. This lane does not
duplicate their transaction code or patch process-global factories.

Before activation, the train must provide a `NodeMutationBackend` adapter that:

1. commits the already-previewed registry row through the existing registry
   verb's single transaction path;
2. persists the previewed lease row through one ruled lease ledger;
3. composes B2 workspace creation/retention with those same verbs;
4. binds `floati node add`, `retire`, `boot`, and `teardown` to the wizard;
5. feeds Harbor Board key events to `add_from_keys`/`retire_from_keys` and uses
   `add_plain`/`retire_plain` when the interactive surface is unavailable;
6. regenerates static help and the bundle manifest after reconciliation.

Until that adapter exists, absence is safer than a wizard that prints a guess
or creates a second mutation path.

## RED-first evidence

- Initial focused run failed at import with `ModuleNotFoundError: No module named
  'floati.node_wizard'`.
- The first implementation exposed a plain-output boundary defect: the first
  preview joined the lease prompt rather than occupying its own flushed line.
  The focused suite failed before commit; the plain adapter now terminates the
  prompt line before preview.
- Focused gate: `python3 -m unittest -v tests.test_node_wizard` (5 tests).

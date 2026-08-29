# WS-B6 provider switch wiring

Status: **DRAFT - dark until the integration train supplies the registry commit adapter**

## Surface delivered

`floati/provider_switch.py` provides a keyboard-first and faithful plain-input
wizard for reassigning one active node to a new harness/model pair. Both entry
paths feed the same validation and plan builder. They accept exactly three
values: node id, harness, and model. There is no credential input, environment
read, process launch, or network path.

The wizard requires active assignment evidence from the same fleet tenant. It
refuses malformed, retired, cross-tenant, unchanged, terminal-unsafe, and
ambiguous input before preview or commit. Model coordinates use a bounded ASCII
grammar. Record identifiers must be lowercase UUIDv7 values without hyphens.

## Registry row and receipt

A successful plan contains exactly two durable rows:

1. an active `registry_entry` retaining the node id and recording the new
   harness in the registry's existing `role` field; and
2. a `provider_switch_receipt` binding the previous registry entry id, the
   previous and replacement harness/model values, and the new registry entry
   id.

Both exact JSON rows are written and flushed as `DRAFT - ledger preview`
output before `ProviderSwitchBackend.commit_switch` is called. The wizard never
opens, appends, or locks a ledger. The receipt contract is published at
`schemas/v0/provider-switch-receipt.schema.json`.

## Integration seam

Before activation, the train must provide one `ProviderSwitchBackend` adapter
that:

1. projects the active registry row together with the latest model assignment;
2. commits the already-previewed active registry row through the registry's
   single reassignment transaction path;
3. appends the already-previewed receipt in the same ruled mutation boundary;
4. adds `provider_switch_receipt` to the durable validator and manifest;
5. binds the Harbor Board provider-switch action to `switch_from_keys`, with
   `switch_plain` as its plain fallback; and
6. regenerates static help and the bundle manifest after reconciliation.

Until that adapter exists, the surface remains dark rather than creating a
second registry writer or claiming a receipt that was not durably appended.

## RED-first evidence

- The first focused run failed at import with `ModuleNotFoundError: No module
  named 'floati.provider_switch'`.
- After the module existed, the focused run remained RED because the versioned
  receipt schema was absent.
- A separate RED proved that arbitrary lowercase hex ids could reach preview;
  ids are now constrained to the durable registry's UUIDv7 shape.
- A separate RED proved that non-registry assignment projections and invalid
  registry ids could reach record construction; the wizard now requires the
  v0 registry kind and durable id shape.
- A legacy-row RED proved that nodes registered before model assignments exist
  can still receive their first model with a typed null predecessor.
- Focused gate: `python3 -m unittest -v tests.test_provider_switch` (9 tests).

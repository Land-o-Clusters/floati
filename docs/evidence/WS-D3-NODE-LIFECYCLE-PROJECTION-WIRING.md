# DRAFT - WS-D3 node lifecycle projection wiring

**Subject:** additive `lane/ws-b-admin` work for WS-D D3, to be sent at the exact
post-scrub branch head.

## DRAFT - Scope and boundary

This row adds `floati/node_projections.py`, the strict v0 projection schema, and
the focused contract tests. It does not edit the registry, records, CLI, help,
manifest, existing lane modules, or any train-owned adapter.

`NodeBootProjection` and `NodeTeardownProjection` retain only the validated root,
node id, source seam, and copied D1 template catalog. They accept no `fleet_map`
constructor argument. Each `project()` invocation reads the active node, active
fleet, declared roots, role record, wake posture, and managed-bus shape from the
source, then validates and copies the result. A changed architect or retired
sibling is therefore reflected by the next projection; no cached topology is a
construction path.

## DRAFT - Projection contract

The boot artifact includes the current identity, harness, B2 workspace and
`STATE.md` paths, fleet map, role/template provenance, interview answers, wake
posture, exact managed-bus shapes, a single-line DRAFT command, and a DRAFT
prompt. The prompt starts by reading `STATE.md` and renders every role stop and
fence verbatim.

The managed shape is typed and immutable. Its send vector is exactly
`send --to --sha --doc --idempotency-key --note`, with `--reply-to` as the only
optional flag; inbox is `inbox`, and acknowledgement is `ack --id`. Missing or
mismatched harness shapes refuse instead of falling back to a guessed command.

The teardown artifact reuses the same live context and emits this fixed ritual:

1. DRAFT - read `STATE.md`;
2. DRAFT - flush state to `STATE.md`;
3. DRAFT - check committed-versus-banked work;
4. DRAFT - push and envelope unbanked work using the exact send shape;
5. DRAFT - report `DRAINED` after intentional inbox silence;
6. DRAFT - close the lease through the train-owned adapter, if present; and
7. DRAFT - retire mechanically while retaining, never deleting, the workspace.

The projector never opens or interprets `STATE.md`, launches a harness, mutates
a ledger, calls a model, uses the network, or creates a state file. JSON is
deterministic and the prose board is ASCII-safe; all generated operational copy
is DRAFT-stamped.

## DRAFT - RED-first and verification evidence

The mandatory first RED was observed before the production module existed:
`python3 -m unittest -v tests.test_node_projections` failed at import with
`ModuleNotFoundError: No module named 'floati.node_projections'`.

After implementation, the same focused suite is green: **8/8 OK**. It covers
stale-map construction refusal and mutation-after-construction freshness, exact
managed flags, role/template provenance, verbatim stop/fence copy, wake refusal,
teardown order and workspace retention, deterministic ASCII output, read-only
source/state behavior, and strict schema rejection of an extra derived field.

The named D1/D2/B2/B4/B6 regression slice is also green: **49/49 OK** across
`test_node_projections`, `test_role_templates`, `test_role_assignment`,
`test_node_wizard`, `test_workspace_layout`, `test_multi_bus_chart`, and
`test_provider_switch`. Private-cache compilation of the new module and tests,
plus `git diff --check`, are green.

The repository-wide discovery run was executed but is not a pass: **1579 tests,
5 failures, and 3 errors**. The failures are the pre-existing/out-of-scope
publication checklist wording and frozen/committed manifest rebaseline gates;
the three capture errors inherit the same committed-tree `tracked_set_mismatch`,
and the run also printed host `sandbox initialization failed: Operation not
permitted` diagnostics. None was a D3 focused or named-regression failure. The
manifest/publication integration remains train-owned by this row's boundary.

The source seam is intentionally dark until the integration train supplies live
ledger reads and command registration. This row performs no CLI registration,
registry mutation, harness launch, state-file interpretation, model call, or
network action.

---
**ADDENDUM (Fable, 2026-08-29, matrix audit):** the failure this document records was
scoped or fixed after it was written — see `docs/evidence/matrix-audit-fable-2026-08-29.md`
for the re-run. The capability-matrix cells citing this receipt are supported by its
passing sections; this note exists so the headline cannot mislead a later reader.

# WS-D2 registry role wizard wiring

Status: **DRAFT - dark until the integration train supplies the registry role adapter**

## Wizard role step

`floati/role_assignment.py` adds a deterministic role interview step for the
node wizard. Keyboard answers and the faithful plain-input fallback select one
typed D1 template and collect exactly one answer for each declared question.
Missing, extra, terminal-unsafe, or undeclared answers refuse before preview or
commit. The wizard never invents a question.

Blank answers use only a default declared by the template. The special
`<architect>` default calls `RoleAssignmentBackend.current_architect` during
that invocation and requires an active same-tenant v0 registry entry whose role
is architect. An explicit answer does not read architect state.

## Registry role record

One successful interview previews exactly one `registry_role_record` containing:

- node id and active state;
- template role, integer version, and canonical SHA-256 digest;
- the complete typed answer mapping; and
- a typed null predecessor for the initial role assignment.

The exact JSON row is written and flushed as `DRAFT - ledger preview` before
`RoleAssignmentBackend.commit_role` is called. The record validates against
`schemas/v0/registry-role-record.schema.json`, which forbids additional fields.
Neither the record nor the wizard accepts a fleet map, sibling roster, boot
command, or other cached projection state.

## Integration seam

Before activation, the train must provide one `RoleAssignmentBackend` adapter
that:

1. reads the target node and current architect through the live registry;
2. commits the already-previewed role row through one registry transaction;
3. adds `registry_role_record` to the durable validator and installed manifest;
4. feeds the D1 shipped and fleet-owned template catalog into the wizard;
5. adds the role selection/interview step to `floati node add`; and
6. treats later role edits as predecessor-bound reassignments rather than
   onboarding.

Until that adapter exists, this row remains dark rather than adding a second
registry writer. It includes no model, network, credential, or harness-launch
path.

## RED-first evidence

- Initial focused run failed with `ModuleNotFoundError: No module named
  'floati.role_assignment'`.
- After the wizard existed, the focused run remained RED because the versioned
  registry role schema was absent.
- The live-architect test changes the registry evidence immediately before the
  interview and proves the new node id is projected without cached topology.
- Focused gate: `python3 -m unittest -v tests.test_role_assignment` (9 tests).

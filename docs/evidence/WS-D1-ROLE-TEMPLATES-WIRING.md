# WS-D1 role templates wiring

Status: **DRAFT - dark until the integration train registers the shipped library**

## Typed template record

`floati/role_templates.py` parses one explicit JSON file into immutable
`RoleTemplate` and `RoleQuestion` values. The v0 record has exact fields for
role, template version, duties, decision rights, stops, fences, cadence, and
declared interview questions. Unknown fields, duplicate question keys,
terminal-unsafe copy, non-JSON input, oversized files, non-regular files, and
symlinked file entries refuse before a template is returned.

The template digest is SHA-256 over canonical typed JSON rather than file byte
order. A later registry role record can therefore bind the exact semantic
template version used by its wizard interview.

## Shipped library

The DRAFT library contains exactly three plain JSON files:

- `roles/shipped/architect.json`
- `roles/shipped/builder.json`
- `roles/shipped/sre.json`

`load_shipped_role_templates` opens those exact names in stable order and does
not scan the directory. Each file validates against
`schemas/v0/role-template.schema.json`. The schema forbids additional fields at
the template and question boundaries.

## Freshness boundary

Templates contain role policy and interview questions only. They cannot contain
a fleet map, sibling roster, generated boot command, or other cached projection
state. The first mandatory RED added a stale `fleet_map` field and proved that
template construction refuses it. Live fleet context remains a D3 invocation-
time input, not template state.

## Integration seam

Before activation, the train must:

1. include the schema and three archetypes in the installed manifest;
2. register the shipped directory as read-only product content;
3. feed the typed library to the D2 wizard role step; and
4. regenerate static help and the bundle manifest after reconciliation.

No command is registered by this row, and no model, network, credential, or
harness-launch path exists.

## RED-first evidence

- Initial focused run failed with `ModuleNotFoundError: No module named
  'floati.role_templates'`.
- After the parser existed, the focused run remained RED because the shipped
  archetype files were absent.
- Focused gate: `python3 -m unittest -v tests.test_role_templates` (7 tests).

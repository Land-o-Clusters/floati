# WS-B4 declared roots and Harbor Chart wiring

Status: **DRAFT - awaiting integration-train reconciliation and Fable copy gate**

## Contract delivered

`schemas/v0/declared-roots.schema.json` defines one explicit, version-zero JSON
file. Every entry names a unique bus id, an absolute existing root, one
architect node, and zero or more downstream bus ids that must also be declared.
Relative paths, symlinked roots, duplicate ids or paths, self edges, and unknown
edge targets refuse rather than resolve or guess.

`floati/multi_bus_chart.py` reads only that file and the exact roots it names.
It validates registry and event ledger snapshots without creating locks,
projects active nodes, verifies the declared architect is active, derives the
latest activity age, and renders downstream relationships. An undeclared bus
beside a declared root is neither opened nor reported.

The compact JSON artifact is the source of the deterministic ASCII twin. The
human renderer uses ASCII bytes only.

## Integration seam

After the train lands, shared CLI reconciliation adds `floati chart` with:

- a required absolute declared-roots file option;
- `--json` for the compact artifact twin;
- ASCII rendering otherwise;
- DRAFT-stamped static help and copy pending Fable's gate;
- final bundle-manifest regeneration after the module and schema land.

The wizard may offer to select or create a declared-roots file, but it must not
search for candidate roots. B5 survey remains a separate, explicitly invoked
operation.

## RED-first evidence

- Initial focused run failed at import with `ModuleNotFoundError: No module named
  'floati.multi_bus_chart'`.
- The schema test then failed with `FileNotFoundError` before the published
  version-zero schema existed.
- Focused gate: `python3 -m unittest -v tests.test_multi_bus_chart` (6 tests).

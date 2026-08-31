# WS-B2 workspace-layout wiring

Status: **DRAFT - awaiting integration-train reconciliation and the architect copy gate**

## Dark module delivered

`floati/workspace_layout.py` composes the existing `Registry` rather than
creating another membership path. Its registration adapter validates node and
harness inputs before filesystem mutation, optionally creates the conventional
folder, and rolls back an empty folder if the registry refuses. Its retirement
adapter appends through `Registry.retire` and only reports workspace state.

The read-only inspection function produces deterministic missing, invalid, and
orphan findings without creating a lock or workspace path.

## Integration seam

The train owns the shared parser and doctor aggregation. Reconciliation adds:

1. `--create-workspace` to `register`, dispatching registration through
   `workspace_layout.register_node`;
2. retirement dispatch through `workspace_layout.retire_node` so the artifact
   includes the retained workspace state;
3. `workspace_layout.inspect_workspace_layout(root)` findings to doctor;
4. the new runtime and help surfaces to the final regenerated bundle manifest.

No existing registry, doctor, parser, help, or manifest file is edited on this
lane during Phase 0.

## RED-first evidence

The initial focused run failed at import with `ModuleNotFoundError: No module
named 'floati.workspace_layout'`. The focused suite then proved nested-only
creation, retirement retention, pre-mutation lexical and collision refusals,
and deterministic read-only doctor awareness.

Focused gate: `python3 -m unittest -v tests.test_workspace_layout`.

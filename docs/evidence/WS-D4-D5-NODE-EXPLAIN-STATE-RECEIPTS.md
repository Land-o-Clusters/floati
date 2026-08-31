# DRAFT - WS-D4/D5 node explanation and state-file receipts

**Subject:** additive `lane/ws-b-admin` work, to be sent at the exact
post-scrub branch head.

## DRAFT - Scope and boundary

This paired row adds `floati/node_explain.py`, `floati/state_receipts.py`, the
state-receipt schema, focused tests, and this evidence record. It does not edit
the D3 projector, registry, records, CLI, help, manifest, shared core, or any
train-owned adapter.

## DRAFT - D4 explanation contract

`NodeExplainProjection` wraps a `NodeBootProjection` and delegates both
`project()` and `to_json()` to the live D3 projector. Each call reads the source
again; no fleet map, role record, wake state, managed-bus shape, or prompt is
cached. `from_boot` is the direct wrapper seam, and the JSON twin is byte-for-
byte the current D3 JSON serialization.

`render_node_explanation` validates the D3 boot shape and renders identity,
workspace, state-file location, current architect, fleet nodes, declared roots,
role provenance, duties, rights, stops, fences, cadence, interview answers,
wake posture, exact managed-bus verbs, command, and prompt. It answers what the
node is and why its role is assigned, emits DRAFT-labelled ASCII, and never
opens or interprets `STATE.md`.

## DRAFT - D5 receipt contract

`StateFileFlushReceipt` derives exactly
`<FloatiRoot>/nodes/<node-id>/STATE.md`. It rejects invalid identifiers,
missing or symlinked path components, non-directory parents, missing vessels,
symlinked vessels, and non-regular files. It opens root, `nodes`, and the node
workspace through no-follow descriptors, then uses `fstat` on the state vessel.
It does not call a content read, parse the file, change its bytes, or append to a
ledger. The returned receipt is the train-owned flush-boundary record with its
UUIDv7 ID, tenant and node identity, canonical vessel path, flush operation,
timestamp, observed mtime, observed byte size, and optional prior mtime. A prior
mtime must be strictly older than the observed mtime.

JSON and prose receipt renderers expose metadata only and retain the DRAFT copy
fence. Persistence remains an adapter concern so this row cannot create a
second lifecycle mutation path.

## DRAFT - RED-first evidence

Before either production module existed, the new focused command failed at
import with both expected missing-module errors:

```text
python3 -m unittest -v tests.test_node_explain tests.test_state_receipts
ModuleNotFoundError: No module named 'floati.node_explain'
ModuleNotFoundError: No module named 'floati.state_receipts'
```

After implementation, the focused D4/D5 suite is **10/10 OK**. It covers live
reprojection, exact JSON identity, prose completeness and ASCII output,
stale-map construction refusal, read-only behavior, schema validation, mtime
advancement, missing/symlink/non-regular refusal, parent-symlink refusal,
opaque bytes without `os.read`, and metadata-only rendering.

The named D3 regression plus this row is **59/59 OK**:

```text
python3 -m unittest -q tests.test_node_explain tests.test_state_receipts tests.test_node_projections tests.test_role_templates tests.test_role_assignment tests.test_node_wizard tests.test_workspace_layout tests.test_multi_bus_chart tests.test_provider_switch
----------------------------------------------------------------------
Ran 59 tests in 0.066s

OK
```

Private-cache compilation and `git diff --check` are green:

```text
PYTHONPYCACHEPREFIX=/tmp/puddle-lane-d4d5-pycache python3 -m py_compile floati/node_explain.py floati/state_receipts.py tests/test_node_explain.py tests/test_state_receipts.py
git diff --check
```

## DRAFT - Full discovery boundary

The full discovery run is not a pass: **1,589 tests, 5 failures, and 3
errors**. The three errors are the pre-existing committed-tree install
`deployment_manifest_invalid` / `tracked_set_mismatch` failures in demo-capture
tests. The five failures are the known publication-checklist wording,
publication manifest rebaseline, frozen protocol JSON rebaseline, and living
documentation sweep failures. The run also printed the known host diagnostic
`sandbox initialization failed: Operation not permitted`. The added D4/D5
tests and the named D3 slice are green; no full-battery failure is attributed to
this row.

## DRAFT - Scrub evidence

Before commit, the required source-tree and Git-history-note scans both returned
`[]`. They must be rerun after commit at the exact branch head and before the
governed push; a non-empty result blocks banking this row.

```text
python3 -c 'from pathlib import Path; from floati.scrub import scan_generated_tree; hits=scan_generated_tree(Path.cwd()); print(hits); raise SystemExit(bool(hits))'
[]
python3 -c 'from pathlib import Path; from floati.scrub import scan_git_history_notes; hits=scan_git_history_notes(Path.cwd()); print(hits); raise SystemExit(bool(hits))'
[]
```

The branch remains dark and additive: no command registration, harness launch,
network action, state-file content interpretation, incumbent-bus artifact, or
shared-core mutation is part of this row.

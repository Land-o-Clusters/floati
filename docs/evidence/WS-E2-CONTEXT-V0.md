# WS-E E2 — context v0 honesty boundary

Status: **DRAFT - access-class amendment awaiting replacement exact-head review**

Initial base snapshot: `981636970d7f5834ad2b7f0c2514675b072b7038`
Branch: `lane/ws-e-context`
Architect rulings: `msg-01a0463950f2750fa13ea9e49dfa7543`,
`msg-01a04673ee327cacba8d813826402cb2`, and
`msg-01a0468762cc7141a71850e67cbc286d`

## Scope

This row adds only new files. `floati/context_absences_v0.py` carries the
shipped, versioned, serialized and self-enumerating E1 dataset for the eight
measured harnesses. The Python carrier is deliberate: the landed deployable
inventory includes `floati/**/*.py` but excludes adjacent `floati/*.json`
data. Every current row is explicitly Class A (external/programmatic) and says
`not exposed to external probes`; no Class B testimony or Class C conclusion is
invented. `floati/context_absences.py` strictly parses it and refuses malformed
citations, numeric `not_exposed` row fields, enumeration drift, and unknown
harnesses. `floati/context.py` projects cited status and the ordered turnover
recipe. The recipe prints the exact landed D3 teardown, D5 state-flush receipt,
and D3 boot argv shapes with typed operator-supplied inputs; it never executes
them or creates substitute persistence.

The shared CLI, static help registry, manifest, frozen protocol baseline, and
existing code/tests remain untouched. `register_cli(commands)` is the dark
activation seam. Its help strings remain `DRAFT -` stamped for the architect restamp.

## E1 evidence derivation

The E1 source row is
`docs/evidence/conformance/E1-context-capability-inventory.md` at
`2601b4a9ebc5d9c388c02c4e6b22700097daeb6e` on
`origin/lane/grok-context-e1`. It names the eight live harnesses: claude,
cline, codex, cursor, grok-build, herdr, opencode, and pi.

Every absence cites
`docs/evidence/conformance/E1-cli-help-probe.json` with SHA-256
`e653035faa98c67c1d7e2407603eecae3932f18bea429b1dfe6061f6e8715f93`.
That digest was recomputed directly from the remote branch object before the
dataset was written; it matches E1's own receipt table.

The owner-ordered scope correction is
`docs/design/tide-tables-spec-2026-08-28.md` at
`e001a6fed34b875083d119c335fc2798944aa752`, delivered initially as
`msg-01a04659587b73e3af2922d40e46e8e3`. E1 measured the external probe
surface correctly but did not measure in-session slash commands. This v0 row
therefore preserves only the Class A result and names that boundary directly.

## RED-first evidence

Before any production module, dataset, or schema existed,
`python3 -m unittest -v tests.test_context` ran all nine initial tests and
failed 9/9 for the intended missing `floati.context` and
`floati.context_absences` modules. The run already contained all four required
fences: no rendered measurement decoration/numeric absence fields; mandatory
path+SHA citation; unknown-harness refusal; and physically read-only turnover.
It also contained the eight-harness, schema, renderer-twin, provenance, and
CLI-seam acceptance tests.

Two later self-review RED/GREEN rounds tightened the direct-render boundary:

- Missing status receipt SHA produced the wrong refusal code, and a mutated
  D5 argv rendered without refusal: 2 failures. Both now refuse.
- A shallow state path raised `IndexError`, and malformed role provenance
  rendered without refusal: one error plus one failure. Both now produce typed
  `context_output_invalid` refusals.
- The higher-reasoning review then exposed ten trust-boundary failures across
  four tests: three caller-selected status citations rendered, a caller-selected
  dataset was accepted, three invented D1 provenance variants rendered, and
  three malformed D2 role-record variants projected. A compatibility RED also
  showed E2 rejecting a valid Unicode D2 answer that is never rendered. Status
  now loads only the shipped dataset, detached renderers bind evidence and role
  provenance back to shipped records, complete D2 metadata is validated, and
  unrendered D2 answers retain the landed D3 text contract.
- After the owner access-class correction arrived, the unchanged candidate ran
  22 focused tests with one failure plus nine errors: the missing class label
  used the generic refusal, typed rows had no `access_class`, and all eight
  status cases omitted it. The replacement now requires Class A in every row
  and projection and renders `not exposed to external probes`.
- The architect's final access-class re-round required a dataset-ID bump and a
  closed `A|B|C` schema vocabulary while retaining only measured Class A rows.
  The tightened 25-test bank ran RED with exactly two failures: shipped ID
  `e1-context-absence-v0` instead of `e1-context-absence-v1`, and schema shape
  `{"const": "A"}` instead of the closed enum. The parser and renderer already
  refused non-A artifacts; direct parser-forged, renderer-missing, and all-eight
  JSON/ASCII twin coverage was added without widening those E2 render paths.

## GREEN and regression evidence

- `tests.test_context`: **25 tests, OK**. The table-driven status and
  JSON/ASCII twin cases cover all eight E1 harnesses.
- D3/D4/D5 bank (`test_node_projections`, `test_node_explain`,
  `test_state_receipts`): **18 tests, OK**.
- CLI bank (`test_admin_cli`, `test_cli`): **43 tests, OK**.
- Schema bank (`test_schemas`): **42 tests, OK**.
- Scrub/name bank (`test_source_scrub`, `test_name_sweep`): **21 tests, OK**
  after the architect rewrote the affected `origin/main` tip to
  `ad62e46a7f039e6e1ee4c88bc2c890162d663715`.
- Private-cache `py_compile` for all three modules and the test: **exit 0**.
- `git diff --check`: **exit 0**.
- Direct manifest verifier: `['tracked_set_mismatch']`.

Full `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover`: **1,851 tests,
three failures plus three
errors**. The failures are exactly:

1. `test_frozen_protocol_json_bytes_match_the_floati_rebaseline`;
2. `test_frozen_protocol_json_inventory_matches_the_floati_rebaseline`; and
3. `test_repository_manifest_matches_current_deployable_tree`.

The errors are exactly:

1. `test_build_candidates_writes_four_hashed_animated_gifs`;
2. `test_lit_buoy_lamp_survives_as_ruled_yellow_pixels`; and
3. `test_text_frames_use_real_product_renderers_and_safe_provenance`.

The first two failures account for the two new v0 schemas (frozen JSON count
133 to 135). The manifest failure is the deliberate new-files-only manifest
seam; all three demo-capture errors are downstream of the committed-tree
installer refusing that same `tracked_set_mismatch`. No E2 behavior test
failed. The full gate remains RED until the integration train activates the
module, regenerates the deployable manifest, and rebaselines the frozen schema
inventory.

The full run also printed existing sandbox-init refusals, argparse refusal
text from negative CLI tests, and existing ResourceWarnings from roster
subprocess fixtures. They did not add test failures.

## Perturbation evidence

Each mutation was applied alone, observed RED, and restored. Final SHA-256
values matched the pre-perturbation values exactly.

- Added a forbidden measurement token to `floati/context.py` -> the source
  fence failed 1/1.
- Removed one serialized dataset receipt SHA -> all eight harness subcases refused with
  `context_absence_citation_missing`.
- Replaced unknown-harness refusal with a generic first row -> the unknown
  harness fence failed 1/1.
- Added a root marker write inside turnover -> the physical root snapshot
  fence failed 1/1 and exposed the exact added path/bytes.

Restored hashes:

- `floati/context.py`:
  `8fbaf6e21f574ff7cfb1fef69b42d0057ffa40535c97d47e06f26d3015bf4020`
- `floati/context_absences.py`:
  `2d04e2f21d419a643f8f0946b3ff7a7a85878eba58beb30d94ab5c3b3354aec3`
- `floati/context_absences_v0.py`:
  `5ee37ab5189446961eae1f70d559d2078d6968389acc617d06e2a86dce90bd94`
- `tests/test_context.py`:
  `50ce81786bb621aa31da399497e7bf2a922b42f301ea7aef8e006d1d3432b5b4`

## Exact-head review

The independent review of initial head
`9950c2e3c5e5cc8f9d6e19d4113b8cb5830c7beb` returned **Not ready** with no
Critical findings, four Important findings, and two Minor findings. The
Important findings were caller-selectable status evidence, detached invented
D1 provenance, incomplete live D2 validation, and a dataset path excluded from
the deployable inventory. The Minor findings were an open status-message schema
and missing repeat-call freshness coverage. The first replacement-head review
returned **With fixes**: it confirmed five findings closed, then showed that a
syntactically valid foreign D2 record ID could still render and that the
physical test still used only an injected source. A further RED now binds
detached turnover rendering to the live D2 ledger, and a real registry-backed
root proves the default status and turnover adapters read without changing any
root path, bytes, inode, size, or mtime. The exact review of
`c30f999a7a94bef0f455255ee7e777071a9234ce` then returned **Ready to
integrate** with no Critical, Important, or Minor findings; focused E2 was
21/21, `git diff --check` passed, and the reviewer independently confirmed the
documented manifest and pre-existing `roles/shipped` activation seams.
That verdict predates the access-class correction; the amended candidate must
receive a replacement exact-head review before its correction envelope.

The review of access-class head
`a42a879774e3c42f1999015d323d2de7e5da6579` returned **With fixes** with no
Critical findings, two Important findings, and one Minor test-coverage finding.
It identified the missing dataset-ID bump, const-A schema vocabulary, stale
exact-head scrub accounting, and the unpinned parser-forged/renderer-missing
matrix plus all-eight twin cases. Head
`220c0c95dae56013a7674864d61d30edb44c4ed2` closed each item. Its independent
replacement review returned **Ready** with no Critical, Important, or Minor
findings; the reviewer independently reran focused E2 at 25/25 and scrub/name
at 21/21, confirmed the clean checkout and `git diff --check`, and verified the
v1 dataset ID, closed `A|B|C` schema with A-only runtime, eight twins, stale-read
fence, source hashes, and new-files-only scope.

## Integration seam

The integration train must call `register_cli`, add/restamp static context
help, add the new modules/data/schemas to the exact deployable manifest,
rebaseline the frozen protocol JSON inventory, and expand the bundle policy
for the pre-existing `roles/shipped` D1 assets consumed by landed D3 and this
turnover projection. Until those steps land and the full suite is rerun on a
frozen tree, this row is a pushed dark candidate, not an activated capability.

## the architect GATE VERDICT: PASS AND MERGED (2026-08-28)

Access-class correction verified in code (scoped rendered sentence with its
derivation pin, closed A|B|C enum, dataset v1, 8 access_class fields).
Seams closed at merge: manifest regenerated, frozen-protocol pin
rebaselined 136→138 for the two reviewed context schemas. **Full suite at
the landing tip: 1,893 tests, OK, exit 0 (pipestatus-captured), frozen
tree** — 1,868 + 25 context tests, the count derives. The lane's three
"downstream demo errors" did not reproduce here — seat-environmental, not
findings. The two-round shape (delivery → rescope crossing mid-flight →
tight correction) is on record as the system absorbing a moving ruling
without churn. Help copy stays DRAFT for restamp wave 2; activation of the
context verbs rides the normal seam. E2 IS COMPLETE; the gauntlet context
family is UNBLOCKED.

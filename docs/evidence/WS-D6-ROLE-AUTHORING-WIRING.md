# WS-D D6 — role authoring and archetype wiring

Status: **DRAFT - independently reviewed dark candidate**

Base: `ad62e46a7f039e6e1ee4c88bc2c890162d663715` (`origin/main`)
Branch: `lane/ws-b-admin`
Architect dispatch: `msg-01a046268dbd79dfbbf6a32cd71ae386`
Copy authority: `docs/design/d6-archetype-copy-pack-2026-08-28.md`

## Scope

D6 adds the voice-passed `github-manager`, `reviewer`, and `researcher`
version-1 templates to the exact shipped role roster and lands the authoring
guide section verbatim as `docs/design/ROLE-AUTHORING.md`. The JSON copy is
unstamped, schema-v0 valid, and contains no inferred defaults. The loader still
opens only its explicit roster and never scans the directory.

The only existing-file production edit is the exact roster constant in
`floati/role_templates.py`. The matching shipped-roster and public role-list
assertions are updated because the architect dispatch explicitly requires the
six-role pin. The three templates, guide, D6 copy test, and this evidence file
are new.

## RED-first evidence

Before any D6 production file or roster change,
`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_role_templates tests.test_role_templates_d6`
ran 10 tests and failed with seven intended contract failures:

- the loader and both exact-roster pins still named only
  `architect|builder|sre`;
- `github-manager.json`, `reviewer.json`, and `researcher.json` were absent;
- `docs/design/ROLE-AUTHORING.md` was absent.

The first harness draft reported the four absent files as raw
`FileNotFoundError`s. Test-only assertions were tightened and rerun before
production so every missing artifact appeared as an explicit contract failure.

After the minimal D6 GREEN, the adjacent public role-list test ran RED once:
`tests.test_admin_cli` had one failure because its old output oracle still
named three roles. Its expected public list was updated to the same six-name
roster and the bank reran GREEN.

## GREEN and regression evidence

- D6 focused (`test_role_templates`, `test_role_templates_d6`): **10 tests,
  OK**.
- Role/wizard bank (`test_role_templates`, `test_role_templates_d6`,
  `test_role_assignment`, `test_node_wizard`): **24 tests, OK**.
- D3/D4/D5 projection bank (`test_node_projections`, `test_node_explain`,
  `test_state_receipts`): **18 tests, OK**.
- Admin/CLI bank (`test_admin_registry`, `test_admin_cli`, `test_cli`): **47
  tests, OK**.
- Schema bank (`test_schemas`): **43 tests, OK**.
- Scrub/name bank (`test_source_scrub`, `test_name_sweep`): **21 tests, OK**.

The direct manifest check at committed candidate
`ede3b4e` reports `digest_mismatch:floati/role_templates.py`; the new role
files are not yet deployable because the ruled activation row owns expansion
of the bundle policy. `git diff --check` exits 0.

Full `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover`: **1,871 tests,
two failures plus three errors**. The failures are:

1. `test_repository_manifest_includes_current_approval_suspension_runtime`
   for `floati/role_templates.py`; and
2. `test_repository_manifest_matches_current_deployable_tree` with the same
   digest mismatch.

The errors are the three demo-capture tests whose committed-tree installer
refuses that manifest mismatch:

1. `test_build_candidates_writes_four_hashed_animated_gifs`;
2. `test_lit_buoy_lamp_survives_as_ruled_yellow_pixels`; and
3. `test_text_frames_use_real_product_renderers_and_safe_provenance`.

No D6 role, copy, schema, projection, wizard, or CLI test failed. Existing
sandbox-init refusal text and roster subprocess `ResourceWarning`s did not add
test failures.

## Copy and source hashes

- `floati/role_templates.py`:
  `36edd3cd1712dd9017d2f8a42793d78e6b639031779dfac448b368d2118731ab`
- `roles/shipped/github-manager.json`:
  `a26fae5d6094e8bb75113990e7ec456b0295b577eecb5b83f9e1f26aacc9cdd7`
- `roles/shipped/researcher.json`:
  `58479ff4c42dd650c20ef99505b6aad712a454c35408665b4eacb7df823f004b`
- `roles/shipped/reviewer.json`:
  `c2cffe8df0f0f661ad6b48fb314fbbf42732cc209d5e33732fa3f93d3f452b15`
- `docs/design/ROLE-AUTHORING.md`:
  `8e99bd458429756b2c4d40641b6ef610db23358f6bd639e5ed5d269f1d7bb45d`
- `tests/test_role_templates.py`:
  `7380f7bedaa97980df1ebe23500703c4b1bd5e056275180e74f9917ba730fbfe`
- `tests/test_role_templates_d6.py`:
  `ac12057ad70c6541ef05aa668a63016a5c2f7ea6d74b5519f5ab10df831fe0c4`
- `tests/test_admin_cli.py`:
  `8fa1897772461f0c94a4210e408060740a9f9f583720d4cfdc4b7cdb35bcb2e4`

## Exact-head review

The independent review of
`495cb64d96bc53a1d461bf5a3117dfd7590eafa3` returned **Ready** with no
Critical, Important, or Minor findings. It independently confirmed the three
templates are byte-faithful to the copy pack, schema-v0/version-1 and
unstamped; the six-role public roster is exact and scan-free; the authoring
guide is byte-exact; all focused counts, scope, hashes, scrub, and
`git diff --check` reconcile; and the manifest/full-suite outcomes are an
honestly dark activation seam rather than a D6 behavior failure.

## Activation seam

D6 does not regenerate `bundle-manifest.v0.json`; the ruled activation row
owns manifest regeneration after the remaining purge repair. Until that row
lands and the frozen-tree suite is green, this is a dark shipped-library
candidate, not an activated release claim.

## FABLE GATE VERDICT: PASS AND MERGED (2026-08-28)

Re-derived: all three archetypes carry the copy pack's sentences VERBATIM
and UNSTAMPED (30/30 lines checked against the pack, zero deviations, zero
stamps — the no-DRAFT role fence would refuse anything else); the authoring
guide is line-complete (18/18); the six-role roster is pinned in both test
banks and the loader stays exact-roster. Manifest regenerated at merge.
**Full suite at the landing tip: 1,896 tests, OK, exit 0
(pipestatus-captured), frozen tree.** The lane's demo errors again did not
reproduce — consistent with seat-environmental. D6 IS COMPLETE; the shipped
role library is six archetypes.

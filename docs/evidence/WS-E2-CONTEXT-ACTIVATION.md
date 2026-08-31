# WS-E E2 — context command-family completion

Status: **DRAFT - exact-head review pending**

Final activation base: `db9824186b2d05f54e8795bc482f419c14f78950`
Branch: `lane/ws-b-admin`
Queue dispatch: `msg-01a048d6220b7da38346b0d9074bb899`
Dark capability evidence: `docs/evidence/WS-E2-CONTEXT-V0.md`

## Scope

The exact activation base already registers the E2 context family once at the
canonical `floati/cli.py` seam because the later Tide integration consumes it.
This row closes the remaining activation surfaces: complete static DRAFT help
for `status`, `turnover`, `policy`, and `reading`; matching generated
copy-ledger rows; and exact bundle inventory for the six shipped role templates
consumed by D1/D2/D3 and E2 provenance.

The final production diff is limited to `floati/helptext.py`,
`floati/manifest.py`, generated `docs/COPY-LEDGER.md`, and generated
`bundle-manifest.v0.json`. The activation test and this evidence record are new
files. No E2 projection, dataset, schema, role template, Tide, wake, purge,
administration, core CLI, or frozen protocol byte is changed.

## Copy authority

the architect wave-2 commit `583a69e` removed `DRAFT -` from the three existing
argparse labels in `floati/context.py`. None of the full static context help
pages existed in that voice pass. Under the current copy-ledger rule, those new
strings remain visibly `DRAFT -` until a later the architect restamp; the generated
ledger records all nine pages exactly.

## Mandatory RED

The first activation test was committed alone as
`2243e4b` (`test: pin E2 context activation seam`) and executed from a clean
detached worktree before any production completion byte was present:

`python3 -B -m unittest -v tests.test_context_activation`

That stale-parent run executed three test methods and failed for its intended
dark seams:
the real parser refused both context subcommands, all three static help
requests refused, and the deployable inventory omitted all six shipped role
templates. Unittest reported five failed assertions and two subtest errors;
there were no unrelated failures. This proves the historical dark predecessor,
not activation against the later Tide-bearing exact base.

After exact-range review found the reconciled topology, replacement RED commit
`d987610` added two current-base fences. The four-method activation bank then
failed eight assertions: one proved `register_cli` was called twice, one proved
the root context synopsis was incomplete, and six proved nested Tide
`policy`/`reading` help paths fell back to the two-command context page. The
duplicate-seam RED passed after removing the stale administration call; the
full-help RED passed after adding all nested static pages and routing.

A later exact-head review found the first provisional Tide examples named
metrics outside the closed catalog. Test commit `a678c44` executed the actual
policy-set and reading-record examples against an isolated registered Codex
node; both refused with `tide_metric_not_derivable`. The examples were then
corrected to the catalog's `context_fraction` and
`self_reported_context_fraction` names with percent-form fraction values.

## Reconciliation review

The overnight worktree began on stale head `0911b3a` with five modified
production/generated files and one new test. The test was preserved first.
The stale source edits were then placed in the named stash
`pre-reconcile E2 activation source backup`, current `origin/main` was merged,
and every old hunk was compared against the current Tide/wake/purge tree.

Stale hunks that would regress current Tide/wake bytes were rejected. Review
then caught and removed one more stale hunk: the administration seam duplicated
the canonical Tide-era `floati/cli.py` registration. The final row keeps one
registration point and applies only complete static help/routing plus exact
shipped-role deployable inventory. The copy ledger and bundle manifest are
generated mechanically; direct verification proves the committed manifest
exactly matches final deployable bytes.

A fresh pre-push fetch found `origin/main` had advanced from `3ad1db9` to
`db98241` with the purge copy restamp and ruled research/brief rows. That tip
was merged before publication; the only overlapping generated surface,
`bundle-manifest.v0.json`, auto-merged and was reverified against the combined
deployable tree.

## GREEN and regression evidence

- Activation/completion bank: **5 tests, OK**.
- Context projection bank: **25 tests, OK**.
- CLI/admin bank: **43 tests, OK**.
- Tide CLI/policy/reader/evaluation bank: **24 tests, OK**.
- Copy-ledger/manifest bank: **28 tests, OK**.
- Source-scrub/name-sweep bank: **21 tests, OK**.
- Direct manifest verification: `[]`, exit 0.
- Direct generated-tree scrub: `[]`, exit 0.
- Direct Git-history-note scrub: `[]`, exit 0.
- `git diff --check`: no output, exit 0.
- Full canonical `python3 -B -m unittest discover`: **1,953 tests, OK**,
  243.923 seconds, exit 0.

The real CLI test also snapshots the direct-home root before and after
`context status --json` and proves byte-for-byte physical read-only behavior.
It rejects a caller-selected turnover `--profile`, so activation does not add
a hidden transport or authority seam. The manifest test pins both the complete
E2 runtime/schema family and exactly the six shipped role dependencies.

## Publication boundary

This row is a command-family completion candidate only. It does not claim release,
installation, deployment, tenant migration, or purge activation. Purge
activation remains a separate architect-dispatched row after this E2
checkpoint.

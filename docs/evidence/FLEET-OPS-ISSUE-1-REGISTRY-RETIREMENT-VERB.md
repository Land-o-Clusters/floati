# Issue #1 — registry retirement verb: public writer + CLI

Status: **GATE REQUESTED — VERB ONLY, NOT EXERCISED ON THE LIVE ROOT**

Date: 2026-08-18

## Identity and authority

- Node: `lane-floati` (builder seat, ad-hoc fleet-ops window)
- Worktree: `~/Projects/floati`
- Branch: `lane/fleet-ops-window`
- Predecessor commit (P2, gated DONE):
  `e4ee04ed053fdac0afa384bdd232b5a332a7505c`
- Governing issue: GitHub issue #1, "Registry retirement verb: public writer
  and CLI for retiring a node"
- Dispatch authority: architect message `msg-01a01791f3557c418e4deddd54224b82`
  — "GO ISSUE #1 (retire verb) … FENCE RESTATED: do NOT exercise it against
  the live root."

## The gap this closes

`Registry.register` appended active-only rows and refused duplicates. Retired
rows could be written only by conformance-test helpers
(`floati/conformance.py::_append_retired_registry_entry`) and by the test
helper in `tests/test_registry_events.py`. There was no lawful public way to
retire a node. The read side already understood retirement: TD-5301's
`active_node_ids` folds latest-row-wins and drops retired nodes, and
`records.py` already admits `state` in `{"active", "retired"}`. This change
adds only the writer and its CLI surface; it changes no projection and no
record schema.

## What was built

`Registry.retire(node_id)` — `floati/registry.py`:

- appends one new `registry_entry` row with `state: "retired"`, under the same
  `transact` lock discipline `register` uses;
- carries the node's **registered** role forward from its latest row rather
  than accepting or inventing one;
- refuses `unknown_node` when the node has no registry row;
- refuses `registry_already_retired` when the node's latest row is retired;
- refuses `node_invalid` from the shared lexical preflight before any ledger
  directory is created.

`floati retire --root ROOT NODE` — `floati/cli.py`, `floati/helptext.py`:

- mirrors `floati register`'s exact shape: positional node, no actor option,
  no authority option, no `--as`. Self-retirement only. A controller retiring
  another node is **not** implemented, because per issue #1 that needs a
  ruling first; `--as`, `--actor`, and `--on-behalf-of` are all rejected with
  exit 20 `arguments_invalid`, and there is a test pinning that.

Append-only is preserved: the retirement row is appended and the prior active
row's bytes are untouched. A test asserts the post-write file *starts with*
the exact pre-write bytes, so a rewrite cannot pass.

## Copy ownership — nothing visible was authored by this lane

Every new visible string is an unfilled `[[placeholder.key]]` registered in
the copy catalog so it surfaces in `docs/COPY-LEDGER.md` for the architect:

| Key | Surface |
| --- | --- |
| `help.retire` | `floati retire --help`, prose slots only |
| `registry.retire.unknown_node` | refusal detail |
| `registry.retire.already_retired` | refusal detail |

`floati retire --help` currently renders `[[help.retire.name]]`,
`[[help.retire.description]]`, `[[help.retire.root]]`, and
`[[help.retire.node]]` literally. The man-page skeleton, the synopsis
`floati retire --root ROOT NODE`, and the example line are the machine command
grammar, not prose. A test asserts each of these three catalog values still
contains `[[` — if a future hand writes prose there without the architect,
that test fails.

Two copy items are therefore **outstanding for the architect** and are not
claimed as done:

1. the four `help.retire.*` strings above;
2. the root help page's `Commands:` list in `floati/helptext.py`, which does
   not name `retire`. That line is the architect's prose and was deliberately
   left unedited. No test cross-checks it against the parser, so nothing
   fails; it is reported here rather than silently changed.

## RED / GREEN evidence

Exit codes captured with `echo "EXIT:$?"` on the command directly; no pipe
stands between any command and its recorded status.

RED — `tests/test_registry_retirement.py` written first, against a tree where
neither the writer nor the verb existed:

```
python3 -m unittest tests.test_registry_retirement
Ran 14 tests — FAILED (failures=7, errors=12) — EXIT:1
```

13 of the 14 test methods failed. Causes: `AttributeError: 'Registry' object
has no attribute 'retire'` for the eight writer tests; `invalid choice:
'retire'` for the four CLI tests; missing catalog keys for the copy tests.
The fourteenth, `test_retire_offers_no_actor_override_pending_the_controller_ruling`,
passed vacuously at RED because the command did not exist at all — it is a
standing guard, not a driver, and it became meaningful only after the verb
landed. Stated plainly rather than counted as a RED signal.

GREEN:

- new tests, `python3 -m unittest tests.test_registry_retirement`:
  `Ran 14 tests`, `OK`, EXIT:0
- adjacent gates (`test_registry_events`, `test_cli`, `test_copy_ledger`,
  `test_name_sweep`, `test_source_scrub`, `test_demo_corpus`,
  `test_demo_capture_assets`): `Ran 94 tests`, `OK`, EXIT:0
- focused source-scrub/corpus/name/capture gate: `Ran 33 tests`, `OK`, EXIT:0
- complete suite, `python3 -m unittest discover`: `Ran 1503 tests`, `OK`,
  0 failures, 0 errors, 0 skips, 177.600 seconds, EXIT:0
- bundle self-test, `python3 -m floati.selftest`: `Ran 1503 tests`, `OK`,
  0 failures, 0 errors, 0 skips, 174.710 seconds, EXIT:0
- bundle artifact:
  `{"canonical_ref":"refs/heads/lane/hm0","status":"bundle_verified"}`
- `git diff --check`: EXIT:0

1503 = the 1489 of the prior gate plus the 14 new tests. No test was deleted,
skipped, or loosened, and no existing assertion was weakened. One existing
test was **strengthened**: `tests/test_cli.py`'s man-page enumeration now
includes `("retire",)`.

### One intermediate full-suite failure, and why it was not a defect

The first full-suite run after the code landed was
`Ran 1503 tests — FAILED (failures=12) — EXIT:1`. All twelve were
`tests.test_manifest` assertions naming exactly `floati/cli.py`,
`floati/copy.py`, `floati/helptext.py`, and `floati/registry.py`:
`bundle-manifest.v0.json` pins a SHA-256 per deployable file, and those four
files had changed. The manifest was regenerated from
`floati.manifest._deployable_paths` plus real on-disk digests — no digest was
typed by hand. The regeneration added no path and removed no path; the
resulting diff is 4 lines changed, one per file. `test_manifest` then ran
`Ran 24 tests`, `OK`, EXIT:0, and the full suite ran clean as recorded above.
Reported as a required product step that was initially missed, not laundered
into the GREEN numbers.

No `sandbox initialization failed` lines were printed and no skips were
reported in either authoritative run. No contamination was observed.

## The live-root fence held

The verb was exercised only against `tempfile` roots inside tests and one
throwaway `mktemp -d` root for a manual end-to-end check, which was deleted.

`~/.floati-bus/puddle-fleet/registry/entries.jsonl` was **read only**, to
prove it is unchanged:

- SHA-256:
  `411f4c31f1b73c2afc1d819465f9ebd9765510d9226b2de8b98007d9cb8a3774`
- 6 rows, every one `state: "active"`: `lane-slipway`, `fable`, `lane-app`,
  `puddle-floati-architect`, `lane-floati`, `lane-puddle-relief`
- count of `"state":"retired"` rows: 0

The dormant rows named in issue #1 — `fable` and the older lane rows — are
still active and were **not** retired. Retiring them is a separate
architect-gated exercise that happens only after this verb passes its gate.

## Explicitly not claimed

- **Not done: the live retirement of the dormant rows.** No write of any kind
  reached `~/.floati-bus/puddle-fleet`. That exercise is not started and is
  not requested by this packet.
- Not claimed: any push to `main`, merge, publication, release, or tag. Only
  `lane/fleet-ops-window` was pushed.
- Not built: controller-retires-another-node. Issue #1 requires a ruling
  first, so no actor, authority, or delegation input exists on the verb.
- Not built: re-registration of a retired node. `register` still refuses any
  node that appears in the ledger at all, so a retired node cannot currently
  be re-registered through the public writer, while
  `active_node_ids` would honor a re-activation row. This asymmetry predates
  this change and is left exactly as found; it is reported, not fixed.
- Not authored: any visible retirement prose. See the copy section — four
  help strings and the root-page command list remain the architect's.
- Not touched: the conformance retired-row helper
  (`floati/conformance.py::_append_retired_registry_entry`) and the
  `tests/test_registry_events.py` helper. They remain as they were; the new
  public writer does not replace them and no test was re-pointed onto it.
- Not touched: items #2 (doctor bus-only fleet profile) and #3
  (installer-shadow docs). Not started.
- Out of scope by fence (R7) and untouched: the wake daemon, installer,
  publication checklist, and herdr.
- Standing from the prior item, per the architect's ruling: the `lane/hm0`
  dirty worktree parked at `stash@{0}` is still parked and untouched. The
  Makefile's missing test/selftest targets remain unfixed by ruling.
- No network call was made beyond `git` and `gh` against the repository's own
  origin. No telemetry.

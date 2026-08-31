# WS-B — Trash-only purge command activation

Status: **DRAFT - exact-head review pending**

Final activation base: `36d8e9bc0f23703cbcc6ef87595c80febec5aa81`
Activation predecessor: `11b0342dc3dc8aabfc43c8925f3bc2e150165102`
Branch: `lane/ws-b-admin`
Queue dispatch: `msg-01a048e1cbf27a46baa47e7c4134d50e`
Dark writer evidence: `docs/evidence/WS-B-PURGE-TRASH-ONLY.md`

## Scope

The dark Trash-only writer and its seven-finding repair were already on the
activation base. Fable's copy restamp was also already banked in `097c32d`:
the preview and three argparse strings no longer carry DRAFT copy, and the
preview states the Trash-only, no-delete bound truthfully for both dry-run and
post-move evidence.

This row wires that existing `floati.purge.register_cli` seam into the one
canonical product parser, adds static help composed from the restamped purge
copy, regenerates the copy ledger, and regenerates the exact bundle manifest
last. The production diff is limited to `floati/cli.py`,
`floati/helptext.py`, generated `docs/COPY-LEDGER.md`, and generated
`bundle-manifest.v0.json`. The activation test and this evidence record are new
files. `floati/purge.py`, uninstall, wake, context, role management, and every
purge receipt/refusal byte remain unchanged.

A fresh pre-review fetch found `origin/main` had advanced from `db98241` to
`0f3e185` with the ship-day wake-session and Regatta train. That tip was merged
before review. The shared parser/help/copy changes merged without conflict and
retained one purge registration plus the static purge page. The bundle manifest
had the expected two digest conflict rows for the independently changed parser
and help bytes; it was regenerated mechanically from the reconciled deployable
tree, after the copy ledger and after every deployable edit. All focused and
full gates below were then rerun against that combined tree.

A second pre-push fetch found `origin/main` at `bf0714b`, now including the
gated E2 row, SD-1 uninstall round-trip repair, and live waiter activation.
That tip was also merged before publication. The only conflict was the
generated `floati/helptext.py` digest row in the bundle manifest; copy and
manifest were regenerated mechanically again. The expanded adjacent bank now
includes deploy as well as uninstall so the current install-owned set and
deletion boundary are tested together.

A third pre-push fetch found `origin/main` at `77058b7` with the SD-2
cross-version reader law and R4 approval-panel brief. It had no purge, parser,
help, copy, uninstall, or deploy overlap; the merge was clean and direct
manifest verification stayed exact. The two-test SD-2 reader fixture was added
to the purge/packaging bank, and every focused and full gate below was rerun on
that combined tree.

A fourth pre-push fetch found `origin/main` at `36d8e9b` with NC-1
`NO_COLOR` and the OW-1 seat-wake correction. It changed only TUI/Regatta
source, tests, docs, and generated manifest bytes relative to the prior base;
the merge was clean and direct manifest verification stayed exact. The ruled
53-test NC-1/Regatta bank and every other final gate below were run on this
base.

## Mandatory RED

The activation test was committed alone as `1da87f8` (`test: pin purge
activation seam`) and then executed before any production wiring changed:

`python3 -B -m unittest -v tests.test_purge_activation`

Four tests ran. Manifest exactness passed because deployable bytes were still
unchanged. The other three activation surfaces proved dark: the real parser
raised `arguments_invalid` for `purge`, the real CLI emitted the same typed
refusal with exit 20 instead of reaching the dry-run writer, and static purge
help was absent. Unittest reported two failures and one error, all at those
intended seams.

After parser/help wiring but before manifest regeneration, the same bank had
three passing tests and one intended packaging failure naming only
`digest_mismatch:floati/cli.py` and
`digest_mismatch:floati/helptext.py`. The copy ledger was then regenerated
mechanically and the bundle manifest was regenerated last from
`_deployable_paths` and final SHA-256 bytes.

## GREEN and regression evidence

- Activation + purge + seven-finding repair + copy/manifest + SD-2 reader
  integration bank: **61 tests, OK**.
- NC-1/Regatta bank: **53 tests, OK**.
- Adjacent CLI + administration + CLI workflows + uninstall + deploy bank:
  **81 tests, OK**.
- Source-scrub/name-sweep bank: **21 tests, OK**.
- Full canonical `python3 -B -m unittest discover`: **2,027 tests, OK**,
  228.577 seconds, exit 0.
- Direct manifest verification: `[]`, exit 0.
- Direct generated-tree scrub: `[]`, exit 0.
- Direct Git-history-note scrub: `[]`, exit 0.
- `git diff --check`: no output, exit 0.

The activation test patches only the account Trash resolver inside its
isolated `/tmp` fixture. It executes `main` with `--dry-run`, proves
the source bytes and root remain in place, proves Trash remains empty, and
requires the real writer's `trash_only: true` evidence. It also proves the
purge seam is registered exactly once and that static help carries the
restamped no-delete contract without a DRAFT stamp.

The full run emitted expected argparse and sandbox-degradation diagnostics
from negative tests and pre-existing `ResourceWarning` lines from the roster
parity battery. The authoritative unittest result was exit 0 with no failures
or errors.

## Publication boundary

This is command-family activation only. It does not change the Trash-only
writer, add a hard-delete primitive, widen caller authority, merge purge into
uninstall, install or deploy a bundle, delete user data, or claim public
release. A successful command artifact remains evidence only for the exact
preview or Trash move it records.

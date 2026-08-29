# POST-V1 Wake/Hold whole-review evidence

## Identity and scope

- Whole-review base: `1b4b5639b946574e0fd196348b5c57a739c1b2a2`.
- Exact reviewed source head: `ec347170bc6f9024b541ab788fa2f0f6e4f36809`.
- Exact closed candidate: `097867580d79d6fc7874d0dc55a689b4f4ab1669`.
- Evidence closure is a separate evidence-only commit; it contains no source
  changes and therefore cannot alter the reviewed source bytes.

Changed paths against the whole-review base:

- `bundle-manifest.v0.json`
- `docs/evidence/POST-V1-WAKE-HOLD.md`
- `docs/superpowers/plans/2026-08-13-wake-hold-refinements.md`
- `docs/superpowers/specs/2026-08-13-wake-hold-refinements-design.md`
- `schemas/v1/wake-decision-artifact.schema.json`
- `schemas/v1/wake-hold-receipt-record.schema.json`
- `schemas/v1/wake-path-status-artifact.schema.json`
- `slip/cli.py`
- `slip/cursor.py`
- `slip/events.py`
- `slip/jsonl.py`
- `slip/projection.py`
- `slip/records.py`
- `slip/tui.py`
- `slip/wake.py`
- `slip/wake_hold.py`
- `tests/test_cli.py`
- `tests/test_copy_ledger.py`
- `tests/test_cursor.py`
- `tests/test_gauntlet_concurrency.py`
- `tests/test_gauntlet_crash.py`
- `tests/test_gauntlet_fuzz.py`
- `tests/test_manifest.py`
- `tests/test_one_shot_wake.py`
- `tests/test_registry_events.py`
- `tests/test_schemas.py`
- `tests/test_wake_hold.py`

No `schemas/v0`, `bundle/c7.1`, or `bundle/c7.2` bytes changed.

## RED and GREEN chronology

1. Six deterministic whole-review tests were added before production edits.
   The combined unchanged-source RED ran six tests: two assertion failures and
   four behavior errors. It established that a substituted absent wake parent
   could redirect registration, an acknowledgment followed by retraction was
   rejected as unavailable, a valid empty legacy delivery receipt poisoned
   replay, the sealed hold writer skipped its record cap, and the evidence did
   not yet name the whole-review base or inventory. The hidden parser spelling
   control was already green.
2. Registration now holds a no-follow wake-parent descriptor, creates the
   deterministic leaf relative to it, validates descriptor identity around
   write/fsync, and failure-quarantines only the exact created leaf. A replaced
   leaf is retained and refused; no replacement pathname is deleted.
3. Replay accepts valid historical acknowledgment evidence after retraction;
   retraction remains the resulting state. Empty legacy delivery receipts are
   no-ops. The sealed wake-hold transaction enforces `MAX_LEDGER_RECORDS`
   before encoding/appending a new row, while exact no-append retries remain
   available.
4. The final pre-evidence focused bank passed `122/122` in `7.561 s`; it
   included Wake/Hold, one-shot registration, CLI, JSONL, and manifest
   controls. The deployable manifest was regenerated mechanically from the
   deployable-path inventory after the final source bytes.
5. Fix Round 2 added one subprocess RED against source head
   `789106333e727d95763a6d510bc9b9ea448b5901`. It failed once in `0.178 s`:
   the typed exit was correctly nonzero, but argparse's invalid-command
   choices exposed both hidden parser entries. The minimal fix filters only
   top-level invalid-command diagnostics. Exact hidden dispatch and required
   arguments are unchanged, and the command has no alias or environment-root
   fallback. The deployable manifest was mechanically regenerated after
   `slip/cli.py` changed.
6. Fix Round 3 began from exact clean evidence head
   `f23bed748a5b373aced1c9da8a9ffe767eedceb5`. Its isolated combined RED ran
   two tests in `0.027 s` and reported three registrar seam failures plus one
   cursor error: short write, write exception, and the post-write `fstat`
   failure stranded the deterministic leaf, while a later lawful retraction
   invalidated an already-presented acknowledgment. The same two tests passed
   in `0.032 s` after the minimal repair. The manifest RED then named only
   `digest_mismatch:slip/cursor.py` and `digest_mismatch:slip/wake.py`; it was
   mechanically regenerated after the final source bytes.
7. Fix Round 4 began from exact clean evidence head
   `e325e41dc0abd32e768523853e9c15e327e39626`. Its first-post-create-`fstat`
   RED ran one test in `0.004 s` and failed once because the zero-byte
   deterministic leaf remained at its path. The replacement-at-fault control
   already retained the unrelated replacement. The same test passed in
   `0.005 s` after rollback recovered identity only from the still-held
   created descriptor. The manifest RED named only
   `digest_mismatch:slip/wake.py`; the manifest was then mechanically
   regenerated.
8. Fix Round 5 began from exact clean evidence head
   `c22943026bfb95dec459a5455bec9b34589450db`. Its post-check/pre-rename race
   RED ran one test in `0.003 s` and failed once because failure quarantine
   moved an unrelated replacement away from the deterministic leaf. The same
   test passed in `0.004 s` after a quarantined identity mismatch restored the
   retained replacement with a same-directory, no-overwrite hard link and
   verified both observed names before refusing. The manifest RED named only
   `digest_mismatch:slip/wake.py`; the manifest was then mechanically
   regenerated.

## Behavioral result

- First presentation produces fresh work and a hold receipt; an exact retry
  reuses it only while the selected work remains current.
- Acknowledgment followed by retraction yields `caught_up` for both the old
  and a new key. The acknowledgment receipt remains durable history.
- A valid legacy `delivery_receipt` with `item_ids=[]` is neutral; a following
  message remains fresh and can produce `fresh_work`.
- A full sealed delivery ledger rejects another append before bytes are
  written. The previous ledger remains readable, and its exact retry remains
  stable.
- Public help, generated copy, and aliases do not expose `wake-evaluate`; its
  hidden parser still requires explicit `--root`, `--as`, and
  `--idempotency-key`. Invalid-command details now list public commands only
  and expose neither `wake-evaluate` nor `wake-callback`.
- Registration continues to report only local `written_unloaded` testimony;
  it makes no host activation claim.
- An acknowledgment remains valid historical evidence after a later lawful
  retraction. Retraction wins current pending state, so ordinary presentation
  and Wake/Hold return no item and `caught_up`; unknown or unpresented
  acknowledgment still refuses.
- Registration captures the newly created leaf's descriptor identity before
  writing. Short or exceptional writes and later `fstat`/durability failures
  leave the deterministic path absent or move that exact created file into a
  bounded same-directory failure quarantine. A replacement is retained and
  refused, and an exact retry can register lawfully.
- If the first identity read itself fails, rollback retains the created file
  descriptor and retries identity observation through that descriptor before
  any pathname move. A matching no-follow leaf may enter failure quarantine;
  a replacement is preserved and refused. If descriptor identity remains
  unavailable, registration refuses without trusting or moving the pathname.
- If a replacement arrives after the pre-rename identity check, the moved
  replacement remains as bounded quarantine evidence. Registration attempts
  a no-overwrite hard-link restoration to the deterministic leaf, verifies
  both observed identities, and refuses. If restoration encounters a new leaf
  or cannot be verified, it refuses without overwriting or deleting unrelated
  bytes. This is local observation, not a pathname-immutability claim.

## Final-byte gates

| Gate | Result |
| --- | --- |
| Focused Wake/Hold/one-shot/CLI/JSONL/manifest bank | `152/152`, 7.793 s |
| Complete concurrency/crash/fuzz gauntlets | `93/93`, 26.327 s |
| Full `python3 -m unittest -q` | exit 0, 30.2 s |
| `python3 -m slip.selftest` | exit 0, 27.0 s |
| Private-cache `py_compile` | exit 0 |
| Direct manifest verification | `[]` |
| Frozen v0/C7 comparison | exit 0 |

Fix Round 2 final-source gates:

| Gate | Result |
| --- | --- |
| Targeted diagnostic/help/copy controls | `3/3`, 0.329 s |
| Focused CLI/Wake/Copy/manifest bank | `132/132`, 7.973 s |
| Live near-miss source scrub | exit 20; public choices only |
| Full `python3 -m unittest -q` | exit 0, 30.2 s |
| `python3 -m slip.selftest` | exit 0, 26.8 s |
| Private-cache `py_compile` | exit 0 |
| Direct manifest verification | `[]` |
| Frozen v0/C7 comparison and `git diff --check` | exit 0 |

Fix Round 3 final-source gates:

| Gate | Result |
| --- | --- |
| Combined targeted GREEN | `2/2`, 0.032 s |
| Focused cursor/events/Wake/Hold/one-shot/CLI/manifest bank | `152/152`, 8.677 s |
| Complete concurrency/crash/fuzz gauntlets | `93/93`, 27.052 s |
| Full `python3 -m unittest -q` | exit 0; `1,409` tests discovered |
| `python3 -m slip.selftest` | exit 0 |
| Private-cache compile | exit 0 |
| Direct manifest verification | `[]` |
| Frozen v0/C7 comparison and `git diff --check` | exit 0 |

Fix Round 4 final-source gates:

| Gate | Result |
| --- | --- |
| First-post-create-`fstat` targeted GREEN | `1/1`, 0.005 s |
| Focused one-shot/Wake/Hold/cursor/events/CLI/manifest bank | `153/153`, 8.484 s |
| Full `python3 -m unittest -q` | exit 0; `1,410` tests discovered |
| `python3 -m slip.selftest` | exit 0 |
| Private-cache compile | exit 0 |
| Direct manifest verification | `[]` |
| Frozen v0/C7 comparison and `git diff --check` | exit 0 |

Fix Round 5 final-source gates:

| Gate | Result |
| --- | --- |
| Post-check/pre-rename targeted GREEN | `1/1`, 0.004 s |
| Whole one-shot/Wake/Hold/cursor/events/CLI fault matrix | `131/131`, 8.607 s |
| Final focused bank including manifest | `154/154`, 8.512 s |
| Full `python3 -m unittest -q` | exit 0; `1,411` tests discovered |
| `python3 -m slip.selftest` | exit 0 |
| Private-cache compile | exit 0 |
| Direct manifest verification | `[]` |
| Frozen v0/C7 comparison and `git diff --check` | exit 0 |

The independent current-task audit receipt is banked here as durable evidence:
READY, with Critical `0`, Important `0`, and Minor `0`; its focused bank passed
`154/154` in `8.323 s`, the exact replacement-race probes were green, direct
manifest verification returned `[]`, and private-cache compile plus frozen-byte
and diff checks exited zero. That receipt is bounded to the Wake/Hold candidate;
it is not Effect, host-activation, publication, or release proof.

## Limits

These local controls do not prove an actual Codex task wake, launchd load,
host activation, resident listener liveness, callback delivery, or Herdr
behavior. `written_unloaded` is local plist testimony only.
